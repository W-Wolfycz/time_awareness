"""静态日程配置的读取、校验与并发安全保存。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from typing import Any

from ..domain.schedule import (
    ScheduleValidationError,
    detect_static_overlaps,
    format_time,
    parse_time,
)
from ..domain.schedule_edit import MAX_NAME_LENGTH, MAX_STATE_LENGTH
from ..log import logger, tag

MAX_STATIC_SLOTS = 256
_KNOWN_TEMPLATE_KEYS = {"time_slot", "sleep_slot"}


class StaticScheduleConfigConflict(Exception):
    """静态日程已被其他页面或配置入口修改。"""


class StaticScheduleConfigPersistenceError(Exception):
    """配置校验成功，但 AstrBotConfig 落盘失败。"""


def _config_slots(config: Any) -> list:
    if not isinstance(config, dict):
        return []
    daily = config.get("daily_schedule", {})
    if not isinstance(daily, dict):
        return []
    slots = daily.get("schedule_templates", [])
    return slots if isinstance(slots, list) else []


def static_schedule_revision(slots: list) -> str:
    """根据当前原始配置生成稳定的乐观并发 revision。"""
    payload = json.dumps(
        slots,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class StaticScheduleConfigService:
    """Plugin Page 的静态日程配置应用服务。"""

    def __init__(
        self,
        *,
        config: dict,
        save_config: Callable[[], Any],
        check_overlap: Callable[[], None] | None = None,
    ):
        self.config = config
        self._save_config = save_config
        self._check_overlap = check_overlap
        self._lock = asyncio.Lock()

    def get(self) -> dict:
        raw_slots = _config_slots(self.config)
        slots = [self._display_slot(item) for item in raw_slots if isinstance(item, dict)]
        return {
            "revision": static_schedule_revision(raw_slots),
            "slots": slots,
            "max_slots": MAX_STATIC_SLOTS,
            "warnings": self._warnings(raw_slots),
        }

    async def save(self, *, revision: str, slots: list) -> dict:
        if not isinstance(slots, list):
            raise TypeError("slots 必须是数组")
        if len(slots) > MAX_STATIC_SLOTS:
            raise ValueError(f"静态日程数量超过上限 {MAX_STATIC_SLOTS}")
        normalized = self._validate_slots(slots)

        async with self._lock:
            current = _config_slots(self.config)
            if not revision or revision != static_schedule_revision(current):
                raise StaticScheduleConfigConflict("静态日程已被其他页面修改，请刷新")

            old_daily = self.config.get("daily_schedule")
            daily = old_daily if isinstance(old_daily, dict) else {}
            old_slots_present = "schedule_templates" in daily
            old_slots = daily.get("schedule_templates")
            if daily is not old_daily:
                self.config["daily_schedule"] = daily
            daily["schedule_templates"] = normalized

            try:
                saved = self._save_config()
            except Exception as exc:  # noqa: BLE001 - 配置持久化边界必须回滚任意失败
                saved = False
                logger.warning(f"{tag()} ⚠️ 静态日程配置落盘异常: {exc}")
            if saved is False:
                if old_slots_present:
                    daily["schedule_templates"] = old_slots
                else:
                    daily.pop("schedule_templates", None)
                if daily is not old_daily:
                    if old_daily is None:
                        self.config.pop("daily_schedule", None)
                    else:
                        self.config["daily_schedule"] = old_daily
                raise StaticScheduleConfigPersistenceError("静态日程配置落盘失败")

            if self._check_overlap is not None:
                self._check_overlap()
            logger.debug(f"{tag()} ✍️ 静态日程已从 Plugin Page 保存: slots={len(normalized)}")
            return {
                "revision": static_schedule_revision(normalized),
                "slots": normalized,
                "max_slots": MAX_STATIC_SLOTS,
                "warnings": self._warnings(normalized),
            }

    @staticmethod
    def _display_slot(item: dict) -> dict:
        template_key = str(item.get("__template_key", "time_slot") or "time_slot")
        if template_key not in _KNOWN_TEMPLATE_KEYS:
            template_key = "time_slot"
        return {
            "__template_key": template_key,
            "name": str(item.get("name", "") or ""),
            "start_time": str(item.get("start_time", "") or ""),
            "end_time": str(item.get("end_time", "") or ""),
            "state_prompt": str(item.get("state_prompt", "") or ""),
        }

    @classmethod
    def _validate_slots(cls, payload: list) -> list[dict]:
        normalized: list[dict] = []
        for index, raw in enumerate(payload, start=1):
            if not isinstance(raw, dict):
                raise TypeError(f"第 {index} 个时段必须是对象")
            name = str(raw.get("name", "") or "").strip()
            state = str(raw.get("state_prompt", "") or "").strip()
            if len(name) > MAX_NAME_LENGTH:
                raise ValueError(f"第 {index} 个时段名称超过 {MAX_NAME_LENGTH} 字")
            if len(state) > MAX_STATE_LENGTH:
                raise ValueError(f"第 {index} 个状态描述超过 {MAX_STATE_LENGTH} 字")
            try:
                start = parse_time(
                    str(raw.get("start_time", "") or ""),
                    allow_24=False,
                    strict=False,
                )
                end = parse_time(
                    str(raw.get("end_time", "") or ""),
                    allow_24=True,
                    strict=False,
                )
            except ScheduleValidationError as exc:
                raise ValueError(f"第 {index} 个时段：{exc}") from exc
            if start == end:
                raise ValueError(f"第 {index} 个时段开始和结束时间不能相同")
            template_key = str(raw.get("__template_key", "time_slot") or "time_slot")
            if template_key not in _KNOWN_TEMPLATE_KEYS:
                template_key = "time_slot"
            normalized.append(
                {
                    "__template_key": template_key,
                    "name": name,
                    "start_time": format_time(start),
                    "end_time": format_time(end),
                    "state_prompt": state,
                }
            )
        return normalized

    @staticmethod
    def _warnings(slots: list) -> list[str]:
        warnings = []
        if len(slots) > MAX_STATIC_SLOTS:
            warnings.append(
                f"当前有 {len(slots)} 个时段，超过可保存上限 {MAX_STATIC_SLOTS}；"
                "请先删除多余时段"
            )
        for index, slot in enumerate(slots, start=1):
            if not isinstance(slot, dict):
                warnings.append(f"第 {index} 个时段不是对象，保存时会被拒绝")
                continue
            if len(str(slot.get("name", "") or "").strip()) > MAX_NAME_LENGTH:
                warnings.append(f"第 {index} 个时段名称超过 {MAX_NAME_LENGTH} 字")
            if len(str(slot.get("state_prompt", "") or "").strip()) > MAX_STATE_LENGTH:
                warnings.append(f"第 {index} 个状态描述超过 {MAX_STATE_LENGTH} 字")
            try:
                start = parse_time(
                    str(slot.get("start_time", "") or ""),
                    allow_24=False,
                    strict=False,
                )
                end = parse_time(
                    str(slot.get("end_time", "") or ""),
                    allow_24=True,
                    strict=False,
                )
                if start == end:
                    warnings.append(f"第 {index} 个时段开始和结束时间相同，不会生效")
            except ScheduleValidationError as exc:
                warnings.append(f"第 {index} 个时段时间无效：{exc}")
        for overlap in detect_static_overlaps(slots):
            minute = overlap.minute
            warnings.append(
                f"第 {overlap.first_index + 1} 与第 {overlap.second_index + 1} 个时段"
                f"在 {minute // 60:02d}:{minute % 60:02d} 重叠；运行时优先使用靠前项"
            )
        return warnings
