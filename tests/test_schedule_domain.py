import datetime

import pytest

from time_awareness.domain.schedule import (
    ScheduleValidationError,
    detect_static_overlaps,
    find_active_daily_slot,
    find_active_static_slot,
    format_time,
    normalize_daily_slots,
    parse_time,
)


def test_time_parsing_has_explicit_24_hour_boundary():
    assert parse_time("00:00") == 0
    assert parse_time("24:00", allow_24=True) == 1440
    assert format_time(1440) == "24:00"
    with pytest.raises(ScheduleValidationError):
        parse_time("24:00")
    with pytest.raises(ScheduleValidationError):
        parse_time("8:00")
    assert parse_time("8:00", strict=False) == 480


def test_static_and_daily_resolution_share_left_closed_right_open_rule():
    now = datetime.datetime(2026, 7, 28, 8, 0)
    static = [
        {"name": "夜间", "start_time": "22:00", "end_time": "08:00"},
        {"name": "日间", "start_time": "08:00", "end_time": "22:00"},
    ]
    daily = [
        {"name": "夜间", "start": "00:00", "end": "08:00", "state": "休息"},
        {"name": "日间", "start": "08:00", "end": "24:00", "state": "清醒"},
    ]
    assert find_active_static_slot(static, now)["name"] == "日间"
    assert find_active_daily_slot(daily, now)["name"] == "日间"


def test_overlap_detection_reports_each_pair_once():
    overlaps = detect_static_overlaps(
        [
            {"start_time": "22:00", "end_time": "08:00"},
            {"start_time": "07:00", "end_time": "09:00"},
        ]
    )
    assert len(overlaps) == 1
    assert (overlaps[0].first_index, overlaps[0].second_index) == (0, 1)
    assert overlaps[0].minute == 7 * 60


