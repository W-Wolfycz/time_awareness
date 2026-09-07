"""services/time_facts_service.py 的传感器开关与事实采集测试。"""

import datetime
from dataclasses import asdict

import pytest

from time_awareness.core.builtin_events import (
    CATEGORY_LEGAL,
    CATEGORY_POLITICAL,
    CATEGORY_SOLAR_TERM,
    CATEGORY_TRADITIONAL,
)
from time_awareness.core.calendar_store import CalendarStore
from time_awareness.services.time_facts_service import TimeFactsCollector

NOW = datetime.datetime(2026, 8, 11, 7, 30, tzinfo=datetime.timezone.utc)


class _LastChat:
    def summary(self, session, now):
        return ""


def _collector(config, calendar_store=None):
    return TimeFactsCollector(
        config=config,
        astrbot_config_provider=lambda: None,
        calendar_store=calendar_store or CalendarStore(),
        last_chat_tracker=_LastChat(),
        now_provider=lambda: NOW,
    )


def test_sensor_enabled_selector_and_legacy():
    config = {
        "time_awareness": {
            "time_sensors": ["workday"],
            "workday_state_enabled": False,
            "lunar_state_enabled": True,
            "almanac_enabled": True,
        }
    }
    collector = _collector(config)
    assert collector.workday_enabled() is True
    assert collector.lunar_enabled() is False and collector.almanac_enabled() is False

    legacy = _collector({"time_awareness": {}})
    assert legacy.workday_enabled() is True and legacy.lunar_enabled() is True
    assert legacy.almanac_enabled() is False
    assert _collector({"time_awareness": {"almanac_enabled": True}}).almanac_enabled() is True


def test_enabled_builtin_categories():
    config = {"calendar": {"builtin_event_categories": ["legal_holidays", "political"]}}
    assert _collector(config).enabled_builtin_categories() == [CATEGORY_LEGAL, CATEGORY_POLITICAL]
    assert _collector({}).enabled_builtin_categories() == [
        CATEGORY_LEGAL,
        CATEGORY_TRADITIONAL,
        CATEGORY_SOLAR_TERM,
    ]
    disabled = {"calendar": {"enable_builtin_events": False}}
    assert _collector(disabled).enabled_builtin_categories() == []


def test_today_events_text():
    store = CalendarStore()
    store.set_events([{"year": 2026, "month": 8, "day": 11, "text": "用户", "repeat": 0}])
    store.set_builtin_events(
        [{"source": "builtin", "year": 2026, "month": 8, "day": 11, "text": "内置"}]
    )
    config = {"calendar": {"enable_calendar": True, "builtin_event_categories": ["political"]}}
    assert _collector(config, store).today_events_text(now=NOW) == "用户、内置"
    assert _collector({"calendar": {"enable_calendar": False}}).today_events_text(now=NOW) == ""


def test_collect_keys_flags_and_scope():
    config = {"time_awareness": {"time_sensors": ["workday", "lunar", "almanac"]}}
    facts = _collector(config).collect(scope="dashboard")
    keys = {"now", "schedule", "workday", "lunar", "almanac", "today_events", "last_chat"}
    assert set(asdict(facts)) == keys
    assert facts.now == NOW
    assert facts.workday.enabled and facts.lunar.enabled and facts.almanac.enabled

    disabled = _collector({"time_awareness": {"time_sensors": []}}).collect(scope="dashboard")
    for sensor in (disabled.workday, disabled.lunar, disabled.almanac):
        assert sensor.enabled is False and sensor.available is False
    with pytest.raises(ValueError):
        _collector({}).collect(scope="bogus")
