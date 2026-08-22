import datetime

import yaml

from time_awareness.core.scheduler import (
    DueGroup,
    ReminderScheduler,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RETRY_WAIT,
    STATUS_SENT,
    Task,
    TriggerResult,
)


UTC = datetime.timezone.utc


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


def test_crash_recovery_respects_late_window(tmp_path):
    """崩溃恢复超过迟到容忍窗口的任务过期丢弃，不深夜补发。"""
    now = datetime.datetime(2026, 7, 16, 4, 10, tzinfo=UTC)
    fire_at = datetime.datetime(2026, 7, 16, 1, 0, tzinfo=UTC)  # 3 小时前
    scheduler = ReminderScheduler(
        str(tmp_path),
        lambda group: None,
        now_provider=lambda: now,
    )
    scheduler.set_max_late_minutes(60)
    # 任务文件里是 processing（进程崩溃时 claim 已持久化）
    (tmp_path / "reminder_tasks.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "last_update": now.isoformat(),
                "tasks": [
                    {
                        "id": "interrupted",
                        "kind": "calendar",
                        "session": "demo",
                        "fire_at": fire_at.isoformat(),
                        "status": "processing",
                        "attempts": 1,
                        "processing_started_at": (
                            now - datetime.timedelta(hours=3)
                        ).isoformat(),
                        "created_at": now.isoformat(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    scheduler.load()
    restored = next(t for t in scheduler._tasks if t.id == "interrupted")
    assert restored.status == "expired"
    assert "迟到容忍窗口" in restored.last_error


def test_failed_task_does_not_block_same_day_enqueue(tmp_path):
    """failed 是终态：同日日历提醒不被永久阻塞（H2）。"""
    now = datetime.datetime(2026, 7, 16, 4, 10, tzinfo=UTC)
    scheduler = ReminderScheduler(
        str(tmp_path),
        lambda group: None,
        now_provider=lambda: now,
    )
    scheduler._tasks = [
        Task(
            id="failed-once",
            kind="calendar",
            session="group_demo",
            fire_at=datetime.datetime(2026, 7, 16, 1, 0, tzinfo=UTC),
            status=STATUS_FAILED,
            attempts=3,
        )
    ]
    assert not scheduler.has_calendar_task_for_session_today("group_demo", datetime.date(2026, 7, 16))
    # 活的任务仍然阻塞
    scheduler._tasks[0].status = STATUS_PENDING
    assert scheduler.has_calendar_task_for_session_today("group_demo", datetime.date(2026, 7, 16))


def test_cancel_task_statuses_and_idempotency(tmp_path):
    """cancel_task：pending 可取消；终态幂等返回 final；processing 不可取消。"""
    now = datetime.datetime(2026, 7, 16, 4, 10, tzinfo=UTC)
    scheduler = ReminderScheduler(
        str(tmp_path),
        lambda group: None,
        now_provider=lambda: now,
    )
    pending_task = scheduler.add_user_task("demo", now + datetime.timedelta(minutes=5), "待取消")
    assert scheduler.cancel_task(pending_task.id) is True
    assert pending_task.status == STATUS_CANCELLED
    # 已终态：重复取消幂等成功
    assert scheduler.cancel_task(pending_task.id) == "final"

    # processing 中（正在发送）不可取消
    processing_task = scheduler.add_user_task("demo", now + datetime.timedelta(minutes=10), "发送中")
    processing_task.status = "processing"
    assert scheduler.cancel_task(processing_task.id) == "processing"

    # 不存在
    assert scheduler.cancel_task("ghost-id") is False


def test_list_terminal_detailed_respects_retention_window(tmp_path):
    """终态历史列表：只含保留期内终态任务，活动任务不混入，倒序输出。"""
    now = datetime.datetime(2026, 7, 16, 4, 10, tzinfo=UTC)
    scheduler = ReminderScheduler(
        str(tmp_path),
        lambda group: None,
        now_provider=lambda: now,
    )
    scheduler.set_state_retention_days(7)

    sent_task = scheduler.add_user_task("demo", now + datetime.timedelta(minutes=5), "已发送")
    sent_task.status = STATUS_SENT
    sent_task.terminal_at = now - datetime.timedelta(days=1)

    failed_task = scheduler.add_user_task("demo", now + datetime.timedelta(minutes=6), "失败任务")
    failed_task.status = STATUS_FAILED
    failed_task.terminal_at = now - datetime.timedelta(days=2)
    failed_task.last_error = "LLM 调用失败"

    old_task = scheduler.add_user_task("demo", now + datetime.timedelta(minutes=7), "过期历史")
    old_task.status = STATUS_CANCELLED
    old_task.terminal_at = now - datetime.timedelta(days=10)

    pending_task = scheduler.add_user_task("demo", now + datetime.timedelta(minutes=8), "待触发")

    finished = scheduler.list_terminal_detailed()
    ids = [item["id"] for item in finished]
    assert ids == [sent_task.id, failed_task.id]  # 按结束时间倒序；超过保留期的 old_task 不返回
    assert pending_task.id not in ids
    failed_item = finished[1]
    assert failed_item["status"] == STATUS_FAILED
    assert failed_item["last_error"] == "LLM 调用失败"
    assert failed_item["terminal_at_iso"]


def test_due_group_normalizes_trigger_result(tmp_path):
    """_normalize_result：False → retryable 失败；None/True → ok。"""
    now = datetime.datetime(2026, 7, 16, 4, 10, tzinfo=UTC)
    scheduler = ReminderScheduler(str(tmp_path), lambda group: None, now_provider=lambda: now)
    assert scheduler._normalize_result(False).retryable is True
    assert scheduler._normalize_result(None).success is True
    assert scheduler._normalize_result(True).success is True
    assert scheduler._normalize_result(TriggerResult.fail("x", retryable=False)).success is False
