import datetime

from time_awareness.core.calendar_store import (
    REPEAT_FOREVER,
    REPEAT_NONE,
    CalendarStore,
    normalize_repeat,
    valid_month_day,
)


def _user_event(year, month, day, text="事项", repeat=REPEAT_NONE, source=None):
    event = {
        "id": f"e-{year}-{month}-{day}-{text}",
        "year": year,
        "month": month,
        "day": day,
        "text": text,
        "repeat": repeat,
    }
    if source:
        event["source"] = source
    return event


def _builtin_event(year, month, day, text="内置事项"):
    return _user_event(year, month, day, text=text, source="builtin")


def test_events_for_date_apply_repeat_rules():
    """repeat 规则：仅当年 / 连续 N 年 / 每年重复。"""
    store = CalendarStore()
    store.set_events([
        _user_event(2025, 8, 10, "仅当年", repeat=REPEAT_NONE),
        _user_event(2025, 8, 11, "连续两年", repeat=1),   # 2025-2026
        _user_event(2025, 8, 12, "每年", repeat=REPEAT_FOREVER),
    ])
    # 2025：全部生效
    assert {e["text"] for e in store.events_for_date(2025, 8, 10)} == {"仅当年"}
    assert {e["text"] for e in store.events_for_date(2025, 8, 11)} == {"连续两年"}
    assert {e["text"] for e in store.events_for_date(2025, 8, 12)} == {"每年"}
    # 2026：连续两年 + 每年；仅当年消失
    assert store.events_for_date(2026, 8, 10) == []
    assert {e["text"] for e in store.events_for_date(2026, 8, 11)} == {"连续两年"}
    assert {e["text"] for e in store.events_for_date(2026, 8, 12)} == {"每年"}
    # 2027：只有每年
    assert store.events_for_date(2027, 8, 11) == []
    assert {e["text"] for e in store.events_for_date(2027, 8, 12)} == {"每年"}


def test_user_event_takes_precedence_over_builtin():
    """同 (month, day, text) 去重：user 优先于 builtin。"""
    store = CalendarStore()
    store.set_events([_user_event(2026, 8, 10, "同文案")])
    store.set_builtin_events([_builtin_event(2026, 8, 10, "同文案")])
    events = store.events_for_date(2026, 8, 10, include_builtin=True)
    assert len(events) == 1
    assert events[0].get("source") != "builtin"  # user 版本保留


def test_builtin_requires_exact_year_match():
    """builtin 事件按公历年份精确匹配，跨年不生效。"""
    store = CalendarStore()
    store.set_builtin_events([_builtin_event(2026, 8, 10)])
    assert len(store.events_for_date(2026, 8, 10)) == 1
    assert store.events_for_date(2027, 8, 10) == []


def test_leap_day_visibility_and_ghost_dates():
    """闰年 2-29 生效；非闰年 2 月视图不出现；4 月不出现 31 日。"""
    store = CalendarStore()
    store.set_events([
        _user_event(2024, 2, 29, "闰日", repeat=REPEAT_FOREVER),
        _user_event(2026, 4, 31, "幽灵", repeat=REPEAT_FOREVER),
    ])
    # 闰年 2024：2-29 可见
    assert {e["text"] for e in store.events_for_date(2024, 2, 29)} == {"闰日"}
    # 非闰年 2027：2 月没有 29 日（month view 只迭代实际天数）
    assert not any(e["text"] == "闰日" for e in store.events_for_month(2027, 2))
    # 4 月视图不出现 31 日（幽灵日期规避）
    assert not any(e["text"] == "幽灵" for e in store.events_for_month(2026, 4))


def test_events_for_month_sorted_by_day_and_deduped():
    store = CalendarStore()
    store.set_events([
        _user_event(2026, 8, 15, "晚"),
        _user_event(2026, 8, 3, "早"),
        _user_event(2026, 8, 15, "晚"),  # 与第一条同 (month, day, text) 去重
    ])
    month = store.events_for_month(2026, 8)
    days = [e["day"] for e in month]
    assert days == sorted(days)
    assert len([e for e in month if e["text"] == "晚"]) == 1


def test_today_text_joins_events():
    store = CalendarStore()
    store.set_events([
        _user_event(2026, 8, 10, "甲"),
        _user_event(2026, 8, 10, "乙"),
    ])
    now = datetime.datetime(2026, 8, 10, 12, 0)
    assert store.today_text(now, separator="、") == "甲、乙"
    assert store.today_text(datetime.datetime(2026, 8, 11, 12, 0)) == ""


def test_normalize_repeat_and_valid_month_day():
    assert normalize_repeat(-1) == REPEAT_FOREVER
    assert normalize_repeat(3) == 3
    assert normalize_repeat(5) == REPEAT_NONE  # 越界回退
    assert normalize_repeat("abc") == REPEAT_NONE
    assert normalize_repeat(None) == REPEAT_NONE
    # 闰年感知：2026 非闰年 2-29 非法；2028 闰年合法
    assert valid_month_day(2, 29, year=2028) is True
    assert valid_month_day(2, 29, year=2026) is False
    assert valid_month_day(2, 29) is True  # 无年份按「年年可重复」上限
    assert valid_month_day(4, 31) is False
    assert valid_month_day(13, 1) is False


def test_clear_and_set_builtin():
    store = CalendarStore()
    store.set_events([_user_event(2026, 1, 1)])
    store.set_builtin_events([_builtin_event(2026, 1, 1)])
    store.clear()
    assert store.events == []
    assert store.builtin_events == []
