"""日程时段统一领域规则：左闭右开 [start,end)，24:00 仅作不可命中右边界，跨午夜拆两段。"""

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
    """把时间文本解析为当天分钟数；strict=False 仅用于用户配置解析（允许省略前导零）。"""
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


def slot_minutes(slot: dict) -> tuple[int, int]:
    """取时段 ``[start_minute, end_minute)``；优先用内部分钟字段，否则宽松解析文本。"""
    if "_start_minute" in slot and "_end_minute" in slot:
        try:
            start = int(slot["_start_minute"])
            end = int(slot["_end_minute"])
            if start != end or "start" not in slot:
                return start, end
        except (TypeError, ValueError):
            pass
    # 宽松解析（接受 H:MM）：静态配置手填的 8:00 也参与渲染，不在渲染层静默丢弃。
    start = parse_time(str(slot.get("start", "")), allow_24=False, strict=False)
    end = parse_time(str(slot.get("end", "")), allow_24=True, strict=False)
    return start, end


def split_cross_midnight(start: int, end: int) -> tuple[tuple[int, int], ...]:
    """将静态时段展开为一个或两个非跨日区间；零长度返回空（start 必须可命中，end 可为 1440）。"""
    if not (0 <= start < MINUTES_PER_DAY and 0 <= end <= MINUTES_PER_DAY):
        raise ScheduleValidationError("静态时段必须落在 00:00-24:00 且起点可命中")
    if start == end:
        return ()
    if start < end:
        return ((start, end),)
    # end == 00:00 时第二段为空，不能把 [00:00,00:00) 带入时间线。
    return tuple(
        (range_start, range_end)
        for range_start, range_end in ((start, MINUTES_PER_DAY), (0, end))
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
    """把静态时段规范化为非跨日段（跨午夜拆两段），非法时段跳过；供命中/重叠/四层 timeline 共用。"""
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
    """两份 AI 日程的确定性结构相似度（0..1），不调用 LLM。"""
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


# ==================== 配置读取（服务层共用，保持字节级一致） ====================


def ai_daily_config(config: dict) -> dict:
    """读取 ``daily_schedule.ai_daily`` 配置块；非 dict 回退空 dict。"""
    daily = config.get("daily_schedule", {}) if isinstance(config, dict) else {}
    daily = daily if isinstance(daily, dict) else {}
    ai_daily = daily.get("ai_daily", {})
    return ai_daily if isinstance(ai_daily, dict) else {}


def ai_priority_over_static(config: dict) -> bool:
    """读取 ``daily_schedule.ai_daily.ai_priority_over_static`` 开关（缺省 False）。"""
    daily = config.get("daily_schedule", {}) if isinstance(config, dict) else {}
    daily = daily if isinstance(daily, dict) else {}
    return bool(daily.get("ai_daily", {}).get("ai_priority_over_static", False))


def static_slots_from_config(config: dict) -> list[dict]:
    """把 ``daily_schedule.schedule_templates`` 转为规范化静态时段（跨午夜拆段、剥离内部分钟字段）。"""
    daily = config.get("daily_schedule", {}) if isinstance(config, dict) else {}
    daily = daily if isinstance(daily, dict) else {}
    value = daily.get("schedule_templates")
    result = [
        {
            "name": str(item.get("name", "") or ""),
            "start": str(item.get("start_time", "") or ""),
            "end": str(item.get("end_time", "") or ""),
            "state": str(item.get("state_prompt", "") or ""),
            "origin": "static",
        }
        for item in (value if isinstance(value, list) else [])
        if isinstance(item, dict)
    ]
    return [
        {
            key: item[key]
            for key in ("name", "start", "end", "state", "origin")
            if key in item
        }
        for item in normalize_static_slots(result)
    ]
