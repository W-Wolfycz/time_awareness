"""把 ``TimeFactsCollector`` 的结构化事实渲染为动态标签、AI 生成输入或 Dashboard DTO。"""

from __future__ import annotations

from typing import Any, Callable

from .time_facts_service import SensorFact, TimeFactsCollector


class TimeContextService:
    _WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    _WEEKDAY_FULL_CN = (
        "星期一",
        "星期二",
        "星期三",
        "星期四",
        "星期五",
        "星期六",
        "星期日",
    )

    def __init__(
        self,
        *,
        config: dict,
        astrbot_config_provider: Callable[[], Any],
        calendar_store,
        last_chat_tracker,
        now_provider: Callable[[], Any] | None = None,
        facts_collector: TimeFactsCollector | None = None,
    ):
        self.config = config
        self.calendar_store = calendar_store
        self.last_chat_tracker = last_chat_tracker
        self.facts = facts_collector or TimeFactsCollector(
            config=config,
            astrbot_config_provider=astrbot_config_provider,
            calendar_store=calendar_store,
            last_chat_tracker=last_chat_tracker,
            now_provider=now_provider,
        )

    def set_daily_schedule_service(self, service) -> None:
        """注入 AI 每日日程解析器，避免事实层反向构造 AstrBot 依赖。"""
        self.facts.set_daily_schedule_service(service)

    def now(self):
        return self.facts.now()

    def time_guidance_enabled(self) -> bool:
        return self.facts.time_guidance_enabled()

    def workday_enabled(self) -> bool:
        return self.facts.workday_enabled()

    def lunar_enabled(self) -> bool:
        return self.facts.lunar_enabled()

    def almanac_enabled(self) -> bool:
        return self.facts.almanac_enabled()

    def last_chat_enabled(self) -> bool:
        return self.facts.last_chat_enabled()

    def build_dynamic(
        self,
        session: str | None = None,
        *,
        now=None,
        include_last_chat: bool = True,
        weather_text: str | None = None,
        schedule_summary: str | None = None,
    ) -> str:
        """把同一 ``now`` 快照渲染成本轮动态 XML 标签；weather/schedule 为空则省略对应标签。"""
        facts = self.facts.collect(
            scope="conversation",
            session=session,
            now=now,
            include_last_chat=include_last_chat,
        )
        parts = []
        if self.time_guidance_enabled():
            parts.append(self.build_datetime_reminder(now=facts.now))
        parts.append(f"<SCHEDULE_STATE>{facts.schedule.state}</SCHEDULE_STATE>")
        if schedule_summary:
            parts.append(f"<TODAY_SCHEDULE>{schedule_summary}</TODAY_SCHEDULE>")
        if weather_text:
            parts.append(f"<WEATHER>今日天气基调：{weather_text}</WEATHER>")
        if facts.workday.available:
            parts.append(f"<WORKDAY_STATE>{facts.workday.value}</WORKDAY_STATE>")
        if facts.lunar.available:
            parts.append(f"<LUNAR_STATE>{facts.lunar.value}</LUNAR_STATE>")
        if facts.almanac.available:
            parts.append(f"<ALMANAC_STATE>{facts.almanac.value}</ALMANAC_STATE>")
        if facts.today_events.available and facts.today_events.value:
            parts.append(f"<TODAY_EVENTS>{facts.today_events.value}</TODAY_EVENTS>")
        if facts.last_chat.available:
            parts.append(f"<LAST_CHAT>{facts.last_chat.value}</LAST_CHAT>")
        return "\n".join(parts)

    def build_datetime_reminder(self, *, now=None) -> str:
        """构造插件自有的中文大写现实时间标签。"""
        now = now or self.now()
        display_now = now if now.tzinfo is not None else now.astimezone()
        current_time = display_now.strftime("%Y-%m-%d %H:%M")
        timezone_name = display_now.strftime("%Z")
        weekday = self._WEEKDAY_FULL_CN[display_now.weekday()]
        return (
            "<SYSTEM_REMINDER>"
            f"当前日期时间：{current_time}（{timezone_name}），{weekday}"
            "</SYSTEM_REMINDER>"
        )

    def today_events_text(self, *, now=None) -> str:
        return self.facts.today_events_text(now=now or self.now())

    def enabled_builtin_categories(self) -> list[str]:
        return self.facts.enabled_builtin_categories()


    @staticmethod
    def _generation_sensor(fact: SensorFact, *, as_of: str, limit: int = 0) -> dict:
        result = {
            "enabled": fact.enabled,
            "available": fact.available,
            "as_of": as_of,
        }
        if fact.kind:
            result["kind"] = fact.kind
        if fact.available:
            result["value"] = fact.value[:limit] if limit else fact.value
        elif fact.error:
            result["error"] = fact.error
        return result

    def build_generation_sensor_snapshot(self, *, now=None) -> dict:
        """构造 Persona 级 AI 日程传感器输入，不读取会话历史或当前日程。"""
        facts = self.facts.collect(scope="persona_daily_generation", now=now)
        as_of = facts.now.isoformat()
        return {
            "workday": self._generation_sensor(facts.workday, as_of=as_of),
            "lunar": self._generation_sensor(facts.lunar, as_of=as_of),
            "almanac": self._generation_sensor(facts.almanac, as_of=as_of),
            "today_events": self._generation_sensor(
                facts.today_events,
                as_of=as_of,
                limit=1000,
            ),
        }

    @staticmethod
    def _greeting(hour: int) -> str:
        if 5 <= hour < 8:
            return "早上好~"
        if 8 <= hour < 11:
            return "上午好~"
        if 11 <= hour < 13:
            return "中午好~"
        if 13 <= hour < 17:
            return "下午好~"
        if 17 <= hour < 19:
            return "傍晚好~"
        if 19 <= hour < 23:
            return "晚上好~"
        if hour >= 23 or hour < 2:
            return "深夜好~"
        return "凌晨好~"

    def build_dashboard_time_info(self) -> dict:
        facts = self.facts.collect(scope="dashboard")
        now = facts.now
        date = now.date()
        offset = now.utcoffset()
        if offset is not None:
            total_minutes = int(offset.total_seconds() // 60)
            sign = "+" if total_minutes >= 0 else "-"
            absolute = abs(total_minutes)
            tz_gmt = f"GMT{sign}{absolute // 60:02d}:{absolute % 60:02d}"
        else:
            tz_gmt = ""

        lunar_detail = facts.lunar.details
        almanac_detail = facts.almanac.details
        return {
            "now_iso": now.isoformat(),
            "now_display": now.strftime("%Y/%m/%d %H:%M:%S"),
            "time_display": now.strftime("%H:%M:%S"),
            "tz_gmt": tz_gmt,
            "date_display": now.strftime("%Y/%m/%d"),
            "greeting": self._greeting(now.hour),
            "weekday_display": self._WEEKDAY_CN[date.weekday()],
            "workday_kind": facts.workday.kind,
            "workday_label": facts.workday.value,
            "lunar_display": facts.lunar.value,
            "lunar_year_chip": lunar_detail.get("lunar_year_chip", ""),
            "lunar_month_day": lunar_detail.get("lunar_month_day", ""),
            "almanac_display": facts.almanac.value,
            "almanac_gz_day": almanac_detail.get("gz_day", ""),
            "almanac_chong_sha": almanac_detail.get("chong_sha", ""),
            "almanac_yi": almanac_detail.get("yi", ""),
            "almanac_ji": almanac_detail.get("ji", ""),
            "lunar_enabled": facts.lunar.enabled,
            "almanac_enabled": facts.almanac.enabled,
        }
