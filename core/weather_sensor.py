"""天气传感器门面：随机天气（白天/黑夜加权抽取），提供 enabled/current_weather/daily_forecast/validate。"""

from __future__ import annotations

import datetime
import secrets
import time
from typing import Callable

from ._datafile import atomic_write_yaml, load_mapping
from .weather.random_weather import daily_weather_outline, parse_pool


class WeatherSensor:
    def __init__(
        self,
        *,
        config: dict,
        data_dir: str = "",
        now_provider: Callable[[], float] | None = None,
    ):
        daily = config.get("daily_schedule", {}) if isinstance(config, dict) else {}
        ai_daily = daily.get("ai_daily", {}) if isinstance(daily, dict) else {}
        self._enabled_flag = bool(
            (ai_daily or {}).get("enabled", False)
            and (ai_daily or {}).get("random_weather_enabled", False)
        )
        raw_pool = (ai_daily or {}).get("weather_pool")
        self._pool = parse_pool(raw_pool if isinstance(raw_pool, list) else None)
        self._now = now_provider or self._default_now
        self._data_dir = str(data_dir or "")
        self._salt = self._ensure_instance_salt()

    @staticmethod
    def _default_now() -> float:
        return time.time()

    def enabled(self) -> bool:
        return self._enabled_flag

    def _today(self) -> datetime.date:
        now = self._now()
        if isinstance(now, datetime.datetime):
            # 插件时区提供的 aware datetime：以它的日期为准（与日程 local_date 同源）
            return now.date()
        if isinstance(now, (int, float)):
            return datetime.date.fromtimestamp(now)
        return datetime.date.today()

    def _ensure_instance_salt(self) -> str:
        """读取/生成实例盐：同数据目录共用，不同设备各自生成（同一天跨设备天气不同）。"""
        if not self._data_dir:
            return ""
        path = f"{self._data_dir}/weather_cache.yaml"
        cache = load_mapping(path)
        if isinstance(cache, dict) and cache.get("instance_salt"):
            return str(cache["instance_salt"])
        salt = secrets.token_hex(8)
        cache = cache if isinstance(cache, dict) else {}
        cache["instance_salt"] = salt
        atomic_write_yaml(path, cache, header="time_awareness weather cache")
        return salt

    def _outline(self, date: datetime.date) -> dict:
        return daily_weather_outline(date, pool=self._pool, salt=self._salt)

    async def current_weather(self) -> dict | None:
        """当日两段基调；未启用返回 None（调用方省略 <WEATHER> 标签）。"""
        if not self.enabled():
            return None
        return self._outline(self._today())

    async def daily_forecast(self, date) -> dict | None:
        """目标日期（date 对象）的两段基调，字段与生成提示词协议一致。"""
        if not self.enabled():
            return None
        outline = self._outline(date)
        return {
            "fxDate": date.isoformat(),
            "textDay": outline["text_day"],
            "textNight": outline["text_night"],
            "tempMax": str(outline["temp_max"]),
            "tempMin": str(outline["temp_min"]),
            "variation": str(outline.get("variation", "") or ""),
        }

    async def validate(self) -> tuple[bool, str]:
        """即时返回今日基调（无网络），供 WebUI 按钮回显。"""
        if not self.enabled():
            return False, "随机天气未启用（daily_schedule.ai_daily.random_weather_enabled）"
        return True, self.render_current(self._outline(self._today()))

    @staticmethod
    def render_current(data: dict) -> str:
        """一句话表述：晴转多云，20~31°C（两段相同则只写一种）。"""
        text_day = str(data.get("text_day", "") or "").strip()
        text_night = str(data.get("text_night", "") or "").strip()
        low = data.get("temp_min")
        high = data.get("temp_max")
        if text_night and text_night != text_day:
            head = f"{text_day}转{text_night}"
        else:
            head = text_day or text_night
        tail = f"{low}~{high}°C" if low is not None and high is not None else ""
        variation = str(data.get("variation", "") or "").strip()
        return "，".join(part for part in (head, tail, variation) if part)
