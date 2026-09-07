"""上次对话时间追踪：自维护字典 + yaml；record_* 只更新内存并经短防抖合并写盘。"""

import asyncio
import datetime

from ._datafile import atomic_write_yaml, load_mapping
from ..log import logger, tag

# 保留期：超 30 天未活跃会话在 load/flush 时清理，避免无限增长
_RETENTION_DAYS = 30


_DEFAULT_FILENAME = "last_chat_times.yaml"


class LastChatTracker:
    def __init__(self, data_dir: str, flush_delay_seconds: float = 2.0):
        self._path = f"{data_dir}/{_DEFAULT_FILENAME}"
        self._sessions: dict[str, dict[str, str]] = {}
        self._flush_delay_seconds = max(0.1, float(flush_delay_seconds))
        self._dirty = False
        self._flush_task: asyncio.Task | None = None

    def load(self) -> None:
        data = load_mapping(self._path) or {}
        sessions = data.get("sessions") or {}
        if isinstance(sessions, dict):
            self._sessions = {
                str(k): {"user": v.get("user", ""), "ai": v.get("ai", "")}
                for k, v in sessions.items()
                if isinstance(v, dict)
            }
        self._cleanup_old()
        self._dirty = False
        logger.info(f"{tag()} 💬 上次对话时间记录已加载: {len(self._sessions)} 会话")

    def _cleanup_old(self) -> int:
        """清理超过保留期未活跃的会话（内存与落盘双清理）。"""
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            days=_RETENTION_DAYS
        )
        before = len(self._sessions)

        def keep(entry: dict) -> bool:
            latest = entry.get("ai") or entry.get("user") or ""
            if not latest:
                return True
            try:
                return datetime.datetime.fromisoformat(latest) >= cutoff
            except (TypeError, ValueError):
                # 解析失败（naive/坏格式）保守保留
                return True

        self._sessions = {
            key: value for key, value in self._sessions.items() if keep(value)
        }
        return before - len(self._sessions)

    def flush(self) -> bool:
        """立即把脏数据写盘（写盘前清理过期会话）。"""
        self._cleanup_old()
        if not self._dirty:
            return True
        ok = atomic_write_yaml(
            self._path,
            {"sessions": self._sessions},
            header="上次对话时间记录",
        )
        if ok:
            self._dirty = False
        return ok

    def _schedule_flush(self) -> None:
        self._dirty = True
        if self._flush_task is not None and not self._flush_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 纯逻辑测试或启动前调用：留待显式 flush/close。
            return
        self._flush_task = loop.create_task(self._flush_later())

    async def _flush_later(self) -> None:
        try:
            await asyncio.sleep(self._flush_delay_seconds)
            self.flush()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"{tag()} ⚠️ 上次对话时间防抖写盘失败: {e}")
        finally:
            self._flush_task = None

    async def close(self) -> None:
        """取消防抖任务并强制写盘；由插件 terminate 调用。"""
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        self._flush_task = None
        if not self.flush():
            logger.warning(f"{tag()} ⚠️ 上次对话时间最终写盘失败")

    def record_user(self, umo: str, now: datetime.datetime) -> None:
        if not umo:
            return
        entry = self._sessions.setdefault(umo, {"user": "", "ai": ""})
        entry["user"] = now.isoformat()
        self._schedule_flush()

    def record_ai(self, umo: str, now: datetime.datetime) -> None:
        if not umo:
            return
        entry = self._sessions.setdefault(umo, {"user": "", "ai": ""})
        entry["ai"] = now.isoformat()
        self._schedule_flush()

    def summary(self, umo: str, now: datetime.datetime) -> str:
        """相对时间摘要（如「用户 5 分钟前 · AI 3 分钟前」）；无记录返回空串。"""
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
