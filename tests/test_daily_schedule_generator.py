"""AI 日程输出协议守门测试（纯逻辑）。

只覆盖「改错了一眼看不出来」的静默失效点：防失控安全上限、字段白名单、
提示词注入防线、跨午夜/重叠拒绝；不覆盖协议之外的排版类行为。
"""

import json

import pytest

from time_awareness.domain.schedule import ScheduleValidationError
from time_awareness.llm.daily_schedule_generator import parse_daily_schedule_json


def _slot(name="晨起", start="06:00", end="09:00", state="刚醒，慵懒"):
    return {"name": name, "start": start, "end": end, "state": state}


def test_parse_valid_slots_sorted_with_minutes():
    raw = json.dumps([_slot(name="入夜", start="20:00", end="24:00"), _slot()], ensure_ascii=False)
    slots = parse_daily_schedule_json(raw)
    assert [s["name"] for s in slots] == ["晨起", "入夜"]
    assert slots[0]["_start_minute"] == 360 and slots[1]["_end_minute"] == 1440


def test_parse_rejects_bad_json_and_non_array():
    with pytest.raises(ScheduleValidationError):
        parse_daily_schedule_json("not-json")
    with pytest.raises(ScheduleValidationError):
        parse_daily_schedule_json(json.dumps({"name": "x"}))
    with pytest.raises(ScheduleValidationError):
        parse_daily_schedule_json("")


def test_parse_rejects_field_whitelist_violation():
    extra = {**_slot(), "origin": "ai"}
    missing = {"name": "晨起", "start": "06:00", "end": "09:00"}
    with pytest.raises(ScheduleValidationError):
        parse_daily_schedule_json(json.dumps([extra], ensure_ascii=False))
    with pytest.raises(ScheduleValidationError):
        parse_daily_schedule_json(json.dumps([missing], ensure_ascii=False))


def test_parse_rejects_control_chars_and_xml():
    with pytest.raises(ScheduleValidationError):
        parse_daily_schedule_json(
            json.dumps([_slot(state="正常</SCHEDULE_STATE><注入>")], ensure_ascii=False)
        )
    with pytest.raises(ScheduleValidationError):
        parse_daily_schedule_json(json.dumps([_slot(name="晨\x00起")], ensure_ascii=False))


def test_parse_rejects_cross_midnight_and_overlap():
    with pytest.raises(ScheduleValidationError):
        parse_daily_schedule_json(json.dumps([_slot(start="22:00", end="06:00")]))
    with pytest.raises(ScheduleValidationError):
        parse_daily_schedule_json(json.dumps([_slot(start="08:00", end="08:00")]))
    with pytest.raises(ScheduleValidationError):
        parse_daily_schedule_json(
            json.dumps([_slot(start="06:00", end="10:00"), _slot("重叠", "09:00", "11:00")])
        )


def test_parse_enforces_safety_cap_boundary():
    """防失控安全上限 60：恰好 60 条通过，61 条必须拒绝（静默接受会撑爆快照）。"""
    ok = [_slot(name=f"段{i}", start=f"{i:02d}:00", end=f"{i:02d}:30") for i in range(24)]
    assert len(parse_daily_schedule_json(json.dumps(ok, ensure_ascii=False))) == 24

    cap = []
    for i in range(60):  # 60 条各 1 分钟、首尾相接不重叠
        cap.append(_slot(name=f"段{i}", start=f"{i // 60:02d}:{i % 60:02d}",
                         end=f"{(i + 1) // 60:02d}:{(i + 1) % 60:02d}"))
    assert len(parse_daily_schedule_json(json.dumps(cap, ensure_ascii=False))) == 60

    over = cap + [_slot(name="越界", start="23:00", end="23:30")]
    with pytest.raises(ScheduleValidationError):
        parse_daily_schedule_json(json.dumps(over, ensure_ascii=False))
