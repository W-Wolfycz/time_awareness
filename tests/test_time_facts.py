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


def test_sensor_selector():
    """传感器只由 time_sensors 多选决定；旧布尔键已从 schema 移除，不再回退。"""
    collector = _collector({"time_awareness": {"time_sensors": ["workday", "almanac"]}})
    assert collector.workday_enabled() is True
    assert collector.lunar_enabled() is False
    assert collector.almanac_enabled() is True

    empty = _collector({"time_awareness": {"time_sensors": []}})
    assert empty.workday_enabled() is False and empty.lunar_enabled() is False
    assert empty.almanac_enabled() is False

    missing = _collector({"time_awareness": {}})
    assert missing.workday_enabled() is False and missing.almanac_enabled() is False


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
