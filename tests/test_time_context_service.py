import datetime

from time_awareness.core.calendar_store import CalendarStore
from time_awareness.core.last_chat_tracker import LastChatTracker
from time_awareness.services.time_context_service import TimeContextService


def test_dynamic_context_keeps_event_and_sensor_boundaries(tmp_path):
    now = datetime.datetime(2026, 7, 16, 12, 0, tzinfo=datetime.timezone.utc)
    config = {
        "time_awareness": {
            "time_guidance_enabled": True,
            "workday_state_enabled": False,
            "lunar_state_enabled": False,
            "almanac_enabled": False,
            "last_chat_enabled": False,
        },
        "daily_schedule": {"enable_schedule": False},
        "calendar": {"enable_calendar": True, "enable_builtin_events": False},
    }
    store = CalendarStore()
    store.set_events(
        [
            {
                "id": "event-demo",
                "year": 2026,
                "month": 7,
                "day": 16,
                "text": "演示日",
                "repeat": 0,
            }
        ]
    )
    service = TimeContextService(
        config=config,
        astrbot_config_provider=lambda: None,
        calendar_store=store,
        last_chat_tracker=LastChatTracker(str(tmp_path)),
        now_provider=lambda: now,
    )
    result = service.build_dynamic("demo:GroupMessage:group_demo")
    assert "<SCHEDULE_STATE>" in result
    assert "<TODAY_EVENTS>演示日</TODAY_EVENTS>" in result
    assert "<WORKDAY_STATE>" not in result
    assert "<LUNAR_STATE>" not in result
    assert "<ALMANAC_STATE>" not in result
