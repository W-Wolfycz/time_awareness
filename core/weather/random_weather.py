"""随机天气提供者：白天/黑夜各自按权重独立抽取，日期种子保证全天稳定。

- 同一天恒定（种子=日期）：对话注入与生成预报永远一致，零「预报失准」。
- 池子只放单段天气；白天与黑夜各自独立加权抽取（白天晴、夜里小雨是合法的）。
- 基调由系统规定，AI 只负责把白天/黑夜基调落到具体时段（哪段下雨、哪段多云），
  不脱离基调自创天气。
"""

from __future__ import annotations

import datetime
import hashlib
import random

# (天气现象, 权重)。权重总和不必为 100，按占比随机。
SCENARIOS: tuple[tuple[str, int], ...] = (
    ("晴", 30),
    ("多云", 25),
    ("阴", 15),
    ("小雨", 10),
    ("雷阵雨", 5),
    ("大风", 4),
    ("雾", 3),
)

# 月度基准高温（℃）。
_MONTH_BASE_HIGH = {
    1: 5, 2: 7, 3: 13, 4: 20, 5: 25, 6: 29,
    7: 32, 8: 31, 9: 27, 10: 21, 11: 14, 12: 8,
}
# 天气现象相对当月基准的温差（℃）：晴热雨凉，跨天变化有界且自然。
_WEATHER_TEMP_DELTA = {
    "晴": 4, "多云": 2, "阴": 0, "小雨": -3,
    "雷阵雨": -4, "大风": -1, "雾": -2,
}


def parse_pool(lines) -> tuple[tuple[str, int], ...]:
    """解析「天气,权重」行列表；非法行跳过；全部非法/为空返回内置默认池。"""
    parsed: list[tuple[str, int]] = []
    for line in lines or ():
        raw = str(line or "").strip()
        if not raw:
            continue
        parts = raw.split(",")
        if len(parts) != 2:
            continue
        name = parts[0].strip()
        if not name:
            continue
        try:
            weight = int(parts[1].strip())
        except ValueError:
            continue
        if weight > 0 and name not in {n for n, _ in parsed}:
            parsed.append((name, weight))
    return tuple(parsed) if parsed else SCENARIOS


def _pick(rnd: random.Random, pool: tuple[tuple[str, int], ...]) -> str:
    total = sum(weight for _, weight in pool)
    pick = rnd.randint(1, total)
    for name, weight in pool:
        if pick <= weight:
            return name
        pick -= weight
    return pool[0][0]


def daily_weather_outline(
    date: datetime.date,
    pool: tuple[tuple[str, int], ...] | None = None,
) -> dict:
    """返回当日两段基调：{text_day, text_night, temp_max, temp_min}。

    ``pool`` 为空时使用内置默认权重池；调用方可用 ``parse_pool`` 传入用户配置。
    """
    pool = pool or SCENARIOS
    # 字符串播种实现历史上变过：用 sha256 派生显式 int 种子，保证跨 Python
    # 版本/跨进程同一天天气恒定（「同日恒定」不依赖版本实现细节）。
    seed = int.from_bytes(
        hashlib.sha256(f"ta-random-weather-{date.isoformat()}".encode()).digest()[:8],
        "big",
    )
    rnd = random.Random(seed)
    text_day = _pick(rnd, pool)
    text_night = _pick(rnd, pool)
    # 温度=月份基准 + 天气现象温差 + ±1°C 日抖动：同月同天气基本恒定，
    # 相邻日温差只来自天气变化（晴转雨降 6-7°C 等自然幅度），无随机跳变。
    base_high = _MONTH_BASE_HIGH.get(date.month, 20)
    day_high = base_high + _WEATHER_TEMP_DELTA.get(text_day, 0) + rnd.randint(-1, 1)
    night_low = base_high - 7 + _WEATHER_TEMP_DELTA.get(text_night, 0) + rnd.randint(-1, 1)
    night_low = min(night_low, day_high - 3)  # 极端组合（白天雷雨、夜里晴）也保证最低温低于最高温
    return {
        "text_day": text_day,
        "text_night": text_night,
        "temp_max": int(day_high),
        "temp_min": int(night_low),
    }
