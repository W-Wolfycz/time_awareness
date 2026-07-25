"""使用真实 AstrBot runtime 与 BubbleReply 运行主动消息适配组合测试。

该文件不由普通 pytest 自动导入；需在可导入 BubbleReply 的部署/工作区中，
使用 AstrBot 自带 Python 直接执行，全程使用假 Context，不会触发真实平台发信。
"""

import asyncio
from types import SimpleNamespace

import astrbot.api.message_components as Comp

from bubble_reply.main import BubbleReplyPlugin
from time_awareness.integrations import astrbot_sender as sender_module
from time_awareness.integrations.astrbot_sender import AstrBotMessageSender


class _Platform:
    def meta(self):
        return SimpleNamespace(
            id="demo",
            name="demo",
            support_streaming_message=True,
        )


class _Context:
    def __init__(self, send_results=None):
        self.platform = _Platform()
        self.platform_manager = SimpleNamespace(platform_insts=[self.platform])
        self.sent = []
        self.attempted = []
        self.send_results = list(send_results or [])

    def get_platform_inst(self, platform_id):
        return self.platform if platform_id == "demo" else None

    def get_config(self, _umo=None):
        return {"provider_settings": {"streaming_response": False}}

    async def send_message(self, session, chain):
        item = (str(session), chain)
        self.attempted.append(item)
        result = self.send_results.pop(0) if self.send_results else True
        if result:
            self.sent.append(item)
        return result


class _Registry:
    def __init__(self, handler):
        handlers = handler if isinstance(handler, (list, tuple)) else [handler]
        self._handlers = [
            SimpleNamespace(
                handler=item,
                handler_full_name="bubble_reply.main.on_decorating_result",
            )
            for item in handlers
        ]

    def get_handlers_by_event_type(self, *_args, **_kwargs):
        return self._handlers


class _EmptyRegistry:
    def get_handlers_by_event_type(self, *_args, **_kwargs):
        return []


async def main():
    plain_context = _Context()
    sender = AstrBotMessageSender(plain_context)
    original_registry = sender_module.star_handlers_registry
    sender_module.star_handlers_registry = _EmptyRegistry()
    try:
        result = await sender.decorate(
            "demo:GroupMessage:10001",
            [Comp.Plain("runtime smoke")],
        )
    finally:
        sender_module.star_handlers_registry = original_registry
    assert len(result) == 1
    assert isinstance(result[0], Comp.Plain)
    assert result[0].text == "runtime smoke"

    context = _Context()
    bubble_reply = BubbleReplyPlugin(
        context,
        {
            "basic_settings": {"split_scope": "ALL"},
            "delay_settings": {"delay_seconds": 0},
        },
    )
    await bubble_reply.initialize()
    sender_module.star_handlers_registry = _Registry(
        bubble_reply.on_decorating_result
    )
    try:
        outcome = await AstrBotMessageSender(context).send_decorated(
            "demo:GroupMessage:group_demo",
            [
                Comp.At(qq="10001"),
                Comp.Plain("第一段\n第二段\n第三段"),
            ],
        )
    finally:
        sender_module.star_handlers_registry = original_registry
        await bubble_reply.terminate()

    assert outcome.success is True
    assert len(context.sent) == 3
    sent_chains = [message.chain for _, message in context.sent]
    assert isinstance(sent_chains[0][0], Comp.At)
    assert str(sent_chains[0][0].qq) == "10001"
    assert [
        component.text
        for chain in sent_chains
        for component in chain
        if isinstance(component, Comp.Plain)
    ] == ["第一段", "第二段", "第三段"]
    assert sum(
        isinstance(component, Comp.At)
        for chain in sent_chains
        for component in chain
    ) == 1

    async def raising_hook(_event):
        class HookFailure(BaseException):
            pass

        raise HookFailure("synthetic hook failure")

    isolated_context = _Context()
    sender_module.star_handlers_registry = _Registry(raising_hook)
    try:
        isolated_outcome = await AstrBotMessageSender(
            isolated_context
        ).send_decorated(
            "demo:GroupMessage:group_demo",
            [Comp.Plain("仍应发送")],
        )
    finally:
        sender_module.star_handlers_registry = original_registry
    assert isolated_outcome.success is True
    assert len(isolated_context.sent) == 1

    async def stop_hook(event):
        event.stop_event()

    stopped_context = _Context()
    sender_module.star_handlers_registry = _Registry(stop_hook)
    try:
        stopped_outcome = await AstrBotMessageSender(
            stopped_context
        ).send_decorated(
            "demo:GroupMessage:group_demo",
            [Comp.Plain("不应重复发送")],
        )
    finally:
        sender_module.star_handlers_registry = original_registry
    assert stopped_outcome.success is True
    assert stopped_context.sent == []

    partial_context = _Context(send_results=[False, True])
    partial_bubble_reply = BubbleReplyPlugin(
        partial_context,
        {
            "basic_settings": {"split_scope": "ALL"},
            "delay_settings": {"delay_seconds": 0},
        },
    )
    await partial_bubble_reply.initialize()
    sender_module.star_handlers_registry = _Registry(
        partial_bubble_reply.on_decorating_result
    )
    try:
        partial_outcome = await AstrBotMessageSender(
            partial_context
        ).send_decorated(
            "demo:GroupMessage:group_demo",
            [
                Comp.At(qq="10001"),
                Comp.Plain("第一段\n第二段\n第三段"),
            ],
        )
    finally:
        sender_module.star_handlers_registry = original_registry
        await partial_bubble_reply.terminate()

    assert partial_outcome.success is False
    assert "前置分段发送失败" in partial_outcome.error
    assert len(partial_context.attempted) == 2
    assert len(partial_context.sent) == 1


if __name__ == "__main__":
    asyncio.run(main())
    print("runtime sender smoke: ok")
