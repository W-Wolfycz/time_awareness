"""人格日程编辑领域规则：左闭右开，start==C 视为已锁定，end==C 视为已结束，end=24:00 合法。"""

from __future__ import annotations

import datetime
import hashlib
from dataclasses import dataclass

from .schedule import (
    UNSAFE_TEXT_REASON,
    ScheduleValidationError,
    format_time,
    has_unsafe_text,
    minute_of_day,
    parse_time,
    slot_minutes,
)

day_minute_of = minute_of_day  # 兼容旧名


# 单日快照时段总数上限（防异常大 payload）
MAX_SLOTS_PER_SNAPSHOT = 24
MAX_NAME_LENGTH = 24
MAX_STATE_LENGTH = 500

# AI 工具的用户层写动作；list 只读，不构造草稿
SLOT_WRITE_ACTIONS = ("add", "update", "delete")

# 写入宽限：LLM 生成 + 网络往返会让请求到达时冻结点已前移。起点落在这个窗口内时静默前移到冻结点
# （绝不写过去），窗口更早的仍按"开始时间不能早于冻结点"拒绝。调小=更严格，0=关闭。
EDIT_START_GRACE_MINUTES = 5

_RELATIVE_DATE_OFFSETS = {
    "今天": 0,
    "今日": 0,
    "本日": 0,
    "today": 0,
    "明天": 1,
    "明日": 1,
    "tomorrow": 1,
    "后天": 2,
    "昨天": -1,
    "昨日": -1,
    "yesterday": -1,
}


@dataclass(frozen=True)
class SlotEditState:
    """单一时段的编辑状态。"""

    mode: str  # "finished" | "active" | "imminent" | "future"
    editable_fields: tuple[str, ...] = ()


def slot_edit_state(
    start_minute: int,
    end_minute: int,
    now_minute: int,
    cutoff_minute: int,
) -> SlotEditState:
    """按时间权限规则判定时段状态与可编辑字段；C==0 时全部按未来处理。"""
    if cutoff_minute == 0:
        return SlotEditState("future", ("start", "end", "name", "state"))
    if end_minute <= cutoff_minute:
        return SlotEditState("finished", ())
    if start_minute <= now_minute:
        # 正在进行：start <= R 且 end > C
        return SlotEditState("active", ("end",))
    if start_minute <= cutoff_minute:
        # 跨冻结点尾段 / 即将开始：R < start <= C，仅可改 end
        return SlotEditState("imminent", ("end",))
    return SlotEditState("future", ("start", "end", "name", "state"))


def slot_ref(snapshot_id: str, index: int) -> str:
    """生成时段引用（前端展示定位用，不携带内容）。"""
    return hashlib.sha256(f"{snapshot_id}:{index}".encode("utf-8")).hexdigest()[:16]


def resolve_target_date(text, today: datetime.date) -> datetime.date | None:
    """把日期说法解析为日期：空串=今天，支持今天/明天/后天/昨天与 ISO、斜杠日期；无法识别返回 None。"""
    raw = str(text or "").strip().lower()
    if not raw:
        return today
    offset = _RELATIVE_DATE_OFFSETS.get(raw)
    if offset is not None:
        return today + datetime.timedelta(days=offset)
    try:
        return datetime.date.fromisoformat(raw.replace("/", "-").replace(".", "-"))
    except ValueError:
        return None


def _clean_slot_text(value, *, field: str, max_length: int) -> str:
    """规整时段文本：折叠空白、限长，拒绝控制字符与 XML 标签（会被注入 prompt）。"""
    text = " ".join(str(value or "").split()).strip()
    if not text:
        raise ScheduleValidationError(f"缺少{field}")
    if len(text) > max_length:
        raise ScheduleValidationError(f"{field}超出上限 {max_length} 字")
    if has_unsafe_text(text):
        raise ScheduleValidationError(f"{field}{UNSAFE_TEXT_REASON}")
    return text


def validate_slot_fields(*, name, start, end, state) -> dict:
    """校验一次写入的 name/start/end/state，返回规范化字段；非法抛 ScheduleValidationError。"""
    cleaned_name = _clean_slot_text(name, field="时段名称", max_length=MAX_NAME_LENGTH)
    cleaned_state = _clean_slot_text(state, field="状态描述", max_length=MAX_STATE_LENGTH)
    start_minute = parse_time(str(start or "").strip(), strict=True)
    end_minute = parse_time(str(end or "").strip(), allow_24=True, strict=True)
    if start_minute >= end_minute:
        raise ScheduleValidationError("开始时间必须早于结束时间")
    return {
        "name": cleaned_name,
        "start": format_time(start_minute),
        "end": format_time(end_minute),
        "state": cleaned_state,
    }


def apply_slot_edit(
    user_slots,
    cutoff: int,
    *,
    action: str,
    user_slot_id: str = "",
    name: str = "",
    start: str = "",
    end: str = "",
    state: str = "",
) -> tuple[list[dict] | None, str]:
    """在快照用户层上执行一次增/改/删，返回 ``(新用户层草稿, 错误信息)``。

    已结束（``end <= cutoff``）的用户段不再进入草稿：它们已被冻结点固化为历史，
    重交会被服务端拒绝；未列出的用户段按服务端协议视为删除，故草稿必须是完整用户层。
    """
    if action not in SLOT_WRITE_ACTIONS:
        return None, f"不支持的动作：{action}"

    kept: list[dict] = []
    for slot in user_slots or []:
        if not isinstance(slot, dict):
            continue
        try:
            _start, slot_end = slot_minutes(slot)
        except (ScheduleValidationError, ValueError):
            continue
        if slot_end > cutoff:
            kept.append(dict(slot))

    if action == "add":
        try:
            fields = validate_slot_fields(name=name, start=start, end=end, state=state)
        except ScheduleValidationError as exc:
            return None, str(exc)
        kept.append({**fields, "origin": "user", "user_slot_id": ""})
        return kept, ""

    slot_id = str(user_slot_id or "").strip()
    if not slot_id:
        return None, "缺少 user_slot_id（先用 action=list 查看用户段 id）"
    index = next(
        (
            position
            for position, slot in enumerate(kept)
            if str(slot.get("user_slot_id", "") or "") == slot_id
        ),
        None,
    )
    if index is None:
        return None, "找不到该时段：可能已结束、已被重新生成或 id 有误，请先用 action=list 重新查看"

    if action == "delete":
        kept.pop(index)
        return kept, ""

    existing = kept[index]
    try:
        fields = validate_slot_fields(
            name=name or existing.get("name", ""),
            start=start or existing.get("start", ""),
            end=end or existing.get("end", ""),
            state=state or existing.get("state", ""),
        )
    except ScheduleValidationError as exc:
        return None, str(exc)
    kept[index] = {**existing, **fields}
    return kept, ""


def strip_internal_fields(slots) -> list[dict]:
    """移除内部分钟字段，落盘时只保留 name/start/end/state/origin。"""
    return [
        {
            k: v
            for k, v in slot.items()
            if k in {"name", "start", "end", "state", "origin"}
        }
        for slot in slots
    ]
