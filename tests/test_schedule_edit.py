"""人格日程编辑领域规则测试（纯逻辑，不依赖 AstrBot/存储）。

2.0.0 起保存协议只接受用户层草稿，整日草稿 + slot_ref 校验路径已删除；
此处保留时段状态判定与 slot_ref 定位的测试。
"""

from time_awareness.domain.schedule_edit import (
    slot_edit_state,
    slot_ref,
)

# R = 10:00 = 600 分钟；C = 10:02 = 602 分钟（示例冻结点）
NOW = 600
CUTOFF = 602


# ==================== 状态判定 ====================


def test_edit_state_classification():
    # 已结束：end <= C
    assert slot_edit_state(540, 600, NOW, CUTOFF).mode == "finished"
    assert slot_edit_state(540, 602, NOW, CUTOFF).mode == "finished"  # end == C
    assert slot_edit_state(600, 603, NOW, CUTOFF).mode == "active"  # start == R
    # 正在进行：start <= R 且 end > C
    state = slot_edit_state(500, 630, NOW, CUTOFF)
    assert state.mode == "active"
    assert state.editable_fields == ("end",)
    # 跨冻结点尾段 / 即将开始：R < start <= C，仅可改 end
    state = slot_edit_state(601, 630, NOW, CUTOFF)
    assert state.mode == "imminent"
    assert state.editable_fields == ("end",)
    state = slot_edit_state(602, 630, NOW, CUTOFF)  # start == C
    assert state.mode == "imminent"
    assert state.editable_fields == ("end",)
    # 未来：start > C
    state = slot_edit_state(603, 630, NOW, CUTOFF)
    assert state.mode == "future"
    assert state.editable_fields == ("start", "end", "name", "state")
    # 24:00 合法且不越界
    assert slot_edit_state(700, 1440, NOW, CUTOFF).mode == "future"


def test_edit_state_window_boundaries():
    """冻结点前后各 1 分钟的状态差异。"""
    # start = C - 1 分钟 → 跨冻结点尾段（仅可改 end）
    assert slot_edit_state(601, 700, NOW, CUTOFF).mode == "imminent"
    assert slot_edit_state(601, 700, NOW, CUTOFF).editable_fields == ("end",)
    # start = C → 跨冻结点尾段（仅可改 end）
    assert slot_edit_state(602, 700, NOW, CUTOFF).mode == "imminent"
    assert slot_edit_state(602, 700, NOW, CUTOFF).editable_fields == ("end",)
    # start = C + 1 分钟 → 未来（可编辑全部字段）
    assert slot_edit_state(603, 700, NOW, CUTOFF).mode == "future"


# ==================== slot_ref ====================


def test_slot_ref_stable_and_distinct():
    assert slot_ref("snap-a", 0) == slot_ref("snap-a", 0)
    assert slot_ref("snap-a", 0) != slot_ref("snap-a", 1)
    assert slot_ref("snap-a", 0) != slot_ref("snap-b", 0)
    assert len(slot_ref("snap-a", 0)) == 16
