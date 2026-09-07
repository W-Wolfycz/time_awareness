"""services/adaptive_policy.py 的配置解析与并发闸门测试。"""

import asyncio

from time_awareness.services.adaptive_policy import (
    AdaptiveConcurrencyGate,
    AdaptiveGenerationPolicy,
    estimate_text_tokens,
    resolve_ai_generation_config,
)


def _policy(adaptive, runtime=None):
    config = {"daily_schedule": {"ai_daily": {"adaptive": adaptive}}}
    if runtime is not None:
        config["runtime"] = runtime
    return AdaptiveGenerationPolicy.from_config(config)


def test_resolve_ai_generation_config():
    ai_daily = {"provider_id": "p1", "use_persona": False, "max_attempts": 5}
    resolved = resolve_ai_generation_config({"daily_schedule": {"ai_daily": ai_daily}})
    assert resolved["provider_id"] == "p1"
    assert resolved["use_persona"] is False and resolved["max_attempts"] == 5
    assert resolve_ai_generation_config({})["max_attempts"] == 3
    assert resolve_ai_generation_config(None)["use_persona"] is True
    assert resolve_ai_generation_config({"daily_schedule": "x"})["provider_id"] == ""


def test_from_config_parses_adaptive():
    policy = _policy(
        {"enabled": True, "recent_days": 7, "candidate_count": 3, "candidate_selection": "llm"}
    )
    assert policy.enabled is True and policy.use_recent_schedules is True
    assert policy.recent_days == 7 and policy.candidate_count == 3
    assert policy.candidate_selection == "llm"


def test_from_config_clamps_fields():
    assert _policy({"recent_days": 0}).use_recent_schedules is False
    assert _policy({"candidate_count": 99}).candidate_count == 5
    assert _policy({"candidate_count": 0}).candidate_count == 1
    assert _policy({"candidate_selection": "x"}).candidate_selection == "heuristic"


def test_effective_disabled_returns_defaults():
    effective = _policy({"enabled": False, "candidate_count": 5}).effective()
    assert effective.enabled is False and effective.candidate_count == 1
    assert effective.use_recent_schedules is False


def test_global_concurrency():
    assert _policy({}, runtime={"llm_max_concurrency": 3}).max_concurrent_llm == 3
    cfg = {"max_concurrent_llm": 4}, {"llm_max_concurrency": 0}
    assert _policy(cfg[0], runtime=cfg[1]).max_concurrent_llm == 4
    assert _policy({}, runtime={"llm_max_concurrency": 100}).max_concurrent_llm == 4
    assert _policy({}, runtime={"llm_max_concurrency": ""}).max_concurrent_llm == 1


def test_wants_candidates_and_effective_judge():
    policy = _policy({"enabled": True, "candidate_count": 3, "candidate_selection": "llm"})
    assert policy.wants_candidates is True and policy.effective_judge is True
    single = _policy({"candidate_count": 1})
    assert single.wants_candidates is False and single.effective_judge is False


def test_estimate_text_tokens():
    assert estimate_text_tokens("你好") == 2
    assert estimate_text_tokens("abc") == 1
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens("中文ab") == 2


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
