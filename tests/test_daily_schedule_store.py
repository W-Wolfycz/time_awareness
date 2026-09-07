"""core/daily_schedule_store.py 的隐私化持久化测试（tmp_path）。"""

import datetime

import pytest

from time_awareness.core._datafile import atomic_write_yaml
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


def test_snapshot_key_and_persona_hash(tmp_path):
    store = _store(tmp_path)
    store.load()
    assert store.snapshot_key("ph", "2026-08-11", "Asia/Shanghai") == "ph:2026-08-11:Asia/Shanghai"
    assert store.snapshot_key("ph", datetime.date(2026, 8, 11)) == "ph:2026-08-11:system-local"
    assert store.persona_hash("persona_demo") == store.persona_hash("persona_demo")
    assert store.persona_hash("persona_demo") != store.persona_hash("other")
    with pytest.raises(ValueError):
        store.persona_hash("")


def test_schema_version_mismatch_resets_store(tmp_path):
    atomic_write_yaml(
        str(tmp_path / "daily_schedule_snapshots.yaml"),
        {"schema_version": 999, "snapshots": {"k": {"status": "ready"}}},
    )
    store = _store(tmp_path)
    assert store.load() is True
    assert store.list_snapshots() == []
