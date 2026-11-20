"""P0 回归：候选评选真的调 LLM；/calendar create 的闸门拿到后必须释放。"""

import asyncio
import datetime
import json

from time_awareness.services.adaptive_policy import AdaptiveConcurrencyGate
from time_awareness.services.daily_schedule_generation import DailyScheduleGeneration

VALID_JSON = json.dumps(
    [
        {"start": "00:00", "end": "06:00", "name": "睡觉", "state": "睡得很沉，翻身把被子裹紧。"},
        {"start": "06:00", "end": "08:00", "name": "起床", "state": "慢慢醒过来，先喝口水。"},
    ],
    ensure_ascii=False,
)
JUDGE_FENCED = '```json\n{"choice": 0, "reason": "更贴合人设"}\n```'


class _Resp:
    def __init__(self, text):
        self.completion_text = text
        self.role = "assistant"


class _Ctx:
    def __init__(self, text):
        self.text = text
        self.calls: list[dict] = []

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        return _Resp(self.text)


def _config():
    return {
        "runtime": {"llm_max_attempts": 2, "llm_max_concurrency": 1},
        "daily_schedule": {"ai_daily": {"enabled": True, "provider_id": "m"}},
    }


def test_judge_calls_llm_and_pins_provider_retries():
    """`_judge` 里曾引用未定义的 policy，被 except 吞掉 → 评选永远失败且无日志。"""

    async def scenario():
        ctx = _Ctx(JUDGE_FENCED)
        gen = DailyScheduleGeneration(
            context=ctx,
            config=_config(),
            time_context=type(
                "T", (), {"now": staticmethod(lambda: datetime.datetime(2026, 11, 20, 0, 0))}
            )(),
            persona_resolver=None,
        )
        choice, calls = await gen._judge(
            provider_id="m",
            valid=[{"slots": json.loads(VALID_JSON)}, {"slots": json.loads(VALID_JSON)}],
            prompt_hint="",
            max_concurrent_llm=1,
            max_attempts=2,
        )
        return ctx, gen, choice, calls

    ctx, gen, choice, calls = asyncio.run(scenario())
    assert choice == 0 and calls == 1
    assert ctx.calls[-1]["request_max_retries"] == 1  # provider 层不自己重试
    assert gen._gate._active == 0  # 槽位已归还


def test_calendar_generate_releases_gate(monkeypatch):
    """`/calendar create` 曾在 acquire 后未标记 held → 槽位永久泄漏（并发上限 1 时全插件卡死）。"""
    import sys
    import types

    from time_awareness.tests._shared import install_astrbot_stubs

    install_astrbot_stubs()
    from time_awareness.commands import calendar_commands as cc

    calls = []

    async def _fake_generate(context, **kwargs):
        calls.append(kwargs)
        return [{"month": 1, "day": 1, "text": "事项", "category": "custom"}]

    monkeypatch.setattr(cc, "generate_calendar_events", _fake_generate)

    class _Manager:
        def import_events(self, events, mode="replace"):
            return len(events)

    gate = AdaptiveConcurrencyGate(max_concurrent=1)
    cmds = cc.CalendarCommands(
        context=types.SimpleNamespace(),
        config={"calendar": {"ai_generate_worldview": "港区", "ai_generate_provider_id": "m"}},
        calendar_store=None,
        calendar_manager=_Manager(),
        builtin_manager=None,
        now_provider=lambda: datetime.datetime(2026, 11, 20, 0, 0),
        enabled_builtin_categories=lambda: [],
        concurrency_gate=gate,
    )

    async def scenario():
        out = [chunk async for chunk in cmds.generate("s")]
        return out

    out = asyncio.run(scenario())
    text = "\n".join(out)
    assert "已重新生成 1 条" in text
    assert gate._active == 0  # 关键：不能泄漏
    assert calls and calls[0]["provider_id"] == "m"


def test_calendar_generate_releases_gate_on_error(monkeypatch):
    import sys
    import types

    from time_awareness.tests._shared import install_astrbot_stubs

    install_astrbot_stubs()
    from time_awareness.commands import calendar_commands as cc

    async def _boom(context, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(cc, "generate_calendar_events", _boom)

    gate = AdaptiveConcurrencyGate(max_concurrent=1)
    cmds = cc.CalendarCommands(
        context=types.SimpleNamespace(),
        config={"calendar": {"ai_generate_worldview": "港区", "ai_generate_provider_id": "m"}},
        calendar_store=None,
        calendar_manager=None,
        builtin_manager=None,
        now_provider=lambda: datetime.datetime(2026, 11, 20, 0, 0),
        enabled_builtin_categories=lambda: [],
        concurrency_gate=gate,
    )

    async def scenario():
        try:
            return [chunk async for chunk in cmds.generate("s")]
        except RuntimeError:
            return None

    asyncio.run(scenario())
    assert gate._active == 0  # 异常路径也必须归还
