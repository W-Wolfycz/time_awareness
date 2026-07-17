import datetime

from time_awareness.utils.time_utils import to_utc


def test_to_utc_converts_aware_datetime():
    dt = datetime.datetime(2026, 7, 16, 12, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
    assert to_utc(dt) == datetime.datetime(2026, 7, 16, 4, 0, tzinfo=datetime.timezone.utc)


def test_to_utc_assumes_configured_timezone_for_legacy_naive_value():
    tz = datetime.timezone(datetime.timedelta(hours=8))
    dt = datetime.datetime(2026, 7, 16, 12, 0)
    assert to_utc(dt, default_tz=tz) == datetime.datetime(
        2026, 7, 16, 4, 0, tzinfo=datetime.timezone.utc
    )
