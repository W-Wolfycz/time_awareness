"""服务层「取数规则」测试：计划/已过各取哪一层、未来日走哪份静态、一轮补几天。

断言的是规则本身（来源选择 + 已执行排除），用对照场景而不是单一样例：
同一个 now，分别给「有快照」「无快照」「快照里静态副本过期」三种输入。
"""

import asyncio
import datetime

from time_awareness.core.daily_schedule_store import DailyScheduleSnapshotStore
from time_awareness.services.daily_schedule_generation import PROMPT_REVISION
from time_awareness.services.daily_schedule_service import DailyScheduleService
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

