"""P0-2 隐私/身份防线：ReminderService 对混合 @ 目标 DueGroup 的 fail-closed。

正常上游（Scheduler 按 (session, target_user_id) 分组）已把不同 target_user_id 的
个人提醒隔离到不同 DueGroup；ReminderService 的检查是防御性 fail-closed，兜住任何
仍混入同一组的输入，避免不同用户的提醒进入同一 LLM prompt、ChatMemory 查询范围或
At 组件。混合 target 的组在产生任何 LLM/发送副作用前拒绝，并返回与现有"发送前失败"
一致的 retryable 语义（由 Scheduler 正确重试直至终态）。
"""

import asyncio
import datetime
from types import SimpleNamespace

from astrbot.api.message_components import At

from time_awareness.core.scheduler import (
    ReminderScheduler,
    STATUS_FAILED,
    STATUS_RETRY_WAIT,
    TriggerResult,
)
from time_awareness.integrations.chat_memory_history import ChatMemoryContextState
from time_awareness.services.reminder_service import ReminderService


UTC = datetime.timezone.utc
GROUP = "demo:GroupMessage:group_demo"
PRIVATE = "demo:FriendMessage:10001"


def _task(kind="user", *, target="", hint="演示提醒"):
    return SimpleNamespace(kind=kind, hint=hint, target_user_id=target)


class _Recorder:
    def __init__(self):
        self.llm_called = False
        self.llm_prompt = ""
        self.chat_memory_called = False
        self.chat_memory_kwargs = None
        self.send_called = False
        self.sent_components = None
        self.delivery_marked = False


class _Context:
    def __init__(self, recorder):
        self.recorder = recorder

    def get_config(self, umo=None):
        return {"provider_settings": {"datetime_system_prompt": False}}

    async def get_current_chat_provider_id(self, umo):
        return "provider_demo"

    async def llm_generate(self, **kwargs):
        self.recorder.llm_called = True
        self.recorder.llm_prompt = kwargs.get("prompt", "")
        return SimpleNamespace(role="assistant", completion_text="演示回复")


class _Sender:
    def __init__(self, recorder):
        self.recorder = recorder

    async def send_decorated(self, session, components, sender_id=""):
        self.recorder.send_called = True
        self.recorder.sent_components = list(components)
        return SimpleNamespace(success=True, error="")


class _Scheduler:
    def __init__(self, recorder):
        self.recorder = recorder

    def mark_delivery_started(self, tasks):
        self.recorder.delivery_marked = True
        return True


class _TimeContext:
    calendar_store = None

    def time_guidance_enabled(self):
        return False

    def last_chat_enabled(self):
        return False


def _build_service(monkeypatch, *, session, use_chat_memory_history=True):
    recorder = _Recorder()
    context = _Context(recorder)

    async def _resolve(*args, **kwargs):
        return ("", None)

    async def _load_memory(*args, **kwargs):
        recorder.chat_memory_called = True
        recorder.chat_memory_kwargs = dict(kwargs)
        return ChatMemoryContextState([])

    monkeypatch.setattr(
        "time_awareness.services.reminder_service.resolve_effective_persona", _resolve
    )
    monkeypatch.setattr(
        "time_awareness.services.reminder_service.load_chat_memory_context_state",
        _load_memory,
    )

    service = ReminderService(
        context=context,
        config={
            "reminder": {
                "reminder_targets": [session],
                "use_chat_memory_history": use_chat_memory_history,
                "reminder_provider_id": "provider_demo",
            }
        },
        scheduler=_Scheduler(recorder),
        time_context=_TimeContext(),
        message_sender=_Sender(recorder),
        last_chat_tracker=object(),
        astrbot_config_provider=lambda: None,
        ensure_builtin_fresh=lambda: None,
    )
    return service, recorder


def _deliver(service, session, tasks):
    return asyncio.run(
        service.deliver(session=session, tasks=tasks, is_late=False, late_minutes=0)
    )


def _ats(components):
    return [c.qq for c in components if isinstance(c, At)]


# ==================== 边界：_resolve_target_scope 单测 ====================


def test_target_scope_boundary():
    scope = ReminderService._resolve_target_scope
    # 同用户 homogeneous → 该用户
    assert (
        scope(
            GROUP,
            [_task("user", target="10001"), _task("followup", target="10001")],
        )
        == "10001"
    )
    # 全空 target homogeneous → 群公共（无 At）
    assert (
        scope(GROUP, [_task("user", target=""), _task("user", target="")]) == ""
    )
    # 只有 calendar → 群公共（无 At）
    assert scope(GROUP, [_task("calendar", target="")]) == ""
    # 不同用户 → mixed → None
    assert (
        scope(GROUP, [_task("user", target="10001"), _task("user", target="10002")])
        is None
    )
    # 空 target 与个人 target 混合 → mixed → None
    assert scope(GROUP, [_task("user", target=""), _task("user", target="10001")]) is None
    # 非群会话不参与 scope 判定（会话本身即身份）
    assert (
        scope(PRIVATE, [_task("user", target="10001"), _task("user", target="10002")])
        == ""
    )


# ==================== fail-closed：混合用户组 ====================


def test_mixed_personal_targets_fail_closed_before_any_side_effect(monkeypatch):
    service, rec = _build_service(monkeypatch, session=GROUP)
    tasks = [
        _task("user", target="10001", hint="甲的私人备忘"),
        _task("user", target="10002", hint="乙的私人备忘"),
    ]
    result = _deliver(service, GROUP, tasks)

    assert result.success is False
    assert result.retryable is True
    # 不产生任何 LLM / ChatMemory / 发送 / delivery 副作用
    assert rec.llm_called is False
    assert rec.chat_memory_called is False
    assert rec.send_called is False
    assert rec.delivery_marked is False
    # 失败信息不泄露真实账号
    assert "10001" not in result.error
    assert "10002" not in result.error


def test_followup_and_user_mixed_targets_also_fail_closed(monkeypatch):
    service, rec = _build_service(monkeypatch, session=GROUP)
    tasks = [
        _task("followup", target="10001"),
        _task("user", target="10002"),
    ]
    result = _deliver(service, GROUP, tasks)

    assert result.success is False
    assert result.retryable is True
    assert rec.llm_called is False
    assert rec.chat_memory_called is False
    assert rec.send_called is False
    assert rec.delivery_marked is False


def test_empty_plus_personal_target_is_mixed_and_fails_closed(monkeypatch):
    service, rec = _build_service(monkeypatch, session=GROUP)
    tasks = [
        _task("user", target="", hint="无法归因的备忘"),
        _task("user", target="10001", hint="甲的私人备忘"),
    ]
    result = _deliver(service, GROUP, tasks)

    assert result.success is False
    assert result.retryable is True
    assert rec.llm_called is False
    assert rec.chat_memory_called is False
    assert rec.send_called is False
    assert rec.delivery_marked is False


# ==================== homogeneous group：保持现有行为 ====================


def test_homogeneous_group_ats_correct_target_and_scopes_history(monkeypatch):
    service, rec = _build_service(monkeypatch, session=GROUP)
    tasks = [
        _task("user", target="10001", hint="甲"),
        _task("followup", target="10001", hint="乙"),
    ]
    result = _deliver(service, GROUP, tasks)

    assert result.success is True
    assert rec.llm_called is True
    # ChatMemory 查询范围 = 同一 target
    assert rec.chat_memory_called is True
    assert rec.chat_memory_kwargs["user_id"] == "10001"
    # 只 At 正确目标
    assert rec.send_called is True
    assert _ats(rec.sent_components) == ["10001"]


def test_homogeneous_empty_target_group_proceeds_without_at(monkeypatch):
    service, rec = _build_service(monkeypatch, session=GROUP)
    tasks = [
        _task("user", target=""),
        _task("followup", target=""),
    ]
    result = _deliver(service, GROUP, tasks)

    assert result.success is True
    assert rec.llm_called is True
    assert rec.chat_memory_called is True
    assert rec.chat_memory_kwargs["user_id"] == ""
    assert _ats(rec.sent_components) == []


def test_calendar_only_group_proceeds_without_at(monkeypatch):
    service, rec = _build_service(monkeypatch, session=GROUP)
    result = _deliver(service, GROUP, [_task("calendar", target="")])

    assert result.success is True
    assert rec.llm_called is True
    assert _ats(rec.sent_components) == []


def test_calendar_can_coexist_with_single_personal_target(monkeypatch):
    # calendar 不携带个人身份，不参与 scope 判定：单一 personal target 仍 homogeneous
    service, rec = _build_service(monkeypatch, session=GROUP)
    tasks = [
        _task("calendar", target=""),
        _task("user", target="10001", hint="甲"),
    ]
    result = _deliver(service, GROUP, tasks)

    assert result.success is True
    assert rec.llm_called is True
    assert _ats(rec.sent_components) == ["10001"]


def test_private_session_ignores_mixed_targets(monkeypatch):
    # 私聊会话本身即单一身份：target_user_id 不参与判定，也不构造 At
    service, rec = _build_service(monkeypatch, session=PRIVATE)
    tasks = [
        _task("user", target="10001"),
        _task("user", target="10002"),
    ]
    result = _deliver(service, PRIVATE, tasks)

    assert result.success is True
    assert _ats(rec.sent_components) == []
    assert rec.chat_memory_kwargs["user_id"] == ""


# ==================== Scheduler 集成：错误返回 → 正确重试/终态 ====================


def test_mixed_rejection_drives_scheduler_retry_then_terminal_failed(tmp_path):
    """fail-closed 返回 retryable=True 且未标记 delivery_started：
    Scheduler 先进入 retry_wait，超过最大尝试后进入终态 failed。
    """
    now = datetime.datetime(2026, 8, 11, 9, 0, tzinfo=UTC)
    scheduler = ReminderScheduler(str(tmp_path), lambda group: None, now_provider=lambda: now)
    scheduler.set_max_attempts(2)
    task = scheduler.add_user_task(
        GROUP,
        now - datetime.timedelta(seconds=1),
        "演示提醒",
    )

    group = scheduler._claim_due(now)[0]
    assert task.status == "processing"
    scheduler._finish_group(
        group,
        TriggerResult.fail("到期组含多个不同 @ 目标，fail-closed 拒绝", retryable=True),
        now,
    )
    assert task.status == STATUS_RETRY_WAIT
    assert task.delivery_started is False

    retry_now = now + datetime.timedelta(minutes=1)
    group2 = scheduler._claim_due(retry_now)[0]
    scheduler._finish_group(
        group2,
        TriggerResult.fail("到期组含多个不同 @ 目标，fail-closed 拒绝", retryable=True),
        retry_now,
    )
    assert task.status == STATUS_FAILED


# ==================== 跨层回归：真实 Scheduler 分组 + ReminderService 发送 ====================


def test_cross_layer_scheduler_groups_isolate_per_target_user(monkeypatch, tmp_path):
    """真实 Scheduler 按 (session, target_user_id) 分组 + ReminderService 发送隔离。

    同一群、同一时间到期的两条个人任务（target 10001/10002）被上游拆成两个
    DueGroup；分别交给 deliver 后，每次发送只 At 正确目标、LLM prompt 不含
    另一人的 hint、ChatMemory 查询范围与 At 一致。
    """
    now = datetime.datetime(2026, 8, 11, 9, 0, tzinfo=UTC)
    scheduler = ReminderScheduler(
        str(tmp_path),
        lambda group: None,
        now_provider=lambda: now,
    )
    scheduler.add_user_task(GROUP, now, "甲的私人备忘", target_user_id="10001")
    scheduler.add_user_task(GROUP, now, "乙的私人备忘", target_user_id="10002")

    groups = scheduler._claim_due(now)
    assert len(groups) == 2
    by_target = {}
    for group in groups:
        assert len(group.tasks) == 1
        task = group.tasks[0]
        assert task.target_user_id in {"10001", "10002"}
        assert group.target_user_id == task.target_user_id
        by_target[task.target_user_id] = group
    assert set(by_target) == {"10001", "10002"}

    hints = {"10001": "甲的私人备忘", "10002": "乙的私人备忘"}
    for uid, group in by_target.items():
        service, recorder = _build_service(monkeypatch, session=GROUP)
        result = asyncio.run(
            service.deliver(
                session=group.session,
                tasks=group.tasks,
                is_late=group.is_late,
                late_minutes=group.late_minutes,
            )
        )

        assert result.success is True
        # 只 At 正确目标
        assert _ats(recorder.sent_components) == [uid]
        # LLM prompt 只含本目标 hint，不含另一人的 hint
        assert hints[uid] in recorder.llm_prompt
        other = "乙的私人备忘" if uid == "10001" else "甲的私人备忘"
        assert other not in recorder.llm_prompt
        # ChatMemory 查询范围与 At 一致
        assert recorder.chat_memory_kwargs["user_id"] == uid

