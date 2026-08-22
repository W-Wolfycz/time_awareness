"""P0 Scheduler 核心修复的聚焦测试。

覆盖范围：
- P0-1 retry_wait（及重启恢复/重试路径）不得绕过 max_late_minutes：超窗任务
  必须进入既有的过期终态（EXPIRED），不得被 _claim_due claim/发送。
- P0-2 分组键至少区分 (session, target_user_id)：同 session 不同目标用户的
  个人任务不合并；空目标与非空目标相互隔离；同一目标仍按时间窗口合并。
- 现有状态转换不被破坏：窗口内 pending / retry_wait 仍正常 claim，
  delivery_started 之后重启不自动重发等既有语义保持不变。
"""

import datetime

import yaml

from time_awareness.core.scheduler import (
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PROCESSING,
    STATUS_RETRY_WAIT,
    ReminderScheduler,
    TriggerResult,
)

UTC = datetime.timezone.utc


def _scheduler(tmp_path, now, *, max_late_minutes=60):
    scheduler = ReminderScheduler(
        str(tmp_path),
        lambda group: None,
        now_provider=lambda: now,
    )
    scheduler.set_max_late_minutes(max_late_minutes)
    return scheduler


# ==================== P0-1: retry_wait / 恢复路径不得绕过迟到窗口 ====================


def test_retry_wait_task_past_late_window_is_expired_not_claimed(tmp_path):
    """重试时间已到、但 fire_at 已超迟到窗口：不进 claim，进入 EXPIRED。"""
    now = datetime.datetime(2026, 7, 16, 4, 10, tzinfo=UTC)
    scheduler = _scheduler(tmp_path, now, max_late_minutes=60)
    task = scheduler.add_user_task(
        "demo:GroupMessage:group_demo",
        now - datetime.timedelta(minutes=90),  # 90 分钟前到期
        "重试任务",
    )
    # 模拟第一次尝试失败、已排好重试
    task.status = STATUS_RETRY_WAIT
    task.next_retry_at = now - datetime.timedelta(minutes=1)
    task.attempts = 1

    groups = scheduler._claim_due(now)

    assert groups == []  # 不会被 claim → 不会触发发送回调
    assert task.status == STATUS_EXPIRED
    assert "迟到容忍窗口" in task.last_error
    assert task.next_retry_at is None


def test_retry_wait_past_window_after_restart_is_not_sent(tmp_path):
    """load/restart 形态：任务文件里是 retry_wait，重启后超窗不得补发。"""
    now = datetime.datetime(2026, 7, 16, 4, 10, tzinfo=UTC)
    fire_at = datetime.datetime(2026, 7, 16, 1, 0, tzinfo=UTC)
    (tmp_path / "reminder_tasks.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "last_update": now.isoformat(),
                "tasks": [
                    {
                        "id": "retry-once",
                        "kind": "user",
                        "session": "demo:GroupMessage:group_demo",
                        "fire_at": fire_at.isoformat(),
                        "status": "retry_wait",
                        "attempts": 1,
                        "next_retry_at": now.isoformat(),
                        "created_at": fire_at.isoformat(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    scheduler = _scheduler(tmp_path, now, max_late_minutes=60)
    scheduler.load()
    task = next(t for t in scheduler._tasks if t.id == "retry-once")
    # load 本身不改变 retry_wait 状态（区别于 processing 恢复分支）
    assert task.status == STATUS_RETRY_WAIT

    groups = scheduler._claim_due(now)

    assert groups == []
    assert task.status == STATUS_EXPIRED
    assert "迟到容忍窗口" in task.last_error


def test_retry_wait_within_late_window_is_still_claimed(tmp_path):
    """窗口内的 retry_wait 仍按既有语义正常 claim（不误伤正常重试）。"""
    now = datetime.datetime(2026, 7, 16, 4, 10, tzinfo=UTC)
    scheduler = _scheduler(tmp_path, now, max_late_minutes=60)
    task = scheduler.add_user_task(
        "demo:GroupMessage:group_demo",
        now - datetime.timedelta(minutes=10),  # 10 分钟前，仍在窗口内
        "正常重试",
    )
    task.status = STATUS_RETRY_WAIT
    task.next_retry_at = now - datetime.timedelta(seconds=5)
    task.attempts = 1

    groups = scheduler._claim_due(now)

    assert len(groups) == 1
    assert groups[0].tasks == [task]
    assert task.status == STATUS_PROCESSING
    assert task.attempts == 2


def test_pending_task_past_late_window_is_expired_not_sent(tmp_path):
    """pending 兜底同样受窗口约束：超窗进 EXPIRED，不 claim。"""
    now = datetime.datetime(2026, 7, 16, 4, 10, tzinfo=UTC)
    scheduler = _scheduler(tmp_path, now, max_late_minutes=60)
    task = scheduler.add_user_task(
        "demo:GroupMessage:group_demo",
        now - datetime.timedelta(minutes=120),
        "超窗 pending",
    )
    assert task.status == STATUS_PENDING

    groups = scheduler._claim_due(now)

    assert groups == []
    assert task.status == STATUS_EXPIRED
    assert "迟到容忍窗口" in task.last_error


# ==================== P0-2: 分组键区分 (session, target_user_id) ====================


def test_different_target_users_in_same_session_are_not_merged(tmp_path):
    """同一 session 不同 target_user_id 的个人任务必须分成独立组。"""
    now = datetime.datetime(2026, 7, 16, 4, 10, tzinfo=UTC)
    scheduler = _scheduler(tmp_path, now)
    session = "demo:GroupMessage:group_demo"
    task_a = scheduler.add_user_task(session, now, "给 A", target_user_id="10001")
    task_b = scheduler.add_user_task(session, now, "给 B", target_user_id="10002")

    groups = scheduler._claim_due(now)

    assert len(groups) == 2
    assert {group.tasks[0].id for group in groups} == {task_a.id, task_b.id}
    assert {group.target_user_id for group in groups} == {"10001", "10002"}
    assert all(group.session == session for group in groups)


def test_empty_and_named_target_users_are_isolated(tmp_path):
    """空目标（会话广播）与非空目标必须隔离，不得同组。"""
    now = datetime.datetime(2026, 7, 16, 4, 10, tzinfo=UTC)
    scheduler = _scheduler(tmp_path, now)
    session = "demo:GroupMessage:group_demo"
    broadcast = scheduler.add_user_task(session, now, "广播", target_user_id="")
    personal = scheduler.add_user_task(session, now, "个人", target_user_id="10001")

    groups = scheduler._claim_due(now)

    assert len(groups) == 2
    by_uid = {group.target_user_id: group.tasks[0].id for group in groups}
    assert by_uid[""] == broadcast.id
    assert by_uid["10001"] == personal.id


def test_same_target_user_tasks_still_merge_by_time_window(tmp_path):
    """同一目标用户的任务仍按现有时间窗口合并为一组。"""
    now = datetime.datetime(2026, 7, 16, 4, 10, tzinfo=UTC)
    scheduler = _scheduler(tmp_path, now)
    session = "demo:GroupMessage:group_demo"
    first = scheduler.add_user_task(
        session,
        now - datetime.timedelta(seconds=30),
        "A",
        target_user_id="10001",
    )
    second = scheduler.add_user_task(
        session,
        now,
        "B",
        target_user_id="10001",
    )

    groups = scheduler._claim_due(now)

    assert len(groups) == 1
    assert {task.id for task in groups[0].tasks} == {first.id, second.id}
    assert groups[0].target_user_id == "10001"


def test_same_session_mixed_broadcast_and_personal_groups(tmp_path):
    """混合场景：广播组 + 两个个人组互不合并。"""
    now = datetime.datetime(2026, 7, 16, 4, 10, tzinfo=UTC)
    scheduler = _scheduler(tmp_path, now)
    session = "demo:GroupMessage:group_demo"
    broadcast1 = scheduler.add_user_task(session, now, "广播1", target_user_id="")
    broadcast2 = scheduler.add_user_task(
        session, now - datetime.timedelta(seconds=20), "广播2", target_user_id=""
    )
    personal_a = scheduler.add_user_task(session, now, "A", target_user_id="10001")
    personal_b = scheduler.add_user_task(session, now, "B", target_user_id="10002")

    groups = scheduler._claim_due(now)

    assert len(groups) == 3
    by_uid = {group.target_user_id: {t.id for t in group.tasks} for group in groups}
    assert by_uid[""] == {broadcast1.id, broadcast2.id}  # 广播仍按窗口合并
    assert by_uid["10001"] == {personal_a.id}
    assert by_uid["10002"] == {personal_b.id}


# ==================== 既有状态转换不被破坏 ====================


def test_existing_status_transitions_still_intact(tmp_path):
    """既有核心状态机：pending→processing→sent / retry_wait / failed 不受影响。"""
    now = datetime.datetime(2026, 7, 16, 4, 10, tzinfo=UTC)
    scheduler = _scheduler(tmp_path, now)
    session = "demo:GroupMessage:group_demo"

    # pending → processing → sent
    ok = scheduler.add_user_task(session, now, "成功")
    ok_group = scheduler._claim_due(now)[0]
    assert ok.status == STATUS_PROCESSING
    scheduler._finish_group(ok_group, TriggerResult.ok(), now)
    assert ok.status == "sent"

    # pending → processing → retryable 失败 → retry_wait（未进入发送阶段）
    retry = scheduler.add_user_task(session, now, "可重试")
    retry_group = scheduler._claim_due(now)[0]
    scheduler._finish_group(
        retry_group,
        TriggerResult.fail("provider unavailable", retryable=True),
        now,
    )
    assert retry.status == STATUS_RETRY_WAIT
    assert retry.next_retry_at == now + datetime.timedelta(minutes=1)

    # pending → processing → delivery 失败 → failed（不自动重试）
    failed = scheduler.add_user_task(session, now, "发送失败")
    failed_group = scheduler._claim_due(now)[0]
    assert scheduler.mark_delivery_started(failed_group.tasks) is True
    scheduler._finish_group(
        failed_group,
        TriggerResult.fail("delivery ambiguous", retryable=False),
        now,
    )
    assert failed.status == STATUS_FAILED

    # retry_wait（delivery_started=True）重启后不得重发：进入 FAILED
    task = scheduler.add_user_task(session, now, "部分发送")
    group = scheduler._claim_due(now)[0]
    assert scheduler.mark_delivery_started(group.tasks) is True
    restored = ReminderScheduler(
        str(tmp_path),
        lambda g: None,
        now_provider=lambda: now,
    )
    restored.load()
    reloaded = next(t for t in restored._tasks if t.id == task.id)
    assert reloaded.status == STATUS_FAILED
