"""services/time_facts_service.py 的传感器开关与事实采集测试。"""

import datetime


from time_awareness.core.calendar_store import CalendarStore
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


def test_workday_kind_distinguishes_workday_weekend_and_holiday():
    """周末判成节假日/节假日判成工作日 → 语气与安排全错，且没有任何报错。"""
    from time_awareness.core.workday_sensor import WorkdaySensor

    sensor = WorkdaySensor()
    workday = sensor.classify(datetime.date(2026, 10, 12))[0]  # 周一
    weekend = sensor.classify(datetime.date(2026, 10, 17))[0]  # 周六
    holiday = sensor.classify(datetime.date(2026, 10, 1))[0]  # 国庆
    assert workday == "workday"
    assert weekend == "weekend"
    assert holiday == "holiday"
