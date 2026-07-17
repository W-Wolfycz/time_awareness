"""使用真实 AstrBot runtime 运行的最小主动消息适配冒烟测试。

该文件不由普通 pytest 自动导入；在 AstrBot 自带 Python 中直接执行。
"""

import asyncio
from types import SimpleNamespace

from astrbot.api.message_components import Plain

from time_awareness.integrations.astrbot_sender import AstrBotMessageSender


class _Platform:
    def meta(self):
        return SimpleNamespace(id="demo", name="demo")


class _Context:
    def __init__(self):
        self.platform = _Platform()
        self.platform_manager = SimpleNamespace(platform_insts=[self.platform])

    def get_platform_inst(self, platform_id):
        return self.platform if platform_id == "demo" else None


async def main():
    sender = AstrBotMessageSender(_Context())
    result = await sender.decorate(
        "demo:GroupMessage:10001",
        [Plain("runtime smoke")],
    )
    assert len(result) == 1
    assert isinstance(result[0], Plain)
    assert result[0].text == "runtime smoke"


if __name__ == "__main__":
    asyncio.run(main())
    print("runtime sender smoke: ok")
