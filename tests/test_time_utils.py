"""utils/time_utils.py 的时区与规整逻辑测试。"""

import datetime

from zoneinfo import ZoneInfo

from time_awareness.utils.time_utils import (
    UTC,
    _bounded_int,
    get_now,
    get_tz,
    timezone_from_key,
    to_timezone_key,
)


class _TzConfig:
    def __init__(self, tz):
        self.timezone = tz


def test_get_tz_precedence():
    assert get_tz({"time_awareness": {"timezone": "Asia/Shanghai"}}) == ZoneInfo("Asia/Shanghai")
    cfg = {"time_awareness": {"timezone": "Asia/Shanghai", "use_astrbot_timezone": True}}
    assert get_tz(cfg, _TzConfig("UTC")) == ZoneInfo("UTC")
    assert get_tz(cfg, None) == ZoneInfo("Asia/Shanghai")  # astrbot 缺失回退插件时区
    assert get_tz({}) is None
    assert get_tz({"time_awareness": {"timezone": "Bad/Zone"}}) is None


def test_timezone_from_key():
    assert timezone_from_key("UTC") == UTC
    assert timezone_from_key("Asia/Shanghai") == ZoneInfo("Asia/Shanghai")
    assert timezone_from_key("UTC+08:00").utcoffset(None) == datetime.timedelta(hours=8)
    assert timezone_from_key("") is not None
    assert timezone_from_key("UTC+24:00") is None
    assert timezone_from_key("Bad/Zone") is None


def test_to_timezone_key():
    dt = datetime.datetime(2026, 8, 11, 0, 0, tzinfo=UTC)
    converted = to_timezone_key(dt, "UTC+08:00")
    assert converted.utcoffset() == datetime.timedelta(hours=8)
    assert to_timezone_key(dt, "Bad/Zone") is dt


def test_get_now():
    assert get_now({"time_awareness": {"timezone": "UTC"}}).tzinfo is not None
    assert get_now({}).tzinfo is None


def test_bounded_int():
    assert _bounded_int("5", 1, 1, 10) == 5
    assert _bounded_int(99, 1, 1, 10) == 10
    assert _bounded_int(-5, 1, 1, 10) == 1
    assert _bounded_int("x", 3, 1, 10) == 3
    assert _bounded_int(None, 3, 1, 10) == 3
