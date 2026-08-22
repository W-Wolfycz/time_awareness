"""2.0.x 配置形态迁移测试（布尔开关 → 多选 selector）。"""

from time_awareness.services.config_migration import (
    migrate_builtin_categories,
    migrate_log_group,
    migrate_time_sensors,
)


def test_migrate_time_sensors_defaults():
    config = {"time_awareness": {"workday_state_enabled": True, "lunar_state_enabled": True,
                                 "almanac_enabled": False}}
    assert migrate_time_sensors(config) is True
    assert config["time_awareness"]["time_sensors"] == ["workday", "lunar"]
    # 一次性标记：再跑不重复迁移，用户后续修改不会被旧值覆盖
    config["time_awareness"]["time_sensors"] = ["almanac"]
    assert migrate_time_sensors(config) is False
    assert config["time_awareness"]["time_sensors"] == ["almanac"]


def test_migrate_time_sensors_respects_explicit_legacy_values():
    config = {"time_awareness": {"workday_state_enabled": False, "lunar_state_enabled": False,
                                 "almanac_enabled": True}}
    assert migrate_time_sensors(config) is True
    assert config["time_awareness"]["time_sensors"] == ["almanac"]


def test_migrate_time_sensors_skips_non_dict_group():
    config = {"time_awareness": "broken"}
    assert migrate_time_sensors(config) is False


def test_migrate_builtin_categories_uses_legacy_group():
    config = {
        "calendar": {
            "enable_builtin_events": True,
            "builtin_events": {
                "legal_holidays": True,
                "traditional": False,
                "solar_terms": True,
                "political": True,
                "international": False,
            },
        }
    }
    assert migrate_builtin_categories(config) is True
    assert config["calendar"]["builtin_event_categories"] == [
        "legal_holidays",
        "solar_terms",
        "political",
    ]
    # 一次性标记
    config["calendar"]["builtin_event_categories"] = []
    assert migrate_builtin_categories(config) is False
    assert config["calendar"]["builtin_event_categories"] == []


def test_migrate_builtin_categories_disabled_master_switch():
    config = {"calendar": {"enable_builtin_events": False, "builtin_events": {}}}
    assert migrate_builtin_categories(config) is True
    assert config["calendar"]["builtin_event_categories"] == []


def test_migrate_builtin_categories_missing_legacy_group_uses_defaults():
    config = {"calendar": {"enable_builtin_events": True}}
    assert migrate_builtin_categories(config) is True
    assert config["calendar"]["builtin_event_categories"] == [
        "legal_holidays",
        "traditional",
        "solar_terms",
    ]


def test_migrate_log_group_promotes_to_top_level():
    config = {"log": {"log_with_bot_id": True}}
    assert migrate_log_group(config) is True
    assert config["log_with_bot_id"] is True
    # 一次性标记
    config["log"]["log_with_bot_id"] = False
    assert migrate_log_group(config) is False
    assert config["log_with_bot_id"] is True


def test_migrate_log_group_overwrites_schema_default_with_legacy_value():
    """AstrBot 会先补顶层默认值，因此一次性迁移必须用旧分组值覆盖顶层键。"""
    config = {"log": {"log_with_bot_id": True}, "log_with_bot_id": False}
    assert migrate_log_group(config) is True
    assert config["log_with_bot_id"] is True
    # 一次性标记：再跑不覆盖
    config["log"]["log_with_bot_id"] = False
    assert migrate_log_group(config) is False
    assert config["log_with_bot_id"] is True


def test_migrate_log_group_never_clobbers_user_edited_top_level():
    """迁移标记因落盘失败丢失后，若用户已把顶层开关改成 True，旧值不得覆盖。"""
    config = {"log": {"log_with_bot_id": False}, "log_with_bot_id": True}
    assert migrate_log_group(config) is True
    assert config["log_with_bot_id"] is True


def test_migrate_time_sensors_never_clobbers_user_edited_selection():
    """标记丢失后，用户已改过的多选不被旧布尔推导值覆盖。"""
    config = {
        "time_awareness": {
            "workday_state_enabled": False,
            "lunar_state_enabled": False,
            "almanac_enabled": False,
            "time_sensors": ["almanac"],  # 用户已在新形态下改过
        }
    }
    assert migrate_time_sensors(config) is True
    assert config["time_awareness"]["time_sensors"] == ["almanac"]


def test_migrate_builtin_categories_never_clobbers_user_edited_selection():
    config = {
        "calendar": {
            "enable_builtin_events": True,
            "builtin_events": {},
            "builtin_event_categories": ["political"],  # 用户已在新形态下改过
        }
    }
    assert migrate_builtin_categories(config) is True
    assert config["calendar"]["builtin_event_categories"] == ["political"]
