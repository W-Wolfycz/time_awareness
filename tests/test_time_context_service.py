import datetime

from time_awareness.core.calendar_store import CalendarStore
from time_awareness.core.last_chat_tracker import LastChatTracker
from time_awareness.services.time_context_service import TimeContextService
from time_awareness.services.daily_schedule_service import ScheduleResolution


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
    assert "<SYSTEM_REMINDER>当前日期时间：2026-07-16 12:00（UTC），星期四</SYSTEM_REMINDER>" in result
    assert "<SCHEDULE_STATE>" in result
    assert "<TODAY_EVENTS>演示日</TODAY_EVENTS>" in result
    assert "<WORKDAY_STATE>" not in result
    assert "<LUNAR_STATE>" not in result
    assert "<ALMANAC_STATE>" not in result


def test_dynamic_context_uses_one_time_snapshot(tmp_path):
    calls = []
    now = datetime.datetime(2026, 7, 23, 7, 0, tzinfo=datetime.timezone.utc)
    config = {
        "time_awareness": {
            "time_guidance_enabled": True,
            "workday_state_enabled": False,
            "lunar_state_enabled": False,
            "almanac_enabled": False,
            "last_chat_enabled": False,
        },
        "daily_schedule": {
            "enable_schedule": True,
            "schedule_templates": [
                {
                    "name": "晨间",
                    "start_time": "07:00",
                    "end_time": "08:00",
                    "state_prompt": "刚进入晨间状态",
                }
            ],
        },
        "calendar": {"enable_calendar": False},
    }

    def now_provider():
        calls.append(None)
        return now

    service = TimeContextService(
        config=config,
        astrbot_config_provider=lambda: None,
        calendar_store=CalendarStore(),
        last_chat_tracker=LastChatTracker(str(tmp_path)),
        now_provider=now_provider,
    )

    result = service.build_dynamic("demo:GroupMessage:group_demo")

    assert calls == [None]
    assert "<SYSTEM_REMINDER>当前日期时间：2026-07-23 07:00（UTC），星期四</SYSTEM_REMINDER>" in result
    assert "<SCHEDULE_STATE>刚进入晨间状态</SCHEDULE_STATE>" in result


def test_dynamic_context_omits_reality_time_when_guidance_disabled(tmp_path):
    now = datetime.datetime(2026, 7, 23, 7, 0, tzinfo=datetime.timezone.utc)
    config = {
        "time_awareness": {
            "time_guidance_enabled": False,
        },
        "daily_schedule": {"enable_schedule": False},
        "calendar": {"enable_calendar": False},
    }
    service = TimeContextService(
        config=config,
        astrbot_config_provider=lambda: None,
        calendar_store=CalendarStore(),
        last_chat_tracker=LastChatTracker(str(tmp_path)),
        now_provider=lambda: now,
    )

    result = service.build_dynamic("demo:GroupMessage:group_demo")

    assert "<SYSTEM_REMINDER>" not in result
    assert "<SCHEDULE_STATE>无固定安排（按人设自然演绎）</SCHEDULE_STATE>" in result


def test_schedule_gap_injects_unspecified_for_persona_time_inference(tmp_path):
    now = datetime.datetime(2026, 7, 28, 18, 0, tzinfo=datetime.timezone.utc)
    config = {
        "time_awareness": {
            "time_guidance_enabled": True,
            "workday_state_enabled": False,
            "lunar_state_enabled": False,
            "almanac_enabled": False,
            "last_chat_enabled": False,
        },
        "daily_schedule": {
            "enable_schedule": True,
            "schedule_templates": [
                {
                    "name": "晨间兜底",
                    "start_time": "08:00",
                    "end_time": "10:00",
                    "state_prompt": "刚刚清醒",
                }
            ],
        },
        "calendar": {"enable_calendar": False},
    }
    service = TimeContextService(
        config=config,
        astrbot_config_provider=lambda: None,
        calendar_store=CalendarStore(),
        last_chat_tracker=LastChatTracker(str(tmp_path)),
        now_provider=lambda: now,
    )

    class SparseResolver:
        def resolve(self, *, now, session):
            return None

    service.set_daily_schedule_service(SparseResolver())
    result = service.build_dynamic("platform_demo:PrivateMessage:10001")

    assert "<SYSTEM_REMINDER>当前日期时间：2026-07-28 18:00（UTC），星期二</SYSTEM_REMINDER>" in result
    assert "<SCHEDULE_STATE>无固定安排（按人设自然演绎）</SCHEDULE_STATE>" in result


def test_ai_daily_schedule_wins_over_static_schedule(tmp_path):
    now = datetime.datetime(2026, 7, 28, 7, 30, tzinfo=datetime.timezone.utc)
    config = {
        "time_awareness": {
            "time_guidance_enabled": True,
            "workday_state_enabled": False,
            "lunar_state_enabled": False,
            "almanac_enabled": False,
            "last_chat_enabled": False,
        },
        "daily_schedule": {
            "enable_schedule": True,
            "schedule_templates": [
                {
                    "name": "兜底",
                    "start_time": "00:00",
                    "end_time": "23:59",
                    "state_prompt": "静态状态",
                }
            ],
        },
        "calendar": {"enable_calendar": False},
    }
    service = TimeContextService(
        config=config,
        astrbot_config_provider=lambda: None,
        calendar_store=CalendarStore(),
        last_chat_tracker=LastChatTracker(str(tmp_path)),
        now_provider=lambda: now,
    )

    class Resolver:
        def resolve(self, *, now, session):
            assert session == "platform_demo:PrivateMessage:10001"
            return ScheduleResolution("AI 状态", "晨间", "ai", "snapshot-demo")

    service.set_daily_schedule_service(Resolver())
    result = service.build_dynamic("platform_demo:PrivateMessage:10001")
    assert "<SCHEDULE_STATE>AI 状态</SCHEDULE_STATE>" in result
    assert "静态状态" not in result


def test_generation_sensor_snapshot_does_not_promote_session_last_chat(tmp_path):
    now = datetime.datetime(2026, 7, 28, 7, 30, tzinfo=datetime.timezone.utc)
    config = {
        "time_awareness": {
            "workday_state_enabled": False,
            "lunar_state_enabled": False,
            "almanac_enabled": False,
            "last_chat_enabled": True,
        },
        "calendar": {"enable_calendar": False},
    }
    service = TimeContextService(
        config=config,
        astrbot_config_provider=lambda: None,
        calendar_store=CalendarStore(),
        last_chat_tracker=LastChatTracker(str(tmp_path)),
        now_provider=lambda: now,
    )
    snapshot = service.build_generation_sensor_snapshot(now=now)
    assert set(snapshot) == {"workday", "lunar", "almanac", "today_events"}
    assert snapshot["workday"]["as_of"] == now.isoformat()
    assert "last_chat" not in snapshot
    assert "current_datetime" not in snapshot
    assert "<SYSTEM_REMINDER>" not in str(snapshot)


def test_dynamic_context_can_omit_last_chat_without_disabling_tracking(tmp_path):
    now = datetime.datetime(2026, 8, 5, 9, 0, tzinfo=datetime.timezone.utc)
    session = "platform_demo:PrivateMessage:10001"
    tracker = LastChatTracker(str(tmp_path))
    tracker.record_user(session, now - datetime.timedelta(minutes=5))
    tracker.record_ai(session, now - datetime.timedelta(minutes=3))
    config = {
        "time_awareness": {
            "time_guidance_enabled": True,
            "workday_state_enabled": False,
            "lunar_state_enabled": False,
            "almanac_enabled": False,
            "last_chat_enabled": True,
        },
        "daily_schedule": {"enable_schedule": False},
        "calendar": {"enable_calendar": False},
    }
    service = TimeContextService(
        config=config,
        astrbot_config_provider=lambda: None,
        calendar_store=CalendarStore(),
        last_chat_tracker=tracker,
        now_provider=lambda: now,
    )

    assert "<LAST_CHAT>用户 5 分钟前 · AI 3 分钟前</LAST_CHAT>" in service.build_dynamic(
        session
    )
    suppressed = service.build_dynamic(session, include_last_chat=False)
    assert "<LAST_CHAT>" not in suppressed
    assert tracker.summary(session, now) == "用户 5 分钟前 · AI 3 分钟前"
