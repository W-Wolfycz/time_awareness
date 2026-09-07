"""最小的 ``astrbot.api`` 桩：logger / AstrBotConfig / message_components（幂等）。"""

import sys
import types


class _RecordingLogger:
    """记录调用级别的 logger，供 ``log._LoggerProxy`` 转发断言。"""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args))

        return record


astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
api = getattr(astrbot, "api", None)
if api is None:
    api = types.ModuleType("astrbot.api")
    astrbot.api = api
    sys.modules["astrbot.api"] = api

if not hasattr(api, "logger"):
    api.logger = _RecordingLogger()

if not hasattr(api, "AstrBotConfig"):
    class AstrBotConfig(dict):
        pass

    api.AstrBotConfig = AstrBotConfig

components = getattr(api, "message_components", None)
if components is None:
    components = types.ModuleType("astrbot.api.message_components")
    api.message_components = components
    sys.modules["astrbot.api.message_components"] = components

if not hasattr(components, "Plain"):
    class Plain:
        def __init__(self, text):
            self.text = text

    class At:
        def __init__(self, qq):
            self.qq = qq

    components.Plain = Plain
    components.At = At
