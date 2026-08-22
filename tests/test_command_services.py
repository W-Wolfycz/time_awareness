import asyncio
import datetime
from types import SimpleNamespace

from time_awareness.commands.calendar_commands import CalendarCommands
from time_awareness.commands.schedule_commands import ScheduleCommands
from time_awareness.core.calendar_manager import CalendarManager
from time_awareness.core.calendar_store import CalendarStore


NOW = datetime.datetime(2026, 7, 28, 7, 30, tzinfo=datetime.timezone.utc)


class DummyBuiltinManager:
    def load_raw(self):
        return {"events": []}

    def regenerate(self, year, categories):
        return 0


class DummyContext:
    async def get_current_chat_provider_id(self, session):
        return "provider_demo"

    def get_config(self):
        return {"provider_settings": {"default_provider_id": "provider_demo"}}

    async def llm_generate(self, **kwargs):
        return SimpleNamespace(
            role="assistant",
            completion_text=(
                '[{"name":"夜间","start":"22:00","end":"08:00",'
                '"state":"正在休息"}]'
            ),
        )


class DisabledDailySchedule:
    def enabled(self):
        return False

    def register_session(self, session, trigger=True):
        return ""

    def get_snapshot_for_session(self, session, now=None):
        return None

    def get_failure_for_session(self, session, now=None):
        return None

    def ensure_available(self):
        return False


async def _collect(async_iter):
    return [item async for item in async_iter]


def test_calendar_commands_own_parsing_and_output(tmp_path):
    store = CalendarStore()
    manager = CalendarManager(str(tmp_path), store=store)
    manager.load()
    commands = CalendarCommands(
        context=DummyContext(),
        config={},
        calendar_store=store,
        calendar_manager=manager,
        builtin_manager=DummyBuiltinManager(),
        now_provider=lambda: NOW,
        enabled_builtin_categories=lambda: [],
    )

    created = commands.add("07-28", "演示事项", "")
    assert created.startswith("已添加 #")
    assert "演示事项" in commands.show("2026-07")
    short_id = store.events[0]["id"][:8]
    assert commands.delete(short_id) == "已删除：演示事项"


def test_calendar_create_replaces_existing_custom_events(monkeypatch, tmp_path):
    store = CalendarStore()
    manager = CalendarManager(str(tmp_path), store=store)
    manager.load()
    manager.add_event(
        {
            "year": 2026,
            "month": 1,
            "day": 1,
            "text": "旧事项",
            "repeat": 0,
        }
    )
    config = {
        "calendar": {
            "ai_generate_worldview": "演示世界观",
            "ai_generate_provider_id": "provider_demo",
        }
    }
    commands = CalendarCommands(
        context=DummyContext(),
        config=config,
        calendar_store=store,
        calendar_manager=manager,
        builtin_manager=DummyBuiltinManager(),
        now_provider=lambda: NOW,
        enabled_builtin_categories=lambda: [],
    )

    async def _generate(*args, **kwargs):
        return [
            {
                "year": 2026,
                "month": 8,
                "day": 5,
                "text": "新事项",
                "repeat": -1,
            }
        ]

    monkeypatch.setattr(
        "time_awareness.commands.calendar_commands.generate_calendar_events",
        _generate,
    )
    messages = asyncio.run(_collect(commands.generate("platform_demo:PrivateMessage:10001")))

    assert [event["text"] for event in store.events] == ["新事项"]
    assert "原有自定义事项已替换" in messages[-1]


def test_schedule_commands_render_static_schedule_without_event_dependency():
    config = {
        "daily_schedule": {
            "enable_schedule": True,
            "schedule_templates": [
                {
                    "name": "夜间",
                    "start_time": "22:00",
                    "end_time": "08:00",
                    "state_prompt": "正在休息",
                },
                {
                    "name": "日间",
                    "start_time": "08:00",
                    "end_time": "22:00",
                    "state_prompt": "保持清醒",
                },
            ],
        }
    }
    commands = ScheduleCommands(
        context=DummyContext(),
        config=config,
        daily_schedule_service=DisabledDailySchedule(),
        now_provider=lambda: NOW,
        save_config=lambda: None,
        check_overlap=lambda: None,
    )

    output = commands.show("platform_demo:PrivateMessage:10001")
    assert "静态日程" in output
    assert "07:30" not in output
    assert "夜间" in output and "日间" in output
