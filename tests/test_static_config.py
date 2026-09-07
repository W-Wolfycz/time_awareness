"""services/static_schedule_config_service.py 的读写与并发冲突测试。"""

import asyncio

import pytest

from time_awareness.services.static_schedule_config_service import (
    StaticScheduleConfigConflict,
    StaticScheduleConfigService,
    static_schedule_revision,
)


def _service(config, save=None):
    return StaticScheduleConfigService(config=config, save_config=save or (lambda: True))


def test_static_schedule_revision():
    slots = [{"start_time": "08:00", "end_time": "09:00"}]
    assert static_schedule_revision(slots) == static_schedule_revision(slots)
    assert len(static_schedule_revision(slots)) == 64
    assert static_schedule_revision(slots) != static_schedule_revision(
        [{"start_time": "09:00", "end_time": "10:00"}]
    )


def test_service_get_shape():
    config = {
        "daily_schedule": {
            "schedule_templates": [{"name": "x", "start_time": "08:00", "end_time": "09:00"}]
        }
    }
    result = _service(config).get()
    assert set(result) == {"revision", "slots", "max_slots", "warnings"}
    assert result["max_slots"] == 256 and result["slots"][0]["start_time"] == "08:00"
    assert isinstance(result["warnings"], list)


def test_save_writes_config_and_returns_revision():
    config = {}
    saved = []
    svc = StaticScheduleConfigService(config=config, save_config=lambda: saved.append(1) or True)
    rev = svc.get()["revision"]
    result = asyncio.run(
        svc.save(revision=rev, slots=[{"name": "x", "start_time": "08:00", "end_time": "09:00"}])
    )
    assert saved == [1]
    written = config["daily_schedule"]["schedule_templates"]
    assert written[0]["start_time"] == "08:00"
    assert result["revision"] == static_schedule_revision(written)


def test_save_error_branches():
    config = {
        "daily_schedule": {
            "schedule_templates": [{"name": "x", "start_time": "08:00", "end_time": "09:00"}]
        }
    }
    with pytest.raises(StaticScheduleConfigConflict):
        asyncio.run(_service(config).save(revision="stale", slots=[]))

    svc = _service({})
    rev = svc.get()["revision"]
    with pytest.raises(ValueError):
        slot = {"name": "x", "start_time": "08:00", "end_time": "08:00"}
        asyncio.run(svc.save(revision=rev, slots=[slot]))
