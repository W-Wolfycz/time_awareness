"""随机天气：白天/黑夜各自独立加权抽取，日期种子保证全天恒定（预报=实况）。"""

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
    salt: str = "",
) -> dict:
    """返回当日两段基调；pool 空时用内置默认池，salt 为实例盐（同盐内当天天气一致）。"""
    pool = pool or SCENARIOS
    # sha256 派生 int 种子，保证跨版本/跨进程同一天天气恒定
    seed = int.from_bytes(
        hashlib.sha256(f"ta-random-weather-{date.isoformat()}-{salt}".encode()).digest()[:8],
        "big",
    )
    rnd = random.Random(seed)
    text_day = _pick(rnd, pool)
    text_night = _pick(rnd, pool)
    # 温度=月份基准+天气温差+±1°C 抖动：同月同天气基本恒定，无随机跳变
    base_high = _MONTH_BASE_HIGH.get(date.month, 20)
    day_high = base_high + _WEATHER_TEMP_DELTA.get(text_day, 0) + rnd.randint(-1, 1)
    night_low = base_high - 7 + _WEATHER_TEMP_DELTA.get(text_night, 0) + rnd.randint(-1, 1)
    night_low = min(night_low, day_high - 3)  # 极端组合（白天雷雨、夜里晴）也保证最低温低于最高温
    return {
        "text_day": text_day,
        "text_night": text_night,
        "temp_max": int(day_high),
        "temp_min": int(night_low),
        "variation": _variation(rnd, text_day, text_night),
    }


def _fmt_minute(minute: int) -> str:
    minute = max(0, min(24 * 60 - 1, minute))
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _variation(rnd: random.Random, text_day: str, text_night: str) -> str:
    """确定性变化窗口：雷阵雨给随机 1-2h 窗口，转场给 17-20 点转场点，小雨给间歇表述。"""
    if text_day == "雷阵雨":
        start = 13 * 60 + rnd.randint(0, 240)  # 13:00-17:00 起，留足 1-2h 余量
        duration = rnd.choice((60, 90, 120))
        return f"{_fmt_minute(start)}-{_fmt_minute(start + duration)} 阵雨，其余多云"
    if text_night == "雷阵雨":
        start = 18 * 60 + rnd.randint(0, 180)  # 18:00-21:00 起
        duration = rnd.choice((60, 90, 120))
        return f"{_fmt_minute(start)}-{_fmt_minute(start + duration)} 阵雨，其余阴"
    if text_night and text_night != text_day:
        # 转场优先于「间歇小雨」：晴转小雨这类给明确转场点更有信息量
        turn = 17 * 60 + rnd.randint(0, 180)  # 17:00-20:00
        return f"{_fmt_minute(turn)} 前后转{text_night}"
    if "小雨" in (text_day, text_night):
        return "间歇性小雨，时断时续"
    return ""
