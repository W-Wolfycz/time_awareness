"""P1 验证：AI 日程策略层——统一退避 / 多候选连续性数据流 / 全局并发配置优先级。

对应本轮拍板的三个 P1 目标：

1. 所有“重试语义”且会再次调用外部 LLM 的循环统一 0/5/30/封顶 30s 退避：
   Step1 边界评估、单候选规划、多候选每个候选、LLM judge、反重复重生成。
   通过可注入 recording sleep 断言顺序（0→5→30），不真实等待；已成功返回的
   候选、纯本地 fallback 与非重试路径不等待。

2. 连续性以候选「时段结构」与日界状态的匹配程度为输入：生产候选只携带
   ``slots``，不携带共享的 Step1 边界（不能把当日边界冒充候选专属字段后与
   自身比较，那会让同轮候选连续性恒为常数）。deterministic/heuristic 评分
   以候选 slots 对当日/前一日 boundary 各维度匹配，同一 boundary 下不同
   候选得到不同分数；LLM judge 显式拿到边界并按「候选↔边界」评判。
   测试从真实生成路径证明分数随候选匹配度变化，且 ``_plan_candidates``
   选出更匹配者。

3. 全局并发上限改为顶层 ``runtime.llm_max_concurrency``：新键优先，
   0/缺省回退旧 nested ``daily_schedule.ai_daily.adaptive.max_concurrent_llm``；
   自适应总开关关闭时全局闸门仍生效；``generation.policy()`` 会重配 gate。
"""

import asyncio
import datetime
import json
from types import SimpleNamespace

import pytest

from time_awareness.llm.daily_schedule_generator import deterministic_score, empty_boundary
from time_awareness.services.adaptive_policy import (
    AdaptiveConcurrencyGate,
    AdaptiveGenerationPolicy,
    EnhancedContext,
)
from time_awareness.services.daily_schedule_generation import (
    DailyScheduleGeneration,
    DailyScheduleGenerationError,
    _retry_delay_seconds,
)

import time_awareness.services.daily_schedule_generation as _gen_mod


NOW = datetime.datetime(2026, 8, 11, 7, 30, tzinfo=datetime.timezone.utc)
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

BOUNDARY_OK = {
    "energy": 50,
    "sleep_debt": 10,
    "focus": 60,
    "social_energy": 50,
    "motivation": 55,
    "physical_state": "状态良好",
    "daily_theme": "常规日",
}


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
    """按 system_prompt 区分 Step1/Step2/judge 的响应 Fake。

    可选 ``log`` 共享事件列表记录每次 LLM 调用（("llm", None)），配合
    ``RecordingSleep`` 断言退避顺序。
    """

    def __init__(self, plan_responses=None, judge_responses=None,
                 boundary_responses=None, log=None):
        self.plan_responses = list(plan_responses or [])
        self.judge_responses = list(judge_responses or [])
        self.boundary_responses = list(boundary_responses or [])
        self.plan_calls = []
        self.judge_calls = []
        self.boundary_calls = []
        self.log = log
        self.provider = SimpleNamespace(
            provider_config={"max_context_tokens": 0},
            get_model=lambda: "model_demo",
        )

    def get_provider_by_id(self, provider_id):
        return self.provider if provider_id == "provider_demo" else None

    def get_config(self):
        return {"provider_settings": {"default_provider_id": "provider_demo"}}

    async def llm_generate(self, **kwargs):
        if self.log is not None:
            self.log.append(("llm", None))
        system_prompt = kwargs.get("system_prompt", "")
        if "角色状态评估器" in system_prompt:
            self.boundary_calls.append(kwargs)
            if self.boundary_responses:
                text = self.boundary_responses.pop(0)
            else:
                text = json.dumps(BOUNDARY_OK, ensure_ascii=False)
            return SimpleNamespace(role="assistant", completion_text=text)
        if "角色作息评审" in system_prompt:
            self.judge_calls.append(kwargs)
            if self.judge_responses:
                text = self.judge_responses.pop(0)
            else:
                text = "not-json"
            return SimpleNamespace(role="assistant", completion_text=text)
        self.plan_calls.append(kwargs)
        if self.plan_responses:
            text = self.plan_responses.pop(0)
        else:
            text = "[]"
        return SimpleNamespace(role="assistant", completion_text=text)


class FakePersonaResolver:
    def __init__(self, resolved="persona_demo"):
        self.resolved = resolved

    async def __call__(self, context, session, event=None):
        return self.resolved, {"prompt": "角色设定"}


class RecordingSleep:
    def __init__(self, log):
        self._log = log

    async def __call__(self, delay):
        self._log.append(("sleep", float(delay)))


async def _no_sleep(_delay):
    return None


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
        elif key == "runtime":
            config["runtime"] = value
        else:
            config["daily_schedule"][key] = value
    return config


def _generation(config, context, *, sleep=None, resolver=None, gate=None):
    return DailyScheduleGeneration(
        context=context,
        config=config,
        time_context=FakeTimeContext(),
        persona_resolver=resolver or FakePersonaResolver(),
        concurrency_gate=gate,
        sleep=sleep,
    )


def _sleeps(log):
    return [delay for kind, delay in log if kind == "sleep"]


def _llm_count(log):
    return sum(1 for kind, _ in log if kind == "llm")


def _slot_names(slots):
    """归一化后的时段可能含内部 _start_minute/_end_minute，按 name 比较。"""
    return [s.get("name") for s in slots]


# ==================== P1-1：统一退避（0/5/30，封顶 30） ====================


def test_retry_delay_helper_sequence():
    """统一退避序列：第 0 次不等待，之后 5/30，超出封顶 30。"""
    assert _retry_delay_seconds(0) == 0.0
    assert _retry_delay_seconds(1) == 5.0
    assert _retry_delay_seconds(2) == 30.0
    assert _retry_delay_seconds(3) == 30.0
    assert _retry_delay_seconds(10) == 30.0


def test_boundary_retry_backoff_order():
    """Step1 边界评估重试：3 次失败按 0→5→30 退避，之后 Step2 正常继续。"""
    log = []
    config = _adaptive_config(adaptive={"enabled": True, "state_continuity_enabled": True})
    context = FakeContext(
        boundary_responses=["not-json", "not-json", "not-json"],
        plan_responses=[json.dumps(SLOTS_A, ensure_ascii=False)],
        log=log,
    )
    generation = _generation(config, context, sleep=RecordingSleep(log))
    result = asyncio.run(generation.generate(
        session=SESSION, now=NOW, persona={"prompt": "角色设定"}))
    assert len(context.boundary_calls) == 3
    assert len(context.plan_calls) == 1
    assert _llm_count(log) == 4
    # 边界降级 → 全 null 种子；退避 5→30（第 0 次不等待）
    assert result.boundary_state == empty_boundary()
    assert _sleeps(log) == [5.0, 30.0]


def test_single_candidate_planning_retry_backoff_order():
    """单候选（基线）规划重试：3 次失败按 0→5→30 退避后抛出。"""
    log = []
    config = _adaptive_config()  # 自适应关闭 → 基线单候选
    context = FakeContext(plan_responses=["not-json", "not-json", "not-json"],
                          log=log)
    generation = _generation(config, context, sleep=RecordingSleep(log))
    with pytest.raises(DailyScheduleGenerationError) as exc_info:
        asyncio.run(generation.generate(
            session=SESSION, now=NOW, persona={"prompt": "角色设定"}))
    assert exc_info.value.retryable is True
    assert len(context.plan_calls) == 3
    assert _sleeps(log) == [5.0, 30.0]
    # 自适应关闭无边界调用；总 LLM 调用 = 3 次规划
    assert _llm_count(log) == 3


def test_multi_candidate_per_candidate_retry_backoff_order():
    """多候选：仅失败的候选重试退避，成功候选不等待。"""
    log = []
    config = _adaptive_config(adaptive={"enabled": True, "candidate_count": 2,
                                         "state_continuity_enabled": True})
    context = FakeContext(
        plan_responses=[
            json.dumps(SLOTS_A, ensure_ascii=False),
            "not-json", "not-json", "not-json",  # 候选 1 三次失败
        ],
        log=log,
    )
    generation = _generation(config, context, sleep=RecordingSleep(log))
    result = asyncio.run(generation.generate(
        session=SESSION, now=NOW, persona={"prompt": "角色设定"}))
    # Step1 1 次 + 候选 0 成功 1 次 + 候选 1 失败 3 次
    assert len(context.boundary_calls) == 1
    assert len(context.plan_calls) == 4
    assert _llm_count(log) == 5
    # 只失败候选产生退避：5 → 30
    assert _sleeps(log) == [5.0, 30.0]
    # 合法候选被采用
    assert _slot_names(result.slots) == _slot_names(SLOTS_A)


def test_judge_retry_backoff_order():
    """LLM judge 重试：3 次失败按 0→5→30 退避后回退本地确定性评分。"""
    log = []
    config = _adaptive_config(adaptive={
        "enabled": True, "candidate_count": 2, "candidate_selection": "llm",
        "state_continuity_enabled": True,
    })
    context = FakeContext(
        plan_responses=[json.dumps(SLOTS_A, ensure_ascii=False),
                        json.dumps(SLOTS_B, ensure_ascii=False)],
        judge_responses=[],  # judge 全失败 → -1
        log=log,
    )
    generation = _generation(config, context, sleep=RecordingSleep(log))
    result = asyncio.run(generation.generate(
        session=SESSION, now=NOW, persona={"prompt": "角色设定"}))
    assert len(context.judge_calls) == 3
    assert _sleeps(log) == [5.0, 30.0]
    assert _slot_names(result.slots) in (
        _slot_names(SLOTS_A), _slot_names(SLOTS_B))


def test_antirepeat_regeneration_retry_backoff_order():
    """反重复重生成内部失败重试：0→5→30，保留原合法候选。"""
    log = []
    config = _adaptive_config(adaptive={"enabled": True, "recent_days": 3,
                                         "state_continuity_enabled": True})
    context = FakeContext(
        plan_responses=[
            json.dumps(SLOTS_A, ensure_ascii=False),  # 初始规划：与昨日重复
            "not-json", "not-json", "not-json",        # 反重复重生成 3 次失败
        ],
        log=log,
    )
    generation = _generation(config, context, sleep=RecordingSleep(log))
    enhanced = EnhancedContext(
        recent_schedules_text='[{"date": "2026-08-10", "slots": []}]',  # JSON 数组字符串
        recent_snapshots=({"slots": SLOTS_A},),
    )
    result = asyncio.run(generation.generate(
        session=SESSION, now=NOW, enhanced=enhanced,
        persona={"prompt": "角色设定"}))
    # 初始 1 次 + 反重复 3 次
    assert len(context.plan_calls) == 4
    assert _llm_count(log) == 5  # Step1 1 次 + 规划 4 次
    assert _sleeps(log) == [5.0, 30.0]
    # 反重复失败保留原合法候选（SLOTS_A）
    assert _slot_names(result.slots) == _slot_names(SLOTS_A)


def test_successful_paths_do_not_sleep():
    """非重试路径不等待：一次性成功（含多候选与 judge 成功）sleep 为空。"""
    log = []
    config = _adaptive_config(adaptive={
        "enabled": True, "candidate_count": 2, "candidate_selection": "llm",
        "state_continuity_enabled": True,
    })
    context = FakeContext(
        plan_responses=[json.dumps(SLOTS_A, ensure_ascii=False),
                        json.dumps(SLOTS_B, ensure_ascii=False)],
        judge_responses=['{"choice": 1, "reason": "ok"}'],
        log=log,
    )
    generation = _generation(config, context, sleep=RecordingSleep(log))
    result = asyncio.run(generation.generate(
        session=SESSION, now=NOW, persona={"prompt": "角色设定"}))
    assert _slot_names(result.slots) == _slot_names(SLOTS_B)
    assert _sleeps(log) == []


# ==================== P1-2：多候选状态连续性（候选 slots ↔ 日界状态） ====================
#
# 连续性必须以候选「时段结构」与边界状态的匹配程度为输入，而不是把共享的
# Step1 当日边界冒充候选专属字段后与自身比较（那会让同轮候选连续性恒为
# 常数）。生产候选只携带 slots；deterministic/LLM judge 都以候选 slots 对
# 当日/前一日边界匹配，因此同一 boundary 下不同候选得到不同分数。

# 恢复型日程：全部为低消耗/恢复时段 → 与「耗尽」边界高度匹配
RECOVERY_SLOTS = [
    {"name": "睡眠", "start": "00:00", "end": "08:00", "state": "沉睡中"},
    {"name": "休息", "start": "08:00", "end": "11:00", "state": "卧床休养"},
    {"name": "独处", "start": "13:00", "end": "17:00", "state": "安静静养"},
]
# 高强度日程：全部为高消耗/任务时段 → 与「耗尽」边界冲突
DEMAND_SLOTS = [
    {"name": "晨练", "start": "06:00", "end": "08:00", "state": "高强度训练"},
    {"name": "执勤", "start": "08:00", "end": "14:00", "state": "巡逻任务密集"},
    {"name": "任务", "start": "15:00", "end": "20:00", "state": "专注处理工作"},
]

DEPLETED_BOUNDARY = {
    "energy": 20,
    "sleep_debt": 70,
    "focus": 25,
    "social_energy": 30,
    "motivation": 20,
    "physical_state": "身体疲惫",
    "daily_theme": "低能量恢复日",
    "unfinished_plans": ["整理资料"],
}


def test_continuity_same_boundary_different_candidates_differ():
    """相同 boundary + 相同 recent_snapshots：匹配程度不同的候选分数不同。

    反重复参考（()）与边界（DEPLETED_BOUNDARY）都固定，两份日程只因与
    边界的匹配程度不同而分数不同——恢复型候选显著优于高强度候选。
    """
    recent = ()
    recovery_score = deterministic_score(
        {"slots": RECOVERY_SLOTS},
        recent_snapshots=recent,
        today_boundary=DEPLETED_BOUNDARY,
    )
    demand_score = deterministic_score(
        {"slots": DEMAND_SLOTS},
        recent_snapshots=recent,
        today_boundary=DEPLETED_BOUNDARY,
    )
    assert recovery_score < demand_score
    # 非恒定：分数确因候选 slots 与边界匹配度不同而变化
    assert recovery_score != demand_score


def test_continuity_same_boundary_same_recent_snapshots_differ():
    """相同 recent_snapshots + 相同 boundary：分数差异来自连续性而非反重复。"""
    recent = ({"slots": SLOTS_A},)  # 两份候选都离 SLOTS_A 足够远，反重复分量中性
    recovery_score = deterministic_score(
        {"slots": RECOVERY_SLOTS},
        recent_snapshots=recent,
        today_boundary=DEPLETED_BOUNDARY,
    )
    demand_score = deterministic_score(
        {"slots": DEMAND_SLOTS},
        recent_snapshots=recent,
        today_boundary=DEPLETED_BOUNDARY,
    )
    assert recovery_score < demand_score


def test_continuity_falls_back_to_previous_day_boundary():
    """Step1 当日边界缺失时，连续性回退前一日 boundary_state。"""
    prev = {
        "boundary_state": {
            "energy": 20, "sleep_debt": 70, "focus": 25,
            "social_energy": 30, "motivation": 20,
            "physical_state": "身体疲惫", "daily_theme": "低能量恢复日",
            "unfinished_plans": ["整理资料"],
        }
    }
    recovery_score = deterministic_score(
        {"slots": RECOVERY_SLOTS},
        recent_snapshots=(),
        previous_day_state=prev,
        today_boundary=None,
    )
    demand_score = deterministic_score(
        {"slots": DEMAND_SLOTS},
        recent_snapshots=(),
        previous_day_state=prev,
        today_boundary=None,
    )
    assert recovery_score < demand_score


def test_production_candidates_carry_no_boundary_and_heuristic_scores_by_slots(monkeypatch):
    """真实生成路径：候选只携带 slots（不带共享 boundary），评分用 slots 对边界匹配。

    旧实现把 Step1 共享 boundary 冒充候选专属字段并与自身比较，同轮候选
    连续性恒为常数；新实现候选不携带边界，确定性评分以候选 slots 的时段
    结构对当日边界匹配，同一 boundary 下不同候选分数不同。
    """
    captured = []
    real_score = _gen_mod.deterministic_score

    def recording_score(candidate, **kwargs):
        captured.append((dict(candidate), dict(kwargs)))
        return real_score(candidate, **kwargs)

    monkeypatch.setattr(_gen_mod, "deterministic_score", recording_score)

    boundary = dict(DEPLETED_BOUNDARY)
    config = _adaptive_config(adaptive={
        "enabled": True,
        "candidate_count": 2,
        "candidate_selection": "heuristic",
        "state_continuity_enabled": True,
    })
    context = FakeContext(
        plan_responses=[json.dumps(RECOVERY_SLOTS, ensure_ascii=False),
                        json.dumps(DEMAND_SLOTS, ensure_ascii=False)],
        boundary_responses=[json.dumps(boundary, ensure_ascii=False)],
    )
    generation = _generation(config, context)
    result = asyncio.run(generation.generate(
        session=SESSION, now=NOW,
        enhanced=EnhancedContext(),
        persona={"prompt": "角色设定"}))
    # 结果边界来自 Step1 真实输出
    assert result.boundary_state["energy"] == boundary["energy"]
    assert len(captured) == 2
    for cand, kwargs in captured:
        # 候选不再携带共享 boundary（旧实现的关键缺陷）
        assert "boundary_state" not in cand
        assert "slots" in cand
        assert kwargs["today_boundary"]["energy"] == boundary["energy"]
        assert kwargs["today_boundary"]["daily_theme"] == boundary["daily_theme"]
    # 真实候选对同一 boundary 评分 → 两个分数不同，恢复型更低
    scores = [deterministic_score(cand, today_boundary=boundary)
              for cand, _ in captured]
    assert len(set(scores)) == 2
    # heuristic 路径选出更匹配者
    assert _slot_names(result.slots) == _slot_names(RECOVERY_SLOTS)


def test_plan_candidates_selects_better_matching_candidate():
    """真实 _plan_candidates 在耗尽边界下选择恢复型候选而非高强度候选。"""
    config = _adaptive_config(adaptive={
        "enabled": True,
        "candidate_count": 2,
        "candidate_selection": "heuristic",
        "state_continuity_enabled": True,
    })
    context = FakeContext(
        plan_responses=[json.dumps(RECOVERY_SLOTS, ensure_ascii=False),
                        json.dumps(DEMAND_SLOTS, ensure_ascii=False)],
        boundary_responses=[json.dumps(DEPLETED_BOUNDARY, ensure_ascii=False)],
    )
    generation = _generation(config, context)
    result = asyncio.run(generation.generate(
        session=SESSION, now=NOW,
        enhanced=EnhancedContext(),
        persona={"prompt": "角色设定"}))
    assert _slot_names(result.slots) == _slot_names(RECOVERY_SLOTS)


def test_judge_prompt_contains_boundary_semantics():
    """LLM judge 必须显式拿到当日边界，并按「候选时段结构 ↔ 边界状态」评判。"""
    config = _adaptive_config(adaptive={
        "enabled": True,
        "candidate_count": 2,
        "candidate_selection": "llm",
        "state_continuity_enabled": True,
    })
    context = FakeContext(
        plan_responses=[json.dumps(RECOVERY_SLOTS, ensure_ascii=False),
                        json.dumps(DEMAND_SLOTS, ensure_ascii=False)],
        boundary_responses=[json.dumps(DEPLETED_BOUNDARY, ensure_ascii=False)],
        judge_responses=['{"choice": 0, "reason": "ok"}'],
    )
    generation = _generation(config, context)
    asyncio.run(generation.generate(
        session=SESSION, now=NOW,
        enhanced=EnhancedContext(),
        persona={"prompt": "角色设定"}))
    assert context.judge_calls, "judge 未被调用"
    prompt = context.judge_calls[0]["prompt"]
    # 当日边界显式进入 judge prompt
    assert "TODAY_BOUNDARY" in prompt
    assert "energy" in prompt and "低能量恢复日" in prompt
    # judge 被要求按候选↔边界评判
    assert "状态连续性" in prompt and "候选摘要自身不携带角色状态" in prompt
    # 候选摘要只含时段结构，不冒充携带边界
    assert "candidate_index" in prompt
    assert "boundary_state" not in prompt


# ==================== P1-2.5：state_continuity_enabled 语义 ====================
#
# ``state_continuity_enabled`` 是真正的消费开关：
# - False：不执行 Step1 边界 LLM 评估、不注入 TODAY_BOUNDARY、continuity
#   不影响 heuristic/judge，多候选只按反重复（或等价候选相关已启用分量）
#   排序，而不是给所有候选加常数后声称关闭；
# - True：才执行 slots↔boundary 连续性匹配与 judge 边界指令（0.7/0.3 权重）。


def test_continuity_disabled_skips_boundary_evaluation():
    """adaptive 开启 + continuity 关闭：不调用 evaluate_boundary，结果边界全 null。"""
    config = _adaptive_config(adaptive={
        "enabled": True,
        "candidate_count": 2,
        "candidate_selection": "heuristic",
        "state_continuity_enabled": False,
    })
    context = FakeContext(
        plan_responses=[json.dumps(RECOVERY_SLOTS, ensure_ascii=False),
                        json.dumps(DEMAND_SLOTS, ensure_ascii=False)],
    )
    generation = _generation(config, context, sleep=_no_sleep)
    result = asyncio.run(generation.generate(
        session=SESSION, now=NOW,
        enhanced=EnhancedContext(),
        persona={"prompt": "角色设定"}))
    assert context.boundary_calls == []  # Step1 不执行
    assert len(context.plan_calls) == 2
    assert result.calls_executed == 2  # 只有 2 次规划，无边界调用
    assert result.boundary_state == empty_boundary()  # 存储恒全维度：全 null 种子


def test_continuity_disabled_antirepeat_works_without_boundary():
    """只开反重复（recent_days，continuity 关闭）：不烧 Step1，反重复照常工作。"""
    config = _adaptive_config(adaptive={
        "enabled": True,
        "recent_days": 3,
        "state_continuity_enabled": False,
    })
    context = FakeContext(
        plan_responses=[json.dumps(SLOTS_A, ensure_ascii=False),
                        json.dumps(SLOTS_B, ensure_ascii=False)],
    )
    generation = _generation(config, context, sleep=_no_sleep)
    enhanced = EnhancedContext(
        recent_schedules_text='[{"date": "2026-08-10", "slots": []}]',
        recent_snapshots=({"slots": SLOTS_A},),
    )
    result = asyncio.run(generation.generate(
        session=SESSION, now=NOW, enhanced=enhanced,
        persona={"prompt": "角色设定"}))
    assert context.boundary_calls == []  # 反重复独立于 Step1
    assert len(context.plan_calls) == 2  # 基础 + 定向重生成
    assert result.regenerated_for_similarity
    assert result.calls_executed == 2


def test_deterministic_score_continuity_disabled_ignores_boundary():
    """continuity_enabled=False：边界不参与评分，注入边界与不注入分数完全一致。

    分数只含反重复分量，没有任何连续性常数项伪装。
    """
    from time_awareness.domain.schedule import schedule_similarity

    recent = ({"slots": SLOTS_A},)
    with_boundary = deterministic_score(
        {"slots": RECOVERY_SLOTS}, recent_snapshots=recent,
        today_boundary=DEPLETED_BOUNDARY, continuity_enabled=False)
    without_boundary = deterministic_score(
        {"slots": RECOVERY_SLOTS}, recent_snapshots=recent,
        today_boundary=None, continuity_enabled=False)
    assert with_boundary == without_boundary
    # 恰好等于纯反重复分量 0.7 × 平均结构相似度
    expected = round(
        0.7 * schedule_similarity(RECOVERY_SLOTS, SLOTS_A), 4)
    assert with_boundary == expected
    assert without_boundary == expected


def test_continuity_disabled_ranking_unaffected_by_boundary():
    """关闭连续性：注入不同 today_boundary 被压制，同候选排序与选择完全不变。

    排序只由反重复（0.7 分量）决定；反重复更优的高强度候选固定胜出，
    与注入的边界无关。
    """
    config = _adaptive_config(adaptive={
        "enabled": True,
        "candidate_count": 2,
        "candidate_selection": "heuristic",
        "state_continuity_enabled": False,
    })
    mixed_recent = ({"slots": [
        {"name": "睡眠", "start": "00:00", "end": "08:00", "state": "沉睡中"},
        {"name": "办公", "start": "09:00", "end": "13:00", "state": "处理邮件"},
    ]},)
    choices = []
    for boundary in (DEPLETED_BOUNDARY, BOUNDARY_OK):
        context = FakeContext(
            plan_responses=[json.dumps(RECOVERY_SLOTS, ensure_ascii=False),
                            json.dumps(DEMAND_SLOTS, ensure_ascii=False)],
        )
        generation = _generation(config, context, sleep=_no_sleep)
        result = asyncio.run(generation.generate(
            session=SESSION, now=NOW,
            enhanced=EnhancedContext(
                today_boundary=dict(boundary),
                recent_snapshots=mixed_recent,
            ),
            persona={"prompt": "角色设定"}))
        assert context.boundary_calls == []  # 边界被压制
        choices.append(tuple(_slot_names(result.slots)))
    # 两种不同边界 → 相同选择；且都是反重复更优的 DEMAND
    assert choices[0] == choices[1]
    assert choices[0] == tuple(_slot_names(DEMAND_SLOTS))


def test_continuity_enabled_invokes_boundary_and_changes_selection():
    """开启连续性：Step1 被调用，且边界改变候选选择（恢复型胜过反重复更优者）。"""
    config = _adaptive_config(adaptive={
        "enabled": True,
        "candidate_count": 2,
        "candidate_selection": "heuristic",
        "state_continuity_enabled": True,
    })
    mixed_recent = ({"slots": [
        {"name": "睡眠", "start": "00:00", "end": "08:00", "state": "沉睡中"},
        {"name": "办公", "start": "09:00", "end": "13:00", "state": "处理邮件"},
    ]},)
    context = FakeContext(
        plan_responses=[json.dumps(RECOVERY_SLOTS, ensure_ascii=False),
                        json.dumps(DEMAND_SLOTS, ensure_ascii=False)],
        boundary_responses=[json.dumps(DEPLETED_BOUNDARY, ensure_ascii=False)],
    )
    generation = _generation(config, context, sleep=_no_sleep)
    result = asyncio.run(generation.generate(
        session=SESSION, now=NOW,
        enhanced=EnhancedContext(recent_snapshots=mixed_recent),
        persona={"prompt": "角色设定"}))
    assert len(context.boundary_calls) == 1  # Step1 被执行
    # 反重复更优的是 DEMAND，但 0.3 连续性权重下恢复型候选胜出
    assert _slot_names(result.slots) == _slot_names(RECOVERY_SLOTS)


def test_continuity_disabled_judge_prompt_has_no_boundary():
    """关闭连续性：LLM judge prompt 不含 TODAY_BOUNDARY，也不追加边界评判指令。"""
    config = _adaptive_config(adaptive={
        "enabled": True,
        "candidate_count": 2,
        "candidate_selection": "llm",
        "state_continuity_enabled": False,
    })
    context = FakeContext(
        plan_responses=[json.dumps(RECOVERY_SLOTS, ensure_ascii=False),
                        json.dumps(DEMAND_SLOTS, ensure_ascii=False)],
        judge_responses=['{"choice": 1, "reason": "ok"}'],
    )
    generation = _generation(config, context, sleep=_no_sleep)
    asyncio.run(generation.generate(
        session=SESSION, now=NOW,
        enhanced=EnhancedContext(today_boundary=dict(DEPLETED_BOUNDARY)),
        persona={"prompt": "角色设定"}))
    assert context.judge_calls, "judge 未被调用"
    prompt = context.judge_calls[0]["prompt"]
    assert "TODAY_BOUNDARY" not in prompt
    assert "PREVIOUS_DAY_STATE" not in prompt
    assert "候选摘要自身不携带日界状态" not in prompt


# ==================== P1-3：全局并发配置优先级 ====================


def test_global_concurrency_new_key_prefers_runtime():
    """新键 runtime.llm_max_concurrency 优先于旧 nested 键。"""
    config = _adaptive_config(
        adaptive={"enabled": True, "max_concurrent_llm": 4},
        runtime={"llm_max_concurrency": 2},
    )
    policy = AdaptiveGenerationPolicy.from_config(config)
    assert policy.max_concurrent_llm == 2


def test_global_concurrency_zero_or_missing_falls_back_to_nested():
    """新键为 0 / 空 / 缺失时回退旧 nested 键（兼容迁移）。"""
    # 0 → 回退 nested 4
    policy = AdaptiveGenerationPolicy.from_config(_adaptive_config(
        adaptive={"max_concurrent_llm": 4},
        runtime={"llm_max_concurrency": 0},
    ))
    assert policy.max_concurrent_llm == 4
    # 空字符串 → 回退 nested 3
    policy = AdaptiveGenerationPolicy.from_config(_adaptive_config(
        adaptive={"max_concurrent_llm": 3},
        runtime={"llm_max_concurrency": ""},
    ))
    assert policy.max_concurrent_llm == 3
    # runtime 组整体缺失（旧配置文件）→ 回退 nested 4
    policy = AdaptiveGenerationPolicy.from_config(_adaptive_config(
        adaptive={"max_concurrent_llm": 4},
    ))
    assert policy.max_concurrent_llm == 4
    # 新键 0 且旧键缺失 → 默认 1
    policy = AdaptiveGenerationPolicy.from_config(_adaptive_config(
        runtime={"llm_max_concurrency": 0},
    ))
    assert policy.max_concurrent_llm == 1


def test_global_concurrency_clamped_to_range():
    policy = AdaptiveGenerationPolicy.from_config(_adaptive_config(
        runtime={"llm_max_concurrency": 99},
    ))
    assert policy.max_concurrent_llm == 4
    policy = AdaptiveGenerationPolicy.from_config(_adaptive_config(
        runtime={"llm_max_concurrency": -5},
    ))
    assert policy.max_concurrent_llm == 1


def test_global_concurrency_survives_adaptive_disabled():
    """自适应总开关关闭时全局并发上限仍生效（基线单候选也受约束）。"""
    config = _adaptive_config(
        adaptive={"enabled": False, "max_concurrent_llm": 1},
        runtime={"llm_max_concurrency": 3},
    )
    policy = AdaptiveGenerationPolicy.from_config(config)
    assert not policy.enabled
    effective = policy.effective()
    assert effective.max_concurrent_llm == 3
    assert effective.candidate_count == 1


def test_generation_policy_reconfigures_gate():
    """generation.policy() 后 gate.max_concurrent 与配置一致。"""
    gate = AdaptiveConcurrencyGate(1)

    config = _adaptive_config(
        adaptive={"enabled": True, "max_concurrent_llm": 4},
        runtime={"llm_max_concurrency": 2},
    )
    generation = _generation(config, FakeContext(), gate=gate)
    policy = asyncio.run(generation.policy())
    assert policy.max_concurrent_llm == 2
    assert gate.max_concurrent == 2

    # runtime=0 → 回退 nested=4 → gate 重配为 4
    config2 = _adaptive_config(
        adaptive={"enabled": True, "max_concurrent_llm": 4},
        runtime={"llm_max_concurrency": 0},
    )
    generation2 = _generation(config2, FakeContext(), gate=gate)
    policy2 = asyncio.run(generation2.policy())
    assert policy2.max_concurrent_llm == 4
    assert gate.max_concurrent == 4
