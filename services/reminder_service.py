"""主动提醒应用服务。"""

import datetime
from typing import Any, Callable

from astrbot.api.message_components import At, Plain

from ..constants import DEFAULT_LATE_PROMPT, DEFAULT_TIME_GUIDANCE_PROMPT
from ..core.scheduler import TriggerResult
from ..integrations.chat_memory_history import (
    ChatMemoryContextState,
    load_chat_memory_context_state,
)
from ..integrations.persona_resolver import (
    extract_persona_prompt,
    resolve_effective_persona,
)
from ..log import logger, tag
from .adaptive_policy import AdaptiveConcurrencyGate
from ..utils.time_utils import coerce_to_aware, get_now


class ReminderService:
    def __init__(
        self,
        *,
        context: Any,
        config: dict,
        scheduler,
        time_context,
        message_sender,
        last_chat_tracker,
        astrbot_config_provider: Callable[[], Any],
        ensure_builtin_fresh: Callable[[], None],
        concurrency_gate: AdaptiveConcurrencyGate | None = None,
    ):
        self.context = context
        self.config = config
        self.scheduler = scheduler
        self.time_context = time_context
        self.message_sender = message_sender
        self.last_chat_tracker = last_chat_tracker
        self._astrbot_config_provider = astrbot_config_provider
        self._ensure_builtin_fresh = ensure_builtin_fresh
        self._llm_gate = concurrency_gate

    def _reminder_config(self) -> dict:
        value = self.config.get("reminder", {})
        return value if isinstance(value, dict) else {}

    def targets(self) -> list[str]:
        targets = self._reminder_config().get("reminder_targets", []) or []
        if isinstance(targets, str):
            targets = [item.strip() for item in targets.split("\n") if item.strip()]
        return [str(item).strip() for item in targets if str(item).strip()]

    def late_prompt(self) -> str:
        text = (
            self._reminder_config().get("reminder_late_prompt") or ""
        ).strip()
        return text or DEFAULT_LATE_PROMPT

    def daily_greeting_without_events(self) -> bool:
        return bool(
            self._reminder_config().get(
                "daily_greeting_without_events",
                True,  # 默认开启：提醒开启而日历未配置时不会静默无提醒
            )
        )

    def create_external_task(
        self,
        *,
        session: str,
        fire_at: datetime.datetime,
        hint: str,
        target_user_id: str = "",
        source: str = "",
    ) -> str:
        session = (session or "").strip()
        hint = (hint or "").strip()
        target_user_id = (target_user_id or "").strip()
        source = (source or "").strip()

        if not session:
            raise ValueError("session 不能为空")
        allowed = self.targets()
        if session not in allowed:
            raise ValueError(
                f"session 不在 reminder_targets 白名单内: {session} "
                f"(白名单共 {len(allowed)} 个)"
            )
        if not hint:
            raise ValueError("hint 不能为空")
        if len(hint) > 500:
            raise ValueError(f"hint 长度 {len(hint)} 超 500 字上限")
        if len(target_user_id) > 64:
            raise ValueError("target_user_id 长度不能超过 64 字符")

        now = get_now(self.config, self._astrbot_config_provider())
        fire_at = coerce_to_aware(fire_at, now)
        min_fire = now + datetime.timedelta(seconds=60)
        max_fire = now + datetime.timedelta(days=365)
        if fire_at <= min_fire:
            raise ValueError(
                f"fire_at 距当前过近（需 > {min_fire.isoformat()}，"
                f"实际 {fire_at.isoformat()}）"
            )
        if fire_at > max_fire:
            raise ValueError(f"fire_at 超 365 天上限（{fire_at.isoformat()}）")

        task = self.scheduler.add_user_task(
            session=session,
            fire_at=fire_at,
            hint=hint,
            target_user_id=target_user_id,
        )
        logger.info(
            f"{tag()} 🔌 外部任务创建: source={source or '-'} id={task.id} "
            f"session={session} fire_at={fire_at.isoformat()}"
        )
        return task.id

    async def deliver(
        self,
        *,
        session: str,
        tasks: list,
        is_late: bool,
        late_minutes: int,
    ) -> TriggerResult:
        # P0-2 隐私/身份防线：混合 @ 目标的 DueGroup 不得合并进同一 LLM prompt、
        # ChatMemory 查询范围或 At。在产生任何 LLM/发送副作用前 fail-closed。
        at_user_id = self._resolve_target_scope(session, tasks)
        if at_user_id is None:
            logger.warning(
                f"{tag()} 🚫 到期组含多个不同 @ 目标的个人提醒，拒绝合并发送: "
                f"session={session} tasks={len(tasks)}"
            )
            return TriggerResult.fail(
                "到期组包含多个不同 @ 目标的个人提醒，已按 fail-closed 拒绝合并发送",
                retryable=True,
            )
        if any(task.kind == "calendar" for task in tasks):
            self._ensure_builtin_fresh()

        system_parts = []
        persona_id, persona = await resolve_effective_persona(self.context, session)
        persona_prompt = extract_persona_prompt(persona)
        if persona_prompt:
            system_parts.append(persona_prompt)
        time_guidance_enabled = self.time_context.time_guidance_enabled()
        if time_guidance_enabled:
            system_parts.append(DEFAULT_TIME_GUIDANCE_PROMPT)
        system_prompt = "\n\n".join(system_parts)

        prompt_parts = self._build_task_prompt_parts(session, tasks)
        prompt = "\n".join(prompt_parts)
        if not prompt.strip():
            logger.debug(f"{tag()} ℹ️ 跳过空提醒 session={session} tasks={len(tasks)}")
            return TriggerResult.ok()
        if is_late:
            late_text = self.late_prompt().replace(
                "{delay_minutes}",
                str(late_minutes),
            )
            prompt = f"{prompt}\n\n{late_text}"
        provider_id = (
            self._reminder_config().get("reminder_provider_id") or ""
        ).strip()
        if not provider_id:
            try:
                provider_id = (
                    await self.context.get_current_chat_provider_id(umo=session) or ""
                ).strip()
            except Exception as e:
                return TriggerResult.fail(
                    f"获取会话 Provider 失败: {e}",
                    retryable=True,
                )
        if not provider_id:
            return TriggerResult.fail("无法确定会话 Provider", retryable=True)

        memory_state = ChatMemoryContextState([])
        if bool(
            self._reminder_config().get(
                "use_chat_memory_history",
                False,
            )
        ):
            memory_state = await load_chat_memory_context_state(
                self.context,
                session,
                persona_id=persona_id,
                user_id=at_user_id,
            )

        # 直接调用 Context.llm_generate 不会进入 AstrBot 的 on_llm_request；
        # 动态数据因此必须由主动路径自己追加到本轮 prompt 末尾。ChatMemory takeover
        # 已提供逐条 cm_time 时省略重复的 LAST_CHAT，但继续维护本地记录以便降级恢复。
        if time_guidance_enabled:
            now = self.time_context.now()
            dynamic = self.time_context.build_dynamic(
                session,
                now=now,
                include_last_chat=not memory_state.takeover_enabled,
            )
            prompt = f"{prompt}\n\n{dynamic}"

        held = False
        try:
            if self._llm_gate is not None:
                held = await self._llm_gate.acquire_configured(self.config)
                if not held:
                    return TriggerResult.fail(
                        "插件全局 LLM 并发等待超时",
                        retryable=True,
                    )
            try:
                response = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    contexts=memory_state.contexts or None,
                )
            except Exception as e:
                return TriggerResult.fail(f"LLM 调用失败: {e}", retryable=True)
        finally:
            if held:
                await self._llm_gate.release()
        if not response or response.role != "assistant":
            return TriggerResult.fail("LLM 响应异常", retryable=True)
        reply = (response.completion_text or "").strip()
        if not reply:
            return TriggerResult.fail("LLM 返回空回复", retryable=True)

        components = []
        if at_user_id:
            components.append(At(qq=at_user_id))
        components.append(Plain(reply))

        # 从这里开始 Hook 可能产生外部发送副作用，失败不再自动重试。
        if not self.scheduler.mark_delivery_started(tasks):
            return TriggerResult.fail(
                "无法持久化 delivery_started 状态，已中止发送",
                retryable=True,
            )
        outcome = await self.message_sender.send_decorated(
            session,
            components,
            sender_id=at_user_id,
        )
        if not outcome.success:
            return TriggerResult.fail(
                f"发送阶段失败: {outcome.error}",
                retryable=False,
            )

        if self.time_context.last_chat_enabled():
            try:
                self.last_chat_tracker.record_ai(
                    session,
                    get_now(self.config, self._astrbot_config_provider()),
                )
            except Exception as e:
                logger.warning(f"{tag()} ⚠️ 主动消息 record_ai 失败 session={session}: {e}")
        logger.info(
            f"{tag()} ✅ LLM 触发已发: session={session} tasks={len(tasks)} "
            f"late={is_late} at={at_user_id or '-'} provider={provider_id}"
        )
        return TriggerResult.ok()

    def _build_task_prompt_parts(self, session: str, tasks: list) -> list[str]:
        parts: list[str] = []
        calendar_added = False
        for task in tasks:
            if task.kind == "calendar":
                if calendar_added:
                    continue
                calendar_added = True
                now = get_now(self.config, self._astrbot_config_provider())
                calendar_config = self.config.get("calendar", {})
                if not isinstance(calendar_config, dict):
                    calendar_config = {}
                if calendar_config.get("enable_calendar", False):
                    include_builtin = bool(self.time_context.enabled_builtin_categories())
                    events = self.time_context.calendar_store.events_for_date(
                        now.year,
                        now.month,
                        now.day,
                        include_builtin=include_builtin,
                    )
                    texts = [str(event.get("text", "")).strip() for event in events]
                    texts = [text for text in texts if text]
                else:
                    texts = []
                if texts:
                    parts.append(f"今天是 {'、'.join(texts)}，可自然提一下")
                elif self.daily_greeting_without_events():
                    parts.append("到点了，主动发一条贴合此时段的日常问候，自然开启对话")
            elif task.kind == "followup":
                if task.hint:
                    parts.append(f"你之前承诺过：{task.hint}。现在到点了，请继续")
                else:
                    parts.append("你之前承诺过现在到点了，请按当时承诺继续")
            elif task.kind == "user":
                if task.hint:
                    parts.append(f"到点了，请主动提起：{task.hint}")
                else:
                    parts.append("到点了，请主动发一条问候/消息")
        return parts

    @staticmethod
    def _resolve_target_scope(session: str, tasks: list) -> str | None:
        """返回到期组统一的 @ 目标身份 scope。

        返回:
            str: homogeneous scope —— 非群会话恒为 ``""``（会话本身即身份，不 At）；
                群会话为所有个人任务（followup/user）唯一的 target_user_id
                （全空即 ``""``，表示群公共消息不 At）。
            None: 群会话下个人任务存在多个不同 target_user_id（含空串与具体
                用户混合）。调用方必须 fail-closed，禁止把不同用户的个人提醒
                合并进同一 LLM prompt / ChatMemory 范围 / At。

        calendar 任务不携带个人身份，不参与 scope 判定，可与任意个人任务共存。
        """
        if ":GroupMessage:" not in session:
            return ""
        scopes = {
            str(task.target_user_id or "").strip()
            for task in tasks
            if task.kind in {"followup", "user"}
        }
        if len(scopes) > 1:
            return None
        return scopes.pop() if scopes else ""
