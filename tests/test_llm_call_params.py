"""插件 LLM 调用的请求参数：重试次数按配置钉住；超时交给独立的 provider 条目。

AstrBot 的超时是 provider 级配置，插件不掺和——需要更长后台超时就在 AstrBot 里单独建
一个模型提供商条目（超时时间设长），再把 ai_daily.provider_id 指过去。
"""

import asyncio
import json

from time_awareness.llm.daily_schedule_generator import (
    request_kwargs,
    evaluate_boundary,
    generate_daily_schedule,
)
from time_awareness.services.persona_summary_service import PersonaSummaryService

VALID_JSON = json.dumps(
    [
        {"start": "00:00", "end": "06:00", "name": "睡觉", "state": "睡得很沉，翻身把被子裹紧。"},
        {"start": "06:00", "end": "08:00", "name": "起床", "state": "慢慢醒过来，先喝口水。"},
    ],
    ensure_ascii=False,
)
BOUNDARY_JSON = json.dumps({"energy": 70, "focus": 55}, ensure_ascii=False)


class _Resp:
    def __init__(self, text):
        self.completion_text = text
        self.role = "assistant"


class _Ctx:
    def __init__(self, text=VALID_JSON):
        self.text = text
        self.calls: list[dict] = []

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        return _Resp(self.text)


def test_request_kwargs_pins_retries_only_when_configured():
    # max_attempts>0：钉 request_max_retries=1，重试权归插件配置
    assert request_kwargs(2) == {"request_max_retries": 1}
    # max_attempts=0：不钉，交给 AstrBot 默认重试策略
    assert request_kwargs(0) == {}
    assert request_kwargs() == {}


def test_max_attempts_reaches_llm_generate():
    """配置值必须真的传到 llm_generate；漏传时行为与 0 完全一样，只有账单看得出来。"""

    async def scenario():
        ctx = _Ctx(VALID_JSON)
        await generate_daily_schedule(ctx, provider_id="m", prompt="p", max_attempts=2)
        pinned = ctx.calls[-1]

        ctx2 = _Ctx(BOUNDARY_JSON)
        await evaluate_boundary(ctx2, provider_id="m", prompt="p", max_attempts=3)
        eval_call = ctx2.calls[-1]

        ctx3 = _Ctx(VALID_JSON)
        await generate_daily_schedule(ctx3, provider_id="m", prompt="p")
        unpinned = ctx3.calls[-1]
        return pinned, eval_call, unpinned

    pinned, eval_call, unpinned = asyncio.run(scenario())
    assert pinned["request_max_retries"] == 1
    assert eval_call["request_max_retries"] == 1
    assert "request_max_retries" not in unpinned  # 默认 0 → 跟随 AstrBot


def test_extraction_zero_attempts_does_not_pin_retries():
    """萃取同样遵守 0 语义。"""

    async def scenario():
        ctx = _Ctx()
        service = PersonaSummaryService(
            context=ctx, config={"daily_schedule": {"ai_daily": {}}}, store=None
        )
        await service._call_llm("m", "人设", max_attempts=0)
        await service._call_llm("m", "人设", max_attempts=2)
        return ctx.calls

    zero, pinned = asyncio.run(scenario())
    assert "request_max_retries" not in zero
    assert pinned["request_max_retries"] == 1


def test_calendar_generation_retries_per_max_attempts():
    """日历创建也按 max_attempts 重试；失败次数不足时不得提前放弃。"""
    from time_awareness.llm.calendar_generator import generate_calendar_events

    class _FlakyCtx:
        def __init__(self, fail_times):
            self.calls = 0
            self.fail_times = fail_times

        async def llm_generate(self, **kwargs):
            self.calls += 1
            if self.calls <= self.fail_times:
                raise RuntimeError("provider blip")
            return _Resp(
                '```json\n[{"month": 1, "day": 1, "text": "开工", "repeat": 0}]\n```'
            )

    async def scenario():
        ctx = _FlakyCtx(fail_times=1)
        events = await generate_calendar_events(
            ctx,
            provider_id="m",
            user_prompt="港区",
            system_prompt="sys",
            current_year=2026,
            max_attempts=2,
        )
        return ctx, events

    ctx, events = asyncio.run(scenario())
    assert ctx.calls == 2  # 第一次失败、第二次成功
    assert events and events[0]["text"] == "开工"
