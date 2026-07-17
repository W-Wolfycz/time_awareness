import datetime

import yaml

from time_awareness.core.scheduler import (
    DueGroup,
    ReminderScheduler,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RETRY_WAIT,
    STATUS_SENT,
    Task,
    TriggerResult,
)


UTC = datetime.timezone.utc


def test_v1_task_migrates_to_v2_and_normalizes_utc():
    raw = {
        "id": "demo-task",
        "kind": "user",
        "session": "group_demo",
        "fire_at": "2026-07-16T12:00:00",
        "hint": "演示提醒",
        "sent": False,
        "given_up": False,
    }
    task = Task.from_dict(raw, default_tz=datetime.timezone(datetime.timedelta(hours=8)))
    assert task is not None
    assert task.status == STATUS_PENDING
    assert task.fire_at == datetime.datetime(2026, 7, 16, 4, 0, tzinfo=UTC)


def test_task_serialization_keeps_status_and_retry_fields():
    task = Task(
        id="demo-task",
        kind="user",
        session="group_demo",
        fire_at=datetime.datetime(2026, 7, 16, 4, 0, tzinfo=UTC),
        status=STATUS_RETRY_WAIT,
        attempts=1,
        next_retry_at=datetime.datetime(2026, 7, 16, 4, 1, tzinfo=UTC),
        delivery_started=True,
    )
    restored = Task.from_dict(task.to_dict())
    assert restored is not None
    assert restored.status == STATUS_RETRY_WAIT
    assert restored.attempts == 1
    assert restored.next_retry_at == datetime.datetime(2026, 7, 16, 4, 1, tzinfo=UTC)
    assert restored.delivery_started is True


def test_trigger_result_distinguishes_safe_retry_from_delivery_failure():
    safe = TriggerResult.fail("provider unavailable", retryable=True)
    delivery = TriggerResult.fail("decorator may have sent earlier segments", retryable=False)
    assert safe.retryable is True
    assert delivery.retryable is False
    assert STATUS_SENT != STATUS_FAILED


def test_scheduler_claims_only_tasks_inside_merge_window(tmp_path):
    now = datetime.datetime(2026, 7, 16, 4, 10, tzinfo=UTC)
    scheduler = ReminderScheduler(
        str(tmp_path),
        lambda group: None,
        now_provider=lambda: now,
    )
    first = scheduler.add_user_task(
        "demo:GroupMessage:group_demo",
        now - datetime.timedelta(minutes=2),
        "first",
    )
    second = scheduler.add_user_task(
        "demo:GroupMessage:group_demo",
        now - datetime.timedelta(seconds=90),
        "second",
    )
    third = scheduler.add_user_task(
        "demo:GroupMessage:group_demo",
        now - datetime.timedelta(seconds=10),
        "third",
    )
    groups = scheduler._claim_due(now)
    assert [[task.id for task in group.tasks] for group in groups] == [
        [first.id, second.id],
        [third.id],
    ]


def test_scheduler_retries_safe_failure_and_stops_on_delivery_failure(tmp_path):
    now = datetime.datetime(2026, 7, 16, 4, 10, tzinfo=UTC)
    scheduler = ReminderScheduler(
        str(tmp_path),
        lambda group: None,
        now_provider=lambda: now,
    )
    retry_task = scheduler.add_user_task(
        "demo:GroupMessage:group_demo",
        now,
        "retry",
    )
    retry_group = scheduler._claim_due(now)[0]
    scheduler._finish_group(
        retry_group,
        TriggerResult.fail("provider unavailable", retryable=True),
        now,
    )
    assert retry_task.status == STATUS_RETRY_WAIT
    assert retry_task.next_retry_at == now + datetime.timedelta(minutes=1)

    failed_task = scheduler.add_user_task(
        "demo:GroupMessage:group_demo",
        now,
        "delivery",
    )
    failed_group = scheduler._claim_due(now)[0]
    scheduler._finish_group(
        failed_group,
        TriggerResult.fail("delivery ambiguous", retryable=False),
        now,
    )
    assert failed_task.status == STATUS_FAILED


def test_interrupted_delivery_is_not_retried_after_restart(tmp_path):
    now = datetime.datetime(2026, 7, 16, 4, 10, tzinfo=UTC)
    scheduler = ReminderScheduler(
        str(tmp_path),
        lambda group: None,
        now_provider=lambda: now,
    )
    task = scheduler.add_user_task(
        "demo:GroupMessage:group_demo",
        now,
        "delivery",
    )
    group = scheduler._claim_due(now)[0]
    assert scheduler.mark_delivery_started(group.tasks) is True

    restored_scheduler = ReminderScheduler(
        str(tmp_path),
        lambda group: None,
        now_provider=lambda: now,
    )
    restored_scheduler.load()
    restored = next(item for item in restored_scheduler._tasks if item.id == task.id)
    assert restored.status == STATUS_FAILED


def test_v1_file_is_rewritten_as_v2_utc(tmp_path):
    task_path = tmp_path / "reminder_tasks.yaml"
    task_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "tasks": [
                    {
                        "id": "legacy-demo",
                        "kind": "user",
                        "session": "demo:GroupMessage:group_demo",
                        "fire_at": "2026-07-16T12:00:00",
                        "hint": "演示提醒",
                    }
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    local_now = datetime.datetime(
        2026,
        7,
        16,
        10,
        0,
        tzinfo=datetime.timezone(datetime.timedelta(hours=8)),
    )
    scheduler = ReminderScheduler(
        str(tmp_path),
        lambda group: None,
        now_provider=lambda: local_now,
    )
    scheduler.load()
    migrated = yaml.safe_load(task_path.read_text(encoding="utf-8"))
    assert migrated["version"] == 2
    assert migrated["tasks"][0]["fire_at"] == "2026-07-16T04:00:00+00:00"


def test_timezone_change_does_not_break_existing_task_comparisons(tmp_path):
    current = [
        datetime.datetime(
            2026,
            7,
            16,
            12,
            0,
            tzinfo=datetime.timezone(datetime.timedelta(hours=8)),
        )
    ]
    scheduler = ReminderScheduler(
        str(tmp_path),
        lambda group: None,
        now_provider=lambda: current[0],
    )
    task = scheduler.add_user_task(
        "demo:GroupMessage:group_demo",
        current[0] + datetime.timedelta(minutes=10),
        "演示提醒",
    )
    current[0] = datetime.datetime(2026, 7, 16, 4, 10, tzinfo=UTC)
    groups = scheduler._claim_due(current[0])
    assert task.fire_at == current[0]
    assert groups[0].tasks == [task]


def test_new_task_rolls_back_when_initial_persistence_fails(tmp_path, monkeypatch):
    now = datetime.datetime(2026, 7, 16, 4, 10, tzinfo=UTC)
    scheduler = ReminderScheduler(
        str(tmp_path),
        lambda group: None,
        now_provider=lambda: now,
    )
    monkeypatch.setattr(scheduler, "save", lambda: False)

    try:
        scheduler.add_user_task(
            "demo:GroupMessage:group_demo",
            now + datetime.timedelta(minutes=10),
            "演示提醒",
        )
    except RuntimeError as e:
        assert "持久化失败" in str(e)
    else:
        raise AssertionError("持久化失败时应拒绝创建任务")

    assert scheduler._tasks == []
