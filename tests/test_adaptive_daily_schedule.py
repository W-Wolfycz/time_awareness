"""自适应 AI 每日日程测试（2.0.0 落地验证）。"""

import asyncio
import datetime
import json
from types import SimpleNamespace

import pytest

from time_awareness.domain.schedule import ScheduleValidationError, schedule_similarity
from time_awareness.llm.daily_schedule_generator import (
    PlanningResult,
    deterministic_score,
    evaluate_boundary,
    parse_daily_schedule_json,
)
from time_awareness.services.adaptive_policy import (
    AdaptiveConcurrencyGate,
    AdaptiveGenerationPolicy,
)
from time_awareness.core.daily_schedule_store import DailyScheduleSnapshotStore
from time_awareness.services.daily_schedule_service import DailyScheduleService


NOW = datetime.datetime(2026, 8, 4, 7, 30, tzinfo=datetime.timezone.utc)
SESSION = "platform_demo:PrivateMessage:10001"

SLOTS_A = [
    {"name": "睡眠", "start": "00:00", "end": "06:00", "state": "沉睡中"},
    {"name": "晨起", "start": "06:00", "end": "09:00", "state": "刚醒，慵懒"},
    {"name": "入夜", "start": "20:00", "end": "24:00", "state": "放松，倦意渐浓"},
]
SLOTS_B = [
    {"name": "夜间值守", "start": "00:00", "end": "04:00", "state": "巡逻中"},
    {"name": "白昼活动", "start": "10:00", "end": "18:00", "state": "精神饱满"},
]


class FakeTimeContext:
    def __init__(self, now=NOW):
        self.current = now

    def now(self):
        return self.current

    def build_generation_sensor_snapshot(self, *, now=None):
        now = now or self.current
        return {"workday": {"enabled": True, "available": True,
                            "as_of": now.isoformat(), "value": "工作日"}}


class FakeContext:
    """按调用序返回响应的 Fake；不同 system_prompt 的调用分开计数。"""

    def __init__(self, plan_responses, judge_responses=None, provider_limit=0,
                 boundary_responses=None):
        self.plan_responses = list(plan_responses)
        self.judge_responses = list(judge_responses or [])
        self.boundary_responses = list(boundary_responses or [])
        self.plan_calls = []
        self.judge_calls = []
        self.boundary_calls = []
        self.provider_umos = []
        self.provider = SimpleNamespace(
            provider_config={"max_context_tokens": provider_limit},
            get_model=lambda: "model_demo",
        )

    def get_provider_by_id(self, provider_id):
        return self.provider if provider_id == "provider_demo" else None

    async def get_current_chat_provider_id(self, umo):
        self.provider_umos.append(umo)
        return "provider_demo"

    def get_config(self):
        return {"provider_settings": {"default_provider_id": "provider_demo"}}

    async def llm_generate(self, **kwargs):
        system_prompt = kwargs.get("system_prompt", "")
        if "角色状态评估器" in system_prompt:
            # Step1：边界评估调用（默认返回合法响应，测试可不关心内容）
            self.boundary_calls.append(kwargs)
            if self.boundary_responses:
                return SimpleNamespace(
                    role="assistant", completion_text=self.boundary_responses.pop(0)
                )
            return SimpleNamespace(
                role="assistant",
                completion_text='{"energy": 50, "sleep_debt": 10, "focus": 60, '
                                '"social_energy": 50, "motivation": 55, '
                                '"physical_state": "状态良好", "daily_theme": "常规日"}',
            )
        if "角色作息评审" in system_prompt:
            self.judge_calls.append(kwargs)
            if self.judge_responses:
                return SimpleNamespace(
                    role="assistant", completion_text=self.judge_responses.pop(0)
                )
            return SimpleNamespace(role="assistant", completion_text="not-json")
        self.plan_calls.append(kwargs)
        if self.plan_responses:
            return SimpleNamespace(
                role="assistant", completion_text=self.plan_responses.pop(0)
            )
        return SimpleNamespace(role="assistant", completion_text="[]")


class FakePersonaResolver:
    """可切换 persona 的解析器（resolved 可变以模拟会话切换）。"""

    def __init__(self, resolved="persona_demo"):
        self.resolved = resolved

    async def __call__(self, context, session, event=None):
        return self.resolved, {"prompt": "角色设定"}


def _adaptive_config(**overrides):
    config = {
        "daily_schedule": {
            "enable_schedule": True,
            "ai_daily": {
                "enabled": True,
                "generation_time": "00:05",
                "max_slots": 24,
                "retention_days": 30,
                "adaptive": {
                    "enabled": False,
                    "recent_days": 0,
                    "state_continuity_enabled": False,
                    "candidate_count": 1,
                    "candidate_selection": "heuristic",
                    "context_token_budget": 0,
                    "max_concurrent_llm": 1,
                    "acquire_timeout_seconds": 10,
                },
            },
            "schedule_templates": [],
            "ai_generation": {
                "provider_id": "",
                "use_persona": True,
                "worldview": "世界观",
            },
        }
    }
    for key, value in overrides.items():
        if key == "adaptive":
            config["daily_schedule"]["ai_daily"]["adaptive"].update(value)
        elif key == "ai_daily_extra":
            config["daily_schedule"]["ai_daily"].update(value)
        elif key == "ai_generation_extra":
            config["daily_schedule"]["ai_generation"].update(value)
        else:
            config["daily_schedule"][key] = value
    return config


def _service(tmp_path, config, context, resolver=None, sleep=None):
    store = DailyScheduleSnapshotStore(str(tmp_path))
    assert store.load()
    return (
        DailyScheduleService(
            context=context,
            config=config,
            store=store,
            time_context=FakeTimeContext(),
            persona_resolver=resolver or FakePersonaResolver(),
            sleep=sleep,
        ),
        store,
    )


async def _no_sleep(_delay):
    return None


# ==================== 配置策略 ====================


def test_policy_defaults_and_candidate_selection():
    policy = AdaptiveGenerationPolicy.from_config(_adaptive_config())
    assert not policy.enabled
    assert policy.effective_candidate_count == 1

    config = _adaptive_config(
        adaptive={
            "enabled": True,
            "candidate_count": 3,
            "candidate_selection": "llm",
        }
    )
    policy = AdaptiveGenerationPolicy.from_config(config)
    effective = policy.effective()
    assert effective.candidate_count == 3
    assert effective.candidate_selection == "llm"
    assert effective.effective_judge

    heuristic = AdaptiveGenerationPolicy.from_config(
        _adaptive_config(
            adaptive={
                "enabled": True,
                "candidate_count": 3,
                "candidate_selection": "heuristic",
            }
        )
    )
    assert not heuristic.effective_judge

    # ai_priority_over_static 与 max_attempts 不受 adaptive.enabled 控制
    config = _adaptive_config(
        ai_daily_extra={"ai_priority_over_static": True},
        ai_generation_extra={"max_attempts": 5},
    )
    policy = AdaptiveGenerationPolicy.from_config(config)
    assert not policy.enabled
    effective = policy.effective()
    assert effective.ai_priority_over_static is True
    assert effective.max_attempts == 5
    assert effective.candidate_count == 1  # 其余增强项仍被总开关关闭

    # 边界钳制
    config = _adaptive_config(
        adaptive={
            "recent_days": 99,
            "candidate_count": 99,
            "candidate_selection": "invalid",
            "context_token_budget": 2000000,
            "max_concurrent_llm": 0,
        }
    )
    policy = AdaptiveGenerationPolicy.from_config(config)
    assert policy.recent_days == 30
    assert policy.candidate_count == 5
    assert policy.candidate_selection == "heuristic"
    assert policy.context_token_budget == 1000000
    assert policy.max_concurrent_llm == 1


def test_policy_enhanced_flag():
    assert not AdaptiveGenerationPolicy.from_config(_adaptive_config()).enhanced
    config = _adaptive_config(adaptive={"enabled": True, "state_continuity_enabled": True})
    assert AdaptiveGenerationPolicy.from_config(config).enhanced


# ==================== 并发闸门 ====================


async def _gate_scenario():
    gate = AdaptiveConcurrencyGate(max_concurrent=1)
    assert await gate.acquire(0.05)
    assert not await gate.acquire(0.05)  # 超时失败
    await gate.release()
    assert await gate.acquire(0.3)
    await gate.release()


def test_gate_limits_concurrency_and_timeout():
    asyncio.run(_gate_scenario())


async def _gate_reconfigure():
    gate = AdaptiveConcurrencyGate(max_concurrent=1)
    await gate.reconfigure(2)
    assert await gate.acquire(0.3)
    assert await gate.acquire(0.3)
    await gate.release()
    await gate.release()


def test_gate_reconfigure_raises_capacity():
    asyncio.run(_gate_reconfigure())


async def _gate_timeout_does_not_leak_slot():
    """acquire 失败不得释放槽位（否则会误唤醒等待者）。"""
    gate = AdaptiveConcurrencyGate(max_concurrent=1)
    assert await gate.acquire(0.3)
    assert not await gate.acquire(0.0)  # 失败：不占槽，也不得误释放
    pending = gate.acquire(2.0)  # 等待中的第三个获取
    await asyncio.sleep(0.05)
    await gate.release()  # 释放真实槽位
    assert await pending  # 等待者被唤醒且拿到槽
    await gate.release()


def test_gate_timeout_does_not_leak_slot():
    asyncio.run(_gate_timeout_does_not_leak_slot())


# ==================== 协议解析 ====================


def test_parse_daily_schedule_json_array_protocol():
    text = json.dumps(SLOTS_A, ensure_ascii=False)
    slots = parse_daily_schedule_json(text, max_slots=24)
    assert len(slots) == 3


# ==================== 相似度 ====================


def test_similarity_identical_vs_different():
    assert schedule_similarity(SLOTS_A, SLOTS_A) >= 0.9
    low = schedule_similarity(SLOTS_A, SLOTS_B)
    assert low < 0.4


def test_deterministic_score_prefers_less_repetitive_candidate():
    recent = ({"slots": SLOTS_A},)
    assert deterministic_score(
        {"slots": SLOTS_B}, recent_snapshots=recent
    ) < deterministic_score({"slots": SLOTS_A}, recent_snapshots=recent)


def test_deterministic_score_ignores_coverage():
    """2.0.0 计划 §2.4：覆盖率不参与评分——空/稀疏与完整候选公平竞争。

    无参考日程/昨日状态时，空、稀疏与完整候选的评分完全相同
    （0.7 × 0.5 中性反重复 + 0.3 × 0.5 中性连续性）。
    """
    assert deterministic_score({"slots": SLOTS_A}) == 0.5
    assert deterministic_score({"slots": []}) == 0.5
    assert deterministic_score({"slots": SLOTS_B}) == 0.5


def test_deterministic_score_continuity_is_candidate_specific():
    """连续性以候选 slots 结构与边界匹配——同一 boundary 下不同候选分数不同。

    旧实现把共享的 Step1 当日边界冒充候选专属 ``boundary_state`` 并与自身
    比较，同轮候选连续性恒为常数；新实现候选只携带 ``slots``，评分用候选
    时段结构对边界匹配。同一 ``today_boundary`` 下，恢复型候选（低消耗）
    在高消耗冲突维度上远优于高强度候选。
    """
    boundary = {
        "energy": 20,
        "sleep_debt": 70,
        "focus": 25,
        "social_energy": 30,
        "motivation": 20,
        "physical_state": "身体疲惫",
        "daily_theme": "低能量恢复日",
        "unfinished_plans": [],
    }
    recovery = {
        "slots": [
            {"name": "睡眠", "start": "00:00", "end": "08:00", "state": "沉睡"},
            {"name": "休息", "start": "08:00", "end": "11:00", "state": "静养"},
            {"name": "独处", "start": "13:00", "end": "17:00", "state": "安静"},
        ]
    }
    demand = {
        "slots": [
            {"name": "训练", "start": "06:00", "end": "12:00", "state": "高强度"},
            {"name": "执勤", "start": "14:00", "end": "20:00", "state": "任务密集"},
        ]
    }
    # 无参考日程（反重复中性 0.5 相同）+ 同一边界 → 分数差异完全来自连续性
    assert deterministic_score(
        recovery, today_boundary=boundary
    ) < deterministic_score(demand, today_boundary=boundary)

    # Step1 缺失时回退前一日 boundary_state，仍是候选相关的
    prev_nested = {"boundary_state": boundary, "actual_schedules": [{"date": "2026-08-03", "slots": SLOTS_A}]}
    assert deterministic_score(
        recovery, previous_day_state=prev_nested, today_boundary=None
    ) < deterministic_score(demand, previous_day_state=prev_nested, today_boundary=None)


# ==================== 生成链路 ====================


def test_adaptive_disabled_is_baseline_single_call(tmp_path):
    config = _adaptive_config()
    context = FakeContext([json.dumps(SLOTS_A, ensure_ascii=False)])
    service, store = _service(tmp_path, config, context)

    result = asyncio.run(service.generate_for_session(SESSION, now=NOW))

    assert result.success and not result.reused
    assert len(context.plan_calls) == 1
    prompt = context.plan_calls[0]["prompt"]
    for block in ("PREVIOUS_DAY_STATE", "RECENT_SCHEDULES"):
        assert block not in prompt
    snapshot = store.get(store.persona_hash("persona_demo"), NOW.date(), "UTC")
    assert "theme" not in snapshot  # theme 已并入 boundary
    # 存储恒全维度：基线模式写入全 null 种子（9 键齐全）
    assert set(snapshot["boundary_state"]) == {
        "energy", "sleep_debt", "focus", "social_energy", "motivation",
        "physical_state", "daily_theme", "daily_style", "unfinished_plans",
    }
    assert all(v is None for v in snapshot["boundary_state"].values())


def test_state_continuity_and_theme_persisted(tmp_path):
    config = _adaptive_config(
        adaptive={
            "enabled": True,
            "state_continuity_enabled": True,
        }
    )
    # Step2 使用数组协议响应；boundary 由 Step1 单独提供
    plan_schedule = json.dumps(
        [
            {"name": "睡眠", "start": "00:00", "end": "06:30", "state": "仍在睡眠中"},
            {"name": "晨起", "start": "07:30", "end": "09:00", "state": "刚醒来不久"},
        ],
        ensure_ascii=False,
    )
    context = FakeContext([plan_schedule])
    service, store = _service(tmp_path, config, context)

    # 先给昨日写一份带日界状态的快照（分层：昨日已完整推进至日末）
    yesterday = NOW.date() - datetime.timedelta(days=1)
    persona_hash = store.persona_hash("persona_demo")
    store.save_ready(
        {
            "snapshot_id": "yesterday",
            "persona_hash": persona_hash,
            "local_date": yesterday.isoformat(),
            "timezone": "UTC",
            "status": "ready",
            "executed_until": "24:00",
            "executed_slots": [
                {**slot, "origin": "executed", "_source_origin": "ai"}
                for slot in SLOTS_A
            ],
            "user_slots": [],
            "ai_slots": [],
            "static_slots": [],
            "slots": SLOTS_A,
            "boundary_state": {"energy": 30, "focus": 20, "daily_theme": "恢复日"},
        },
        retention_days=30,
        today=NOW.date(),
    )

    # Step1：边界评估响应（当日初始日界状态，含 daily_theme）
    boundary_payload = json.dumps(
        {
            "energy": 48,
            "sleep_debt": 20,
            "focus": 60,
            "social_energy": 40,
            "motivation": 55,
            "physical_state": "状态良好",
            "daily_theme": "低能量恢复日",
            "unfinished_plans": ["整理资料"],
        },
        ensure_ascii=False,
    )
    context.boundary_responses.append(boundary_payload)
    result = asyncio.run(service.generate_for_session(SESSION, now=NOW))
    assert result.success
    # Step1 输入含昨日边界与连续实际日程
    boundary_prompt = context.boundary_calls[0]["prompt"]
    assert "PREVIOUS_BOUNDARY" in boundary_prompt
    assert "恢复日" in boundary_prompt  # 昨日 daily_theme 注入
    assert "ACTUAL_SCHEDULES" in boundary_prompt
    assert '"date": "2026-08-03"' in boundary_prompt
    # Step2 参考当日初始日界状态（TODAY_BOUNDARY），输出数组协议
    prompt = context.plan_calls[0]["prompt"]
    assert "TODAY_BOUNDARY" in prompt
    assert "低能量恢复日" in prompt  # Step1 产物（daily_theme）注入 Step2
    system_prompt = context.plan_calls[0]["system_prompt"]
    assert '"theme"' not in system_prompt
    assert "boundary_state" not in system_prompt

    snapshot = store.get(persona_hash, NOW.date(), "UTC")
    assert snapshot["boundary_state"]["energy"] == 48
    assert snapshot["boundary_state"]["unfinished_plans"] == ["整理资料"]
    assert snapshot["boundary_state"]["daily_theme"] == "低能量恢复日"


def test_recent_schedule_summary_honors_fourteen_day_range(tmp_path):
    config = _adaptive_config(
        adaptive={
            "enabled": True,
            "recent_days": 14,
        }
    )
    context = FakeContext([json.dumps(SLOTS_A, ensure_ascii=False)])
    service, store = _service(tmp_path, config, context)
    persona_hash = store.persona_hash("persona_demo")
    for offset in range(1, 15):
        day = NOW.date() - datetime.timedelta(days=offset)
        assert store.save_ready(
            {
                "snapshot_id": f"day-{offset}",
                "persona_hash": persona_hash,
                "local_date": day.isoformat(),
                "timezone": "UTC",
                "status": "ready",
                "slots": SLOTS_A,
            },
            retention_days=30,
            today=NOW.date(),
        )

    policy = AdaptiveGenerationPolicy.from_config(config).effective()
    text, snapshots = service._recent_schedules_text(persona_hash, policy, NOW)

    assert len(json.loads(text)) == 14
    assert len(snapshots) == 14


def test_recent_schedules_antirepeat_regenerates_with_budget(tmp_path):
    """最近日程相似度过高且还有预算 → 定向重生成一次。"""
    config = _adaptive_config(
        adaptive={
            "enabled": True,
            "recent_days": 3,
        }
    )
    yesterday = NOW.date() - datetime.timedelta(days=1)

    class _Ctx(FakeContext):
        def __init__(self):
            super().__init__([json.dumps(SLOTS_A), json.dumps(SLOTS_B)])
            self.first_prompt = None

        async def llm_generate(self, **kwargs):
            if not self.first_prompt:
                self.first_prompt = kwargs.get("prompt", "")
            return await super().llm_generate(**kwargs)

    context = _Ctx()
    service, store = _service(tmp_path, config, context)
    persona_hash = store.persona_hash("persona_demo")
    store.save_ready(
        {
            "snapshot_id": "yesterday",
            "persona_hash": persona_hash,
            "local_date": yesterday.isoformat(),
            "timezone": "UTC",
            "status": "ready",
            # 权威四层（动态渲染以四层重建时间线，不读 slots 缓存）
            "executed_until": "00:00",
            "executed_slots": [],
            "user_slots": [],
            "ai_slots": SLOTS_A,
            "static_slots": [],
            "slots": [],
        },
        retention_days=30,
        today=NOW.date(),
    )

    result = asyncio.run(service.generate_for_session(SESSION, now=NOW))
    assert result.success
    assert len(context.plan_calls) == 2  # 第一次重复 → 定向重生成
    assert "ANTI_REPEAT" in context.plan_calls[1]["prompt"]
    assert "RECENT_SCHEDULES" in context.plan_calls[0]["prompt"]
    # 第二次（不同）结果被采用；ai_slots 只保留 cutoff 之后的部分
    # （07:30 生成：SLOTS_B 的 00:00-04:00 已冻结为空，不回填）
    snapshot = store.get(persona_hash, NOW.date(), "UTC")
    assert snapshot["ai_slots"][0]["name"] == "白昼活动"
    assert snapshot["ai_slots"][0]["start"] == "10:00"


def test_multi_candidate_with_judge(tmp_path):
    config = _adaptive_config(
        adaptive={
            "enabled": True,
            "candidate_count": 2,
            "candidate_selection": "llm",
        }
    )
    context = FakeContext(
        [json.dumps(SLOTS_A, ensure_ascii=False), json.dumps(SLOTS_B, ensure_ascii=False)],
        judge_responses=['{"choice": 1, "reason": "更贴合人设"}'],
    )
    service, store = _service(tmp_path, config, context)

    result = asyncio.run(service.generate_for_session(SESSION, now=NOW))
    assert result.success
    assert len(context.plan_calls) == 2  # 2 候选
    assert len(context.judge_calls) == 1  # 1 裁判
    # judge 输入必须携带人设参考（评审维度 1 依赖）
    judge_kwargs = context.judge_calls[0]
    assert "角色设定" in str(judge_kwargs.get("prompt", ""))
    snapshot = store.get(store.persona_hash("persona_demo"), NOW.date(), "UTC")
    # 裁判选了 index 1（SLOTS_B）；裁剪后 00:00-04:00 冻结为空
    assert snapshot["ai_slots"][0]["name"] == "白昼活动"


def test_fill_boundary_empty_object_inherits_previous_state():
    """「全无变化」的空对象 {} 也必须继承上次角色状态，连续性不能断。"""
    from types import SimpleNamespace

    from time_awareness.services.daily_schedule_generation import DailyScheduleGeneration

    enhanced = SimpleNamespace(
        previous_day_state={
            "boundary_state": {
                "energy": 50, "sleep_debt": 10, "focus": 60,
                "social_energy": 40, "motivation": 55,
                "physical_state": "状态良好", "daily_theme": "常规日",
                "unfinished_plans": ["整理资料"],
            }
        }
    )
    filled = DailyScheduleGeneration._fill_boundary({}, enhanced)
    assert filled["energy"] == 50
    assert filled["sleep_debt"] == 10
    assert filled["daily_theme"] == "常规日"
    # 上次状态没有 daily_style 时回填为 null（新维度向后兼容）
    assert filled["daily_style"] is None
    # 无历史时 {} 回填为全 null 种子
    empty = DailyScheduleGeneration._fill_boundary({}, SimpleNamespace(previous_day_state={}))
    assert set(empty) == {
        "energy", "sleep_debt", "focus", "social_energy", "motivation",
        "physical_state", "daily_theme", "daily_style", "unfinished_plans",
    }
    assert empty["energy"] is None
    assert empty["daily_style"] is None


def test_multi_candidate_judge_failure_falls_back_deterministic(tmp_path):
    config = _adaptive_config(
        adaptive={
            "enabled": True,
            "candidate_count": 2,
            "candidate_selection": "llm",
        }
    )
    context = FakeContext(
        [json.dumps(SLOTS_A, ensure_ascii=False), json.dumps(SLOTS_B, ensure_ascii=False)],
        judge_responses=[],  # judge 返回非 JSON → -1
    )
    service, store = _service(tmp_path, config, context, sleep=_no_sleep)

    result = asyncio.run(service.generate_for_session(SESSION, now=NOW))
    assert result.success
    assert len(context.judge_calls) == 3  # 裁判重试 max_attempts 次后回退本地评分
    snapshot = store.get(store.persona_hash("persona_demo"), NOW.date(), "UTC")
    assert snapshot["ai_slots"]  # 仍有一个合法候选被采用


def test_heuristic_selection_uses_all_candidates_without_judge(tmp_path):
    config = _adaptive_config(
        adaptive={
            "enabled": True,
            "candidate_count": 3,
            "candidate_selection": "heuristic",
        }
    )
    context = FakeContext(
        [
            json.dumps(SLOTS_A, ensure_ascii=False),
            json.dumps(SLOTS_B, ensure_ascii=False),
            json.dumps(SLOTS_A, ensure_ascii=False),
        ]
    )
    service, _ = _service(tmp_path, config, context)

    result = asyncio.run(service.generate_for_session(SESSION, now=NOW))
    assert result.success
    assert len(context.plan_calls) == 3
    assert len(context.judge_calls) == 0


def test_concurrency_timeout_falls_back(tmp_path):
    """并发槽被占用且 acquire_timeout=0（非阻塞立即尝试）→ 基础规划失败可重试。"""
    config = _adaptive_config(adaptive={"enabled": True, "acquire_timeout_seconds": 0})
    context = FakeContext([json.dumps(SLOTS_A, ensure_ascii=False)])
    service, _ = _service(tmp_path, config, context, sleep=_no_sleep)
    gate = service.generation._gate
    assert asyncio.run(gate.acquire(0.1))  # 占住全局并发槽

    try:
        result = asyncio.run(service.generate_for_session(SESSION, now=NOW))
    finally:
        asyncio.run(gate.release())

    assert not result.success
    assert result.error_type == "concurrency_timeout"
    assert result.retryable


def test_nonblocking_acquire_succeeds_when_slot_free(tmp_path):
    """acquire_timeout=0 且槽位空闲：非阻塞立即成功（不被 wait_for(0) 误伤）。"""
    config = _adaptive_config(adaptive={"enabled": True, "acquire_timeout_seconds": 0})
    context = FakeContext([json.dumps(SLOTS_A, ensure_ascii=False)])
    service, store = _service(tmp_path, config, context)

    result = asyncio.run(service.generate_for_session(SESSION, now=NOW))
    assert result.success
    assert len(context.plan_calls) == 1


def test_adaptive_disabled_suppresses_residual_subswitches(tmp_path):
    """总开关关闭但子开关残留为 true：仍然走基线数组协议、一次调用。"""
    config = _adaptive_config(
        adaptive={
            "enabled": False,
            "use_daily_theme": True,
            "state_continuity_enabled": True,
            "recent_days": 3,
            "candidate_count": 3,
        }
    )
    context = FakeContext([json.dumps(SLOTS_A, ensure_ascii=False)])
    service, _ = _service(tmp_path, config, context)

    result = asyncio.run(service.generate_for_session(SESSION, now=NOW))
    assert result.success
    assert len(context.plan_calls) == 1
    prompt = context.plan_calls[0]["prompt"]
    system_prompt = context.plan_calls[0]["system_prompt"]
    for block in ("PREVIOUS_DAY_STATE", "RECENT_SCHEDULES"):
        assert block not in prompt
    assert "boundary_state" not in system_prompt  # 基础数组协议


def test_timezone_is_part_of_snapshot_key(tmp_path):
    """同一 Persona 同一天不同时区：各自独立快照，互不覆盖。"""
    context = FakeContext(
        [json.dumps(SLOTS_A, ensure_ascii=False), json.dumps(SLOTS_B, ensure_ascii=False)]
    )
    service, store = _service(tmp_path, _adaptive_config(), context)
    shanghai = NOW.astimezone(
        datetime.timezone(datetime.timedelta(hours=8), name="Asia/Shanghai")
    )

    first = asyncio.run(service.generate_for_session(SESSION, now=NOW))
    second = asyncio.run(service.generate_for_session(SESSION, now=shanghai))

    assert first.success and second.success
    assert not second.reused
    assert len(context.plan_calls) == 2
    assert store.get(store.persona_hash("persona_demo"), NOW.date(), "UTC") is not None
    assert (
        store.get(store.persona_hash("persona_demo"), NOW.date(), "Asia/Shanghai")
        is not None
    )


def test_calls_executed_counts_regeneration(tmp_path):
    """反重复重生成时 calls_executed 累加而不是被覆盖。

    开启状态连续性，让调用数 = Step1 边界评估 + 基础 + 定向重生成（3 次）；
    若只开反重复（连续性关闭）则 Step1 不执行，调用数会少一次（见
    P1-3 的独立语义测试）。
    """
    from time_awareness.services.adaptive_policy import EnhancedContext

    config = _adaptive_config(
        adaptive={
            "enabled": True,
            "recent_days": 3,
            "state_continuity_enabled": True,
        }
    )
    context = FakeContext(
        [json.dumps(SLOTS_A, ensure_ascii=False), json.dumps(SLOTS_B, ensure_ascii=False)]
    )
    service, store = _service(tmp_path, config, context)
    persona_hash = store.persona_hash("persona_demo")
    yesterday = NOW.date() - datetime.timedelta(days=1)
    snapshot = {
        "snapshot_id": "yesterday",
        "persona_hash": persona_hash,
        "local_date": yesterday.isoformat(),
        "timezone": "UTC",
        "status": "ready",
        "slots": SLOTS_A,
    }
    store.save_ready(snapshot, retention_days=30, today=NOW.date())
    enhanced = EnhancedContext(
        recent_schedules_text="[{\"date\":\"2026-08-03\",\"slots\":[]}]",
        recent_snapshots=(snapshot,),
    )

    async def _run():
        return await service.generation.generate(
            session=SESSION,
            now=NOW,
            enhanced=enhanced,
            persona={"prompt": "角色设定"},
        )

    generated = asyncio.run(_run())
    assert generated.calls_executed == 3  # Step1 边界评估 + 基础 + 定向重生成
    assert generated.regenerated_for_similarity


def test_context_token_budget_prioritizes_previous_day_state(tmp_path):
    """小 Token 预算优先保留昨日边界状态，最近日程可被丢弃。"""
    config = _adaptive_config(
        adaptive={
            "enabled": True,
            "recent_days": 3,
            "state_continuity_enabled": True,
            "context_token_budget": 120,
        }
    )
    context = FakeContext([json.dumps(SLOTS_A, ensure_ascii=False)])
    service, store = _service(tmp_path, config, context)
    persona_hash = store.persona_hash("persona_demo")
    yesterday = NOW.date() - datetime.timedelta(days=1)
    store.save_ready(
        {
            "snapshot_id": "yesterday",
            "persona_hash": persona_hash,
            "local_date": yesterday.isoformat(),
            "timezone": "UTC",
            "status": "ready",
            "executed_until": "24:00",
            "executed_slots": [
                {**slot, "origin": "executed", "_source_origin": "ai"}
                for slot in SLOTS_A
            ],
            "user_slots": [],
            "ai_slots": [],
            "static_slots": [],
            "slots": SLOTS_A,
            "boundary_state": {"energy": 30, "focus": 20, "daily_theme": "恢复日"},
        },
        retention_days=30,
        today=NOW.date(),
    )

    result = asyncio.run(service.generate_for_session(SESSION, now=NOW))
    assert result.success
    prompt = context.plan_calls[0]["prompt"]
    assert "TODAY_BOUNDARY" in prompt
    assert "RECENT_SCHEDULES" not in prompt


def test_auto_context_budget_uses_provider_limit_eighty_percent(tmp_path):
    config = _adaptive_config(adaptive={"enabled": True})
    context = FakeContext([json.dumps(SLOTS_A, ensure_ascii=False)], provider_limit=10000)
    service, _ = _service(tmp_path, config, context)
    policy = asyncio.run(service.generation.policy())
    sensors = service.time_context.build_generation_sensor_snapshot(now=NOW)

    budget = service.generation._automatic_enhanced_token_budget(
        provider_id="provider_demo",
        now=NOW,
        persona_prompt="角色设定",
        sensors=sensors,
        policy=policy,
    )

    assert budget is not None
    assert 0 < budget < 8000  # 80% 总输入预算扣除固定 Prompt 后的剩余值


def test_multi_candidate_partial_failure_uses_valid(tmp_path):
    """部分候选失败时使用剩余合法候选。"""
    config = _adaptive_config(adaptive={"enabled": True, "candidate_count": 2})
    context = FakeContext(
        [json.dumps(SLOTS_A, ensure_ascii=False), "not-json"]  # 第二个候选失败
    )
    service, store = _service(tmp_path, config, context, sleep=_no_sleep)

    result = asyncio.run(service.generate_for_session(SESSION, now=NOW))
    assert result.success
    snapshot = store.get(store.persona_hash("persona_demo"), NOW.date(), "UTC")
    # 合法候选 SLOTS_A 被采用；00:00-06:00 冻结为空，剩余从 07:30 起
    assert snapshot["ai_slots"][0]["name"] == "晨起"
    assert snapshot["ai_slots"][0]["start"] == "07:30"


def test_failure_record_carries_underlying_detail(tmp_path):
    """生成失败时失败记录保留底层异常全文，供部署端诊断。"""
    config = _adaptive_config()
    # 三次规划尝试全部失败（耗尽后 Fake 会返回合法空日程，必须喂满失败响应）
    context = FakeContext(["not-json", "not-json", "not-json"])
    service, store = _service(tmp_path, config, context, sleep=_no_sleep)

    result = asyncio.run(service.generate_for_session(SESSION, now=NOW))
    assert not result.success
    assert result.error_type == "invalid_output"
    failure = store.get_failure(store.persona_hash("persona_demo"), NOW.date(), "UTC")
    assert failure is not None
    assert "输出不是合法 JSON" in failure["detail"]


# ==================== 复检回归（重试 / 冻结竞态 / 预算丢弃 / 时区归一化） ====================


def test_retry_runs_three_attempts_without_call_budget(tmp_path):
    """移除调用上限后，2 候选失败会按固定策略执行三次 attempt。"""
    config = _adaptive_config(
        adaptive={
            "enabled": True,
            "candidate_count": 2,
        }
    )
    context = FakeContext(["not-json", "not-json", "not-json", "not-json",
                           "not-json", "not-json"])  # 全部失败
    service, _ = _service(tmp_path, config, context, sleep=_no_sleep)

    async def _run():
        await service.register_session_async(SESSION, trigger=False)
        persona_hash = service._identity_for(SESSION)
        now = service.time_context.now()
        from time_awareness.services.daily_schedule_service import FrozenIdentity

        task = asyncio.create_task(
            service._generate_with_retries(
                FrozenIdentity(
                    session=SESSION,
                    persona_hash=persona_hash,
                    persona={"prompt": "角色设定"},
                    timezone="UTC",
                    local_date=now.date(),
                    now=now,
                ),
                force=False,
            )
        )
        return await task

    result = asyncio.run(_run())
    assert not result.success
    assert len(context.plan_calls) == 6


def test_queued_generation_uses_global_default_provider(tmp_path):
    """2.0.0 计划 §4：Provider 只读配置/全局默认，不按 UMO 查询会话 Provider。"""
    config = _adaptive_config()
    context = FakeContext([json.dumps(SLOTS_A, ensure_ascii=False)])
    service, _ = _service(tmp_path, config, context, sleep=_no_sleep)

    async def _run():
        await service.register_session_async(SESSION, trigger=False)
        assert service.queue_generation(SESSION)
        await asyncio.gather(*list(service._tasks.values()))

    asyncio.run(_run())
    assert context.provider_umos == []
    assert context.plan_calls[0]["chat_provider_id"] == "provider_demo"


def test_retry_keeps_frozen_identity_after_switch(tmp_path):
    """排队后会话切换到另一 Persona：原任务仍用冻结身份写原 key。"""
    config = _adaptive_config()
    context = FakeContext([json.dumps(SLOTS_A, ensure_ascii=False)])
    resolver = FakePersonaResolver("persona_a")
    service, store = _service(tmp_path, config, context, resolver=resolver, sleep=_no_sleep)

    async def _run():
        # 排队（冻结 persona_a）
        await service.register_session_async(SESSION, trigger=False)
        persona_a = store.persona_hash("persona_a")
        # 手动构造冻结任务（模拟 queue_generation 冻结语义）
        now = service.time_context.now()
        from time_awareness.services.daily_schedule_service import FrozenIdentity
        frozen = FrozenIdentity(
            session=SESSION,
            persona_hash=persona_a,
            persona={"prompt": "角色设定"},
            timezone="UTC",
            local_date=now.date(),
            now=now,
        )
        task = asyncio.create_task(service._generate_with_retries(frozen, force=False))
        # 任务执行期间会话切换到 persona_b
        resolver.resolved = "persona_b"
        await service.register_session_async(SESSION, trigger=False)
        result = await task
        return result

    result = asyncio.run(_run())
    assert result.success
    # 冻结身份写入 persona_a 的 key
    assert store.get(store.persona_hash("persona_a"), NOW.date(), "UTC") is not None
    # persona_b 尚未生成（切换后才注册，未触发其生成）
    assert store.get(store.persona_hash("persona_b"), NOW.date(), "UTC") is None


def test_budget_dropped_recent_does_not_trigger_antirepeat(tmp_path):
    """最近日程块被预算丢弃后，不再拿隐藏快照触发反重复重生成。"""
    config = _adaptive_config(
        adaptive={
            "enabled": True,
            "recent_days": 3,
            "context_token_budget": 30,  # 小到装不下最近日程 JSON
        }
    )
    yesterday = NOW.date() - datetime.timedelta(days=1)
    context = FakeContext([json.dumps(SLOTS_A, ensure_ascii=False)])
    service, store = _service(tmp_path, config, context)
    persona_hash = store.persona_hash("persona_demo")
    store.save_ready(
        {
            "snapshot_id": "yesterday",
            "persona_hash": persona_hash,
            "local_date": yesterday.isoformat(),
            "timezone": "UTC",
            "status": "ready",
            "executed_until": "24:00",
            "executed_slots": [
                {**slot, "origin": "executed", "_source_origin": "ai"}
                for slot in SLOTS_A
            ],
            "user_slots": [],
            "ai_slots": [],
            "static_slots": [],
            "slots": SLOTS_A,  # 与今日生成结果完全相同 → 若无预算丢弃会触发重生成
        },
        retention_days=30,
        today=NOW.date(),
    )

    result = asyncio.run(service.generate_for_session(SESSION, now=NOW))
    assert result.success
    assert len(context.plan_calls) == 1  # 无重生成
    prompt = context.plan_calls[0]["prompt"]
    assert "RECENT_SCHEDULES" not in prompt


def test_timezone_key_normalizes_utc_aliases():
    """等价 UTC 表示归一化，避免同一时区产生重复快照 key。"""
    from zoneinfo import ZoneInfo

    from time_awareness.services.daily_schedule_service import DailyScheduleService

    utc_naive = datetime.datetime(2026, 8, 4, 7, 30, tzinfo=datetime.timezone.utc)
    utc_offset = datetime.datetime(
        2026, 8, 4, 7, 30, tzinfo=datetime.timezone(datetime.timedelta(0))
    )
    assert DailyScheduleService._timezone_key(utc_naive) == "UTC"
    assert DailyScheduleService._timezone_key(utc_offset) == "UTC"
    etc_utc = datetime.datetime(2026, 8, 4, 7, 30, tzinfo=ZoneInfo("Etc/UTC"))
    assert DailyScheduleService._timezone_key(etc_utc) == "UTC"
    shanghai = datetime.datetime(2026, 8, 4, 7, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert DailyScheduleService._timezone_key(shanghai) == "Asia/Shanghai"


# ==================== 空日程评分与相似度 ====================


def test_empty_candidate_scoring_and_similarity_safe():
    """相似度、候选评分对空日程不抛异常，且空日程不触发反重复。"""
    from time_awareness.domain.schedule import schedule_similarity
    from time_awareness.llm.daily_schedule_generator import deterministic_score

    assert schedule_similarity([], []) == 0.0
    assert schedule_similarity([], SLOTS_A) == 0.0
    score = deterministic_score({"slots": [], "boundary_state": {}}, recent_snapshots=())
    assert 0.0 <= score <= 1.0
    score_with_recent = deterministic_score(
        {"slots": [], "boundary_state": {}},
        recent_snapshots=(
            {"slots": SLOTS_A},
            {"slots": SLOTS_B},
        ),
    )
    assert 0.0 <= score_with_recent <= 1.0


def test_antirepeat_failure_keeps_original_valid_candidate(tmp_path):
    """反重复重生成最终失败时保留原合法候选（计划 §4.3）。"""
    config = _adaptive_config(
        adaptive={
            "enabled": True,
            "recent_days": 3,
        }
    )
    yesterday = NOW.date() - datetime.timedelta(days=1)
    context = FakeContext(
        [json.dumps(SLOTS_A, ensure_ascii=False),
         "not-json", "not-json", "not-json"]  # 反重复重生成 3 次尝试全失败
    )
    service, store = _service(tmp_path, config, context, sleep=_no_sleep)
    persona_hash = store.persona_hash("persona_demo")
    store.save_ready(
        {
            "snapshot_id": "yesterday",
            "persona_hash": persona_hash,
            "local_date": yesterday.isoformat(),
            "timezone": "UTC",
            "status": "ready",
            # 未推进到日末 + 权威四层数据（动态渲染入口以四层重建时间线）
            "executed_until": "00:00",
            "executed_slots": [],
            "user_slots": [],
            "ai_slots": SLOTS_A,
            "static_slots": [],
            "slots": [],
        },
        retention_days=30,
        today=NOW.date(),
    )

    result = asyncio.run(service.generate_for_session(SESSION, now=NOW))
    assert result.success
    # 1 次初始规划 + max_attempts(3) 次反重复尝试
    assert len(context.plan_calls) == 1 + 3
    assert "ANTI_REPEAT" in context.plan_calls[1]["prompt"]
    snapshot = store.get(persona_hash, NOW.date(), "UTC")
    # 原合法候选被保留（裁剪到 cutoff 后）
    assert snapshot["ai_slots"][0]["name"] == "晨起"
    assert snapshot["ai_slots"][0]["start"] == "07:30"


def test_antirepeat_failure_keeps_candidate_in_multi_candidate(tmp_path):
    """多候选路径反重复失败同样保留原合法候选。"""
    config = _adaptive_config(
        adaptive={
            "enabled": True,
            "recent_days": 3,
            "candidate_count": 2,
        }
    )
    yesterday = NOW.date() - datetime.timedelta(days=1)
    context = FakeContext(
        [json.dumps(SLOTS_A, ensure_ascii=False),
         json.dumps(SLOTS_A, ensure_ascii=False),  # 两个候选都与昨日重复
         "not-json", "not-json", "not-json"]  # 反重复重生成 3 次尝试全失败
    )
    service, store = _service(tmp_path, config, context, sleep=_no_sleep)
    persona_hash = store.persona_hash("persona_demo")
    store.save_ready(
        {
            "snapshot_id": "yesterday",
            "persona_hash": persona_hash,
            "local_date": yesterday.isoformat(),
            "timezone": "UTC",
            "status": "ready",
            # 未推进到日末 + 权威四层数据（动态渲染入口以四层重建时间线）
            "executed_until": "00:00",
            "executed_slots": [],
            "user_slots": [],
            "ai_slots": SLOTS_A,
            "static_slots": [],
            "slots": [],
        },
        retention_days=30,
        today=NOW.date(),
    )

    result = asyncio.run(service.generate_for_session(SESSION, now=NOW))
    assert result.success
    assert len(context.plan_calls) == 2 + 3  # 2 候选 + 3 次反重复尝试
    snapshot = store.get(persona_hash, NOW.date(), "UTC")
    assert snapshot["ai_slots"][0]["name"] == "晨起"


# ==================== 2.1.0 丰富度：池注入 / 最近主题反重复 / 结构反同构 ====================


def test_boundary_prompt_injects_pools_and_recent_themes(tmp_path):
    """主题/风格池与最近主题反重复注入 Step1；allow_custom 影响 instruction。"""
    config = _adaptive_config(
        adaptive={
            "enabled": True,
            "state_continuity_enabled": True,
            "recent_days": 3,
            "theme_pool": ["演习日", "远征日"],
            "style_pool": ["沉稳", "元气"],
            "allow_custom_theme": False,
        },
    )
    context = FakeContext([json.dumps(SLOTS_A, ensure_ascii=False)])
    service, store = _service(tmp_path, config, context, sleep=_no_sleep)

    # 昨日快照带 theme/style，作为 RECENT_THEMES 数据源
    yesterday = NOW.date() - datetime.timedelta(days=1)
    persona_hash = store.persona_hash("persona_demo")
    store.save_ready(
        {
            "snapshot_id": "yesterday",
            "persona_hash": persona_hash,
            "local_date": yesterday.isoformat(),
            "timezone": "UTC",
            "status": "ready",
            "executed_until": "24:00",
            "executed_slots": [],
            "user_slots": [],
            "ai_slots": [
                {"name": "睡眠", "start": "00:00", "end": "08:00", "state": "睡觉"},
            ],
            "static_slots": [],
            "slots": [
                {"name": "睡眠", "start": "00:00", "end": "08:00", "state": "睡觉"},
            ],
            "boundary_state": {
                "energy": 30,
                "daily_theme": "港区休整日",
                "daily_style": "慵懒",
            },
        },
        retention_days=30,
        today=NOW.date(),
    )

    result = asyncio.run(service.generate_for_session(SESSION, now=NOW))
    assert result.success
    boundary_prompt = context.boundary_calls[0]["prompt"]
    assert "THEME_POOL" in boundary_prompt
    assert "演习日" in boundary_prompt and "远征日" in boundary_prompt
    assert "不得自创" in boundary_prompt  # allow_custom_theme=False
    assert "STYLE_POOL" in boundary_prompt and "沉稳" in boundary_prompt
    assert "RECENT_THEMES" in boundary_prompt
    assert "港区休整日" in boundary_prompt and "慵懒" in boundary_prompt


def test_boundary_prompt_omits_pools_when_empty(tmp_path):
    """池为空列表时不注入对应块（清空=关闭维度）。"""
    config = _adaptive_config(
        adaptive={
            "enabled": True,
            "state_continuity_enabled": True,
            "theme_pool": [],
            "style_pool": [],
        },
    )
    context = FakeContext([json.dumps(SLOTS_A, ensure_ascii=False)])
    service, _ = _service(tmp_path, config, context, sleep=_no_sleep)

    result = asyncio.run(service.generate_for_session(SESSION, now=NOW))
    assert result.success
    boundary_prompt = context.boundary_calls[0]["prompt"]
    assert "THEME_POOL" not in boundary_prompt
    assert "STYLE_POOL" not in boundary_prompt
    assert "RECENT_THEMES" not in boundary_prompt  # recent_days=0 不注入


def test_structure_identical_triggers_antirepeat_regeneration(tmp_path):
    """结构签名相同（内容不同）的候选触发定向重生成，治「骨架复读」。"""
    same_structure = json.dumps(
        [
            {"name": "晨起", "start": "07:00", "end": "08:00", "state": "刚睡醒"},
            {"name": "早餐", "start": "08:00", "end": "09:00", "state": "正在吃饭"},
        ],
        ensure_ascii=False,
    )
    regenerated = json.dumps(
        [{"name": "外出", "start": "06:00", "end": "19:00", "state": "在外面"}],
        ensure_ascii=False,
    )
    config = _adaptive_config(adaptive={"enabled": True, "recent_days": 3})
    context = FakeContext([same_structure, regenerated])
    service, store = _service(tmp_path, config, context, sleep=_no_sleep)

    yesterday = NOW.date() - datetime.timedelta(days=1)
    persona_hash = store.persona_hash("persona_demo")
    store.save_ready(
        {
            "snapshot_id": "yesterday",
            "persona_hash": persona_hash,
            "local_date": yesterday.isoformat(),
            "timezone": "UTC",
            "status": "ready",
            "executed_until": "24:00",
            "executed_slots": [],
            "user_slots": [],
            "ai_slots": [
                {"name": "a", "start": "07:00", "end": "08:00", "state": "x"},
                {"name": "b", "start": "08:00", "end": "09:00", "state": "y"},
            ],
            "static_slots": [],
            "slots": [
                {"name": "a", "start": "07:00", "end": "08:00", "state": "x"},
                {"name": "b", "start": "08:00", "end": "09:00", "state": "y"},
            ],
            "boundary_state": {},
        },
        retention_days=30,
        today=NOW.date(),
    )

    result = asyncio.run(service.generate_for_session(SESSION, now=NOW))
    assert result.success
    assert len(context.plan_calls) == 2  # 第一次命中结构重复 → 定向重生成
    snapshot = store.get(persona_hash, NOW.date(), "UTC")
    assert snapshot["ai_slots"][0]["name"] == "外出"
