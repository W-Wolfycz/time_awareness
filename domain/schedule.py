"""日程时段的统一领域规则。

本模块不依赖 AstrBot、配置对象或持久化实现，是以下语义的唯一来源：

- ``HH:MM`` 解析与 ``24:00`` 边界；
- 左闭右开 ``[start, end)`` 命中；
- 静态时段跨午夜展开；
- 静态时段重叠检测；
- AI 单日快照校验与静态日程稀疏合并（2.0.0 起动态合并见 timeline.py）。

统一运行语义（P1 约定）：
- 运行时午夜边界视为 ``00:00``：``minute_of_day`` 只返回 ``0..1439``，
  ``24:00`` 永远不会被命中。
- 配置里的 ``end_time=24:00`` 只表示不可抵达的右开端点（``1440``），
  是合法右边界；起点 ``00:00`` 是唯一的午夜可命中时刻。
- 静态跨午夜段（``start > end``，如 ``22:00→08:00``）在规范化/存储边界
  拆成两个非跨日段：``[22:00, 当日结束)`` 与 ``[00:00, 08:00)``
  （见 ``normalize_static_slots`` / ``split_cross_midnight``）。

静态兜底允许跨午夜；AI 单日快照不允许跨午夜，但 ``end`` 可以为
``24:00``。两种协议复用相同的分钟和值域规则，不共用宽松校验器。
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from typing import Iterable


MINUTES_PER_DAY = 24 * 60
_STRICT_TIME_RE = re.compile(r"^(\d{2}):(\d{2})$")
_LENIENT_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ScheduleValidationError(ValueError):
    """日程结构或时间边界不符合协议。"""


@dataclass(frozen=True)
class ScheduleOverlap:
    """两个静态时段首次发生重叠的位置。"""

    first_index: int
    second_index: int
    minute: int


def parse_time(
    value: str,
    *,
    allow_24: bool = False,
    strict: bool = True,
) -> int:
    """把时间文本解析为当天分钟数。

    ``strict=False`` 仅用于用户配置解析（``schedule_templates`` 与
    ``generation_time`` 允许省略前导零，如 ``8:00``）；AI 输出与保存协议
    必须使用严格的两位小时 ``HH:MM``。
    """
    if not isinstance(value, str):
        raise ScheduleValidationError("时间必须是字符串")
    match = (_STRICT_TIME_RE if strict else _LENIENT_TIME_RE).fullmatch(value.strip())
    if not match:
        raise ScheduleValidationError("时间格式必须为 HH:MM")
    hour = int(match.group(1))
    minute = int(match.group(2))
    if allow_24 and hour == 24 and minute == 0:
        return MINUTES_PER_DAY
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleValidationError("时间超出 00:00-24:00")
    return hour * 60 + minute


def format_time(value: int) -> str:
    """把 ``0..1440`` 分钟格式化为 ``HH:MM``。"""
    if not isinstance(value, int) or not (0 <= value <= MINUTES_PER_DAY):
        raise ScheduleValidationError("分钟数超出 0-1440")
    if value == MINUTES_PER_DAY:
        return "24:00"
    return f"{value // 60:02d}:{value % 60:02d}"


def minute_of_day(now: datetime.datetime) -> int:
    return now.hour * 60 + now.minute


def split_cross_midnight(start: int, end: int) -> tuple[tuple[int, int], ...]:
    """将静态时段展开为一个或两个日内非跨日区间；零长度返回空。

    - ``start`` 必须是可命中时刻（``0..1439``；``24:00`` 不可命中所以拒绝）；
    - ``end`` 可以是右开边界 ``1440``（即 ``24:00``，只表示不可抵达的右端
      点，永远不会被命中）——此时是单段 ``[start, 1440)``；
    - ``start < end``：单段 ``(start, end)``；
    - ``start > end``：跨午夜，拆成 ``(start, 1440)`` 与 ``(0, end)`` 两段。
    """
    if not (0 <= start < MINUTES_PER_DAY and 0 <= end <= MINUTES_PER_DAY):
        raise ScheduleValidationError("静态时段必须落在 00:00-24:00 且起点可命中")
    if start == end:
        return ()
    if start < end:
        return ((start, end),)
    # ``end == 00:00`` 时第二段为空，不能把 ``[00:00, 00:00)``
    # 作为一个真实时段带入时间线。
    return tuple(
        (range_start, range_end)
        for range_start, range_end in (
            (start, MINUTES_PER_DAY),
            (0, end),
        )
        if range_start < range_end
    )


def is_minute_in_static_range(current: int, start: int, end: int) -> bool:
    """按左闭右开规则判断分钟是否命中允许跨午夜的静态时段。"""
    return any(range_start <= current < range_end for range_start, range_end in split_cross_midnight(start, end))


def find_active_static_slot(
    slots: Iterable[dict],
    now: datetime.datetime,
) -> dict | None:
    """按配置顺序返回当前命中的第一个静态时段。"""
    current = minute_of_day(now)
    for slot in slots or []:
        if not isinstance(slot, dict):
            continue
        try:
            start = parse_time(str(slot.get("start_time", "")), strict=False)
            # end 允许 24:00：只作不可命中的右开端点（与 timeline 一致）
            end = parse_time(str(slot.get("end_time", "")), allow_24=True, strict=False)
        except ScheduleValidationError:
            continue
        if is_minute_in_static_range(current, start, end):
            return slot
    return None


def find_active_daily_slot(
    slots: Iterable[dict],
    now: datetime.datetime,
) -> dict | None:
    """返回当前命中的 AI 单日快照时段。"""
    current = minute_of_day(now)
    for slot in slots or []:
        if not isinstance(slot, dict):
            continue
        try:
            start = parse_time(str(slot.get("start", "")), strict=True)
            end = parse_time(str(slot.get("end", "")), allow_24=True, strict=True)
        except ScheduleValidationError:
            continue
        if start < end and start <= current < end:
            return slot
    return None


def detect_static_overlaps(slots: Iterable[dict]) -> list[ScheduleOverlap]:
    """检测静态时段重叠，每对时段只返回第一次冲突。"""
    slot_list = list(slots or [])
    occupied: list[int | None] = [None] * MINUTES_PER_DAY
    seen_pairs: set[tuple[int, int]] = set()
    overlaps: list[ScheduleOverlap] = []

    for index, slot in enumerate(slot_list):
        if not isinstance(slot, dict):
            continue
        try:
            start = parse_time(str(slot.get("start_time", "")), strict=False)
            # end 允许 24:00：与命中/时间线路径共用同一右开语义
            end = parse_time(str(slot.get("end_time", "")), allow_24=True, strict=False)
            ranges = split_cross_midnight(start, end)
        except ScheduleValidationError:
            continue
        for range_start, range_end in ranges:
            for minute in range(range_start, range_end):
                other = occupied[minute]
                if other is None:
                    occupied[minute] = index
                    continue
                pair = (other, index)
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    overlaps.append(ScheduleOverlap(other, index, minute))
    return overlaps


def normalize_static_slots(slots: Iterable[dict]) -> list[dict]:
    """把静态时段规范化为非跨日段（跨午夜拆两段）——规范化/存储边界。

    输入兼容两种形态：配置源（``start_time``/``end_time``）与时间线槽位
    （``start``/``end``）；``end`` 允许 ``24:00``（只作右开边界，不可命中）。
    每个跨午夜段（``start > end``）拆成两个非跨日段：:

        [start, 当日结束) 与 [00:00, end)

    非法时段跳过。返回的槽位统一为 ``start``/``end``（HH:MM）加内部分钟
    字段 ``_start_minute``/``_end_minute``，供命中/重叠/四层 timeline 共用，
    避免各路径各自实现跨午夜语义。
    """
    result: list[dict] = []
    for slot in slots or []:
        if not isinstance(slot, dict):
            continue
        start_raw = str(slot.get("start_time", slot.get("start", "")) or "")
        end_raw = str(slot.get("end_time", slot.get("end", "")) or "")
        try:
            start = parse_time(start_raw, allow_24=False, strict=False)
            end = parse_time(end_raw, allow_24=True, strict=False)
        except ScheduleValidationError:
            continue
        for range_start, range_end in split_cross_midnight(start, end):
            fragment = dict(slot)
            fragment["start"] = format_time(range_start)
            fragment["end"] = format_time(range_end)
            fragment["_start_minute"] = range_start
            fragment["_end_minute"] = range_end
            fragment.pop("start_time", None)
            fragment.pop("end_time", None)
            result.append(fragment)
    return result


def normalize_daily_slots(slots: Iterable[dict]) -> list[dict]:
    """校验、排序 AI 单日时段，并添加供合并使用的分钟字段。"""
    normalized: list[dict] = []
    for slot in slots:
        start = parse_time(str(slot.get("start", "")), strict=True)
        end = parse_time(str(slot.get("end", "")), allow_24=True, strict=True)
        if start >= end:
            raise ScheduleValidationError("AI 单日快照不允许零长度或跨午夜时段")
        normalized.append(
            {
                **slot,
                "start": format_time(start),
                "end": format_time(end),
                "_start_minute": start,
                "_end_minute": end,
            }
        )

    normalized.sort(key=lambda item: item["_start_minute"])
    previous_end = 0
    for index, slot in enumerate(normalized):
        if index and slot["_start_minute"] < previous_end:
            raise ScheduleValidationError("AI 日程时段存在重叠")
        previous_end = slot["_end_minute"]
    return normalized


def sort_static_slots(slots: Iterable[dict]) -> list[dict]:
    """按开始时间排序；非法时段稳定地放在末尾。"""
    indexed = list(enumerate(item for item in (slots or []) if isinstance(item, dict)))

    def key(entry: tuple[int, dict]) -> tuple[int, int]:
        index, item = entry
        try:
            return parse_time(str(item.get("start_time", "")), strict=False), index
        except ScheduleValidationError:
            return MINUTES_PER_DAY + 1, index

    return [item for _, item in sorted(indexed, key=key)]


def _minute_bitmap(slots: Iterable[dict]) -> tuple[set[int], int]:
    """把时段集合压成 (位图 int 集合, 覆盖分钟数)；用于相似度计算。"""
    covered = set()
    for slot in slots or []:
        if not isinstance(slot, dict):
            continue
        try:
            start = parse_time(str(slot.get("start", "")), strict=True)
            end = parse_time(str(slot.get("end", "")), allow_24=True, strict=True)
        except ScheduleValidationError:
            continue
        if start < end:
            covered.update(range(start, end))
    return covered, len(covered)


def schedule_similarity(a: Iterable[dict], b: Iterable[dict]) -> float:
    """两份 AI 日程的确定性结构相似度（0..1）。

    时段边界 + 名称 + 状态指纹的确定性检查，不调用 LLM。
    1.0 表示完全重复，0.0 表示完全不同；``SIMILARITY_REPEAT_THRESHOLD``
    以上的组合将被视为「与最近日程重复」。
    """
    a_list = [item for item in (a or []) if isinstance(item, dict)]
    b_list = [item for item in (b or []) if isinstance(item, dict)]
    if not a_list or not b_list:
        return 0.0

    a_minutes, a_total = _minute_bitmap(a_list)
    b_minutes, b_total = _minute_bitmap(b_list)
    if not a_total or not b_total:
        time_jaccard = 0.0
    else:
        overlap = len(a_minutes & b_minutes)
        union = len(a_minutes | b_minutes)
        time_jaccard = overlap / union if union else 0.0

    a_names = {str(slot.get("name", "")).strip() for slot in a_list}
    b_names = {str(slot.get("name", "")).strip() for slot in b_list}
    a_names.discard("")
    b_names.discard("")
    name_jaccard = (
        len(a_names & b_names) / len(a_names | b_names)
        if (a_names or b_names)
        else 0.0
    )

    a_states = {" ".join(str(slot.get("state", "")).split())[:40] for slot in a_list}
    b_states = {" ".join(str(slot.get("state", "")).split())[:40] for slot in b_list}
    state_jaccard = (
        len(a_states & b_states) / len(a_states | b_states)
        if (a_states or b_states)
        else 0.0
    )

    return round(0.5 * time_jaccard + 0.3 * name_jaccard + 0.2 * state_jaccard, 4)
