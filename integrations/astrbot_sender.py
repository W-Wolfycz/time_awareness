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


class _ProactiveMessageEvent(AstrMessageEvent):
    """让主动消息装饰器获得与普通平台事件一致的 ``send()`` 语义。

    基础 ``AstrMessageEvent.send()`` 只记录发送指标，不会调用平台。BubbleReply
    等装饰器会用 ``event.send()`` 发出前 N-1 段，因此主动消息必须在这里显式
    委托 ``Context.send_message()``，否则前置分段（以及通常位于首段的 At）会丢失。
    """

    def __init__(
        self,
        *,
        context: Any,
        outbound_session: MessageSession,
        message_obj: AstrBotMessage,
        platform_meta: Any,
        session_id: str,
    ) -> None:
        self._context = context
        self._outbound_session = outbound_session
        self._active_send_error = ""
        super().__init__(
            message_str="",
            message_obj=message_obj,
            platform_meta=platform_meta,
            session_id=session_id,
        )

    async def send(self, message: MessageChain) -> None:
        try:
            sent = await self._context.send_message(self._outbound_session, message)
        except Exception as e:
            self._active_send_error = f"{type(e).__name__}: {e}"
            raise
        if not sent:
            self._active_send_error = (
                f"找不到 session 对应的平台: {self._outbound_session}"
            )
            raise RuntimeError(self._active_send_error)
        # 保留 AstrMessageEvent 的发送标记与指标语义；平台子类同样在实际发送成功后调用。
        await super().send(message)

    @property
    def active_send_error(self) -> str:
        """装饰器通过 ``event.send()`` 发送前置分段时的最近一次失败。"""
        return self._active_send_error


class AstrBotMessageSender:
    def __init__(self, context: Any):
        self.context = context

    async def send_decorated(
        self,
        session: str,
        components: list,
        *,
        sender_id: str = "",
    ) -> SendOutcome:
        """运行装饰链并发送剩余消息链。"""
        try:
            processed, event = await self._decorate(
                session,
                components,
                sender_id=sender_id,
            )
        except Exception as e:
            logger.error(f"{tag()} ❌ 主动消息装饰链失败: {e}")
            return SendOutcome(False, str(e), side_effects_started=True)

        if not processed:
            # Hook 清空结果通常表示它已自行完成发送或明确吞掉消息。
            if event and event.active_send_error:
                return SendOutcome(
                    False,
                    f"装饰器前置分段发送失败: {event.active_send_error}",
                    side_effects_started=True,
                )
            return SendOutcome(True, side_effects_started=True)

        try:
            sent = await self.context.send_message(
                session,
                MessageChain(chain=processed),
            )
        except Exception as e:
            # 装饰链未执行（platform_inst 缺失）时失败没有副作用，可安全重试
            return SendOutcome(
                False, str(e), side_effects_started=event is not None
            )
        if not sent:
            return SendOutcome(
                False,
                f"找不到 session 对应的平台: {session}",
                side_effects_started=event is not None,
            )
        if event and event.active_send_error:
            # BubbleReply 等装饰器会在单段失败后保留剩余结果，让最终发送仍可继续。
            # 此时尽量发完剩余内容，但不能把整条任务误记为成功，也不能自动重发。
            return SendOutcome(
                False,
                f"装饰器前置分段发送失败: {event.active_send_error}",
                side_effects_started=True,
            )
        return SendOutcome(True, side_effects_started=True)

    async def _decorate(
        self,
        session: str,
        components: list,
        *,
        sender_id: str = "",
    ) -> tuple[list, _ProactiveMessageEvent | None]:
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
            return list(components), None

        target_id = message_session.session_id
        message_obj = AstrBotMessage()
        message_obj.type = message_session.message_type
        if message_session.message_type == MessageType.GROUP_MESSAGE:
            message_obj.group = Group(group_id=target_id)
        message_obj.session_id = target_id
        message_obj.message = list(components)
        message_obj.self_id = "bot"
        # 主动消息没有真实的入站发送者。群个人提醒用目标用户身份，供依赖
        # sender_id 的装饰器正确选作用域；群广播及私聊主动消息保持空值。
        synthetic_sender_id = (
            str(sender_id or "").strip()
            if message_session.message_type == MessageType.GROUP_MESSAGE
            else ""
        )
        message_obj.sender = MessageMember(user_id=synthetic_sender_id)
        message_obj.message_str = ""
        message_obj.raw_message = None
        message_obj.message_id = ""

        event = _ProactiveMessageEvent(
            context=self.context,
            outbound_session=message_session,
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
            except BaseException as e:
                logger.warning(
                    f"{tag()} ⚠️ 装饰钩子执行失败 {handler.handler_full_name}: {e}"
                )
            if event.is_stopped():
                # 与 AstrBot ResultDecorateStage 一致：stop_event() 后不再进入
                # Respond/最终发送阶段。装饰器若已自行发送，调用方会将其视为已处理。
                return [], event

        final_result = event.get_result()
        if final_result and final_result.chain is not None:
            return list(final_result.chain), event
        return [], event
