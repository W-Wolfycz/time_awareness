import asyncio
from types import SimpleNamespace

from time_awareness.integrations.persona_resolver import (
    resolve_effective_persona,
    resolve_persona_prompt,
)


class _ConversationManager:
    async def get_curr_conversation_id(self, umo):
        return "conversation_demo"

    async def get_conversation(self, umo, conversation_id):
        return SimpleNamespace(persona_id="persona_conversation")


class _PersonaManager:
    def __init__(self, result):
        self.result = result
        self.kwargs = None

    async def resolve_selected_persona(self, **kwargs):
        self.kwargs = kwargs
        return self.result


class _Platform:
    def meta(self):
        return SimpleNamespace(name="platform_name_demo")


class _Context:
    def __init__(self, result):
        self.conversation_manager = _ConversationManager()
        self.persona_manager = _PersonaManager(result)

    def get_config(self, umo):
        return {
            "provider_settings": {
                "default_personality": "persona_default",
            }
        }

    def get_platform_inst(self, platform_id):
        assert platform_id == "platform_id_demo"
        return _Platform()


def test_effective_persona_matches_chat_memory_resolution_inputs():
    context = _Context(
        (
            "persona_forced",
            {"name": "persona_forced", "prompt": "演示人设提示词"},
            "persona_forced",
            False,
        )
    )
    persona_id, persona = asyncio.run(
        resolve_effective_persona(
            context,
            "platform_id_demo:GroupMessage:group_demo",
        )
    )

    assert persona_id == "persona_forced"
    assert persona["prompt"] == "演示人设提示词"
    assert context.persona_manager.kwargs == {
        "umo": "platform_id_demo:GroupMessage:group_demo",
        "conversation_persona_id": "persona_conversation",
        "platform_name": "platform_name_demo",
        "provider_settings": {
            "default_personality": "persona_default",
        },
    }

    prompt = asyncio.run(
        resolve_persona_prompt(
            context,
            "platform_id_demo:GroupMessage:group_demo",
        )
    )
    assert prompt == "演示人设提示词"


def test_none_persona_marker_disables_persona_prompt():
    context = _Context(("[%None]", None, None, False))
    persona_id, persona = asyncio.run(
        resolve_effective_persona(
            context,
            "platform_id_demo:GroupMessage:group_demo",
        )
    )
    assert persona_id == ""
    assert persona is None
    assert (
        asyncio.run(
            resolve_persona_prompt(
                context,
                "platform_id_demo:GroupMessage:group_demo",
            )
        )
        == ""
    )
