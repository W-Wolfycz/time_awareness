import datetime
from pathlib import Path

from time_awareness.core.daily_schedule_store import DailyScheduleSnapshotStore


def _snapshot(persona, date, snapshot_id="snapshot-a"):
    return {
        "snapshot_id": snapshot_id,
        "persona_hash": persona,
        "local_date": date.isoformat(),
        "timezone": "UTC",
        "status": "ready",
        "slots": [
            {
                "name": "全天",
                "start": "00:00",
                "end": "24:00",
                "state": "状态稳定",
            }
        ],
    }


def test_store_uses_stable_hmac_persona_and_does_not_persist_raw_id(tmp_path):
    raw_persona_id = "persona_private_demo"
    store = DailyScheduleSnapshotStore(str(tmp_path))
    assert store.load() is True
    persona = store.persona_hash(raw_persona_id)
    assert persona.startswith("persona_")
    assert raw_persona_id not in persona

    today = datetime.date(2026, 7, 28)
    assert store.save_ready(_snapshot(persona, today), retention_days=30, today=today)

    all_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in Path(tmp_path).iterdir()
        if path.is_file()
    )
    assert raw_persona_id not in all_text

    reloaded = DailyScheduleSnapshotStore(str(tmp_path))
    assert reloaded.load() is True
    assert reloaded.persona_hash(raw_persona_id) == persona
    assert reloaded.get(persona, today, "UTC")["snapshot_id"] == "snapshot-a"

def test_multiple_personas_persist_independent_snapshots(tmp_path):
    store = DailyScheduleSnapshotStore(str(tmp_path))
    assert store.load()
    today = datetime.date(2026, 8, 5)
    persona_a = store.persona_hash("persona_demo_a")
    persona_b = store.persona_hash("persona_demo_b")
    assert store.save_ready(
        _snapshot(persona_a, today, "snapshot-a"),
        retention_days=30,
        today=today,
    )
    assert store.save_ready(
        _snapshot(persona_b, today, "snapshot-b"),
        retention_days=30,
        today=today,
    )

    reloaded = DailyScheduleSnapshotStore(str(tmp_path))
    assert reloaded.load()
    assert reloaded.get(persona_a, today, "UTC")["snapshot_id"] == "snapshot-a"
    assert reloaded.get(persona_b, today, "UTC")["snapshot_id"] == "snapshot-b"


def test_failure_does_not_replace_ready_snapshot(tmp_path):
    store = DailyScheduleSnapshotStore(str(tmp_path))
    assert store.load()
    today = datetime.date(2026, 7, 28)
    persona = store.persona_hash("persona_demo")
    assert store.save_ready(_snapshot(persona, today), retention_days=30, today=today)
    assert store.record_failure(
        persona_hash=persona,
        local_date=today,
        failed_at="2026-07-28T08:00:00+00:00",
        error_type="invalid_output",
        retention_days=30,
        today=today,
        timezone="UTC",
    )
    assert store.get(persona, today, "UTC")["snapshot_id"] == "snapshot-a"
    assert store.get_failure(persona, today, "UTC")["error_type"] == "invalid_output"


def test_failed_atomic_switch_restores_old_in_memory_state(tmp_path, monkeypatch):
    store = DailyScheduleSnapshotStore(str(tmp_path))
    assert store.load()
    today = datetime.date(2026, 7, 28)
    persona = store.persona_hash("persona_demo")
    assert store.save_ready(_snapshot(persona, today), retention_days=30, today=today)

    monkeypatch.setattr(store, "_save", lambda: False)
    assert not store.save_ready(
        _snapshot(persona, today, snapshot_id="snapshot-b"),
        retention_days=30,
        today=today,
    )
    assert store.get(persona, today, "UTC")["snapshot_id"] == "snapshot-a"


def test_cleanup_removes_only_expired_records(tmp_path):
    store = DailyScheduleSnapshotStore(str(tmp_path))
    assert store.load()
    persona = store.persona_hash("persona_demo")
    today = datetime.date(2026, 7, 28)
    expired = today - datetime.timedelta(days=30)
    boundary = today - datetime.timedelta(days=29)
    assert store.save_ready(_snapshot(persona, expired, "expired"), retention_days=365, today=today)
    assert store.save_ready(_snapshot(persona, boundary, "boundary"), retention_days=365, today=today)
    assert store.save_ready(_snapshot(persona, today, "today"), retention_days=365, today=today)
    assert store.cleanup(retention_days=30, today=today)
    assert store.get(persona, expired, "UTC") is None
    assert store.get(persona, boundary, "UTC")["snapshot_id"] == "boundary"
    assert store.get(persona, today, "UTC")["snapshot_id"] == "today"


def test_mismatched_schema_is_ignored_on_load(tmp_path):
    """协议版本不一致时整体忽略，避免误读其他结构。"""
    store = DailyScheduleSnapshotStore(str(tmp_path))
    assert store.load()
    mismatched = (
        "schema_version: 6\n"  # 2.0.0 重置为 1，旧 v6 快照整体忽略
        "snapshots:\n"
        "  'bot_abc123:2026-07-28':\n"
        "    snapshot_id: old\n"
        "    local_date: '2026-07-28'\n"
        "    status: ready\n"
        "    slots: []\n"
        "failures: {}\n"
    )
    (tmp_path / "daily_schedule_snapshots.yaml").write_text(
        mismatched, encoding="utf-8"
    )

    reloaded = DailyScheduleSnapshotStore(str(tmp_path))
    assert reloaded.load()
    today = datetime.date(2026, 7, 28)
    assert reloaded.get("bot_abc123", today, "UTC") is None
    assert reloaded.get("persona_anything", today, "UTC") is None
    # 新的保存仍正常工作（新 schema 覆盖旧内容）
    persona = reloaded.persona_hash("persona_demo")
    assert reloaded.save_ready(_snapshot(persona, today), retention_days=30, today=today)
    assert reloaded.get(persona, today, "UTC")["snapshot_id"] == "snapshot-a"


def test_load_readonly_does_not_create_secret_and_enumerates(tmp_path):
    """查看页面只读加载：不创建 secret 文件，仍可枚举快照。"""
    store = DailyScheduleSnapshotStore(str(tmp_path))
    assert store.load()
    persona = store.persona_hash("persona_demo")
    today = datetime.date(2026, 7, 28)
    assert store.save_ready(_snapshot(persona, today), retention_days=30, today=today)
    secret_path = tmp_path / "daily_schedule_persona_secret.yaml"
    secret_path.unlink()

    fresh = DailyScheduleSnapshotStore(str(tmp_path))
    assert fresh.load_readonly()
    assert not secret_path.exists()  # 不创建 secret
    assert fresh.get(persona, today, "UTC") is not None
    by_persona = fresh.snapshots_by_persona()
    assert persona in by_persona
    assert by_persona[persona][0]["local_date"] == "2026-07-28"
    assert len(fresh.list_snapshots()) == 1


def test_enumeration_excludes_failures_and_non_ready(tmp_path):
    store = DailyScheduleSnapshotStore(str(tmp_path))
    assert store.load()
    persona = store.persona_hash("persona_demo")
    today = datetime.date(2026, 7, 28)
    assert store.save_ready(_snapshot(persona, today), retention_days=30, today=today)
    assert store.record_failure(
        persona_hash=persona,
        local_date=today,
        failed_at="2026-07-28T08:00:00+00:00",
        error_type="invalid_output",
        retention_days=30,
        today=today,
        timezone="UTC",
    )
    assert len(store.list_snapshots()) == 1
    assert store.list_snapshots()[0]["persona_hash"] == persona


