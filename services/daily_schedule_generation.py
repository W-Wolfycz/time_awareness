"""AI 每日日程的一次生成用例（Provider/Persona/事实/增强上下文/Prompt/输出协议）。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..domain.schedule import _CONTROL_CHARS, ai_daily_config, schedule_similarity
from ..integrations.persona_resolver import extract_persona_prompt, resolve_effective_persona
from ..constants import DEFAULT_DAILY_SCHEDULE_SYSTEM_PROMPT
from ..log import logger, tag
from ..llm.daily_schedule_generator import (
    json_block,
    PlanningResult,
    ScheduleValidationError,
    deterministic_score,
    empty_boundary,
    evaluate_boundary,
    generate_daily_schedule,
    select_candidate_with_llm,
)
from dataclasses import replace as dc_replace
from .adaptive_policy import (
    DEFAULT_ENHANCED_CONTEXT_TOKENS,
    SIMILARITY_REPEAT_THRESHOLD,
    AdaptiveConcurrencyGate,
    AdaptiveGenerationPolicy,
    EnhancedContext,
    estimate_text_tokens,
    resolve_ai_generation_config,
)


PROMPT_REVISION = "daily_schedule_v11"

# 所有“重试语义”LLM 循环的统一退避序列（秒）：0/5/30s，超出封顶 30s；通过可注入 sleep 实现。
_RETRY_BACKOFF_SECONDS = (0.0, 5.0, 30.0)


def _retry_delay_seconds(attempt: int) -> float:
    """返回第 ``attempt`` 次尝试（0-based）前的等待秒数；第 0 次不等待。"""
    if attempt <= 0:
        return 0.0
    return _RETRY_BACKOFF_SECONDS[min(attempt, len(_RETRY_BACKOFF_SECONDS) - 1)]


@dataclass(frozen=True)
class GeneratedDailySchedule:
    slots: list[dict]
    source: str
    provider_id: str
    boundary_state: dict = field(default_factory=dict)
    calls_executed: int = 1
    skipped: tuple[str, ...] = ()
    context_tokens: int = 0
    regenerated_for_similarity: bool = False


class DailyScheduleGenerationError(RuntimeError):
    def __init__(
        self,
        error_type: str,
        message: str,
        *,
        retryable: bool,
        calls_executed: int = 0,
        detail: str = "",
    ):
        super().__init__(message)
        self.error_type = error_type
        self.user_message = message
        self.retryable = retryable
        self.calls_executed = int(calls_executed or 0)
        # 底层异常全文（已单行化），用于失败日志/记录。
        self.detail = str(detail or "").strip()


def exception_detail(exc: BaseException) -> str:
    """把异常压成单行可诊断文本：保留全文，仅折叠换行/连续空白。"""
    text = str(exc).strip()
    if not text:
        text = type(exc).__name__
    return " ".join(text.split())


class DailyScheduleGeneration:
    def __init__(
        self,
        *,
        context: Any,
        config: dict,
        time_context,
        persona_resolver: Callable[..., Awaitable[tuple[str, Any | None]]] = resolve_effective_persona,
        concurrency_gate: AdaptiveConcurrencyGate | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ):
        self.context = context
        self.config = config
        self.time_context = time_context
        self._persona_resolver = persona_resolver
        self._sleep = sleep or asyncio.sleep
        self._gate = concurrency_gate or AdaptiveConcurrencyGate(
            AdaptiveGenerationPolicy.from_config(config).max_concurrent_llm
        )

    async def policy(self) -> AdaptiveGenerationPolicy:
        """返回实际生效策略（总开关关闭时子配置残留一律不生效）。"""
        policy = AdaptiveGenerationPolicy.from_config(self.config).effective()
        await self._gate.reconfigure(policy.max_concurrent_llm)
        return policy

    def max_slots(self) -> int:
        daily_schedule = self.config.get("daily_schedule", {})
        ai_daily = daily_schedule.get("ai_daily", {}) if isinstance(daily_schedule, dict) else {}
        try:
            value = int(ai_daily.get("max_slots", 24) or 24)
        except (TypeError, ValueError):
            value = 24
        return max(1, min(48, value))

    def _generation_config(self) -> dict:
        """返回 AI 日程生成输入配置（扁平字段优先、旧分组回退）。"""
        return resolve_ai_generation_config(self.config)

    def _ai_daily_config(self) -> dict:
        """返回 ``daily_schedule.ai_daily`` 原始配置（无旧分组语义的新键）。"""
        return ai_daily_config(self.config)

    def _adaptive_config(self) -> dict:
        """返回 ``daily_schedule.ai_daily.adaptive`` 原始配置（池归属自适应分组）。"""
        adaptive = self._ai_daily_config().get("adaptive", {})
        return adaptive if isinstance(adaptive, dict) else {}

    def _pool_items(self, key: str) -> list[str]:
        """读取创意池 list 配置：逐条 trim、去重、上限 50。"""
        value = self._adaptive_config().get(key)
        if not isinstance(value, list):
            return []
        items: list[str] = []
        for entry in value:
            item = str(entry or "").strip()
            if item and item not in items:
                items.append(item)
        return items[:50]

    def _theme_pool(self) -> list[str]:
        return self._pool_items("theme_pool")

    def _style_pool(self) -> list[str]:
        return self._pool_items("style_pool")

    def _allow_custom_theme(self) -> bool:
        value = self._adaptive_config().get("allow_custom_theme", True)
        return value if isinstance(value, bool) else True

    @staticmethod
    def _recent_themes(enhanced: EnhancedContext) -> list[dict]:
        """从反重复已抓取的最近快照中提取各天 theme/style（窗口随 recent_days）。"""
        themes: list[dict] = []
        for snapshot in enhanced.recent_snapshots or ():
            if not isinstance(snapshot, dict):
                continue
            boundary = snapshot.get("boundary_state")
            if not isinstance(boundary, dict):
                continue
            theme = str(boundary.get("daily_theme", "") or "").strip()
            style = str(boundary.get("daily_style", "") or "").strip()
            if theme or style:
                themes.append(
                    {
                        "date": str(snapshot.get("local_date", "") or ""),
                        "daily_theme": theme,
                        "daily_style": style,
                    }
                )
        return themes

    async def generate(
        self,
        *,
        session: str,
        now,
        enhanced: EnhancedContext | None = None,
        persona: Any | None = None,
        forecast: dict | None = None,
    ) -> GeneratedDailySchedule:
        """生成当天日程；persona 由 service 层同源传入，forecast 为目标日天气基调。"""
        policy = await self.policy()
        enhanced = enhanced or EnhancedContext()
        provider_id = await self._resolve_provider_id()
        persona_prompt = await self._resolve_persona_prompt(session, persona)
        sensors = self.time_context.build_generation_sensor_snapshot(now=now)
        logger.debug(
            f"{tag()} AI 日程生成开始: provider={provider_id} "
            f"adaptive={policy.enabled} continuity={policy.state_continuity_enabled} "
            f"candidates={policy.effective_candidate_count}"
        )
        # Step1：角色状态评估（当日初始 boundary），失败降级不阻塞排日程
        calls_executed = 0
        skipped: list[str] = []
        # Step1 边界评估仅在「状态连续性」开启时执行。
        if policy.enabled and policy.state_continuity_enabled:
            today_boundary, boundary_calls, boundary_skipped = (
                await self._evaluate_today_boundary(
                    provider_id=provider_id,
                    persona_prompt=persona_prompt,
                    sensors=sensors,
                    now=now,
                    enhanced=enhanced,
                )
            )
            calls_executed += boundary_calls
            skipped.extend(boundary_skipped)
            enhanced = dc_replace(enhanced, today_boundary=today_boundary)
            if today_boundary:
                logger.debug(
                    f"{tag()} Step1 角色状态评估完成: "
                    f"theme={today_boundary.get('daily_theme', '-') or '-'} "
                    f"skipped={','.join(boundary_skipped) or '-'}"
                )
            else:
                logger.debug(
                    f"{tag()} Step1 角色状态评估未产出（以空状态继续）: "
                    f"skipped={','.join(boundary_skipped) or '-'}"
                )
        elif not policy.state_continuity_enabled:
            # 连续性关闭：压制调用方/测试注入的边界数据。
            enhanced = dc_replace(
                enhanced,
                today_boundary=None,
                previous_day_state=None,
            )
        enhanced = self._fit_enhanced_context(
            enhanced=enhanced,
            provider_id=provider_id,
            now=now,
            persona_prompt=persona_prompt,
            sensors=sensors,
            policy=policy,
        )
        result, executed, extra_skipped, anti_repeat = await self._plan_schedule(
            policy=policy,
            provider_id=provider_id,
            persona_prompt=persona_prompt,
            sensors=sensors,
            now=now,
            enhanced=enhanced,
            forecast=forecast,
        )
        calls_executed += executed
        skipped.extend(extra_skipped)

        return GeneratedDailySchedule(
            slots=result.slots,
            source="ai",
            provider_id=provider_id,
            # 存储恒全维度：成功 = 全字段回填；失败 = 全 null 种子
            boundary_state=enhanced.today_boundary or empty_boundary(),
            calls_executed=calls_executed,
            skipped=tuple(skipped),
            context_tokens=enhanced.used_tokens,
            regenerated_for_similarity=anti_repeat is not None,
        )

    async def _plan_schedule(
        self,
        *,
        policy: AdaptiveGenerationPolicy,
        provider_id: str,
        persona_prompt: str,
        sensors: dict,
        now,
        enhanced: EnhancedContext,
        forecast: dict | None = None,
    ) -> tuple[PlanningResult, int, list[str], str | None]:
        """Step2：单候选或多候选规划（含反重复定向重生成），返回 (result, executed, skipped, anti_repeat)。"""
        if not policy.enabled or not policy.wants_candidates:
            result, executed, extra_skipped = await self._plan_once(
                provider_id=provider_id,
                persona_prompt=persona_prompt,
                sensors=sensors,
                now=now,
                policy=policy,
                enhanced=enhanced,
                anti_repeat=None,
                forecast=forecast,
            )
            repeat_message = "单候选与最近日程相似度过高，定向重生成"
        else:
            result, executed, extra_skipped = await self._plan_candidates(
                provider_id=provider_id,
                persona_prompt=persona_prompt,
                sensors=sensors,
                now=now,
                policy=policy,
                enhanced=enhanced,
                forecast=forecast,
            )
            repeat_message = "多候选结果与最近日程相似度过高，定向重生成"
        anti_repeat = None
        if (
            policy.use_recent_schedules
            and enhanced.recent_snapshots
            and self._is_repeat(result.slots, enhanced.recent_snapshots)
        ):
            logger.debug(f"{tag()} {repeat_message}")
            anti_repeat = self._anti_repeat_instruction(enhanced)
            try:
                result, executed2, extra2 = await self._plan_once(
                    provider_id=provider_id,
                    persona_prompt=persona_prompt,
                    sensors=sensors,
                    now=now,
                    policy=policy,
                    enhanced=enhanced,
                    anti_repeat=anti_repeat,
                    forecast=forecast,
                )
            except DailyScheduleGenerationError as exc:
                # 反重复重生成最终失败：保留原合法候选（计划 §4.3）
                extra_skipped = extra_skipped + ["antirepeat:failed"]
                anti_repeat = None
                logger.debug(
                    f"{tag()} 反重复重生成失败，保留原合法候选: {exception_detail(exc)}"
                )
            else:
                executed += executed2
                extra_skipped = extra_skipped + extra2
        return result, executed, extra_skipped, anti_repeat

    # ---------------- 单候选 ----------------

    async def _plan_once(
        self,
        *,
        provider_id: str,
        persona_prompt: str,
        sensors: dict,
        now,
        policy: AdaptiveGenerationPolicy,
        enhanced: EnhancedContext,
        anti_repeat: str | None,
        forecast: dict | None = None,
    ) -> tuple[PlanningResult, int, list[str]]:
        prompt = self._build_prompt(
            now=now,
            persona_prompt=persona_prompt,
            sensors=sensors,
            policy=policy,
            enhanced=enhanced,
            anti_repeat=anti_repeat,
            forecast=forecast,
        )
        calls = 0
        for attempt in range(policy.max_attempts):
            if attempt > 0:
                await self._sleep(_retry_delay_seconds(attempt))
            held = await self._gate.acquire(policy.acquire_timeout_seconds)
            if not held:
                if attempt + 1 < policy.max_attempts:
                    continue
                raise DailyScheduleGenerationError(
                    "concurrency_timeout", "并发额度耗尽，使用旧快照/静态日程",
                    retryable=True, calls_executed=calls)
            try:
                calls += 1
                result, _ = await generate_daily_schedule(
                    self.context, provider_id=provider_id, prompt=prompt,
                )
                return result, calls, (["planning:retried"] if attempt else [])
            except Exception as exc:
                if attempt + 1 >= policy.max_attempts:
                    kind = "invalid_output" if isinstance(exc, ScheduleValidationError) else "llm_error"
                    message = "模型输出未通过日程协议校验" if kind == "invalid_output" else "调用日程生成模型失败"
                    raise DailyScheduleGenerationError(kind, message, retryable=True,
                                                       calls_executed=calls,
                                                       detail=exception_detail(exc)) from exc
                logger.debug(
                    f"{tag()} AI 日程规划第 {attempt + 1}/{policy.max_attempts} 次尝试失败: "
                    f"{exception_detail(exc)}"
                )
            finally:
                await self._gate.release()
        raise AssertionError("unreachable")

    def _is_repeat(self, slots: list[dict], recent_snapshots: tuple[dict, ...]) -> bool:
        signature = self._structure_signature(slots)
        for snapshot in recent_snapshots:
            # 结构级反重复：段落的 (start,end) 序列与最近某天完全相同即判重复；空日程不参与。
            if signature and self._structure_signature(
                snapshot.get("ai_slots") or snapshot.get("slots") or []
            ) == signature:
                return True
            if schedule_similarity(slots, snapshot.get("slots") or []) >= SIMILARITY_REPEAT_THRESHOLD:
                return True
        return False

    @staticmethod
    def _structure_signature(slots: list[dict]) -> tuple[tuple[str, str], ...]:
        """段落的 (start, end) 序列签名，用于结构级反重复。"""
        return tuple(
            (str(slot.get("start", "")), str(slot.get("end", "")))
            for slot in slots
            if isinstance(slot, dict)
        )

    @staticmethod
    def _anti_repeat_instruction(enhanced: EnhancedContext) -> str:
        return (
            "最近日程（仅作反重复参考，不得照抄）：\n"
            f"{enhanced.recent_schedules_text}\n"
            "本次生成的时段边界、名称与状态必须与最近日程有明显差异，不要做同义词改写。"
        )

    # ---------------- 多候选 ----------------

    async def _plan_candidates(
        self,
        *,
        provider_id: str,
        persona_prompt: str,
        sensors: dict,
        now,
        policy: AdaptiveGenerationPolicy,
        enhanced: EnhancedContext,
        forecast: dict | None = None,
    ) -> tuple[PlanningResult, int, list[str]]:
        count = policy.effective_candidate_count
        judge = policy.effective_judge
        skipped: list[str] = []

        if count <= 1:
            result, executed, _ = await self._plan_once(
                provider_id=provider_id,
                persona_prompt=persona_prompt,
                sensors=sensors,
                now=now,
                policy=policy,
                enhanced=enhanced,
                anti_repeat=None,
                forecast=forecast,
            )
            return result, executed, skipped

        base_prompt = self._build_prompt(
            now=now,
            persona_prompt=persona_prompt,
            sensors=sensors,
            policy=policy,
            enhanced=enhanced,
            anti_repeat=None,
            forecast=forecast,
        )

        async def _one(index: int) -> tuple[dict | None, int, str]:
            """返回 (候选或 None, LLM 调用数, 首个失败详情)；候选只携带 slots。"""
            calls = 0
            first_error = ""
            for attempt in range(policy.max_attempts):
                if attempt > 0:
                    await self._sleep(_retry_delay_seconds(attempt))
                held = await self._gate.acquire(policy.acquire_timeout_seconds)
                if not held:
                    continue
                try:
                    calls += 1
                    result, _ = await generate_daily_schedule(
                        self.context, provider_id=provider_id, prompt=base_prompt,
                    )
                    return {"index": index, "slots": result.slots}, calls, ""
                except Exception as exc:
                    if not first_error:
                        first_error = exception_detail(exc)
                    logger.debug(
                        f"{tag()} 候选 {index + 1} 第 {attempt + 1}/{policy.max_attempts} 次尝试失败: "
                        f"{exception_detail(exc)}"
                    )
                finally:
                    await self._gate.release()
            return None, calls, first_error

        attempts = await asyncio.gather(*(_one(index) for index in range(count)))
        valid = [candidate for candidate, _, _ in attempts if candidate is not None]
        executed = sum(calls for _, calls, _ in attempts)
        logger.debug(
            f"{tag()} 多候选规划: 合法 {len(valid)}/{count} 份，实际调用 {executed} 次"
        )
        if len(valid) < count:
            skipped.append(f"candidates:partial_failure({count - len(valid)}/{count})")
        if not valid:
            raise DailyScheduleGenerationError(
                "llm_error",
                "所有候选日程生成失败",
                retryable=True,
                calls_executed=executed,
                detail=next((err for _, _, err in attempts if err), ""),
            )

        chosen: dict
        # continuity_enabled 透传：关闭时 judge 不带边界指令，deterministic 回退只按反重复排序。
        if judge and len(valid) > 1:
            choice, judge_calls = await self._judge(
                provider_id=provider_id,
                valid=valid,
                prompt_hint=self._judge_hint(
                    enhanced,
                    continuity_enabled=policy.state_continuity_enabled,
                ),
                acquire_timeout_seconds=policy.acquire_timeout_seconds,
                max_attempts=policy.max_attempts,
                persona_prompt=persona_prompt,
                recent_schedules_text=enhanced.recent_schedules_text,
                continuity_enabled=policy.state_continuity_enabled,
            )
            executed += judge_calls
            if choice is None:
                skipped.append("judge:failed")
                chosen = min(
                    valid,
                    key=lambda candidate: deterministic_score(
                        candidate,
                        recent_snapshots=enhanced.recent_snapshots,
                        previous_day_state=enhanced.previous_day_state,
                        today_boundary=enhanced.today_boundary,
                        continuity_enabled=policy.state_continuity_enabled,
                    ),
                )
            else:
                chosen = valid[choice]
        else:
            chosen = min(
                valid,
                key=lambda candidate: deterministic_score(
                    candidate,
                    recent_snapshots=enhanced.recent_snapshots,
                    previous_day_state=enhanced.previous_day_state,
                    today_boundary=enhanced.today_boundary,
                    continuity_enabled=policy.state_continuity_enabled,
                ),
            )

        return (
            PlanningResult(slots=chosen["slots"]),
            executed,
            skipped,
        )

    async def _judge(
        self,
        *,
        provider_id: str,
        valid: list[dict],
        prompt_hint: str,
        acquire_timeout_seconds: float,
        max_attempts: int,
        persona_prompt: str = "",
        recent_schedules_text: str = "",
        continuity_enabled: bool = True,
    ) -> tuple[int | None, int]:
        """返回（选择下标或 None，实际调用数）。"""
        calls = 0
        for attempt in range(max_attempts):
            if attempt > 0:
                await self._sleep(_retry_delay_seconds(attempt))
            held = await self._gate.acquire(acquire_timeout_seconds)
            if not held:
                continue
            try:
                calls += 1
                choice = await select_candidate_with_llm(
                    self.context, provider_id=provider_id, candidates=valid,
                    prompt_hint=prompt_hint,
                    persona_prompt=persona_prompt,
                    recent_schedules_text=recent_schedules_text,
                    continuity_enabled=continuity_enabled)
                if choice >= 0:
                    return choice, calls
            except Exception:
                pass
            finally:
                await self._gate.release()
        return None, calls

    @staticmethod
    def _judge_hint(
        enhanced: EnhancedContext,
        *,
        continuity_enabled: bool = True,
    ) -> str:
        """把角色状态交给 judge 按「候选结构 ↔ 边界状态」评审；连续性关闭时返回空串。"""
        if not continuity_enabled:
            return ""
        parts = []
        if enhanced.today_boundary:
            parts.append(
                json_block(
                    "TODAY_BOUNDARY", enhanced.today_boundary
                )
            )
        if enhanced.previous_day_state:
            parts.append(
                json_block(
                    "PREVIOUS_DAY_STATE", enhanced.previous_day_state
                )
            )
        return "\n\n".join(parts)

    # ---------------- 增强上下文 Token 预算 ----------------

    def _fit_enhanced_context(
        self,
        *,
        enhanced: EnhancedContext,
        provider_id: str,
        now,
        persona_prompt: str,
        sensors: dict,
        policy: AdaptiveGenerationPolicy,
    ) -> EnhancedContext:
        if not (enhanced.previous_day_state or enhanced.recent_schedules_text):
            return enhanced

        automatic_budget = self._automatic_enhanced_token_budget(
            provider_id=provider_id,
            now=now,
            persona_prompt=persona_prompt,
            sensors=sensors,
            policy=policy,
        )
        manual_budget = max(0, int(policy.context_token_budget or 0))
        if manual_budget > 0:
            budget = (
                min(manual_budget, automatic_budget)
                if automatic_budget is not None
                else manual_budget
            )
        else:
            budget = (
                automatic_budget
                if automatic_budget is not None
                else DEFAULT_ENHANCED_CONTEXT_TOKENS
            )
        budget = max(0, int(budget))

        skipped = list(enhanced.skipped)
        used = 0
        previous_day_state = None
        recent_text = ""
        recent_snapshots = enhanced.recent_snapshots

        if enhanced.previous_day_state:
            full_block = json_block(
                "PREVIOUS_DAY_STATE", enhanced.previous_day_state
            )
            full_tokens = estimate_text_tokens(full_block)
            if full_tokens <= budget:
                previous_day_state = enhanced.previous_day_state
                used += full_tokens
            else:
                previous_day_state = self._trim_previous_day_state(
                    enhanced.previous_day_state,
                    budget,
                )
                if previous_day_state:
                    used += estimate_text_tokens(
                        json_block("PREVIOUS_DAY_STATE", previous_day_state)
                    )
                    skipped.append("previous_day_state:truncated")
                else:
                    skipped.append("previous_day_state:budget_exhausted")

        if enhanced.recent_schedules_text:
            remaining = max(0, budget - used)
            recent_text = self._trim_recent_schedules_text(
                enhanced.recent_schedules_text,
                remaining,
            )
            if recent_text:
                used += estimate_text_tokens(
                    json_block("RECENT_SCHEDULES", {"content": recent_text})
                )
                if recent_text != enhanced.recent_schedules_text:
                    skipped.append("recent_schedules:truncated")
            else:
                recent_snapshots = ()
                skipped.append("recent_schedules:budget_exhausted")

        return EnhancedContext(
            recent_schedules_text=recent_text,
            recent_snapshots=recent_snapshots,
            previous_day_state=previous_day_state,
            # Step1 产物：当日初始角色状态，原样透传给 Step2（不裁剪）
            today_boundary=enhanced.today_boundary,
            used_tokens=used,
            skipped=tuple(skipped),
        )

    def _automatic_enhanced_token_budget(
        self,
        *,
        provider_id: str,
        now,
        persona_prompt: str,
        sensors: dict,
        policy: AdaptiveGenerationPolicy,
    ) -> int | None:
        context_limit = self._provider_context_limit(provider_id)
        if context_limit is None:
            return None
        target_input_tokens = max(0, int(context_limit * 0.8))
        base_prompt = self._build_prompt(
            now=now,
            persona_prompt=persona_prompt,
            sensors=sensors,
            policy=policy,
            enhanced=EnhancedContext(),
            anti_repeat=None,
        )
        base_tokens = estimate_text_tokens(
            DEFAULT_DAILY_SCHEDULE_SYSTEM_PROMPT + "\n" + base_prompt
        )
        return max(0, target_input_tokens - base_tokens)

    def _provider_context_limit(self, provider_id: str) -> int | None:
        try:
            provider = self.context.get_provider_by_id(provider_id)
        except Exception:
            provider = None
        if provider is None:
            return None

        provider_config = getattr(provider, "provider_config", {})
        if isinstance(provider_config, dict):
            try:
                configured = int(provider_config.get("max_context_tokens", 0) or 0)
            except (TypeError, ValueError):
                configured = 0
            if configured > 0:
                return configured

        try:
            model = str(provider.get_model() or "").strip()
            from astrbot.core.utils.llm_metadata import LLM_METADATAS

            metadata = LLM_METADATAS.get(model)
            metadata = metadata if isinstance(metadata, dict) else {}
            limits = metadata.get("limit", {})
            limits = limits if isinstance(limits, dict) else {}
            limit = limits.get("context", 0)
            parsed = int(limit or 0)
            return parsed if parsed > 0 else None
        except Exception:
            return None

    @classmethod
    def _trim_previous_day_state(cls, value: dict, budget: int) -> dict | None:
        if budget <= 0 or not isinstance(value, dict):
            return None
        core = {
            "boundary_state": value.get("boundary_state", {}),
            "boundary_date": str(value.get("boundary_date", "") or ""),
        }
        if estimate_text_tokens(json_block("PREVIOUS_DAY_STATE", core)) > budget:
            return None
        schedules = value.get("actual_schedules", [])
        if not isinstance(schedules, list) or not schedules:
            return core
        candidate = {**core, "actual_schedules": []}
        if estimate_text_tokens(
            json_block("PREVIOUS_DAY_STATE", candidate)
        ) > budget:
            return core
        # 优先保留最近几天（列表为 旧→新，先倒序保留最近，再恢复顺序）
        kept: list[dict] = []
        for entry in reversed(schedules):
            if not isinstance(entry, dict):
                continue
            candidate = {**core, "actual_schedules": [*kept, entry]}
            if estimate_text_tokens(
                json_block("PREVIOUS_DAY_STATE", candidate)
            ) > budget:
                break
            kept.append(entry)
        kept.reverse()
        return {**core, "actual_schedules": kept}

    @classmethod
    def _trim_recent_schedules_text(cls, text: str, budget: int) -> str:
        if budget <= 0 or not text:
            return ""
        try:
            entries = json.loads(text)
        except (TypeError, ValueError):
            return ""
        if not isinstance(entries, list):
            return ""
        kept = []
        for entry in entries:
            candidate = json.dumps([*kept, entry], ensure_ascii=False, sort_keys=True)
            block = json_block("RECENT_SCHEDULES", {"content": candidate})
            if estimate_text_tokens(block) > budget:
                break
            kept.append(entry)
        return json.dumps(kept, ensure_ascii=False, sort_keys=True) if kept else ""

    # ---------------- Provider / Persona ----------------

    async def _resolve_provider_id(self) -> str:
        """解析生成 Provider：ai_daily.provider_id → 全局 default_provider_id。"""
        generation_config = self._generation_config()
        provider_id = str(generation_config.get("provider_id", "") or "").strip()
        if not provider_id:
            # “留空使用主对话 LLM”仅指 AstrBot 全局默认 Provider
            try:
                config = self.context.get_config()
            except Exception:
                config = None
            if isinstance(config, dict):
                provider_settings = config.get("provider_settings", {}) or {}
                if isinstance(provider_settings, dict):
                    provider_id = str(
                        provider_settings.get("default_provider_id", "") or ""
                    ).strip()
        if not provider_id:
            raise DailyScheduleGenerationError(
                "provider_unavailable",
                "无法确定 AI 每日日程使用的模型",
                retryable=False,
            )
        return provider_id

    async def _resolve_persona_prompt(self, session: str, persona: Any | None) -> str:
        """从 persona 提取 prompt；未传入时回退自行解析。"""
        generation_config = self._generation_config()
        if not generation_config.get("use_persona", True):
            return ""
        if persona is None:
            try:
                _, persona = await self._persona_resolver(self.context, session)
            except Exception:
                return ""
        return extract_persona_prompt(persona)

    # ---------------- Prompt ----------------

    @staticmethod
    def _safe_reference(value: Any, *, limit: int | None = None) -> str:
        text = _CONTROL_CHARS.sub(" ", str(value or "")).strip()
        return text[:limit] if limit is not None else text

    # ---------------- Step1：角色状态评估 ----------------

    async def _evaluate_today_boundary(
        self,
        *,
        provider_id: str,
        persona_prompt: str,
        sensors: dict,
        now,
        enhanced: EnhancedContext,
    ) -> tuple[dict | None, int, list[str]]:
        """评估当日初始日界状态（Step1）；失败降级返回 None，不阻塞生成。"""
        prompt = self._build_boundary_prompt(
            now=now,
            persona_prompt=persona_prompt,
            sensors=sensors,
            enhanced=enhanced,
        )
        policy = await self.policy()
        calls = 0
        gate_timeouts = 0
        for attempt in range(policy.max_attempts):
            if attempt > 0:
                await self._sleep(_retry_delay_seconds(attempt))
            held = await self._gate.acquire(policy.acquire_timeout_seconds)
            if not held:
                gate_timeouts += 1
                continue
            try:
                try:
                    calls += 1
                    boundary = await evaluate_boundary(
                        self.context, provider_id=provider_id, prompt=prompt)
                    return (
                        self._fill_boundary(boundary, enhanced),
                        calls,
                        [] if attempt == 0 else ["boundary:retried"],
                    )
                except Exception as exc:
                    logger.debug(
                        f"{tag()} Step1 角色状态评估第 {attempt + 1}/{policy.max_attempts} 次尝试失败: "
                        f"{exception_detail(exc)}"
                    )
                    continue
            finally:
                await self._gate.release()
        if calls == 0 and gate_timeouts:
            return None, 0, ["boundary:concurrency_timeout"]
        return None, calls, ["boundary:evaluation_failed"]

    @staticmethod
    def _fill_boundary(
        boundary: dict,
        enhanced: EnhancedContext,
    ) -> dict:
        """回填缺失维度：沿用上次角色状态值，首次为 null；空对象同样走回填继承。"""
        if not isinstance(boundary, dict):
            return {}
        previous = (enhanced.previous_day_state or {}).get("boundary_state") or {}
        filled = dict(boundary)
        for key in (
            "energy", "sleep_debt", "focus", "social_energy", "motivation",
            "physical_state", "daily_theme", "daily_style", "unfinished_plans",
        ):
            if key not in filled:
                filled[key] = previous.get(key) if previous else None
        return filled

    def _build_boundary_prompt(
        self,
        *,
        now,
        persona_prompt: str,
        sensors: dict,
        enhanced: EnhancedContext,
    ) -> str:
        """Step1 输入：日期/世界观/人设/传感器 + 最近可用边界 + 实际时间线。"""
        generation_config = self._generation_config()
        worldview = self._safe_reference(
            generation_config.get("worldview", ""),
        )
        timezone = str(now.tzinfo) if now.tzinfo is not None else str(now.astimezone().tzinfo or "system-local")
        previous = enhanced.previous_day_state or {}
        previous_boundary = previous.get("boundary_state") or {}
        actual_schedules = [
            {
                "date": str(entry.get("date", "") or ""),
                "slots": entry.get("slots", []) or [],
            }
            for entry in (previous.get("actual_schedules") or [])
            if isinstance(entry, dict)
        ]
        blocks = [
            json_block(
                "DATE_CONTEXT",
                {
                    "local_date": now.date().isoformat(),
                    "weekday": now.strftime("%A"),
                    "timezone": timezone,
                    "generated_at_local": now.strftime("%Y-%m-%d %H:%M:%S"),
                },
            ),
            json_block("WORLDVIEW", {"content": worldview}),
            json_block(
                "PERSONA_REFERENCE",
                {
                    "enabled": bool(generation_config.get("use_persona", True)),
                    "available": bool(persona_prompt),
                    "content": self._safe_reference(persona_prompt),
                },
            ),
            json_block("SENSORS", sensors),
        ]
        if previous_boundary:
            blocks.append(json_block("PREVIOUS_BOUNDARY", previous_boundary))
        if actual_schedules:
            blocks.append(json_block("ACTUAL_SCHEDULES", actual_schedules))
        theme_pool = self._theme_pool()
        if theme_pool:
            allow_custom = self._allow_custom_theme()
            blocks.append(
                json_block(
                    "THEME_POOL",
                    {
                        "pool": theme_pool,
                        "allow_custom": allow_custom,
                        "instruction": (
                            "必须优先从 pool 中挑选一个最贴合当日状态的类型作为 daily_theme；"
                            "仅当池中没有任何类型贴合时才可自创 2–12 字的具体类型标签，"
                            "禁止使用「常规日」「日常」等空泛标签。"
                            if allow_custom
                            else "必须严格从 pool 中挑选一个类型作为 daily_theme，不得自创。"
                        ),
                    },
                )
            )
        style_pool = self._style_pool()
        if style_pool:
            allow_custom = self._allow_custom_theme()
            blocks.append(
                json_block(
                    "STYLE_POOL",
                    {
                        "pool": style_pool,
                        "allow_custom": allow_custom,
                        "instruction": (
                            "必须优先从 pool 中挑选一个最贴合当日基调的状态色彩作为 daily_style；"
                            "仅当池中没有任何色彩贴合时才可自创 ≤12 字的风格词。"
                            if allow_custom
                            else "必须严格从 pool 中挑选一个状态色彩作为 daily_style，不得自创。"
                        ),
                    },
                )
            )
        recent_themes = self._recent_themes(enhanced)
        if recent_themes:
            blocks.append(
                json_block(
                    "RECENT_THEMES",
                    {
                        "instruction": "以下最近 N 天实际使用过的主题与状态色彩仅作反重复参考："
                        "当日 daily_theme 与 daily_style 不得与其中任何一天完全相同；"
                        "状态确实相似时，换一个相近但不同的类型/风格。",
                        "items": recent_themes,
                    },
                )
            )
        return "\n\n".join(blocks)

    def _build_prompt(
        self,
        *,
        now,
        persona_prompt: str,
        sensors: dict,
        policy: AdaptiveGenerationPolicy,
        enhanced: EnhancedContext,
        anti_repeat: str | None,
        forecast: dict | None = None,
    ) -> str:
        daily_schedule = self.config.get("daily_schedule", {})
        if not isinstance(daily_schedule, dict):
            daily_schedule = {}
        generation_config = self._generation_config()
        worldview = self._safe_reference(
            generation_config.get("worldview", ""),
        )
        static_slots = [
            {
                "name": self._safe_reference(item.get("name", ""), limit=24),
                "start": self._safe_reference(item.get("start_time", ""), limit=5),
                "end": self._safe_reference(item.get("end_time", ""), limit=5),
                "state": self._safe_reference(item.get("state_prompt", ""), limit=500),
            }
            for item in daily_schedule.get("schedule_templates") or []
            if isinstance(item, dict)
        ]
        timezone = str(now.tzinfo) if now.tzinfo is not None else str(now.astimezone().tzinfo or "system-local")
        blocks = [
            json_block(
                "DATE_CONTEXT",
                {
                    "local_date": now.date().isoformat(),
                    "weekday": now.strftime("%A"),
                    "timezone": timezone,
                    "generated_at_local": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "target_slots": self.max_slots(),
                },
            ),
            json_block("WORLDVIEW", {"content": worldview}),
            json_block(
                "PERSONA_REFERENCE",
                {
                    "enabled": bool(generation_config.get("use_persona", True)),
                    "available": bool(persona_prompt),
                    "content": self._safe_reference(persona_prompt),
                },
            ),
            json_block("SENSORS", sensors),
        ]
        # 增强上下文已在 service 层裁剪；只有连续性开启才注入 TODAY_BOUNDARY（双重保险）。
        if enhanced.today_boundary and policy.state_continuity_enabled:
            blocks.append(
                json_block("TODAY_BOUNDARY", enhanced.today_boundary)
            )
        if enhanced.recent_schedules_text:
            blocks.append(
                json_block("RECENT_SCHEDULES", {"content": enhanced.recent_schedules_text})
            )
        if forecast:
            blocks.append(
                json_block(
                    "WEATHER_FORECAST",
                    {
                        "instruction": "目标日的天气基调（白天/夜间各一段），是角色所在世界当天的天气，"
                        "由系统规定、不存在现实冲突：可把基调落实到具体时段——如白天「小雨」可只安排"
                        "其中 1–2 个时段下雨、其余多云或阴；雨天安排室内、高温避开正午"
                        "外出；state 与天气保持一致，不得脱离基调自创其他天气。",
                        "date": self._safe_reference(forecast.get("fxDate", ""), limit=10),
                        "text_day": self._safe_reference(forecast.get("textDay", ""), limit=40),
                        "text_night": self._safe_reference(forecast.get("textNight", ""), limit=40),
                        "temp_max": self._safe_reference(forecast.get("tempMax", ""), limit=10),
                        "temp_min": self._safe_reference(forecast.get("tempMin", ""), limit=10),
                        **({
                            "variation": self._safe_reference(
                                forecast.get("variation", ""), limit=60
                            )
                        } if forecast.get("variation") else {}),
                    },
                )
            )
        if static_slots:
            # 静态日程在两种优先级下都注入；是否可覆盖由块内 instruction 说明。
            instruction = (
                "以上静态日程仅作参考：你可以覆盖其中任何时段；"
                "未覆盖的时段将由静态日程兜底填充。"
                if policy.ai_priority_over_static
                else "以上静态日程固定生效、优先级高于你的输出："
                "这些时段无需重复规划、留空即可；只有以下时段是固定的，"
                "其余时间请正常规划，不要因此留出不必要的空档。"
            )
            blocks.append(
                json_block(
                    "STATIC_SCHEDULE",
                    {
                        "instruction": instruction,
                        "slots": static_slots,
                    },
                )
            )
        if anti_repeat:
            blocks.append(json_block("ANTI_REPEAT", {"content": anti_repeat}))
        return "\n\n".join(blocks)
