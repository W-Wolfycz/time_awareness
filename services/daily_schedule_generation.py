"""AI 每日日程的一次生成用例。

负责 Provider、Persona、结构化事实、增强上下文、Prompt 与输出协议；
不负责 persona/date 幂等、锁、重试、后台任务或快照持久化（由
``DailyScheduleService`` 负责）。

支持自适应流水线：多候选并行（受并发闸门限制）、
可选 LLM 评选、最近日程相似度检测与定向重生成。2.0.0 起 Step2 统一数组
协议（theme/boundary_state 由 Step1 边界评估独立产出，对象协议已移除）。
关闭全部增强项时退化为一次基础规划调用。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..domain.schedule import schedule_similarity
from ..integrations.persona_resolver import extract_persona_prompt, resolve_effective_persona
from ..constants import DEFAULT_DAILY_SCHEDULE_SYSTEM_PROMPT
from ..llm.daily_schedule_generator import (
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


PROMPT_REVISION = "daily_schedule_v2"
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# AI 日程所有“重试语义”LLM 循环的统一退避序列（秒）：第 1 次尝试不等待，
# 之后 5s、30s，超出封顶 30s——与 ``DailyScheduleService._generate_with_retries``
# docstring 的 0/5/30s 契约一致。统一覆盖 Step1 边界评估、单候选规划、多候选
# 每个候选、LLM judge 与反重复重生成内部的失败重试；不用于已成功返回的候选、
# 纯本地 fallback 或任何非重试路径。通过可注入 ``sleep`` 实现，测试注入 fake
# 避免真实等待。
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
    ):
        super().__init__(message)
        self.error_type = error_type
        self.user_message = message
        self.retryable = retryable
        # 本次 attempt 内已实际发起的逻辑 LLM 调用数，仅用于诊断日志。
        self.calls_executed = int(calls_executed or 0)


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

    async def generate(
        self,
        *,
        session: str,
        now,
        enhanced: EnhancedContext | None = None,
        persona: Any | None = None,
    ) -> GeneratedDailySchedule:
        """生成当天日程。

        ``persona`` 由 service 层在解析身份时同一次解析得到并传入，保证
        快照归属与注入内容同源；未传入时（独立使用/测试）内部回退自行解析。
        """
        policy = await self.policy()
        enhanced = enhanced or EnhancedContext()
        provider_id = await self._resolve_provider_id(session)
        persona_prompt = await self._resolve_persona_prompt(session, persona)
        sensors = self.time_context.build_generation_sensor_snapshot(now=now)
        # Step1：角色状态评估（当日初始 boundary），失败降级不阻塞排日程
        calls_executed = 0
        skipped: list[str] = []
        # Step1 边界评估仅在「状态连续性」开启时执行：关闭时既不调用
        # boundary LLM，也不注入 TODAY_BOUNDARY，更不让连续性影响评分。
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
        elif not policy.state_continuity_enabled:
            # 连续性关闭：防御性压制调用方/测试注入的边界数据，确保
            # Step2 prompt、judge 与评分路径都不会拿到边界语义。
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
            )
            anti_repeat = None
            if (
                policy.enabled
                and policy.use_recent_schedules
                and enhanced.recent_snapshots
                and self._is_repeat(result.slots, enhanced.recent_snapshots)
            ):
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
                    )
                except DailyScheduleGenerationError:
                    # 反重复重生成最终失败：保留原合法候选（计划 §4.3）
                    extra_skipped = extra_skipped + ["antirepeat:failed"]
                    anti_repeat = None
                else:
                    executed += executed2
                    extra_skipped = extra_skipped + extra2
            return result, executed, extra_skipped, anti_repeat

        result, executed, extra_skipped = await self._plan_candidates(
            provider_id=provider_id,
            persona_prompt=persona_prompt,
            sensors=sensors,
            now=now,
            policy=policy,
            enhanced=enhanced,
        )
        anti_repeat = None
        if (
            policy.use_recent_schedules
            and enhanced.recent_snapshots
            and self._is_repeat(result.slots, enhanced.recent_snapshots)
        ):
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
                )
            except DailyScheduleGenerationError:
                # 反重复重生成最终失败：保留原合法候选（计划 §4.3）
                extra_skipped = extra_skipped + ["antirepeat:failed"]
                anti_repeat = None
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
    ) -> tuple[PlanningResult, int, list[str]]:
        prompt = self._build_prompt(
            now=now,
            persona_prompt=persona_prompt,
            sensors=sensors,
            policy=policy,
            enhanced=enhanced,
            anti_repeat=anti_repeat,
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
                    max_slots=self.max_slots())
                return result, calls, (["planning:retried"] if attempt else [])
            except Exception as exc:
                if attempt + 1 >= policy.max_attempts:
                    kind = "invalid_output" if isinstance(exc, ScheduleValidationError) else "llm_error"
                    message = "模型输出未通过日程协议校验" if kind == "invalid_output" else "调用日程生成模型失败"
                    raise DailyScheduleGenerationError(kind, message, retryable=True,
                                                       calls_executed=calls) from exc
            finally:
                await self._gate.release()
        raise AssertionError("unreachable")

    def _is_repeat(self, slots: list[dict], recent_snapshots: tuple[dict, ...]) -> bool:
        for snapshot in recent_snapshots:
            if schedule_similarity(slots, snapshot.get("slots") or []) >= SIMILARITY_REPEAT_THRESHOLD:
                return True
        return False

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
            )
            return result, executed, skipped

        base_prompt = self._build_prompt(
            now=now,
            persona_prompt=persona_prompt,
            sensors=sensors,
            policy=policy,
            enhanced=enhanced,
            anti_repeat=None,
        )

        async def _one(index: int) -> tuple[dict | None, int]:
            """返回 (候选或 None, 是否实际发起了 LLM 调用)。

            并发槽超时不发起调用（attempted=False）；发起但失败仍计入调用。
            候选只携带 ``slots``，不携带角色状态——Step1 当日边界是所有
            候选共享的当日初始状态，不是候选专属字段。确定性/启发式连续性
            评分与 LLM judge 都以候选 slots 对边界（enhanced.today_boundary
            或前一日 boundary_state）匹配，见 ``deterministic_score``。
            """
            calls = 0
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
                        max_slots=self.max_slots())
                    return {"index": index, "slots": result.slots}, calls
                except Exception:
                    pass
                finally:
                    await self._gate.release()
            return None, calls

        attempts = await asyncio.gather(*(_one(index) for index in range(count)))
        valid = [candidate for candidate, _ in attempts if candidate is not None]
        executed = sum(calls for _, calls in attempts)
        if len(valid) < count:
            skipped.append(f"candidates:partial_failure({count - len(valid)}/{count})")
        if not valid:
            raise DailyScheduleGenerationError(
                "llm_error",
                "所有候选日程生成失败",
                retryable=True,
                calls_executed=executed,
            )

        chosen: dict
        # continuity_enabled 透传：关闭时 judge 不带边界指令，deterministic
        # 回退也只按反重复分量排序（不做 slots↔boundary 匹配）。
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
        """把角色状态显式交给 judge，让它按「候选时段结构 ↔ 边界状态」评审。

        当日边界（Step1 边界评估产物）优先；Step1 缺失时昨日状态仍作为参考。
        与生成请求同风格（``<TAG>`` + JSON 块）。``state_continuity_enabled=False``
        时返回空串，judge 不携带任何边界语义（只按候选自身内容与反重复倾向评选）。
        """
        if not continuity_enabled:
            return ""
        parts = []
        if enhanced.today_boundary:
            parts.append(
                DailyScheduleGeneration._json_block(
                    "TODAY_BOUNDARY", enhanced.today_boundary
                )
            )
        if enhanced.previous_day_state:
            parts.append(
                DailyScheduleGeneration._json_block(
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
            full_block = self._json_block(
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
                        self._json_block("PREVIOUS_DAY_STATE", previous_day_state)
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
                    self._json_block("RECENT_SCHEDULES", {"content": recent_text})
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
        if estimate_text_tokens(cls._json_block("PREVIOUS_DAY_STATE", core)) > budget:
            return None
        schedules = value.get("actual_schedules", [])
        if not isinstance(schedules, list) or not schedules:
            return core
        candidate = {**core, "actual_schedules": []}
        if estimate_text_tokens(
            cls._json_block("PREVIOUS_DAY_STATE", candidate)
        ) > budget:
            return core
        # 优先保留最近几天（列表为 旧→新，先倒序保留最近，再恢复顺序）
        kept: list[dict] = []
        for entry in reversed(schedules):
            if not isinstance(entry, dict):
                continue
            candidate = {**core, "actual_schedules": [*kept, entry]}
            if estimate_text_tokens(
                cls._json_block("PREVIOUS_DAY_STATE", candidate)
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
            block = cls._json_block("RECENT_SCHEDULES", {"content": candidate})
            if estimate_text_tokens(block) > budget:
                break
            kept.append(entry)
        return json.dumps(kept, ensure_ascii=False, sort_keys=True) if kept else ""

    # ---------------- Provider / Persona ----------------

    async def _resolve_provider_id(self, session: str) -> str:
        """解析 AI 日程生成使用的 Provider（2.0.0 计划 §4）。

        固定顺序：``daily_schedule.ai_daily.provider_id`` → AstrBot 全局
        ``provider_settings.default_provider_id``。不从 UMO 猜测，也不尝试
        读取某个会话临时 Provider（会话隔离偏好不影响日程生成）。
        """
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
        """从 persona 对象提取 prompt。

        优先使用 service 层同一次解析得到的 persona（与身份归属同源）；
        未传入时（独立使用/测试）回退自行解析。
        """
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

    @staticmethod
    def _json_block(name: str, value: Any) -> str:
        body = json.dumps(value, ensure_ascii=False, sort_keys=True)
        body = body.replace("<", "\\u003c").replace(">", "\\u003e")
        return f"<{name}>\n{body}\n</{name}>"

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
        """评估当日初始日界状态（Step1）。

        输入：前一个可用日界状态 + 最近连续实际日程（含用户修改）+ 当日传感器。
        失败时降级返回 None（Step2 无边界参考照常排日程），不阻塞生成。
        """
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
                except Exception:
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
        """回填缺失维度：未输出的维度沿用上次角色状态值，首次为 null。

        「全部无变化」时模型会输出空对象 ``{}``——它同样是稀疏输出，必须
        走同一回填路径继承上次状态，否则连续性链会断。
        """
        if not isinstance(boundary, dict):
            return {}
        previous = (enhanced.previous_day_state or {}).get("boundary_state") or {}
        filled = dict(boundary)
        for key in (
            "energy", "sleep_debt", "focus", "social_energy", "motivation",
            "physical_state", "daily_theme", "unfinished_plans",
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
        """Step1 输入：日期/世界观/人设/传感器 + 最近可用边界 + 实际时间线。

        边界取回溯到的最近一个非空角色状态；实际时间线自该边界日起
        （空边界分支为整个参考窗口）携带，含用户修改与静态兜底。
        """
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
            self._json_block(
                "DATE_CONTEXT",
                {
                    "local_date": now.date().isoformat(),
                    "weekday": now.strftime("%A"),
                    "timezone": timezone,
                    "generated_at_local": now.strftime("%Y-%m-%d %H:%M:%S"),
                },
            ),
            self._json_block("WORLDVIEW", {"content": worldview}),
            self._json_block(
                "PERSONA_REFERENCE",
                {
                    "enabled": bool(generation_config.get("use_persona", True)),
                    "available": bool(persona_prompt),
                    "content": self._safe_reference(persona_prompt),
                },
            ),
            self._json_block("SENSORS", sensors),
        ]
        if previous_boundary:
            blocks.append(self._json_block("PREVIOUS_BOUNDARY", previous_boundary))
        if actual_schedules:
            blocks.append(self._json_block("ACTUAL_SCHEDULES", actual_schedules))
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
    ) -> str:
        daily_schedule = self.config.get("daily_schedule", {})
        if not isinstance(daily_schedule, dict):
            daily_schedule = {}
        generation_config = self._generation_config()
        worldview = self._safe_reference(
            generation_config.get("worldview", ""),
        )
        static_slots = []
        for item in daily_schedule.get("schedule_templates") or []:
            if not isinstance(item, dict):
                continue
            static_slots.append(
                {
                    "name": self._safe_reference(item.get("name", ""), limit=24),
                    "start": self._safe_reference(item.get("start_time", ""), limit=5),
                    "end": self._safe_reference(item.get("end_time", ""), limit=5),
                    "state": self._safe_reference(item.get("state_prompt", ""), limit=500),
                }
            )
        timezone = str(now.tzinfo) if now.tzinfo is not None else str(now.astimezone().tzinfo or "system-local")
        blocks = [
            self._json_block(
                "DATE_CONTEXT",
                {
                    "local_date": now.date().isoformat(),
                    "weekday": now.strftime("%A"),
                    "timezone": timezone,
                    "generated_at_local": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "max_slots": self.max_slots(),
                },
            ),
            self._json_block("WORLDVIEW", {"content": worldview}),
            self._json_block(
                "PERSONA_REFERENCE",
                {
                    "enabled": bool(generation_config.get("use_persona", True)),
                    "available": bool(persona_prompt),
                    "content": self._safe_reference(persona_prompt),
                },
            ),
            self._json_block("SENSORS", sensors),
        ]
        # 增强上下文（已在 service 层按预算裁剪）
        # 只有状态连续性开启时才注入 TODAY_BOUNDARY（generate() 已压制，
        # 此处双重保险，防止任何调用方绕过 generate() 直接构造 prompt）。
        if enhanced.today_boundary and policy.state_continuity_enabled:
            blocks.append(
                self._json_block("TODAY_BOUNDARY", enhanced.today_boundary)
            )
        if enhanced.recent_schedules_text:
            blocks.append(
                self._json_block("RECENT_SCHEDULES", {"content": enhanced.recent_schedules_text})
            )
        if static_slots:
            # 静态日程在两种优先级下都注入，让规划模型知道其存在；
            # 是否可覆盖由块内 instruction 按配置说明。
            instruction = (
                "以上静态日程仅作参考：你可以覆盖其中任何时段；"
                "未覆盖的时段将由静态日程兜底填充。"
                if policy.ai_priority_over_static
                else "以上静态日程固定生效、优先级高于你的输出："
                "这些时段无需重复规划、留空即可；只有以下时段是固定的，"
                "其余时间请正常规划，不要因此留出不必要的空档。"
            )
            blocks.append(
                self._json_block(
                    "STATIC_SCHEDULE",
                    {
                        "instruction": instruction,
                        "slots": static_slots,
                    },
                )
            )
        if anti_repeat:
            blocks.append(self._json_block("ANTI_REPEAT", {"content": anti_repeat}))
        return "\n\n".join(blocks)
