"""人格日程编辑的领域规则（纯逻辑，不依赖 AstrBot / 存储 / Web API）。

时间权限规则（R = 服务端当前分钟，C = 服务端冻结点=下一整分钟，按目标
日期/时区换算为当天分钟数）：

    状态        判定                    允许修改
    ──────────  ─────────────────────  ─────────────────────
    已结束      end <= C               全部只读
    正在进行    start <= R 且 end > C  仅 end
    即将开始    R < start <= C         仅 end（或删除）
    未来        start > C              start/end/name/state

边界统一左闭右开：``start == C`` 视为已进入锁定区；``end == C`` 视为已结束。
``end=24:00`` 合法；AI 单日快照不允许跨午夜。

2.0.0 起保存协议只接受用户层草稿（``DailyScheduleAdminService`` 内部校验），
不再有整日草稿 + slot_ref 校验路径；``slot_ref`` 保留仅用于前端定位展示。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any


# 单日快照时段总数上限（防异常大 payload）
MAX_SLOTS_PER_SNAPSHOT = 24
MAX_NAME_LENGTH = 24
MAX_STATE_LENGTH = 500


# ---------------- 时段分类 ----------------

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
    """按时间权限规则判定时段状态与可编辑字段。

    ``cutoff_minute`` 即 C（服务端冻结点=下一整分钟，已按目标时区换算为
    当天分钟）；``[00:00, C)`` 视为已结束。

    ``R < start <= C`` 是跨冻结点/即将开始的用户尾段（``[cutoff, 24:00)``
    仍属未来）：仅允许修改结束时间（或删除），不允许移动开始时间或改名。

    ``C == 0`` 是哨兵（未来日期查看，或恰在午夜）：尚无任何冻结，
    全部时段按未来处理、可整段编辑。
    """
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


def day_minute_of(dt: Any) -> int:
    """把 aware datetime 换算为当天分钟数（0..1440）。"""
    return dt.hour * 60 + dt.minute


# ---------------- slot_ref ----------------

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
