"""时间表管理器：calendar_data.yaml 读写、CRUD、批量清除、导入导出与校验；写后回填 CalendarStore。"""

import datetime
import os
import uuid

import yaml
from ..log import logger, tag

from ._datafile import (
    atomic_write_yaml,
    dump_yaml_str,
    load_mapping,
)
from .calendar_store import (
    MAX_EVENT_TEXT_LENGTH,
    MAX_EVENTS,
    CalendarStore,
    normalize_repeat,
    valid_month_day,
)


CALENDAR_FILE_NAME = "calendar_data.yaml"
CALENDAR_DATA_VERSION = 1
MIN_YEAR = 1970
MAX_YEAR = 9999


class CalendarManager:
    """时间表管理器；直接接收数据目录字符串，避免与其他系统耦合。"""

    def __init__(self, data_dir: str, store: CalendarStore | None = None):
        self.data_dir = data_dir
        self.store = store if store is not None else CalendarStore()

    def _calendar_file_path(self) -> str:
        return os.path.join(self.data_dir, CALENDAR_FILE_NAME)

    def normalize_event(self, raw: dict) -> dict | None:
        """将单条原始事项规整为合法结构；非法返回 None。"""
        if not isinstance(raw, dict):
            return None

        text = str(raw.get("text", "")).strip()
        if not text:
            return None
        if len(text) > MAX_EVENT_TEXT_LENGTH:
            text = text[:MAX_EVENT_TEXT_LENGTH]

        try:
            month = int(raw.get("month"))
            day = int(raw.get("day"))
        except (TypeError, ValueError):
            return None
        if not valid_month_day(month, day):
            return None

        repeat = normalize_repeat(raw.get("repeat", 0))

        try:
            year = int(raw.get("year"))
        except (TypeError, ValueError):
            year = datetime.datetime.now().year
        if not (MIN_YEAR <= year <= MAX_YEAR):
            year = datetime.datetime.now().year

        event_id = str(raw.get("id") or "").strip() or uuid.uuid4().hex

        return {
            "id": event_id,
            "year": year,
            "month": month,
            "day": day,
            "text": text,
            "repeat": repeat,
        }

    def _normalize_events(self, raw_events) -> list:
        """规整一组事项，丢弃非法项，去重 id，限制总量。"""
        if not isinstance(raw_events, list):
            return []
        normalized = []
        seen_ids = set()
        for raw in raw_events:
            event = self.normalize_event(raw)
            if event is None:
                continue
            if event["id"] in seen_ids:
                event["id"] = uuid.uuid4().hex
            seen_ids.add(event["id"])
            normalized.append(event)
            if len(normalized) >= MAX_EVENTS:
                logger.warning(f"{tag()} ⚠️ 时间表事项超过上限 {MAX_EVENTS}，已截断")
                break
        return normalized

    def load(self) -> None:
        """从 ``calendar_data.yaml`` 加载事项到内存单例（启动时调用）。"""
        try:
            calendar_file = self._calendar_file_path()

            if not os.path.exists(calendar_file):
                self.store.set_events([])
                logger.info(f"{tag()} ℹ️ 暂无时间表数据文件（首次运行）")
                return

            data = load_mapping(calendar_file)
            if data is None:
                return

            events = self._normalize_events(data.get("events", []))
            self.store.set_events(events)
            logger.info(f"{tag()} ✅ 已加载 {len(events)} 条时间表事项")
        except (FileNotFoundError, OSError) as e:
            logger.warning(f"{tag()} ⚠️ 时间表加载失败: {e}")

    def save(self) -> bool:
        """将内存单例中的事项原子性写入文件。"""
        try:
            calendar_file = self._calendar_file_path()
            payload = self._build_payload()
            ok = atomic_write_yaml(
                calendar_file,
                payload,
                header="time_awareness 时间表数据（自动生成，可手动编辑）",
            )
            if ok:
                logger.debug(f"{tag()} ✅ 时间表已保存到: {calendar_file}")
            return ok
        except Exception as e:
            logger.error(f"{tag()} ❌ 时间表保存错误: {e}")
            return False

    def _build_payload(self) -> dict:
        return {
            "version": CALENDAR_DATA_VERSION,
            "last_update": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "events": list(self.store.events),
        }

    def add_event(self, raw: dict) -> dict | None:
        """新增一条事项；成功返回创建后的事项，失败返回 None。"""
        if len(self.store.events) >= MAX_EVENTS:
            logger.warning(f"{tag()} ⚠️ 时间表事项已达上限 {MAX_EVENTS}，拒绝新增")
            return None
        payload = dict(raw or {})
        payload.pop("id", None)
        event = self.normalize_event(payload)
        if event is None:
            return None
        self.store.events.append(event)
        if not self.save():
            self.store.events.pop()
            return None
        return event

    def delete_event(self, event_id: str) -> bool:
        """按 id 删除事项；成功返回 True，未找到返回 False。"""
        event_id = str(event_id or "").strip()
        if not event_id:
            return False
        for idx, existing in enumerate(self.store.events):
            if existing.get("id") == event_id:
                removed = self.store.events.pop(idx)
                if not self.save():
                    self.store.events.insert(idx, removed)
                    return False
                return True
        return False

    def clear(
        self, scope: str = "all", year: int | None = None, month: int | None = None
    ) -> int:
        """批量清除；scope=all/year/month，返回删除数量（保存失败返回 0 不改动）。"""
        before = self.store.events
        if scope == "all":
            kept = []
        elif scope == "year" and year is not None:
            kept = [e for e in before if e.get("year") != year]
        elif scope == "month" and year is not None and month is not None:
            kept = [
                e
                for e in before
                if not (e.get("year") == year and e.get("month") == month)
            ]
        else:
            logger.warning(f"{tag()} ⚠️ 非法的清除范围: scope={scope}")
            return 0

        removed_count = len(before) - len(kept)
        if removed_count <= 0:
            return 0

        self.store.set_events(kept)
        if not self.save():
            self.store.set_events(before)
            return 0
        return removed_count

    def export_yaml(self) -> str:
        """将当前全部事项导出为 YAML 文本。"""
        payload = {
            "version": CALENDAR_DATA_VERSION,
            "events": [
                {
                    "year": e.get("year"),
                    "month": e.get("month"),
                    "day": e.get("day"),
                    "text": e.get("text"),
                    "repeat": e.get("repeat"),
                }
                for e in self.store.events
            ],
        }
        return dump_yaml_str(payload, header="time_awareness 时间表导出文件（YAML）")

    @staticmethod
    def parse_import_content(content: str):
        """解析导入 YAML：顶层数组或含 events 字段的映射；非法返回 None。"""
        if not isinstance(content, str) or not content.strip():
            return None
        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError:
            return None
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict) and isinstance(parsed.get("events"), list):
            return parsed["events"]
        return None

    def import_events(self, raw_events, mode: str = "merge") -> int:
        """导入事项；mode=merge/replace，返回规整后数量，保存失败返回 -1。"""
        incoming = self._normalize_events(raw_events)
        for event in incoming:
            event["id"] = uuid.uuid4().hex

        before = self.store.events
        if mode == "replace":
            merged = incoming
        else:
            merged = before + incoming
            if len(merged) > MAX_EVENTS:
                merged = merged[:MAX_EVENTS]
                logger.warning(f"{tag()} ⚠️ 导入后超过上限 {MAX_EVENTS}，已截断")

        self.store.set_events(merged)
        if not self.save():
            self.store.set_events(before)
            return -1
        # merge 返回本次实际新增量；replace 返回替换后实际落库量。
        return len(merged) if mode == "replace" else max(0, len(merged) - len(before))
