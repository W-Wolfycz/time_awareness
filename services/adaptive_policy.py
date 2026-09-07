"""AI 每日日程自适应策略与并发闸门：配置解析、能力开关与并发控制（不发起 LLM 调用）。"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass

from ..utils.time_utils import _bounded_int


DEFAULT_ENHANCED_CONTEXT_TOKENS = 8000
DEFAULT_ACQUIRE_TIMEOUT_SECONDS = 10.0

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
        "max_attempts": 3,
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
    context_token_budget: int = 0
    # 顶层 runtime.llm_max_concurrency 优先；0/空回退旧 nested max_concurrent_llm。
    max_concurrent_llm: int = 1
    acquire_timeout_seconds: float = DEFAULT_ACQUIRE_TIMEOUT_SECONDS
    ai_priority_over_static: bool = False
    max_attempts: int = 3


    # ---------------- 解析 ----------------

    @classmethod
    def from_config(cls, config: dict) -> "AdaptiveGenerationPolicy":
        daily = config.get("daily_schedule", {})
        ai_daily = daily.get("ai_daily", {}) if isinstance(daily, dict) else {}
        adaptive = ai_daily.get("adaptive", {}) if isinstance(ai_daily, dict) else {}
        generation = resolve_ai_generation_config(config)
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
            context_token_budget=_bounded_int(
                adaptive.get("context_token_budget"), 0, 0, 1000000
            ),
            max_concurrent_llm=cls._global_concurrency(adaptive=adaptive, runtime=runtime),
            acquire_timeout_seconds=float(
                _bounded_int(adaptive.get("acquire_timeout_seconds"), 10, 0, 120)
            ),
            ai_priority_over_static=bool(
                ai_daily.get("ai_priority_over_static", False)
            ),
            max_attempts=_bounded_int(
                generation.get("max_attempts", 3) if isinstance(generation, dict) else 3,
                3, 1, 5,
            ),
        )

    # ---------------- 预算决策 ----------------

    @classmethod
    def _global_concurrency(cls, *, adaptive: dict, runtime: dict) -> int:
        """全局 AI 日程 LLM 并发上限：顶层优先，0/空回退旧值，钳制到 1–4。"""
        raw = runtime.get("llm_max_concurrency")
        if raw in (None, 0, "", "0"):
            raw = adaptive.get("max_concurrent_llm", 1)
        return _bounded_int(raw, 1, 1, 4)

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


def estimate_text_tokens(text: str) -> int:
    """复用 AstrBot 默认估算口径：中文约 0.6 token/字，其余约 0.3。"""
    value = str(text or "")
    chinese = sum(1 for char in value if "\u4e00" <= char <= "\u9fff")
    estimated = chinese * 0.6 + (len(value) - chinese) * 0.3
    return max(0, math.ceil(estimated))


@dataclass(frozen=True)
class EnhancedContext:
    """service 层算好的增强上下文（文本已按预算裁剪），交给 generation 组装 Prompt。"""

    recent_schedules_text: str = ""
    recent_snapshots: tuple[dict, ...] = ()
    previous_day_state: dict | None = None
    today_boundary: dict | None = None
    used_tokens: int = 0
    skipped: tuple[str, ...] = ()


class AdaptiveConcurrencyGate:
    """本插件所有直接 LLM 调用共用的全局并发闸门；超时获取不到槽位返回 False。"""

    def __init__(self, max_concurrent: int = 1):
        self._max = max(1, int(max_concurrent))
        self._active = 0
        self._condition = asyncio.Condition()

    @property
    def max_concurrent(self) -> int:
        return self._max

    async def reconfigure(self, max_concurrent: int) -> None:
        value = max(1, int(max_concurrent))
        if value == self._max:
            return
        async with self._condition:
            self._max = value
            self._condition.notify_all()

    async def acquire(self, timeout: float) -> bool:
        """获取并发槽；超时返回 False；timeout<=0 为非阻塞立即尝试。"""
        try:
            async with self._condition:
                if timeout <= 0:
                    if self._active < self._max:
                        self._active += 1
                        return True
                    return False
                try:
                    await asyncio.wait_for(
                        self._condition.wait_for(lambda: self._active < self._max),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    return False
                self._active += 1
                return True
        except asyncio.CancelledError:
            raise

    async def acquire_configured(self, config: dict) -> bool:
        """按当前配置刷新容量并获取槽位。"""
        policy = AdaptiveGenerationPolicy.from_config(config).effective()
        await self.reconfigure(policy.max_concurrent_llm)
        return await self.acquire(policy.acquire_timeout_seconds)

    async def release(self) -> None:
        async with self._condition:
            self._active = max(0, self._active - 1)
            self._condition.notify()
