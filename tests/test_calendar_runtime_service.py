import datetime

from time_awareness.core.calendar_store import CalendarStore
from time_awareness.core.scheduler import ReminderScheduler
from time_awareness.services.calendar_runtime_service import CalendarRuntimeService


NOW = datetime.datetime(2026, 7, 28, 8, 0, tzinfo=datetime.timezone.utc)


class FakeBuiltinManager:
    def ensure_fresh(self, year, categories):
        return 1

    def load_raw(self):
        return {"events": [{"year": 2026, "month": 7, "day": 28, "text": "演示"}]}

    def is_stale(self, year, categories):
        return False


class FakeTimeContext:
    """now 来自可变的 now_provider（与真实调度器同一时钟源），可推进时间。"""

    def __init__(self, now_provider):
        self._now_provider = now_provider

    def now(self):
        return self._now_provider()

    def enabled_builtin_categories(self):
        return ["traditional"]


def _runtime(config, tmp_path, current):
    """构造使用真实 ReminderScheduler 的运行时；current 是可变时钟源。"""
    scheduler = ReminderScheduler(
        str(tmp_path),
        lambda group: None,
        now_provider=lambda: current[0],
    )
    scheduler.load()
    store = CalendarStore()
    runtime = CalendarRuntimeService(
        config=config,
        calendar_store=store,
        builtin_manager=FakeBuiltinManager(),
        scheduler=scheduler,
        time_context=FakeTimeContext(lambda: current[0]),
        targets_provider=lambda: ["platform_demo:PrivateMessage:10001"],
    )
    return runtime, scheduler, store


def test_runtime_applies_bounds_and_enqueues_once(tmp_path):
    current = [NOW]
    runtime, scheduler, _ = _runtime(
        {
            "reminder": {
                "enable_reminder": True,
                "reminder_time": "09:00",
                "reminder_max_late_minutes": "60",
                "reminder_max_attempts": "99",
            },
            "calendar": {"enable_calendar": True},
        },
        tmp_path,
        current,
    )

    runtime.apply_reminder_config()
    assert scheduler._max_late_minutes_value == 60
    assert scheduler._max_attempts == 10
    assert runtime.scan_and_enqueue_today_events() == 1
    assert scheduler.all_pending()[0].fire_at.hour == 9
    # 真实调度器去重：活任务按插件时区同日判断，不重复入队
    assert runtime.scan_and_enqueue_today_events() == 0


def test_dedup_uses_plugin_tz_not_host_tz(tmp_path):
    """去重必须与 fire_at 同用插件时区：宿主时区与插件时区不一致时不重复入队。

    场景：插件时区 +13:00（如 Pacific/Auckland），宿主系统时区 UTC。
    插件时区 8-10 日 09:00 = UTC 8-9 日 20:00 → UTC 日期 8-9，与插件日期
    8-10 不同——若去重误用宿主时区会把同一天的提醒判为不同日期而重复入队。
    """
    plugin_tz = datetime.timezone(datetime.timedelta(hours=13))
    current = [datetime.datetime(2026, 8, 10, 7, 0, tzinfo=plugin_tz)]
    runtime, scheduler, _ = _runtime(
        {
            "reminder": {
                "enable_reminder": True,
                "reminder_time": "09:00",
                "reminder_max_late_minutes": "60",
            },
            "calendar": {"enable_calendar": True},
        },
        tmp_path,
        current,
    )

    assert runtime.scan_and_enqueue_today_events() == 1
    # 第二次扫描（模拟重启补扫）：宿主 UTC 时区会判为 8-9（不同日期），
    # 插件时区（+13）下 fire_at 是 8-10，必须去重不重复入队。
    assert runtime.scan_and_enqueue_today_events() == 0
    assert len(scheduler.all_pending()) == 1


def test_runtime_initializes_builtin_events_into_shared_store(tmp_path):
    current = [NOW]
    runtime, _, store = _runtime({"reminder": {}}, tmp_path, current)
    runtime.initialize_builtin_events()
    assert len(store.builtin_events) == 1


def test_scan_skips_when_calendar_off_and_no_greeting(tmp_path):
    """日历关闭且未启用每日问候时，日历提醒不入队（避免静默消费）。"""
    current = [NOW]
    runtime, scheduler, _ = _runtime(
        {
            "reminder": {
                "enable_reminder": True,
                "reminder_time": "09:00",
                "daily_greeting_without_events": False,
            },
            "calendar": {"enable_calendar": False},
        },
        tmp_path,
        current,
    )
    assert runtime.scan_and_enqueue_today_events() == 0
    assert scheduler.all_pending() == []


def test_scan_skips_when_reminder_disabled(tmp_path):
    current = [NOW]
    runtime, scheduler, _ = _runtime({"reminder": {}}, tmp_path, current)
    assert runtime.scan_and_enqueue_today_events() == 0
    assert scheduler.all_pending() == []
