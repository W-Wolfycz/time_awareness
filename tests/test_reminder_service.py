import asyncio
import datetime

import pytest

from time_awareness.services.reminder_service import ReminderService


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
    async def get_current_chat_provider_id(self, umo):
        raise RuntimeError("provider unavailable")


class _SuccessContext:
    def __init__(self):
        self.kwargs = None

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
