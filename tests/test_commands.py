"""commands 应用服务的纯逻辑测试（不依赖 AstrMessageEvent）。"""

from time_awareness.commands.calendar_commands import CalendarCommands
from time_awareness.commands.schedule_commands import ScheduleCommands
from time_awareness.core.calendar_manager import CalendarManager
from time_awareness.core.calendar_store import CalendarStore
from time_awareness.tests._shared import NOW


class _BuiltinManager:
    def load_raw(self):
        return {"events": []}


class _DummyDailySchedule:
    def enabled(self):
        return False

    def register_session(self, session):
        pass

    def get_snapshot_for_session(self, session, now=None):
        return None


def _calendar_commands(tmp_path, store=None):
    store = store or CalendarStore()
    return CalendarCommands(
        context=None,
        config={},
        calendar_store=store,
        calendar_manager=CalendarManager(str(tmp_path), store=store),
        builtin_manager=_BuiltinManager(),
        now_provider=lambda: NOW,
        enabled_builtin_categories=lambda: [],
    )


def _schedule_commands(config):
    return ScheduleCommands(
        context=None,
        config=config,
        daily_schedule_service=_DummyDailySchedule(),
        now_provider=lambda: NOW,
        save_config=lambda: True,
        check_overlap=lambda: None,
    )


def test_help_text_contains_command_names():
    assert "/calendar show" in CalendarCommands.help_text()
    assert "/calendar add" in CalendarCommands.help_text()
    assert "/schedule show" in ScheduleCommands.help_text()
    assert "/schedule static" in ScheduleCommands.help_text()


def test_calendar_show_month(tmp_path):
    store = CalendarStore()
    store.set_events(
        [{"id": "abc12345", "year": 2026, "month": 8, "day": 11, "text": "测试", "repeat": 0}]
    )
    output = _calendar_commands(tmp_path, store).show("2026-08")
    assert "[2026-08 共 1 条]" in output and "#abc12345 08-11 测试" in output


def test_calendar_add_and_delete(tmp_path):
    store = CalendarStore()
    commands = _calendar_commands(tmp_path, store)
    output = commands.add("08-11", "测试事件", "")
    assert "已添加" in output
    assert store.events[0]["month"] == 8 and store.events[0]["year"] == 2026
    assert "已删除" in commands.delete(store.events[0]["id"])
    assert store.events == []
    assert "日期格式无法解析" in commands.add("bad", "标题", "")
    assert "添加失败" in commands.add("13-99", "标题", "")


def test_schedule_static_sorted_and_show_fallback():
    config = {
        "daily_schedule": {
            "schedule_templates": [
                {"name": "晚", "start_time": "22:00", "end_time": "23:00"},
                {"name": "早", "start_time": "07:00", "end_time": "08:00"},
            ]
        }
    }
    commands = _schedule_commands(config)
    static = commands.static()
    assert "[静态日程 共 2 条" in static
    assert static.index("07:00-08:00") < static.index("22:00-23:00")
    show = commands.show("10001")
    assert "静态日程" in show and "07:00-08:00" in show
