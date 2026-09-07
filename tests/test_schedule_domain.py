"""domain/schedule.py 的边界与不变量测试。"""

import datetime

import pytest

from time_awareness.domain.schedule import (
    ScheduleValidationError,
    detect_static_overlaps,
    find_active_daily_slot,
    find_active_static_slot,
    format_time,
    is_minute_in_static_range,
    minute_of_day,
    normalize_static_slots,
    parse_time,
    slot_minutes,
    sort_static_slots,
    split_cross_midnight,
)


def test_parse_time_strict_and_lenient():
    assert parse_time("08:00") == 480
    assert parse_time("23:59") == 23 * 60 + 59
    assert parse_time("8:00", strict=False) == 480
    with pytest.raises(ScheduleValidationError):
        parse_time("8:00")  # strict 需要 HH:MM


def test_parse_time_allow_24_and_invalid():
    assert parse_time("24:00", allow_24=True) == 1440
    with pytest.raises(ScheduleValidationError):
        parse_time("24:00")
    for bad in ("25:00", "abc", "8:5"):
        with pytest.raises(ScheduleValidationError):
            parse_time(bad, strict=False)
    with pytest.raises(ScheduleValidationError):
        parse_time(123)


def test_format_time_and_minute_of_day():
    assert format_time(0) == "00:00"
    assert format_time(480) == "08:00"
    assert format_time(1440) == "24:00"
    with pytest.raises(ScheduleValidationError):
        format_time(1441)
    assert minute_of_day(datetime.datetime(2026, 8, 11, 7, 30)) == 450


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


def test_detect_static_overlaps():
    overlaps = detect_static_overlaps(
        [{"start_time": "08:00", "end_time": "10:00"}, {"start_time": "09:00", "end_time": "11:00"}]
    )
    assert (overlaps[0].first_index, overlaps[0].second_index, overlaps[0].minute) == (0, 1, 540)
    cross = detect_static_overlaps(
        [{"start_time": "22:00", "end_time": "06:00"}, {"start_time": "05:00", "end_time": "07:00"}]
    )
    assert cross[0].minute == 300


def test_normalize_and_sort_static_slots():
    result = normalize_static_slots([{"name": "x", "start_time": "22:00", "end_time": "08:00"}])
    assert len(result) == 2
    assert result[0]["start"] == "22:00" and result[0]["end"] == "24:00"
    assert result[1]["start"] == "00:00" and result[1]["_start_minute"] == 0
    assert "start_time" not in result[0] and "end_time" not in result[0]
    slots = [
        {"name": "c", "start_time": "09:00"},
        {"name": "a", "start_time": "07:00"},
        {"name": "bad", "start_time": "nope"},
    ]
    assert [s["name"] for s in sort_static_slots(slots)] == ["a", "c", "bad"]
