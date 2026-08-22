"""包内日志 wrapper：

- ``log_with_bot_id``：在日志前缀中附加机器人实例标识（如 ``[time_awareness:bot_demo]``），
  区分多 bot 共存场景。前缀通过 ``tag(event)`` 在调用点拼装——只有能拿到 event
  的调用点（hook/命令）才会带 platform_id，后台调度等无 event 的日志保持原样。

日志级别统一跟随 AstrBot 原生配置，不提供插件级提级（2.0.0 起移除 debug_to_info）。

各模块统一通过 ``logger.debug/info/...`` 调用，前缀用 ``tag(event=None)`` 函数获取。
"""

from astrbot.api import logger as _astrbot_logger


class _LoggerProxy:
    """转发到 astrbot logger。"""

    def debug(self, msg, *args, **kwargs):
        _astrbot_logger.debug(msg, *args, **kwargs)

    def info(self, msg, *args, **kwargs):
        _astrbot_logger.info(msg, *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        _astrbot_logger.warning(msg, *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        _astrbot_logger.error(msg, *args, **kwargs)

    def critical(self, msg, *args, **kwargs):
        _astrbot_logger.critical(msg, *args, **kwargs)

    def exception(self, msg, *args, **kwargs):
        _astrbot_logger.exception(msg, *args, **kwargs)


logger = _LoggerProxy()


# ==================== bot 实例区分 ====================

_with_bot_id = False


def configure(log_with_bot_id: bool = False) -> None:
    """启动时由 main.py 调用，按配置区分机器人实例。"""
    global _with_bot_id
    _with_bot_id = bool(log_with_bot_id)


def set_log_with_bot_id(enabled: bool) -> None:
    """独立 setter（用于配置热更新场景）。"""
    global _with_bot_id
    _with_bot_id = bool(enabled)


def tag(event=None) -> str:
    """日志前缀。

    - 默认返回 ``[time_awareness]``
    - ``log_with_bot_id=True`` 且传入 event 时返回 ``[time_awareness:platform_id]``

    后台调度 / 启动初始化等无 event 的调用点，传 ``tag()`` 即可——
    即使启用了 ``log_with_bot_id`` 也只会输出不带后缀的默认前缀。
    """
    if _with_bot_id and event is not None:
        try:
            pid = event.get_platform_id()
            if pid:
                return f"[time_awareness:{pid}]"
        except Exception:
            pass
    return "[time_awareness]"
