"""随机天气端到端：生成链路 WEATHER_FORECAST 注入、对话链路 <WEATHER>、时区种子一致。"""

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
    time_context = FakeTimeContext()
    service = DailyScheduleService(
        context=context,
        config=config,
        store=store,
        time_context=time_context,
        persona_resolver=FakePersonaResolver("persona_demo"),
        concurrency_gate=None,
        sleep=_no_sleep,
        weather_forecast_provider=_fixed_forecast,
    )
    return service, store


def test_generation_injects_weather_forecast(tmp_path):
    """生成提示词含 <WEATHER_FORECAST>，快照正常落库。"""
    config = _adaptive_config(adaptive={"enabled": True})
    context = FakeContext([json.dumps(
        [{"name": "睡眠", "start": "00:00", "end": "08:00", "state": "在睡觉"}],
        ensure_ascii=False,
    )])
    service, store = _service(tmp_path, context, config)

    result = asyncio.run(service.generate_for_session(SESSION, now=NOW))
    assert result.success
    prompt = context.plan_calls[0]["prompt"]
    assert "WEATHER_FORECAST" in prompt
    assert '"text_day": "小雨"' in prompt and '"text_night": "多云"' in prompt
    assert "precip" not in prompt
    snapshot = store.get(store.persona_hash("persona_demo"), NOW.date(), "UTC")
    assert snapshot and snapshot["status"] == "ready"


def test_conversation_injects_weather_label(tmp_path):
    """build_dynamic：带天气输出 <WEATHER>，不带则静默省略。"""
    calendar_store = CalendarStore()
    tracker = LastChatTracker(str(tmp_path))
    service = TimeContextService(
        config={"time_awareness": {"time_guidance_enabled": True}},
        astrbot_config_provider=lambda: None,
        calendar_store=calendar_store,
        last_chat_tracker=tracker,
        now_provider=lambda: NOW,
    )
    with_wx = service.build_dynamic(session=None, weather_text="晴转多云，20~31°C")
    assert "<WEATHER>今日天气基调：晴转多云，20~31°C</WEATHER>" in with_wx
    without = service.build_dynamic(session=None, weather_text=None)
    assert "WEATHER" not in without


def test_timezone_aware_seed_matches_provider_date():
    """OS 时区与插件时区不一致时，天气种子仍以插件时区日期为准。"""
    from time_awareness.core.weather_sensor import WeatherSensor

    aware = datetime.datetime(
        2026, 8, 24, 1, 0,
        tzinfo=datetime.timezone(datetime.timedelta(hours=8)),  # 东八区凌晨 1 点
    )
    sensor = WeatherSensor(
        config={"daily_schedule": {"ai_daily": {"enabled": True, "random_weather_enabled": True}}},
        now_provider=lambda: aware,
    )
    current = asyncio.run(sensor.current_weather())
    forecast = asyncio.run(sensor.daily_forecast(aware.date()))
    # 同一插件时区日期：实况与预报必须同一基调（即使 OS 本地是前一天）
    assert current["text_day"] == forecast["textDay"]
    assert forecast["fxDate"] == "2026-08-24"
