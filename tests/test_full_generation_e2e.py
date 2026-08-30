"""全开生成流程端到端：自适应 + 连续性 + 最近日程 + 双候选 + LLM 裁判 + 主题池 + 随机天气 + 静态日程。"""

import asyncio
import datetime
import importlib.util
import json
import pathlib

# 仓库测试惯例：pytest 收集下跨测试文件无法用包名导入，按文件路径加载共享 helper。
_spec = importlib.util.spec_from_file_location(
    "ta_adaptive_helpers",
    pathlib.Path(__file__).parent / "test_adaptive_daily_schedule.py",
)
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)
NOW = _helpers.NOW
SESSION = _helpers.SESSION
_adaptive_config = _helpers._adaptive_config
_no_sleep = _helpers._no_sleep
FakeContext = _helpers.FakeContext
FakePersonaResolver = _helpers.FakePersonaResolver
FakeTimeContext = _helpers.FakeTimeContext

from time_awareness.core.calendar_store import CalendarStore
from time_awareness.core.daily_schedule_store import DailyScheduleSnapshotStore
from time_awareness.core.last_chat_tracker import LastChatTracker
from time_awareness.services.daily_schedule_service import DailyScheduleService
from time_awareness.services.time_context_service import TimeContextService

# 候选 A / B：全部落在 NOW(07:30) 之后，避免被冻结裁剪，便于断言。
_CANDIDATE_A = json.dumps(
    [
        {"name": "上午休整", "start": "08:00", "end": "11:00", "state": "慢慢恢复状态"},
        {"name": "午后散步", "start": "15:00", "end": "17:00", "state": "轻松地走走"},
    ],
    ensure_ascii=False,
)
_CANDIDATE_B = json.dumps(
    [
        {"name": "上午训练", "start": "08:00", "end": "11:30", "state": "专注进行海上训练"},
        {"name": "装备维护", "start": "14:00", "end": "16:00", "state": "认真检修装备"},
    ],
    ensure_ascii=False,
)
_BOUNDARY = json.dumps(
    {
        "energy": 55, "sleep_debt": 10, "focus": 70,
        "social_energy": 50, "motivation": 60,
        "physical_state": "状态良好",
        "daily_theme": "演习日", "daily_style": "沉稳",
        "unfinished_plans": [],
    },
    ensure_ascii=False,
)
_JUDGE = json.dumps({"choice": 1, "reason": "更贴合演习日"}, ensure_ascii=False)


async def _fixed_forecast(date):
    return {
        "fxDate": date.isoformat(),
        "textDay": "小雨",
        "textNight": "多云",
        "tempMax": "23",
        "tempMin": "18",
    }


def _service(tmp_path, context, config):
    store = DailyScheduleSnapshotStore(str(tmp_path))
    assert store.load()
    return (
        DailyScheduleService(
            context=context,
            config=config,
            store=store,
            time_context=FakeTimeContext(),
            persona_resolver=FakePersonaResolver("persona_demo"),
            concurrency_gate=None,
            sleep=_no_sleep,
            weather_forecast_provider=_fixed_forecast,
        ),
        store,
    )


def _seed_yesterday(store):
    """昨日 ready 快照：给 Step1 提供 PREVIOUS_BOUNDARY 与 RECENT_THEMES 数据源。"""
    yesterday = NOW.date() - datetime.timedelta(days=1)
    persona_hash = store.persona_hash("persona_demo")
    store.save_ready(
        {
            "snapshot_id": "yesterday",
            "persona_hash": persona_hash,
            "local_date": yesterday.isoformat(),
            "timezone": "UTC",
            "status": "ready",
            "executed_until": "24:00",
            "executed_slots": [],
            "user_slots": [],
            "ai_slots": [
                {"name": "睡眠", "start": "00:00", "end": "08:00", "state": "在睡觉"},
            ],
            "static_slots": [],
            "slots": [
                {"name": "睡眠", "start": "00:00", "end": "08:00", "state": "在睡觉"},
            ],
            "boundary_state": {
                "energy": 30,
                "daily_theme": "港区休整日",
                "daily_style": "慵懒",
            },
        },
        retention_days=30,
        today=NOW.date(),
    )
    return persona_hash


def test_full_generation_flow(tmp_path):
    config = _adaptive_config(
        adaptive={
            "enabled": True,
            "state_continuity_enabled": True,
            "recent_days": 3,
            "candidate_count": 2,
            "candidate_selection": "llm",
            "context_token_budget": 0,
            "theme_pool": ["演习日", "外出日"],
            "style_pool": ["沉稳", "元气"],
            "allow_custom_theme": True,
        },
        ai_daily_extra={
            "random_weather_enabled": True,
            "ai_priority_over_static": True,
        },
        schedule_templates=[
            {"name": "睡眠", "start_time": "00:00", "end_time": "07:00", "state_prompt": "在睡觉"},
        ],
    )
    context = FakeContext(
        [_CANDIDATE_A, _CANDIDATE_B],
        judge_responses=[_JUDGE],
        boundary_responses=[_BOUNDARY],
    )
    service, store = _service(tmp_path, context, config)
    persona_hash = _seed_yesterday(store)

    result = asyncio.run(service.generate_for_session(SESSION, now=NOW))
    assert result.success
    assert len(context.boundary_calls) == 1
    assert len(context.plan_calls) == 2  # 双候选各一次
    assert len(context.judge_calls) == 1  # LLM 裁判一次

    # Step1：池与最近主题反重复、昨日边界与连续实际日程全部注入
    boundary_prompt = context.boundary_calls[0]["prompt"]
    for block in ("THEME_POOL", "STYLE_POOL", "RECENT_THEMES",
                  "PREVIOUS_BOUNDARY", "ACTUAL_SCHEDULES"):
        assert block in boundary_prompt, block
    assert "演习日" in boundary_prompt and "沉稳" in boundary_prompt  # 池词条
    assert "港区休整日" in boundary_prompt and "慵懒" in boundary_prompt  # 最近主题

    # Step2：当日边界、天气预报、静态日程、最近日程全部注入
    plan_prompt = context.plan_calls[0]["prompt"]
    for block in ("TODAY_BOUNDARY", "WEATHER_FORECAST", "STATIC_SCHEDULE",
                  "RECENT_SCHEDULES"):
        assert block in plan_prompt, block
    assert "小雨" in plan_prompt  # 天气预报
    assert "演习日" in plan_prompt and "沉稳" in plan_prompt  # Step1 产物

    # 裁判：拿到人设与当日边界
    judge_prompt = context.judge_calls[0]["prompt"]
    assert "角色设定" in judge_prompt
    assert "TODAY_BOUNDARY" in judge_prompt

    # 快照：全维度边界、四层齐全、AI 层为评委选中的候选 B
    snapshot = store.get(persona_hash, NOW.date(), "UTC")
    assert snapshot and snapshot["status"] == "ready"
    assert snapshot["boundary_state"]["daily_theme"] == "演习日"
    assert snapshot["boundary_state"]["daily_style"] == "沉稳"
    # 空数组=显式清空：昨日未完成计划不得被继承
    assert snapshot["boundary_state"]["unfinished_plans"] == []
    for layer in ("executed_slots", "user_slots", "ai_slots", "static_slots"):
        assert isinstance(snapshot.get(layer), list), layer
    assert snapshot["ai_slots"][0]["name"] == "上午训练"
    assert isinstance(snapshot.get("slots"), list) and snapshot["slots"]


def test_conversation_full_dynamic(tmp_path):
    """对话注入：天气 + 日程状态同轮出现。"""
    calendar_store = CalendarStore()
    tracker = LastChatTracker(str(tmp_path))
    service = TimeContextService(
        config={"time_awareness": {"time_guidance_enabled": True}},
        astrbot_config_provider=lambda: None,
        calendar_store=calendar_store,
        last_chat_tracker=tracker,
        now_provider=lambda: NOW,
    )
    text = service.build_dynamic(session=None, weather_text="晴转多云，20~31°C")
    assert "<WEATHER>今日天气基调：晴转多云，20~31°C</WEATHER>" in text
    assert "<SCHEDULE_STATE>" in text
