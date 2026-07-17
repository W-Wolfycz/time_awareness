"""时间上下文组装服务。

保持既有架构决策：静态规则由 main 注入 system prompt；本服务只生成每轮动态
状态数据。工作日、农历、黄历 sensor 与事件清单保持独立。
"""

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
from ..log import logger, tag
from ..utils.time_utils import find_active_schedule_slot, get_now


class TimeContextService:
    _WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

    def __init__(
        self,
        *,
        config: dict,
        astrbot_config_provider: Callable[[], Any],
        calendar_store,
        last_chat_tracker,
        now_provider: Callable[[], Any] | None = None,
    ):
        self.config = config
        self._astrbot_config_provider = astrbot_config_provider
        self.calendar_store = calendar_store
        self.last_chat_tracker = last_chat_tracker
        self._now_provider = now_provider

    def now(self):
        if self._now_provider is not None:
            return self._now_provider()
        return get_now(self.config, self._astrbot_config_provider())

    def time_guidance_enabled(self) -> bool:
        return bool(self.config.get("time_awareness", {}).get("time_guidance_enabled", True))

    def workday_enabled(self) -> bool:
        return bool(self.config.get("time_awareness", {}).get("workday_state_enabled", True))

    def lunar_enabled(self) -> bool:
        return bool(self.config.get("time_awareness", {}).get("lunar_state_enabled", True))

    def almanac_enabled(self) -> bool:
        return bool(self.config.get("time_awareness", {}).get("almanac_enabled", False))

    def last_chat_enabled(self) -> bool:
        return bool(self.config.get("time_awareness", {}).get("last_chat_enabled", True))

    def build_dynamic(self, session: str | None = None) -> str:
        parts = [f"<SCHEDULE_STATE>{self.resolve_schedule_state()}</SCHEDULE_STATE>"]
        now = self.now()
        date = now.date()

        if self.workday_enabled():
            _, label = workday_sensor.classify(date)
            parts.append(f"<WORKDAY_STATE>{label}</WORKDAY_STATE>")

        if self.lunar_enabled():
            lunar_text = lunar_sensor.describe(date)
            if lunar_text:
                parts.append(f"<LUNAR_STATE>{lunar_text}</LUNAR_STATE>")

        if self.almanac_enabled():
            almanac_text = almanac_sensor.describe(date)
            if almanac_text:
                parts.append(f"<ALMANAC_STATE>{almanac_text}</ALMANAC_STATE>")

        events = self.today_events_text(now=now)
        if events:
            parts.append(f"<TODAY_EVENTS>{events}</TODAY_EVENTS>")

        if self.last_chat_enabled() and session:
            summary = self.last_chat_tracker.summary(session, now)
            if summary:
                parts.append(f"<LAST_CHAT>{summary}</LAST_CHAT>")
        return "\n".join(parts)

    def today_events_text(self, *, now=None) -> str:
        calendar_config = self.config.get("calendar", {})
        if not calendar_config.get("enable_calendar", False):
            return ""
        now = now or self.now()
        include_builtin = calendar_config.get("enable_builtin_events", True) and bool(
            self.enabled_builtin_categories()
        )
        return self.calendar_store.today_text(now, include_builtin=include_builtin)

    def enabled_builtin_categories(self) -> list:
        calendar_config = self.config.get("calendar", {})
        if not calendar_config.get("enable_builtin_events", True):
            return []
        builtin_config = calendar_config.get("builtin_events", {})
        result = []
        if builtin_config.get("legal_holidays", True):
            result.append(CATEGORY_LEGAL)
        if builtin_config.get("traditional", True):
            result.append(CATEGORY_TRADITIONAL)
        if builtin_config.get("solar_terms", True):
            result.append(CATEGORY_SOLAR_TERM)
        if builtin_config.get("political", False):
            result.append(CATEGORY_POLITICAL)
        if builtin_config.get("international", False):
            result.append(CATEGORY_INTERNATIONAL)
        return result

    def resolve_schedule_state(self) -> str:
        daily_schedule = self.config.get("daily_schedule", {})
        if not daily_schedule.get("enable_schedule", False):
            return "未指定（按人设自然推断）"
        now = self.now()
        slot = find_active_schedule_slot(daily_schedule, now)
        if slot is None:
            logger.debug(f"{tag()} 日程表未命中任何时段（now={now.strftime('%H:%M')}）")
            return "未指定（按人设自然推断）"
        name = (slot.get("name") or "").strip()
        state = (slot.get("state_prompt") or "").strip()
        logger.debug(
            f"{tag()} 日程表命中时段：name={name or '(无名)'} "
            f"{slot.get('start_time')}-{slot.get('end_time')} state_prompt={len(state)}字"
        )
        return state or "未指定（按人设自然推断）"

    def build_dashboard_time_info(self) -> dict:
        now = self.now()
        date = now.date()
        offset = now.utcoffset()
        if offset is not None:
            total_minutes = int(offset.total_seconds() // 60)
            sign = "+" if total_minutes >= 0 else "-"
            absolute = abs(total_minutes)
            tz_gmt = f"GMT{sign}{absolute // 60:02d}:{absolute % 60:02d}"
        else:
            tz_gmt = ""

        hour = now.hour
        if 5 <= hour < 8:
            greeting = "早上好~"
        elif 8 <= hour < 11:
            greeting = "上午好~"
        elif 11 <= hour < 13:
            greeting = "中午好~"
        elif 13 <= hour < 17:
            greeting = "下午好~"
        elif 17 <= hour < 19:
            greeting = "傍晚好~"
        elif 19 <= hour < 23:
            greeting = "晚上好~"
        elif hour >= 23 or hour < 2:
            greeting = "深夜好~"
        else:
            greeting = "凌晨好~"

        if self.workday_enabled():
            workday_kind, workday_label = workday_sensor.classify(date)
        else:
            workday_kind, workday_label = "", ""

        lunar_display = lunar_sensor.describe(date) if self.lunar_enabled() else ""
        lunar_detail = (
            lunar_sensor.describe_detailed(date)
            if self.lunar_enabled()
            else {"lunar_year_chip": "", "lunar_month_day": ""}
        )
        almanac_display = almanac_sensor.describe(date) if self.almanac_enabled() else ""
        almanac_detail = (
            almanac_sensor.describe_detailed(date)
            if self.almanac_enabled()
            else {"gz_day": "", "chong_sha": "", "yi": "", "ji": ""}
        )

        return {
            "now_iso": now.isoformat(),
            "now_display": now.strftime("%Y/%m/%d %H:%M:%S"),
            "time_display": now.strftime("%H:%M:%S"),
            "tz_gmt": tz_gmt,
            "date_display": now.strftime("%Y/%m/%d"),
            "greeting": greeting,
            "weekday_display": self._WEEKDAY_CN[date.weekday()],
            "workday_kind": workday_kind,
            "workday_label": workday_label,
            "lunar_display": lunar_display,
            "lunar_year_chip": lunar_detail.get("lunar_year_chip", ""),
            "lunar_month_day": lunar_detail.get("lunar_month_day", ""),
            "almanac_display": almanac_display,
            "almanac_gz_day": almanac_detail.get("gz_day", ""),
            "almanac_chong_sha": almanac_detail.get("chong_sha", ""),
            "almanac_yi": almanac_detail.get("yi", ""),
            "almanac_ji": almanac_detail.get("ji", ""),
            "lunar_enabled": self.lunar_enabled(),
            "almanac_enabled": self.almanac_enabled(),
        }
