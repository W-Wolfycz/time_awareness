"""随机天气种子与插件时区回归（纯函数 + 门面单测）。

覆盖 AGENTS.md 记录过的事故面：按日种子必须用插件时区日期（禁用 OS 本地时区，
否则差一天）、种子 (date, salt) 确定、同日稳定且实况=预报、温度合理。
"""

import asyncio
import datetime

from time_awareness.core.weather_sensor import WeatherSensor


def _sensor(tmp_path, **ai_daily):
    base = {"enabled": True, "random_weather_enabled": True}
    base.update(ai_daily)
    return WeatherSensor(
        config={"daily_schedule": {"ai_daily": base}}, data_dir=str(tmp_path)
    )


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
