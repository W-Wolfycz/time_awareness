"""分层日程时间线领域算法测试（纯逻辑）。"""

import datetime

from time_awareness.domain.timeline import (
    advance_executed,
    clip_slots,
    merge_future_layers,
    next_minute_cutoff,
    render_effective_timeline,
    subtract_slots,
)


def _slot(name, start, end, state="s", origin=None, source_origin=None):
    slot = {"name": name, "start": start, "end": end, "state": state}
    if origin:
        slot["origin"] = origin
    if source_origin:
        slot["source_origin"] = source_origin
    from time_awareness.domain.schedule import format_time, parse_time

    slot["_start_minute"] = parse_time(start, allow_24=False, strict=True)
    slot["_end_minute"] = parse_time(end, allow_24=True, strict=True)
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


# ==================== 冻结边界 ====================


def test_cutoff_next_whole_minute_and_stable():
    now = datetime.datetime(2026, 8, 9, 17, 31, 24, 123456)
    assert next_minute_cutoff(now) == 17 * 60 + 32
    exact = datetime.datetime(2026, 8, 9, 17, 31, 0, 0)
    assert next_minute_cutoff(exact) == 17 * 60 + 31
    edge = datetime.datetime(2026, 8, 9, 23, 59, 30)
    assert next_minute_cutoff(edge) == 1440  # 24:00


# ==================== 裁剪 / 相减 ====================


def test_clip_slots():
    slots = [_slot("全天", "00:00", "24:00")]
    clipped = clip_slots(slots, 60, 120)
    assert len(clipped) == 1
    assert clipped[0]["start"] == "01:00" and clipped[0]["end"] == "02:00"
    assert clip_slots(slots, 100, 200)[0]["start"] == "01:40"


def test_subtract_slots_splits_and_remerges_same_source():
    base = [_slot("午间", "12:00", "14:00")]
    blockers = [_slot("用户", "12:30", "13:30")]
    result = subtract_slots(base, blockers)
    assert [(s["start"], s["end"]) for s in result] == [("12:00", "12:30"), ("13:30", "14:00")]
    # 完全覆盖 → 空
    assert subtract_slots(base, [_slot("用户", "12:00", "14:00")]) == []
    # 无重叠 → 原样
    assert len(subtract_slots(base, [_slot("用户", "09:00", "10:00")])) == 1


# ==================== 未来层合并 ====================


def test_merge_user_first_then_priority_order():
    user = [_slot("用户", "13:00", "15:00", origin="user")]
    ai = [_slot("AI段", "12:00", "14:00", origin="ai"), _slot("AI晚", "18:00", "20:00", origin="ai")]
    static = [_slot("静态午", "11:00", "13:00", origin="static")]

    # static 优先（默认）：静态 11-13 占满（AI 12-13 无法填入）、用户 13-15、AI 18-20
    merged = merge_future_layers(user, ai, static, ai_priority=False)
    by_origin = [(s["origin"], s["start"], s["end"]) for s in merged]
    assert ("static", "11:00", "13:00") in by_origin
    assert ("user", "13:00", "15:00") in by_origin
    assert ("ai", "18:00", "20:00") in by_origin
    assert ("ai", "12:00", "13:00") not in by_origin  # 被静态占满

    # ai 优先：AI 12-13、静态 11-12（13-14 被用户占，静态只余 11-12）
    merged_ai = merge_future_layers(user, ai, static, ai_priority=True)
    by_origin_ai = [(s["origin"], s["start"], s["end"]) for s in merged_ai]
    assert ("ai", "12:00", "13:00") in by_origin_ai
    assert ("static", "11:00", "12:00") in by_origin_ai


def test_merge_remerges_split_adjacent_same_source():
    user = [_slot("用户", "12:30", "13:00")]
    ai = [_slot("AI段", "12:00", "14:00", origin="ai")]
    merged = merge_future_layers(user, ai, [], ai_priority=False)
    ai_parts = [(s["start"], s["end"]) for s in merged if s["origin"] == "ai"]
    assert ("12:00", "12:30") in ai_parts
    assert ("13:00", "14:00") in ai_parts
    # 不同来源相邻不合并
    merged2 = merge_future_layers([], ai, [_slot("静态", "12:00", "12:30", origin="static")], ai_priority=False)
    origins = [s["origin"] for s in merged2]
    assert origins == ["static", "ai"]  # 相邻但不同来源 → 两段


def test_merge_keeps_gaps():
    ai = [_slot("A", "08:00", "10:00", origin="ai"), _slot("B", "14:00", "16:00", origin="ai")]
    merged = merge_future_layers([], ai, [], ai_priority=False)
    # 空档 10:00-14:00 保留：两段边界精确、不被填充延伸
    assert [(s["start"], s["end"]) for s in merged] == [
        ("08:00", "10:00"),
        ("14:00", "16:00"),
    ]


# ==================== 推进冻结边界 ====================


def test_advance_freeze_executed_and_never_backfill():
    snapshot = _snapshot(
        ai_slots=[_slot("AI", "08:00", "10:00", origin="ai")],
        static_slots=[_slot("静态", "09:00", "09:30", origin="static")],
    )
    updated = advance_executed(snapshot, cutoff=540, ai_priority=False)
    # [00:00, 09:00) 已冻结：所有段完全落在冻结区内（end == cutoff 为合法端点）
    frozen = updated["executed_slots"]
    assert frozen
    assert all(_m(s["start"]) < 540 and _m(s["end"]) <= 540 for s in frozen)
    # 冻结部分：AI 08:00-09:00（被裁剪到 cutoff）；静态 09:00-09:30 起点
    # 在 cutoff 上，属未来层，不得被冻结（冻结空档不可回填的镜像断言）
    ai_parts = [s for s in frozen if s.get("_source_origin") == "ai"]
    assert [(_m(s["start"]), _m(s["end"])) for s in ai_parts] == [(480, 540)]
    assert not any(s.get("_source_origin") == "static" for s in frozen)
    # 空白区间同样冻结：executed_until 到达 cutoff
    assert updated["executed_until"] == "09:00"
    # 未来层裁剪：AI 只剩 cutoff 之后的部分（09:00-10:00）
    assert [(_m(s["start"]), _m(s["end"])) for s in updated["ai_slots"]] == [(540, 600)]
    # 静态层同样裁剪：09:00-09:30 从 cutoff 起保留
    assert [(_m(s["start"]), _m(s["end"])) for s in updated["static_slots"]] == [(540, 570)]


def test_advance_executed_holds_against_user_and_static():
    """已执行区间不会被用户/静态/AI 覆盖。"""
    snapshot = _snapshot(
        executed_slots=[{
            **_slot("已执行", "08:00", "10:00", origin="executed"),
            "_source_origin": "ai",
        }],
        user_slots=[_slot("用户", "08:30", "09:30", origin="user")],
        ai_slots=[_slot("AI", "08:00", "09:00", origin="ai")],
    )
    updated = advance_executed(snapshot, cutoff=600, ai_priority=False)
    frozen = updated["executed_slots"]
    # 08:00-10:00 全部为已执行（executed 恒占，用户/AI 无法覆盖）
    covered = sorted((_m(s["start"]), _m(s["end"])) for s in frozen)
    assert covered == [(480, 600)]
    assert all(s["origin"] == "executed" for s in frozen)
    assert frozen[0]["_source_origin"] == "ai"


def test_advance_freezes_snapshot_static_copy():
    """推进固化使用快照内的 static_slots 副本（旧数据不因渲染而改写）。"""
    snapshot = _snapshot(
        static_slots=[_slot("旧静态", "08:00", "09:00", origin="static")],
        ai_slots=[],
    )
    updated = advance_executed(snapshot, cutoff=540, ai_priority=False)
    frozen = updated["executed_slots"]
    static_parts = [s for s in frozen if s.get("_source_origin") == "static"]
    # 快照内静态副本 08:00-09:00 固化到冻结边界
    assert [(_m(s["start"]), _m(s["end"])) for s in static_parts] == [(480, 540)]


# ==================== 动态渲染 ====================


def test_render_effective_timeline():
    snapshot = _snapshot(
        executed_until="09:00",
        executed_slots=[
            _slot("已执行", "08:00", "09:00", origin="executed", source_origin="ai")
        ],
        user_slots=[_slot("用户", "12:00", "13:00", origin="user")],
        ai_slots=[_slot("AI午", "12:30", "14:00", origin="ai")],
    )
    current_static = [_slot("静态", "11:00", "11:30", origin="static")]
    slots = render_effective_timeline(snapshot, cutoff=540, current_static=current_static, ai_priority=False)
    by_origin = [(s["origin"], s["start"], s["end"]) for s in slots]
    assert ("executed", "08:00", "09:00") in by_origin
    assert ("static", "11:00", "11:30") in by_origin
    # 用户 12:00-13:00 覆盖 AI 12:30-13:00 → AI 只剩 13:00-14:00
    assert ("user", "12:00", "13:00") in by_origin
    assert ("ai", "13:00", "14:00") in by_origin
    executed = next(s for s in slots if s["origin"] == "executed")
    assert executed["source_origin"] == "ai"


def test_render_does_not_backfill_frozen_gap():
    """cutoff 之前的空白是已确定事实，后续 AI/静态不得回填。"""
    snapshot = _snapshot(
        executed_until="10:00",
        executed_slots=[_slot("已执行", "08:00", "09:00", origin="executed", source_origin="ai")],
        ai_slots=[_slot("AI", "08:30", "10:30", origin="ai")],
    )
    slots = render_effective_timeline(snapshot, cutoff=600, current_static=[], ai_priority=False)
    ai_parts = [(s["start"], s["end"]) for s in slots if s["origin"] == "ai"]
    assert ai_parts == [("10:00", "10:30")]  # 09:00-10:00 空档不回填


def _m(value):
    from time_awareness.domain.schedule import parse_time

    return parse_time(str(value), allow_24=True, strict=True)


# ==================== 合法空 AI 日程渲染 ====================


def test_render_empty_ai_with_static_still_renders_static():
    """空 AI 层不使快照无效：静态层按当前配置正常渲染。"""
    snapshot = _snapshot(ai_slots=[], user_slots=[])
    current_static = [_slot("静态午", "12:00", "13:00", origin="static")]
    slots = render_effective_timeline(snapshot, cutoff=600, current_static=current_static, ai_priority=False)
    assert [(s["origin"], s["start"], s["end"]) for s in slots] == [
        ("static", "12:00", "13:00")
    ]


def test_render_accepts_lenient_hmm_static_times():
    """用户手填 H:MM（8:00）静态配置在渲染层不静默丢弃（2.0.0 §7 容错）。"""
    snapshot = _snapshot(ai_slots=[], user_slots=[])
    # 直接构造无 _start_minute 的原始配置形态（_current_static_slots 产物）
    current_static = [{
        "name": "午段", "start": "9:00", "end": "11:00",
        "state": "s", "origin": "static",
    }]
    slots = render_effective_timeline(snapshot, cutoff=480, current_static=current_static, ai_priority=False)
    assert [(s["origin"], s["start"], s["end"]) for s in slots] == [
        ("static", "09:00", "11:00")
    ]


def test_render_all_layers_empty_keeps_gap():
    """用户/AI/静态层全空时保持空档，不产生任何时段。"""
    snapshot = _snapshot(ai_slots=[], user_slots=[], static_slots=[])
    assert render_effective_timeline(snapshot, cutoff=600, current_static=[], ai_priority=False) == []


def test_advance_empty_layers_freeze_gap_only():
    """全空快照推进冻结边界：仅固化空白，不产生时段。"""
    updated = advance_executed(_snapshot(), cutoff=540, ai_priority=False)
    assert updated["executed_slots"] == []
    assert updated["executed_until"] == "09:00"


def test_advance_merges_adjacent_same_source_fragments():
    """多次推进后同源相邻 executed 段合并，不产生分钟级碎片。"""
    from time_awareness.domain.schedule import format_time

    snapshot = _snapshot(
        user_slots=[{
            **_slot("用户安排", "10:00", "12:00", origin="user"),
            "user_slot_id": "u1",
        }],
    )
    for cutoff in (601, 602, 603, 604):
        updated = advance_executed(snapshot, cutoff, ai_priority=False)
        snapshot = {**snapshot, **updated}
        # 模拟保存尾段：起点推进到最新冻结点，id 保留
        snapshot["user_slots"] = [{
            "name": "用户安排", "start": format_time(cutoff), "end": "12:00",
            "state": "s", "origin": "user", "user_slot_id": "u1",
            "_start_minute": cutoff, "_end_minute": 720,
        }]
    # 4 次保存后 executed_slots 只有一段 10:00-10:04（同源合并）
    assert [(_m(s["start"]), _m(s["end"])) for s in snapshot["executed_slots"]] == [
        (600, 604)
    ]
    assert snapshot["executed_slots"][0]["user_slot_id"] == "u1"


def test_advance_strips_internal_placeholder_marker():
    """内部 `_executed` 占位标记不落入持久化 executed_slots。"""
    snapshot = _snapshot(
        executed_slots=[{
            **_slot("已冻结", "07:00", "08:00", origin="executed"),
            "_source_origin": "ai",
        }],
    )
    updated = advance_executed(snapshot, cutoff=700, ai_priority=False)
    assert updated["executed_slots"]
    assert all("_executed" not in s for s in updated["executed_slots"])
    rendered = render_effective_timeline(
        {**snapshot, **updated}, cutoff=700, current_static=[], ai_priority=False
    )
    assert all("_executed" not in s for s in rendered)


def test_advance_does_not_merge_different_sources_or_slots():
    """不同来源或不同业务时段相邻不合并。"""
    snapshot = _snapshot(
        executed_slots=[
            {
                **_slot("已执行A", "08:00", "09:00", origin="executed"),
                "_source_origin": "ai",
            },
            {
                **_slot("已执行B", "09:00", "10:00", origin="executed"),
                "_source_origin": "static",
            },
        ],
    )
    updated = advance_executed(snapshot, 600, ai_priority=False)
    covered = [(_m(s["start"]), _m(s["end"])) for s in updated["executed_slots"]]
    assert covered == [(480, 540), (540, 600)]  # 相邻但不同来源 → 两段


# ==================== 正在进行时段合并（Q4） ====================


def test_merge_active_slot_fragments_joins_cross_now_same_source():
    """渲染推进把当前时段切成 executed 前段 + future 尾段：合并回一段。"""
    from time_awareness.domain.timeline import merge_active_slot_fragments

    slots = [
        {"origin": "executed", "source_origin": "ai", "name": "任务",
         "start": "14:00", "end": "15:00", "state": "专注",
         "_start_minute": 840, "_end_minute": 900},
        {"origin": "ai", "source_origin": "ai", "name": "任务",
         "start": "15:00", "end": "16:00", "state": "专注",
         "_start_minute": 900, "_end_minute": 960},
    ]
    merged = merge_active_slot_fragments(slots, now_minute=900)
    assert len(merged) == 1
    assert merged[0]["origin"] == "ai"
    assert merged[0]["start"] == "14:00"
    assert merged[0]["_start_minute"] == 840


def test_merge_active_slot_fragments_does_not_join_unrelated():
    """非当前时刻的邻接段（不同源/不同名/不跨 now）不合并。"""
    from time_awareness.domain.timeline import merge_active_slot_fragments

    # 不同来源：不合并
    slots = [
        {"origin": "executed", "source_origin": "static", "name": "A",
         "start": "14:00", "end": "15:00", "state": "s",
         "_start_minute": 840, "_end_minute": 900},
        {"origin": "ai", "source_origin": "ai", "name": "A",
         "start": "15:00", "end": "16:00", "state": "s",
         "_start_minute": 900, "_end_minute": 960},
    ]
    assert len(merge_active_slot_fragments(slots, now_minute=900)) == 2
    # 不跨越当前时刻（now 在段外）：不合并
    slots2 = [
        {"origin": "executed", "source_origin": "ai", "name": "A",
         "start": "10:00", "end": "11:00", "state": "s",
         "_start_minute": 600, "_end_minute": 660},
        {"origin": "ai", "source_origin": "ai", "name": "A",
         "start": "11:00", "end": "12:00", "state": "s",
         "_start_minute": 660, "_end_minute": 720},
    ]
    assert len(merge_active_slot_fragments(slots2, now_minute=900)) == 2
