"""人格日程编辑领域规则：左闭右开，start==C 视为已锁定，end==C 视为已结束，end=24:00 合法。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .schedule import minute_of_day

day_minute_of = minute_of_day  # 兼容旧名


# 单日快照时段总数上限（防异常大 payload）
MAX_SLOTS_PER_SNAPSHOT = 24
MAX_NAME_LENGTH = 24
MAX_STATE_LENGTH = 500


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
