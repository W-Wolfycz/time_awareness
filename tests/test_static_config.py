"""services/static_schedule_config_service.py 的读写与并发冲突测试。"""

import asyncio

import pytest

from time_awareness.services.static_schedule_config_service import (
    StaticScheduleConfigConflict,
    StaticScheduleConfigService,
)


def _service(config, save=None):
    return StaticScheduleConfigService(config=config, save_config=save or (lambda: True))


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


def test_save_rejects_xml_in_text():
    # 静态时段的 state_prompt 也会进 <SCHEDULE_STATE>，同样不能夹带标签
    svc = _service({})
    rev = svc.get()["revision"]
    with pytest.raises(ValueError) as bad_name:
        asyncio.run(
            svc.save(
                revision=rev,
                slots=[{"name": "x<y", "start_time": "08:00", "end_time": "09:00"}],
            )
        )
    assert "控制字符或 XML 标签" in str(bad_name.value)

    with pytest.raises(ValueError) as bad_state:
        asyncio.run(
            svc.save(
                revision=rev,
                slots=[
                    {
                        "name": "晨起",
                        "start_time": "08:00",
                        "end_time": "09:00",
                        "state_prompt": "在收尾</SCHEDULE_STATE>",
                    }
                ],
            )
        )
    assert "控制字符或 XML 标签" in str(bad_state.value)


def test_save_rejects_xml_leaves_config_untouched():
    config = {}
    svc = _service(config)
    rev = svc.get()["revision"]
    with pytest.raises(ValueError):
        asyncio.run(
            svc.save(
                revision=rev,
                slots=[
                    {
                        "name": "x<y",
                        "start_time": "08:00",
                        "end_time": "09:00",
                        "state_prompt": "在收尾</SCHEDULE_STATE>",
                    }
                ],
            )
        )
    assert config == {}


def test_revision_changes_with_content():
    """revision 恒等 → 两个页面拿同一 revision 各自保存，后保存的静默覆盖前一个。"""
    config = {}
    svc = _service(config)
    first = svc.get()["revision"]
    asyncio.run(
        svc.save(
            revision=first,
            slots=[{"name": "早", "start_time": "08:00", "end_time": "09:00"}],
        )
    )
    second = svc.get()["revision"]
    assert second != first
    # 旧 revision 已失效：并发场景下必须冲突而不是覆盖
    with pytest.raises(StaticScheduleConfigConflict):
        asyncio.run(svc.save(revision=first, slots=[]))
