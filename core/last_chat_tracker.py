"""
上次对话时间追踪——自维护字典 + yaml 持久化。

记录每个会话（按 ``unified_msg_origin``）的：

- 用户上次发言时间（``on_llm_request`` 钩子写入，命令天然被过滤）
- AI 上次发送时间（``after_message_sent`` 钩子写入，命令消息显式跳过）

不集成 ``chat_memory``：避免跨插件耦合（``query_history`` 必传 ``conversation_id``，
要复用 ``_get_curr_cid`` 私有 API）。

数据结构（持久化到 ``last_chat_times.yaml``）::

    sessions:
      "aiocqhttp:GroupMessage:12345":
        user: "2026-07-08T14:30:21+08:00"
        ai:  "2026-07-08T14:30:45+08:00"

每次 ``record_*`` 即时调 ``atomic_write_yaml`` 写盘（文件 < 1KB，实测 < 5ms），
无需 terminate 显式刷盘。
"""

import datetime

from ._datafile import atomic_write_yaml, load_mapping
from ..log import logger, tag


_DEFAULT_FILENAME = "last_chat_times.yaml"


class LastChatTracker:
    def __init__(self, data_dir: str):
        self._path = f"{data_dir}/{_DEFAULT_FILENAME}"
        self._sessions: dict[str, dict[str, str]] = {}

    def load(self) -> None:
        data = load_mapping(self._path) or {}
        sessions = data.get("sessions") or {}
        if isinstance(sessions, dict):
            self._sessions = {
                str(k): {"user": v.get("user", ""), "ai": v.get("ai", "")}
                for k, v in sessions.items()
                if isinstance(v, dict)
            }
        logger.info(f"{tag()} 💬 上次对话时间记录已加载: {len(self._sessions)} 会话")

    def _save(self) -> None:
        atomic_write_yaml(self._path, {"sessions": self._sessions}, header="上次对话时间记录")

    def record_user(self, umo: str, now: datetime.datetime) -> None:
        if not umo:
            return
        entry = self._sessions.setdefault(umo, {"user": "", "ai": ""})
        entry["user"] = now.isoformat()
        self._save()

    def record_ai(self, umo: str, now: datetime.datetime) -> None:
        if not umo:
            return
        entry = self._sessions.setdefault(umo, {"user": "", "ai": ""})
        entry["ai"] = now.isoformat()
        self._save()

    def summary(self, umo: str, now: datetime.datetime) -> str:
        """返回相对时间摘要，如「用户 5 分钟前 · AI 3 分钟前」。

        全无记录返回空串（调用方据此省略 ``<LAST_CHAT>`` 标签）；
        仅一边有记录只显示一边。
        """
        entry = self._sessions.get(umo)
        if not entry:
            return ""
        parts = []
        if entry.get("user"):
            parts.append(f"用户 {self._relative(entry['user'], now)}")
        if entry.get("ai"):
            parts.append(f"AI {self._relative(entry['ai'], now)}")
        return " · ".join(parts) if parts else ""

    @staticmethod
    def _relative(iso_str: str, now: datetime.datetime) -> str:
        try:
            dt = datetime.datetime.fromisoformat(iso_str)
        except (ValueError, TypeError):
            return ""
        # 对齐 tzinfo（naive vs aware 不能直接相减）
        if dt.tzinfo is None and now.tzinfo is not None:
            dt = dt.replace(tzinfo=now.tzinfo)
        elif dt.tzinfo is not None and now.tzinfo is None:
            now = now.replace(tzinfo=dt.tzinfo)
        delta = now - dt
        secs = delta.total_seconds()
        if secs < 0:
            return "刚刚"  # 时钟漂移兜底
        if secs < 60:
            return "刚刚"
        if secs < 3600:
            return f"{int(secs // 60)} 分钟前"
        if secs < 86400:
            return f"{int(secs // 3600)} 小时前"
        if secs < 86400 * 7:
            return f"{int(secs // 86400)} 天前"
        return dt.strftime("%m-%d")  # 超过一周用日期
