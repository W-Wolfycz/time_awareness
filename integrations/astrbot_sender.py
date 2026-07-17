"""主动消息发送适配层。

``Context.send_message`` 不经过 ResultDecorate Pipeline。为了兼容其他分段插件，
这里构造最小事件并按注册优先级运行 ``on_decorating_result``。Hook 可能主动发送
部分消息，因此一旦开始执行装饰链，后续失败均视为不可安全自动重试。
"""

from dataclasses import dataclass
from typing import Any

from astrbot.core.message.message_event_result import (
    MessageChain,
    MessageEventResult,
    ResultContentType,
)
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, Group, MessageMember
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.star.star_handler import EventType, star_handlers_registry

from ..log import logger, tag


@dataclass(frozen=True)
class SendOutcome:
    success: bool
    error: str = ""
    side_effects_started: bool = False


class AstrBotMessageSender:
    def __init__(self, context: Any):
        self.context = context

    async def send_decorated(self, session: str, components: list) -> SendOutcome:
        """运行装饰链并发送剩余消息链。"""
        try:
            processed = await self.decorate(session, components)
        except Exception as e:
            logger.error(f"{tag()} ❌ 主动消息装饰链失败: {e}")
            return SendOutcome(False, str(e), side_effects_started=True)

        if not processed:
            # Hook 清空结果通常表示它已自行完成发送或明确吞掉消息。
            return SendOutcome(True, side_effects_started=True)

        try:
            sent = await self.context.send_message(
                session,
                MessageChain(chain=processed),
            )
        except Exception as e:
            return SendOutcome(False, str(e), side_effects_started=True)
        if not sent:
            return SendOutcome(
                False,
                f"找不到 session 对应的平台: {session}",
                side_effects_started=True,
            )
        return SendOutcome(True, side_effects_started=True)

    async def decorate(self, session: str, components: list) -> list:
        message_session = MessageSession.from_str(session)
        platform_inst = self.context.get_platform_inst(message_session.platform_id)
        if platform_inst is None:
            platform_inst = next(
                (
                    platform
                    for platform in self.context.platform_manager.platform_insts
                    if platform.meta().name == message_session.platform_id
                ),
                None,
            )
        if platform_inst is None:
            return list(components)

        target_id = message_session.session_id
        message_obj = AstrBotMessage()
        message_obj.type = message_session.message_type
        if message_session.message_type == MessageType.GROUP_MESSAGE:
            message_obj.group = Group(group_id=target_id)
        message_obj.session_id = target_id
        message_obj.message = list(components)
        message_obj.self_id = "bot"
        message_obj.sender = MessageMember(user_id=target_id)
        message_obj.message_str = ""
        message_obj.raw_message = None
        message_obj.message_id = ""

        event = AstrMessageEvent(
            message_str="",
            message_obj=message_obj,
            platform_meta=platform_inst.meta(),
            session_id=target_id,
        )
        result = MessageEventResult()
        result.chain = list(components)
        result.set_result_content_type(ResultContentType.LLM_RESULT)
        event.set_result(result)

        handlers = star_handlers_registry.get_handlers_by_event_type(
            EventType.OnDecoratingResultEvent,
            plugins_name=event.plugins_name,
        )
        logger.debug(
            f"{tag()} 🎭 跑装饰链: session={session} handlers={len(handlers)}"
        )
        for handler in handlers:
            try:
                await handler.handler(event)
            except Exception as e:
                logger.warning(
                    f"{tag()} ⚠️ 装饰钩子执行失败 {handler.handler_full_name}: {e}"
                )
            if event.is_stopped():
                break

        final_result = event.get_result()
        if final_result and final_result.chain is not None:
            return list(final_result.chain)
        return []
