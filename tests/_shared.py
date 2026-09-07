"""共享测试基础设施：astrbot 桩安装器 + 通用 Fake（幂等）。"""

import datetime
import sys
import types

NOW = datetime.datetime(2026, 8, 11, 7, 30, tzinfo=datetime.timezone.utc)


def _identity(func):
    return func


def _api():
    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = getattr(astrbot, "api", None)
    if api is None:
        api = types.ModuleType("astrbot.api")
        astrbot.api = api
        sys.modules["astrbot.api"] = api
    return api


def _module(name, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


def install_astrbot_stubs():
    if "astrbot.api.event" not in sys.modules:
        class _PermissionType:
            ADMIN = "admin"

        class _Filter:
            PermissionType = _PermissionType
            on_llm_request = lambda self, priority=-100: _identity
            after_message_sent = lambda self, *a, **k: _identity
            permission_type = lambda self, perm: _identity
            llm_tool = lambda self, name="", **k: _identity

            def command_group(self, name):
                def decorate(func):
                    func.command = lambda _cmd: _identity
                    return func

                return decorate

        _api().event = _module(
            "astrbot.api.event", AstrMessageEvent=type("AstrMessageEvent", (), {}), filter=_Filter()
        )

    if "astrbot.api.provider" not in sys.modules:
        class ProviderRequest:
            def __init__(self, system_prompt="", extra_user_content_parts=None):
                self.system_prompt = system_prompt
                self.extra_user_content_parts = list(extra_user_content_parts or [])

        _api().provider = _module("astrbot.api.provider", ProviderRequest=ProviderRequest)

    if "astrbot.api.star" not in sys.modules:
        class Star:
            def __init__(self, context=None):
                self.context = context

        class StarTools:
            @staticmethod
            def get_data_dir(name):
                import os

                return os.path.join(os.getcwd(), "data", "plugin_data", str(name))

        _api().star = _module(
            "astrbot.api.star", Context=type("Context", (), {}), Star=Star, StarTools=StarTools
        )

    if "astrbot.core" not in sys.modules:
        class TextPart:
            def __init__(self, text="", **kwargs):
                self.text = text
                self.temp = False

            def mark_as_temp(self):
                self.temp = True
                return self

        class MessageChain:
            def __init__(self, chain=None):
                self.chain = list(chain or [])

        class MessageEventResult:
            def __init__(self):
                self.chain = None

            def set_result_content_type(self, content_type):
                self.result_content_type = content_type

        class Group:
            def __init__(self, group_id=""):
                self.group_id = group_id

        class MessageMember:
            def __init__(self, user_id=""):
                self.user_id = user_id

        class MessageSession:
            def __init__(self, platform_id="", session_id="", message_type=None):
                self.platform_id = platform_id
                self.session_id = session_id
                self.message_type = message_type

            @classmethod
            def from_str(cls, text):
                parts = str(text or "").split(":", 2)
                return cls(parts[0] if parts else "", parts[2] if len(parts) > 2 else "")

        class _HandlersRegistry:
            def get_handlers_by_event_type(self, event_type, plugins_name=None):
                return []

        core = _module("astrbot.core")
        core.agent = _module("astrbot.core.agent")
        core.agent.message = _module("astrbot.core.agent.message", TextPart=TextPart)
        core.message = _module("astrbot.core.message")
        core.message.message_event_result = _module(
            "astrbot.core.message.message_event_result",
            MessageChain=MessageChain,
            MessageEventResult=MessageEventResult,
            ResultContentType=type("ResultContentType", (), {"LLM_RESULT": "llm_result"}),
        )
        core.platform = _module("astrbot.core.platform")
        core.platform.astr_message_event = _module(
            "astrbot.core.platform.astr_message_event",
            AstrMessageEvent=type("AstrMessageEvent", (), {}),
        )
        core.platform.astrbot_message = _module(
            "astrbot.core.platform.astrbot_message",
            AstrBotMessage=type("AstrBotMessage", (), {}),
            Group=Group,
            MessageMember=MessageMember,
        )
        core.platform.message_session = _module(
            "astrbot.core.platform.message_session", MessageSession=MessageSession
        )
        core.platform.message_type = _module(
            "astrbot.core.platform.message_type",
            MessageType=type("MessageType", (), {"GROUP_MESSAGE": "group_message"}),
        )
        core.star = _module("astrbot.core.star")
        core.star.star_handler = _module(
            "astrbot.core.star.star_handler",
            EventType=type("EventType", (), {"OnDecoratingResultEvent": "on_decorating_result"}),
            star_handlers_registry=_HandlersRegistry(),
        )

    if "quart" not in sys.modules:
        class _Request:
            def __init__(self):
                self.args = {}

            async def get_json(self):
                return None

        _module("quart", request=_Request(), jsonify=lambda payload: payload)


class FakeContext:
    """最小 AstrBot 上下文桩：记录 Web API 路由，``get_config`` 安全降级。"""

    def __init__(self, config=None):
        self._config = config
        self.web_routes = []

    def register_web_api(self, path, handler, methods, description):
        self.web_routes.append((path, handler, methods, description))

    def get_config(self, **kwargs):
        return self._config


class FakeEvent:
    """满足 ``AstrMessageEvent`` 接口的最小事件桩。"""

    def __init__(self, unified_msg_origin="", message_str="", sender_id=""):
        self.unified_msg_origin = unified_msg_origin
        self.message_obj = types.SimpleNamespace(message_str=message_str)
        self._sender_id = sender_id

    def get_extra(self, key, default=False):
        return default

    def get_sender_id(self):
        return self._sender_id
