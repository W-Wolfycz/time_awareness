import datetime

from time_awareness.utils.time_utils import find_active_schedule_slot, to_utc


def test_to_utc_converts_aware_datetime():
    dt = datetime.datetime(2026, 7, 16, 12, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
    assert to_utc(dt) == datetime.datetime(2026, 7, 16, 4, 0, tzinfo=datetime.timezone.utc)


def test_to_utc_assumes_configured_timezone_for_legacy_naive_value():
    tz = datetime.timezone(datetime.timedelta(hours=8))
    dt = datetime.datetime(2026, 7, 16, 12, 0)
    assert to_utc(dt, default_tz=tz) == datetime.datetime(
        2026, 7, 16, 4, 0, tzinfo=datetime.timezone.utc
    )


def test_schedule_slots_are_left_closed_and_right_open():
    schedule = {
        "schedule_templates": [
            {"name": "第一段", "start_time": "00:00", "end_time": "07:00"},
            {"name": "第二段", "start_time": "07:00", "end_time": "07:30"},
        ]
    }

    before_boundary = find_active_schedule_slot(
        schedule,
        datetime.datetime(2026, 7, 23, 6, 59),
    )
    at_boundary = find_active_schedule_slot(
        schedule,
        datetime.datetime(2026, 7, 23, 7, 0),
    )
    at_right_edge = find_active_schedule_slot(
        schedule,
        datetime.datetime(2026, 7, 23, 7, 30),
    )

    assert before_boundary["name"] == "第一段"
    assert at_boundary["name"] == "第二段"
    assert at_right_edge is None


def test_schedule_gap_at_0700_is_not_silently_assigned():
    schedule = {
        "schedule_templates": [
            {"name": "第一段", "start_time": "00:00", "end_time": "07:00"},
            {"name": "第二段", "start_time": "07:01", "end_time": "07:30"},
        ]
    }

    assert (
        find_active_schedule_slot(
            schedule,
            datetime.datetime(2026, 7, 23, 7, 0),
        )
        is None
    )
