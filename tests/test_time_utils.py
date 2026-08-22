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


# ==================== 时区工具（安全关键） ====================


def test_to_utc_interprets_naive_as_local_or_default_tz():
    from time_awareness.utils.time_utils import UTC, to_utc

    aware = datetime.datetime(2026, 8, 10, 9, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
    assert to_utc(aware) == datetime.datetime(2026, 8, 10, 1, 0, tzinfo=UTC)
    # naive + default_tz：按 default_tz 解释
    naive = datetime.datetime(2026, 8, 10, 9, 0)
    tz8 = datetime.timezone(datetime.timedelta(hours=8))
    assert to_utc(naive, default_tz=tz8) == datetime.datetime(2026, 8, 10, 1, 0, tzinfo=UTC)
    # 非 datetime 输入抛 TypeError
    try:
        to_utc("2026-08-10")
        raise AssertionError("应当抛 TypeError")
    except TypeError:
        pass


def test_coerce_to_aware_aligns_mixed_awareness():
    from time_awareness.utils.time_utils import coerce_to_aware

    tz8 = datetime.timezone(datetime.timedelta(hours=8))
    utc = datetime.timezone.utc
    # reference aware、dt naive：naive 按 reference 时区解释
    naive = datetime.datetime(2026, 8, 10, 9, 0)
    aware_ref = datetime.datetime(2026, 8, 10, 9, 0, tzinfo=tz8)
    aligned = coerce_to_aware(naive, aware_ref)
    assert aligned.tzinfo == tz8
    # reference naive、dt aware：aware 转系统本地后去 tzinfo（不改变墙钟时刻语义）
    aware = datetime.datetime(2026, 8, 10, 9, 0, tzinfo=utc)
    naive_ref = datetime.datetime(2026, 8, 10, 9, 0)
    aligned2 = coerce_to_aware(aware, naive_ref)
    assert aligned2.tzinfo is None
    # 两者都 aware：转换到 reference 时区
    assert coerce_to_aware(aware, aware_ref) == aware.astimezone(tz8)
    # 两者都 naive：原样返回
    assert coerce_to_aware(naive, naive_ref) == naive


def test_get_tz_validates_config_and_falls_back():
    from time_awareness.utils.time_utils import get_tz

    # 有效 IANA
    tz = get_tz({"time_awareness": {"timezone": "Asia/Shanghai"}})
    assert tz is not None
    assert str(tz) == "Asia/Shanghai"
    # 无效 IANA：回退 None（系统本地）
    assert get_tz({"time_awareness": {"timezone": "Not/AZone"}}) is None
    # 非字符串（数字）：不抛异常，回退 None
    assert get_tz({"time_awareness": {"timezone": 8}}) is None
    # 未配置：None
    assert get_tz({"time_awareness": {}}) is None


def test_find_active_schedule_slot_cross_midnight_and_gaps():
    from time_awareness.utils.time_utils import find_active_schedule_slot

    config = {
        "schedule_templates": [
            {"name": "睡眠", "start_time": "22:00", "end_time": "08:00", "state_prompt": "睡"},
            {"name": "日间", "start_time": "10:00", "end_time": "12:00", "state_prompt": "忙"},
        ]
    }
    # 跨午夜夜间段：凌晨 07:00 命中
    night = find_active_schedule_slot(config, datetime.datetime(2026, 8, 10, 7, 0))
    assert night["name"] == "睡眠"
    # 白天段命中
    day = find_active_schedule_slot(config, datetime.datetime(2026, 8, 10, 11, 0))
    assert day["name"] == "日间"
    # 空档（12:00-22:00 之间）不命中
    assert find_active_schedule_slot(config, datetime.datetime(2026, 8, 10, 15, 0)) is None
    # 空配置
    assert find_active_schedule_slot({"schedule_templates": []}, datetime.datetime(2026, 8, 10, 7, 0)) is None
