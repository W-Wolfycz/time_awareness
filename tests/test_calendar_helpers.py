import datetime

from time_awareness.commands.calendar_helpers import (
    find_event_by_short_id,
    parse_event_date,
    parse_year_month,
)
from time_awareness.core.calendar_store import CalendarStore


def test_calendar_command_parsers():
    now = datetime.datetime(2026, 7, 16)
    assert parse_year_month("2026-07", now) == (2026, 7)
    assert parse_year_month("bad", now) is None
    assert parse_event_date("2026-07-16") == (7, 16, 2026)
    assert parse_event_date("07-16") == (7, 16, None)


def test_find_event_supports_id_prefix_and_exact_text():
    store = CalendarStore()
    event = {"id": "event-demo-0001", "text": "演示日"}
    store.set_events([event])
    assert find_event_by_short_id(store, "event-demo") is event
    assert find_event_by_short_id(store, "演示日") is event
