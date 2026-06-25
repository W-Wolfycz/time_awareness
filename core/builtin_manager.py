"""
内置现实日历事件管理器

负责 ``builtin_events.yaml`` 的读写与跨年重新生成。

数据文件结构::

    version: 1
    year: 2026                    # 当前数据对应的公历年
    generated_at: "2026-06-26 09:00:00"
    enabled_categories:           # 当前数据使用的分类开关
      - legal_holiday
      - traditional
      - solar_term
    events:                       # 已生成的内置事件列表
      - id: "builtin:legal_holiday:01-01:元旦"
        category: legal_holiday
        year: 2026
        month: 1
        day: 1
        text: "元旦"
        source: builtin

设计要点：

- **跨年自动重新生成**：``is_stale(year, categories)`` 检查年份或类别开关是否
  与当前文件一致；不一致即触发 ``regenerate`` 并写回。
- **regen 语义**：regen 即「重新初始化」——年内日历不变，所以 regen 永远生成
  当前年份的完整列表，不做任何过滤、不保留用户对 builtin 的删除。
- **分类开关变更**：开关变化时也算「过期」，触发重新生成。
"""

import datetime
import os

from astrbot.api import logger

from ._datafile import atomic_write_yaml, load_mapping
from .builtin_events import generate_for_year

_PREFIX = "[time_awareness]"

BUILTIN_FILE_NAME = "builtin_events.yaml"
BUILTIN_DATA_VERSION = 1


class BuiltinManager:
    """内置事件管理器。"""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._file = os.path.join(data_dir, BUILTIN_FILE_NAME)

    # ==================== 路径 ====================

    def file_path(self) -> str:
        return self._file

    # ==================== 读取 ====================

    def load_raw(self) -> dict:
        """加载原始数据。文件不存在或损坏返回空结构。"""
        if not os.path.exists(self._file):
            return self._empty_payload()
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

    # ==================== 写入 ====================

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
            logger.error(f"{_PREFIX} ❌ 内置事件保存失败")
        return ok

    # ==================== 业务方法 ====================

    def is_stale(self, year: int, categories: list) -> bool:
        """检查当前文件是否过期（年份或分类不匹配）。"""
        data = self.load_raw()
        if data.get("year") != year:
            return True
        existing = set(data.get("enabled_categories") or [])
        desired = set(categories)
        return existing != desired

    def regenerate(self, year: int, categories: list) -> int:
        """重新生成指定年份的内置事件并写入文件。

        Returns:
            生成的事件数量；保存失败返回 -1
        """
        events = generate_for_year(year, categories)
        ok = self.save(year=year, events=events, categories=categories)
        if not ok:
            return -1
        logger.info(
            f"{_PREFIX} ✅ 内置事件已重新生成: year={year} "
            f"categories={categories} events={len(events)}"
        )
        return len(events)

    def ensure_fresh(self, year: int, categories: list) -> int:
        """如果文件过期则重新生成，否则直接返回当前事件数。

        用于插件启动时调用：跨年或分类开关变更时自动 regen。
        """
        if self.is_stale(year, categories):
            return self.regenerate(year, categories)
        data = self.load_raw()
        return len(data.get("events") or [])
