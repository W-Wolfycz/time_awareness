"""分层日程时间线领域算法（纯逻辑，不依赖 AstrBot/存储/Web API）。

2.0.0 起人格日程改为「来源层 + 已执行历史」模型：

- ``executed_slots``：已结束时间线，永久冻结（带最终来源）；
- ``user_slots``：用户未来覆盖层（高于 AI/静态）；
- ``ai_slots``：最近一次 AI 原始日程；
- ``static_slots``：关键操作时采用的静态日程副本（推进历史时用旧副本，
  不得用新配置反写历史）；
- ``executed_until``：冻结边界（下一整分钟）；``[00:00, cutoff)`` 已确定
  （包括空白区间），``[cutoff, 24:00)`` 仍属未来按来源层动态合并。
"""

from __future__ import annotations

from typing import Any, Iterable

from .schedule import (
    MINUTES_PER_DAY,
    ScheduleValidationError,
    format_time,
    normalize_static_slots,
    parse_time,
    split_cross_midnight,
)


def next_minute_cutoff(now: Any) -> int:
    """冻结边界：下一整分钟（非整分钟向上取整，恰在整分钟保持不变）。

    返回当天分钟数（0..1440）；``now`` 只需有 hour/minute/second/microsecond。
    """
    if now.second == 0 and now.microsecond == 0:
        return now.hour * 60 + now.minute
    return now.hour * 60 + now.minute + 1


def _slot_minutes(slot: dict) -> tuple[int, int]:
    if "_start_minute" in slot and "_end_minute" in slot:
        try:
            start = int(slot["_start_minute"])
            end = int(slot["_end_minute"])
            if start != end or "start" not in slot:
                return start, end
        except (TypeError, ValueError):
            pass
    # 宽松解析（接受 H:MM）：静态配置手填的 `8:00` 也能参与渲染，
    # 不在渲染层静默丢弃（2.0.0 计划 §7 启动规范化之外的用户手填容错）。
    start = parse_time(str(slot.get("start", "")), allow_24=False, strict=False)
    end = parse_time(str(slot.get("end", "")), allow_24=True, strict=False)
    return start, end


def _copy_with_minutes(slot: dict, start: int, end: int) -> dict:
    fragment = dict(slot)
    fragment["start"] = format_time(start)
    fragment["end"] = format_time(end)
    fragment["_start_minute"] = start
    fragment["_end_minute"] = end
    return fragment


def _day_segments(start: int, end: int) -> list[tuple[int, int]]:
    """返回槽位在当天内的非跨日区间（跨午夜拆两段；零长度返回空）。

    与 ``domain.schedule.split_cross_midnight`` 共用同一拆分实现，保证
    命中/重叠/裁剪/合并各路径的跨午夜语义一致。``end`` 可为 ``1440``
    （``24:00``，只作右开不可命中边界）；非法分钟值按空处理。
    """
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
            start, end = _slot_minutes(slot)
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


def subtract_slots(
    base: Iterable[dict],
    blockers: Iterable[dict],
) -> list[dict]:
    """``base`` 时段减去 ``blockers`` 的覆盖区间，返回剩余片段。

    同源（同一原始时段）被拆分出的相邻片段会重新合并，但不同来源或
    不同原始时段不会误合并。
    """
    blockers_list = []
    for blocker in blockers or []:
        if not isinstance(blocker, dict):
            continue
        try:
            start, end = _slot_minutes(blocker)
        except (ScheduleValidationError, ValueError):
            continue
        blockers_list.extend(_day_segments(start, end))
    blockers_list.sort()

    result = []
    for slot in base or []:
        if not isinstance(slot, dict):
            continue
        try:
            start, end = _slot_minutes(slot)
        except (ScheduleValidationError, ValueError):
            continue
        segments = _day_segments(start, end)
        if not segments:
            continue
        for block_start, block_end in blockers_list:
            next_segments = []
            for seg_start, seg_end in segments:
                if block_end <= seg_start or block_start >= seg_end:
                    next_segments.append((seg_start, seg_end))
                    continue
                if seg_start < block_start:
                    next_segments.append((seg_start, min(block_start, seg_end)))
                if block_end < seg_end:
                    next_segments.append((max(block_end, seg_start), seg_end))
            segments = next_segments
            if not segments:
                break
        for seg_start, seg_end in segments:
            result.append(_copy_with_minutes(slot, seg_start, seg_end))
    return result


def merge_future_layers(
    user: Iterable[dict],
    ai: Iterable[dict],
    static: Iterable[dict],
    ai_priority: bool,
) -> list[dict]:
    """合并未来区间（由调用方先裁剪到 ``[cutoff, 24:00)``）。

    优先级：用户先占位；AI 与静态日程按 ``ai_priority`` 决定先后填充剩余
    空档。不要求覆盖全天。返回带 ``origin`` 的时段（相邻同源片段已合并）。
    """
    timeline: list[tuple[str, int, dict] | None] = [None] * MINUTES_PER_DAY

    def place(slots: Iterable[dict], origin: str, prefix: str) -> None:
        for index, slot in enumerate(slots or []):
            if not isinstance(slot, dict):
                continue
            try:
                start, end = _slot_minutes(slot)
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
    """推进冻结边界：固化 ``[00:00, cutoff)`` 的实际有效时间线。

    已执行区间只从操作发生前的实际有效时间线截取；历史推进使用快照中的
    旧 ``static_slots`` 副本，不加载最新静态配置反写历史（``current_static``
    不作为本函数输入——未来层渲染时才使用最新静态配置）。返回需要写回
    快照的字段（executed_slots / executed_until / 各未来层的裁剪结果）。
    """
    cutoff = max(0, min(MINUTES_PER_DAY, int(cutoff)))
    executed = [s for s in snapshot.get("executed_slots", []) if isinstance(s, dict)]
    user = [s for s in snapshot.get("user_slots", []) if isinstance(s, dict)]
    ai = [s for s in snapshot.get("ai_slots", []) if isinstance(s, dict)]
    # 静态层在存储边界规范化：跨午夜段在此拆成两个非跨日段，后续各层不再
    # 各自实现跨午夜语义（静态槽位进入四层 timeline 的唯一入口）。
    old_static = normalize_static_slots(snapshot.get("static_slots", []))

    # [00:00, cutoff) 有效时间线：executed 恒占（最高优先级，先占位）
    # → 旧 user/ai/static 按优先级填剩余
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
        start, end = _slot_minutes(slot)
        if start >= cutoff:
            continue
        clipped = _copy_with_minutes(slot, start, min(end, cutoff))
        clipped["origin"] = "executed"
        clipped["_source_origin"] = slot.get("_source_origin", slot.get("origin", "ai"))
        # 内部占位标记不落入持久化数据
        clipped.pop("_executed", None)
        frozen.append(clipped)

    # 合并相邻同源片段：同一原始业务时段（来源 + id + 名称 + 状态一致）被
    # 多次推进拆分出的相邻分钟段合并为一段，避免 executed_slots 碎片化。
    # 不同来源或不同业务时段绝不合并。
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
    """把渲染推进切开的「正在进行」时段合并为一段（展示层语义）。

    渲染路径把冻结边界动态推进到当前时刻时，跨越 now 的时段会被切成
    executed 前段 + future 尾段（如 14:00-16:00 在 15:00 查看时显示为
    「已结束 14:00-15:00」+「即将开始 15:00-16:00」）。这里把紧邻 now、
    同源同名同状态的相邻段合并回完整时段，消费方按 mode=active 显示
    「正在执行」而不是切成两半。

    已完整推进（executed_until=24:00）的快照没有切口，本函数幂等。
    """
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
                # 用户段只有在稳定身份一致时才是同一段的两半。仅靠同名、
                # 同状态会把两个恰好相邻的独立用户段错误合并。
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
    """动态渲染当日有效时间线。

    ``[00:00, cutoff)`` 使用已冻结的 ``executed_slots``（含空白，不补回填）；
    ``[cutoff, 24:00)`` 由 ``user_slots`` + ``ai_slots`` + 最新静态配置按
    优先级动态合并。``executed`` 段保留 ``source_origin`` 供展示。
    """
    cutoff = max(0, min(MINUTES_PER_DAY, int(cutoff)))
    executed = [s for s in snapshot.get("executed_slots", []) if isinstance(s, dict)]
    user = [s for s in snapshot.get("user_slots", []) if isinstance(s, dict)]
    ai = [s for s in snapshot.get("ai_slots", []) if isinstance(s, dict)]
    # 静态配置在渲染入口规范化：跨午夜段在此拆成两个非跨日段后进入未来层。
    current_static = normalize_static_slots(current_static or [])

    result = []
    for slot in executed:
        start, end = _slot_minutes(slot)
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
