"""服务层「取数规则」测试：计划/已过各取哪一层、未来日走哪份静态、一轮补几天。

断言的是规则本身（来源选择 + 已执行排除），用对照场景而不是单一样例：
同一个 now，分别给「有快照」「无快照」「快照里静态副本过期」三种输入。
"""

import asyncio
import datetime

from time_awareness.core.daily_schedule_store import DailyScheduleSnapshotStore
from time_awareness.services.daily_schedule_generation import PROMPT_REVISION
from time_awareness.services.daily_schedule_service import (
    GENERATION_RETRY_GAP_MINUTES,
    DailyScheduleService,
)
from time_awareness.tests._shared import FakeContext

NOW = datetime.datetime(2026, 10, 20, 10, 0, tzinfo=datetime.timezone.utc)
TODAY = NOW.date()
TOMORROW = TODAY + datetime.timedelta(days=1)
SESSION = "aiocqhttp:GroupMessage:10001"
PERSONA_ID = "persona_demo"
STATIC_TEMPLATES = [{"name": "晨会", "start_time": "08:00", "end_time": "09:00"}]


class _FakeTime:
    def __init__(self, now):
        self._now = now

    def now(self):
        return self._now


async def _resolver(context, session, event=None):
    return PERSONA_ID, None


def _config(horizon=24, templates=None, enabled=True):
    return {
        "daily_schedule": {
            "enable_schedule": True,
            "ai_daily": {"enabled": enabled, "plan_horizon_hours": horizon},
            "schedule_templates": list(templates or []),
        }
    }


def _snapshot(persona_hash, day, *, executed=(), ai=(), static=()):
    return {
        "persona_hash": persona_hash,
        "local_date": day.isoformat(),
        "timezone": "UTC",
        "snapshot_id": f"snap-{day.isoformat()}",
        "status": "ready",
        "source": "ai",
        "generated_at": f"{day.isoformat()}T00:05:00+00:00",
        "prompt_revision": PROMPT_REVISION,
        "executed_until": "00:00",
        "executed_slots": [dict(slot) for slot in executed],
        "ai_slots": [dict(slot) for slot in ai],
        "user_slots": [],
        "static_slots": [dict(slot) for slot in static],
        "boundary_state": None,
        "slots": [],
    }


async def _service(tmp_path, config):
    store = DailyScheduleSnapshotStore(str(tmp_path))
    store.load()
    service = DailyScheduleService(
        context=FakeContext(),
        config=config,
        store=store,
        time_context=_FakeTime(NOW),
        persona_resolver=_resolver,
    )
    await service.register_session_async(SESSION, trigger=False)
    return service, store


def test_plan_takes_unexecuted_layers_and_static_only_when_no_snapshot(tmp_path):
    """计划=未执行层的拼装；已过=executed；明天没有快照才退回静态模板。"""

    async def scenario():
        service, store = await _service(tmp_path, _config(templates=STATIC_TEMPLATES))
        persona_hash = service._identity_for(SESSION)
        store.save_ready(
            _snapshot(
                persona_hash,
                TODAY,
                executed=[{"name": "早饭", "start": "07:00", "end": "08:00", "state": "在吃"}],
                ai=[{"name": "开会", "start": "14:00", "end": "15:00", "state": "在开会"}],
            ),
            retention_days=30,
            today=TODAY,
        )
        plan = service.plan_summary(SESSION, now=NOW)
        done = service.done_summary(SESSION, now=NOW)
        return plan or "", done or ""

    plan, done = asyncio.run(scenario())
    # 未执行才进计划，已执行的段不能出现在计划里
    assert "14:00-15:00 开会" in plan and "早饭" not in plan
    # 已过只取 executed
    assert "07:00-08:00 早饭" in done and "开会" not in done
    # 明天没有快照 → 静态模板兜底（日程是拼出来的，不是"未规划"）
    assert "明天" in plan and "08:00-09:00 晨会" in plan


def test_future_day_uses_current_static_not_snapshot_copy(tmp_path):
    """未来日有快照时，静态应取"当前配置"：改过静态作息后计划不能还显示旧副本。"""

    async def scenario():
        service, store = await _service(tmp_path, _config(horizon=48, templates=STATIC_TEMPLATES))
        persona_hash = service._identity_for(SESSION)
        store.save_ready(
            _snapshot(
                persona_hash,
                TOMORROW,
                ai=[{"name": "排练", "start": "14:00", "end": "15:00", "state": "在排练"}],
                static=[],  # 快照里的静态副本是空的（生成时还没有晨会）
            ),
            retention_days=30,
            today=TODAY,
        )
        return service.plan_summary(SESSION, now=NOW)

    plan = asyncio.run(scenario()) or ""
    # 只看"明天"那一段：今天的静态兜底也会出现晨会，整体 in 会被它蒙混过去
    tomorrow_part = plan.split("明天", 1)[-1].split("后天", 1)[0]
    assert "14:00-15:00 排练" in tomorrow_part
    assert "08:00-09:00 晨会" in tomorrow_part


def test_daily_loop_prefetches_days_covering_window(tmp_path, monkeypatch):
    """24h 窗口：一轮补 2 天（今天 + 明天），少补计划就会断档。"""

    async def scenario():
        service, _ = await _service(tmp_path, _config(horizon=24))
        seen = []

        async def fake_queue(*, target_date=None):
            seen.append(target_date)
            return 0

        sleeps = []

        async def fake_sleep(_seconds):
            sleeps.append(_seconds)
            if len(sleeps) > 1:
                raise asyncio.CancelledError

        monkeypatch.setattr(service, "queue_known_profiles", fake_queue)
        service._sleep = fake_sleep
        await service.run_daily_loop()
        return seen

    seen = asyncio.run(scenario())
    assert seen == [TODAY, TOMORROW]


def test_horizon_increase_fills_missing_days_once(tmp_path, monkeypatch):
    """窗口调大后要立刻补齐新窗口的天（静默成本：少补=计划断档、反复补=白烧调用）。"""
    config = _config(horizon=24)

    async def scenario():
        service, _ = await _service(tmp_path, config)
        seen = []

        async def fake_queue(*, target_date=None):
            seen.append(target_date)
            return 1

        monkeypatch.setattr(service, "queue_known_profiles", fake_queue)
        service._observed_horizon = 24          # 启动时观察到的窗口
        config["daily_schedule"]["ai_daily"]["plan_horizon_hours"] = 48
        service.watch_plan_horizon()            # 调大 → 补 3 天
        await asyncio.sleep(0)
        filled = list(seen)
        seen.clear()
        service.watch_plan_horizon()            # 窗口未变 → 不再补
        await asyncio.sleep(0)
        return filled, list(seen)

    filled, repeated = asyncio.run(scenario())
    assert filled == [TODAY, TOMORROW, TODAY + datetime.timedelta(days=2)]
    assert repeated == []


def test_scheduled_scan_fills_failed_future_day(tmp_path):
    """定时扫描补未来日程：目标日期还没过，即使上次失败也必须重新生成。"""

    async def scenario():
        service, store = await _service(tmp_path, _config())
        persona_hash = service._identity_for(SESSION)
        store.record_failure(
            persona_hash=persona_hash,
            local_date=TOMORROW,
            failed_at=(NOW - datetime.timedelta(minutes=5)).isoformat(),
            error_type="llm_error",
            retention_days=30,
            today=TODAY,
            timezone="UTC",
            detail="Request timed out.",
        )
        queued = await service.queue_known_profiles(target_date=TOMORROW)
        await service._wait_pending_generations()
        return queued

    assert asyncio.run(scenario()) >= 1


def test_repeat_attempt_is_throttled_then_allowed(tmp_path):
    """同一天重复触发按 30 分钟限速（避免逐条消息打上游），过了窗口就再试。"""

    async def scenario():
        service, _store = await _service(tmp_path, _config())
        first = service.queue_generation(SESSION, target_date=TOMORROW)
        await service._wait_pending_generations()
        second = service.queue_generation(SESSION, target_date=TOMORROW)

        service.time_context._now = NOW + datetime.timedelta(
            minutes=GENERATION_RETRY_GAP_MINUTES + 1
        )
        third = service.queue_generation(SESSION, target_date=TOMORROW)
        await service._wait_pending_generations()
        return first, second, third

    first, second, third = asyncio.run(scenario())
    assert first is True
    assert second is False  # 限速窗口内不再打上游
    assert third is True  # 过窗后重新尝试


def test_past_date_is_not_generated(tmp_path):
    """过去的日期没有补生成的意义：直接跳过。"""

    async def scenario():
        service, _store = await _service(tmp_path, _config())
        yesterday = TODAY - datetime.timedelta(days=1)
        return service.queue_generation(SESSION, target_date=yesterday)

    assert asyncio.run(scenario()) is False


def test_scan_targets_follow_generation_mode(tmp_path):
    """扫描区间 = 触发目标日起铺满预生成天数：``-HH:MM`` 从明天起（今天由昨天那轮负责）。"""

    async def scenario(generation_time):
        config = _config()
        config["daily_schedule"]["ai_daily"]["generation_time"] = generation_time
        suffix = generation_time.replace(":", "").replace("-", "m")
        service, _store = await _service(tmp_path / suffix, config)
        return service._scan_target_dates(NOW)

    prefetch = asyncio.run(scenario("-23:30"))
    same_day = asyncio.run(scenario("00:05"))
    assert prefetch[0] == TOMORROW
    assert same_day[0] == TODAY
    assert len(prefetch) == len(same_day)


def test_today_is_generated_after_failure(tmp_path):
    """今天还没过完：即使今天已有一笔失败记录，消息触发也要重新排队。"""

    async def scenario():
        service, store = await _service(tmp_path, _config())
        persona_hash = service._identity_for(SESSION)
        store.record_failure(
            persona_hash=persona_hash,
            local_date=TODAY,
            failed_at=(NOW - datetime.timedelta(minutes=5)).isoformat(),
            error_type="llm_error",
            retention_days=30,
            today=TODAY,
            timezone="UTC",
        )
        queued = service.queue_generation(SESSION, target_date=TODAY)
        await service._wait_pending_generations()
        return queued

    assert asyncio.run(scenario()) is True


def test_scan_ignores_message_throttle(tmp_path):
    """定时扫描经真实入口（queue_known_profiles）也不受消息路径限速约束。"""

    async def scenario():
        service, _store = await _service(tmp_path, _config(horizon=48))
        from_message = service.queue_generation(SESSION, target_date=TODAY)
        await service._wait_pending_generations()
        # 同一目标日、几分钟内：消息路径已受限速，扫描仍须照排
        from_scan = await service.queue_known_profiles(target_date=TODAY)
        await service._wait_pending_generations()
        return from_message, from_scan

    from_message, from_scan = asyncio.run(scenario())
    assert from_message is True
    assert from_scan >= 1


def test_repeat_message_trigger_is_throttled(tmp_path):
    """消息路径反复触发同一天时受限速保护，不会每条消息都打上游。"""

    async def scenario():
        service, _store = await _service(tmp_path, _config())
        first = service.queue_generation(SESSION, target_date=TODAY)
        await service._wait_pending_generations()
        second = service.queue_generation(SESSION, target_date=TODAY)
        return first, second

    first, second = asyncio.run(scenario())
    assert first is True
    assert second is False


def test_message_entry_is_throttled_and_targets_today(tmp_path):
    """真实消息入口（register_session_async）只补今天，且连续两次只打一次上游。"""

    async def scenario():
        service, _store = await _service(tmp_path, _config())
        first = await service.register_session_async(SESSION, trigger=True)
        await service._wait_pending_generations()
        keys_after_first = len(service._attempt_at)
        second = await service.register_session_async(SESSION, trigger=True)
        tasks_before_wait = dict(service._tasks)
        await service._wait_pending_generations()
        return first, second, keys_after_first, tasks_before_wait, service

    first, second, keys_after_first, tasks_before_wait, service = asyncio.run(scenario())
    assert first and second  # 两次都注册成功
    assert keys_after_first == 1  # target_date 恒为今天
    assert tasks_before_wait == {}  # 第二次被限速，没有新任务
    assert list(service._attempt_at) == [f"{service._identity_for(SESSION)}:"
                                        f"{TODAY.isoformat()}:UTC"]


def test_daily_loop_survives_exception(tmp_path):
    """定时扫描抛一次异常后循环必须继续：否则窗口永久不补，且只有日志能看出来。"""

    async def scenario():
        service, _store = await _service(tmp_path, _config())
        sleeps = []
        calls = {"scan": 0}

        async def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 4:
                raise asyncio.CancelledError()

        async def flaky_scan(*, target_date=None):
            calls["scan"] += 1
            if calls["scan"] == 1:
                raise RuntimeError("boom")
            return 0

        service._sleep = fake_sleep
        service.queue_known_profiles = flaky_scan
        try:
            await service.run_daily_loop()
        except asyncio.CancelledError:
            pass
        return calls["scan"], sleeps

    scans, sleeps = asyncio.run(scenario())
    assert scans >= 2  # 异常后进入下一轮
    assert 60.0 in sleeps  # 异常后短暂退避再继续
