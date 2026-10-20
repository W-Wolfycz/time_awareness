"""services/adaptive_policy.py 的配置解析与并发闸门测试。"""

import asyncio

from time_awareness.services.adaptive_policy import (
    AdaptiveConcurrencyGate,
    AdaptiveGenerationPolicy,
)


def _policy(adaptive, runtime=None):
    config = {"daily_schedule": {"ai_daily": {"adaptive": adaptive}}}
    if runtime is not None:
        config["runtime"] = runtime
    return AdaptiveGenerationPolicy.from_config(config)


def test_from_config_clamps_fields():
    assert _policy({"recent_days": 0}).use_recent_schedules is False
    assert _policy({"candidate_count": 99}).candidate_count == 5
    assert _policy({"candidate_count": 0}).candidate_count == 1
    assert _policy({"candidate_selection": "x"}).candidate_selection == "heuristic"


def test_global_concurrency():
    assert _policy({}, runtime={"llm_max_concurrency": 3}).max_concurrent_llm == 3
    cfg = {"max_concurrent_llm": 4}, {"llm_max_concurrency": 0}
    assert _policy(cfg[0], runtime=cfg[1]).max_concurrent_llm == 4
    assert _policy({}, runtime={"llm_max_concurrency": 100}).max_concurrent_llm == 4
    assert _policy({}, runtime={"llm_max_concurrency": ""}).max_concurrent_llm == 1


def test_gate_exclusive_release_and_max_three():
    async def scenario():
        gate = AdaptiveConcurrencyGate(max_concurrent=1)
        assert await gate.acquire(0) is True
        assert await gate.acquire(0) is False
        await gate.release()
        assert await gate.acquire(0) is True

        three = AdaptiveConcurrencyGate(max_concurrent=3)
        assert all([await three.acquire(0) for _ in range(3)])
        assert await three.acquire(0) is False

    asyncio.run(scenario())
