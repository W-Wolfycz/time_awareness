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
import random
import re

from astrbot.api import AstrBotConfig
from .log import logger, configure as configure_log, tag
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools

from .constants import (
    DEFAULT_LATE_PROMPT,
    DEFAULT_SCHEDULE_GENERATE_SYSTEM_PROMPT,
    DEFAULT_TIME_GUIDANCE_PROMPT,
    LEGACY_DEFAULT_TIME_GUIDANCE_PROMPT,
)
from .migrate import migrate as migrate_config
from .core.builtin_events import (
    ALL_CATEGORIES,
    CATEGORY_ALMANAC,
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
from .utils.time_utils import find_active_schedule_slot, get_now, get_tz
from .web_api import register_web_apis

PLUGIN_DATA_DIR_NAME = "time_awareness"

# 匹配 LLM 回复中误输出的 <time>...</time> 标签（注入用，不应出现在回复正文）。
# 同时覆盖成对标签、自闭合、单独开/闭标签、带属性等形式。
_TIME_TAG_PATTERN = re.compile(
    r'<time(?:\s[^>]*)?>.*?</time>|</?time(?:\s[^>]*)?/?>',
    re.IGNORECASE | re.DOTALL,
)


class CalendarPlusPlugin(Star):
    """时间感知 + 智能日历 + 主动提醒。"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}

        # 配置链式迁移：v0 → v1（sleep_* → daily_schedule.schedule_templates[0]）等
        # 迁移幂等可重试，失败时保留原 config 不 bump 版本号
        # 仅在真发生迁移时落盘，避免每次启动都写文件
        try:
            version_before = self.config.get("config_version", 0) if isinstance(self.config, dict) else 0
            migrated = migrate_config(self.config)
            if migrated is not self.config:
                self.config = migrated
            version_after = self.config.get("config_version", 0) if isinstance(self.config, dict) else 0
            if version_after != version_before:
                self._try_save_config()
        except Exception as e:
            logger.warning(f"{tag()} ⚠️ 配置迁移调用异常（忽略，继续启动）: {e}")

        # 日志提级：debug → info（参照 chat_memory 的 debug_to_info 模式）
        # 日志前缀带 bot 实例标识：区分多 bot 共存场景（参照 emotion_favour）
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

        self.calendar_manager = CalendarManager(self.data_dir)
        self.builtin_manager = BuiltinManager(self.data_dir)
        # 调度器：trigger_callback 委托给 self._on_due；
        # now_provider 让 scheduler 用与 fire_at 同时区的 now（避免 aware/naive 比较报错）
        self.scheduler = ReminderScheduler(
            self.data_dir,
            self._on_due,
            now_provider=lambda: get_now(self.config, self._astrbot_config()),
        )
        self._apply_reminder_config()
        # 每日 0 点扫描当天事项的 asyncio task
        self._daily_scan_task: asyncio.Task | None = None
        # chat_memory 实例缓存（成功解析后缓存；失败不缓存以便下次重试）
        self._chat_memory = None

        logger.info(f"{tag()} 插件已初始化，数据目录: {self.data_dir}")

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
                logger.info(f"{tag()} 📅 内置事件已就绪: {count} 条 (year={year})")
                # 加载到内存单例
                builtin_data = self.builtin_manager.load_raw()
                calendar_store.set_builtin_events(builtin_data.get("events") or [])
            else:
                calendar_store.set_builtin_events([])
        except Exception as e:
            logger.error(f"{tag()} ❌ 内置事件初始化失败: {e}")
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
                    sleep_sec = max(60, (next_midnight - now).total_seconds())
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
                return
            now = get_now(self.config, self._astrbot_config())
            if not self.builtin_manager.is_stale(now.year, categories):
                return
            count = self.builtin_manager.regenerate(now.year, categories)
            if count >= 0:
                builtin_data = self.builtin_manager.load_raw()
                calendar_store.set_builtin_events(builtin_data.get("events") or [])
                logger.info(
                    f"{tag()} 📅 跨年/配置变更自动 regen 内置事件: "
                    f"year={now.year} events={count}"
                )
        except Exception as e:
            logger.error(f"{tag()} ❌ 内置事件跨年 regen 失败: {e}")

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
        task_summary = ",".join(f"{t.kind}:{t.id[:8]}" for t in group.tasks)
        late_info = f" late={group.late_minutes}min" if group.is_late else ""
        logger.info(
            f"{tag()} 🔔 触发到期任务: session={group.session} "
            f"tasks={len(group.tasks)} [{task_summary}]{late_info}"
        )
        try:
            await self._fire_with_llm(
                group.session, group.tasks, group.is_late, group.late_minutes
            )
        except Exception as e:
            logger.error(f"{tag()} ❌ _on_due 异常 session={group.session}: {e}")

    async def _fire_with_llm(
        self,
        session: str,
        tasks: list,
        is_late: bool,
        late_minutes: int,
    ) -> None:
        """合并多任务成一次 LLM 调用，把回复发出去。

        完整上下文：
        - system_prompt = 人设 prompt + 时间引导 prompt（含 {daily_schedule_state} 占位符，
          命中睡眠/清醒时段时自动展开为对应 state_prompt）
        - contexts（对话历史）= 从 chat_memory 插件按 (umo, cid) 拉取，不按用户过滤
          （群聊场景：整个群的所有人混合历史）。CM 未安装 / 拉取失败 → 降级为不带历史
        - prompt（本轮 user）= 各任务的提示文本拼接 + 迟到提示（如迟到）

        日历任务的内容**不**使用 task 入队时的快照——到点动态查 store。
        这样用户在 0 点后对日历事件 / 内置分类开关的任何改动都能在 fire 时反映。
        """
        # 若有日历任务：先确保内置事件新鲜（应对 0 点后用户改分类开关的情况）
        if any(t.kind == "calendar" for t in tasks):
            self._check_builtin_fresh()

        # ---- 1. 构造 system_prompt ----
        system_parts = []
        persona_prompt = ""

        # 人设 prompt（参考 emotion_favour 的简化方案）
        try:
            persona = await self.context.persona_manager.get_default_persona_v3(session)
            persona_prompt = (persona or {}).get("prompt") or ""
            if persona_prompt:
                system_parts.append(persona_prompt)
        except Exception as e:
            logger.warning(f"{tag()} ⚠️ 获取人设失败 session={session}: {e}")

        # 时间引导 prompt（time_awareness 自己的；含 {daily_schedule_state} 占位符）
        guidance = self._resolve_placeholders(self._effective_time_guidance_prompt())
        if guidance:
            system_parts.append(guidance)

        system_prompt = "\n\n".join(system_parts)
        logger.debug(
            f"{tag()} 🔧 主动消息 system_prompt: session={session} "
            f"persona={len(persona_prompt)}字 guidance={len(guidance)}字 "
            f"→ 合并 {len(system_prompt)} 字"
        )

        # ---- 2. 拉对话历史（从 chat_memory 插件）----
        contexts = await self._fetch_history_contexts(session)
        logger.debug(
            f"{tag()} 🔧 主动消息 contexts: session={session} 拉取 {len(contexts)} 条历史"
        )

        # ---- 3. 构造本轮 prompt ----
        # 日历任务到点动态查询当日事件（不依赖 task 入队时的快照，
        # 这样用户在 0 点后对日历的任何改动都能在 fire 时反映）
        parts = []
        calendar_added = False
        for t in tasks:
            if t.kind == "calendar":
                if calendar_added:
                    continue  # 同 group 多条 calendar task 只查一次
                calendar_added = True
                now_date = get_now(self.config, self._astrbot_config()).date()
                today_events = calendar_store.events_for_date(
                    now_date.year, now_date.month, now_date.day
                )
                texts = [str(e.get("text", "")).strip() for e in today_events]
                texts = [t for t in texts if t]
                logger.info(
                    f"{tag()} 📅 calendar 触发: session={session} "
                    f"当日事件={len(texts)} 条"
                    + (f" → {('、'.join(texts))[:80]}" if texts else "")
                )
                if texts:
                    parts.append(f"今天是 {'、'.join(texts)}，可自然提一下")
            elif t.kind == "followup":
                hint_preview = (t.hint or "(无承诺内容)")[:80]
                logger.info(
                    f"{tag()} 📝 followup 触发: id={t.id[:8]} session={session} "
                    f"target={t.target_user_id or '-'} hint={hint_preview}"
                )
                if t.hint:
                    parts.append(f"你之前承诺过：{t.hint}。现在到点了，请继续")
                else:
                    parts.append("你之前承诺过现在到点了，请按当时承诺继续")
            elif t.kind == "user":
                hint_preview = (t.hint or "(无内容)")[:80]
                logger.info(
                    f"{tag()} 🧑 user 触发: id={t.id[:8]} session={session} "
                    f"target={t.target_user_id or '-'} hint={hint_preview}"
                )
                if t.hint:
                    parts.append(f"到点了，请主动提起：{t.hint}")
                else:
                    parts.append("到点了，请主动发一条问候/消息")

        prompt = "\n".join(parts)
        if not prompt.strip():
            # 当日事项为空且无 followup/user 提示 → 无内容可发，跳过
            logger.info(f"{tag()} ℹ️ 跳过空提醒 session={session} tasks={len(tasks)}")
            return

        if is_late:
            late_text = self._late_prompt().replace(
                "{delay_minutes}", str(late_minutes)
            )
            prompt = f"{prompt}\n\n{late_text}"

        # 多段输出格式提示：让 LLM 用空行分段，发送时按 \n 切成独立消息
        prompt = f"{prompt}\n\n（如需分多段表达，用空行分隔，每段将作为独立消息发出）"

        # debug：打印最终 prompt（完整内容）
        logger.debug(
            f"{tag()} 🔧 主动消息 prompt 最终({len(prompt)} 字):\n{prompt}"
        )

        # ---- 4. 调 LLM ----
        try:
            llm_kwargs = {
                "prompt": prompt,
                "system_prompt": system_prompt,
            }
            if contexts:
                llm_kwargs["contexts"] = contexts
            # AstrBot 4.25+ 把 chat_provider_id 改成必填：配置了就用配置，否则回会话默认 provider
            provider_id = (self.config.get("reminder", {}).get("reminder_provider_id") or "").strip()
            if not provider_id:
                try:
                    provider_id = (await self.context.get_current_chat_provider_id(umo=session) or "").strip()
                except Exception as e:
                    logger.warning(f"{tag()} ⚠️ 获取默认 provider 失败 session={session}: {e}")
            if not provider_id:
                logger.error(f"{tag()} ❌ 无法确定 provider，跳过 session={session}")
                return
            llm_kwargs["chat_provider_id"] = provider_id
            logger.debug(
                f"{tag()} 🔧 主动消息 调 LLM: session={session} provider={provider_id} "
                f"system={len(system_prompt)}字 prompt={len(prompt)}字 "
                f"contexts={'是' if contexts else '否'}({len(contexts)}条)"
            )
            llm_response = await self.context.llm_generate(**llm_kwargs)
            if not llm_response or llm_response.role != "assistant":
                logger.error(
                    f"{tag()} ❌ LLM 响应异常 session={session}: {llm_response}"
                )
                return
            reply = (llm_response.completion_text or "").strip()
            if not reply:
                logger.warning(f"{tag()} ⚠️ LLM 返回空回复 session={session}")
                return
            # 群聊 + followup/user + 有 target 时前置 At；calendar 群发不 at，私聊一对一无需 at
            is_group = "GroupMessage" in session
            at_uid = ""
            if is_group:
                for t in tasks:
                    if t.kind in ("followup", "user") and t.target_user_id:
                        at_uid = t.target_user_id
                        break

            # 按空行切段发送（首段带 At），跳过空段；段间随机延迟 0.5-2s 模拟打字
            segments = [s.strip() for s in reply.split("\n") if s.strip()]
            for i, seg in enumerate(segments):
                await self.context.send_message(
                    session, _build_chain(seg, at_uid if i == 0 else "")
                )
                if i < len(segments) - 1:
                    await asyncio.sleep(random.uniform(0.5, 2.0))
            logger.info(
                f"{tag()} ✅ LLM 触发已发: session={session} tasks={len(tasks)} "
                f"late={is_late} history={len(contexts)} at={at_uid or '-'} "
                f"segments={len(segments)} provider={provider_id or 'main'}"
            )
        except Exception as e:
            logger.error(f"{tag()} ❌ LLM 触发失败 session={session}: {e}")

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
                f"{tag()} ⚠️ 获取 conversation_id 失败 session={session}: {e}"
            )
            return []
        if not cid:
            logger.debug(f"{tag()} 无 conversation_id，跳过历史拉取 session={session}")
            return []

        cm = self._resolve_chat_memory()
        if cm is None:
            logger.debug(f"{tag()} chat_memory 未安装，跳过历史拉取")
            return []

        try:
            records = await cm.query_history(session, cid, None, limit)
        except Exception as e:
            logger.warning(
                f"{tag()} ⚠️ 从 chat_memory 拉取历史失败 session={session}: {e}"
            )
            return []

        contexts = []
        for r in records:
            role = r.get("role")
            content = r.get("content") or ""
            if not role or not content:
                continue
            # 截取前 16 字符 "YYYY-MM-DD HH:MM"（CM 存的可能是完整 datetime 字符串）
            # 用 <time> XML 标签包裹而非方括号前缀，避免 LLM 把时间戳当成正文格式模仿
            created = str(r.get("created_at", ""))[:16].strip()
            if created:
                contexts.append({"role": role, "content": f"<time>{created}</time> {content}"})
            else:
                contexts.append({"role": role, "content": content})
        return contexts

    def _resolve_chat_memory(self):
        """定位 chat_memory 插件实例。AstrBot 注册表 → sys.modules fallback。

        成功后缓存到 ``self._chat_memory``；失败不缓存以便下次重试。
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
        if be.get("almanac", False):
            result.append(CATEGORY_ALMANAC)
        return result

    def _resolve_schedule_state(self) -> str:
        """{daily_schedule_state} 的取值：命中时段的 state_prompt；未命中返回占位文本。

        - 未启用日程表感知 → 返回「未指定（按人设自然推断）」
        - 启用但未命中任何时段（gap）→ 同上
        - 命中时段但 state_prompt 为空 → 同上
        """
        ds = self.config.get("daily_schedule", {})
        if not ds.get("enable_schedule", False):
            return "未指定（按人设自然推断）"
        now = get_now(self.config, self._astrbot_config())
        slot = find_active_schedule_slot(ds, now)
        if slot is None:
            return "未指定（按人设自然推断）"
        state = (slot.get("state_prompt") or "").strip()
        return state or "未指定（按人设自然推断）"

    def _resolve_placeholders(self, text: str) -> str:
        if not text:
            return ""
        text = text.replace("{calendar_today}", self._resolve_calendar_today())
        text = text.replace("{daily_schedule_state}", self._resolve_schedule_state())
        return text

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

    @filter.on_llm_request()
    async def inject_time_context(self, event: AstrMessageEvent, req: ProviderRequest):
        """时间引导 prompt（含 {daily_schedule_state} 占位符）→ system_prompt 末尾。

        sleep_prompt 已废弃：睡眠时段的 state_prompt 现在通过 {daily_schedule_state}
        占位符统一走 system_prompt（与日程表机制一致）。
        """
        try:
            guidance = self._resolve_placeholders(self._effective_time_guidance_prompt())
            self._append_system_prompt(req, guidance)
        except Exception as e:
            logger.error(f"{tag(event)} ❌ on_llm_request 注入失败: {e}")

    @filter.on_llm_response()
    async def strip_time_tags_from_response(
        self,
        event: AstrMessageEvent,
        response: LLMResponse,
    ) -> None:
        """剥掉 LLM 回复中误模仿的 <time>...</time> 标签。

        历史对话中我们用 <time> 包裹时间戳注入，部分 LLM 会模仿该格式输出；
        此钩子在回复落盘前清洗，确保用户看到的是干净正文。
        """
        text = response.completion_text or ""
        if not text or "<time" not in text.lower():
            return
        cleaned = _TIME_TAG_PATTERN.sub("", text)
        if cleaned != text:
            response.completion_text = cleaned
            logger.debug(f"{tag(event)} 🧹 已剥离 <time> 标签")

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
            "/calendar builtin_list [分类]  — 列出当年所有内置事件（可按「法定/传统/节气/政治/国际/黄历」过滤）\n"
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
            "内置现实日历事件（默认开法定/传统/节气，关政治/国际/黄历）：\n"
            "- 法定节假日（含调休）、传统农历节日、二十四节气\n"
            "- 政治纪念日、国际/西方节日、黄历（每日干支/宜忌）\n"
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
        /calendar builtin_list 黄历      — 仅列出每日黄历
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
            "黄历": CATEGORY_ALMANAC, "老黄历": CATEGORY_ALMANAC, "almanac": CATEGORY_ALMANAC,
        }
        return mapping.get(text.lower(), "")

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
            "- 时段状态通过 {daily_schedule_state} 占位符注入到时间感知 prompt\n"
            "- 未启用日程表感知时占位符返回「未指定（按人设自然推断）」\n"
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
            start = str(s.get("start_time", "")).strip()
            end = str(s.get("end_time", "")).strip()
            state = (s.get("state_prompt") or "").strip() or "(无状态描述)"
            # state 截断到 40 字，过长用省略号
            state_preview = state if len(state) <= 40 else state[:37] + "..."
            marker = " ← 当前" if start == active_start else ""
            lines.append(f"  {start}-{end}{marker}  {state_preview}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @schedule_group.command("create")
    async def schedule_create(self, event: AstrMessageEvent, confirm: str = ""):
        """按配置里的世界观或当前人设生成日程表（覆盖现有 schedule_templates）。

        优先级：
        1. 配置 daily_schedule.ai_generate_worldview 非空 → 用之
        2. 当前会话的人设 prompt 非空 → 用之作为世界观输入
        3. 都为空 → 拒绝执行

        已有现有时段时需显式追加 ``confirm`` 参数确认覆盖。
        """
        ds = self.config.get("daily_schedule", {})
        theme = (ds.get("ai_generate_worldview", "") or "").strip()

        # 人设兜底
        persona_used = False
        if not theme:
            session = event.unified_msg_origin or ""
            try:
                persona = await self.context.persona_manager.get_default_persona_v3(session)
                persona_prompt = (persona or {}).get("prompt") or ""
            except Exception as e:
                logger.warning(f"{tag()} ⚠️ 获取人设失败 session={session}: {e}")
                persona_prompt = ""
            if persona_prompt.strip():
                theme = persona_prompt.strip()
                persona_used = True

        if not theme:
            yield event.plain_result(
                "未配置世界观，且当前会话没有人设 prompt。\n"
                "请先在配置 daily_schedule.ai_generate_worldview 填写主题，"
                "或为当前会话配置人设后重试。"
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

        async for msg in self._do_schedule_generate(event, theme, persona_used):
            yield msg

    async def _do_schedule_generate(
        self, event: AstrMessageEvent, theme: str, persona_used: bool
    ):
        """实际执行 LLM 调用 + 解析 + 落盘。"""
        ds = self.config.get("daily_schedule", {})
        provider_id = (ds.get("ai_generate_provider_id", "") or "").strip()

        source_tag = "当前人设" if persona_used else "配置世界观"
        theme_preview = theme if len(theme) <= 60 else theme[:57] + "..."
        yield event.plain_result(
            f"正在调用 AI 生成日程表（来源：{source_tag}「{theme_preview}」），请稍候……"
        )

        # 调 LLM
        try:
            llm_kwargs = {
                "prompt": theme,
                "system_prompt": DEFAULT_SCHEDULE_GENERATE_SYSTEM_PROMPT,
            }
            if provider_id:
                llm_kwargs["chat_provider_id"] = provider_id
            llm_response = await self.context.llm_generate(**llm_kwargs)
        except Exception as e:
            logger.error(f"{tag()} ❌ AI 生成日程表 LLM 调用失败: {e}")
            yield event.plain_result(f"AI 生成失败：{e}")
            return

        if not llm_response or llm_response.role != "assistant":
            yield event.plain_result("AI 响应异常，请检查 provider 配置。")
            return

        raw_text = (llm_response.completion_text or "").strip()
        slots_data = self._parse_schedule_json(raw_text)
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
            active_state = (active.get("state_prompt") or "").strip()
            active_info = (
                f"\n当前命中时段：{active.get('start_time')}-{active.get('end_time')}"
                + (f"（{active_state[:40]}）" if active_state else "")
            )

        yield event.plain_result(
            f"✅ 已生成 {len(new_slots)} 条时段并写入配置。\n"
            f"来源：{source_tag}\n"
            f"使用 /schedule show 查看完整列表。{active_info}"
        )

    @staticmethod
    def _parse_schedule_json(text: str) -> list[dict] | None:
        """解析 LLM 输出的日程表 JSON。

        返回 [{"start": "HH:MM", "end": "HH:MM", "state": "..."}, ...]；
        解析失败或字段不全返回 None。
        """
        import json

        # 去掉可能的 Markdown 代码块包裹
        cleaned = text.strip()
        if cleaned.startswith("```"):
            # 去掉首尾 ``` 行
            lines = cleaned.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines)

        try:
            data = json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            # 尝试提取首个 [ 到 ] 之间的内容
            match = re.search(r"\[.*\]", cleaned, re.DOTALL)
            if not match:
                return None
            try:
                data = json.loads(match.group(0))
            except (json.JSONDecodeError, ValueError):
                return None

        if not isinstance(data, list) or not data:
            return None

        result = []
        for item in data:
            if not isinstance(item, dict):
                continue
            start = str(item.get("start", "")).strip()
            end = str(item.get("end", "")).strip()
            state = str(item.get("state", "")).strip()
            if not start or not end or not state:
                continue
            # 校验 HH:MM 格式
            valid = True
            for t in (start, end):
                try:
                    h, m = t.split(":")
                    if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
                        valid = False
                        break
                except (ValueError, AttributeError):
                    valid = False
                    break
            if not valid:
                continue
            result.append({"start": start, "end": end, "state": state})

        return result if result else None

    # ==================== LLM 工具：schedule_followup ====================

    @filter.llm_tool(name="schedule_followup")
    async def schedule_followup_tool(
        self,
        event: AstrMessageEvent,
        delay_minutes: int,
        hint: str = "",
    ) -> str:
        """
        当以下任一情况发生时调用此工具：

        1. 你对当前对话承诺「X 分钟后再继续/再说某事」（用户明确要求提醒、晚点再聊）。
        2. 用户请你执行耗时的现实任务（如买水、跑腿、查资料、做饭），但你作为虚拟人格
           无法实际执行——可模拟「已去执行」的过程：估算合理耗时后调用此工具，
           到点时插件重新唤醒你，由你以「任务已完成」的口吻继续对话，让体验更拟人。

        约束：仅在用户**明确**请求执行现实任务、或**明确**要求稍后提醒时调用；
        闲聊、已完成的事、用户没请你做的事不要触发，避免滥用。

        到点时插件会带完整人设/历史/状态重新唤醒你，由你看到 hint 文本，生成符合人设的回复。

        Args:
            delay_minutes(int): 几分钟后触发（范围 1-1440，即最多 24 小时）。
                场景 1：用户指定的时间。
                场景 2：你估算的现实任务合理耗时（买水约 5 分钟、跑腿约 30 分钟、电影约 2 小时）。
            hint(string): 到点时你会看到这条文本作为备忘，要写得让「未来的你」能延续当时状态。
                场景 1 必填：承诺了什么、要继续什么。
                场景 2 必填：模拟执行了什么任务、到点该以什么口吻汇报
                （如「假装去买了水，告诉用户水买回来了，可附上虚构的细节」）。
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
        # at 目标固定为当前对话的发起人（群聊场景前置 At 组件）
        target_uid = event.get_sender_id() or ""
        self.scheduler.add_followup_task(
            session=session,
            fire_at=fire_at,
            hint=(hint or "").strip(),
            target_user_id=target_uid,
        )
        hint_preview = f"（备忘：{(hint or '').strip()[:30]}）" if (hint or "").strip() else ""
        time_str = fire_at.strftime('%H:%M')
        return (
            f"已安排 {delay} 分钟后（{time_str}）触发主动消息{hint_preview}。\n"
            f"\n"
            f"<REPLY_GUIDE>\n"
            f"是否回复用户由你判断：\n"
            f"- 若你刚才已向用户说明过此事（承诺过/确认过），本轮可直接返回空回复，不要重复；\n"
            f"- 若你刚才未提及此事（如直接调用了工具），请用一两句话简短告知用户已安排。\n"
            f"仅在确有新信息需要补充时才说话。返回空回复是允许的。\n"
            f"</REPLY_GUIDE>"
        )

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
            CATEGORY_ALMANAC: "黄历",
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


def _build_chain(text: str, at_uid: str = ""):
    """构造 MessageChain，用于 context.send_message。

    ``at_uid`` 非空时前置 At 组件（仅群聊 followup 场景）。
    渲染规则参照 attool：At 后跟零宽空格 + 空格，避免与后续文本粘连；
    不在系统里调 attool 插件——主动消息走 ``context.send_message`` 不经 hook 链。
    """
    from astrbot.api.event import MessageChain
    from astrbot.core.message.components import At, Plain

    chain = MessageChain()
    if at_uid:
        chain.chain = [At(qq=at_uid), Plain("​ ​" + text)]
    else:
        chain.chain = [Plain(text)]
    return chain
