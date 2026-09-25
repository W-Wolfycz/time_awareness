"""当前会话 Persona 解析适配：session 强制 > conversation persona > 会话默认。"""

from typing import Any

from ..log import logger, tag


def _platform_name(context: Any, umo: str, event: Any = None) -> str:
    """取得与 event.get_platform_name() 一致的平台名称（无事件时经 UMO 平台 id 查找实例）。"""
    if event is not None:
        try:
            return (event.get_platform_name() or "").strip()
        except Exception:
            pass

    platform_id = umo.split(":", 1)[0] if umo else ""
    if not platform_id:
        return ""
    try:
        platform = context.get_platform_inst(platform_id)
        if platform is not None:
            return (platform.meta().name or "").strip()
    except Exception as e:
        logger.debug(f"{tag()} platform name 查询失败 umo={umo}: {e}")
    return platform_id


async def resolve_effective_persona(
    context: Any,
    umo: str,
    event: Any = None,
) -> tuple[str, Any | None]:
    """按 chat_memory 方式解析生效 Persona；None/[%None]/异常返回 ("", None)。"""
    try:
        conversation_persona_id = None
        try:
            conversation_manager = context.conversation_manager
            conversation_id = await conversation_manager.get_curr_conversation_id(umo)
            if conversation_id:
                conversation = await conversation_manager.get_conversation(
                    umo,
                    conversation_id,
                )
                conversation_persona_id = getattr(conversation, "persona_id", None) or None
        except Exception as e:
            logger.debug(f"{tag()} conversation persona 查询失败 umo={umo}: {e}")

        config = context.get_config(umo=umo) or {}
        provider_settings = config.get("provider_settings", {}) or {}
        resolved, persona, _, _ = await context.persona_manager.resolve_selected_persona(
            umo=umo,
            conversation_persona_id=conversation_persona_id,
            platform_name=_platform_name(context, umo, event),
            provider_settings=provider_settings,
        )
        if not resolved or resolved == "[%None]":
            return "", None
        return str(resolved), persona
    except Exception as e:
        logger.warning(f"{tag()} ⚠️ resolve_selected_persona 失败 umo={umo}: {e}")
        return "", None


def extract_persona_prompt(persona: Any | None) -> str:
    """从 ``resolve_selected_persona`` 返回对象中提取 prompt。"""
    if not persona:
        return ""
    if isinstance(persona, dict):
        return (persona.get("prompt") or "").strip()
    return (getattr(persona, "prompt", "") or "").strip()
