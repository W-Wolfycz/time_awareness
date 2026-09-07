"""core/calendar_store.py 与 core/builtin_events.py 的规则测试。"""

import datetime

from time_awareness.core.builtin_events import (
    CATEGORY_INTERNATIONAL,
    CATEGORY_POLITICAL,
    generate_for_year,
)
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


def test_events_for_date_builtin_exact_year():
    store = _store()
    store.set_builtin_events(
        [{"source": "builtin", "year": 2026, "month": 8, "day": 11, "text": "内置"}]
    )
    assert len(store.events_for_date(2026, 8, 11)) == 1
    assert store.events_for_date(2027, 8, 11) == []


def test_events_for_month_and_today_text():
    store = _store(
        [
            {"year": 2026, "month": 8, "day": 20, "text": "后", "repeat": 0},
            {"year": 2026, "month": 8, "day": 5, "text": "前", "repeat": 0},
            {"year": 2026, "month": 8, "day": 5, "text": "前", "repeat": 0},
        ],
        [{"source": "builtin", "year": 2026, "month": 8, "day": 11, "text": "内置"}],
    )
    assert [e["text"] for e in store.events_for_month(2026, 8)] == ["前", "内置", "后"]
    assert store.today_text(datetime.datetime(2026, 8, 11)) == "内置"


def test_generate_for_year_fields_and_bounds():
    events = generate_for_year(2026, [CATEGORY_POLITICAL, CATEGORY_INTERNATIONAL])
    assert {e["category"] for e in events} == {CATEGORY_POLITICAL, CATEGORY_INTERNATIONAL}
    for event in events:
        assert event["year"] == 2026 and event["source"] == "builtin"
        assert 1 <= event["month"] <= 12 and event["text"]
        assert event["id"].startswith("builtin:")


def test_generate_for_year_unknown_category_ignored():
    assert generate_for_year(2026, ["bogus"]) == []
    events = generate_for_year(2026, [CATEGORY_POLITICAL, "bogus"])
    assert {e["category"] for e in events} == {CATEGORY_POLITICAL}
