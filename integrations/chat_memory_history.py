"""ChatMemory takeover contexts 读取适配；未安装/未启用/查询失败均返回空列表。"""

from dataclasses import dataclass
from typing import Any

from ..log import logger, tag


CHAT_MEMORY_TAKEOVER_APPLIED_EXTRA = "chat_memory_takeover_applied"


@dataclass(frozen=True)
class ChatMemoryContextState:
    """一次只读 takeover 查询的结果与开关状态。"""

    contexts: list[dict]
    takeover_enabled: bool = False


def is_chat_memory_takeover_applied(event: Any) -> bool:
    """判断 ChatMemory 是否已接管当前请求；读取失败按未接管处理。"""
    try:
        return bool(event.get_extra(CHAT_MEMORY_TAKEOVER_APPLIED_EXTRA, False))
    except Exception:
        return False


def _resolve_chat_memory(context: Any):
    """返回已激活且提供只读 takeover API 的 ChatMemory 实例。"""
    try:
        star = context.get_registered_star("chat_memory")
        if star is None or not bool(getattr(star, "activated", True)):
            return None
        for candidate in (
            star,
            getattr(star, "star", None),
            getattr(star, "star_cls", None),
        ):
            if candidate is not None and callable(
                getattr(candidate, "build_takeover_contexts", None)
            ):
                return candidate
    except Exception as e:
        logger.debug(f"{tag()} ChatMemory 实例查询失败: {e}")
    return None


async def load_chat_memory_context_state(
    context: Any,
    umo: str,
    *,
    persona_id: str,
    user_id: str = "",
) -> ChatMemoryContextState:
    """读取与 ChatMemory 当前接管配置一致的 LLM contexts（persona_id/user_id 始终传入）。"""
    chat_memory = _resolve_chat_memory(context)
    if chat_memory is None:
        logger.debug(f"{tag()} ChatMemory 未安装或未启用，跳过主动提醒历史注入")
        return ChatMemoryContextState([])

    try:
        conversation_manager = context.conversation_manager
        conversation_id = await conversation_manager.get_curr_conversation_id(umo)
        if not conversation_id:
            logger.debug(f"{tag()} 当前会话没有 conversation id，跳过历史注入")
            return ChatMemoryContextState([])

        effective_user_id = (user_id or "").strip()
        parts = (umo or "").split(":", 2)
        if not effective_user_id and len(parts) == 3 and parts[1] == "FriendMessage":
            effective_user_id = parts[2]

        contexts = await chat_memory.build_takeover_contexts(
            umo=umo,
            user_id=effective_user_id,
            conversation_id=conversation_id,
            persona_id=persona_id,
        )
    except Exception as e:
        logger.warning(f"{tag()} ⚠️ ChatMemory 接管上下文查询失败，已降级为空历史: {e}")
        return ChatMemoryContextState([])

    if contexts is None:
        logger.debug(f"{tag()} ChatMemory context takeover 未启用，跳过历史注入")
        return ChatMemoryContextState([])
    return ChatMemoryContextState(list(contexts), takeover_enabled=True)


async def load_chat_memory_contexts(
    context: Any,
    umo: str,
    *,
    persona_id: str,
    user_id: str = "",
) -> list[dict]:
    """兼容接口：只返回 takeover contexts，不暴露状态对象。"""
    state = await load_chat_memory_context_state(
        context,
        umo,
        persona_id=persona_id,
        user_id=user_id,
    )
    return state.contexts
