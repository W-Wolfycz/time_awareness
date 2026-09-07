"""时间工具：时区解析、当前时间、跨午夜判定、日程命中。"""

import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..domain.schedule import (
    ScheduleValidationError,
    find_active_static_slot,
    is_minute_in_static_range,
    minute_of_day,
    parse_time,
)
from ..log import logger, tag

UTC = datetime.timezone.utc


def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
    """将任意输入解析为 [low, high] 区间内的整数；解析失败返回 default。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


def _get_astrbot_timezone(astrbot_config) -> str:
    """从 AstrBot 全局配置中读取时区字符串。"""
    try:
        if hasattr(astrbot_config, "get"):
            tz = astrbot_config.get("timezone", "") or ""
            if tz and isinstance(tz, str):
                return tz
        if hasattr(astrbot_config, "timezone"):
            tz = astrbot_config.timezone or ""
            if isinstance(tz, str):
                return tz
    except Exception as e:
        logger.debug(f"{tag()} 读取 AstrBot 时区失败: {e}")
    return ""


def get_tz(config: dict, astrbot_config=None):
    """获取有效时区对象：AstrBot 全局时区 > 插件自身时区 > 系统本地时区。"""
    time_awareness_config = config.get("time_awareness", {})
    if not isinstance(time_awareness_config, dict):
        time_awareness_config = {}

    use_astrbot = time_awareness_config.get("use_astrbot_timezone", False)
    if use_astrbot and astrbot_config is not None:
        tz_str = _get_astrbot_timezone(astrbot_config)
        if tz_str:
            try:
                return ZoneInfo(tz_str)
            except (ZoneInfoNotFoundError, KeyError) as e:
                logger.warning(
                    f"{tag()} ⚠️ AstrBot 时区配置无效 '{tz_str}': {e}，回退到插件时区配置"
                )
        elif use_astrbot:
            logger.debug(
                f"{tag()} 已启用「跟随 AstrBot 时区」但 AstrBot 未配置时区，回退到插件时区配置"
            )

    tz_str = time_awareness_config.get("timezone", "")
    if not tz_str or not isinstance(tz_str, str):
        return None
    try:
        return ZoneInfo(tz_str)
    except (ZoneInfoNotFoundError, KeyError, TypeError) as e:
        logger.warning(f"{tag()} ⚠️ 无效的时区配置 '{tz_str}': {e}，回退到系统本地时区")
        return None


def timezone_from_key(key: str) -> datetime.tzinfo | None:
    """解析快照时区键；支持 IANA、UTC 与 ``UTC±HH:MM``。"""
    value = str(key or "").strip()
    if not value or value == "system-local":
        return datetime.datetime.now().astimezone().tzinfo
    if value == "UTC":
        return UTC
    if value.startswith("UTC") and len(value) == 9 and value[3] in "+-":
        try:
            hours, minutes = (int(part) for part in value[4:].split(":", 1))
            if hours > 23 or minutes > 59:
                return None
            delta = datetime.timedelta(hours=hours, minutes=minutes)
            return datetime.timezone(delta if value[3] == "+" else -delta)
        except (TypeError, ValueError):
            return None
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, KeyError, TypeError, ValueError):
        return None


def to_timezone_key(dt: datetime.datetime, key: str) -> datetime.datetime:
    """把当前时刻转换到快照记录的时区；无效键保持原值。"""
    timezone = timezone_from_key(key)
    if timezone is None:
        return dt
    if dt.tzinfo is None:
        local_tz = datetime.datetime.now().astimezone().tzinfo or UTC
        dt = dt.replace(tzinfo=local_tz)
    return dt.astimezone(timezone)


def get_now(config: dict, astrbot_config=None) -> datetime.datetime:
    """获取当前时间（使用有效时区）。"""
    tz = get_tz(config, astrbot_config)
    if tz is not None:
        return datetime.datetime.now(tz=tz)
    return datetime.datetime.now()


def to_utc(
    dt: datetime.datetime,
    default_tz: datetime.tzinfo | None = None,
) -> datetime.datetime:
    """将时间转换成内部统一使用的 UTC aware datetime（naive 用 default_tz 或系统本地时区解释）。"""
    if not isinstance(dt, datetime.datetime):
        raise TypeError(f"dt 必须是 datetime.datetime，收到 {type(dt)!r}")
    if dt.tzinfo is None:
        if default_tz is None:
            default_tz = datetime.datetime.now().astimezone().tzinfo or UTC
        dt = dt.replace(tzinfo=default_tz)
    return dt.astimezone(UTC)


def utc_now() -> datetime.datetime:
    """返回 UTC aware 当前时间。"""
    return datetime.datetime.now(UTC)


def coerce_to_aware(dt: datetime.datetime, reference: datetime.datetime) -> datetime.datetime:
    """把 dt 的时区属性规整到与 reference 同性质，避免 aware/naive 比较报错。"""
    if dt.tzinfo is None and reference.tzinfo is not None:
        return dt.replace(tzinfo=reference.tzinfo)
    if dt.tzinfo is not None and reference.tzinfo is None:
        local_tz = datetime.datetime.now().astimezone().tzinfo or UTC
        return dt.astimezone(local_tz).replace(tzinfo=None)
    if dt.tzinfo is not None and reference.tzinfo is not None:
        return dt.astimezone(reference.tzinfo)
    return dt


def find_active_schedule_slot(
    daily_schedule_config: dict,
    now: "datetime.datetime | None" = None,
) -> dict | None:
    """返回当前时间命中的时段 dict；未命中返回 None（用于 <SCHEDULE_STATE> 状态文本解析）。"""
    slots = daily_schedule_config.get("schedule_templates") or []
    if not isinstance(slots, list) or not slots:
        return None
    if now is None:
        now = datetime.datetime.now()
    return find_active_static_slot(slots, now)


def _is_in_range(now: "datetime.datetime", time_range: str) -> bool:
    """检查 now 是否落在 time_range 内（end exclusive）。支持跨午夜。"""
    try:
        start_time, end_time = time_range.split("-", 1)
        start = parse_time(start_time.strip(), strict=False)
        end = parse_time(end_time.strip(), strict=False)
        return is_minute_in_static_range(minute_of_day(now), start, end)
    except (ScheduleValidationError, ValueError) as e:
        logger.warning(f"{tag()} ⚠️ 时段范围解析错误 '{time_range}': {e}")
        return False
