"""P1 时区标准化 / Scheduler 终态清理 / 日期锁回收 / 迟到 0 语义聚焦测试。

覆盖范围：
- 时区 key：ZoneInfo → IANA；固定 offset → UTC±HH:MM 稳定回退键；UTC 别名 → UTC；
  naive → system-local；绝不把 CST 之类歧义缩写当 IANA 存储。
- WebUI：app.js 的时区安全 helper（safeTimeZone / fixedOffsetMinutes /
  timezoneDisplayLabel / formatInTimeZone）——固定偏移不能传给 Intl，无效时区不抛
  RangeError，面向用户显示 UTC+x（node 可用时执行，否则 skip）。
- 迟到语义：reminder_max_late_minutes=0 表示「不检查迟到」，不得再被 or 60/钳到 1；
  正数保持既有超窗过期语义。
- Scheduler 终态任务按 runtime.state_retention_days 回收，不误删活动任务。
- daily schedule 日期锁按 state_retention_days 回收未占用锁，不删持锁/新锁。
"""

import asyncio
import datetime
import shutil
import subprocess
import textwrap

import pytest
import yaml
from zoneinfo import ZoneInfo

from time_awareness.core.daily_schedule_store import DailyScheduleSnapshotStore
from time_awareness.core.scheduler import (
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PROCESSING,
    STATUS_RETRY_WAIT,
    STATUS_SENT,
    ReminderScheduler,
    TriggerResult,
)
from time_awareness.services.calendar_runtime_service import CalendarRuntimeService
from time_awareness.services.daily_schedule_service import DailyScheduleService


UTC = datetime.timezone.utc
SESSION = "demo:GroupMessage:group_demo"


# ==================== Scheduler 构造辅助 ====================


def _scheduler(tmp_path, now, *, max_late_minutes=60):
    scheduler = ReminderScheduler(
        str(tmp_path),
        lambda group: None,
        now_provider=lambda: now,
    )
    scheduler.set_max_late_minutes(max_late_minutes)
    return scheduler


# ==================== 1. 时区 key 标准化（后端存储键） ====================


def test_timezone_key_iana_kept():
    now = datetime.datetime(2026, 8, 11, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert DailyScheduleService._timezone_key(now) == "Asia/Shanghai"


def test_timezone_key_fixed_offset_normalized():
    now = datetime.datetime(
        2026, 8, 11, 12, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=8))
    )
    assert DailyScheduleService._timezone_key(now) == "UTC+08:00"
    now_minutes = datetime.datetime(
        2026, 8, 11, 12, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    )
    assert DailyScheduleService._timezone_key(now_minutes) == "UTC+05:30"
    now_neg = datetime.datetime(
        2026, 8, 11, 12, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=-3))
    )
    assert DailyScheduleService._timezone_key(now_neg) == "UTC-03:00"


def test_timezone_key_utc_aliases_normalized():
    assert (
        DailyScheduleService._timezone_key(
            datetime.datetime(2026, 8, 11, 12, 0, tzinfo=datetime.timezone.utc)
        )
        == "UTC"
    )
    assert (
        DailyScheduleService._timezone_key(
            datetime.datetime(2026, 8, 11, 12, 0, tzinfo=ZoneInfo("Etc/UTC"))
        )
        == "UTC"
    )


def test_timezone_key_naive_falls_back_to_system_local():
    assert (
        DailyScheduleService._timezone_key(datetime.datetime(2026, 8, 11, 12, 0))
        == "system-local"
    )


def test_timezone_key_does_not_store_ambiguous_abbrev_as_iana():
    """CST 之类歧义缩写不得被当作 IANA key 存储（回退稳定键）。"""
    # 模拟一个 tzinfo 的 str 是歧义缩写（防御：真实 datetime 库不会这样）
    class FakeTz(datetime.tzinfo):
        def utcoffset(self, dt):
            return datetime.timedelta(hours=8)

        def tzname(self, dt):
            return "CST"

        def __str__(self):
            return "CST"

    key = DailyScheduleService._timezone_key(
        datetime.datetime(2026, 8, 11, 12, 0, tzinfo=FakeTz())
    )
    assert key != "CST"
    assert key == "system-local"


def test_state_retention_days_default_and_clamp(tmp_path):
    """无 runtime.state_retention_days 时默认 7；非法/越界回退并钳制。"""
    now = datetime.datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
    scheduler = _scheduler(tmp_path, now)
    assert scheduler._state_retention_days == 7
    scheduler.set_state_retention_days(400)
    assert scheduler._state_retention_days == 365
    scheduler.set_state_retention_days(0)
    assert scheduler._state_retention_days == 1
    scheduler.set_state_retention_days("abc")
    assert scheduler._state_retention_days == 7


# ==================== 2. 迟到语义：0 = 不检查迟到 ====================


def test_max_late_zero_not_clamped_to_one(tmp_path):
    now = datetime.datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
    scheduler = _scheduler(tmp_path, now, max_late_minutes=0)
    assert scheduler._max_late_minutes_value == 0


def test_max_late_zero_pending_past_due_is_claimed_not_expired(tmp_path):
    """0 模式：很久以前到期的 pending 任务不过期，仍正常 claim 发送。"""
    now = datetime.datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
    scheduler = _scheduler(tmp_path, now, max_late_minutes=0)
    task = scheduler.add_user_task(
        SESSION, now - datetime.timedelta(hours=5), "离线很久的提醒"
    )
    assert scheduler._cleanup_expired(now) == 0
    assert task.status == STATUS_PENDING

    groups = scheduler._claim_due(now)
    assert len(groups) == 1
    assert groups[0].tasks == [task]
    assert task.status == STATUS_PROCESSING


def test_max_late_zero_retry_wait_past_due_still_claimed(tmp_path):
    now = datetime.datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
    scheduler = _scheduler(tmp_path, now, max_late_minutes=0)
    task = scheduler.add_user_task(
        SESSION, now - datetime.timedelta(hours=3), "重试任务"
    )
    task.status = STATUS_RETRY_WAIT
    task.next_retry_at = now - datetime.timedelta(seconds=5)
    task.attempts = 1

    groups = scheduler._claim_due(now)
    assert len(groups) == 1
    assert task.status == STATUS_PROCESSING


def test_max_late_zero_load_processing_recovers_to_retry_wait(tmp_path):
    """0 模式：processing 崩溃恢复不受迟到窗口约束，恢复为可重试任务。"""
    now = datetime.datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
    fire_at = now - datetime.timedelta(hours=6)
    (tmp_path / "reminder_tasks.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "last_update": now.isoformat(),
                "tasks": [
                    {
                        "id": "proc-1",
                        "kind": "user",
                        "session": SESSION,
                        "fire_at": fire_at.isoformat(),
                        "status": "processing",
                        "attempts": 1,
                        "processing_started_at": now.isoformat(),
                        "created_at": fire_at.isoformat(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    scheduler = _scheduler(tmp_path, now, max_late_minutes=0)
    scheduler.load()
    task = next(t for t in scheduler._tasks if t.id == "proc-1")
    assert task.status == STATUS_RETRY_WAIT
    assert task.next_retry_at is not None
    assert task.terminal_at is None


def test_max_late_zero_recover_processing_retries_not_expires(tmp_path):
    now = datetime.datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
    scheduler = _scheduler(tmp_path, now, max_late_minutes=0)
    task = scheduler.add_user_task(
        SESSION, now - datetime.timedelta(hours=4), "取消恢复"
    )
    group = scheduler._claim_due(now)[0]  # → processing
    scheduler._recover_processing(group, "调度器被取消")
    assert task.status == STATUS_RETRY_WAIT
    assert task.terminal_at is None


def test_positive_late_semantics_preserved(tmp_path):
    """正数（如 60）保持既有行为：超窗 pending/retry_wait 仍进 EXPIRED。"""
    now = datetime.datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
    scheduler = _scheduler(tmp_path, now, max_late_minutes=60)
    task = scheduler.add_user_task(
        SESSION, now - datetime.timedelta(minutes=90), "超窗"
    )
    assert scheduler._cleanup_expired(now) == 1
    assert task.status == STATUS_EXPIRED
    assert task.terminal_at is not None


# ==================== 3. Scheduler 终态任务回收 ====================


def test_terminal_tasks_cleaned_after_retention_days(tmp_path):
    now = datetime.datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
    scheduler = _scheduler(tmp_path, now)
    scheduler.set_state_retention_days(7)

    old_sent = scheduler.add_user_task(SESSION, now - datetime.timedelta(days=10), "旧")
    old_sent.status = STATUS_SENT
    old_sent.terminal_at = now - datetime.timedelta(days=10)

    recent_failed = scheduler.add_user_task(SESSION, now - datetime.timedelta(days=3), "新")
    recent_failed.status = STATUS_FAILED
    recent_failed.terminal_at = now - datetime.timedelta(days=3)

    active = scheduler.add_user_task(SESSION, now, "活动")

    removed = scheduler._cleanup_terminal_tasks(now)
    assert removed == 1
    assert old_sent not in scheduler._tasks
    assert recent_failed in scheduler._tasks
    assert active in scheduler._tasks


def test_terminal_tasks_without_terminal_at_fallback_to_created_at(tmp_path):
    """旧格式无 terminal_at：回退 created_at（再退 fire_at）判断保留期。"""
    now = datetime.datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
    scheduler = _scheduler(tmp_path, now)
    scheduler.set_state_retention_days(7)

    old = scheduler.add_user_task(SESSION, now - datetime.timedelta(days=20), "旧")
    old.status = STATUS_CANCELLED
    old.terminal_at = None
    old.created_at = now - datetime.timedelta(days=20)

    removed = scheduler._cleanup_terminal_tasks(now)
    assert removed == 1
    assert old not in scheduler._tasks


def test_active_tasks_never_cleaned_by_retention(tmp_path):
    now = datetime.datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
    scheduler = _scheduler(tmp_path, now)
    scheduler.set_state_retention_days(7)

    pending = scheduler.add_user_task(SESSION, now, "pending")
    retry = scheduler.add_user_task(SESSION, now, "retry")
    retry.status = STATUS_RETRY_WAIT
    retry.next_retry_at = now
    processing = scheduler.add_user_task(SESSION, now, "processing")
    processing.status = STATUS_PROCESSING

    assert scheduler._cleanup_terminal_tasks(now) == 0
    assert pending in scheduler._tasks
    assert retry in scheduler._tasks
    assert processing in scheduler._tasks


def test_terminal_at_persisted_roundtrip(tmp_path):
    """terminal_at 落盘后能完整读回。"""
    now = datetime.datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
    scheduler = _scheduler(tmp_path, now)
    task = scheduler.add_user_task(SESSION, now, "测试")
    task.status = STATUS_SENT
    task.terminal_at = now
    scheduler.save()

    restored = ReminderScheduler(str(tmp_path), lambda g: None, now_provider=lambda: now)
    restored.load()
    assert restored._tasks == []  # sent 不落盘

    # 直接用字典层验证 failed 落盘含 terminal_at
    data = yaml.safe_load((tmp_path / "reminder_tasks.yaml").read_text(encoding="utf-8"))
    task2 = scheduler.add_user_task(SESSION, now, "测试2")
    task2.status = STATUS_FAILED
    task2.terminal_at = now
    scheduler.save()
    data = yaml.safe_load((tmp_path / "reminder_tasks.yaml").read_text(encoding="utf-8"))
    saved = next(t for t in data["tasks"] if t["id"] == task2.id)
    assert saved["status"] == "failed"
    assert saved["terminal_at"] == now.isoformat()


# ==================== 4. daily schedule 日期锁回收 ====================


class _FakeTimeContext:
    def __init__(self, now):
        self._now = now

    def now(self):
        return self._now


class _FakePersonaResolver:
    async def __call__(self, context, session, event=None):
        return "persona_demo", None


def _schedule_service(tmp_path, now):
    store = DailyScheduleSnapshotStore(str(tmp_path))
    assert store.load()
    service = DailyScheduleService(
        context=object(),
        config={
            "runtime": {"state_retention_days": 7},
            "daily_schedule": {
                "ai_daily": {"enabled": True, "retention_days": 30},
            },
        },
        store=store,
        time_context=_FakeTimeContext(now),
        persona_resolver=_FakePersonaResolver(),
    )
    return service


def test_stale_unheld_locks_cleaned_held_and_fresh_kept(tmp_path):
    now = datetime.datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
    service = _schedule_service(tmp_path, now)

    stale_unheld = DailyScheduleSnapshotStore.snapshot_key("persona_a", "2026-07-20", "UTC")
    stale_held = DailyScheduleSnapshotStore.snapshot_key("persona_b", "2026-07-21", "UTC")
    within = DailyScheduleSnapshotStore.snapshot_key("persona_c", "2026-08-08", "UTC")
    today = DailyScheduleSnapshotStore.snapshot_key("persona_d", "2026-08-11", "UTC")
    for key in (stale_unheld, stale_held, within, today):
        service._locks[key] = asyncio.Lock()

    async def _hold():
        await service._locks[stale_held].acquire()

    asyncio.run(_hold())

    removed = service._cleanup_stale_locks()
    assert removed == 1
    assert stale_unheld not in service._locks  # 过期未占用 → 回收
    assert stale_held in service._locks        # 过期但被持有 → 保留
    assert within in service._locks            # 保留期内 → 保留
    assert today in service._locks             # 当日 → 保留


def test_lock_cleanup_respects_config_retention(tmp_path):
    """保留期来自 runtime.state_retention_days；缺省/非法回退 7。"""
    now = datetime.datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
    service = _schedule_service(tmp_path, now)
    assert service.state_retention_days() == 7

    # 配置非法字符串 → 回退 7
    service.config["runtime"]["state_retention_days"] = "abc"
    assert service.state_retention_days() == 7
    # 无 runtime 键 → 默认 7
    service.config["runtime"] = {}
    assert service.state_retention_days() == 7
    # 越界 → 钳制
    service.config["runtime"] = {"state_retention_days": 999}
    assert service.state_retention_days() == 365


def test_lock_key_with_fixed_offset_tz_still_parsed(tmp_path):
    """锁键时区段含冒号（UTC+08:00）也能正确解析出日期。"""
    now = datetime.datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
    service = _schedule_service(tmp_path, now)
    key = DailyScheduleSnapshotStore.snapshot_key("persona_x", "2026-07-01", "UTC+08:00")
    service._locks[key] = asyncio.Lock()
    removed = service._cleanup_stale_locks()
    assert removed == 1
    assert key not in service._locks


# ==================== 5. CalendarRuntimeService 迟到 0 ====================


class _FakeBuiltinManager:
    def ensure_fresh(self, year, categories):
        return 1

    def load_raw(self):
        return {"events": []}

    def is_stale(self, year, categories):
        return False


class _FakeTimeContextRuntime:
    def __init__(self, now_provider):
        self._now_provider = now_provider

    def now(self):
        return self._now_provider()

    def enabled_builtin_categories(self):
        return []


def _runtime(config, tmp_path, current):
    scheduler = ReminderScheduler(
        str(tmp_path),
        lambda group: None,
        now_provider=lambda: current[0],
    )
    scheduler.load()
    runtime = CalendarRuntimeService(
        config=config,
        calendar_store=object(),
        builtin_manager=_FakeBuiltinManager(),
        scheduler=scheduler,
        time_context=_FakeTimeContextRuntime(lambda: current[0]),
        targets_provider=lambda: ["platform_demo:PrivateMessage:10001"],
    )
    return runtime, scheduler


def test_state_retention_zero_clamped_consistently(tmp_path):
    """0 按 schema 下界钳为 1；仅缺失/None 使用默认 7。"""
    now = datetime.datetime(2026, 8, 11, 8, 0, tzinfo=UTC)

    schedule_service = _schedule_service(tmp_path / "daily", now)
    schedule_service.config["runtime"] = {"state_retention_days": 0}
    assert schedule_service.state_retention_days() == 1
    schedule_service.config["runtime"] = {"state_retention_days": None}
    assert schedule_service.state_retention_days() == 7
    schedule_service.config["runtime"] = {}
    assert schedule_service.state_retention_days() == 7

    current = [now]
    runtime, scheduler = _runtime(
        {"runtime": {"state_retention_days": 0}},
        tmp_path / "scheduler",
        current,
    )
    runtime.apply_reminder_config()
    assert scheduler._state_retention_days == 1

    runtime.config["runtime"]["state_retention_days"] = None
    runtime.apply_reminder_config()
    assert scheduler._state_retention_days == 7


def test_apply_reminder_config_zero_late_not_swallowed(tmp_path):
    """reminder_max_late_minutes=0 不得被 `or 60` 吞成 60。"""
    current = [datetime.datetime(2026, 8, 11, 8, 0, tzinfo=UTC)]
    runtime, scheduler = _runtime(
        {"reminder": {"reminder_max_late_minutes": 0}}, tmp_path, current
    )
    runtime.apply_reminder_config()
    assert scheduler._max_late_minutes_value == 0


def test_apply_reminder_config_positive_late_unchanged(tmp_path):
    current = [datetime.datetime(2026, 8, 11, 8, 0, tzinfo=UTC)]
    runtime, scheduler = _runtime(
        {"reminder": {"reminder_max_late_minutes": 45}}, tmp_path, current
    )
    runtime.apply_reminder_config()
    assert scheduler._max_late_minutes_value == 45


def test_scan_enqueue_zero_late_still_enqueues_past_fire(tmp_path):
    """max_late=0：提醒时刻早已过去也照常入队（不按窗口丢弃）。"""
    current = [datetime.datetime(2026, 8, 11, 12, 0, tzinfo=UTC)]  # 12:00，reminder_time 09:00
    runtime, scheduler = _runtime(
        {
            "reminder": {
                "enable_reminder": True,
                "reminder_time": "09:00",
                "reminder_max_late_minutes": 0,
            },
            "calendar": {"enable_calendar": True},
        },
        tmp_path,
        current,
    )
    assert runtime.scan_and_enqueue_today_events() == 1
    assert len(scheduler.all_pending()) == 1


def test_scan_enqueue_positive_late_skips_when_past_window(tmp_path):
    """正数语义保留：超窗（跨日漂移）时仍跳过入队。"""
    current = [datetime.datetime(2026, 8, 11, 12, 0, tzinfo=UTC)]
    runtime, scheduler = _runtime(
        {
            "reminder": {
                "enable_reminder": True,
                "reminder_time": "09:00",
                "reminder_max_late_minutes": 60,
            },
            "calendar": {"enable_calendar": True},
        },
        tmp_path,
        current,
    )
    assert runtime.scan_and_enqueue_today_events() == 0
    assert scheduler.all_pending() == []


# ==================== 6. WebUI 时区安全 helper（node 可用时） ====================

_WEBUI_JS = None


def _load_app_js():
    global _WEBUI_JS
    if _WEBUI_JS is None:
        import pathlib

        _WEBUI_JS = pathlib.Path(__file__).resolve().parent.parent / "pages" / "webui" / "app.js"
        if not _WEBUI_JS.exists():
            pytest.skip("pages/webui/app.js 不存在")
    return str(_WEBUI_JS)


@pytest.mark.skipif(shutil.which("node") is None, reason="node 不可用")
def test_webui_timezone_helpers_safe_and_correct():
    """提取 app.js 时区安全 helper 块，用 node 验证：
    - 固定偏移/歧义缩写不被当作 IANA；无效时区不抛 RangeError；
    - 固定偏移格式化为等价墙钟；面向用户显示 UTC+x。"""
    app_js = _load_app_js()
    script = textwrap.dedent(
        """
        const fs = require('fs');
        const src = fs.readFileSync(process.argv[1], 'utf8');
        const start = src.indexOf('// ==================== 时区安全工具');
        const end = src.indexOf('let scheduleClockTimer = null;', start);
        if (start < 0 || end < 0) throw new Error('helper block not found');
        const block = src.slice(start, end);
        const fn = new Function(block + `\\n; return { safeTimeZone, fixedOffsetMinutes,
          currentOffsetMinutes, formatOffsetLabel, timezoneDisplayLabel,
          formatInTimeZone, formatToPartsInTimeZone };`);
        const h = fn();
        const ok = (cond) => { if (!cond) throw new Error('assertion failed'); };

        // safeTimeZone：IANA 通过，固定偏移/歧义缩写拒绝
        ok(h.safeTimeZone('Asia/Shanghai') === 'Asia/Shanghai');
        ok(h.safeTimeZone('UTC+08:00') === undefined);
        ok(h.safeTimeZone('CST') === undefined);
        ok(h.safeTimeZone('system-local') === undefined);

        // fixedOffsetMinutes
        ok(h.fixedOffsetMinutes('UTC+08:00') === 480);
        ok(h.fixedOffsetMinutes('UTC+05:30') === 330);
        ok(h.fixedOffsetMinutes('UTC-03:00') === -180);
        ok(h.fixedOffsetMinutes('Asia/Shanghai') === null);

        // 面向用户显示 UTC+x
        ok(h.timezoneDisplayLabel('Asia/Shanghai') === 'UTC+8');
        ok(h.timezoneDisplayLabel('UTC+08:00') === 'UTC+8');
        ok(h.timezoneDisplayLabel('system-local') === '系统本地');
        ok(h.timezoneDisplayLabel('') === '');

        // 固定偏移格式化 = 等价墙钟；无效时区回退不抛错
        const date = new Date(Date.UTC(2026, 7, 11, 0, 0, 0));
        const opts = { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' };
        ok(h.formatInTimeZone(date, 'UTC+08:00', 'en-GB', opts) === '08:00');
        ok(h.formatInTimeZone(date, 'Asia/Shanghai', 'en-GB', opts) === '08:00');
        ok(typeof h.formatInTimeZone(date, 'CST', 'en-GB', opts) === 'string');

        // formatToParts 固定偏移小时数正确
        const parts = h.formatToPartsInTimeZone(date, 'UTC+08:00', 'en-GB', opts);
        ok(Number(parts.find(p => p.type === 'hour')?.value || 0) === 8);

        // currentOffsetMinutes
        ok(h.currentOffsetMinutes('Asia/Shanghai') === 480);
        ok(h.currentOffsetMinutes('UTC') === 0);
        ok(h.currentOffsetMinutes('CST') === null);

        console.log('JS_ASSERT_OK');
        """
    )
    proc = subprocess.run(
        ["node", "-e", script, app_js],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"node 断言失败:\n{proc.stdout}\n{proc.stderr}"
    assert "JS_ASSERT_OK" in proc.stdout
