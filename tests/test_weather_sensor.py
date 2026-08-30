"""随机天气门面单测：开关、同日稳定、预报=实况、用户池、加权分布 sanity。"""

import asyncio
import datetime

from time_awareness.core.weather.random_weather import daily_weather_outline, parse_pool
from time_awareness.core.weather_sensor import WeatherSensor


def _make(**ai_daily):
    base = {"enabled": True, "random_weather_enabled": True}
    base.update(ai_daily)
    return WeatherSensor(config={"daily_schedule": {"ai_daily": base}})


def test_enabled_flag():
    assert _make(random_weather_enabled=True).enabled()
    assert not _make(random_weather_enabled=False).enabled()
    assert not WeatherSensor(config={}).enabled()


def test_same_day_stable_and_forecast_matches_reality():
    sensor = _make()
    first = asyncio.run(sensor.current_weather())
    second = asyncio.run(sensor.current_weather())
    assert first == second
    assert set(first) == {"text_day", "text_night", "temp_min", "temp_max"}

    today = datetime.date.today()
    forecast = asyncio.run(sensor.daily_forecast(today))
    assert forecast["textDay"] == first["text_day"]
    assert forecast["textNight"] == first["text_night"]
    assert forecast["tempMax"] == str(first["temp_max"])
    assert forecast["tempMin"] == str(first["temp_min"])


def test_disabled_returns_none():
    sensor = _make(random_weather_enabled=False)
    assert asyncio.run(sensor.current_weather()) is None
    assert asyncio.run(sensor.daily_forecast(datetime.date.today())) is None
    ok, message = asyncio.run(sensor.validate())
    assert not ok and "未启用" in message


def test_validate_ok_when_enabled():
    sensor = _make()
    ok, message = asyncio.run(sensor.validate())
    assert ok and "°C" in message


def test_render_current_empty_and_partial():
    assert WeatherSensor.render_current({}) == ""
    assert WeatherSensor.render_current({"text_day": "晴"}) == "晴"
    assert WeatherSensor.render_current({"temp_min": 1, "temp_max": 2}) == "1~2°C"


def test_render_current_whole_day_sentence():
    assert (
        WeatherSensor.render_current(
            {"text_day": "晴", "text_night": "多云", "temp_min": 20, "temp_max": 31}
        )
        == "晴转多云，20~31°C"
    )
    assert (
        WeatherSensor.render_current(
            {"text_day": "小雨", "text_night": "小雨", "temp_min": 18, "temp_max": 23}
        )
        == "小雨，18~23°C"
    )


def test_parse_pool():
    assert parse_pool(["晴,30", "小雨,10", "坏行", "", "阴"]) == (("晴", 30), ("小雨", 10))
    assert parse_pool([]) == parse_pool(None)  # 空/无 → 内置默认
    assert parse_pool(["雪,5"]) == (("雪", 5),)
    assert parse_pool(["全是坏行", "阴", "雨,-1", "晴,0"]) == parse_pool([])  # 全非法 → 内置
    assert parse_pool(["晴,30", "晴,70"]) == (("晴", 30),)  # 同名保留第一个


def test_custom_pool_controls_weather():
    sensor = _make(weather_pool=["雪,100"])
    current = asyncio.run(sensor.current_weather())
    assert current["text_day"] == "雪" and current["text_night"] == "雪"


def test_fixed_seed_deterministic_and_diverse():
    """固定日期种子回归：30 天输出确定且多样，权重高的「晴」不少于最低档。"""
    from collections import Counter

    counter = Counter()
    for offset in range(30):
        date = datetime.date(2026, 8, 1) + datetime.timedelta(days=offset)
        counter[daily_weather_outline(date)["text_day"]] += 1
    assert len(counter) >= 3
    assert counter["晴"] >= counter["雾"]


def test_temperature_smooth_and_sane():
    """8 月 31 天：相邻日温差有界（只随天气变化）、温度在合理区间、最低<最高。"""
    dates = [datetime.date(2026, 8, 1) + datetime.timedelta(days=i) for i in range(31)]
    outlines = [daily_weather_outline(d) for d in dates]
    highs = [o["temp_max"] for o in outlines]
    for a, b in zip(highs, highs[1:]):
        assert abs(a - b) <= 10  # 极端天气切换（晴↔雷阵雨 8°C）+ 抖动 ±2
    assert all(20 <= h <= 42 for h in highs)
    assert all(o["temp_min"] < o["temp_max"] for o in outlines)
