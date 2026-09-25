"""分层日程时间线领域算法：[00:00,cutoff) 已冻结（含空白），[cutoff,24:00) 按来源层动态合并。"""

from __future__ import annotations

from typing import Any, Iterable

from .schedule import (
    MINUTES_PER_DAY,
    ScheduleValidationError,
    format_time,
    normalize_static_slots,
    slot_minutes,
    split_cross_midnight,
)


def next_minute_cutoff(now: Any) -> int:
    """冻结边界：下一整分钟（非整分钟向上取整，恰在整分钟保持不变）。"""
    if now.second == 0 and now.microsecond == 0:
        return now.hour * 60 + now.minute
    return now.hour * 60 + now.minute + 1


def _copy_with_minutes(slot: dict, start: int, end: int) -> dict:
    fragment = dict(slot)
    fragment["start"] = format_time(start)
    fragment["end"] = format_time(end)
    fragment["_start_minute"] = start
    fragment["_end_minute"] = end
    return fragment


def _day_segments(start: int, end: int) -> list[tuple[int, int]]:
    """返回槽位在当天内的非跨日区间（跨午夜拆两段，与 split_cross_midnight 共用实现）。"""
    try:
        return list(split_cross_midnight(start, end))
    except ScheduleValidationError:
        return []


def clip_slots(
    slots: Iterable[dict],
    start_minute: int,
    end_minute: int,
) -> list[dict]:
    """裁剪时段到 ``[start_minute, end_minute)``；保留来源字段。"""
    result = []
    for slot in slots or []:
        if not isinstance(slot, dict):
            continue
        try:
            start, end = slot_minutes(slot)
        except (ScheduleValidationError, ValueError):
            continue
        ranges = _day_segments(start, end)
        for range_start, range_end in ranges:
            if range_end <= start_minute or range_start >= end_minute:
                continue
            clip_start = max(range_start, start_minute)
            clip_end = min(range_end, end_minute)
            if clip_start >= clip_end:
                continue
            result.append(_copy_with_minutes(slot, clip_start, clip_end))
    return result


def merge_future_layers(
    user: Iterable[dict],
    ai: Iterable[dict],
    static: Iterable[dict],
    ai_priority: bool,
) -> list[dict]:
    """合并未来区间：用户先占位，AI 与静态按 ai_priority 决定先后填充；返回带 origin 的时段。"""
    timeline: list[tuple[str, int, dict] | None] = [None] * MINUTES_PER_DAY

    def place(slots: Iterable[dict], origin: str, prefix: str) -> None:
        for index, slot in enumerate(slots or []):
            if not isinstance(slot, dict):
                continue
            try:
                start, end = slot_minutes(slot)
            except (ScheduleValidationError, ValueError):
                continue
            # 跨午夜（end < start）拆两段占位；同源两段合并时视为同一 token
            token = (origin, f"{prefix}{index}")
            ranges = _day_segments(start, end)
            for range_start, range_end in ranges:
                for minute in range(range_start, min(range_end, MINUTES_PER_DAY)):
                    if timeline[minute] is None:
                        timeline[minute] = (origin, token, slot)

    place(user, "user", "u")
    if ai_priority:
        place(ai, "ai", "a")
        place(static, "static", "s")
    else:
        place(static, "static", "s")
        place(ai, "ai", "a")

    merged: list[dict] = []
    current_token: tuple[str, str] | None = None
    segment_start = 0
    segment_slot: dict | None = None
    for minute in range(MINUTES_PER_DAY + 1):
        entry = timeline[minute] if minute < MINUTES_PER_DAY else None
        if entry is not None:
            origin, token, slot = entry
            if token == current_token:
                continue  # 延长当前段
        if current_token is not None and segment_slot is not None:
            segment = _copy_with_minutes(
                segment_slot, segment_start, minute
            )
            segment["origin"] = current_token[0]
            merged.append(segment)
        if entry is not None and minute < MINUTES_PER_DAY:
            origin, token, slot = entry
            current_token = token
            segment_start = minute
            segment_slot = slot
        else:
            current_token = None
            segment_slot = None
    return merged


def advance_executed(
    snapshot: dict,
    cutoff: int,
    ai_priority: bool,
) -> dict:
    """推进冻结边界：固化 [00:00,cutoff) 有效时间线；用旧 static_slots 副本，不反写历史。"""
    cutoff = max(0, min(MINUTES_PER_DAY, int(cutoff)))
    executed = [s for s in snapshot.get("executed_slots", []) if isinstance(s, dict)]
    user = [s for s in snapshot.get("user_slots", []) if isinstance(s, dict)]
    ai = [s for s in snapshot.get("ai_slots", []) if isinstance(s, dict)]
    # 静态层在存储边界规范化：跨午夜拆两段（静态槽位进入四层 timeline 的唯一入口）。
    old_static = normalize_static_slots(snapshot.get("static_slots", []))

    # [00:00, cutoff) 有效时间线：executed 恒占（最高优先级）→ 旧 user/ai/static 按优先级填剩余
    executed_placeholders = [
        {
            "_executed": True,
            **s,
            "origin": "executed",
            "_source_origin": s.get(
                "_source_origin", s.get("source_origin", s.get("origin", "ai"))
            ),
        }
        for s in executed
    ]
    history = merge_future_layers(
        executed_placeholders + user,
        ai,
        old_static,
        ai_priority,
    )
    frozen = []
    for slot in history:
        start, end = slot_minutes(slot)
        if start >= cutoff:
            continue
        clipped = _copy_with_minutes(slot, start, min(end, cutoff))
        clipped["origin"] = "executed"
        clipped["_source_origin"] = slot.get("_source_origin", slot.get("origin", "ai"))
        # 内部占位标记不落入持久化数据
        clipped.pop("_executed", None)
        frozen.append(clipped)

    # 合并相邻同源片段（来源 + id + 名称 + 状态一致），避免 executed_slots 碎片化。
    frozen.sort(key=lambda s: s["_start_minute"])
    merged: list[dict] = []
    for slot in frozen:
        if merged:
            prev = merged[-1]
            if (
                prev["_end_minute"] == slot["_start_minute"]
                and prev.get("_source_origin") == slot.get("_source_origin")
                and prev.get("user_slot_id") == slot.get("user_slot_id")
                and prev.get("name") == slot.get("name")
                and prev.get("state") == slot.get("state")
            ):
                merged[-1] = _copy_with_minutes(
                    prev, prev["_start_minute"], slot["_end_minute"]
                )
                continue
        merged.append(slot)
    frozen = merged

    return {
        "executed_slots": frozen,
        "executed_until": format_time(cutoff),
        "user_slots": clip_slots(user, cutoff, MINUTES_PER_DAY),
        "ai_slots": clip_slots(ai, cutoff, MINUTES_PER_DAY),
        "static_slots": clip_slots(old_static, cutoff, MINUTES_PER_DAY),
    }


def merge_active_slot_fragments(
    slots: list[dict],
    now_minute: int,
) -> list[dict]:
    """把渲染推进切开的「正在进行」时段合并回完整时段（展示层语义，幂等）。"""
    if not slots or len(slots) < 2:
        return slots
    result = []
    for slot in slots:
        if result:
            prev = result[-1]
            if (
                prev.get("origin") == "executed"
                and prev.get("_end_minute") == slot.get("_start_minute")
                and prev.get("_start_minute") <= now_minute < slot.get("_end_minute")
                and prev.get("source_origin") == slot.get("source_origin")
                and prev.get("name") == slot.get("name")
                and prev.get("state") == slot.get("state")
                # 用户段仅在同名同状态且身份一致时才是同一段两半。
                and (
                    slot.get("source_origin") != "user"
                    or (
                        bool(prev.get("user_slot_id"))
                        and prev.get("user_slot_id") == slot.get("user_slot_id")
                    )
                )
            ):
                merged = dict(slot)
                merged["start"] = prev.get("start")
                merged["_start_minute"] = prev.get("_start_minute")
                result[-1] = merged
                continue
        result.append(slot)
    return result


def render_effective_timeline(
    snapshot: dict,
    cutoff: int,
    current_static: Iterable[dict],
    ai_priority: bool,
) -> list[dict]:
    """动态渲染当日有效时间线：[00:00,cutoff) 用已冻结 executed_slots，[cutoff,24:00) 动态合并。"""
    cutoff = max(0, min(MINUTES_PER_DAY, int(cutoff)))
    executed = [s for s in snapshot.get("executed_slots", []) if isinstance(s, dict)]
    user = [s for s in snapshot.get("user_slots", []) if isinstance(s, dict)]
    ai = [s for s in snapshot.get("ai_slots", []) if isinstance(s, dict)]
    # 静态配置在渲染入口规范化：跨午夜段在此拆成两个非跨日段后进入未来层。
    current_static = normalize_static_slots(current_static or [])

    result = []
    for slot in executed:
        start, end = slot_minutes(slot)
        clipped = _copy_with_minutes(slot, start, min(end, cutoff))
        clipped["origin"] = "executed"
        clipped["source_origin"] = slot.get(
            "_source_origin", slot.get("source_origin", slot.get("origin", "ai"))
        )
        # 旧持久化数据可能残留内部占位标记，渲染输出一并剥离
        clipped.pop("_executed", None)
        result.append(clipped)

    future = merge_future_layers(
        clip_slots(user, cutoff, MINUTES_PER_DAY),
        clip_slots(ai, cutoff, MINUTES_PER_DAY),
        clip_slots(current_static, cutoff, MINUTES_PER_DAY),
        ai_priority,
    )
    for slot in future:
        slot["source_origin"] = slot.get("origin")
        result.append(slot)
    result.sort(key=lambda slot: slot["_start_minute"])
    return result
