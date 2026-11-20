"""人设预萃取结果的持久化：按「人设文本指纹 + 提示词版本」缓存，一次萃取终身复用。"""

from __future__ import annotations

import copy
import datetime
import os

from ._datafile import atomic_write_yaml, load_mapping
from ..log import logger, tag


SUMMARY_SCHEMA_VERSION = 1
SUMMARY_FILE_NAME = "persona_summaries.yaml"
# 条目上限：键是人设指纹，人设每改一次就多一条，上限防长期无限增长
MAX_ENTRIES = 64


class PersonaSummaryStore:
    """``persona_summaries.yaml`` 的读写：key = 人设指纹，value = 摘要与其元信息。"""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._path = os.path.join(data_dir, SUMMARY_FILE_NAME)
        self._entries: dict[str, dict] = {}

    def load(self) -> bool:
        os.makedirs(self.data_dir, exist_ok=True)
        data = load_mapping(self._path)
        if data is None:
            self._entries = {}
            return True
        if data.get("schema_version") != SUMMARY_SCHEMA_VERSION:
            logger.info(
                f"{tag()} 🗑️ 人设摘要文件 schema_version={data.get('schema_version')} "
                f"与当前 {SUMMARY_SCHEMA_VERSION} 不一致，已忽略（会重新萃取）"
            )
            self._entries = {}
            return True
        entries = data.get("entries")
        self._entries = entries if isinstance(entries, dict) else {}
        if self._entries:
            logger.info(f"{tag()} ✅ 已加载 {len(self._entries)} 份人设摘要")
        return True

    def get(self, fingerprint: str, revision: str) -> dict | None:
        """按指纹 + 提示词版本取摘要；版本不匹配视为未命中（提示词改过就要重萃）。"""
        entry = self._entries.get(str(fingerprint or ""))
        if not isinstance(entry, dict):
            return None
        if str(entry.get("revision", "")) != str(revision):
            return None
        summary = entry.get("summary")
        return copy.deepcopy(summary) if isinstance(summary, dict) else None

    def entry(self, fingerprint: str) -> dict | None:
        """取完整条目（含模型、时间、字数），供 /schedule summary 展示。"""
        entry = self._entries.get(str(fingerprint or ""))
        return copy.deepcopy(entry) if isinstance(entry, dict) else None

    def put(
        self,
        fingerprint: str,
        *,
        summary: dict,
        revision: str,
        model: str,
        chars: int,
        persona_label: str = "",
    ) -> bool:
        key = str(fingerprint or "")
        if not key or not isinstance(summary, dict):
            return False
        self._entries.pop(key, None)
        while len(self._entries) >= MAX_ENTRIES:
            oldest = min(
                self._entries,
                key=lambda k: str(self._entries[k].get("created_at", "")),
            )
            self._entries.pop(oldest, None)
        self._entries[key] = {
            "revision": str(revision),
            "model": str(model or ""),
            "chars": int(chars),
            "persona_label": str(persona_label or ""),
            "created_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "summary": copy.deepcopy(summary),
        }
        return self._save()

    def _save(self) -> bool:
        return atomic_write_yaml(
            self._path,
            {
                "schema_version": SUMMARY_SCHEMA_VERSION,
                "entries": self._entries,
            },
            header="time_awareness 人设预萃取缓存（按人设文本指纹索引，可安全删除以强制重萃）",
        )
