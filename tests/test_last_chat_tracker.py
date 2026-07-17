import datetime

from time_awareness.core.last_chat_tracker import LastChatTracker


def test_record_is_buffered_until_flush(tmp_path):
    tracker = LastChatTracker(str(tmp_path))
    tracker.record_user(
        "demo:GroupMessage:group_demo",
        datetime.datetime(2026, 7, 16, 12, 0, tzinfo=datetime.timezone.utc),
    )
    path = tmp_path / "last_chat_times.yaml"
    assert not path.exists()
    assert tracker.flush() is True
    assert path.exists()
