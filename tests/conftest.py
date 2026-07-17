"""为不依赖 AstrBot runtime 的纯逻辑测试提供最小 logger stub。"""

import sys
import types


class _Logger:
    def debug(self, *args, **kwargs):
        pass

    info = debug
    warning = debug
    error = debug
    critical = debug
    exception = debug


if "astrbot.api" not in sys.modules:
    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()
    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    astrbot.api = api
    sys.modules["astrbot.api"] = api

    components = types.ModuleType("astrbot.api.message_components")

    class Plain:
        def __init__(self, text):
            self.text = text

    class At:
        def __init__(self, qq):
            self.qq = qq

    components.Plain = Plain
    components.At = At
    sys.modules["astrbot.api.message_components"] = components
