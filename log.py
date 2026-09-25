"""包内日志 wrapper：转发 astrbot logger，并用 tag(event) 拼装前缀区分多 bot。"""

from astrbot.api import logger as _astrbot_logger


class _LoggerProxy:
    def __getattr__(self, name):
        return getattr(_astrbot_logger, name)


logger = _LoggerProxy()


_with_bot_id = False


def configure(log_with_bot_id: bool = False) -> None:
    global _with_bot_id
    _with_bot_id = bool(log_with_bot_id)


def tag(event=None) -> str:
    """返回 [time_awareness]；启用 log_with_bot_id 且有 event 时追加 [platform:{id}]。"""
    if _with_bot_id and event is not None:
        try:
            pid = event.get_platform_id()
            if pid:
                return f"[time_awareness][platform:{pid}]"
        except Exception:
            pass
    return "[time_awareness]"
