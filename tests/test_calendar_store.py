from time_awareness.core.calendar_store import CalendarStore


def test_calendar_store_instances_are_isolated():
    first = CalendarStore()
    second = CalendarStore()
    first.set_events([{"id": "event-demo"}])
    assert first.events == [{"id": "event-demo"}]
    assert second.events == []
