"""插件类入口与事件分支：在桩下构造插件并走通生命周期关键路径。"""

import asyncio

from time_awareness.tests._shared import FakeContext, FakeEvent, install_astrbot_stubs

install_astrbot_stubs()

from astrbot.api import logger as api_logger  # noqa: E402
from astrbot.api.provider import ProviderRequest  # noqa: E402
from astrbot.api.star import StarTools  # noqa: E402
from time_awareness.constants import DEFAULT_TIME_GUIDANCE_PROMPT  # noqa: E402
from time_awareness.log import logger as proxy_logger  # noqa: E402
from time_awareness.main import TimeAwarenessPlugin  # noqa: E402

GROUP_1 = "aiocqhttp:GroupMessage:10001"


def _config():
    return {
        "daily_schedule": {"enable_schedule": False},
        "time_awareness": {"time_guidance_enabled": True, "time_sensors": []},
        "calendar": {"enable_builtin_events": False},
    }


def test_plugin_entry_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(
        StarTools, "get_data_dir", staticmethod(lambda name: str(tmp_path))
    )

    async def scenario():
        plugin = TimeAwarenessPlugin(FakeContext(), _config())
        await plugin.initialize()
        try:
            event = FakeEvent(unified_msg_origin=GROUP_1, message_str="你好")
            req = ProviderRequest(system_prompt="", extra_user_content_parts=[])
            await plugin.inject_time_context(event, req)
            assert req.system_prompt == DEFAULT_TIME_GUIDANCE_PROMPT
            assert len(req.extra_user_content_parts) == 1
            assert "<SCHEDULE_STATE>" in req.extra_user_content_parts[0].text

            # 静态规则幂等，动态临时片段每次追加
            await plugin.inject_time_context(event, req)
            assert req.system_prompt.count(DEFAULT_TIME_GUIDANCE_PROMPT) == 1
            assert len(req.extra_user_content_parts) == 2

            await plugin._record_ai_sent_time(
                FakeEvent(unified_msg_origin=GROUP_1, message_str="回复")
            )
            assert plugin.last_chat_tracker._sessions[GROUP_1]["ai"]

            await plugin._record_ai_sent_time(
                FakeEvent(unified_msg_origin="aiocqhttp:GroupMessage:20002", message_str="/x")
            )
            assert "aiocqhttp:GroupMessage:20002" not in plugin.last_chat_tracker._sessions
        finally:
            await plugin.terminate()

    asyncio.run(scenario())


def test_logger_proxy_forwards_levels():
    api_logger.calls.clear()
    proxy_logger.debug("d")
    proxy_logger.info("i")
    proxy_logger.warning("w")
    proxy_logger.error("e")
    assert [c[0] for c in api_logger.calls] == ["debug", "info", "warning", "error"]
