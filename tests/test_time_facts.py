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
from time_awareness.core.workday_sensor import workday_sensor
from time_awareness.services.time_facts_service import TimeFactsCollector
from time_awareness.tests._shared import FakeLastChatTracker

NOW = datetime.datetime(2026, 8, 11, 7, 30, tzinfo=datetime.timezone.utc)


def _collector(config, calendar_store=None):
    return TimeFactsCollector(
        config=config,
        astrbot_config_provider=lambda: None,
        calendar_store=calendar_store or CalendarStore(),
        last_chat_tracker=FakeLastChatTracker(),
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
    keys = {
        "now", "schedule", "workday", "lunar", "almanac",
        "today_events", "upcoming", "last_chat",
    }
    assert set(asdict(facts)) == keys
    assert facts.now == NOW
    assert facts.workday.enabled and facts.lunar.enabled and facts.almanac.enabled

    disabled = _collector({"time_awareness": {"time_sensors": []}}).collect(scope="dashboard")
    for sensor in (disabled.workday, disabled.lunar, disabled.almanac):
        assert sensor.enabled is False and sensor.available is False
    with pytest.raises(ValueError):
        _collector({}).collect(scope="bogus")


def test_workday_kinds_distinguish_weekend_from_holiday():
    # 普通周末被算成「节假日」会让模型按节日庆祝（且「周末」标签永远不会出现）；
    # 只有带节日名的休息日才是节假日，调休上班的周末是第三种
    assert workday_sensor.classify(datetime.date(2026, 10, 17)) == ("weekend", "周末（周六）")
    assert workday_sensor.classify(datetime.date(2026, 10, 1)) == ("holiday", "节假日（周四）")
    assert workday_sensor.classify(datetime.date(2026, 10, 10)) == ("adjusted", "调休工作日（周六）")
    assert workday_sensor.classify(datetime.date(2026, 10, 12)) == ("workday", "工作日（周一）")
    assert workday_sensor.classify(datetime.date(2026, 10, 16)) == ("workday", "工作日（周五）")
