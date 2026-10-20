"""日程计划（未执行段）的纯逻辑：预生成天数、按天裁剪与渲染。

"已发生"由 ``<SCHEDULE_DONE>`` 承担，"未发生"由 ``<SCHEDULE_PLAN>`` 承担——两者同一套渲染规则，
不区分"今天/明天"，只按日期分段。
"""

from __future__ import annotations

import datetime

from .schedule import (
    MINUTES_PER_DAY,
    ScheduleValidationError,
    ai_daily_config,
    format_time,
    normalize_static_slots,
    slot_minutes,
)
from .upcoming import relative_day_label

# 预生成天数的安全上限：48h 窗口在"生成当天"模式下最多需要 4 天（今天 + 未来三天）
PLAN_MAX_PRE_GENERATE_DAYS = 4
# 计划窗口的默认值与上限（小时）
PLAN_DEFAULT_HORIZON_HOURS = 24
PLAN_MAX_HORIZON_HOURS = 48
_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def resolve_plan_horizon(config) -> int:
    """读 ``daily_schedule.ai_daily.plan_horizon_hours``：0=不注入计划，非法/缺失按默认 24。"""
    raw = ai_daily_config(config).get("plan_horizon_hours", PLAN_DEFAULT_HORIZON_HOURS)
    try:
        hours = int(raw)
    except (TypeError, ValueError):
        return PLAN_DEFAULT_HORIZON_HOURS
    return max(0, min(PLAN_MAX_HORIZON_HOURS, hours))


def plan_days_ahead(horizon_hours: int) -> int:
    """预生成几天：``ceil(horizon/24) + 1``（24h → 2 天，48h → 3 天，0=不注入时仍只生成 1 天）。

    多出来的那一天是"跨日那一天"：窗口末端一旦跨过午夜，就必须有下一天的快照。
    每天触发前几分钟内，窗口末端会落到"还没生成的最远一天之后"——下一轮触发生成后立刻补齐，
    这点空档（≤ 触发间隔）是有意接受的成本取舍。
    """
    if horizon_hours <= 0:
        return 1
    days = (int(horizon_hours) + 23) // 24 + 1
    return max(1, min(PLAN_MAX_PRE_GENERATE_DAYS, days))


def clip_day_slots(slots, *, start_bound: int, end_bound: int) -> list[dict]:
    """把某天的时段裁到 ``[start_bound, end_bound)``；裁空的段丢弃、跨界的段收边。

    入口先规范化：跨午夜段（22:00-08:00）若不拆开会被 ``slot_minutes`` 解析成
    (1320, 480)，再裁就整段消失——静态模板兜底曾因此静默丢段。
    """
    clipped: list[dict] = []
    for slot in normalize_static_slots(slots):
        if not isinstance(slot, dict):
            continue
        try:
            start, end = slot_minutes(slot)
        except (ScheduleValidationError, ValueError):
            continue
        start, end = max(start, start_bound), min(end, end_bound)
        if end <= start:
            continue
        clipped.append({**slot, "start": format_time(start), "end": format_time(end)})
    return clipped


def join_slots(slots) -> str | None:
    """拼成一行 ``HH:MM-HH:MM 名称 | …``；无内容返回 None。"""
    parts: list[str] = []
    for slot in slots or []:
        if not isinstance(slot, dict):
            continue
        start = str(slot.get("start", "") or "").strip()
        end = str(slot.get("end", "") or "").strip()
        if not start or not end:
            continue
        name = str(slot.get("name", "") or "").strip()[:24]
        parts.append(f"{start}-{end} {name}".rstrip())
    return " | ".join(parts) or None


def render_plan_block(
    days: list[tuple[datetime.date, list[dict]]],
    now: datetime.datetime,
) -> str | None:
    """把 ``[(日期, 该日未执行段)]`` 渲染成计划块正文；全部为空返回 None。

    每天一行表头（``今天 10-20 周二`` / ``明天 …``），没有时段的日子写「（未规划）」，
    让模型知道计划到哪儿为止。
    """
    if not any(slots for _, slots in days):
        return None
    lines: list[str] = []
    for day, slots in days:
        offset = (day - now.date()).days
        if offset == 0:
            label = "今天"
        else:
            label = relative_day_label(offset - 1) or ""
        header = f"{label} {day.strftime('%m-%d')} {_WEEKDAY_CN[day.weekday()]}".strip()
        lines.append(header)
        if not slots:
            lines.append("  （未规划）")
            continue
        for slot in slots:
            start = str(slot.get("start", "") or "").strip()
            end = str(slot.get("end", "") or "").strip()
            name = str(slot.get("name", "") or "").strip()[:24]
            lines.append(f"  {start}-{end} {name}".rstrip())
    return "\n".join(lines)


def plan_deadline(now: datetime.datetime, horizon_hours: int) -> datetime.datetime:
    """计划窗口右端；``horizon_hours <= 0`` 由调用方视为"不注入"。"""
    return now + datetime.timedelta(hours=max(0, int(horizon_hours)))


def day_bounds(
    day: datetime.date,
    now: datetime.datetime,
    deadline: datetime.datetime,
) -> tuple[int, int] | None:
    """某天在计划窗口内的分钟区间；完全落在窗口外返回 None。"""
    if day > deadline.date():
        return None
    start_bound = now.hour * 60 + now.minute if day == now.date() else 0
    end_bound = (
        MINUTES_PER_DAY
        if day < deadline.date()
        else deadline.hour * 60 + deadline.minute
    )
    if day == deadline.date() and end_bound <= start_bound:
        return None
    return start_bound, end_bound
