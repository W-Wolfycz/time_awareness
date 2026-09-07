"""日历运行期维护与每日提醒扫描。"""

from __future__ import annotations

import asyncio
import datetime
from collections.abc import Awaitable, Callable
from typing import Any

from ..domain.schedule import ScheduleValidationError, parse_time
from ..log import logger, tag
from ..utils.time_utils import _bounded_int


class CalendarRuntimeService:
    """维护内置事件新鲜度，并为白名单会话安排当日日历提醒。"""

    def __init__(
        self,
        *,
        config: dict,
        calendar_store,
        builtin_manager,
        scheduler,
        time_context,
        targets_provider: Callable[[], list[str]],
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ):
        self.config = config
        self.calendar_store = calendar_store
        self.builtin_manager = builtin_manager
        self.scheduler = scheduler
        self.time_context = time_context
        self._targets_provider = targets_provider
        self._sleep = sleep

    def _config_group(self, name: str) -> dict:
        value = self.config.get(name, {})
        return value if isinstance(value, dict) else {}

    def _max_late_minutes(self, reminder: dict) -> int:
        return _bounded_int(
            reminder.get("reminder_max_late_minutes", 60), 60, 0, 7 * 24 * 60
        )

    def apply_reminder_config(self) -> None:
        reminder = self._config_group("reminder")
        # 0 表示“不检查迟到”，不能再用 `or 60` 把 0 吞成默认 60。
        self.scheduler.set_max_late_minutes(self._max_late_minutes(reminder))
        self.scheduler.set_max_attempts(
            _bounded_int(
                reminder.get("reminder_max_attempts", 3) or 3,
                3,
                1,
                10,
            )
        )
        # 顶层 runtime.state_retention_days（1-365，缺省 7）只影响 Scheduler 终态任务与 daily locks 回收。
        runtime = self._config_group("runtime")
        self.scheduler.set_state_retention_days(
            _bounded_int(runtime.get("state_retention_days", 7), 7, 1, 365)
        )

    def initialize_builtin_events(self) -> None:
        """启动时确保当前年份和分类配置对应的内置事件已加载。"""
        try:
            now = self.time_context.now()
            categories = self.time_context.enabled_builtin_categories()
            if not categories:
                self.calendar_store.set_builtin_events([])
                return
            count = self.builtin_manager.ensure_fresh(now.year, categories)
            logger.info(f"{tag()} 📅 内置事件已就绪: {count} 条 (year={now.year})")
            data = self.builtin_manager.load_raw()
            self.calendar_store.set_builtin_events(data.get("events") or [])
        except Exception as exc:
            logger.error(f"{tag()} ❌ 内置事件初始化失败: {exc}")
            self.calendar_store.set_builtin_events([])

    def ensure_builtin_fresh(self) -> None:
        """跨年或分类变化时刷新内置事件。"""
        try:
            categories = self.time_context.enabled_builtin_categories()
            if not categories:
                self.calendar_store.set_builtin_events([])
                return
            now = self.time_context.now()
            if not self.builtin_manager.is_stale(now.year, categories):
                return
            count = self.builtin_manager.regenerate(now.year, categories)
            if count >= 0:
                data = self.builtin_manager.load_raw()
                self.calendar_store.set_builtin_events(data.get("events") or [])
                logger.info(
                    f"{tag()} 📅 跨年/配置变更自动 regen 内置事件: "
                    f"year={now.year} events={count}"
                )
        except Exception as exc:
            logger.error(f"{tag()} ❌ 内置事件跨年 regen 失败: {exc}")

    def scan_and_enqueue_today_events(self) -> int:
        reminder = self._config_group("reminder")
        if not reminder.get("enable_reminder", False):
            return 0
        # 日历关闭且无问候需求时提醒无内容可发，不入队。
        calendar_config = self._config_group("calendar")
        if (
            not calendar_config.get("enable_calendar", False)
            and not reminder.get("daily_greeting_without_events", True)
        ):
            logger.info(
                f"{tag()} ⏭️ 日历提醒跳过入队：calendar.enable_calendar 关闭且"
                "未启用每日问候（daily_greeting_without_events）"
            )
            return 0
        targets = self._targets_provider()
        if not targets:
            logger.warning(f"{tag()} ⚠️ reminder 启用但 reminder_targets 为空，跳过扫描")
            return 0

        now = self.time_context.now()
        try:
            reminder_minute = parse_time(
                str(reminder.get("reminder_time", "09:00") or "09:00"),
                strict=False,
            )
            hour, minute = divmod(reminder_minute, 60)
        except ScheduleValidationError:
            logger.warning(
                f"{tag()} ⚠️ reminder_time 格式错误: "
                f"{reminder.get('reminder_time')}，回退 09:00"
            )
            hour, minute = 9, 0
        fire_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

        max_late = self._max_late_minutes(reminder)
        # max_late==0：不检查迟到，时刻过去也照常入队（Scheduler 到期即发送）。
        if max_late > 0:
            cutoff = now - datetime.timedelta(minutes=max_late)
            if fire_at < cutoff:
                logger.warning(
                    f"{tag()} ⚠️ 跳过今日提醒入队：fire_at={fire_at.isoformat()} "
                    f"now={now.isoformat()} 已超出 {max_late} 分钟容忍窗口"
                    "（疑似跨日扫描时钟漂移导致 now 仍是昨天）"
                )
                return 0

        enqueued = 0
        today = now.date()
        # 去重与 fire_at 同用插件时区基准，避免宿主/插件时区不一致时重复入队。
        now_tz = now.tzinfo
        for session in targets:
            if self.scheduler.has_calendar_task_for_session_today(
                session, today, now_tz
            ):
                continue
            self.scheduler.add_calendar_task(session=session, fire_at=fire_at)
            enqueued += 1
        if enqueued:
            logger.info(f"{tag()} 📅 今日提醒入队 {enqueued} 条 session")
        return enqueued

    async def run_daily_loop(self) -> None:
        """每日零点后刷新当日提醒和内置事件。"""
        try:
            while True:
                try:
                    now = self.time_context.now()
                    next_midnight = (now + datetime.timedelta(days=1)).replace(
                        hour=0,
                        minute=0,
                        second=0,
                        microsecond=0,
                    )
                    await self._sleep(
                        max(60, (next_midnight - now).total_seconds() + 5)
                    )
                    self.scan_and_enqueue_today_events()
                    self.ensure_builtin_fresh()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error(f"{tag()} ❌ 每日扫描异常（继续循环）: {exc}")
                    await self._sleep(60)
        except asyncio.CancelledError:
            logger.info(f"{tag()} 每日扫描循环已取消")
