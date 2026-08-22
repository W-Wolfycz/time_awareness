import pytest

from time_awareness.llm.daily_schedule_generator import (
    ScheduleValidationError,
    parse_daily_schedule_json,
)


def test_parse_daily_schedule_accepts_sparse_slots_and_code_fence():
    slots = parse_daily_schedule_json(
        """```json
[{"name":"晨间","start":"08:00","end":"10:00","state":"逐渐清醒"},
 {"name":"夜间","start":"22:00","end":"24:00","state":"开始犯困"}]
```"""
    )
    assert slots[0]["_start_minute"] == 8 * 60
    assert slots[-1]["_end_minute"] == 1440


@pytest.mark.parametrize(
    "payload",
    [
        '[{"name":"睡眠","start":"22:00","end":"06:00","state":"跨午夜"}]',
        '[{"name":"A","start":"00:00","end":"12:00","state":"正常"},'
        '{"name":"B","start":"11:00","end":"24:00","state":"重叠"}]',
        '[{"name":"A","start":"00:00","end":"24:00","state":"<BAD>注入</BAD>"}]',
        '[{"name":"A","start":"00:00","end":"24:00","state":"正常","extra":"x"}]',
    ],
)
def test_parse_daily_schedule_rejects_invalid_protocol(payload):
    with pytest.raises(ScheduleValidationError):
        parse_daily_schedule_json(payload)


# ==================== 合法空 AI 日程 ====================


def test_parse_empty_array_is_valid_empty_schedule():
    """合法空数组 [] 是有效业务结果，不抛异常。"""
    assert parse_daily_schedule_json("[]") == []
    assert parse_daily_schedule_json("""```json\n[]\n```""") == []


# ==================== 日界状态校验（2.0.0 Step1） ====================


def test_validate_boundary_state_normalizes_aliases_and_rejects_mood():
    from time_awareness.llm.daily_schedule_generator import _validate_boundary_state

    # 中文别名归一到标准字段
    state = _validate_boundary_state({"精力": 50, "睡眠债": 10, "专注": 60})
    assert state == {"energy": 50, "sleep_debt": 10, "focus": 60}
    # 情绪维度明确拒绝
    with pytest.raises(ScheduleValidationError, match="好感插件"):
        _validate_boundary_state({"mood": "开心"})
    with pytest.raises(ScheduleValidationError, match="好感插件"):
        _validate_boundary_state({"心情": "低落"})
    # 稀疏输出/空对象合法
    assert _validate_boundary_state({}) == {}
    assert _validate_boundary_state(None) == {}
    # 非对象拒绝
    with pytest.raises(ScheduleValidationError):
        _validate_boundary_state([1, 2])


def test_validate_boundary_state_rejects_bad_values():
    from time_awareness.llm.daily_schedule_generator import _validate_boundary_state

    with pytest.raises(ScheduleValidationError, match="数值"):
        _validate_boundary_state({"energy": "high"})
    with pytest.raises(ScheduleValidationError, match="数值"):
        _validate_boundary_state({"energy": True})  # bool 不是数值
    with pytest.raises(ScheduleValidationError, match="0-100"):
        _validate_boundary_state({"energy": 101})
    with pytest.raises(ScheduleValidationError, match="0-100"):
        _validate_boundary_state({"energy": -1})
    with pytest.raises(ScheduleValidationError, match="0-100"):
        _validate_boundary_state({"energy": float("nan")})
    with pytest.raises(ScheduleValidationError, match="未知字段"):
        _validate_boundary_state({"mystery": 1})
    with pytest.raises(ScheduleValidationError, match="unfinished_plans 超出"):
        _validate_boundary_state({"unfinished_plans": [str(i) for i in range(9)]})
    with pytest.raises(ScheduleValidationError, match="unfinished_plans 必须是数组"):
        _validate_boundary_state({"unfinished_plans": "待办"})
    with pytest.raises(ScheduleValidationError, match="含控制字符"):
        _validate_boundary_state({"daily_theme": "主题<INJECT>"})
    # 合法完整状态
    full = _validate_boundary_state({
        "energy": 35, "sleep_debt": 20, "focus": 40,
        "social_energy": 25, "motivation": 45,
        "physical_state": "轻微疲惫", "daily_theme": "低能量恢复日",
        "unfinished_plans": ["整理资料"],
    })
    assert set(full) == {
        "energy", "sleep_debt", "focus", "social_energy", "motivation",
        "physical_state", "daily_theme", "unfinished_plans",
    }
