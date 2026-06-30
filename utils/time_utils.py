"""
时间工具模块。

提供时间相关的工具函数：时区解析、当前时间、跨午夜时段判定、日程表时段命中。
"""

import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from ..log import logger, tag



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


def find_active_schedule_slot(
    daily_schedule_config: dict,
    now: "datetime.datetime | None" = None,
) -> dict | None:
    """返回当前时间命中的时段 dict；未命中返回 None。

    用于 {daily_schedule_state} 占位符解析。命中规则：
    - 遍历 schedule_templates，每个时段用 _is_in_range 判断（支持跨午夜）
    - end_time exclusive：当前时间 < end_time 才算命中，连续时段边界（如 08:00）不会双命中
    - 多个时段命中时取列表顺序的第一个（运行期不重排）

    Args:
        daily_schedule_config: config["daily_schedule"] 子字典
        now: 已带正确时区（与 fire_at 同源）的当前时间；为 None 时取系统当前时间（naive）
    """
    slots = daily_schedule_config.get("schedule_templates") or []
    if not isinstance(slots, list) or not slots:
        return None

    if now is None:
        now = datetime.datetime.now()

    for slot in slots:
        if not isinstance(slot, dict):
            continue
        start = str(slot.get("start_time", "")).strip()
        end = str(slot.get("end_time", "")).strip()
        if not start or not end:
            continue
        if _is_in_range(now, f"{start}-{end}"):
            return slot
    return None


def _is_in_range(now: "datetime.datetime", time_range: str) -> bool:
    """检查 now 是否落在 time_range 内（end exclusive）。支持跨午夜。"""
    try:
        start_time, end_time = time_range.split("-")
        start_hour, start_min = map(int, start_time.strip().split(":"))
        end_hour, end_min = map(int, end_time.strip().split(":"))
        current_minutes = now.hour * 60 + now.minute
        start_minutes = start_hour * 60 + start_min
        end_minutes = end_hour * 60 + end_min
        if start_minutes > end_minutes:
            # 跨午夜：当前 >= start 或 当前 < end（end 是次日时刻，exclusive）
            return current_minutes >= start_minutes or current_minutes < end_minutes
        else:
            return start_minutes <= current_minutes < end_minutes
    except Exception as e:
        logger.warning(f"{tag()} ⚠️ 时段范围解析错误 '{time_range}': {e}")
        return False
