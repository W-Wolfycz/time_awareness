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
    # 旧键 adaptive.max_concurrent_llm 已从 schema 移除且无回退：0/空一律按 1
    assert _policy({"max_concurrent_llm": 4}, runtime={"llm_max_concurrency": 0}).max_concurrent_llm == 1
    assert _policy({}, runtime={"llm_max_concurrency": 100}).max_concurrent_llm == 4
    assert _policy({}, runtime={"llm_max_concurrency": ""}).max_concurrent_llm == 1


def test_gate_capacity_and_release():
    """并发上限决定同时能跑几个：占满后新的 acquire_wait 必须等，释放一个立刻放行。"""

    async def scenario():
        gate = AdaptiveConcurrencyGate(max_concurrent=3)
        for _ in range(3):
            await gate.acquire_wait(3)

        blocked = asyncio.create_task(gate.acquire_wait(3))
        await asyncio.sleep(0.05)
        assert not blocked.done()
        await gate.release()
        assert await asyncio.wait_for(blocked, 1) is None  # acquire_wait 不返回布尔

    asyncio.run(scenario())


def test_acquire_wait_blocks_until_released():
    """队列不设超时：拿不到槽位就一直等，不能退化成"拿不到就放弃"。"""

    async def scenario():
        gate = AdaptiveConcurrencyGate(max_concurrent=1)
        await gate.acquire_wait(1)

        waiter = asyncio.create_task(gate.acquire_wait(1))
        await asyncio.sleep(0.05)
        assert not waiter.done()  # 还在等，没有立刻放弃
        await gate.release()
        await asyncio.wait_for(waiter, 1)

    asyncio.run(scenario())


def test_max_attempts_prefers_runtime():
    """重试次数只认 runtime.llm_max_attempts；旧 ai_daily 位置已被配置裁剪，不再回退。"""

    def ai_policy(ai_daily, runtime=None):
        config = {"daily_schedule": {"ai_daily": ai_daily}}
        if runtime is not None:
            config["runtime"] = runtime
        return AdaptiveGenerationPolicy.from_config(config)

    # 旧键 ai_daily.max_attempts 会被 AstrBot 配置完整性检查删掉，代码不再回退 → 走默认
    assert ai_policy({"max_attempts": 4}).max_attempts == 3
    assert ai_policy({}, {"llm_max_attempts": 2}).max_attempts == 2
    assert ai_policy({"max_attempts": 4}, {"llm_max_attempts": 2}).max_attempts == 2
    assert ai_policy({}, {"llm_max_attempts": 99}).max_attempts == 5  # 上限钳制
    assert ai_policy({}, {"llm_max_attempts": 0}).max_attempts == 0  # 0 = 跟随 AstrBot 默认
    assert ai_policy({}, {"llm_max_attempts": -3}).max_attempts == 0  # 负数收敛到 0
