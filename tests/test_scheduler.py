"""core/scheduler.py 的队列/持久化/恢复逻辑测试（不启动调度循环）。"""

import datetime

from time_awareness.core._datafile import atomic_write_yaml
from time_awareness.core.scheduler import (
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PROCESSING,
    STATUS_RETRY_WAIT,
    STATUS_SENT,
    ReminderScheduler,
    Task,
)
from time_awareness.tests._shared import NOW


async def _ok_cb(group):
    return True


def _scheduler(tmp_path, now=NOW):
    return ReminderScheduler(str(tmp_path), _ok_cb, now_provider=lambda: now)


def _task(**overrides):
    base = {"id": "t1", "kind": "user", "session": "10001", "fire_at": NOW}
    base.update(overrides)
    return Task(**base)


def _raw(**overrides):
    raw = {
        "id": "p1",
        "kind": "user",
        "session": "10001",
        "fire_at": NOW.isoformat(),
        "created_at": NOW.isoformat(),
        "status": "processing",
        "attempts": 0,
        "delivery_started": False,
    }
    raw.update(overrides)
    return raw


def test_task_round_trip():
    task = _task(
        id="abc",
        kind="followup",
        session="group_demo",
        fire_at=NOW + datetime.timedelta(minutes=30),
        hint="备忘",
        target_user_id="10001",
        status=STATUS_RETRY_WAIT,
        attempts=2,
        next_retry_at=NOW + datetime.timedelta(minutes=5),
        created_at=NOW,
    )
    restored = Task.from_dict(task.to_dict())
    assert restored.id == "abc" and restored.kind == "followup"
    assert restored.session == "group_demo" and restored.fire_at == task.fire_at
    assert restored.hint == "备忘" and restored.attempts == 2
    assert restored.target_user_id == "10001"


def test_task_from_dict_invalid_and_unknown_status():
    assert Task.from_dict({"kind": "user", "session": "s", "fire_at": "x"}) is None
    assert Task.from_dict({"id": "x", "kind": "bad", "session": "s", "fire_at": NOW}) is None
    assert Task.from_dict({"id": "x", "kind": "user", "session": "", "fire_at": NOW}) is None
    assert Task.from_dict({**_raw(), "status": "weird"}).status == STATUS_PENDING


def test_load_missing_and_invalid(tmp_path):
    sched = _scheduler(tmp_path)
    sched.load()
    assert sched._tasks == []
    (tmp_path / "reminder_tasks.yaml").write_text(":\n  - broken [", encoding="utf-8")
    sched.load()
    assert sched._tasks == []


def test_load_recovery(tmp_path):
    now = NOW
    recent = (now - datetime.timedelta(minutes=1)).isoformat()
    _write(tmp_path, [_raw(fire_at=recent)])
    sched = _scheduler(tmp_path, now=now)
    sched.load()
    assert sched._tasks[0].status == STATUS_RETRY_WAIT
    assert sched._tasks[0].next_retry_at is not None

    late = (now - datetime.timedelta(hours=2)).isoformat()
    _write(tmp_path, [_raw(fire_at=late)])
    expired = _scheduler(tmp_path, now=now)
    expired.load()
    assert expired._tasks[0].status == "expired"

    _write(tmp_path, [_raw(fire_at=late)])
    zero = _scheduler(tmp_path, now=now)
    zero.set_max_late_minutes(0)
    zero.load()
    assert zero._tasks[0].status == STATUS_RETRY_WAIT


def _write(tmp_path, tasks):
    atomic_write_yaml(str(tmp_path / "reminder_tasks.yaml"), {"version": 2, "tasks": tasks})


def test_add_and_filter_tasks(tmp_path):
    sched = _scheduler(tmp_path)
    fire = NOW + datetime.timedelta(minutes=10)
    cal = sched.add_calendar_task("10001", fire)
    fup = sched.add_followup_task("10001", fire, "备忘", target_user_id="u1")
    usr = sched.add_user_task("10001", fire, "提示")
    assert [cal.kind, fup.kind, usr.kind] == ["calendar", "followup", "user"]
    assert fup.target_user_id == "u1" and len(sched._tasks) == 3

    sched._tasks = [
        _task(id="a", status=STATUS_PENDING),
        _task(id="b", status=STATUS_RETRY_WAIT),
        _task(id="c", status=STATUS_FAILED),
        _task(id="d", status=STATUS_SENT),
    ]
    assert [t.id for t in sched.all_pending()] == ["a", "b"]
    assert [t.id for t in sched.all_failed()] == ["c"]


def test_cancel_task_branches(tmp_path):
    sched = _scheduler(tmp_path)
    sched._tasks = [
        _task(id="active", status=STATUS_PENDING),
        _task(id="processing", status=STATUS_PROCESSING),
        _task(id="done", status=STATUS_SENT),
    ]
    assert sched.cancel_task("missing") is False
    assert sched.cancel_task("processing") == "processing"
    assert sched.cancel_task("done") == "final"
    assert sched.cancel_task("active") is True
    assert sched._tasks[0].status == "cancelled"


def test_claim_due_groups_and_marks_late(tmp_path):
    sched = _scheduler(tmp_path)
    now = NOW
    base_fire = now - datetime.timedelta(minutes=2)
    sched._tasks = [
        _task(id="a", session="10001", fire_at=base_fire),
        _task(id="b", session="10001", fire_at=base_fire + datetime.timedelta(seconds=5)),
        _task(id="c", session="20002", fire_at=base_fire),
    ]
    groups = sched._claim_due(now)
    assert len(groups) == 2
    grouped = {g.session: sorted(t.id for t in g.tasks) for g in groups}
    assert grouped["10001"] == ["a", "b"] and grouped["20002"] == ["c"]
    late_group = next(g for g in groups if g.session == "10001")
    assert late_group.is_late is True and late_group.late_minutes == 2
    assert all(t.status == STATUS_PROCESSING for t in sched._tasks)


def test_setter_clamping(tmp_path):
    sched = _scheduler(tmp_path)
    sched.set_max_late_minutes(-5)
    assert sched._max_late_minutes_value == 0
    sched.set_max_late_minutes(100)
    assert sched._max_late_minutes_value == 100
    sched.set_max_attempts(0)
    assert sched._max_attempts == 1
    sched.set_max_attempts(50)
    assert sched._max_attempts == 10
    sched.set_state_retention_days(0)
    assert sched.state_retention_days() == 1
    sched.set_state_retention_days(1000)
    assert sched.state_retention_days() == 365
    sched.set_state_retention_days("bad")
    assert sched.state_retention_days() == 7
