"""
时间工具模块。

提供时间相关的工具函数：时区解析、当前时间、跨午夜时段判定、睡眠窗口检测。
"""

import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from ..log import logger, tag

from ..constants import DEFAULT_SLEEP_PROMPT, LEGACY_DEFAULT_SLEEP_PROMPT



def _get_astrbot_timezone(astrbot_config) -> str:
    """从 AstrBot 全局配置中读取时区字符串。"""
    try:
        if hasattr(astrbot_config, "get"):
            tz = astrbot_config.get("timezone", "") or ""
            if tz:
                return tz
        if hasattr(astrbot_config, "timezone"):
            return astrbot_config.timezone or ""
    except Exception as e:
        logger.debug(f"{tag()} 读取 AstrBot 时区失败: {e}")
    return ""


def get_tz(config: dict, astrbot_config=None):
    """获取有效时区对象。

    优先级：AstrBot 全局时区（需启用开关）> 插件自身时区 > 系统本地时区。
    """
    time_awareness_config = config.get("time_awareness", {})

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
    if not tz_str:
        return None
    try:
        return ZoneInfo(tz_str)
    except (ZoneInfoNotFoundError, KeyError) as e:
        logger.warning(f"{tag()} ⚠️ 无效的时区配置 '{tz_str}': {e}，回退到系统本地时区")
        return None


def get_now(config: dict, astrbot_config=None) -> datetime.datetime:
    """获取当前时间（使用有效时区）。"""
    tz = get_tz(config, astrbot_config)
    if tz is not None:
        return datetime.datetime.now(tz=tz)
    return datetime.datetime.now()


def is_in_time_range(time_range: str, tz=None) -> bool:
    """检查当前时间是否在指定范围内。支持跨午夜（如 "22:00-8:00"）。"""
    try:
        start_time, end_time = time_range.split("-")
        start_hour, start_min = map(int, start_time.split(":"))
        end_hour, end_min = map(int, end_time.split(":"))

        now = (
            datetime.datetime.now(tz=tz) if tz is not None else datetime.datetime.now()
        )
        current_minutes = now.hour * 60 + now.minute
        start_minutes = start_hour * 60 + start_min
        end_minutes = end_hour * 60 + end_min

        if start_minutes > end_minutes:
            return current_minutes >= start_minutes or current_minutes <= end_minutes
        else:
            return start_minutes <= current_minutes <= end_minutes
    except Exception as e:
        logger.warning(f"{tag()} ⚠️ 时间范围解析错误: {e}")
        return False


def is_sleep_time(config: dict, astrbot_config=None) -> bool:
    """检查当前是否处于睡眠时间段。"""
    time_awareness_config = config.get("time_awareness", {})
    if not time_awareness_config.get("sleep_mode_enabled", False):
        return False
    sleep_hours = time_awareness_config.get("sleep_hours", "22:00-8:00")
    tz = get_tz(config, astrbot_config)
    return is_in_time_range(sleep_hours, tz=tz)


def get_sleep_prompt_if_active(config: dict, astrbot_config=None) -> str:
    """获取睡眠提示文本。不在睡眠时段则返回空字符串。

    用户配置为空或等于旧默认值时，回退到新默认值（与 constants.DEFAULT_SLEEP_PROMPT 一致）。
    """
    if not is_sleep_time(config, astrbot_config):
        return ""
    time_awareness_config = config.get("time_awareness", {})
    custom = (time_awareness_config.get("sleep_prompt") or "").strip()
    if not custom or custom == LEGACY_DEFAULT_SLEEP_PROMPT:
        return DEFAULT_SLEEP_PROMPT
    return custom
