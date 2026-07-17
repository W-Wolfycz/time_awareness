"""ChatMemory takeover contexts 读取适配。

只通过 ``context.get_registered_star`` 使用 ChatMemory 的公开实例 API，不直接导入
对方插件模块。插件未安装、未激活、没有当前 conversation 或查询失败时均返回空列表。
"""

from typing import Any

from ..log import logger, tag


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


async def load_chat_memory_contexts(
    context: Any,
    umo: str,
    *,
    persona_id: str,
    user_id: str = "",
) -> list[dict]:
    """读取与 ChatMemory 当前接管配置完全一致的 LLM ``contexts``。

    ``persona_id`` 与 ``user_id`` 始终传给 ChatMemory，由 ChatMemory 自己应用 persona、
    cross_session、full_group、状态、内容白名单、前缀和预算等接管配置。私聊没有显式
    ``user_id`` 时使用 UMO 的 session id；群聊不推断用户，防止静默扩大查询范围。
    """
    chat_memory = _resolve_chat_memory(context)
    if chat_memory is None:
        logger.debug(f"{tag()} ChatMemory 未安装或未启用，跳过主动提醒历史注入")
        return []

    try:
        conversation_manager = context.conversation_manager
        conversation_id = await conversation_manager.get_curr_conversation_id(umo)
        if not conversation_id:
            logger.debug(f"{tag()} 当前会话没有 conversation id，跳过历史注入")
            return []

        effective_user_id = (user_id or "").strip()
        parts = (umo or "").split(":", 2)
        if (
            not effective_user_id
            and len(parts) == 3
            and parts[1] == "FriendMessage"
        ):
            effective_user_id = parts[2]

        contexts = await chat_memory.build_takeover_contexts(
            umo=umo,
            user_id=effective_user_id,
            conversation_id=conversation_id,
            persona_id=persona_id,
        )
    except Exception as e:
        logger.warning(f"{tag()} ⚠️ ChatMemory 接管上下文查询失败，已降级为空历史: {e}")
        return []

    if contexts is None:
        logger.debug(f"{tag()} ChatMemory context takeover 未启用，跳过历史注入")
        return []
    return contexts
