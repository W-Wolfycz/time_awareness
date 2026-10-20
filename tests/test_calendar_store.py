"""core/calendar_store.py 与 core/builtin_events.py 的规则测试。"""


from time_awareness.core.calendar_store import (
    REPEAT_FOREVER,
    CalendarStore,
    normalize_repeat,
    valid_month_day,
)


def _store(events=None, builtin=None):
    store = CalendarStore()
    if events is not None:
        store.set_events(events)
    if builtin is not None:
        store.set_builtin_events(builtin)
    return store


def test_normalize_repeat_and_valid_month_day():
    assert normalize_repeat(-1) == REPEAT_FOREVER
    assert normalize_repeat(0) == 0 and normalize_repeat(4) == 4
    assert normalize_repeat(5) == 0 and normalize_repeat("bad") == 0
    assert valid_month_day(2, 29) is True and valid_month_day(2, 30) is False
    assert valid_month_day(2, 29, 2024) is True and valid_month_day(2, 29, 2025) is False
    assert valid_month_day(13, 1) is False and valid_month_day(4, 31) is False


def test_events_for_date_repeat_rules():
    store = _store(
        [
            {"year": 2026, "month": 8, "day": 11, "text": "A", "repeat": 0},
            {"year": 2026, "month": 8, "day": 11, "text": "B", "repeat": 1},
            {"year": 2026, "month": 8, "day": 11, "text": "C", "repeat": -1},
        ]
    )
    assert len(store.events_for_date(2026, 8, 11)) == 3
    assert len(store.events_for_date(2025, 8, 11)) == 1  # 仅 forever
    assert len(store.events_for_date(2027, 8, 11)) == 2  # B + forever
    assert len(store.events_for_date(2028, 8, 11)) == 1  # 仅 forever
    assert store.events_for_date(2030, 8, 11)[0]["text"] == "C"
