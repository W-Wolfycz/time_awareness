"""ChatMemory takeover 标记读取适配。"""

from typing import Any


CHAT_MEMORY_TAKEOVER_APPLIED_EXTRA = "chat_memory_takeover_applied"


def is_chat_memory_takeover_applied(event: Any) -> bool:
    """判断 ChatMemory 是否已接管当前请求；读取失败按未接管处理。"""
    try:
        return bool(event.get_extra(CHAT_MEMORY_TAKEOVER_APPLIED_EXTRA, False))
    except Exception:
        return False
