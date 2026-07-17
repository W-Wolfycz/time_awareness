import asyncio
from types import SimpleNamespace

from time_awareness.integrations.chat_memory_history import (
    load_chat_memory_contexts,
)


class _ChatMemory:
    def __init__(self, result=None):
        self.kwargs = None
        self.result = result if result is not None else [
            {"role": "user", "content": "第一轮问题", "_no_save": True},
            {"role": "assistant", "content": "第一轮回答", "_no_save": True},
            {"role": "user", "content": "第二轮问题", "_no_save": True},
            {"role": "assistant", "content": "第二轮回答", "_no_save": True},
        ]

    async def build_takeover_contexts(self, **kwargs):
        self.kwargs = kwargs
        return self.result


class _ConversationManager:
    async def get_curr_conversation_id(self, umo):
        return "conversation_demo"


class _Context:
    def __init__(self, *, activated=True):
        self.chat_memory = _ChatMemory()
        self.conversation_manager = _ConversationManager()
        self.metadata = SimpleNamespace(
            activated=activated,
            star_cls=self.chat_memory,
        )

    def get_registered_star(self, name):
        assert name == "chat_memory"
        return self.metadata


def test_chat_memory_history_uses_takeover_context_api():
    context = _Context()
    contexts = asyncio.run(
        load_chat_memory_contexts(
            context,
            "demo:GroupMessage:group_demo",
            persona_id="persona_demo",
            user_id="10001",
        )
    )

    assert contexts == [
        {"role": "user", "content": "第一轮问题", "_no_save": True},
        {"role": "assistant", "content": "第一轮回答", "_no_save": True},
        {"role": "user", "content": "第二轮问题", "_no_save": True},
        {"role": "assistant", "content": "第二轮回答", "_no_save": True},
    ]
    assert context.chat_memory.kwargs == {
        "umo": "demo:GroupMessage:group_demo",
        "user_id": "10001",
        "conversation_id": "conversation_demo",
        "persona_id": "persona_demo",
    }


def test_private_session_derives_user_id_and_disabled_takeover_is_empty():
    context = _Context()
    context.chat_memory.result = None
    contexts = asyncio.run(
        load_chat_memory_contexts(
            context,
            "demo:FriendMessage:10001",
            persona_id="persona_demo",
        )
    )
    assert contexts == []
    assert context.chat_memory.kwargs["user_id"] == "10001"


def test_inactive_chat_memory_skips_history_injection():
    context = _Context(activated=False)
    contexts = asyncio.run(
        load_chat_memory_contexts(
            context,
            "demo:GroupMessage:group_demo",
            persona_id="persona_demo",
        )
    )
    assert contexts == []
    assert context.chat_memory.kwargs is None
