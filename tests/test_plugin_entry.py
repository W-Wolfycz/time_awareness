"""插件类入口与事件分支：在桩下构造插件并走通生命周期关键路径。"""

import asyncio

from time_awareness.tests._shared import FakeContext, FakeEvent, install_astrbot_stubs

install_astrbot_stubs()

from astrbot.api.provider import ProviderRequest  # noqa: E402
from astrbot.api.star import StarTools  # noqa: E402
from time_awareness.constants import DEFAULT_TIME_GUIDANCE_PROMPT  # noqa: E402
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


def test_schedule_tool_injection_follows_config(tmp_path, monkeypatch):
    """工具注入接线：关闭（或日程不可用）时不注入，开启后注入带 handler 的工具。"""
    monkeypatch.setattr(
        StarTools, "get_data_dir", staticmethod(lambda name: str(tmp_path))
    )

    async def scenario():
        off = TimeAwarenessPlugin(FakeContext(), _config())
        await off.initialize()
        try:
            req = ProviderRequest(system_prompt="", extra_user_content_parts=[])
            await off.inject_time_context(
                FakeEvent(unified_msg_origin=GROUP_1, message_str="你好"), req
            )
            assert req.func_tool is None or req.func_tool.get_tool("daily_schedule") is None
        finally:
            await off.terminate()

        config = {
            "daily_schedule": {
                "enable_schedule": True,
                "ai_daily": {"enabled": True, "allow_ai_edit": True},
            },
            "time_awareness": {"time_guidance_enabled": True, "time_sensors": []},
            "calendar": {"enable_builtin_events": False},
        }
        on = TimeAwarenessPlugin(FakeContext(), config)
        await on.initialize()
        try:
            assert on.daily_schedule_service.enabled() is True
            req = ProviderRequest(system_prompt="", extra_user_content_parts=[])
            await on.inject_time_context(
                FakeEvent(unified_msg_origin=GROUP_1, message_str="你好"), req
            )
            tool = req.func_tool.get_tool("daily_schedule") if req.func_tool else None
            assert tool is not None and callable(tool.handler)

            # handler 透传：工具参数真的到达服务层（不是被吞掉或错位）
            event = FakeEvent(unified_msg_origin=GROUP_1, message_str="你好")
            assert "action 非法" in await on._handle_schedule_tool(event, action="bogus")
            assert "过去的日期" in await on._handle_schedule_tool(
                event, action="add", date="昨天", name="x", start="19:00", end="20:00", state="y"
            )
        finally:
            await on.terminate()

        # 时笺总开关关闭时不注入：模型看不到时间与日程上下文，不该改日程
        muted = {
            "daily_schedule": {
                "enable_schedule": True,
                "ai_daily": {"enabled": True, "allow_ai_edit": True},
            },
            "time_awareness": {"time_guidance_enabled": False, "time_sensors": []},
            "calendar": {"enable_builtin_events": False},
        }
        off_guidance = TimeAwarenessPlugin(FakeContext(), muted)
        await off_guidance.initialize()
        try:
            req = ProviderRequest(system_prompt="", extra_user_content_parts=[])
            await off_guidance.inject_time_context(
                FakeEvent(unified_msg_origin=GROUP_1, message_str="你好"), req
            )
            assert req.func_tool is None or req.func_tool.get_tool("daily_schedule") is None
        finally:
            await off_guidance.terminate()

    asyncio.run(scenario())
