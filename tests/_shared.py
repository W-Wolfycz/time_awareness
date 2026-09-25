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

        core = _module("astrbot.core")
        core.agent = _module("astrbot.core.agent")
        core.agent.message = _module("astrbot.core.agent.message", TextPart=TextPart)

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

    def __init__(self, unified_msg_origin="", message_str=""):
        self.unified_msg_origin = unified_msg_origin
        self.message_obj = types.SimpleNamespace(message_str=message_str)

    def get_extra(self, key, default=False):
        return default
