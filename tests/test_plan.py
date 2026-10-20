"""domain/plan.py 的纯逻辑测试：预生成天数、按天裁剪、计划块渲染。"""

import datetime

from time_awareness.domain.plan import (
    PLAN_MAX_PRE_GENERATE_DAYS,
    clip_day_slots,
    day_bounds,
    plan_days_ahead,
    plan_deadline,
    render_plan_block,
    resolve_plan_horizon,
)

NOW = datetime.datetime(2026, 10, 20, 10, 0, tzinfo=datetime.timezone.utc)
DAY = datetime.date(2026, 10, 20)
SLOTS = [
    {"start": "08:00", "end": "09:00", "name": "早饭"},
    {"start": "14:00", "end": "16:00", "name": "开会"},
]


def test_plan_days_ahead_is_ceil_plus_one():
    """ceil(h/24)+1：24h→2 天、48h→3 天；少一天窗口末端跨日就断档，多一天白烧调用。"""
    expected = {0: 1, 6: 2, 12: 2, 24: 2, 25: 3, 36: 3, 48: 3, 240: PLAN_MAX_PRE_GENERATE_DAYS}
    for horizon, days in expected.items():
        assert plan_days_ahead(horizon) == days


def test_plan_days_ahead_covers_window_at_any_moment():
    """任一时刻的窗口末端都落在已生成的最远一天之内（触发前几分钟的空档由下一轮补上）。

    采样步长 15 分钟：最后 5 分钟窗口末端会落到下下天，这是有意接受的成本取舍。
    """
    trigger = datetime.datetime(2026, 10, 20, 0, 5, tzinfo=datetime.timezone.utc)
    for horizon in (6, 12, 24, 36, 48):
        last_day = trigger.date() + datetime.timedelta(days=plan_days_ahead(horizon) - 1)
        for minutes in range(0, 24 * 60, 15):
            moment = trigger + datetime.timedelta(minutes=minutes)
            assert last_day >= (moment + datetime.timedelta(hours=horizon)).date()


def test_plan_block_pipeline_clips_bounds_and_labels():
    """deadline → day_bounds → clip → render 是同一条流水线：单摘一层现象都一样。

    不裁左端 → 计划里出现已经过去的时段；不挡窗外日 → 凭空多一天「（未规划）」；
    不写相对日名 → 模型分不清哪行是哪天。
    """
    tomorrow = NOW.date() + datetime.timedelta(days=1)
    per_day = {
        NOW.date(): SLOTS,
        tomorrow: [
            {"start": "01:00", "end": "07:30", "name": "睡觉"},
            {"start": "09:30", "end": "11:00", "name": "外出"},
        ],
    }
    deadline = plan_deadline(NOW, 24)  # 次日 10:00
    days = []
    day = NOW.date()
    while day <= deadline.date():
        bounds = day_bounds(day, NOW, deadline)
        slots = clip_day_slots(per_day.get(day, []), start_bound=bounds[0], end_bound=bounds[1])
        days.append((day, slots))
        day += datetime.timedelta(days=1)

    block = render_plan_block(days, NOW) or ""
    lines = block.splitlines()
    # 今天从"此刻"（10:00）起算：08:00 的早饭消失、14:00 的会议保留
    assert lines[0].startswith("今天 10-20 周二")
    assert "  14:00-16:00 开会" in block and "早饭" not in block
    # 明天截止到 deadline（10:00）：09:30-11:00 被裁成 09:30-10:00
    assert lines[2].startswith("明天 10-21 周三")
    assert "  01:00-07:30 睡觉" in block and "  09:30-10:00 外出" in block
    # 窗口之外的日子整段不出现
    assert "后天" not in block
    # 全空 → 不产生块（调用方据此省略标签）
    assert render_plan_block([(NOW.date(), [])], NOW) is None


def test_plan_horizon_reads_config():
    """配置键被忽略=窗口永远 24h：注入变长变短都无感受，预生成天数也跟着错。"""
    assert resolve_plan_horizon({}) == 24
    assert resolve_plan_horizon({"daily_schedule": {"ai_daily": {"plan_horizon_hours": 0}}}) == 0
    assert resolve_plan_horizon({"daily_schedule": {"ai_daily": {"plan_horizon_hours": 48}}}) == 48
    assert resolve_plan_horizon({"daily_schedule": {"ai_daily": {"plan_horizon_hours": 999}}}) == 48
    assert resolve_plan_horizon({"daily_schedule": {"ai_daily": {"plan_horizon_hours": -5}}}) == 0
    assert resolve_plan_horizon({"daily_schedule": {"ai_daily": {"plan_horizon_hours": "x"}}}) == 24


def test_plan_keeps_cross_midnight_static_slots():
    """静态模板 22:00-08:00 必须先拆段再裁：整段拿去裁会被解析成 (1320,480) 直接丢掉。

    v2.3.3 引入过这个静默回归（计划块里"夜间守岗"整段消失，没有任何报错）。
    """
    raw = [{"name": "夜间守岗", "start": "22:00", "end": "08:00"}]
    clipped = clip_day_slots(raw, start_bound=1200, end_bound=1440)
    assert [(slot["start"], slot["end"], slot["name"]) for slot in clipped] == [
        ("22:00", "24:00", "夜间守岗")
    ]
