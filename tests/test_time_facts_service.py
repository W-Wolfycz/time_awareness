import datetime

from time_awareness.core.calendar_store import CalendarStore
from time_awareness.services.time_facts_service import TimeFactsCollector


NOW = datetime.datetime(2026, 7, 28, 7, 30, tzinfo=datetime.timezone.utc)


class FakeWorkday:
    def __init__(self):
        self.calls = 0

    def classify(self, date):
        self.calls += 1
        return "workday", "工作日（周二）"


class FakeLunar:
    def __init__(self):
        self.calls = 0

    def collect(self, date):
        self.calls += 1
        return "农历演示", {"lunar_year_chip": "演示年", "lunar_month_day": "演示日"}


class FakeAlmanac:
    def __init__(self):
        self.calls = 0

    def collect(self, date):
        self.calls += 1
        return "黄历演示", {"gz_day": "演示日", "chong_sha": "", "yi": "", "ji": ""}


class GuardedLastChat:
    def __init__(self):
        self.calls = 0

    def summary(self, session, now):
        self.calls += 1
        return "用户 5 分钟前"


def _collector(scope_config=None):
    config = scope_config or {
        "time_awareness": {
            "workday_state_enabled": True,
            "lunar_state_enabled": True,
            "almanac_enabled": True,
            "last_chat_enabled": True,
        },
        "daily_schedule": {"enable_schedule": False},
        "calendar": {"enable_calendar": False},
    }
    workday = FakeWorkday()
    lunar = FakeLunar()
    almanac = FakeAlmanac()
    last_chat = GuardedLastChat()
    collector = TimeFactsCollector(
        config=config,
        astrbot_config_provider=lambda: None,
        calendar_store=CalendarStore(),
        last_chat_tracker=last_chat,
        now_provider=lambda: NOW,
        workday=workday,
        lunar=lunar,
        almanac=almanac,
    )
    return collector, workday, lunar, almanac, last_chat


def test_dashboard_collects_each_sensor_once():
    collector, workday, lunar, almanac, last_chat = _collector()
    facts = collector.collect(scope="dashboard")

    assert facts.now is NOW
    assert facts.lunar.details["lunar_year_chip"] == "演示年"
    assert (workday.calls, lunar.calls, almanac.calls) == (1, 1, 1)
    assert last_chat.calls == 0


def test_bot_generation_scope_never_reads_conversation_last_chat():
    collector, _, _, _, last_chat = _collector()
    facts = collector.collect(
        scope="persona_daily_generation",
        session="platform_demo:PrivateMessage:10001",
    )

    assert not facts.last_chat.available
    assert last_chat.calls == 0


def test_conversation_scope_reads_last_chat_only_for_current_session():
    collector, _, _, _, last_chat = _collector()
    facts = collector.collect(
        scope="conversation",
        session="platform_demo:PrivateMessage:10001",
    )

    assert facts.last_chat.value == "用户 5 分钟前"
    assert last_chat.calls == 1


# ==================== 多选 selector 语义（2.0.x） ====================


def test_time_sensors_selector_controls_each_sensor():
    """新形态：time_sensors 列表逐项控制传感器；空列表=全部关闭。"""
    collector, _, _, _, _ = _collector(
        {
            "time_awareness": {"time_sensors": ["lunar"], "last_chat_enabled": True},
            "daily_schedule": {"enable_schedule": False},
            "calendar": {"enable_calendar": False},
        }
    )
    assert collector.workday_enabled() is False
    assert collector.lunar_enabled() is True
    assert collector.almanac_enabled() is False

    collector_empty, _, _, _, _ = _collector(
        {
            "time_awareness": {"time_sensors": [], "last_chat_enabled": True},
            "daily_schedule": {"enable_schedule": False},
            "calendar": {"enable_calendar": False},
        }
    )
    assert collector_empty.workday_enabled() is False
    assert collector_empty.lunar_enabled() is False
    assert collector_empty.almanac_enabled() is False


def test_time_sensors_missing_falls_back_to_legacy_bools():
    """未迁移的旧配置：无 time_sensors 时仍按旧布尔开关解释。"""
    collector, _, _, _, _ = _collector()
    assert collector.workday_enabled() is True
    assert collector.lunar_enabled() is True
    assert collector.almanac_enabled() is True  # 夹具里旧布尔全开


def test_builtin_event_categories_selector_maps_to_categories():
    """新形态：builtin_event_categories 列表直接决定启用的分类。"""
    collector, _, _, _, _ = _collector(
        {
            "time_awareness": {"time_sensors": ["workday"]},
            "daily_schedule": {"enable_schedule": False},
            "calendar": {
                "enable_calendar": True,
                "builtin_event_categories": ["legal_holidays", "political"],
            },
        }
    )
    assert set(collector.enabled_builtin_categories()) == {
        "legal_holiday",
        "political",
    }

    collector_empty, _, _, _, _ = _collector(
        {
            "time_awareness": {"time_sensors": ["workday"]},
            "daily_schedule": {"enable_schedule": False},
            "calendar": {
                "enable_calendar": True,
                "builtin_event_categories": [],
            },
        }
    )
    assert collector_empty.enabled_builtin_categories() == []


def test_builtin_categories_missing_falls_back_to_legacy_shape():
    """未迁移的旧配置：无 builtin_event_categories 时按旧布尔/分组解释。"""
    collector, _, _, _, _ = _collector(
        {
            "time_awareness": {"time_sensors": ["workday"]},
            "daily_schedule": {"enable_schedule": False},
            "calendar": {
                "enable_calendar": True,
                "enable_builtin_events": True,
                "builtin_events": {
                    "legal_holidays": True,
                    "traditional": False,
                    "solar_terms": False,
                    "political": True,
                    "international": False,
                },
            },
        }
    )
    assert set(collector.enabled_builtin_categories()) == {
        "legal_holiday",
        "political",
    }

    collector_disabled, _, _, _, _ = _collector(
        {
            "time_awareness": {"time_sensors": ["workday"]},
            "daily_schedule": {"enable_schedule": False},
            "calendar": {"enable_calendar": True, "enable_builtin_events": False},
        }
    )
    assert collector_disabled.enabled_builtin_categories() == []
