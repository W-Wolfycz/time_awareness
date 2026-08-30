"""
time_awareness — 时笺

为 LLM 注入：
1. 静态时间规则 → system_prompt 末尾（保 Prompt 缓存前缀稳定）
2. 现实时间、当前时段状态 / 当日事项 → extra_user_content_parts + mark_as_temp
   （注入到当前轮 user 消息末尾，不持久化到历史）

日历管理通过聊天命令 `/calendar show|add|del|create|export|import|help`。
日程表管理通过 `/schedule show|static|regenerate|help`。

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

from .commands.calendar_commands import CalendarCommands
from .commands.schedule_commands import ScheduleCommands
from .constants import DEFAULT_TIME_GUIDANCE_PROMPT
from .log import configure as configure_log
from .log import logger, tag
from .core.builtin_manager import BuiltinManager
from .core.calendar_manager import CalendarManager
from .core.calendar_store import CalendarStore
from .core.daily_schedule_store import DailyScheduleSnapshotStore
from .core.last_chat_tracker import LastChatTracker
from .core.scheduler import DueGroup, ReminderScheduler, TriggerResult
from .core.weather_sensor import WeatherSensor
from .domain.schedule import (
    ScheduleValidationError,
    detect_static_overlaps,
    format_time,
    parse_time,
)
from .integrations.astrbot_sender import AstrBotMessageSender
from .integrations.chat_memory_history import is_chat_memory_takeover_applied
from .services.time_context_service import TimeContextService
from .services.daily_schedule_service import DailyScheduleService
from .services.adaptive_policy import (
    AdaptiveConcurrencyGate,
    AdaptiveGenerationPolicy,
)
from .services.calendar_runtime_service import CalendarRuntimeService
from .services.config_migration import (
    migrate_builtin_categories,
    migrate_log_group,
    migrate_time_sensors,
)
from .services.daily_schedule_admin_service import DailyScheduleAdminService
from .services.reminder_service import ReminderService
from .services.static_schedule_config_service import StaticScheduleConfigService
from .utils.time_utils import get_now
from .web_api import register_web_apis

PLUGIN_DATA_DIR_NAME = "time_awareness"
_AI_GENERATION_DEFAULTS = {
    "provider_id": "",
    "use_persona": True,
    "worldview": "",
    "max_attempts": 3,
}


class TimeAwarenessPlugin(Star):
    """时笺 + 智能日历 + 主动提醒。"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self._migrate_ai_generation_config()

        # 2.0.x：布尔开关形态 → 多选 selector 形态（一次性迁移，见 config_migration）
        migrated_sensors = migrate_time_sensors(self.config)
        migrated_builtin = migrate_builtin_categories(self.config)
        migrated_log = migrate_log_group(self.config)
        if migrated_sensors or migrated_builtin or migrated_log:
            logger.info(
                f"{tag()} 配置形态迁移完成: sensors={migrated_sensors} "
                f"builtin={migrated_builtin} log_group={migrated_log}"
            )
            self._try_save_config()

        # 日志前缀带 bot 实例标识：区分多 bot 共存场景（顶层全局配置项；
        # 兼容旧 ``log`` 分组，迁移后旧值已提升到顶层）
        log_with_bot_id = self.config.get("log_with_bot_id", None)
        if log_with_bot_id is None:
            log_cfg = self.config.get("log", {})
            log_with_bot_id = (
                log_cfg.get("log_with_bot_id", False)
                if isinstance(log_cfg, dict)
                else False
            )
        configure_log(log_with_bot_id=log_with_bot_id)

        # 启动后检测时段重叠（仅 warning）
        self._check_schedule_overlap()
        # 2.0.0：静态日程时间格式窄范围规范化（H:MM → HH:MM，有变化即落盘）
        self._normalize_static_template_times()

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
        self.weather_sensor = WeatherSensor(
            config=self.config,
            # 以插件时区为准取「今天」：与日程 local_date、<SYSTEM_REMINDER> 同源，
            # 避免 VPS 系统时区与插件配置时区不一致时天气种子日期差一天。
            now_provider=lambda: get_now(self.config, self._astrbot_config()),
        )
        self.time_context = TimeContextService(
            config=self.config,
            astrbot_config_provider=self._astrbot_config,
            calendar_store=self.calendar_store,
            last_chat_tracker=self.last_chat_tracker,
        )
        # 插件级唯一 LLM 闸门：AI 日程、主动提醒与 /calendar create 共用。
        self.llm_gate = AdaptiveConcurrencyGate(
            AdaptiveGenerationPolicy.from_config(self.config).max_concurrent_llm
        )
        self.daily_schedule_store = DailyScheduleSnapshotStore(self.data_dir)
        self.daily_schedule_service = DailyScheduleService(
            context=self.context,
            config=self.config,
            store=self.daily_schedule_store,
            time_context=self.time_context,
            concurrency_gate=self.llm_gate,
            weather_forecast_provider=self.weather_sensor.daily_forecast,
        )
        self.time_context.set_daily_schedule_service(self.daily_schedule_service)
        # 人格日程人工编辑服务（WebUI 查询/编辑快照，与生成服务共用锁）
        self.daily_schedule_admin = DailyScheduleAdminService(
            context=self.context,
            store=self.daily_schedule_store,
            daily_schedule_service=self.daily_schedule_service,
        )
        # 调度器：trigger_callback 委托给 self._on_due；
        # now_provider 让 scheduler 用与 fire_at 同时区的 now（避免 aware/naive 比较报错）
        self.scheduler = ReminderScheduler(
            self.data_dir,
            self._on_due,
            now_provider=lambda: get_now(self.config, self._astrbot_config()),
        )
        self.calendar_runtime = CalendarRuntimeService(
            config=self.config,
            calendar_store=self.calendar_store,
            builtin_manager=self.builtin_manager,
            scheduler=self.scheduler,
            time_context=self.time_context,
            targets_provider=lambda: self.reminder_service.targets(),
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
            ensure_builtin_fresh=self.calendar_runtime.ensure_builtin_fresh,
            concurrency_gate=self.llm_gate,
        )
        self.calendar_commands = CalendarCommands(
            context=self.context,
            config=self.config,
            calendar_store=self.calendar_store,
            calendar_manager=self.calendar_manager,
            builtin_manager=self.builtin_manager,
            now_provider=self.time_context.now,
            enabled_builtin_categories=self.time_context.enabled_builtin_categories,
            concurrency_gate=self.llm_gate,
        )
        self.schedule_commands = ScheduleCommands(
            context=self.context,
            config=self.config,
            daily_schedule_service=self.daily_schedule_service,
            now_provider=self.time_context.now,
            save_config=self._try_save_config,
            check_overlap=self._check_schedule_overlap,
        )
        self.static_schedule_config = StaticScheduleConfigService(
            config=self.config,
            save_config=self._try_save_config,
            check_overlap=self._check_schedule_overlap,
        )
        self.calendar_runtime.apply_reminder_config()
        # 每日 0 点扫描当天事项的 asyncio task
        self._daily_scan_task: asyncio.Task | None = None
        # AI 每日日程在配置时刻生成；与 reminder 开关独立。
        self._daily_schedule_task: asyncio.Task | None = None
        self.web_api = None

        logger.info(f"{tag()} 插件已初始化，数据目录: {self.data_dir}")

    async def initialize(self):
        """AstrBot 启动时调用：加载数据 + 启动调度。

        任一后续步骤失败时，按已启动资源逆序自行回滚（见
        ``_shutdown_resources``），再重新抛出异常；不依赖 AstrBot 失败加载
        时的自动 terminate。
        """
        self.calendar_manager.load()
        self.last_chat_tracker.load()
        daily_store_ready = False
        if self.daily_schedule_service.configured_enabled():
            daily_store_ready = self.daily_schedule_store.load()
        self.daily_schedule_service.set_available(daily_store_ready)

        self.calendar_runtime.initialize_builtin_events()
        self.calendar_runtime.apply_reminder_config()
        self.scheduler.load()

        reminder_cfg = self.config.get("reminder", {})
        if not isinstance(reminder_cfg, dict):
            reminder_cfg = {}
        try:
            if reminder_cfg.get("enable_reminder", False):
                # 启动时先扫一遍今天的事项入队（错过当日提醒时刻仍可补发）
                self.calendar_runtime.scan_and_enqueue_today_events()
                self.scheduler.start()
                # 启动每日 0 点扫描循环
                self._daily_scan_task = asyncio.create_task(
                    self.calendar_runtime.run_daily_loop()
                )

            if self.daily_schedule_service.enabled():
                # reminder_targets 是启动阶段唯一安全可得的代表会话；其他 Bot 在首次
                # 普通消息到来时注册。Persona 身份解析失败时会跳过（首次消息再补）。
                # UMO 仅保存在当前进程，不写入快照。
                for session in self.reminder_service.targets():
                    await self.daily_schedule_service.register_session_async(
                        session, trigger=False
                    )
                await self.daily_schedule_service.queue_known_profiles()
                self._daily_schedule_task = asyncio.create_task(
                    self.daily_schedule_service.run_daily_loop()
                )

            # 注册 Plugin Pages 后端 API（鉴权继承 AstrBot 主 webui）
            self.web_api = register_web_apis(self.context, self)
        except BaseException:
            logger.warning(f"{tag()} ⚠️ 初始化失败，按已启动资源逆序回滚")
            await self._shutdown_resources()
            raise

        if self.weather_sensor.enabled():
            asyncio.create_task(self._announce_random_weather())
        logger.info(f"{tag()} ✅ 初始化完成")

    async def _announce_random_weather(self) -> None:
        """启动播报：随机天气无网络、即时可得，直接输出今日基调。"""
        try:
            ok, message = await self.weather_sensor.validate()
        except Exception as e:
            logger.debug(f"{tag()} 随机天气获取异常: {e}")
            return
        if ok:
            logger.info(f"{tag()} 🌦️ 今日随机天气：{message}")
        else:
            logger.debug(f"{tag()} 随机天气未启用")

    async def _shutdown_resources(self) -> None:
        """按已启动资源逆序停止后台资源；幂等，供 initialize 回滚与 terminate 共用。

        顺序：AI 每日日程循环 → 每日扫描 → 提醒调度器 → 上次对话时间刷盘。
        各步骤本身幂等（task 置 None、close 清理后再次调用为空操作），
        重复调用不会造成双清理。
        """
        if self._daily_schedule_task is not None:
            self._daily_schedule_task.cancel()
            try:
                await self._daily_schedule_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"{tag()} ⚠️ AI 每日日程 task 停止异常: {e}")
            self._daily_schedule_task = None
        try:
            await self.daily_schedule_service.close()
        except Exception as e:
            logger.warning(f"{tag()} ⚠️ AI 每日日程生成任务停止异常: {e}")
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

    async def terminate(self):
        """AstrBot 卸载/关闭时调用：停调度循环 + 每日扫描。"""
        await self._shutdown_resources()
        logger.info(f"{tag()} ✅ 已终止")

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
        task_summary = ",".join(f"{t.kind}:{t.id}" for t in group.tasks)
        late_info = f" late={group.late_minutes}min" if group.is_late else ""
        logger.info(
            f"{tag()} 🔔 触发到期任务: session={group.session} "
            f"tasks={len(group.tasks)} [{task_summary}]{late_info}"
        )
        await self.daily_schedule_service.register_session_async(group.session)
        return await self.reminder_service.deliver(
            session=group.session,
            tasks=group.tasks,
            is_late=group.is_late,
            late_minutes=group.late_minutes,
        )

    # ==================== AstrBot 全局配置 ====================

    def _astrbot_config(self):
        try:
            return self.context.get_config()
        except Exception:
            return None

    def _try_save_config(self) -> bool:
        """落盘 self.config（规范化 / 命令覆盖后调用）。

        AstrBotConfig 是 dict 子类，持久化方法是 ``save_config()``（不是 ``save``）。
        失败仅 warning：内存中的配置更新仍可供本次运行使用，下次启动会重跑。
        """
        save_fn = getattr(self.config, "save_config", None)
        if not callable(save_fn):
            logger.debug(f"{tag()} self.config 无 save_config 方法，跳过落盘")
            return False
        try:
            save_fn()
            logger.debug(f"{tag()} 配置已落盘")
            return True
        except Exception as e:
            logger.warning(f"{tag()} ⚠️ 配置落盘失败（不致命，下次启动会重跑）: {e}")
            return False

    def _migrate_ai_generation_config(self) -> None:
        """把两代旧生成输入分组复制到 AI 每日日程直接字段。

        AstrBot 会在插件构造前按新 schema 补齐默认值，并删除 schema 外键，
        因此旧 ``daily_schedule.ai_generation`` 与上一版
        ``daily_schedule.ai_daily.ai_generation`` 都必须继续以隐藏字段存在。
        一次性标记确保旧值只复制一次；直接字段中的非默认显式值优先，避免
        升级时丢失 Provider、世界观、Persona 开关或重试次数。
        """
        daily = self.config.get("daily_schedule", {})
        if not isinstance(daily, dict) or daily.get("_ai_generation_flat_migrated", False):
            return

        ai_daily = daily.get("ai_daily")
        if not isinstance(ai_daily, dict):
            ai_daily = {}
            daily["ai_daily"] = ai_daily
        legacy = daily.get("ai_generation")
        nested = ai_daily.get("ai_generation")

        merged = dict(_AI_GENERATION_DEFAULTS)
        if isinstance(legacy, dict):
            merged.update(legacy)
        if isinstance(nested, dict):
            if daily.get("_ai_generation_migrated", False):
                merged.update(nested)
            else:
                for key, default in _AI_GENERATION_DEFAULTS.items():
                    if key in nested and nested[key] != default:
                        merged[key] = nested[key]
        for key, default in _AI_GENERATION_DEFAULTS.items():
            if key in ai_daily:
                value = ai_daily[key]
                if value != default:
                    merged[key] = value

        for key, value in merged.items():
            ai_daily[key] = value
        daily["_ai_generation_flat_migrated"] = True
        # 保留上一版标记的真实状态；旧字段仍留在 schema 内以免 AstrBot 在构造
        # 插件前删掉用户数据。
        if not daily.get("_ai_generation_migrated", False):
            daily["_ai_generation_migrated"] = True
        logger.info(f"{tag()} AI 日程生成输入已拆平到 ai_daily 直接字段")
        self._try_save_config()

    def _normalize_static_template_times(self) -> None:
        """2.0.0 计划 §7：对 ``schedule_templates`` 时间字段做一次窄范围规范化。

        只把 ``H:MM → HH:MM``（如 ``8:00 → 08:00``）并立即尝试保存配置；
        不触碰其他 AI 快照、提醒任务和日历数据。非法时间值不规范化、
        不报错（后续解析宽容接受或按原语义处理）。
        """
        daily_schedule = self.config.get("daily_schedule", {})
        slots = (
            daily_schedule.get("schedule_templates")
            if isinstance(daily_schedule, dict)
            else None
        )
        if not isinstance(slots, list):
            return
        changed = False
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            for key, allow_24 in (("start_time", False), ("end_time", True)):
                raw = str(slot.get(key, "") or "").strip()
                if not raw:
                    continue
                try:
                    minute = parse_time(raw, allow_24=allow_24, strict=False)
                except ScheduleValidationError:
                    continue  # 非法值不规范化
                normalized = format_time(minute)
                if normalized != raw:
                    slot[key] = normalized
                    changed = True
                    logger.info(
                        f"{tag()} 静态日程时间规范化: {raw} → {normalized} "
                        f"（slot: {slot.get('name', '')!r}）"
                    )
        if changed:
            self._try_save_config()

    def _check_schedule_overlap(self) -> None:
        """检测 schedule_templates 时段重叠，仅 warning 不强制 reject。

        重叠定义：两个时段在某个时间点同时命中。跨午夜时段（end < start）展开为
        [start, 24:00) ∪ [00:00, end) 后再比较。多个时段命中同一时刻即视为重叠。
        """
        daily_schedule = self.config.get("daily_schedule", {})
        if not isinstance(daily_schedule, dict):
            daily_schedule = {}
        slots = daily_schedule.get("schedule_templates") or []
        if not isinstance(slots, list) or len(slots) < 2:
            return
        for overlap in detect_static_overlaps(slots):
            first = slots[overlap.first_index]
            second = slots[overlap.second_index]
            logger.warning(
                f"{tag()} ⚠️ 日程表时段重叠："
                f"slot#{overlap.second_index}({second.get('start_time')}-{second.get('end_time')}) "
                f"与 slot#{overlap.first_index}("
                f"{first.get('start_time')}-{first.get('end_time')}) "
                f"在 {overlap.minute // 60:02d}:{overlap.minute % 60:02d} "
                f"重叠（行为：取列表中靠前的时段）"
            )

    # priority 约定（2026-08-17 拍板）：-150 必须晚于 chat_memory 的 -100
    # （本 Hook 读取其写入的事件级接管标记），并早于各终结器（-1000/-1000000）。
    # 不选 -200/-250/-260：与 livingmemory_cm、self_utils 同值时执行顺序依赖
    # 插件加载顺序，不确定。修改此值前必须重新核对同环境所有 on_llm_request 的
    # priority（见 docs/PROJECT_CONTEXT.md）。
    @filter.on_llm_request(priority=-150)
    async def inject_time_context(self, event: AstrMessageEvent, req: ProviderRequest):
        """时间引导注入（按 wiki.txt 规范拆双路）：

        - 静态规则 ``DEFAULT_TIME_GUIDANCE_PROMPT`` → ``req.system_prompt`` 末尾。
          system_prompt 永远字节稳定（除了规则文本就是 persona），命中 provider Prompt 缓存。
        - 动态数据（``<SYSTEM_REMINDER>`` + ``<SCHEDULE_STATE>`` + ``<WORKDAY_STATE>`` +
          ``<TODAY_EVENTS>`` +
          ``<LAST_CHAT>``）→ ``req.extra_user_content_parts``，
          ``mark_as_temp()`` 标记后历史持久化时被剥掉，下一轮历史里消失。

        动态数据本质上只是"当前态"信息，不需要在历史里累积；persona + 静态规则在
        system_prompt 永远在场，LLM 推理不受影响。
        """
        # 静态规则注入与 AI 日程注册不依赖 time_guidance 开关：
        # 单独关闭时间引导不应让 AI 每日日程对新会话静默失效。
        umo = event.unified_msg_origin
        if self.daily_schedule_service.enabled():
            try:
                await self.daily_schedule_service.register_session_async(
                    umo, event=event
                )
            except Exception as e:
                logger.warning(f"{tag(event)} ⚠️ AI 日程会话注册失败: {e}")

        if not self.time_context.time_guidance_enabled():
            return
        try:
            existing = (getattr(req, "system_prompt", "") or "").strip()
            rules = DEFAULT_TIME_GUIDANCE_PROMPT
            # 幂等：同一 ProviderRequest 被重复处理（外部重试/多 provider 分流）
            # 时不再叠加规则文本，避免污染 system_prompt 前缀缓存
            if rules not in existing:
                req.system_prompt = (
                    f"{existing.rstrip()}\n\n{rules}" if existing else rules
                )
            # 记录用户消息时间——on_llm_request 天然过滤命令（命令不调 LLM）。
            # 单独 try：record 失败不应影响 dyn 注入（否则 LLM 这次完全没时间感知）
            if self.time_context.last_chat_enabled() and umo:
                try:
                    self.last_chat_tracker.record_user(
                        umo, get_now(self.config, self._astrbot_config())
                    )
                except Exception as e:
                    logger.warning(f"{tag(event)} ⚠️ record_user 失败: {e}")
            # 动态注入单独隔离：任一传感器异常不应让整个动态上下文消失
            try:
                weather_text = None
                if self.weather_sensor.enabled():
                    try:
                        current = await self.weather_sensor.current_weather()
                        if current:
                            weather_text = self.weather_sensor.render_current(current)
                    except Exception as e:
                        logger.debug(f"{tag(event)} 天气实况查询失败: {e}")
                dyn = self.time_context.build_dynamic(
                    session=umo,
                    include_last_chat=not is_chat_memory_takeover_applied(event),
                    weather_text=weather_text,
                )
                req.extra_user_content_parts.append(TextPart(text=dyn).mark_as_temp())
            except Exception as e:
                logger.warning(f"{tag(event)} ⚠️ 动态时间上下文构建失败: {e}")
        except Exception as e:
            logger.error(f"{tag(event)} ❌ on_llm_request 注入失败: {e}")

    @filter.after_message_sent()
    async def _record_ai_sent_time(self, event: AstrMessageEvent):
        """AstrBot 消息发送流程结束后记录时间——用于 ``<LAST_CHAT>`` 标签。

        命令消息（以 ``/`` 开头）不计入：避免 ``/calendar show`` 这类管理操作被
        误判为「聊天」。该 Hook 表示发送阶段已尝试完成，不是平台送达确认。
        """
        if not self.time_context.last_chat_enabled():
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
        yield event.plain_result(self.calendar_commands.help_text())

    @calendar_group.command("show")
    async def cmd_list(self, event: AstrMessageEvent, month: str = ""):
        """列出指定月份的事项（用户 + 内置合并）。"""
        yield event.plain_result(self.calendar_commands.show(month))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @calendar_group.command("add")
    async def cmd_add(self, event: AstrMessageEvent, date_str: str = "", repeat_or_title: str = "", title: str = ""):
        """新增一条事项。"""
        yield event.plain_result(
            self.calendar_commands.add(date_str, repeat_or_title, title)
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @calendar_group.command("del")
    async def cmd_delete(self, event: AstrMessageEvent, event_id: str = ""):
        """按 id 删除用户事项。"""
        yield event.plain_result(self.calendar_commands.delete(event_id))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @calendar_group.command("create")
    async def cmd_generate(self, event: AstrMessageEvent):
        """按配置里的世界观重新生成完整日历并导入。"""
        async for text in self.calendar_commands.generate(event.unified_msg_origin or ""):
            yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @calendar_group.command("export")
    async def cmd_export(self, event: AstrMessageEvent):
        """导出当前数据为 YAML 文本。"""
        yield event.plain_result(self.calendar_commands.export_yaml())

    @filter.permission_type(filter.PermissionType.ADMIN)
    @calendar_group.command("import")
    async def cmd_import(self, event: AstrMessageEvent, replace: str = ""):
        """从回复的 YAML 文本批量导入；加 replace 参数先清空再导入。

        使用方式：回复一条 YAML 文本消息，再发送 /calendar import [replace]。
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

        yield event.plain_result(
            self.calendar_commands.import_yaml(
                content, replace=str(replace or "").strip().lower() == "replace"
            )
        )

    # ==================== 内置事件管理 ====================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @calendar_group.command("builtin_regen")
    async def cmd_builtin_regen(self, event: AstrMessageEvent):
        """重新生成当年的内置现实日历事件。"""
        yield event.plain_result(self.calendar_commands.builtin_regenerate())

    @filter.permission_type(filter.PermissionType.ADMIN)
    @calendar_group.command("builtin_list")
    async def cmd_builtin_list(self, event: AstrMessageEvent, category: str = ""):
        """列出当年所有内置事件，可按分类过滤。"""
        yield event.plain_result(self.calendar_commands.builtin_list(category))

    # ==================== 单日日程表 ====================

    @filter.command_group("schedule")
    def schedule_group(self):
        """单日日程表管理命令组入口（时段状态感知）。"""

    @schedule_group.command("help")
    async def schedule_help(self, event: AstrMessageEvent):
        """显示日程表命令帮助。"""
        yield event.plain_result(self.schedule_commands.help_text())

    @schedule_group.command("show")
    async def schedule_show(self, event: AstrMessageEvent):
        """显示当前 Persona 今天实际使用的 AI 快照或静态日程。"""
        session = event.unified_msg_origin or ""
        if session:
            await self.daily_schedule_service.register_session_async(session, event=event)
        yield event.plain_result(self.schedule_commands.show(session))

    @schedule_group.command("static")
    async def schedule_static(self, event: AstrMessageEvent):
        """显示静态日程配置（schedule_templates）。"""
        yield event.plain_result(self.schedule_commands.static())

    @filter.permission_type(filter.PermissionType.ADMIN)
    @schedule_group.command("regenerate")
    async def schedule_regenerate(self, event: AstrMessageEvent):
        """强制重新生成当前 Persona 的今日 AI 快照。"""
        async for text in self.schedule_commands.regenerate(
            event.unified_msg_origin or "",
            event=event,
        ):
            yield event.plain_result(text)

    # ==================== LLM 工具：schedule_followup ====================

    @filter.llm_tool(name="schedule_followup")
    async def schedule_followup_tool(
        self,
        event: AstrMessageEvent,
        delay_minutes: int,
        hint: str,
    ) -> str:
        """
        安排一个稍后触发的主动消息：到点后你会再次被唤醒，并收到当时的时间、角色状态与本条备忘，
        以便延续当前的状态和话题。

        触发场景：
        - 用户明确要求「X 分钟后提醒我」「晚点再聊」等
        - 你和用户约定了稍后继续某事（如「等我下」「一会儿告诉你」）

        不要触发的场景：
        - 用户没明确要求时主动定时（避免打扰）
        - 已完成的事或闲聊中无事可约

        Args:
            delay_minutes(int): 几分钟后触发。范围 1-1440（最多 24 小时）。
            hint(string): 给「未来的自己」的备忘。必填。写清楚约定了什么、
                要继续什么、以什么口吻回应。
        """
        reminder_cfg = self.config.get("reminder", {})
        if not isinstance(reminder_cfg, dict):
            reminder_cfg = {}
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
        if session not in self.reminder_service.targets():
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
        hint_preview = f"（备忘：{hint}）"
        time_str = fire_at.strftime('%H:%M')
        return (
            f"已安排 {delay} 分钟后（{time_str}）触发主动消息{hint_preview}。\n"
            f"本轮无需向用户复述本次工具调用——若刚才已和用户说过安排，直接返回空回复即可；若未说过，简短告知一声即可。"
        )
