"""core/daily_schedule_store.py 的隐私化持久化测试（tmp_path）。"""

import datetime
import pathlib


from time_awareness.core.daily_schedule_store import DailyScheduleSnapshotStore

TODAY = datetime.date(2026, 8, 11)


def _snapshot(local_date="2026-08-11"):
    return {
        "persona_hash": "persona_hash_1",
        "local_date": local_date,
        "timezone": "UTC",
        "snapshot_id": "snap1",
        "status": "ready",
        "slots": [{"name": "早", "start": "07:00", "end": "08:00"}],
    }


def _store(tmp_path):
    return DailyScheduleSnapshotStore(str(tmp_path))


def test_load_save_get_and_round_trip(tmp_path):
    store = _store(tmp_path)
    assert store.load() is True and store.list_snapshots() == []
    assert store.save_ready(_snapshot(), retention_days=30, today=TODAY) is True
    fetched = store.get("persona_hash_1", "2026-08-11", "UTC")
    fetched["slots"][0]["name"] = "改"
    assert store.get("persona_hash_1", "2026-08-11", "UTC")["slots"][0]["name"] == "早"

    reloaded = _store(tmp_path)
    reloaded.load()
    assert reloaded.get("persona_hash_1", "2026-08-11", "UTC")["snapshot_id"] == "snap1"


def test_record_failure(tmp_path):
    store = _store(tmp_path)
    store.load()
    store.save_ready(_snapshot(), retention_days=30, today=TODAY)
    ok = store.record_failure(
        persona_hash="persona_hash_1",
        local_date="2026-08-11",
        failed_at="x",
        error_type="llm_error",
        retention_days=30,
        today=TODAY,
        timezone="UTC",
        detail="boom",
    )
    assert ok is True
    failure = store.get_failure("persona_hash_1", "2026-08-11", "UTC")
    assert failure["error_type"] == "llm_error" and failure["detail"] == "boom"
    assert store.get("persona_hash_1", "2026-08-11", "UTC") is not None


def test_cleanup_retention_days(tmp_path):
    store = _store(tmp_path)
    store.load()
    store.save_ready(_snapshot(local_date="2026-08-08"), retention_days=30, today=TODAY)
    store.save_ready(_snapshot(local_date="2026-08-09"), retention_days=30, today=TODAY)
    assert store.cleanup(retention_days=3, today=TODAY) is True
    assert store.get("persona_hash_1", "2026-08-08", "UTC") is None
    assert store.get("persona_hash_1", "2026-08-09", "UTC") is not None


def test_snapshot_key_includes_timezone():
    """key 丢时区 → 同一 persona/日期在两个时区下互相串用快照，读写静默错位。"""
    store = _store(pathlib.Path("."))
    utc = store.snapshot_key("persona_hash_1", TODAY, "UTC")
    shifted = store.snapshot_key("persona_hash_1", TODAY, "UTC+08:00")
    other_day = store.snapshot_key("persona_hash_1", TODAY + datetime.timedelta(days=1), "UTC")
    assert utc != shifted and utc != other_day
