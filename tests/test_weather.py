"""随机天气种子与插件时区回归（纯函数 + 门面单测）。

覆盖 AGENTS.md 记录过的事故面：按日种子必须用插件时区日期（禁用 OS 本地时区，
否则差一天）、种子 (date, salt) 确定、同日稳定且实况=预报、温度合理。
"""

import asyncio
import datetime
from collections import Counter

from time_awareness.core.weather.random_weather import daily_weather_outline
from time_awareness.core.weather_sensor import WeatherSensor


def _sensor(tmp_path, **ai_daily):
    base = {"enabled": True, "random_weather_enabled": True}
    base.update(ai_daily)
    return WeatherSensor(
        config={"daily_schedule": {"ai_daily": base}}, data_dir=str(tmp_path)
    )


def test_seed_deterministic_and_diverse():
    """固定日期输出确定且多样：30 天至少 3 种天气，高权重「晴」不少于最低档。"""
    counter = Counter()
    for offset in range(30):
        date = datetime.date(2026, 8, 1) + datetime.timedelta(days=offset)
        first = daily_weather_outline(date)
        second = daily_weather_outline(date)
        assert first == second  # 同日期同种子 → 完全确定
        counter[first["text_day"]] += 1
    assert len(counter) >= 3
    assert counter["晴"] >= counter["雾"]


def test_same_day_stable_and_forecast_matches_reality(tmp_path):
    """同部署同日稳定；实况与预报同基调（同一插件时区日期）。"""
    sensor = _sensor(tmp_path)
    first = asyncio.run(sensor.current_weather())
    second = asyncio.run(sensor.current_weather())
    assert first == second
    assert set(first) == {"text_day", "text_night", "temp_min", "temp_max", "variation"}

    today = datetime.date.today()
    forecast = asyncio.run(sensor.daily_forecast(today))
    assert forecast["textDay"] == first["text_day"]
    assert forecast["textNight"] == first["text_night"]
    assert forecast["tempMax"] == str(first["temp_max"])
    assert forecast["tempMin"] == str(first["temp_min"])


def test_plugin_timezone_date_seed_not_os_local(tmp_path):
    """OS 时区与插件时区不一致时，天气种子仍以插件时区日期为准。"""
    aware = datetime.datetime(
        2026, 8, 24, 1, 0,
        tzinfo=datetime.timezone(datetime.timedelta(hours=8)),  # 东八区凌晨 1 点
    )
    sensor = WeatherSensor(
        config={
            "daily_schedule": {"ai_daily": {"enabled": True, "random_weather_enabled": True}}
        },
        data_dir=str(tmp_path),
        now_provider=lambda: aware,
    )
    current = asyncio.run(sensor.current_weather())
    forecast = asyncio.run(sensor.daily_forecast(aware.date()))
    # 同一插件时区日期：实况与预报必须同一基调（即使 OS 本地是前一天）
    assert current["text_day"] == forecast["textDay"]
    assert forecast["fxDate"] == "2026-08-24"


def test_temperature_smooth_and_sane():
    """8 月 31 天：相邻日温差有界、温度在合理区间、最低<最高。"""
    dates = [datetime.date(2026, 8, 1) + datetime.timedelta(days=i) for i in range(31)]
    outlines = [daily_weather_outline(d) for d in dates]
    highs = [o["temp_max"] for o in outlines]
    for a, b in zip(highs, highs[1:]):
        assert abs(a - b) <= 10  # 极端天气切换（晴↔雷阵雨 8°C）+ 抖动 ±2
    assert all(20 <= h <= 42 for h in highs)
    assert all(o["temp_min"] < o["temp_max"] for o in outlines)
