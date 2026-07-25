import asyncio
import datetime

import pytest

from time_awareness.core.calendar_store import CalendarStore
from time_awareness.core.last_chat_tracker import LastChatTracker
from time_awareness.services.reminder_service import ReminderService
from time_awareness.services.time_context_service import TimeContextService


class _Scheduler:
    def __init__(self):
        self.created = []
        self.delivery_marked = False

    def add_user_task(self, **kwargs):
        self.created.append(kwargs)
        return type("Task", (), {"id": "task_demo_0001"})()

    def mark_delivery_started(self, tasks):
        self.delivery_marked = True
        return True


class _Context:
    def get_config(self, umo=None):
        return {"provider_settings": {"datetime_system_prompt": False}}

    async def get_current_chat_provider_id(self, umo):
        raise RuntimeError("provider unavailable")


class _SuccessContext:
    def __init__(self, *, datetime_system_prompt=False):
        self.kwargs = None
        self.datetime_system_prompt = datetime_system_prompt

    def get_config(self, umo=None):
        return {
            "provider_settings": {
                "datetime_system_prompt": self.datetime_system_prompt,
            }
        }

    async def get_current_chat_provider_id(self, umo):
        return "provider_demo"

    async def llm_generate(self, **kwargs):
        self.kwargs = kwargs
        return type(
            "Response",
            (),
            {"role": "assistant", "completion_text": "演示回复"},
        )()


class _FailedSender:
    async def send_decorated(self, session, components):
        return type("Outcome", (), {"success": False, "error": "send failed"})()


class _TimeContext:
    calendar_store = None

    def time_guidance_enabled(self):
        return False

    def last_chat_enabled(self):
        return False


def _service():
    scheduler = _Scheduler()
    service = ReminderService(
        context=_Context(),
        config={
            "reminder": {
                "reminder_targets": ["demo:GroupMessage:group_demo"],
            }
        },
        scheduler=scheduler,
        time_context=_TimeContext(),
        message_sender=object(),
        last_chat_tracker=object(),
        astrbot_config_provider=lambda: None,
        ensure_builtin_fresh=lambda: None,
    )
    service._astrbot_config_provider = lambda: None
    return service, scheduler


def test_external_task_validation_and_creation():
    now = datetime.datetime.now()
    service, scheduler = _service()
    task_id = service.create_external_task(
        session="demo:GroupMessage:group_demo",
        fire_at=now + datetime.timedelta(minutes=10),
        hint="演示提醒",
        source="test",
    )
    assert task_id == "task_demo_0001"
    assert scheduler.created[0]["hint"] == "演示提醒"

    with pytest.raises(ValueError):
        service.create_external_task(
            session="demo:GroupMessage:not_allowed",
            fire_at=now + datetime.timedelta(minutes=10),
            hint="演示提醒",
        )


def test_provider_failure_is_safe_to_retry_before_delivery():
    service, _ = _service()
    task = type(
        "Task",
        (),
        {"kind": "user", "hint": "演示提醒", "target_user_id": ""},
    )()
    result = asyncio.run(
        service.deliver(
            session="demo:GroupMessage:group_demo",
            tasks=[task],
            is_late=False,
            late_minutes=0,
        )
    )
    assert result.success is False
    assert result.retryable is True


def test_delivery_failure_is_not_safe_to_retry():
    service, scheduler = _service()
    service.context = _SuccessContext()
    service.message_sender = _FailedSender()
    task = type(
        "Task",
        (),
        {"kind": "user", "hint": "演示提醒", "target_user_id": ""},
    )()
    result = asyncio.run(
        service.deliver(
            session="demo:GroupMessage:group_demo",
            tasks=[task],
            is_late=False,
            late_minutes=0,
        )
    )
    assert scheduler.delivery_marked is True
    assert result.success is False
    assert result.retryable is False


def test_chat_memory_history_checkbox_passes_contexts_to_llm(monkeypatch):
    service, _ = _service()
    context = _SuccessContext()
    service.context = context
    service.config["reminder"]["use_chat_memory_history"] = True
    service.message_sender = _FailedSender()

    history_call = {}

    async def _history(*args, **kwargs):
        history_call["args"] = args
        history_call["kwargs"] = kwargs
        return [
            {"role": "user", "content": "历史问题"},
            {"role": "assistant", "content": "历史回答"},
        ]

    monkeypatch.setattr(
        "time_awareness.services.reminder_service.load_chat_memory_contexts",
        _history,
    )
    task = type(
        "Task",
        (),
        {"kind": "user", "hint": "演示提醒", "target_user_id": "10001"},
    )()
    asyncio.run(
        service.deliver(
            session="demo:GroupMessage:group_demo",
            tasks=[task],
            is_late=False,
            late_minutes=0,
        )
    )

    assert context.kwargs["contexts"] == [
        {"role": "user", "content": "历史问题"},
        {"role": "assistant", "content": "历史回答"},
    ]
    assert history_call["kwargs"] == {
        "persona_id": "",
        "user_id": "10001",
    }


def test_proactive_llm_gets_plugin_datetime_even_when_astrbot_setting_is_off(tmp_path):
    context = _SuccessContext(datetime_system_prompt=False)
    fixed_now = datetime.datetime(
        2026,
        7,
        24,
        9,
        0,
        tzinfo=datetime.timezone(datetime.timedelta(hours=8), "CST"),
    )
    now_calls = []
    time_config = {
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
    }

    def now_provider():
        now_calls.append(True)
        return fixed_now

    time_context = TimeContextService(
        config=time_config,
        astrbot_config_provider=lambda: {"timezone": "Asia/Shanghai"},
        calendar_store=CalendarStore(),
        last_chat_tracker=LastChatTracker(str(tmp_path)),
        now_provider=now_provider,
    )
    service = ReminderService(
        context=context,
        config={
            "reminder": {
                "reminder_targets": ["demo:GroupMessage:group_demo"],
            }
        },
        scheduler=_Scheduler(),
        time_context=time_context,
        message_sender=_FailedSender(),
        last_chat_tracker=object(),
        astrbot_config_provider=lambda: {"timezone": "Asia/Shanghai"},
        ensure_builtin_fresh=lambda: None,
    )
    task = type(
        "Task",
        (),
        {"kind": "user", "hint": "演示提醒", "target_user_id": ""},
    )()

    result = asyncio.run(
        service.deliver(
            session="demo:GroupMessage:group_demo",
            tasks=[task],
            is_late=False,
            late_minutes=0,
        )
    )

    assert result.success is False
    assert context.kwargs["system_prompt"].startswith("<TIME_GUIDE>")
    assert "正在清醒地开始上午安排" not in context.kwargs["system_prompt"]
    assert (
        "<SYSTEM_REMINDER>当前日期时间：2026-07-24 09:00（CST），星期五</SYSTEM_REMINDER>"
    ) in context.kwargs["prompt"]
    assert "<SCHEDULE_STATE>正在清醒地开始上午安排</SCHEDULE_STATE>" in context.kwargs[
        "prompt"
    ]
    assert "用空行分隔，每段将作为独立消息发出" not in context.kwargs["prompt"]
    assert len(now_calls) == 1
