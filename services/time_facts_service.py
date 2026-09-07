"""结构化时间事实采集；传感器只在这里解释配置并采集事实，格式渲染归各表现层。"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.almanac_sensor import almanac_sensor
from ..core.builtin_events import (
    CATEGORY_INTERNATIONAL,
    CATEGORY_LEGAL,
    CATEGORY_POLITICAL,
    CATEGORY_SOLAR_TERM,
    CATEGORY_TRADITIONAL,
)
from ..core.lunar_sensor import lunar_sensor
from ..core.workday_sensor import workday_sensor
from ..domain.schedule import find_active_static_slot
from ..log import logger, tag
from ..utils.time_utils import get_now


UNSPECIFIED_SCHEDULE_STATE = "无固定安排（按人设自然演绎）"

SENSOR_SELECT_KEYS = frozenset({"workday", "lunar", "almanac"})

BUILTIN_SELECT_CATEGORY_MAP = {
    "legal_holidays": CATEGORY_LEGAL,
    "traditional": CATEGORY_TRADITIONAL,
    "solar_terms": CATEGORY_SOLAR_TERM,
    "political": CATEGORY_POLITICAL,
    "international": CATEGORY_INTERNATIONAL,
}

_LEGACY_BUILTIN_TRUE_DEFAULTS = frozenset({"legal_holidays", "traditional", "solar_terms"})


@dataclass(frozen=True)
class SensorFact:
    enabled: bool
    available: bool
    value: str = ""
    kind: str = ""
    details: dict[str, str] = field(default_factory=dict)
    error: str = ""
    reason: str = ""


@dataclass(frozen=True)
class ScheduleFact:
    state: str = UNSPECIFIED_SCHEDULE_STATE
    name: str = ""
    source: str = "none"
    snapshot_id: str = ""
    start: str = ""
    end: str = ""


@dataclass(frozen=True)
class TimeFacts:
    now: datetime.datetime
    schedule: ScheduleFact
    workday: SensorFact
    lunar: SensorFact
    almanac: SensorFact
    today_events: SensorFact
    last_chat: SensorFact

    @property
    def display_now(self) -> datetime.datetime:
        return self.now if self.now.tzinfo is not None else self.now.astimezone()


class TimeFactsCollector:
    """按 conversation / persona generation / dashboard 作用域采集事实。"""

    def __init__(
        self,
        *,
        config: dict,
        astrbot_config_provider: Callable[[], Any],
        calendar_store,
        last_chat_tracker,
        now_provider: Callable[[], datetime.datetime] | None = None,
        workday=workday_sensor,
        lunar=lunar_sensor,
        almanac=almanac_sensor,
    ):
        self.config = config
        self._astrbot_config_provider = astrbot_config_provider
        self.calendar_store = calendar_store
        self.last_chat_tracker = last_chat_tracker
        self._now_provider = now_provider
        self._workday = workday
        self._lunar = lunar
        self._almanac = almanac
        self.daily_schedule_service = None

    def set_daily_schedule_service(self, service) -> None:
        self.daily_schedule_service = service

    def now(self) -> datetime.datetime:
        if self._now_provider is not None:
            return self._now_provider()
        return get_now(self.config, self._astrbot_config_provider())

    def _time_config(self) -> dict:
        value = self.config.get("time_awareness", {})
        return value if isinstance(value, dict) else {}

    def _calendar_config(self) -> dict:
        value = self.config.get("calendar", {})
        return value if isinstance(value, dict) else {}

    def time_guidance_enabled(self) -> bool:
        return bool(self._time_config().get("time_guidance_enabled", True))

    def _time_sensor_selection(self) -> set[str] | None:
        """读取 ``time_sensors`` 多选列表；None 表示未迁移，回退旧布尔形态。"""
        value = self._time_config().get("time_sensors")
        if not isinstance(value, list):
            return None
        return {str(v) for v in value if str(v) in SENSOR_SELECT_KEYS}

    def _sensor_enabled(self, key: str, legacy_key: str, default: bool) -> bool:
        selection = self._time_sensor_selection()
        if selection is not None:
            return key in selection
        return bool(self._time_config().get(legacy_key, default))

    def workday_enabled(self) -> bool:
        return self._sensor_enabled("workday", "workday_state_enabled", True)

    def lunar_enabled(self) -> bool:
        return self._sensor_enabled("lunar", "lunar_state_enabled", True)

    def almanac_enabled(self) -> bool:
        return self._sensor_enabled("almanac", "almanac_enabled", False)

    def last_chat_enabled(self) -> bool:
        return bool(self._time_config().get("last_chat_enabled", True))

    def enabled_builtin_categories(self) -> list[str]:
        calendar_config = self._calendar_config()
        value = calendar_config.get("builtin_event_categories")
        if isinstance(value, list):
            return [
                BUILTIN_SELECT_CATEGORY_MAP[str(v)]
                for v in value
                if str(v) in BUILTIN_SELECT_CATEGORY_MAP
            ]
        # 兼容旧形态（未迁移的存量配置）
        if not calendar_config.get("enable_builtin_events", True):
            return []
        builtin_config = calendar_config.get("builtin_events", {})
        if not isinstance(builtin_config, dict):
            builtin_config = {}
        return [
            category
            for key, category in BUILTIN_SELECT_CATEGORY_MAP.items()
            if builtin_config.get(key, key in _LEGACY_BUILTIN_TRUE_DEFAULTS)
        ]

    def today_events_text(self, *, now: datetime.datetime) -> str:
        calendar_config = self._calendar_config()
        if not calendar_config.get("enable_calendar", False):
            return ""
        include_builtin = bool(self.enabled_builtin_categories())
        return self.calendar_store.today_text(now, include_builtin=include_builtin)

    def resolve_schedule(
        self,
        *,
        now: datetime.datetime,
        session: str | None,
    ) -> ScheduleFact:
        daily_schedule = self.config.get("daily_schedule", {})
        if not isinstance(daily_schedule, dict) or not daily_schedule.get(
            "enable_schedule", False
        ):
            return ScheduleFact()

        if self.daily_schedule_service is not None and session:
            try:
                resolution = self.daily_schedule_service.resolve(now=now, session=session)
                if resolution is not None:
                    logger.debug(
                        f"{tag()} AI 日程命中时段：name={resolution.slot_name or '(无名)'} "
                        f"source={resolution.source} snapshot={resolution.snapshot_id}"
                    )
                    return ScheduleFact(
                        state=resolution.state_prompt,
                        name=resolution.slot_name,
                        source=resolution.source,
                        snapshot_id=resolution.snapshot_id,
                    )
            except Exception as exc:
                logger.warning(
                    f"{tag()} ⚠️ AI 日程解析失败，回退静态日程: {type(exc).__name__}"
                )

        slot = find_active_static_slot(daily_schedule.get("schedule_templates") or [], now)
        if slot is None:
            logger.debug(f"{tag()} 日程表未命中任何时段（now={now.strftime('%H:%M')}）")
            return ScheduleFact()
        name = str(slot.get("name", "") or "").strip()
        state = str(slot.get("state_prompt", "") or "").strip()
        logger.debug(
            f"{tag()} 日程表命中时段：name={name or '(无名)'} "
            f"{slot.get('start_time')}-{slot.get('end_time')} state_prompt={len(state)}字"
        )
        return ScheduleFact(
            state=state or UNSPECIFIED_SCHEDULE_STATE,
            name=name,
            source="static",
            start=str(slot.get("start_time", "") or "").strip(),
            end=str(slot.get("end_time", "") or "").strip(),
        )

    def collect(
        self,
        *,
        scope: str,
        session: str | None = None,
        now: datetime.datetime | None = None,
        include_last_chat: bool = True,
    ) -> TimeFacts:
        """采集同一时刻不可变快照；Persona 级生成作用域不读取会话级上次对话。"""
        if scope not in {"conversation", "persona_daily_generation", "dashboard"}:
            raise ValueError(f"未知时间事实作用域: {scope}")
        now = now or self.now()
        date = now.date()
        schedule = (
            self.resolve_schedule(now=now, session=session)
            if scope == "conversation"
            else ScheduleFact()
        )

        workday = SensorFact(False, False)
        if self.workday_enabled():
            try:
                kind, value = self._workday.classify(date)
                workday = SensorFact(True, bool(value), value=value, kind=kind)
            except Exception as exc:
                workday = SensorFact(True, False, error=type(exc).__name__)

        lunar = SensorFact(False, False)
        if self.lunar_enabled():
            try:
                value, details = self._lunar.collect(date)
                lunar = SensorFact(True, bool(value), value=value, details=details)
            except Exception as exc:
                lunar = SensorFact(True, False, error=type(exc).__name__)

        almanac = SensorFact(False, False)
        if self.almanac_enabled():
            try:
                value, details = self._almanac.collect(date)
                almanac = SensorFact(True, bool(value), value=value, details=details)
            except Exception as exc:
                almanac = SensorFact(True, False, error=type(exc).__name__)

        calendar_enabled = bool(self._calendar_config().get("enable_calendar", False))
        today_events = SensorFact(False, False)
        if calendar_enabled:
            try:
                value = self.today_events_text(now=now)
                today_events = SensorFact(True, True, value=value)
            except Exception as exc:
                today_events = SensorFact(True, False, error=type(exc).__name__)

        last_chat_enabled = self.last_chat_enabled() and include_last_chat
        last_chat = SensorFact(last_chat_enabled, False)
        if scope == "conversation" and last_chat_enabled and session:
            try:
                value = self.last_chat_tracker.summary(session, now)
                last_chat = SensorFact(True, bool(value), value=value or "")
            except Exception as exc:
                last_chat = SensorFact(True, False, error=type(exc).__name__)

        return TimeFacts(
            now=now,
            schedule=schedule,
            workday=workday,
            lunar=lunar,
            almanac=almanac,
            today_events=today_events,
            last_chat=last_chat,
        )
