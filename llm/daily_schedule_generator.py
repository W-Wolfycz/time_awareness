"""AI 每日日程输出解析与严格校验（Step2 统一数组协议；未知键/XML/控制字符/越界状态一律拒绝）。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from ..constants import (
    DEFAULT_BOUNDARY_EVALUATION_SYSTEM_PROMPT,
    DEFAULT_DAILY_SCHEDULE_JUDGE_SYSTEM_PROMPT,
    DEFAULT_DAILY_SCHEDULE_SYSTEM_PROMPT,
)
from ..domain.schedule import (
    _CONTROL_CHARS,
    ScheduleValidationError,
    normalize_daily_slots,
    parse_time,
    schedule_similarity,
)
from ..log import logger, tag
from .json_response import json_block, parse_json_response


# 日界状态白名单键（有限范围、去身份化）
_BOUNDARY_STATE_NUMERIC_KEYS = {"energy", "sleep_debt", "focus", "social_energy", "motivation"}
_BOUNDARY_STATE_STRING_KEYS = {"physical_state", "daily_theme", "daily_style"}


def empty_boundary() -> dict:
    """全维度 null 种子：保证 boundary_state 恒为 9 个维度齐全。"""
    return {
        "energy": None,
        "sleep_debt": None,
        "focus": None,
        "social_energy": None,
        "motivation": None,
        "physical_state": None,
        "daily_theme": None,
        "daily_style": None,
        "unfinished_plans": None,
    }

# 情绪维度已交由好感插件管理，模型若输出则校验时明确拒绝而非静默接受。
_MOOD_ALIASES = {"mood", "心情", "情绪", "状态", "心境"}

# 已知别名归一化映射：LLM 用中文/同义词输出时归并到标准字段
_BOUNDARY_ALIASES = {
    "精力": "energy", "体力": "energy", "元气": "energy", "活力": "energy",
    "睡眠债": "sleep_debt", "欠觉": "sleep_debt", "缺觉": "sleep_debt",
    "专注": "focus", "专注度": "focus", "注意力": "focus",
    "社交电量": "social_energy", "社交能量": "social_energy", "交际余量": "social_energy",
    "动力": "motivation", "干劲": "motivation",
    "身体状态": "physical_state", "身体状况": "physical_state",
    "主题": "daily_theme", "基调": "daily_theme", "今日主题": "daily_theme",
    "风格": "daily_style", "状态色彩": "daily_style", "今日风格": "daily_style",
    "气质": "daily_style",
}


def _clean_text(value: str, *, field: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ScheduleValidationError(f"{field} 必须是字符串")
    text = " ".join(value.split()).strip()
    if not text:
        raise ScheduleValidationError(f"{field} 不能为空")
    if len(text) > max_length:
        raise ScheduleValidationError(f"{field} 超出长度上限")
    if _CONTROL_CHARS.search(text) or "<" in text or ">" in text:
        raise ScheduleValidationError(f"{field} 含控制字符或 XML 标签")
    return text


def _validate_slot(item: Any) -> dict:
    if not isinstance(item, dict):
        raise ScheduleValidationError("时段必须是对象")
    allowed_keys = {"name", "start", "end", "state"}
    if set(item) != allowed_keys:
        raise ScheduleValidationError("时段字段必须且只能包含 name/start/end/state")
    name = _clean_text(item["name"], field="name", max_length=24)
    state = _clean_text(item["state"], field="state", max_length=500)
    start_raw = item["start"]
    end_raw = item["end"]
    if not isinstance(start_raw, str) or not isinstance(end_raw, str):
        raise ScheduleValidationError("start/end 必须是字符串")
    return {
        "name": name,
        "start": start_raw.strip(),
        "end": end_raw.strip(),
        "state": state,
    }


def _validate_boundary_state(value: Any) -> dict:
    """校验日界状态；缺失/空/全省略返回 {}（9 维度，别名归一化，情绪维度拒绝）。"""
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise ScheduleValidationError("boundary_state 必须是对象")

    normalized: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str):
            raise ScheduleValidationError("boundary_state 字段名必须是字符串")
        key = raw_key.strip()
        if key in _MOOD_ALIASES:
            raise ScheduleValidationError(
                "boundary_state 的情绪维度（mood/心情/情绪等）已交由好感插件管理，请移除该字段"
            )
        key = _BOUNDARY_ALIASES.get(key, key)
        normalized[key] = raw_value

    allowed = _BOUNDARY_STATE_NUMERIC_KEYS | _BOUNDARY_STATE_STRING_KEYS | {
        "unfinished_plans"
    }
    if set(normalized) - allowed:
        unknown = ", ".join(sorted(set(normalized) - allowed))
        raise ScheduleValidationError(f"boundary_state 含未知字段：{unknown}")

    result: dict[str, Any] = {}
    for key in _BOUNDARY_STATE_NUMERIC_KEYS:
        if key not in normalized or normalized[key] is None:
            continue
        raw_number = normalized[key]
        if isinstance(raw_number, bool) or not isinstance(raw_number, (int, float)):
            raise ScheduleValidationError(f"boundary_state.{key} 必须是数值")
        number = float(raw_number)
        if not math.isfinite(number) or not 0.0 <= number <= 100.0:
            raise ScheduleValidationError(f"boundary_state.{key} 必须位于 0-100")
        result[key] = number
    for key, max_length in (
        ("physical_state", 12),
        ("daily_theme", 20),
        ("daily_style", 12),
    ):
        if key in normalized and normalized[key] not in (None, ""):
            result[key] = _clean_text(
                normalized[key],
                field=f"boundary_state.{key}",
                max_length=max_length,
            )
    if "unfinished_plans" in normalized and normalized["unfinished_plans"] is not None:
        plans = normalized["unfinished_plans"]
        if not isinstance(plans, list):
            raise ScheduleValidationError("unfinished_plans 必须是数组")
        if len(plans) > 8:
            raise ScheduleValidationError("unfinished_plans 超出 8 项上限")
        # 空数组是「显式清空」，不得与「键缺失」混同（否则昨日计划被错误继承）。
        cleaned = []
        for index, plan in enumerate(plans):
            cleaned.append(
                _clean_text(plan, field=f"unfinished_plans[{index}]", max_length=60)
            )
        result["unfinished_plans"] = cleaned
    return result


@dataclass(frozen=True)
class PlanningResult:
    """一次规划输出的解析结果（Step2 统一数组协议）。"""

    slots: list[dict]


def parse_daily_schedule_json(text: str) -> list[dict]:
    """解析基础数组协议输出；只接受协议字段、日内非跨午夜且互不重叠的时段。"""
    try:
        data = parse_json_response(text, require_closed_fence=True)
    except ValueError as exc:
        raise ScheduleValidationError("输出不是合法 JSON") from exc
    if not isinstance(data, list):
        raise ScheduleValidationError("顶层必须是数组")
    # 时段数量的「期望值」由提示词层 <DATE_CONTEXT>.target_slots 表达，此处只保留固定防失控安全上限。
    _safety_cap = 60
    if len(data) > _safety_cap:
        raise ScheduleValidationError(f"时段数量超出安全上限（{_safety_cap}）")

    slots = [_validate_slot(item) for item in data]
    return normalize_daily_slots(slots)


def _candidate_summary(candidate: dict, index: int) -> dict:
    """把单个候选压成结构化摘要（judge 输入；候选不携带角色状态，避免共享边界冒充候选数据）。"""
    slots = candidate.get("slots") or []
    summary = []
    for slot in slots:
        state = " ".join(str(slot.get("state", "")).split())
        summary.append(
            {
                "name": str(slot.get("name", "")),
                "start": str(slot.get("start", "")),
                "end": str(slot.get("end", "")),
                "state_fingerprint": state[:40],
            }
        )
    return {
        "candidate_index": index,
        "slots": summary,
    }


# 候选不携带日界状态（Step1 边界是候选共享参考）；文本维度关键词归一化匹配，数值维度只在耗尽区间启用粗粒度规则，缺失一律中性 0.5。

_RECOVERY_KEYWORDS = (
    "睡眠", "午休", "小憩", "休息", "放松", "独处", "静养", "冥想",
    "打盹", "歇", "疗养", "休整", "安静", "散步",
)
_DEMAND_KEYWORDS = (
    "训练", "执勤", "巡逻", "战斗", "任务", "工作", "会议", "学习", "作业",
    "加班", "冲刺", "演习", "值班", "运动", "健身", "跑步", "创作", "写作",
    "备课", "研究", "项目", "高强度", "忙碌", "专注",
)
_SOCIAL_KEYWORDS = (
    "社交", "聚会", "聊天", "团建", "会客", "拜访", "接待", "约会",
    "陪伴", "群聊", "见面", "出门",
)
_LOW_THEME_KEYWORDS = (
    "低能量", "恢复", "休息", "放松", "休整", "调养", "疗愈", "悠闲",
)
_HIGH_THEME_KEYWORDS = (
    "繁忙", "执勤", "任务", "高强度", "紧张", "冲刺", "专注", "战斗", "工作", "忙碌",
)
_PHYSICAL_STRESS_KEYWORDS = (
    "酸痛", "疲惫", "疲劳", "不适", "疼痛", "虚弱", "疲乏", "损伤", "发烧",
)
# 数值维度「耗尽」阈值：energy/focus/social_energy/motivation 低于此视为耗尽
_DEPLETED_LOW = 40.0
# sleep_debt 高于此视为高睡眠债
_SLEEP_DEBT_HIGH = 60.0


def _slot_minutes(slot: dict) -> tuple[int, int] | None:
    """取时段 ``[start_minute, end_minute)``；无法解析返回 None。"""
    start = slot.get("_start_minute")
    end = slot.get("_end_minute")
    if isinstance(start, int) and isinstance(end, int):
        return start, end
    try:
        start = parse_time(str(slot.get("start", "")), strict=False)
        end = parse_time(str(slot.get("end", "")), allow_24=True, strict=False)
    except ScheduleValidationError:
        return None
    return start, end


def _slot_demand(slot: dict) -> int:
    """粗粒度时段消耗分类：-1 恢复 / 0 中性 / +1 高消耗。"""
    text = f"{slot.get('name', '')} {slot.get('state', '')}"
    if any(keyword in text for keyword in _RECOVERY_KEYWORDS):
        return -1
    if any(keyword in text for keyword in _DEMAND_KEYWORDS):
        return 1
    return 0


def _aggregate_fraction(slots: list[dict], predicate) -> float:
    """按 predicate 命中的时段覆盖分钟数 / 全部覆盖分钟数（0..1）。"""
    total = 0.0
    matched = 0.0
    for slot in slots or []:
        minutes = _slot_minutes(slot) if isinstance(slot, dict) else None
        if not minutes:
            continue
        start, end = minutes
        length = max(0, end - start)
        if length <= 0:
            continue
        total += length
        if predicate(slot):
            matched += length
    return matched / total if total > 0 else 0.0


def _aggregate_demand_fraction(slots: list[dict]) -> float:
    """高消耗时段覆盖分钟数 / 全部覆盖分钟数（0..1）。"""
    return _aggregate_fraction(slots, lambda slot: _slot_demand(slot) == 1)


def _aggregate_social_fraction(slots: list[dict]) -> float:
    """社交类时段覆盖分钟数 / 全部覆盖分钟数（0..1）。"""
    return _aggregate_fraction(
        slots,
        lambda slot: any(
            keyword in f"{slot.get('name', '')} {slot.get('state', '')}"
            for keyword in _SOCIAL_KEYWORDS
        ),
    )


def _plan_covered(plan: str, slots: list[dict]) -> bool:
    """未完成计划是否在候选日程中被显式安排（完整或 ≥2 字连续片段）。"""
    normalized = " ".join(str(plan or "").split()).strip()
    if not normalized:
        return False
    text = " ".join(
        f"{slot.get('name', '')} {slot.get('state', '')}"
        for slot in slots or []
    )
    if normalized in text:
        return True
    if len(normalized) >= 2:
        for index in range(len(normalized) - 1):
            if normalized[index:index + 2] in text:
                return True
    return False


def _resolve_boundary_reference(
    *,
    today_boundary: dict | None,
    previous_day_state: dict | None,
) -> dict:
    """选择连续性参考日界状态：优先当日 Step1 边界，全 null 时回退前一日 boundary_state。"""
    if isinstance(today_boundary, dict) and any(
        value is not None for value in today_boundary.values()
    ):
        return today_boundary
    if isinstance(previous_day_state, dict):
        previous = previous_day_state.get("boundary_state", previous_day_state)
        if isinstance(previous, dict) and any(
            value is not None for value in previous.values()
        ):
            return previous
    return {}


def _boundary_continuity_penalty(slots: list[dict], boundary: dict) -> float:
    """候选日程 ↔ 日界状态的连续性惩罚（0..1，8 维度等权平均，越低越连续）。"""
    if not isinstance(boundary, dict) or not boundary:
        return 0.5

    demand = _aggregate_demand_fraction(slots)
    social = _aggregate_social_fraction(slots)

    penalties: list[float] = []
    for key, inverted in (
        ("energy", False),
        ("sleep_debt", True),
        ("focus", False),
        ("social_energy", False),
        ("motivation", False),
    ):
        value = boundary.get(key)
        if value is None:
            penalties.append(0.5)
            continue
        depleted = (
            value >= _SLEEP_DEBT_HIGH if inverted else value <= _DEPLETED_LOW
        )
        penalties.append(social if (depleted and key == "social_energy") else (demand if depleted else 0.5))

    theme = str(boundary.get("daily_theme") or "").strip()
    if theme and any(keyword in theme for keyword in _LOW_THEME_KEYWORDS):
        theme_conflict = demand
    elif theme and any(keyword in theme for keyword in _HIGH_THEME_KEYWORDS):
        theme_conflict = 1.0 - demand
    else:
        theme_conflict = 0.5

    physical = str(boundary.get("physical_state") or "").strip()
    if physical and any(
        keyword in physical for keyword in _PHYSICAL_STRESS_KEYWORDS
    ):
        physical_conflict = demand
    else:
        physical_conflict = 0.5

    unfinished = boundary.get("unfinished_plans")
    if isinstance(unfinished, list) and unfinished:
        covered = sum(1 for plan in unfinished if _plan_covered(plan, slots))
        unfinished_conflict = 1.0 - (covered / len(unfinished))
    else:
        unfinished_conflict = 0.5

    penalties.extend((theme_conflict, physical_conflict, unfinished_conflict))
    return sum(penalties) / len(penalties)


def deterministic_score(
    candidate: dict,
    *,
    recent_snapshots: tuple[dict, ...] = (),
    previous_day_state: dict | None = None,
    today_boundary: dict | None = None,
    continuity_enabled: bool = True,
) -> float:
    """无 LLM 的候选评分（分数越低越好）：反重复 0.7 + 状态连续性 0.3；continuity_enabled=False 时只返回反重复分量。"""
    slots = candidate.get("slots") or []
    score = 0.0

    if recent_snapshots:
        avg_similarity = sum(
            schedule_similarity(slots, snapshot.get("slots") or [])
            for snapshot in recent_snapshots
        ) / max(1, len(recent_snapshots))
        score += 0.7 * avg_similarity
    else:
        # 无参考日程时的中性基准，避免评分完全由连续性决定
        score += 0.7 * 0.5

    if not continuity_enabled:
        # 状态连续性关闭：不执行 slots↔boundary 匹配，也不加 0.3×中性常数项。
        return round(score, 4)

    boundary = _resolve_boundary_reference(
        today_boundary=today_boundary,
        previous_day_state=previous_day_state,
    )
    score += 0.3 * _boundary_continuity_penalty(slots, boundary)

    return round(score, 4)


async def generate_daily_schedule(
    context: Any,
    *,
    provider_id: str,
    prompt: str,
) -> tuple[PlanningResult, str]:
    """调用 Provider 并返回已经过数组协议校验的规划结果和原始文本。"""
    response = await context.llm_generate(
        chat_provider_id=provider_id,
        prompt=prompt,
        system_prompt=DEFAULT_DAILY_SCHEDULE_SYSTEM_PROMPT,
    )
    if not response or getattr(response, "role", "") != "assistant":
        raise ScheduleValidationError("LLM 响应角色异常")
    raw_text = (getattr(response, "completion_text", "") or "").strip()
    if not raw_text:
        raise ScheduleValidationError("LLM 返回空内容")
    return (
        PlanningResult(slots=parse_daily_schedule_json(raw_text)),
        raw_text,
    )


async def evaluate_boundary(
    context: Any,
    *,
    provider_id: str,
    prompt: str,
) -> dict:
    """Step1：评估当日初始日界状态（8 维度，别名归一化，稀疏输出）。"""
    response = await context.llm_generate(
        chat_provider_id=provider_id,
        prompt=prompt,
        system_prompt=DEFAULT_BOUNDARY_EVALUATION_SYSTEM_PROMPT,
    )
    if not response or getattr(response, "role", "") != "assistant":
        raise ScheduleValidationError("LLM 响应角色异常")
    raw_text = (getattr(response, "completion_text", "") or "").strip()
    if not raw_text:
        raise ScheduleValidationError("LLM 返回空内容")
    try:
        data = parse_json_response(raw_text, require_closed_fence=True)
    except ValueError as exc:
        raise ScheduleValidationError("边界评估输出不是合法 JSON") from exc
    return _validate_boundary_state(data)


# judge 必须按「候选时段结构 ↔ 角色状态」评判连续性；候选摘要不携带边界。
_JUDGE_BOUNDARY_INSTRUCTION = (
    "评审「状态连续性」时必须以上文输入的角色状态（TODAY_BOUNDARY，含 "
    "PREVIOUS_DAY_STATE 参考）为准，对照每个候选的时段结构逐条判断："
    "低能量/高睡眠债/低专注/低动力/身体不适/低社交电量的当日，高强度、"
    "高消耗或密集社交时段应视为连续性差；低能量恢复主题的当日应更多安排"
    "休息恢复时段。候选摘要自身不携带角色状态，不能凭名称猜边界。"
)


async def select_candidate_with_llm(
    context: Any,
    *,
    provider_id: str,
    candidates: list[dict],
    prompt_hint: str,
    persona_prompt: str = "",
    recent_schedules_text: str = "",
    continuity_enabled: bool = True,
) -> int:
    """使用 LLM 在候选中选择最自然且不重复的一个，返回其下标；失败返回 -1。"""
    if len(candidates) < 2:
        return 0

    summaries = [
        _candidate_summary(candidate, index)
        for index, candidate in enumerate(candidates)
    ]
    judge_parts = []
    if str(persona_prompt or "").strip():
        judge_parts.append(
            json_block("PERSONA", {"content": str(persona_prompt).strip()})
        )
    judge_parts.append(prompt_hint)
    if str(recent_schedules_text or "").strip():
        judge_parts.append(
            json_block("RECENT_SCHEDULES", {"content": str(recent_schedules_text).strip()})
        )
    judge_parts.append(json_block("CANDIDATES", summaries))
    if continuity_enabled:
        # 只有状态连续性开启时才追加「候选↔边界」评判指令与边界语义。
        judge_parts.append(_JUDGE_BOUNDARY_INSTRUCTION)
    judge_parts.append(
        "请只输出一个 JSON 对象：{\"choice\": 候选下标, \"reason\": \"一句话理由\"}。"
    )
    judge_prompt = "\n\n".join(judge_parts)
    try:
        response = await context.llm_generate(
            chat_provider_id=provider_id,
            prompt=judge_prompt,
            system_prompt=DEFAULT_DAILY_SCHEDULE_JUDGE_SYSTEM_PROMPT,
        )
        if not response or getattr(response, "role", "") != "assistant":
            return -1
        raw = (getattr(response, "completion_text", "") or "").strip()
        data = parse_json_response(raw, require_closed_fence=True)
        choice = int(data["choice"]) if isinstance(data, dict) else -1
        reason = (
            str(data.get("reason", "")).strip()[:40]
            if isinstance(data, dict)
            else ""
        )
        if 0 <= choice < len(candidates):
            logger.debug(
                f"{tag()} 🤖 裁判选择候选 {choice}/{len(candidates)} 理由: {reason or '-'}"
            )
            return choice
        return -1
    except Exception:
        return -1
