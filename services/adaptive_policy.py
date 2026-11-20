"""AI 每日日程自适应策略与并发闸门：配置解析、能力开关与并发控制（不发起 LLM 调用）。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ..utils.time_utils import _bounded_int



# 确定性相似度阈值：超过则判定与最近日程重复
SIMILARITY_REPEAT_THRESHOLD = 0.75


def resolve_ai_generation_config(config: dict) -> dict:
    """读取 AI 日程生成输入：仅读取 ai_daily 扁平字段。"""
    daily = config.get("daily_schedule", {}) if isinstance(config, dict) else {}
    if not isinstance(daily, dict):
        daily = {}
    ai_daily = daily.get("ai_daily", {})
    if not isinstance(ai_daily, dict):
        ai_daily = {}

    defaults = {
        "provider_id": "",
        "use_persona": True,
        "worldview": "",
    }
    resolved = dict(defaults)
    for key in defaults:
        if key in ai_daily:
            resolved[key] = ai_daily[key]
    return resolved


@dataclass(frozen=True)
class AdaptiveGenerationPolicy:
    """daily_schedule.ai_daily.adaptive 配置的解析结果。"""

    enabled: bool = False
    use_recent_schedules: bool = False
    recent_days: int = 0
    state_continuity_enabled: bool = False
    candidate_count: int = 1
    candidate_selection: str = "heuristic"
    max_concurrent_llm: int = 1
    ai_priority_over_static: bool = False
    max_attempts: int = 3


    # ---------------- 解析 ----------------

    @classmethod
    def from_config(cls, config: dict) -> "AdaptiveGenerationPolicy":
        daily = config.get("daily_schedule", {})
        ai_daily = daily.get("ai_daily", {}) if isinstance(daily, dict) else {}
        adaptive = ai_daily.get("adaptive", {}) if isinstance(ai_daily, dict) else {}
        runtime = config.get("runtime", {})
        if not isinstance(adaptive, dict):
            adaptive = {}
        if not isinstance(runtime, dict):
            runtime = {}
        enabled = bool(adaptive.get("enabled", False))
        recent_days = _bounded_int(adaptive.get("recent_days"), 0, 0, 30)
        use_recent_schedules = recent_days > 0
        state_continuity_enabled = bool(
            adaptive.get("state_continuity_enabled", False)
        )
        candidate_selection = str(
            adaptive.get("candidate_selection", "heuristic")
            or "heuristic"
        ).strip().lower()
        if candidate_selection not in {"heuristic", "llm"}:
            candidate_selection = "heuristic"
        return cls(
            enabled=enabled,
            use_recent_schedules=use_recent_schedules,
            recent_days=recent_days,
            state_continuity_enabled=state_continuity_enabled,
            candidate_count=_bounded_int(adaptive.get("candidate_count"), 1, 1, 5),
            candidate_selection=candidate_selection,
            max_concurrent_llm=cls._global_concurrency(adaptive=adaptive, runtime=runtime),
            ai_priority_over_static=bool(
                ai_daily.get("ai_priority_over_static", False)
            ),
            max_attempts=_bounded_int(
                runtime.get("llm_max_attempts"),
                3,
                0,
                5,
            ),
        )

    # ---------------- 预算决策 ----------------

    @classmethod
    def _global_concurrency(cls, *, adaptive: dict, runtime: dict) -> int:
        """全局 LLM 并发上限：0/空按 1，钳制到 1–4。"""
        return _bounded_int(runtime.get("llm_max_concurrency"), 1, 1, 4)

    @property
    def enhanced(self) -> bool:
        """是否有任何 C 阶段低成本增强开启。"""
        return bool(
            self.use_recent_schedules
            or self.state_continuity_enabled
        )

    @property
    def wants_candidates(self) -> bool:
        return self.candidate_count > 1

    def effective(self) -> "AdaptiveGenerationPolicy":
        """实际生效策略：总开关关闭时返回全默认，残留子配置不生效。"""
        if not self.enabled:
            return AdaptiveGenerationPolicy(
                ai_priority_over_static=self.ai_priority_over_static,
                max_attempts=self.max_attempts,
                # 全局并发上限不随自适应总开关关闭；基线单候选调用同样受其约束
                max_concurrent_llm=self.max_concurrent_llm,
            )
        return self

    @property
    def effective_candidate_count(self) -> int:
        return self.effective().candidate_count

    @property
    def effective_judge(self) -> bool:
        policy = self.effective()
        return policy.candidate_selection == "llm" and policy.candidate_count > 1



@dataclass(frozen=True)
class EnhancedContext:
    """service 层准备好的增强上下文，交给 generation 组装 Prompt。"""

    recent_schedules_text: str = ""
    recent_snapshots: tuple[dict, ...] = ()
    previous_day_state: dict | None = None
    today_boundary: dict | None = None


class AdaptiveConcurrencyGate:
    """本插件所有直接 LLM 调用共用的全局并发闸门：队列不设超时，拿不到槽位就一直等。"""

    def __init__(self, max_concurrent: int = 1):
        self._max = max(1, int(max_concurrent))
        self._active = 0
        self._condition = asyncio.Condition()

    async def reconfigure(self, max_concurrent: int) -> None:
        value = max(1, int(max_concurrent))
        if value == self._max:
            return
        async with self._condition:
            self._max = value
            self._condition.notify_all()

    async def acquire_wait(self, max_concurrent: int) -> None:
        """按配置刷新容量后一直等，直到拿到槽位（队列不设超时）。"""
        await self.reconfigure(max_concurrent)
        async with self._condition:
            await self._condition.wait_for(lambda: self._active < self._max)
            self._active += 1

    async def release(self) -> None:
        async with self._condition:
            self._active = max(0, self._active - 1)
            self._condition.notify()
