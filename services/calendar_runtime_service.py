"""日历运行期维护：内置事件加载与跨年刷新。"""

from __future__ import annotations

import asyncio
import datetime
from collections.abc import Awaitable, Callable
from typing import Any

from ..log import logger, tag


class CalendarRuntimeService:
    """维护内置事件新鲜度（启动加载 + 每日跨年检查）。"""

    def __init__(
        self,
        *,
        calendar_store,
        builtin_manager,
        time_context,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ):
        self.calendar_store = calendar_store
        self.builtin_manager = builtin_manager
        self.time_context = time_context
        self._sleep = sleep

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

    async def run_daily_loop(self) -> None:
        """每日零点后检查内置事件是否需要跨年刷新。"""
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
                    self.ensure_builtin_fresh()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error(f"{tag()} ❌ 每日扫描异常（继续循环）: {exc}")
                    await self._sleep(60)
        except asyncio.CancelledError:
            logger.info(f"{tag()} 每日扫描循环已取消")
