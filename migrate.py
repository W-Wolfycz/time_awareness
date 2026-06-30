"""time_awareness 配置迁移：基于 config_version 字段的链式迁移。

机制：
- 配置里若没有 config_version 字段，视为版本 0
- 每次插件加载调用 migrate(config)，从声明的版本一路迁移到 CURRENT_CONFIG_VERSION
- 每个版本间的迁移逻辑独立成 _migrate_vN_to_vNp1 函数，注册在 _MIGRATIONS 表里
- 迁移幂等：已经是目标版本时 no-op；老格式解析失败时跳过而不报错

新增版本时：
1. 把 CURRENT_CONFIG_VERSION 加 1
2. 写 _migrate_vN_to_vNp1 函数（原地修改并返回 config）
3. 注册到 _MIGRATIONS[N+1]
"""
import logging
from typing import Callable, Optional

from .constants import DEFAULT_SLEEP_PROMPT, LEGACY_DEFAULT_SLEEP_PROMPT

logger = logging.getLogger("astrbot")

CURRENT_CONFIG_VERSION = 1


def migrate(config: dict) -> dict:
    """主入口：从声明的版本迁移到 CURRENT_CONFIG_VERSION。原地修改并返回。
    迁移失败时保留原 config 不变（只打 error 日志），不抛异常。"""
    if not isinstance(config, dict):
        return config

    version = config.get("config_version", 0)
    if not isinstance(version, int) or version < 0:
        version = 0

    if version >= CURRENT_CONFIG_VERSION:
        return config

    initial_version = version
    while version < CURRENT_CONFIG_VERSION:
        target = version + 1
        step_fn = _MIGRATIONS.get(target)
        if step_fn is None:
            logger.warning(
                f"[time_awareness] 配置迁移中断：v{version} → v{target} 没有注册的迁移函数"
            )
            break
        try:
            new_config = step_fn(config)
            if new_config is None:
                # 迁移函数主动放弃（如数据格式损坏），不 bump 版本号，下次启动再试
                logger.info(
                    f"[time_awareness] v{version}→v{target} 迁移函数跳过（数据格式不符合预期），版本号保持 v{version}"
                )
                break
            config = new_config
            config["config_version"] = target
            version = target
        except Exception as e:
            logger.error(
                f"[time_awareness] 配置迁移失败 v{version} → v{target}: {e}",
                exc_info=True,
            )
            break

    if version != initial_version:
        logger.info(
            f"[time_awareness] 配置已迁移：v{initial_version} → v{version}"
        )
    return config


def _migrate_v0_to_v1(config: dict) -> Optional[dict]:
    """v0 → v1：time_awareness.sleep_* 三字段迁移到 daily_schedule.schedule_templates[0]。

    - 读 time_awareness.sleep_mode_enabled / sleep_hours / sleep_prompt
    - 若启用且 sleep_hours 解析成功：
      - 创建一条 schedule_template：start_time/end_time/state_prompt
      - state_prompt = 用户自定义 sleep_prompt（非空且非 legacy 默认）或 DEFAULT_SLEEP_PROMPT
      - daily_schedule.enable_schedule = True（沿用老启用状态）
    - 若未启用：不创建时段，daily_schedule.enable_schedule = False
    - 删除 time_awareness.sleep_mode_enabled / sleep_hours / sleep_prompt

    返回值：
    - config：迁移成功（无论是否创建时段）→ 主入口会 bump 版本号
    - None：sleep_hours 数据格式损坏（无法解析）→ 主入口不 bump，下次启动再试
    """
    ta = config.get("time_awareness")
    if not isinstance(ta, dict):
        # 没这节配置，直接视为符合新版本（无数据可迁）
        return config

    # 检查是否真的有老字段；若都已不存在，跳过迁移逻辑（仍 bump 版本号）
    has_legacy_fields = any(
        k in ta for k in ("sleep_mode_enabled", "sleep_hours", "sleep_prompt")
    )
    if not has_legacy_fields:
        return config

    sleep_enabled = bool(ta.get("sleep_mode_enabled", False))
    sleep_hours = str(ta.get("sleep_hours", "") or "").strip()
    sleep_prompt_raw = str(ta.get("sleep_prompt", "") or "").strip()

    # 准备 daily_schedule 子字典
    ds = config.get("daily_schedule")
    if not isinstance(ds, dict):
        ds = {}
        config["daily_schedule"] = ds

    # 仅在 sleep_enabled 且 sleep_hours 有效时创建时段
    if sleep_enabled and sleep_hours:
        # 校验 sleep_hours 格式 "HH:MM-HH:MM"
        parsed = _parse_sleep_hours(sleep_hours)
        if parsed is None:
            logger.warning(
                f"[time_awareness] v0→v1 sleep_hours 格式无法解析 '{sleep_hours}'，迁移跳过（不 bump 版本号）"
            )
            return None

        start_time, end_time = parsed
        # state_prompt：用户自定义优先；为空或为 legacy 默认时回退到新默认
        if sleep_prompt_raw and sleep_prompt_raw != LEGACY_DEFAULT_SLEEP_PROMPT:
            state_prompt = sleep_prompt_raw
        else:
            state_prompt = DEFAULT_SLEEP_PROMPT

        # 创建时段实例（template_list 元素带 __template_key 元字段）
        slot = {
            "__template_key": "time_slot",
            "start_time": start_time,
            "end_time": end_time,
            "state_prompt": state_prompt,
        }
        # 若 daily_schedule 已有 schedule_templates，追加；否则新建
        existing = ds.get("schedule_templates")
        if not isinstance(existing, list):
            existing = []
        existing.append(slot)
        ds["schedule_templates"] = existing
        ds.setdefault("enable_schedule", True)
        ds["enable_schedule"] = True

        logger.info(
            f"[time_awareness] v0→v1 迁移睡眠窗口：{sleep_hours} → daily_schedule.schedule_templates (state_prompt {len(state_prompt)}字)"
        )
    else:
        # 未启用：保留 enable_schedule 的现有值（若已存在），否则默认 False；
        # 同时确保 schedule_templates 字段存在（空列表），与计划约定的迁移后形态一致
        ds.setdefault("enable_schedule", False)
        if not isinstance(ds.get("schedule_templates"), list):
            ds["schedule_templates"] = []
        logger.info(
            f"[time_awareness] v0→v1 老睡眠字段未启用，迁移为空日程表"
        )

    # 删除老字段
    for k in ("sleep_mode_enabled", "sleep_hours", "sleep_prompt"):
        ta.pop(k, None)

    return config


def _parse_sleep_hours(sleep_hours: str) -> Optional[tuple[str, str]]:
    """解析 "HH:MM-HH:MM" → (start, end)；格式错误返回 None。"""
    try:
        start_str, end_str = sleep_hours.split("-")
        start_h, start_m = map(int, start_str.strip().split(":"))
        end_h, end_m = map(int, end_str.strip().split(":"))
        if not (0 <= start_h <= 23 and 0 <= start_m <= 59):
            return None
        if not (0 <= end_h <= 23 and 0 <= end_m <= 59):
            return None
        return (f"{start_h:02d}:{start_m:02d}", f"{end_h:02d}:{end_m:02d}")
    except (ValueError, AttributeError):
        return None


_MIGRATIONS: dict[int, Callable[[dict], dict]] = {
    1: _migrate_v0_to_v1,
}
