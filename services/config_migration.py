"""2.0.x 配置形态迁移（纯函数，无 AstrBot 依赖，可本地单测）。

把旧布尔开关形态一次性迁移为多选 selector 形态：

- ``time_awareness.{workday_state_enabled,lunar_state_enabled,almanac_enabled}``
  → ``time_awareness.time_sensors``（list，空列表 = 全部关闭）
- ``calendar.enable_builtin_events`` + ``calendar.builtin_events``（5 个分类布尔）
  → ``calendar.builtin_event_categories``（list，空列表 = 关闭内置现实事件）
- ``log.log_with_bot_id``（旧分组）→ 顶层 ``log_with_bot_id``

AstrBot 会在插件构造前按新 schema 补齐默认值并删除 schema 外键，因此旧键
必须继续以隐藏字段存在于 schema 中，本模块才有机会在构造后读取并迁移。
每个迁移只执行一次（hidden 标记）；并且只有在新键仍为 schema 默认值时才
写入——防止迁移标记因配置落盘失败丢失后，重跑迁移覆盖用户已修改的新配置。
"""

from __future__ import annotations

from typing import Any

# ---------------- 时间传感器 selector ----------------

SENSOR_KEYS = ("workday", "lunar", "almanac")

SENSOR_SELECT_DEFAULT = ["workday", "lunar"]

SENSOR_LEGACY_BOOLS = {
    "workday": "workday_state_enabled",
    "lunar": "lunar_state_enabled",
    "almanac": "almanac_enabled",
}

SENSOR_LEGACY_DEFAULTS = {"workday": True, "lunar": True, "almanac": False}

SENSOR_MIGRATED_FLAG = "_sensor_selector_migrated"

# ---------------- 内置事件 selector ----------------

BUILTIN_KEYS = (
    "legal_holidays",
    "traditional",
    "solar_terms",
    "political",
    "international",
)

BUILTIN_SELECT_DEFAULT = ["legal_holidays", "traditional", "solar_terms"]

BUILTIN_LEGACY_DEFAULTS = {
    "legal_holidays": True,
    "traditional": True,
    "solar_terms": True,
    "political": False,
    "international": False,
}

BUILTIN_MIGRATED_FLAG = "_builtin_selector_migrated"

# ---------------- 日志分组 ----------------

LOG_MIGRATED_FLAG = "_log_group_migrated"


def migrate_time_sensors(config: dict) -> bool:
    """旧传感器布尔 → ``time_sensors`` 列表；返回本次是否执行了迁移。"""
    time_cfg = config.get("time_awareness")
    if not isinstance(time_cfg, dict):
        return False
    if time_cfg.get(SENSOR_MIGRATED_FLAG, False):
        return False
    selected = [
        key
        for key, legacy_key in SENSOR_LEGACY_BOOLS.items()
        if bool(time_cfg.get(legacy_key, SENSOR_LEGACY_DEFAULTS[key]))
    ]
    current = time_cfg.get("time_sensors")
    if not isinstance(current, list) or current == SENSOR_SELECT_DEFAULT:
        # 新键缺失/仍是默认值（用户尚未修改）才写入旧值推导结果
        time_cfg["time_sensors"] = selected
    time_cfg[SENSOR_MIGRATED_FLAG] = True
    return True


def migrate_builtin_categories(config: dict) -> bool:
    """旧内置事件开关 → ``builtin_event_categories`` 列表；返回是否执行了迁移。"""
    calendar_cfg = config.get("calendar")
    if not isinstance(calendar_cfg, dict):
        return False
    if calendar_cfg.get(BUILTIN_MIGRATED_FLAG, False):
        return False
    if not bool(calendar_cfg.get("enable_builtin_events", True)):
        selected: list[str] = []
    else:
        legacy_group = calendar_cfg.get("builtin_events")
        group = legacy_group if isinstance(legacy_group, dict) else {}
        selected = [
            key
            for key in BUILTIN_KEYS
            if bool(group.get(key, BUILTIN_LEGACY_DEFAULTS[key]))
        ]
    current = calendar_cfg.get("builtin_event_categories")
    if not isinstance(current, list) or current == BUILTIN_SELECT_DEFAULT:
        calendar_cfg["builtin_event_categories"] = selected
    calendar_cfg[BUILTIN_MIGRATED_FLAG] = True
    return True


def migrate_log_group(config: dict) -> bool:
    """旧 ``log`` 分组 → 顶层 ``log_with_bot_id``；返回是否执行了迁移。

    AstrBot 会在插件构造前按新 schema 补齐顶层默认值（False），因此首次加载
    时顶层值必为默认、旧分组才持有用户真实设置；直接用旧值覆盖。
    若顶层键已是 True（迁移标记因落盘失败丢失、用户之后手动改过），则跳过
    覆盖，避免旧配置影响新配置。
    """
    legacy = config.get("log")
    if not isinstance(legacy, dict):
        return False
    if legacy.get(LOG_MIGRATED_FLAG, False):
        return False
    if not bool(config.get("log_with_bot_id", False)):
        config["log_with_bot_id"] = bool(legacy.get("log_with_bot_id", False))
    legacy[LOG_MIGRATED_FLAG] = True
    return True
