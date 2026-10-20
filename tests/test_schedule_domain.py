"""domain/schedule.py 的边界与不变量测试。"""

import datetime

import pytest

from time_awareness.domain.schedule import (
    ScheduleValidationError,
    find_active_daily_slot,
    find_active_static_slot,
    is_minute_in_static_range,
    slot_minutes,
    split_cross_midnight,
)


def test_slot_minutes():
    assert slot_minutes({"_start_minute": 100, "_end_minute": 200}) == (100, 200)
    assert slot_minutes({"start": "8:00", "end": "9:00"}) == (480, 540)


def test_split_cross_midnight_and_range():
    assert split_cross_midnight(480, 540) == ((480, 540),)
    assert split_cross_midnight(1320, 480) == ((1320, 1440), (0, 480))
    assert split_cross_midnight(480, 480) == ()
    with pytest.raises(ScheduleValidationError):
        split_cross_midnight(1440, 100)
    assert is_minute_in_static_range(500, 480, 540)
    assert not is_minute_in_static_range(540, 480, 540)  # 右开
    assert is_minute_in_static_range(1380, 1320, 480)  # 跨午夜


def test_find_active_slots():
    slots = [
        {"name": "a", "start_time": "06:00", "end_time": "07:00"},
        {"name": "b", "start_time": "07:00", "end_time": "08:00"},
        {"name": "late", "start_time": "22:00", "end_time": "06:00"},
    ]
    assert find_active_static_slot(slots, datetime.datetime(2026, 8, 11, 7, 30))["name"] == "b"
    assert find_active_static_slot(slots, datetime.datetime(2026, 8, 11, 23, 0))["name"] == "late"
    daily = [{"start": "07:00", "end": "08:00", "state": "x"}]
    assert find_active_daily_slot(daily, datetime.datetime(2026, 8, 11, 7, 30))["state"] == "x"
