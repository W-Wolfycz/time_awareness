"""utils/time_utils.py 的时区与规整逻辑测试。"""


from zoneinfo import ZoneInfo

from time_awareness.utils.time_utils import (
    get_now,
    get_tz,
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


def test_get_now():
    assert get_now({"time_awareness": {"timezone": "UTC"}}).tzinfo is not None
    assert get_now({}).tzinfo is None
