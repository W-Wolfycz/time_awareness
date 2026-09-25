"""domain/timeline.py 四层冻结时间线核心不变量（纯逻辑单测）。

只覆盖高风险不变量：合并优先级、冻结不回填、跨午夜、渲染回退、
用户覆盖裁剪与占位符卫生；完整行为覆盖见历史测试（git 34223ec）。
"""

from time_awareness.domain.schedule import normalize_static_slots, parse_time
from time_awareness.domain.timeline import (
    advance_executed,
    merge_future_layers,
    render_effective_timeline,
)


def _m(value):
    return parse_time(str(value), allow_24=True, strict=True)


def _slot(name, start, end, state="s", origin=None, source_origin=None):
    slot = {"name": name, "start": start, "end": end, "state": state}
    if origin:
        slot["origin"] = origin
    if source_origin:
        slot["source_origin"] = source_origin
    slot["_start_minute"] = _m(start)
    slot["_end_minute"] = _m(end)
    return slot


def _snapshot(**overrides):
    snapshot = {
        "snapshot_id": "snap-1",
        "executed_until": "00:00",
        "executed_slots": [],
        "user_slots": [],
        "ai_slots": [],
        "static_slots": [],
    }
    snapshot.update(overrides)
    return snapshot


# ==================== 未来层合并：优先级与相邻规则 ====================


def test_merge_priority_user_ai_static():
    """优先级 user > ai > static（static 优先默认），ai_priority 开关翻转 static/ai。"""
    user = [_slot("用户", "13:00", "15:00", origin="user")]
    ai = [
        _slot("AI段", "12:00", "14:00", origin="ai"),
        _slot("AI晚", "18:00", "20:00", origin="ai"),
    ]
    static = [_slot("静态午", "11:00", "13:00", origin="static")]

    merged = merge_future_layers(user, ai, static, ai_priority=False)
    by_origin = [(s["origin"], s["start"], s["end"]) for s in merged]
    assert ("static", "11:00", "13:00") in by_origin
    assert ("user", "13:00", "15:00") in by_origin
    assert ("ai", "18:00", "20:00") in by_origin
    assert ("ai", "12:00", "13:00") not in by_origin  # 被静态占满

    merged_ai = merge_future_layers(user, ai, static, ai_priority=True)
    by_origin_ai = [(s["origin"], s["start"], s["end"]) for s in merged_ai]
    assert ("ai", "12:00", "13:00") in by_origin_ai
    assert ("static", "11:00", "12:00") in by_origin_ai  # 13-14 被用户占


def test_merge_remerges_split_and_keeps_gaps():
    """同源被裁剪出的相邻碎片重新合并；不同来源相邻不合并；空档保留。"""
    user = [_slot("用户", "12:30", "13:00")]
    ai = [_slot("AI段", "12:00", "14:00", origin="ai")]
    merged = merge_future_layers(user, ai, [], ai_priority=False)
    ai_parts = sorted((s["start"], s["end"]) for s in merged if s["origin"] == "ai")
    assert ai_parts == [("12:00", "12:30"), ("13:00", "14:00")]

    merged2 = merge_future_layers(
        [], ai, [_slot("静态", "12:00", "12:30", origin="static")], ai_priority=False
    )
    assert [s["origin"] for s in merged2] == ["static", "ai"]

    gapped = merge_future_layers(
        [],
        [_slot("A", "08:00", "10:00", origin="ai"), _slot("B", "14:00", "16:00", origin="ai")],
        [],
        ai_priority=False,
    )
    assert [(s["start"], s["end"]) for s in gapped] == [
        ("08:00", "10:00"),
        ("14:00", "16:00"),
    ]


# ==================== 冻结边界推进 ====================


def test_advance_freezes_executed_and_never_backfills():
    """推进到 cutoff：cutoff 前全部固化、空档不回填、未来层被裁剪。"""
    snapshot = _snapshot(
        ai_slots=[_slot("AI", "08:00", "10:00", origin="ai")],
        static_slots=[_slot("静态", "09:00", "09:30", origin="static")],
    )
    updated = advance_executed(snapshot, cutoff=540, ai_priority=False)
    frozen = updated["executed_slots"]
    assert all(_m(s["start"]) < 540 and _m(s["end"]) <= 540 for s in frozen)
    ai_parts = [s for s in frozen if s.get("_source_origin") == "ai"]
    assert [(_m(s["start"]), _m(s["end"])) for s in ai_parts] == [(480, 540)]
    assert not any(s.get("_source_origin") == "static" for s in frozen)
    assert updated["executed_until"] == "09:00"
    assert [(_m(s["start"]), _m(s["end"])) for s in updated["ai_slots"]] == [(540, 600)]
    assert [(_m(s["start"]), _m(s["end"])) for s in updated["static_slots"]] == [(540, 570)]


def test_advance_holds_executed_against_user_and_static():
    """已执行区间恒占：用户/AI/静态无法覆盖冻结区。"""
    snapshot = _snapshot(
        executed_slots=[
            {**_slot("已执行", "08:00", "10:00", origin="executed"), "_source_origin": "ai"}
        ],
        user_slots=[_slot("用户", "08:30", "09:30", origin="user")],
        ai_slots=[_slot("AI", "08:00", "09:00", origin="ai")],
    )
    updated = advance_executed(snapshot, cutoff=600, ai_priority=False)
    frozen = updated["executed_slots"]
    assert sorted((_m(s["start"]), _m(s["end"])) for s in frozen) == [(480, 600)]
    assert all(s["origin"] == "executed" for s in frozen)


def test_advance_strips_internal_placeholder_marker():
    """内部占位标记不落入持久化 executed_slots 与渲染结果。"""
    snapshot = _snapshot(
        executed_slots=[
            {**_slot("已冻结", "07:00", "08:00", origin="executed"), "_source_origin": "ai"}
        ],
    )
    updated = advance_executed(snapshot, cutoff=700, ai_priority=False)
    assert updated["executed_slots"]
    assert all("_executed" not in s for s in updated["executed_slots"])
    rendered = render_effective_timeline(
        {**snapshot, **updated}, cutoff=700, current_static=[], ai_priority=False
    )
    assert all("_executed" not in s for s in rendered)


# ==================== 动态渲染 ====================


def test_render_user_overrides_ai_with_executed_and_static():
    """渲染 = executed + 静态 + 用户覆盖 AI 的未来层。"""
    snapshot = _snapshot(
        executed_until="09:00",
        executed_slots=[
            _slot("已执行", "08:00", "09:00", origin="executed", source_origin="ai")
        ],
        user_slots=[_slot("用户", "12:00", "13:00", origin="user")],
        ai_slots=[_slot("AI午", "12:30", "14:00", origin="ai")],
    )
    slots = render_effective_timeline(
        snapshot,
        cutoff=540,
        current_static=[_slot("静态", "11:00", "11:30", origin="static")],
        ai_priority=False,
    )
    by_origin = [(s["origin"], s["start"], s["end"]) for s in slots]
    assert ("executed", "08:00", "09:00") in by_origin
    assert ("static", "11:00", "11:30") in by_origin
    assert ("user", "12:00", "13:00") in by_origin
    assert ("ai", "13:00", "14:00") in by_origin


def test_render_does_not_backfill_frozen_gap():
    """cutoff 之前的空白是已确定事实：AI 不得回填。"""
    snapshot = _snapshot(
        executed_until="10:00",
        executed_slots=[
            _slot("已执行", "08:00", "09:00", origin="executed", source_origin="ai")
        ],
        ai_slots=[_slot("AI", "08:30", "10:30", origin="ai")],
    )
    slots = render_effective_timeline(
        snapshot, cutoff=600, current_static=[], ai_priority=False
    )
    assert [(s["start"], s["end"]) for s in slots if s["origin"] == "ai"] == [
        ("10:00", "10:30")
    ]


def test_render_empty_ai_with_static_still_renders_static():
    """合法空 AI 快照仍渲染静态层（空 AI ≠ 无日程）。"""
    snapshot = _snapshot(ai_slots=[], user_slots=[])
    slots = render_effective_timeline(
        snapshot,
        cutoff=600,
        current_static=[_slot("静态午", "12:00", "13:00", origin="static")],
        ai_priority=False,
    )
    assert [(s["origin"], s["start"], s["end"]) for s in slots] == [
        ("static", "12:00", "13:00")
    ]


# ==================== 用户覆盖裁剪与跨午夜 ====================


def test_cross_midnight_merge_advance_render():
    """跨午夜静态段在存储边界拆分：合并、冻结、渲染三处一致。"""
    static = normalize_static_slots(
        [{"name": "夜间", "start_time": "22:00", "end_time": "08:00", "origin": "static"}]
    )
    merged = merge_future_layers([], [], static, ai_priority=False)
    parts = [(s["origin"], s["start"], s["end"]) for s in merged]
    assert ("static", "22:00", "24:00") in parts
    assert ("static", "00:00", "08:00") in parts

    snapshot = _snapshot(
        static_slots=[
            {"name": "夜间", "start_time": "22:00", "end_time": "08:00", "origin": "static"}
        ]
    )
    updated = advance_executed(snapshot, cutoff=23 * 60, ai_priority=False)
    frozen = sorted(
        (s["_start_minute"], s["_end_minute"]) for s in updated["executed_slots"]
    )
    assert frozen == [(0, 480), (1320, 1380)]
    assert [(_m(s["start"]), _m(s["end"])) for s in updated["static_slots"]] == [
        (1380, 1440)
    ]

    rendered = render_effective_timeline(
        _snapshot(),
        cutoff=0,
        current_static=[
            {"name": "夜间", "start_time": "22:00", "end_time": "08:00", "origin": "static"}
        ],
        ai_priority=False,
    )
    parts = [(s["origin"], s["start"], s["end"]) for s in rendered]
    assert ("static", "00:00", "08:00") in parts
    assert ("static", "22:00", "24:00") in parts
