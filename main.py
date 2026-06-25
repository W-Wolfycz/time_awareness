"""
time_awareness — 时间感知与智能日历

为 LLM 注入：
1. 静态：时间感知增强 prompt → system_prompt 末尾
2. 动态：若处于睡眠窗口 → extra_user_content_parts
3. 占位符：解析 {calendar_today}（在 1 / 2 文本上做 replace）

日历管理通过聊天命令 `/calendar show|add|del|create|export|import|help`。

主动提醒：
- 当日日历事项在指定时刻主动发送到白名单会话
- LLM 可通过 schedule_followup 工具自行安排"X 分钟后再说"
"""

import asyncio
import datetime
import os

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools

from .constants import (
    DEFAULT_LATE_PROMPT,
    DEFAULT_TIME_GUIDANCE_PROMPT,
    LEGACY_DEFAULT_TIME_GUIDANCE_PROMPT,
)
from .core.builtin_events import (
    ALL_CATEGORIES,
    CATEGORY_LEGAL,
    CATEGORY_POLITICAL,
    CATEGORY_INTERNATIONAL,
    CATEGORY_SOLAR_TERM,
    CATEGORY_TRADITIONAL,
)
from .core.builtin_manager import BuiltinManager
from .core.calendar_manager import CalendarManager
from .core.calendar_store import REPEAT_FOREVER, REPEAT_NONE, calendar_store
from .core.scheduler import DueGroup, ReminderScheduler
from .llm.calendar_generator import (
    DEFAULT_MAX_GENERATE,
    build_system_prompt,
    generate_calendar_events,
)
from .utils.time_utils import get_now, get_sleep_prompt_if_active, get_tz

_PREFIX = "[time_awareness]"
PLUGIN_DATA_DIR_NAME = "time_awareness"


class CalendarPlusPlugin(Star):
    """时间感知 + 智能日历 + 主动提醒。"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}

        # 数据目录
        try:
            self.data_dir = str(StarTools.get_data_dir(PLUGIN_DATA_DIR_NAME))
        except Exception as e:
            base = os.path.join(os.getcwd(), "data", "plugin_data", PLUGIN_DATA_DIR_NAME)
            os.makedirs(base, exist_ok=True)
            self.data_dir = base
            logger.warning(
                f"{_PREFIX} ⚠️ StarTools.get_data_dir 不可用，回退到 {base}: {e}"
            )

        self.calendar_manager = CalendarManager(self.data_dir)
        self.builtin_manager = BuiltinManager(self.data_dir)
        # 调度器：trigger_callback 委托给 self._on_due
        self.scheduler = ReminderScheduler(self.data_dir, self._on_due)
        self._apply_reminder_config()
        # 每日 0 点扫描当天事项的 asyncio task
        self._daily_scan_task: asyncio.Task | None = None
        # chat_memory 实例缓存（成功解析后缓存；失败不缓存以便下次重试）
        self._chat_memory = None

        logger.info(f"{_PREFIX} 插件已初始化，数据目录: {self.data_dir}")

    async def initialize(self):
        """AstrBot 启动时调用：加载数据 + 启动调度。"""
        self.calendar_manager.load()

        # 内置事件：跨年/分类开关变更时自动 regen
        try:
            now = get_now(self.config, self._astrbot_config())
            year = now.year
            categories = self._enabled_builtin_categories()
            if categories:
                count = self.builtin_manager.ensure_fresh(year, categories)
                logger.info(f"{_PREFIX} 📅 内置事件已就绪: {count} 条 (year={year})")
                # 加载到内存单例
                builtin_data = self.builtin_manager.load_raw()
                calendar_store.set_builtin_events(builtin_data.get("events") or [])
            else:
                calendar_store.set_builtin_events([])
        except Exception as e:
            logger.error(f"{_PREFIX} ❌ 内置事件初始化失败: {e}")
            calendar_store.set_builtin_events([])

        self._apply_reminder_config()
        self.scheduler.load()

        reminder_cfg = self.config.get("reminder", {})
        if reminder_cfg.get("enable_reminder", False):
            # 启动时先扫一遍今天的事项入队（错过当日提醒时刻仍可补发）
            self._scan_and_enqueue_today_events()
            self.scheduler.start()
            # 启动每日 0 点扫描循环
            self._daily_scan_task = asyncio.create_task(self._daily_scan_loop())

        logger.info(f"{_PREFIX} ✅ 初始化完成")

    async def terminate(self):
        """AstrBot 卸载/关闭时调用：停调度循环 + 每日扫描。"""
        if self._daily_scan_task is not None:
            self._daily_scan_task.cancel()
            try:
                await self._daily_scan_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"{_PREFIX} ⚠️ 每日扫描 task 停止异常: {e}")
            self._daily_scan_task = None
        try:
            await self.scheduler.stop()
        except Exception as e:
            logger.warning(f"{_PREFIX} ⚠️ 调度器停止异常: {e}")
        logger.info(f"{_PREFIX} ✅ 已终止")

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
                    sleep_sec = max(60, (next_midnight - now).total_seconds())
                    await asyncio.sleep(sleep_sec)
                    # 醒来，扫今天的入队（add_calendar_task 内部会唤醒调度器）
                    self._scan_and_enqueue_today_events()
                    # 跨年时自动 regen 内置事件
                    self._check_builtin_fresh()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"{_PREFIX} ❌ 每日扫描异常（继续循环）: {e}")
                    await asyncio.sleep(60)
        except asyncio.CancelledError:
            logger.info(f"{_PREFIX} 每日扫描循环已取消")

    def _check_builtin_fresh(self) -> None:
        """检查内置事件是否需要重新生成（跨年或分类变更）。"""
        try:
            categories = self._enabled_builtin_categories()
            if not categories:
                return
            now = get_now(self.config, self._astrbot_config())
            if not self.builtin_manager.is_stale(now.year, categories):
                return
            count = self.builtin_manager.regenerate(now.year, categories)
            if count >= 0:
                builtin_data = self.builtin_manager.load_raw()
                calendar_store.set_builtin_events(builtin_data.get("events") or [])
                logger.info(
                    f"{_PREFIX} 📅 跨年/配置变更自动 regen 内置事件: "
                    f"year={now.year} events={count}"
                )
        except Exception as e:
            logger.error(f"{_PREFIX} ❌ 内置事件跨年 regen 失败: {e}")

    # ==================== 提醒配置 ====================

    def _apply_reminder_config(self) -> None:
        """把 reminder 组配置下发给调度器。"""
        reminder_cfg = self.config.get("reminder", {})
        max_late = int(reminder_cfg.get("reminder_max_late_minutes", 60) or 60)
        self.scheduler.set_max_late_minutes(max_late)

    def _reminder_targets(self) -> list:
        """白名单 UMO 列表（已 strip、过滤空项）。"""
        targets = self.config.get("reminder", {}).get("reminder_targets", []) or []
        if isinstance(targets, str):
            # 兼容字符串换行格式
            targets = [t.strip() for t in targets.split("\n") if t.strip()]
        return [str(t).strip() for t in targets if str(t).strip()]

    def _late_prompt(self) -> str:
        """迟到提示词（空或等于旧默认值时回退到新默认值，未来兼容预留）。"""
        text = (self.config.get("reminder", {}).get("reminder_late_prompt") or "").strip()
        return text or DEFAULT_LATE_PROMPT

    # ==================== 调度回调 ====================

    async def _on_due(self, group: DueGroup) -> None:
        """到期任务组的触发回调：合并所有任务，调一次 LLM 生成回复并发出。"""
        try:
            await self._fire_with_llm(
                group.session, group.tasks, group.is_late, group.late_minutes
            )
        except Exception as e:
            logger.error(f"{_PREFIX} ❌ _on_due 异常 session={group.session}: {e}")

    async def _fire_with_llm(
        self,
        session: str,
        tasks: list,
        is_late: bool,
        late_minutes: int,
    ) -> None:
        """合并多任务成一次 LLM 调用，把回复发出去。

        完整上下文：
        - system_prompt = 人设 prompt + 时间引导 prompt + 睡眠提示（如在睡眠窗口）
        - contexts（对话历史）= 从 chat_memory 插件按 (umo, cid) 拉取，不按用户过滤
          （群聊场景：整个群的所有人混合历史）。CM 未安装 / 拉取失败 → 降级为不带历史
        - prompt（本轮 user）= 各任务的提示文本拼接 + 迟到提示（如迟到）
        """
        # ---- 1. 构造 system_prompt ----
        system_parts = []

        # 人设 prompt（参考 emotion_favour 的简化方案）
        try:
            persona = await self.context.persona_manager.get_default_persona_v3(session)
            persona_prompt = (persona or {}).get("prompt") or ""
            if persona_prompt:
                system_parts.append(persona_prompt)
        except Exception as e:
            logger.warning(f"{_PREFIX} ⚠️ 获取人设失败 session={session}: {e}")

        # 时间引导 prompt（time_awareness 自己的）
        guidance = self._resolve_placeholders(self._effective_time_guidance_prompt())
        if guidance:
            system_parts.append(guidance)

        # 睡眠提示词（睡眠窗口内附加，让 LLM 回复带困意）
        sleep_prompt = get_sleep_prompt_if_active(self.config, self._astrbot_config())
        if sleep_prompt:
            system_parts.append(sleep_prompt)

        system_prompt = "\n\n".join(system_parts)

        # ---- 2. 拉对话历史（从 chat_memory 插件）----
        contexts = await self._fetch_history_contexts(session)

        # ---- 3. 构造本轮 prompt ----
        parts = []
        for t in tasks:
            if t.kind == "calendar":
                parts.append(f"今天是 {t.calendar_event_text}，可自然提一下")
            elif t.kind == "followup":
                if t.hint:
                    parts.append(f"你之前承诺过：{t.hint}。现在到点了，请继续")
                else:
                    parts.append("你之前承诺过现在到点了，请按当时承诺继续")
        prompt = "\n".join(parts)

        if is_late:
            late_text = self._late_prompt().replace(
                "{delay_minutes}", str(late_minutes)
            )
            prompt = f"{prompt}\n\n{late_text}"

        # ---- 4. 调 LLM ----
        try:
            llm_kwargs = {
                "prompt": prompt,
                "system_prompt": system_prompt,
            }
            if contexts:
                llm_kwargs["contexts"] = contexts
            llm_response = await self.context.llm_generate(**llm_kwargs)
            if not llm_response or llm_response.role != "assistant":
                logger.warning(
                    f"{_PREFIX} ⚠️ LLM 响应异常 session={session}: {llm_response}"
                )
                return
            reply = (llm_response.completion_text or "").strip()
            if not reply:
                logger.warning(f"{_PREFIX} ⚠️ LLM 返回空回复 session={session}")
                return
            await self.context.send_message(session, _build_text_chain(reply))
            logger.info(
                f"{_PREFIX} ✅ LLM 触发已发: session={session} tasks={len(tasks)} "
                f"late={is_late} history={len(contexts)}"
            )
        except Exception as e:
            logger.error(f"{_PREFIX} ❌ LLM 触发失败 session={session}: {e}")

    async def _fetch_history_contexts(self, session: str) -> list:
        """从 chat_memory 插件拉取该会话最近 N 轮对话历史，作为 llm_generate 的 contexts。

        - 不传 user_id（拿群聊整群的混合历史，群聊场景下整群共享一个 cid）
        - 每条历史在 content 前加 ``[YYYY-MM-DD HH:MM]`` 时间戳前缀，让 LLM 能感知
          对话发生时间（token 开销很小，但有利于「好久不见」之类的自然语气）
        - chat_memory 未安装 / 拉取失败 → 返回空 list，主流程降级为不带历史
        """
        reminder_cfg = self.config.get("reminder", {})
        if not reminder_cfg.get("include_history", True):
            return []

        try:
            rounds = int(reminder_cfg.get("history_rounds", 10) or 10)
        except (TypeError, ValueError):
            rounds = 10
        rounds = max(1, min(50, rounds))
        limit = rounds * 2  # 每轮 = user + assistant

        # 拿当前 conversation_id
        try:
            cid = await self.context.conversation_manager.get_curr_conversation_id(session)
        except Exception as e:
            logger.warning(
                f"{_PREFIX} ⚠️ 获取 conversation_id 失败 session={session}: {e}"
            )
            return []
        if not cid:
            logger.debug(f"{_PREFIX} 无 conversation_id，跳过历史拉取 session={session}")
            return []

        cm = self._resolve_chat_memory()
        if cm is None:
            logger.debug(f"{_PREFIX} chat_memory 未安装，跳过历史拉取")
            return []

        try:
            records = await cm.query_history(session, cid, None, limit)
        except Exception as e:
            logger.warning(
                f"{_PREFIX} ⚠️ 从 chat_memory 拉取历史失败 session={session}: {e}"
            )
            return []

        contexts = []
        for r in records:
            role = r.get("role")
            content = r.get("content") or ""
            if not role or not content:
                continue
            # 截取前 16 字符 "YYYY-MM-DD HH:MM"（CM 存的可能是完整 datetime 字符串）
            created = str(r.get("created_at", ""))[:16].strip()
            if created:
                contexts.append({"role": role, "content": f"[{created}] {content}"})
            else:
                contexts.append({"role": role, "content": content})
        return contexts

    def _resolve_chat_memory(self):
        """定位 chat_memory 插件实例。AstrBot 注册表 → sys.modules fallback。

        成功后缓存到 ``self._chat_memory``；失败不缓存以便下次重试。
        与 emotion_favour / llm_sentinel 的解析逻辑保持一致。
        """
        if self._chat_memory is not None:
            return self._chat_memory
        try:
            star = self.context.get_registered_star("chat_memory")
            if star is not None:
                # 不同 AstrBot 版本包装层级可能不同，依次尝试
                for candidate in (
                    star,
                    getattr(star, "star", None),
                    getattr(star, "star_cls", None),
                ):
                    if candidate is not None and hasattr(candidate, "query_history"):
                        self._chat_memory = candidate
                        return candidate
        except Exception:
            pass
        import sys
        mod = sys.modules.get("chat_memory.main") or sys.modules.get("chat_memory")
        if mod is not None and hasattr(mod, "query_history"):
            self._chat_memory = mod
            return mod
        return None

    # ==================== 每日日历扫描 ====================

    def _scan_and_enqueue_today_events(self) -> int:
        """扫今天的日历事项，对每个白名单 session 入队（已入队的不重复）。"""
        reminder_cfg = self.config.get("reminder", {})
        if not reminder_cfg.get("enable_reminder", False):
            return 0
        targets = self._reminder_targets()
        if not targets:
            logger.warning(f"{_PREFIX} ⚠️ reminder 启用但 reminder_targets 为空，跳过扫描")
            return 0

        now = get_now(self.config, self._astrbot_config())
        today = now.date()
        today_events = calendar_store.events_for_date(today.year, today.month, today.day)
        if not today_events:
            return 0

        # 计算 fire_at：今日 reminder_time，若已过则立即入队（调度器会立刻触发）
        time_str = reminder_cfg.get("reminder_time", "09:00") or "09:00"
        try:
            hh, mm = time_str.split(":")
            fire_at = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        except (ValueError, AttributeError):
            logger.warning(f"{_PREFIX} ⚠️ reminder_time 格式错误: {time_str}，回退 09:00")
            fire_at = now.replace(hour=9, minute=0, second=0, microsecond=0)

        enqueued = 0
        for session in targets:
            for event in today_events:
                event_id = str(event.get("id", ""))
                if self.scheduler.has_calendar_task_for_session_today(
                    session, event_id, today
                ):
                    continue
                self.scheduler.add_calendar_task(
                    session=session,
                    fire_at=fire_at,
                    event_id=event_id,
                    event_text=str(event.get("text", "")),
                )
                enqueued += 1
        if enqueued > 0:
            logger.info(f"{_PREFIX} 📅 今日事项扫描入队 {enqueued} 条")
        return enqueued

    # ==================== AstrBot 全局配置 ====================

    def _astrbot_config(self):
        try:
            return self.context.get_config()
        except Exception:
            return None

    # ==================== 时间引导 / 睡眠 / 占位符 ====================

    def _effective_time_guidance_prompt(self) -> str:
        """读取自定义提示词；为空或等于旧默认值时回退到新默认值。"""
        ta = self.config.get("time_awareness", {})
        if not ta.get("time_guidance_enabled", True):
            return ""
        custom = (ta.get("time_guidance_prompt") or "").strip()
        if not custom or custom == LEGACY_DEFAULT_TIME_GUIDANCE_PROMPT:
            return DEFAULT_TIME_GUIDANCE_PROMPT
        return custom

    def _resolve_calendar_today(self) -> str:
        """{calendar_today} 的取值：当日事项按 separator 拼接，无事项返回 empty_text。"""
        cal = self.config.get("calendar", {})
        if not cal.get("enable_calendar", False):
            return cal.get("calendar_empty_text", "") or ""
        now = get_now(self.config, self._astrbot_config())
        separator = cal.get("calendar_separator", "、")
        empty_text = cal.get("calendar_empty_text", "") or ""
        include_builtin = cal.get("enable_builtin_events", True) and bool(self._enabled_builtin_categories())
        return calendar_store.today_text(
            now, separator=separator, empty_text=empty_text, include_builtin=include_builtin
        )

    # ==================== 内置日历事件 ====================

    def _enabled_builtin_categories(self) -> list:
        """根据配置返回启用的内置事件分类列表。

        受总开关 ``calendar.enable_builtin_events`` 与各分类子开关共同控制。
        5 类开关位于 ``calendar.builtin_events`` 子组。
        """
        cal = self.config.get("calendar", {})
        if not cal.get("enable_builtin_events", True):
            return []
        be = cal.get("builtin_events", {})
        result = []
        if be.get("legal_holidays", True):
            result.append(CATEGORY_LEGAL)
        if be.get("traditional", True):
            result.append(CATEGORY_TRADITIONAL)
        if be.get("solar_terms", True):
            result.append(CATEGORY_SOLAR_TERM)
        if be.get("political", False):
            result.append(CATEGORY_POLITICAL)
        if be.get("international", False):
            result.append(CATEGORY_INTERNATIONAL)
        return result

    def _resolve_placeholders(self, text: str) -> str:
        if not text:
            return ""
        return text.replace("{calendar_today}", self._resolve_calendar_today())

    @staticmethod
    def _append_system_prompt(req, additional: str) -> None:
        additional = (additional or "").strip()
        if not additional:
            return
        system_prompt = getattr(req, "system_prompt", "") or ""
        if system_prompt.strip():
            req.system_prompt = f"{system_prompt.rstrip()}\n\n{additional}"
        else:
            req.system_prompt = additional

    @staticmethod
    def _append_dynamic_content(req, text: str) -> None:
        """将动态上下文追加到 extra_user_content_parts 末尾（位于本轮用户输入之后）。"""
        text = (text or "").strip()
        if not text:
            return
        try:
            from astrbot.core.agent.message import TextPart
        except ImportError:
            logger.warning(f"{_PREFIX} ⚠️ 无法导入 TextPart，跳过附带信息注入")
            return
        part = TextPart(text=text)
        mark_as_temp = getattr(part, "mark_as_temp", None)
        if callable(mark_as_temp):
            part = mark_as_temp()
        req.extra_user_content_parts.append(part)

    @filter.on_llm_request()
    async def inject_time_context(self, event: AstrMessageEvent, req: ProviderRequest):
        """三件事：时间引导 prompt（静态）→ system_prompt；睡眠提示（动态）→ extra_user_content_parts；解析占位符。"""
        try:
            guidance = self._resolve_placeholders(self._effective_time_guidance_prompt())
            self._append_system_prompt(req, guidance)

            sleep_prompt = get_sleep_prompt_if_active(self.config, self._astrbot_config())
            if sleep_prompt:
                sleep_prompt = self._resolve_placeholders(sleep_prompt)
                self._append_dynamic_content(req, sleep_prompt)
        except Exception as e:
            logger.error(f"{_PREFIX} ❌ on_llm_request 注入失败: {e}")

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
            "/calendar del <id 或 文本>  — 删除事项（管理员；内置事件删除后会被加入黑名单不再生成）\n"
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
            "/calendar del 元旦  — 按文本删除内置事件\n"
            "\n"
            "内置现实日历事件（默认开法定/传统/节气，关政治/国际）：\n"
            "- 法定节假日（含调休）、传统农历节日、二十四节气\n"
            "- 政治纪念日、国际/西方节日\n"
            "- 跨年自动重新生成；/calendar del 删除的项会被加入黑名单\n"
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
            parsed = self._parse_year_month(month, now)
            if parsed is None:
                yield event.plain_result(f"格式错误：{month}，应为 YYYY-MM 或 YYYY-MM-DD。")
                return
            year, mon = parsed

        include_builtin = bool(self._enabled_builtin_categories())
        events = calendar_store.events_for_month(year, mon, include_builtin=include_builtin)
        if not events:
            yield event.plain_result(f"{year}-{mon:02d} 无事项。")
            return

        lines = [f"[{year}-{mon:02d} 共 {len(events)} 条]"]
        for e in events:
            day = e.get("day", 0)
            text = e.get("text", "")
            if e.get("source") == "builtin":
                cat_tag = self._category_short_tag(e.get("category", ""))
                lines.append(f"  {mon:02d}-{day:02d} {text} [{cat_tag}]")
            else:
                repeat_tag = self._describe_repeat(e.get("repeat", 0))
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

        parsed = self._parse_event_date(date_str)
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
            f"{self._describe_repeat(event_obj['repeat'])}）"
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

        target = self._find_event_by_short_id(event_id)
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

        provider_id = cal.get("ai_generate_provider_id", "") or ""
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
        total = len(calendar_store.events)
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
        calendar_store.set_builtin_events(builtin_data.get("events") or [])

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
        cat_filter = self._parse_category_input(category)
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
            cat_tag = self._category_short_tag(e.get("category", ""))
            text = e.get("text", "")
            lines.append(
                f"  {e.get('month', 0):02d}-{e.get('day', 0):02d} {text} [{cat_tag}]"
            )
        yield event.plain_result("\n".join(lines))

    def _parse_category_input(self, text: str) -> str:
        """把用户输入的分类名（中/英）映射到常量。空返回空串。"""
        text = (text or "").strip()
        if not text:
            return ""
        mapping = {
            "法定": CATEGORY_LEGAL, "法定节假日": CATEGORY_LEGAL, "legal": CATEGORY_LEGAL,
            "传统": CATEGORY_TRADITIONAL, "traditional": CATEGORY_TRADITIONAL,
            "节气": CATEGORY_SOLAR_TERM, "solar": CATEGORY_SOLAR_TERM, "solar_term": CATEGORY_SOLAR_TERM,
            "政治": CATEGORY_POLITICAL, "政治纪念": CATEGORY_POLITICAL, "political": CATEGORY_POLITICAL,
            "国际": CATEGORY_INTERNATIONAL, "international": CATEGORY_INTERNATIONAL,
            "西方": CATEGORY_INTERNATIONAL,
        }
        return mapping.get(text.lower(), "")

    # ==================== LLM 工具：schedule_followup ====================

    @filter.llm_tool(name="schedule_followup")
    async def schedule_followup_tool(
        self,
        event: AstrMessageEvent,
        delay_minutes: int,
        hint: str = "",
    ) -> str:
        """
        当你对用户承诺「X 分钟后再说/再做某事」时调用此工具，到点会自动主动发消息给当前会话。
        到点时插件会自动调用你（带人设/历史/睡眠状态等完整上下文），由你生成符合人设的回复。
        仅当前会话在管理员提醒白名单内时才会真正入队，否则返回失败。

        Args:
            delay_minutes(int): 几分钟后触发（范围 1-1440，即最多 24 小时）
            hint(string): 可选。备忘录——到点时你会看到这条提示，提醒自己当时承诺了什么。建议简短（如"准备好了，出发"）
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

        # 白名单校验
        session = event.unified_msg_origin or ""
        if session not in self._reminder_targets():
            return "失败：当前会话不在提醒白名单内"

        now = get_now(self.config, self._astrbot_config())
        fire_at = now + datetime.timedelta(minutes=delay)
        self.scheduler.add_followup_task(
            session=session,
            fire_at=fire_at,
            hint=(hint or "").strip(),
        )
        hint_preview = f"（备忘：{(hint or '').strip()[:30]}）" if (hint or "").strip() else ""
        return f"已安排 {delay} 分钟后（{fire_at.strftime('%H:%M')}）主动发消息{hint_preview}"

    # ==================== 辅助函数 ====================

    @staticmethod
    def _describe_repeat(repeat: int) -> str:
        if repeat == REPEAT_FOREVER:
            return "（每年）"
        if repeat == REPEAT_NONE:
            return ""
        return f"（连续 {repeat + 1} 年）"

    @staticmethod
    def _category_short_tag(category: str) -> str:
        """内置事件分类的短标签（用于 /calendar show 显示）。"""
        return {
            CATEGORY_LEGAL: "法定",
            CATEGORY_TRADITIONAL: "传统",
            CATEGORY_SOLAR_TERM: "节气",
            CATEGORY_POLITICAL: "政治",
            CATEGORY_INTERNATIONAL: "国际",
        }.get(category, "内")

    def _find_event_by_short_id(self, short: str) -> dict | None:
        """根据 id 前缀查找事件（同时查 user + builtin），支持完整 id 或前 8 位短 id。"""
        short = (short or "").strip().lower()
        if not short:
            return None
        # builtin ID 形如 "builtin:legal_holiday:01-01:元旦"，short 用前 8 位不直观；
        # 所以对 builtin ID 还要支持按文本匹配（如 "元旦"、"国庆节"）
        for e in list(calendar_store.events) + list(calendar_store.builtin_events):
            full = str(e.get("id", "")).lower()
            if full == short or full.startswith(short):
                return e
        # 文本兜底匹配
        for e in list(calendar_store.events) + list(calendar_store.builtin_events):
            if str(e.get("text", "")).strip().lower() == short:
                return e
        return None

    def _parse_year_month(self, text: str, now) -> tuple | None:
        """解析 YYYY-MM 或 YYYY-MM-DD，返回 (year, month)。"""
        text = (text or "").strip()
        try:
            if "-" in text:
                parts = text.split("-")
                if len(parts) >= 2:
                    year = int(parts[0])
                    month = int(parts[1])
                    if 1 <= month <= 12 and 1970 <= year <= 9999:
                        return year, month
        except ValueError:
            return None
        return None

    def _parse_event_date(self, text: str) -> tuple | None:
        """解析事件日期。返回 (month, day, year_or_None)。

        支持 YYYY-MM-DD（基准年为指定年份）或 MM-DD（基准年由上层回填）。
        """
        text = (text or "").strip()
        if not text:
            return None
        try:
            parts = text.split("-")
            if len(parts) == 3:
                year = int(parts[0])
                month = int(parts[1])
                day = int(parts[2])
                return month, day, year
            if len(parts) == 2:
                month = int(parts[0])
                day = int(parts[1])
                return month, day, None
        except ValueError:
            return None
        return None


def _build_text_chain(text: str):
    """构造只含一条文本的 MessageChain，用于 context.send_message。"""
    from astrbot.api.event import MessageChain

    return MessageChain().message(text)
