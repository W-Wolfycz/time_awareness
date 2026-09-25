"""内置现实日历事件管理器：builtin_events.yaml 读写与跨年重生成；年份/分类开关变更触发 regen。"""

import datetime
import os

from ..log import logger, tag

from ._datafile import atomic_write_yaml, load_mapping
from .builtin_events import generate_for_year


BUILTIN_FILE_NAME = "builtin_events.yaml"
BUILTIN_DATA_VERSION = 1


class BuiltinManager:
    """内置事件管理器。"""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._file = os.path.join(data_dir, BUILTIN_FILE_NAME)

    def load_raw(self) -> dict:
        """加载原始数据。文件不存在或损坏返回空结构。"""
        data = load_mapping(self._file)
        if data is None:
            return self._empty_payload()
        return data

    @staticmethod
    def _empty_payload() -> dict:
        return {
            "version": BUILTIN_DATA_VERSION,
            "year": None,
            "generated_at": "",
            "enabled_categories": [],
            "events": [],
        }

    def save(self, year: int, events: list, categories: list) -> bool:
        """原子写入 builtin_events.yaml。"""
        payload = {
            "version": BUILTIN_DATA_VERSION,
            "year": year,
            "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "enabled_categories": list(categories),
            "events": events,
        }
        ok = atomic_write_yaml(
            self._file,
            payload,
            header="time_awareness 内置日历事件（自动生成，可手动编辑）",
        )
        if not ok:
            logger.error(f"{tag()} ❌ 内置事件保存失败")
        return ok

    def is_stale(self, year: int, categories: list) -> bool:
        """检查当前文件是否过期（年份或分类不匹配）。"""
        data = self.load_raw()
        if data.get("year") != year:
            return True
        existing = set(data.get("enabled_categories") or [])
        desired = set(categories)
        return existing != desired

    def regenerate(self, year: int, categories: list) -> int:
        """重新生成指定年份的内置事件并写入；保存失败返回 -1。"""
        events = generate_for_year(year, categories)
        if not events and categories:
            # 生成为空且分类启用（chinese_calendar 未覆盖该年）：保留旧数据、保持 stale，下次扫描重试
            logger.warning(
                f"{tag()} ⚠️ 内置事件生成为空且分类已启用（可能 chinese_calendar "
                f"未覆盖 {year} 年）：保留旧数据，下次扫描将重试"
            )
            return -1
        ok = self.save(year=year, events=events, categories=categories)
        if not ok:
            return -1
        logger.info(
            f"{tag()} ✅ 内置事件已重新生成: year={year} "
            f"categories={categories} events={len(events)}"
        )
        return len(events)

    def ensure_fresh(self, year: int, categories: list) -> int:
        """过期则重新生成，否则返回当前事件数；供启动时调用。"""
        if self.is_stale(year, categories):
            return self.regenerate(year, categories)
        data = self.load_raw()
        return len(data.get("events") or [])
