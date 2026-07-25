"""在目标 AstrBot runtime 中验证主动提醒的时间注入。

使用假 Provider/发送器，不访问真实模型或平台；需用 AstrBot 自带 Python 直接执行。
"""

from __future__ import annotations

import asyncio
import datetime
import tempfile
from pathlib import Path

from time_awareness.core.calendar_store import CalendarStore
from time_awareness.core.last_chat_tracker import LastChatTracker
from time_awareness.services.reminder_service import ReminderService
from time_awareness.services.time_context_service import TimeContextService


class _Scheduler:
    def mark_delivery_started(self, tasks):
        return True


class _Sender:
    async def send_decorated(self, session, components):
        return type("Outcome", (), {"success": True, "error": ""})()


class _Context:
    def __init__(self):
        self.llm_kwargs = None

    def get_config(self, umo=None):
        return {
            "provider_settings": {
                "datetime_system_prompt": False,
            }
        }

    async def get_current_chat_provider_id(self, umo):
        return "provider_demo"

    async def llm_generate(self, **kwargs):
        self.llm_kwargs = kwargs
        return type(
            "Response",
            (),
            {"role": "assistant", "completion_text": "演示主动回复"},
        )()


async def main():
    context = _Context()
    fixed_now = datetime.datetime(
        2026,
        7,
        24,
        9,
        0,
        tzinfo=datetime.timezone(datetime.timedelta(hours=8), "CST"),
    )
    config = {
        "time_awareness": {
            "time_guidance_enabled": True,
            "workday_state_enabled": False,
            "lunar_state_enabled": False,
            "almanac_enabled": False,
            "last_chat_enabled": False,
        },
        "daily_schedule": {
            "enable_schedule": True,
            "schedule_templates": [
                {
                    "name": "晨间",
                    "start_time": "08:00",
                    "end_time": "10:00",
                    "state_prompt": "正在清醒地开始上午安排",
                }
            ],
        },
        "calendar": {"enable_calendar": False},
        "reminder": {
            "reminder_targets": ["demo:GroupMessage:group_demo"],
        },
    }

    with tempfile.TemporaryDirectory() as temp_dir:
        time_context = TimeContextService(
            config=config,
            astrbot_config_provider=lambda: {"timezone": "Asia/Shanghai"},
            calendar_store=CalendarStore(),
            last_chat_tracker=LastChatTracker(str(Path(temp_dir))),
            now_provider=lambda: fixed_now,
        )
        service = ReminderService(
            context=context,
            config=config,
            scheduler=_Scheduler(),
            time_context=time_context,
            message_sender=_Sender(),
            last_chat_tracker=object(),
            astrbot_config_provider=lambda: {"timezone": "Asia/Shanghai"},
            ensure_builtin_fresh=lambda: None,
        )
        task = type(
            "Task",
            (),
            {"kind": "user", "hint": "演示提醒", "target_user_id": ""},
        )()
        result = await service.deliver(
            session="demo:GroupMessage:group_demo",
            tasks=[task],
            is_late=False,
            late_minutes=0,
        )

    assert result.success is True
    assert context.llm_kwargs is not None
    assert (
        "<SYSTEM_REMINDER>当前日期时间：2026-07-24 09:00（CST），星期五</SYSTEM_REMINDER>"
    ) in context.llm_kwargs["prompt"]
    assert "<SCHEDULE_STATE>正在清醒地开始上午安排</SCHEDULE_STATE>" in context.llm_kwargs[
        "prompt"
    ]
    assert "<TIME_GUIDE>" in context.llm_kwargs["system_prompt"]
    assert "<SCHEDULE_STATE>正在清醒地开始上午安排</SCHEDULE_STATE>" not in context.llm_kwargs[
        "system_prompt"
    ]


if __name__ == "__main__":
    asyncio.run(main())
    print("runtime reminder time smoke: ok")
