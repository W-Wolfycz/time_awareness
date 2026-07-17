"""
time_awareness — 时间感知增强

为 LLM 注入：
1. 静态时间规则 → system_prompt 末尾（保 Prompt 缓存前缀稳定）
2. 当前时段状态 / 当日事项 → extra_user_content_parts + mark_as_temp
   （注入到当前轮 user 消息末尾，不持久化到历史）

日历管理通过聊天命令 `/calendar show|add|del|create|export|import|help`。
日程表管理通过 `/schedule create|show|help`。

主动提醒：
- 当日日历事项在指定时刻主动发送到白名单会话
- LLM 可通过 schedule_followup 工具自行安排"X 分钟后再说"
- 发送阶段走 OnDecoratingResultEvent 装饰链（允许其他分段插件接管分段）
"""

import asyncio
import datetime
import os

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.message import TextPart

from .constants import DEFAULT_TIME_GUIDANCE_PROMPT
from .commands.calendar_helpers import (
    category_short_tag,
    describe_repeat,
    find_event_by_short_id,
    parse_category_input,
    parse_event_date,
    parse_year_month,
)
from .log import configure as configure_log
from .log import logger, tag
from .core.builtin_manager import BuiltinManager
from .core.calendar_manager import CalendarManager
from .core.calendar_store import CalendarStore, REPEAT_FOREVER
from .core.last_chat_tracker import LastChatTracker
from .core.scheduler import DueGroup, ReminderScheduler, TriggerResult
from .integrations.astrbot_sender import AstrBotMessageSender
from .integrations.persona_resolver import resolve_persona_prompt
from .llm.calendar_generator import (
    DEFAULT_MAX_GENERATE,
    build_system_prompt,
    generate_calendar_events,
)
from .llm.schedule_generator import generate_schedule
from .services.time_context_service import TimeContextService
from .services.reminder_service import ReminderService
from .utils.time_utils import find_active_schedule_slot, get_now
from .web_api import register_web_apis

PLUGIN_DATA_DIR_NAME = "time_awareness"


class CalendarPlusPlugin(Star):
    """时间感知 + 智能日历 + 主动提醒。"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}

        # 日志提级：debug → info
        # 日志前缀带 bot 实例标识：区分多 bot 共存场景
        log_cfg = self.config.get("log", {})
        configure_log(
            debug_to_info=log_cfg.get("debug_to_info", False),
            log_with_bot_id=log_cfg.get("log_with_bot_id", False),
        )

        # 启动后检测时段重叠（仅 warning）
        self._check_schedule_overlap()

        # 数据目录
        try:
            self.data_dir = str(StarTools.get_data_dir(PLUGIN_DATA_DIR_NAME))
        except Exception as e:
            base = os.path.join(os.getcwd(), "data", "plugin_data", PLUGIN_DATA_DIR_NAME)
            os.makedirs(base, exist_ok=True)
            self.data_dir = base
            logger.warning(
                f"{tag()} ⚠️ StarTools.get_data_dir 不可用，回退到 {base}: {e}"
            )

        self.calendar_store = CalendarStore()
        self.calendar_manager = CalendarManager(self.data_dir, store=self.calendar_store)
        self.builtin_manager = BuiltinManager(self.data_dir)
        self.last_chat_tracker = LastChatTracker(self.data_dir)
        self.time_context = TimeContextService(
            config=self.config,
            astrbot_config_provider=self._astrbot_config,
            calendar_store=self.calendar_store,
            last_chat_tracker=self.last_chat_tracker,
        )
        # 调度器：trigger_callback 委托给 self._on_due；
        # now_provider 让 scheduler 用与 fire_at 同时区的 now（避免 aware/naive 比较报错）
        self.scheduler = ReminderScheduler(
            self.data_dir,
            self._on_due,
            now_provider=lambda: get_now(self.config, self._astrbot_config()),
        )
        self.message_sender = AstrBotMessageSender(self.context)
        self.reminder_service = ReminderService(
            context=self.context,
            config=self.config,
            scheduler=self.scheduler,
            time_context=self.time_context,
            message_sender=self.message_sender,
            last_chat_tracker=self.last_chat_tracker,
            astrbot_config_provider=self._astrbot_config,
            ensure_builtin_fresh=self._check_builtin_fresh,
        )
        self._apply_reminder_config()
        # 每日 0 点扫描当天事项的 asyncio task
        self._daily_scan_task: asyncio.Task | None = None

        logger.info(f"{tag()} 插件已初始化，数据目录: {self.data_dir}")

    async def initialize(self):
        """AstrBot 启动时调用：加载数据 + 启动调度。"""
        self.calendar_manager.load()
        self.last_chat_tracker.load()

        # v1.7.1 迁移：黄历配置统一到 time_awareness.almanac_enabled
        # 历史位置 calendar.builtin_events.almanac（v1.6.0）/ calendar.almanac_enabled（v1.7.0）
        # 启动时自动迁到新位置（紧邻农历配置）。AstrBot 加载 schema 后会剥离已删除字段，
        # 但用户可能在配置文件里有残留——这里读 raw 字典兜底检测。
        self._migrate_legacy_almanac_config()

        # 内置事件：跨年/分类开关变更时自动 regen
        # 注：v1.7.0 后 almanac 不再在 builtin categories 内，旧 yaml 的
        # enabled_categories 含 almanac，必然触发 stale，首次启动 regen 会自动清掉黄历条目
        try:
            now = get_now(self.config, self._astrbot_config())
            year = now.year
            categories = self._enabled_builtin_categories()
            if categories:
                count = self.builtin_manager.ensure_fresh(year, categories)
                logger.info(f"{tag()} 📅 内置事件已就绪: {count} 条 (year={year})")
                # 加载到内存单例
                builtin_data = self.builtin_manager.load_raw()
                self.calendar_store.set_builtin_events(builtin_data.get("events") or [])
            else:
                self.calendar_store.set_builtin_events([])
        except Exception as e:
            logger.error(f"{tag()} ❌ 内置事件初始化失败: {e}")
            self.calendar_store.set_builtin_events([])

        self._apply_reminder_config()
        self.scheduler.load()

        reminder_cfg = self.config.get("reminder", {})
        if reminder_cfg.get("enable_reminder", False):
            # 启动时先扫一遍今天的事项入队（错过当日提醒时刻仍可补发）
            self._scan_and_enqueue_today_events()
            self.scheduler.start()
            # 启动每日 0 点扫描循环
            self._daily_scan_task = asyncio.create_task(self._daily_scan_loop())

        # 注册 Plugin Pages 后端 API（鉴权继承 AstrBot 主 webui）
        register_web_apis(self.context, self)

        logger.info(f"{tag()} ✅ 初始化完成")

    async def terminate(self):
        """AstrBot 卸载/关闭时调用：停调度循环 + 每日扫描。"""
        if self._daily_scan_task is not None:
            self._daily_scan_task.cancel()
            try:
                await self._daily_scan_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"{tag()} ⚠️ 每日扫描 task 停止异常: {e}")
            self._daily_scan_task = None
        try:
            await self.scheduler.stop()
        except Exception as e:
            logger.warning(f"{tag()} ⚠️ 调度器停止异常: {e}")
        try:
            await self.last_chat_tracker.close()
        except Exception as e:
            logger.warning(f"{tag()} ⚠️ 上次对话时间刷盘异常: {e}")
        logger.info(f"{tag()} ✅ 已终止")

    async def _daily_scan_loop(self):
        """每日 0 点扫今天的事项入队 + 跨年自动重新生成内置事件。"""
        try:
            while True:
                try:
                    now = get_now(self.config, self._astrbot_config())
                    # 下一个 0 点
                    next_midnight = (now + datetime.timedelta(days=1)).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    # +5s 缓冲：asyncio.sleep 实际唤醒可能早几毫秒，若卡在午夜前醒来
                    # _scan_and_enqueue_today_events 的 cutoff 防御会把 fire_at（昨日 09:00）
                    # 误判为超出 max_late 容忍窗口而跳过，导致当日提醒整日不入队。
                    # +5s 让唤醒一定落在次日，max(60, ...) 仍是异常恢复下的下限。
                    sleep_sec = max(60, (next_midnight - now).total_seconds() + 5)
                    await asyncio.sleep(sleep_sec)
                    # 醒来，扫今天的入队（add_calendar_task 内部会唤醒调度器）
                    self._scan_and_enqueue_today_events()
                    # 跨年时自动 regen 内置事件
                    self._check_builtin_fresh()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"{tag()} ❌ 每日扫描异常（继续循环）: {e}")
                    await asyncio.sleep(60)
        except asyncio.CancelledError:
            logger.info(f"{tag()} 每日扫描循环已取消")

    def _check_builtin_fresh(self) -> None:
        """检查内置事件是否需要重新生成（跨年或分类变更）。"""
        try:
            categories = self._enabled_builtin_categories()
            if not categories:
                self.calendar_store.set_builtin_events([])
                return
            now = get_now(self.config, self._astrbot_config())
            if not self.builtin_manager.is_stale(now.year, categories):
                return
            count = self.builtin_manager.regenerate(now.year, categories)
            if count >= 0:
                builtin_data = self.builtin_manager.load_raw()
                self.calendar_store.set_builtin_events(builtin_data.get("events") or [])
                logger.info(
                    f"{tag()} 📅 跨年/配置变更自动 regen 内置事件: "
                    f"year={now.year} events={count}"
                )
        except Exception as e:
            logger.error(f"{tag()} ❌ 内置事件跨年 regen 失败: {e}")

    def _migrate_legacy_almanac_config(self) -> None:
        """把旧的黄历配置位置自动迁移到 ``time_awareness.almanac_enabled``。

        AstrBot 加载 schema 时会剥离已删除字段，但用户磁盘配置可能仍有残留——
        这里从 ``self.config`` 读残留值，检测到旧位置为 true 且新位置未启用时
        自动启用并落盘。

        幂等：已迁移过或无旧配置时不做任何事。
        """
        try:
            ta = self.config.get("time_awareness", {})
            if not isinstance(ta, dict):
                ta = {}
                self.config["time_awareness"] = ta
            if bool(ta.get("almanac_enabled", False)):
                return  # 新开关已启用——无需迁移

            legacy_sources = []
            cal = self.config.get("calendar", {})
            if isinstance(cal, dict):
                be = cal.get("builtin_events", {})
                if isinstance(be, dict) and be.get("almanac", False):
                    legacy_sources.append("calendar.builtin_events.almanac")
                if cal.get("almanac_enabled", False):
                    legacy_sources.append("calendar.almanac_enabled")

            if not legacy_sources:
                return

            ta["almanac_enabled"] = True
            self._try_save_config()
            logger.info(
                f"{tag()} 🔄 检测到旧黄历配置 {' / '.join(legacy_sources)}=true，"
                f"已自动迁移到 time_awareness.almanac_enabled=true"
            )
        except Exception as e:
            logger.warning(f"{tag()} ⚠️ almanac 配置迁移失败（不致命）: {e}")

    # ==================== 提醒配置 ====================

    def _apply_reminder_config(self) -> None:
        """把 reminder 组配置下发给调度器。"""
        reminder_cfg = self.config.get("reminder", {})
        try:
            max_late = int(reminder_cfg.get("reminder_max_late_minutes", 60) or 60)
        except (TypeError, ValueError):
            max_late = 60
        self.scheduler.set_max_late_minutes(max_late)
        try:
            max_attempts = int(reminder_cfg.get("reminder_max_attempts", 3) or 3)
        except (TypeError, ValueError):
            max_attempts = 3
        self.scheduler.set_max_attempts(max_attempts)

    def _reminder_targets(self) -> list:
        """白名单 UMO 列表（已 strip、过滤空项）。"""
        return self.reminder_service.targets()

    async def _resolve_persona_prompt(
        self, umo: str, event: AstrMessageEvent | None = None
    ) -> str:
        """取当前会话生效的 Persona prompt；实现位于 AstrBot 适配层。"""
        return await resolve_persona_prompt(self.context, umo, event=event)

    # ==================== 插件间 API ====================

    async def create_external_task(
        self,
        session: str,
        fire_at: datetime.datetime,
        hint: str,
        target_user_id: str = "",
        source: str = "",
    ) -> str:
        """供其他插件调用的任务创建 API（与 WebUI 手动添加任务同型，``kind=user``）。

        调用方示例（在其他插件里）::

            star_meta = self.context.get_registered_star("time_awareness")
            if star_meta and star_meta.star_cls:
                ta = star_meta.star_cls
                task_id = await ta.create_external_task(
                    session="aiocqhttp:GroupMessage:10001",
                    fire_at=<aware datetime>,
                    hint="提醒内容",
                    source="plugin:my_plugin",
                )

        Args:
            session: 目标会话 UMO（必须在 ``reminder_targets`` 白名单内）。
            fire_at: 触发时间。aware/naive 均可——naive 按插件当前有效时区
                （与 ``get_now`` 同源）解释；内部用 ``coerce_to_aware`` 规整。
            hint: 任务提示文本（到点后注入到 LLM 的 user prompt），1-500 字。
            target_user_id: 群聊 @ 目标用户 ID（可空，≤64 字符）。
            source: 调用方标识（仅用于日志追踪，如 ``plugin:my_plugin``）。

        Returns:
            task_id: 16 进制 UUID（32 字符），可用于 ``scheduler.cancel_task`` 取消。

        Raises:
            ValueError: session 为空或不在白名单 / hint 为空或过长 /
                fire_at 不在 [now+60s, now+365d] 窗口内。
        """
        return self.reminder_service.create_external_task(
            session=session,
            fire_at=fire_at,
            hint=hint,
            target_user_id=target_user_id,
            source=source,
        )

    # ==================== 调度回调 ====================

    async def _on_due(self, group: DueGroup) -> TriggerResult:
        """到期任务组回调；显式返回是否可以安全重试。"""
        task_summary = ",".join(f"{t.kind}:{t.id[:8]}" for t in group.tasks)
        late_info = f" late={group.late_minutes}min" if group.is_late else ""
        logger.info(
            f"{tag()} 🔔 触发到期任务: session={group.session} "
            f"tasks={len(group.tasks)} [{task_summary}]{late_info}"
        )
        return await self._fire_with_llm(
            group.session,
            group.tasks,
            group.is_late,
            group.late_minutes,
        )

    async def _fire_with_llm(
        self,
        session: str,
        tasks: list,
        is_late: bool,
        late_minutes: int,
    ) -> TriggerResult:
        """兼容旧调用方；主动提醒编排已迁移到 ``ReminderService``。"""
        return await self.reminder_service.deliver(
            session=session,
            tasks=tasks,
            is_late=is_late,
            late_minutes=late_minutes,
        )

    # ==================== 每日日历扫描 ====================

    def _scan_and_enqueue_today_events(self) -> int:
        """为每个白名单 session 入队今日提醒（每 session 一条；事件内容在 fire 时查）。

        注意：不在扫描阶段读取事件——用户在 0 点后任何对日历/分类的改动
        （add/del/create/import/builtin_regen/开关分类）都会被 fire 时的动态查询覆盖。
        """
        reminder_cfg = self.config.get("reminder", {})
        if not reminder_cfg.get("enable_reminder", False):
            return 0
        targets = self._reminder_targets()
        if not targets:
            logger.warning(f"{tag()} ⚠️ reminder 启用但 reminder_targets 为空，跳过扫描")
            return 0

        now = get_now(self.config, self._astrbot_config())
        today = now.date()

        # 计算 fire_at：今日 reminder_time，若已过则立即入队（调度器会立刻触发）
        time_str = reminder_cfg.get("reminder_time", "09:00") or "09:00"
        try:
            hh, mm = time_str.split(":")
            fire_at = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        except (ValueError, AttributeError):
            logger.warning(f"{tag()} ⚠️ reminder_time 格式错误: {time_str}，回退 09:00")
            fire_at = now.replace(hour=9, minute=0, second=0, microsecond=0)

        # 防御：跨日扫描在午夜前几毫秒醒来时，get_now() 仍是昨天，
        # replace(hour=9) 会得到昨天 09:00（已过去 15 小时）。
        # 与 scheduler._cleanup_expired 行为对齐：超过 max_late 容忍窗口直接跳过，
        # 等下一个扫描周期（次日 0 点）重新入队。
        max_late = int(reminder_cfg.get("reminder_max_late_minutes", 60) or 60)
        cutoff = now - datetime.timedelta(minutes=max_late)
        if fire_at < cutoff:
            logger.warning(
                f"{tag()} ⚠️ 跳过今日提醒入队：fire_at={fire_at.isoformat()} "
                f"now={now.isoformat()} 已超出 {max_late} 分钟容忍窗口"
                f"（疑似跨日扫描时钟漂移导致 now 仍是昨天）"
            )
            return 0

        enqueued = 0
        for session in targets:
            if self.scheduler.has_calendar_task_for_session_today(session, today):
                continue
            self.scheduler.add_calendar_task(session=session, fire_at=fire_at)
            enqueued += 1
        if enqueued > 0:
            logger.info(f"{tag()} 📅 今日提醒入队 {enqueued} 条 session")
        return enqueued

    # ==================== AstrBot 全局配置 ====================

    def _astrbot_config(self):
        try:
            return self.context.get_config()
        except Exception:
            return None

    # ==================== 时间引导 / 状态解析 ====================

    def _time_guidance_enabled(self) -> bool:
        """时间引导总开关（仅控制注入与否；规则文本与数据格式硬编码在 constants）。"""
        return self.time_context.time_guidance_enabled()

    def _build_dynamic_time_data(self, session: str | None = None) -> str:
        """动态时间数据：时段状态 + 工作日性质 + 农历 + 当日事项 + 黄历 + 上次对话时间。

        所有标签受 ``time_guidance_enabled`` 总开关约束（关掉则不调本方法）；
        子标签各自有独立开关。

        - ``<SCHEDULE_STATE>``：总是输出（rule 3 显式处理「未指定」语义）
        - ``<WORKDAY_STATE>``：受 ``workday_state_enabled`` 控制；总是有值（chinese_calendar
          未覆盖年份时降级为「周X」）
        - ``<LUNAR_STATE>``：受 ``lunar_state_enabled`` 控制；``lunar_python`` 异常时省略
        - ``<TODAY_EVENTS>``：仅在有事项时输出；空时省略，让 LLM 从缺失推断「今日无特殊事件」
        - ``<ALMANAC_STATE>``：受 ``time_awareness.almanac_enabled`` 控制；``lunar_python`` 异常时省略
          （v1.7.0 从 ``<TODAY_EVENTS>`` 中分离，避免一年 365 条占用事件清单）
        - ``<LAST_CHAT>``：受 ``last_chat_enabled`` 控制；首次对话无记录时省略

        Args:
            session: ``unified_msg_origin``。正常对话路径由 ``inject_time_context``
                     传入；主动消息路径由 ``_fire_with_llm`` 传入。
        """
        return self.time_context.build_dynamic(session)

    def _last_chat_enabled(self) -> bool:
        return self.time_context.last_chat_enabled()

    # ==================== Dashboard 时间感知面板 ====================

    def build_dashboard_time_info(self) -> dict:
        """组装 dashboard 顶部时间感知面板所需数据（供 ``web_api /dashboard/stats`` 调用）。

        所有字段在异常时降级为空串/默认值，确保前端不崩。
        返回字典字段：

        - ``now_iso`` / ``now_display`` / ``time_display`` / ``date_display``：当前时间多种格式
        - ``greeting``：时段问候语（早上/上午/中午/下午/傍晚/晚上/深夜/凌晨 好~）
        - ``weekday_display``：周X
        - ``workday_kind`` / ``workday_label``：工作日性质（chip 配色用 kind）
        - ``lunar_display`` / ``almanac_display``：农历/黄历文本（关闭或异常时为空）
        - ``lunar_year_chip`` / ``lunar_month_day``：WebUI 用的农历拆分字段
          （"丙午🐴年" / "五月廿六"）；lunar 关闭时两字段为空
        - ``lunar_enabled`` / ``almanac_enabled``：开关状态
        """
        return self.time_context.build_dashboard_time_info()

    # ==================== 内置日历事件 ====================

    def _enabled_builtin_categories(self) -> list:
        """根据配置返回启用的内置事件分类列表。

        受总开关 ``calendar.enable_builtin_events`` 与各分类子开关共同控制。
        5 类开关位于 ``calendar.builtin_events`` 子组。

        注意：黄历（v1.7.0 前 ``almanac`` 子项，v1.7.0 ``calendar.almanac_enabled``）已迁移到 ``time_awareness.almanac_enabled``
        顶层开关，走独立 ``<ALMANAC_STATE>`` 标签，不再在此处返回。
        """
        return self.time_context.enabled_builtin_categories()

    def _try_save_config(self) -> None:
        """落盘 self.config（迁移 / 命令覆盖后调用）。

        AstrBotConfig 是 dict 子类，持久化方法是 ``save_config()``（不是 ``save``）。
        失败仅 warning：内存中已迁移，下次启动会重跑，不影响运行。
        """
        save_fn = getattr(self.config, "save_config", None)
        if not callable(save_fn):
            logger.debug(f"{tag()} self.config 无 save_config 方法，跳过落盘")
            return
        try:
            save_fn()
            logger.debug(f"{tag()} 配置已落盘")
        except Exception as e:
            logger.warning(f"{tag()} ⚠️ 配置落盘失败（不致命，下次启动会重跑）: {e}")

    def _check_schedule_overlap(self) -> None:
        """检测 schedule_templates 时段重叠，仅 warning 不强制 reject。

        重叠定义：两个时段在某个时间点同时命中。跨午夜时段（end < start）展开为
        [start, 24:00) ∪ [00:00, end) 后再比较。多个时段命中同一时刻即视为重叠。
        """
        slots = self.config.get("daily_schedule", {}).get("schedule_templates") or []
        if not isinstance(slots, list) or len(slots) < 2:
            return

        def to_ranges(s: dict) -> list[tuple[int, int]]:
            """把单个时段转成 [(start_min, end_min), ...] 的 24h 分钟表示（已展开跨午夜）。"""
            start = str(s.get("start_time", "")).strip()
            end = str(s.get("end_time", "")).strip()
            try:
                sh, sm = map(int, start.split(":"))
                eh, em = map(int, end.split(":"))
            except (ValueError, AttributeError):
                return []
            s_min = sh * 60 + sm
            e_min = eh * 60 + em
            if s_min == e_min:
                return []
            if s_min < e_min:
                return [(s_min, e_min)]
            # 跨午夜：拆成两段
            return [(s_min, 24 * 60), (0, e_min)]

        # 用位图（每分钟一个 bit）检测重叠
        occupied: list[int | None] = [None] * (24 * 60)  # 每分钟记录第一个占用的 slot 索引
        for idx, slot in enumerate(slots):
            if not isinstance(slot, dict):
                continue
            for s_min, e_min in to_ranges(slot):
                for m in range(s_min, e_min):
                    if occupied[m] is not None:
                        other_idx = occupied[m]
                        logger.warning(
                            f"{tag()} ⚠️ 日程表时段重叠："
                            f"slot#{idx}({slot.get('start_time')}-{slot.get('end_time')}) "
                            f"与 slot#{other_idx}("
                            f"{slots[other_idx].get('start_time')}-{slots[other_idx].get('end_time')}) "
                            f"在 {m // 60:02d}:{m % 60:02d} 重叠（行为：取列表中靠前的时段）"
                        )
                    else:
                        occupied[m] = idx

    @filter.on_llm_request()
    async def inject_time_context(self, event: AstrMessageEvent, req: ProviderRequest):
        """时间引导注入（按 wiki.txt 规范拆双路）：

        - 静态规则 ``DEFAULT_TIME_GUIDANCE_PROMPT`` → ``req.system_prompt`` 末尾。
          system_prompt 永远字节稳定（除了规则文本就是 persona），命中 provider Prompt 缓存。
        - 动态数据（``<SCHEDULE_STATE>`` + ``<WORKDAY_STATE>`` + ``<TODAY_EVENTS>`` +
          ``<LAST_CHAT>``）→ ``req.extra_user_content_parts``，
          ``mark_as_temp()`` 标记后历史持久化时被剥掉，下一轮历史里消失。

        动态数据本质上只是"当前态"信息，不需要在历史里累积；persona + 静态规则在
        system_prompt 永远在场，LLM 推理不受影响。
        """
        if not self._time_guidance_enabled():
            return
        try:
            existing = (getattr(req, "system_prompt", "") or "").strip()
            rules = DEFAULT_TIME_GUIDANCE_PROMPT
            req.system_prompt = (
                f"{existing.rstrip()}\n\n{rules}" if existing else rules
            )
            # 记录用户消息时间——on_llm_request 天然过滤命令（命令不调 LLM）。
            # 单独 try：record 失败不应影响 dyn 注入（否则 LLM 这次完全没时间感知）
            umo = event.unified_msg_origin
            if self._last_chat_enabled() and umo:
                try:
                    self.last_chat_tracker.record_user(
                        umo, get_now(self.config, self._astrbot_config())
                    )
                except Exception as e:
                    logger.warning(f"{tag(event)} ⚠️ record_user 失败: {e}")
            dyn = self._build_dynamic_time_data(session=umo)
            req.extra_user_content_parts.append(TextPart(text=dyn).mark_as_temp())
        except Exception as e:
            logger.error(f"{tag(event)} ❌ on_llm_request 注入失败: {e}")

    @filter.after_message_sent()
    async def _record_ai_sent_time(self, event: AstrMessageEvent):
        """AI 发送消息后记录时间——用于 ``<LAST_CHAT>`` 标签。

        命令消息（以 ``/`` 开头）不计入：避免 ``/calendar show`` 这类管理操作被
        误判为「聊天」。``after_message_sent`` 在 LLM 失败时不触发，这是预期行为。
        """
        if not self._last_chat_enabled():
            return
        try:
            original = getattr(event.message_obj, "message_str", "") or ""
            if original.strip().startswith("/"):
                return
        except Exception:
            pass
        try:
            umo = event.unified_msg_origin
            if not umo:
                return
            self.last_chat_tracker.record_ai(
                umo, get_now(self.config, self._astrbot_config())
            )
        except Exception as e:
            logger.warning(f"{tag(event)} ⚠️ 记录 AI 发送时间失败: {e}")

    # ==================== 命令树 ====================

    @filter.command_group("calendar")
    def calendar_group(self):
        """日历管理命令组入口。"""

    @calendar_group.command("help")
    async def cmd_help(self, event: AstrMessageEvent):
        """显示日历命令帮助。"""
        text = (
            "[time_awareness 命令]\n"
            "/calendar show [YYYY-MM]  — 列出指定月份（默认本月）的事项\n"
            "/calendar add <日期> [重复] <标题>  — 新增事项（管理员）\n"
            "/calendar del <id>  — 删除用户事项（管理员；内置事件不允许删除，请用配置开关）\n"
            "/calendar create  — 按配置里的世界观重新生成完整日历（管理员）\n"
            "/calendar export  — 输出当前数据为 YAML（管理员）\n"
            "/calendar import  — 回复一条 YAML 文本批量导入（管理员）\n"
            "/calendar builtin_regen  — 重新生成当年内置现实事件（管理员）\n"
            "/calendar builtin_list [分类]  — 列出当年所有内置事件（可按「法定/传统/节气/政治/国际」过滤）\n"
            "/calendar help  — 显示本帮助\n"
            "\n"
            "日期格式：\n"
            "- YYYY-MM-DD（如 2026-06-24）\n"
            "- MM-DD（如 06-24，自动补当前年份）\n"
            "\n"
            "重复参数（可选）：\n"
            "- 0  = 仅当年（默认）\n"
            "- 1-4 = 从基准年起连续 N+1 年\n"
            "- 9  = 永久每年重复\n"
            "\n"
            "示例：\n"
            "/calendar add 2026-06-24 测试事件\n"
            "/calendar add 06-24 9 每年生日\n"
            "/calendar del abc12345  — 按 id 删除用户事件\n"
            "\n"
            "内置现实日历事件（默认开法定/传统/节气，关政治/国际）：\n"
            "- 法定节假日（含调休）、传统农历节日、二十四节气\n"
            "- 政治纪念日、国际/西方节日\n"
            "- 黄历开关已迁到 time_awareness.almanac_enabled（v1.7.1，紧邻农历配置）\n"
            "- 跨年自动重新生成\n"
            "\n"
            "主动提醒（reminder 配置启用后）：\n"
            "- 当日事项会在配置的时刻主动发到白名单会话\n"
            "- LLM 可调用 schedule_followup 工具安排「X 分钟后再说」"
        )
        yield event.plain_result(text)

    @calendar_group.command("show")
    async def cmd_list(self, event: AstrMessageEvent, month: str = ""):
        """列出指定月份的事项（用户 + 内置合并）。"""
        now = get_now(self.config, self._astrbot_config())
        year, mon = now.year, now.month
        if month:
            parsed = parse_year_month(month, now)
            if parsed is None:
                yield event.plain_result(f"格式错误：{month}，应为 YYYY-MM 或 YYYY-MM-DD。")
                return
            year, mon = parsed

        include_builtin = bool(self._enabled_builtin_categories())
        events = self.calendar_store.events_for_month(year, mon, include_builtin=include_builtin)
        if not events:
            yield event.plain_result(f"{year}-{mon:02d} 无事项。")
            return

        lines = [f"[{year}-{mon:02d} 共 {len(events)} 条]"]
        for e in events:
            day = e.get("day", 0)
            text = e.get("text", "")
            if e.get("source") == "builtin":
                cat_tag = category_short_tag(e.get("category", ""))
                lines.append(f"  {mon:02d}-{day:02d} {text} [{cat_tag}]")
            else:
                repeat_tag = describe_repeat(e.get("repeat", 0))
                lines.append(f"  #{e.get('id', '')[:8]} {mon:02d}-{day:02d} {text}{repeat_tag}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @calendar_group.command("add")
    async def cmd_add(self, event: AstrMessageEvent, date_str: str = "", repeat_or_title: str = "", title: str = ""):
        """新增一条事项。

        用法：
        /calendar add <日期> <标题>
        /calendar add <日期> <重复> <标题>
        重复：0=仅当年（默认）/1-4=连续N+1年/9=永久每年
        """
        if not date_str:
            yield event.plain_result(
                "用法：/calendar add <日期> [重复] <标题>\n"
                "日期：YYYY-MM-DD 或 MM-DD；重复：0(默认)/1-4/9(永久)\n"
                "示例：/calendar add 2026-06-24 测试事件\n"
                "      /calendar add 06-24 9 每年生日"
            )
            return

        repeat = 0
        if title:
            # 三参数模式：date + repeat + title
            try:
                repeat_val = int(repeat_or_title)
            except ValueError:
                yield event.plain_result(
                    f"重复参数必须是整数（0/1-4/9），收到：{repeat_or_title}"
                )
                return
            if repeat_val == 9:
                repeat = REPEAT_FOREVER  # -1
            elif 0 <= repeat_val <= 4:
                repeat = repeat_val
            else:
                yield event.plain_result(
                    f"重复参数越界：{repeat_val}（应为 0/1-4/9）"
                )
                return
            final_title = title
        elif repeat_or_title:
            final_title = repeat_or_title
        else:
            yield event.plain_result("缺少标题。用法：/calendar add <日期> [重复] <标题>")
            return

        parsed = parse_event_date(date_str)
        if parsed is None:
            yield event.plain_result(
                f"日期格式无法解析：{date_str}\n支持 YYYY-MM-DD 或 MM-DD\n"
                "示例：/calendar add 06-24 测试事件"
            )
            return

        now = get_now(self.config, self._astrbot_config())
        month, day, year = parsed
        if year is None:
            year = now.year

        raw = {
            "year": year,
            "month": month,
            "day": day,
            "text": final_title,
            "repeat": repeat,
        }
        event_obj = self.calendar_manager.add_event(raw)
        if event_obj is None:
            yield event.plain_result("添加失败，请检查参数或日志。")
            return
        yield event.plain_result(
            f"已添加 #{event_obj['id'][:8]}：{event_obj['text']}"
            f"（{event_obj['year']}-{event_obj['month']:02d}-{event_obj['day']:02d}"
            f"{describe_repeat(event_obj['repeat'])}）"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @calendar_group.command("del")
    async def cmd_delete(self, event: AstrMessageEvent, event_id: str = ""):
        """按 id 删除事项（仅对用户事件生效）。

        内置事件不允许单条删除——年内日历不会变，regen 会复活。
        关闭整类请用配置开关；批量定制请直接编辑 builtin_events.yaml。
        """
        if not event_id:
            yield event.plain_result(
                "用法：/calendar del <id> 或 /calendar del <文本>\n"
                "id 从 /calendar show 获取（可只输入前 8 位）；也可直接输入完整事件文本"
            )
            return

        target = find_event_by_short_id(self.calendar_store, event_id)
        if target is None:
            yield event.plain_result(f"未找到 id 为 {event_id} 的事项。")
            return

        if target.get("source") == "builtin":
            yield event.plain_result(
                f"内置事件「{target.get('text', '')}」不允许单条删除。\n"
                f"如需关闭整类，请在配置里改 calendar.builtin_events.* 开关；"
                f"如需批量定制，请直接编辑 builtin_events.yaml。"
            )
            return

        ok = self.calendar_manager.delete_event(target["id"])
        if ok:
            yield event.plain_result(f"已删除：{target.get('text', '')}")
        else:
            yield event.plain_result("删除失败，请检查日志。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @calendar_group.command("create")
    async def cmd_generate(self, event: AstrMessageEvent):
        """按配置里的世界观重新生成完整日历并导入。"""
        cal = self.config.get("calendar", {})
        theme = (cal.get("ai_generate_worldview", "") or "").strip()
        if not theme:
            yield event.plain_result(
                "未配置世界观。请先在配置 calendar.ai_generate_worldview 填写主题/世界观设定后重试。"
            )
            return

        provider_id = (cal.get("ai_generate_provider_id", "") or "").strip()
        if not provider_id:
            try:
                provider_id = await self.context.get_current_chat_provider_id(
                    event.unified_msg_origin
                )
            except Exception as e:
                yield event.plain_result(f"无法确定当前会话 Provider：{e}")
                return
        now = get_now(self.config, self._astrbot_config())
        system_prompt = build_system_prompt(now.year, DEFAULT_MAX_GENERATE)

        yield event.plain_result(f"正在调用 AI 生成「{theme}」主题日历，请稍候……")

        events = await generate_calendar_events(
            self.context,
            provider_id=provider_id,
            user_prompt=theme,
            system_prompt=system_prompt,
            current_year=now.year,
        )
        if not events:
            yield event.plain_result("AI 生成失败或返回为空，请检查 provider 配置与日志。")
            return

        count = self.calendar_manager.import_events(events, mode="merge")
        if count < 0:
            yield event.plain_result("生成成功但写入文件失败，请检查日志。")
            return
        yield event.plain_result(f"✅ 已导入 {count} 条事项（主题：{theme}）。使用 /calendar show 查看全部。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @calendar_group.command("export")
    async def cmd_export(self, event: AstrMessageEvent):
        """导出当前数据为 YAML 文本。"""
        total = len(self.calendar_store.events)
        if total == 0:
            yield event.plain_result("当前时间表为空。")
            return
        yaml_text = self.calendar_manager.export_yaml()
        preview = yaml_text if len(yaml_text) <= 3500 else yaml_text[:3500] + f"\n...（共 {len(yaml_text)} 字符，已截断）"
        yield event.plain_result(preview)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @calendar_group.command("import")
    async def cmd_import(self, event: AstrMessageEvent):
        """从回复的 YAML 文本批量导入。

        使用方式：回复一条 YAML 文本消息，再发送 /calendar import。
        """
        content = ""
        try:
            reply = getattr(event.message_obj, "reply", None)
            if reply:
                chain = getattr(reply, "chain", None) or getattr(reply, "message_chain", None)
                if chain:
                    for c in chain:
                        text = getattr(c, "text", None) or (c if isinstance(c, str) else "")
                        if text:
                            content += text
        except Exception:
            content = ""

        if not content:
            yield event.plain_result(
                "请回复一条 YAML 文本消息后发送 /calendar import。\n"
                "（YAML 顶层可为数组，或含 events 字段的映射）"
            )
            return

        raw_events = CalendarManager.parse_import_content(content)
        if raw_events is None:
            yield event.plain_result("YAML 解析失败，请检查格式（顶层为数组或含 events 字段的映射）。")
            return
        count = self.calendar_manager.import_events(raw_events, mode="merge")
        if count < 0:
            yield event.plain_result("解析成功但写入文件失败，请检查日志。")
            return
        yield event.plain_result(f"✅ 已导入 {count} 条事项。")

    # ==================== 内置事件管理 ====================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @calendar_group.command("builtin_regen")
    async def cmd_builtin_regen(self, event: AstrMessageEvent):
        """重新生成当年的内置现实日历事件。

        触发时机：
        - 每年 1/1 自动触发（跨年检测）
        - 分类开关变更后手动刷新
        - 用户想清空黑名单重新纳入所有事件
        """
        now = get_now(self.config, self._astrbot_config())
        categories = self._enabled_builtin_categories()
        if not categories:
            yield event.plain_result(
                "未启用任何内置事件分类。请在配置中开启 calendar.enable_builtin_events 与至少一个分类开关。"
            )
            return

        count = self.builtin_manager.regenerate(now.year, categories)
        if count < 0:
            yield event.plain_result("重新生成失败，请检查日志。")
            return

        # 同步到内存单例
        builtin_data = self.builtin_manager.load_raw()
        self.calendar_store.set_builtin_events(builtin_data.get("events") or [])

        yield event.plain_result(
            f"✅ 已重新生成 {now.year} 年内置事件：{count} 条\n"
            f"启用分类：{', '.join(categories)}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @calendar_group.command("builtin_list")
    async def cmd_builtin_list(self, event: AstrMessageEvent, category: str = ""):
        """列出当年所有内置事件，可按分类过滤。

        用法：
        /calendar builtin_list           — 列出所有分类的全部事件
        /calendar builtin_list 法定      — 仅列出法定节假日
        /calendar builtin_list 传统      — 仅列出传统节日
        /calendar builtin_list 节气      — 仅列出二十四节气
        /calendar builtin_list 政治      — 仅列出政治纪念日
        /calendar builtin_list 国际      — 仅列出国际/西方节日
        """
        now = get_now(self.config, self._astrbot_config())
        builtin_data = self.builtin_manager.load_raw()
        events = builtin_data.get("events") or []

        # 分类过滤
        cat_filter = parse_category_input(category)
        if cat_filter:
            events = [e for e in events if e.get("category") == cat_filter]

        if not events:
            yield event.plain_result(
                f"无内置事件数据（year={now.year}，category={category or '全部'}）。\n"
                f"如需重新生成：/calendar builtin_regen"
            )
            return

        # 按月日排序
        events.sort(key=lambda e: (e.get("month", 0), e.get("day", 0)))
        lines = [f"[{now.year} 内置事件 {len(events)} 条]"]
        for e in events:
            cat_tag = category_short_tag(e.get("category", ""))
            text = e.get("text", "")
            lines.append(
                f"  {e.get('month', 0):02d}-{e.get('day', 0):02d} {text} [{cat_tag}]"
            )
        yield event.plain_result("\n".join(lines))

    # ==================== 单日日程表 ====================

    @filter.command_group("schedule")
    def schedule_group(self):
        """单日日程表管理命令组入口（时段状态感知）。"""

    @schedule_group.command("help")
    async def schedule_help(self, event: AstrMessageEvent):
        """显示日程表命令帮助。"""
        text = (
            "[time_awareness 日程表命令]\n"
            "/schedule show  — 列出当前所有时段（按开始时间排序）\n"
            "/schedule create  — 按配置世界观或当前人设生成日程表（覆盖现有，管理员）\n"
            "/schedule help  — 显示本帮助\n"
            "\n"
            "说明：\n"
            "- 命中时段的 state_prompt 通过 <SCHEDULE_STATE> 标签注入到当前轮 user 消息末尾\n"
            "- 未启用日程表感知或未命中任何时段时返回「未指定（按人设自然推断）」\n"
            "- 时段编辑也可在 AstrBot 主 webui 配置 daily_schedule.schedule_templates 完成\n"
            "\n"
            "时间格式：HH:MM（24 小时制）\n"
            "- end_time < start_time 视为跨午夜（如 22:00→08:00）\n"
            "- end_time exclusive：当前时间 < end_time 才算命中\n"
            "\n"
            "示例：\n"
            "/schedule show  — 查看当前时段配置\n"
            "/schedule create  — 用 AI 生成一整套作息表"
        )
        yield event.plain_result(text)

    @schedule_group.command("show")
    async def schedule_show(self, event: AstrMessageEvent):
        """列出当前所有时段（按 start_time 排序）。"""
        ds = self.config.get("daily_schedule", {})
        enabled = bool(ds.get("enable_schedule", False))
        slots = ds.get("schedule_templates") or []
        if not isinstance(slots, list):
            slots = []

        if not slots:
            yield event.plain_result(
                f"[日程表] 共 0 条时段（感知{'已启用' if enabled else '未启用'}）。\n"
                f"用 /schedule create 让 AI 生成，或在主 webui 手动添加。"
            )
            return

        def sort_key(s: dict) -> tuple[int, int]:
            try:
                h, m = str(s.get("start_time", "00:00")).split(":")
                return (int(h), int(m))
            except Exception:
                return (99, 99)

        ordered = sorted(
            [s for s in slots if isinstance(s, dict)],
            key=sort_key,
        )

        # 高亮当前命中的时段
        now = get_now(self.config, self._astrbot_config())
        active = find_active_schedule_slot(ds, now)
        active_start = str((active or {}).get("start_time", "")).strip() if active else ""

        lines = [f"[日程表 共 {len(ordered)} 条时段（感知{'已启用' if enabled else '未启用'}）]"]
        for s in ordered:
            name = (s.get("name") or "").strip()
            start = str(s.get("start_time", "")).strip()
            end = str(s.get("end_time", "")).strip()
            state = (s.get("state_prompt") or "").strip() or "(无状态描述)"
            # state 截断到 40 字，过长用省略号
            state_preview = state if len(state) <= 40 else state[:37] + "..."
            name_tag = f"[{name}] " if name else ""
            marker = " ← 当前" if start == active_start else ""
            lines.append(f"  {start}-{end}{marker}  {name_tag}{state_preview}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @schedule_group.command("create")
    async def schedule_create(self, event: AstrMessageEvent, confirm: str = ""):
        """按配置生成日程表（覆盖现有 schedule_templates）。

        输入源由配置 ``ai_generate_use_persona`` 控制：
        - True（默认）：「当前会话人设 prompt」+ ``ai_generate_worldview`` 拼接为联合输入
        - False：仅读 ``ai_generate_worldview``
        两者都为空时拒绝执行。已有现有时段时需追加 ``confirm`` 参数确认覆盖。
        """
        ds = self.config.get("daily_schedule", {})
        use_persona = bool(ds.get("ai_generate_use_persona", True))
        worldview = (ds.get("ai_generate_worldview", "") or "").strip()

        persona_prompt = ""
        if use_persona:
            session = event.unified_msg_origin or ""
            persona_prompt = await self._resolve_persona_prompt(session, event=event)

        # 拼接联合输入：人设 + 世界观
        parts = []
        if persona_prompt:
            parts.append(persona_prompt)
        if worldview:
            parts.append(worldview)
        theme = "\n\n".join(parts) if parts else ""
        persona_used = bool(persona_prompt)

        if not theme:
            if use_persona:
                yield event.plain_result(
                    "未提供任何输入：当前会话没有人设 prompt，且 daily_schedule.ai_generate_worldview 为空。\n"
                    "请为人设配置 prompt，或在配置里填写世界观设定后重试。"
                )
            else:
                yield event.plain_result(
                    "未配置世界观（daily_schedule.ai_generate_worldview 为空）。\n"
                    "请填写世界观后重试，或勾选「人设 + 世界观联合输入」开关。"
                )
            return

        # 二次确认（覆盖现有 schedule_templates）
        existing = ds.get("schedule_templates") or []
        if isinstance(existing, list) and len(existing) > 0 and (confirm or "").strip().lower() != "confirm":
            yield event.plain_result(
                f"⚠️ 当前已有 {len(existing)} 条时段，本次生成会覆盖。\n"
                f"确认覆盖请发送：/schedule create confirm"
            )
            return

        async for msg in self._do_schedule_generate(event, theme, persona_used, worldview_used=bool(worldview)):
            yield msg

    async def _do_schedule_generate(
        self, event: AstrMessageEvent, theme: str, persona_used: bool, worldview_used: bool = False
    ):
        """实际执行 LLM 调用 + 解析 + 落盘。"""
        ds = self.config.get("daily_schedule", {})
        provider_id = (ds.get("ai_generate_provider_id", "") or "").strip()
        if not provider_id:
            try:
                provider_id = await self.context.get_current_chat_provider_id(
                    event.unified_msg_origin
                )
            except Exception as e:
                yield event.plain_result(f"无法确定当前会话 Provider：{e}")
                return

        if persona_used and worldview_used:
            source_tag = "人设 + 世界观"
        elif persona_used:
            source_tag = "当前人设"
        else:
            source_tag = "配置世界观"
        theme_preview = theme if len(theme) <= 60 else theme[:57] + "..."
        yield event.plain_result(
            f"正在调用 AI 生成日程表（来源：{source_tag}「{theme_preview}」），请稍候……"
        )

        # 调 LLM
        try:
            slots_data, raw_text = await generate_schedule(
                self.context,
                theme=theme,
                provider_id=provider_id,
            )
        except Exception as e:
            logger.error(f"{tag()} ❌ AI 生成日程表 LLM 调用失败: {e}")
            yield event.plain_result(f"AI 生成失败：{e}")
            return

        if slots_data is None:
            logger.error(
                f"{tag()} ❌ AI 输出 JSON 解析失败，原文前 200 字: {raw_text[:200]}"
            )
            yield event.plain_result(
                "AI 输出 JSON 解析失败。请稍后重试，或检查 provider 是否遵守系统提示词。"
            )
            return

        # 转成 template_list 元素格式（带 __template_key 元字段）
        new_slots = []
        for item in slots_data:
            slot = {
                "__template_key": "time_slot",
                "name": item.get("name", ""),
                "start_time": item["start"],
                "end_time": item["end"],
                "state_prompt": item["state"],
            }
            new_slots.append(slot)

        # 写回 self.config（dict 视图）
        if not isinstance(self.config.get("daily_schedule"), dict):
            self.config["daily_schedule"] = {}
        self.config["daily_schedule"]["schedule_templates"] = new_slots
        self.config["daily_schedule"]["enable_schedule"] = True

        # 落盘
        self._try_save_config()

        # 启动时重叠检测（迁移 / 命令覆盖都过一遍）
        self._check_schedule_overlap()

        now = get_now(self.config, self._astrbot_config())
        active = find_active_schedule_slot(self.config.get("daily_schedule", {}), now)
        active_info = ""
        if active:
            active_name = (active.get("name") or "").strip()
            active_state = (active.get("state_prompt") or "").strip()
            name_part = f"[{active_name}] " if active_name else ""
            active_info = (
                f"\n当前命中时段：{name_part}{active.get('start_time')}-{active.get('end_time')}"
                + (f"（{active_state[:40]}）" if active_state else "")
            )

        yield event.plain_result(
            f"✅ 已生成 {len(new_slots)} 条时段并写入配置。\n"
            f"来源：{source_tag}\n"
            f"使用 /schedule show 查看完整列表。{active_info}"
        )

    # ==================== LLM 工具：schedule_followup ====================

    @filter.llm_tool(name="schedule_followup")
    async def schedule_followup_tool(
        self,
        event: AstrMessageEvent,
        delay_minutes: int,
        hint: str,
    ) -> str:
        """
        安排一个未来时刻的主动消息：到点时插件会重新唤醒你，提供当前人设、时间、日程状态
        和 hint 备忘；管理员启用 ChatMemory 历史注入后，还会提供 CM 当前配置生成的接管上下文。

        触发场景：
        - 用户明确要求「X 分钟后提醒我」「晚点再聊」等
        - 你和用户约定了稍后继续某事（如「等我下」「一会儿告诉你」）

        不要触发的场景：
        - 用户没明确要求时主动定时（避免打扰）
        - 已完成的事或闲聊中无事可约

        Args:
            delay_minutes(int): 几分钟后触发。范围 1-1440（最多 24 小时）。
            hint(string): 给「未来的自己」的备忘。必填。要写得让未来的你能延续此刻状态——
                约定了什么、要继续什么、以什么口吻回应。
        """
        reminder_cfg = self.config.get("reminder", {})
        if not reminder_cfg.get("enable_reminder", False):
            return "失败：主动提醒功能未启用"
        if not reminder_cfg.get("followup_tool_enabled", True):
            return "失败：schedule_followup 工具已被管理员禁用"

        # 校验 delay_minutes
        try:
            delay = int(delay_minutes)
        except (TypeError, ValueError):
            return f"失败：delay_minutes 必须是整数，收到 {delay_minutes!r}"
        if delay < 1 or delay > 1440:
            return f"失败：delay_minutes 越界（应为 1-1440，收到 {delay}）"

        hint = (hint or "").strip()
        if not hint:
            return "失败：hint 不能为空"
        if len(hint) > 500:
            return "失败：hint 长度不能超过 500 字"

        # 白名单校验
        session = event.unified_msg_origin or ""
        if session not in self._reminder_targets():
            return "失败：当前会话不在提醒白名单内"

        now = get_now(self.config, self._astrbot_config())
        fire_at = now + datetime.timedelta(minutes=delay)
        # at 目标固定为当前对话的发起人（群聊场景前置 At 组件）
        target_uid = event.get_sender_id() or ""
        self.scheduler.add_followup_task(
            session=session,
            fire_at=fire_at,
            hint=hint,
            target_user_id=target_uid,
        )
        hint_preview = f"（备忘：{hint[:30]}）"
        time_str = fire_at.strftime('%H:%M')
        return (
            f"已安排 {delay} 分钟后（{time_str}）触发主动消息{hint_preview}。\n"
            f"本轮无需向用户复述本次工具调用——若刚才已和用户说过安排，直接返回空回复即可；若未说过，简短告知一声即可。"
        )
