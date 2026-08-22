"""AI 每日日程输出解析与严格校验。

Step2 规划统一使用数组协议：``[{name,start,end,state}, ...]``（允许空数组）。
theme/boundary_state 由 Step1 边界评估（``evaluate_boundary``）独立输出，
不再使用旧的对象协议。未知键、XML 标签、控制字符与越界状态值一律拒绝。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

from ..constants import (
    DEFAULT_BOUNDARY_EVALUATION_SYSTEM_PROMPT,
    DEFAULT_DAILY_SCHEDULE_JUDGE_SYSTEM_PROMPT,
    DEFAULT_DAILY_SCHEDULE_SYSTEM_PROMPT,
)
from ..domain.schedule import (
    ScheduleValidationError,
    normalize_daily_slots,
    parse_time,
    schedule_similarity,
)
from ..log import logger, tag
from .json_response import parse_json_response


_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# 日界状态白名单键（有限范围、去身份化）
_BOUNDARY_STATE_NUMERIC_KEYS = {"energy", "sleep_debt", "focus", "social_energy", "motivation"}
_BOUNDARY_STATE_STRING_KEYS = {"physical_state", "daily_theme"}


def empty_boundary() -> dict:
    """全维度 null 种子：边界评估失败/无历史时存储仍保持全维度（缺失 = null）。

    保证快照 boundary_state 恒为 8 个维度齐全，任何消费方都不需要处理缺字段。
    """
    return {
        "energy": None,
        "sleep_debt": None,
        "focus": None,
        "social_energy": None,
        "motivation": None,
        "physical_state": None,
        "daily_theme": None,
        "unfinished_plans": None,
    }

# 情绪（mood/心情/情绪等）已交由好感插件管理，日界状态不再承载——
# 模型若输出这些维度，校验时给出明确提示而非静默接受。
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


def _validate_theme(value: Any) -> dict:
    """校验 theme 对象；缺失或空对象返回 {}。"""
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise ScheduleValidationError("theme 必须是对象")
    allowed = {"archetype", "variation_axis", "main_goal"}
    if set(value) - allowed:
        raise ScheduleValidationError("theme 含未知字段")
    result = {}
    for key in ("archetype", "variation_axis", "main_goal"):
        raw = value.get(key, "")
        if raw in (None, ""):
            continue
        result[key] = _clean_text(
            raw,
            field=f"theme.{key}",
            max_length=60 if key == "archetype" else 160,
        )
    return result


def _validate_boundary_state(value: Any) -> dict:
    """校验日界状态；缺失/空对象/全省略（稀疏输出）返回 {}。

    固定 8 维度：energy/sleep_debt/focus/social_energy/motivation（0-100）、
    physical_state/daily_theme（短标签）、unfinished_plans（数组）。
    模型的中文/同义字段按别名映射归一化；情绪维度（mood/心情/情绪等）
    已交由好感插件管理，输出时给出明确提示拒绝。
    """
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
    if "physical_state" in normalized and normalized["physical_state"] not in (None, ""):
        result["physical_state"] = _clean_text(
            normalized["physical_state"],
            field="boundary_state.physical_state",
            max_length=12,
        )
    if "daily_theme" in normalized and normalized["daily_theme"] not in (None, ""):
        result["daily_theme"] = _clean_text(
            normalized["daily_theme"],
            field="boundary_state.daily_theme",
            max_length=20,
        )
    if "unfinished_plans" in normalized and normalized["unfinished_plans"] not in (None, []):
        plans = normalized["unfinished_plans"]
        if not isinstance(plans, list) or not plans:
            raise ScheduleValidationError("unfinished_plans 必须是数组")
        if len(plans) > 8:
            raise ScheduleValidationError("unfinished_plans 超出 8 项上限")
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


def parse_daily_schedule_json(text: str, *, max_slots: int = 24) -> list[dict]:
    """解析基础数组协议输出；只接受协议字段、日内非跨午夜且互不重叠的时段。

    允许合法的空数组 ``[]``（当天没有需要明确列出的角色时段），返回空列表。
    """
    try:
        data = parse_json_response(text, require_closed_fence=True)
    except ValueError as exc:
        raise ScheduleValidationError("输出不是合法 JSON") from exc
    if not isinstance(data, list):
        raise ScheduleValidationError("顶层必须是数组")
    max_slots = max(1, min(48, int(max_slots)))
    if len(data) > max_slots:
        raise ScheduleValidationError("时段数量超出上限")

    slots = [_validate_slot(item) for item in data]
    return normalize_daily_slots(slots)


def _candidate_summary(candidate: dict, index: int) -> dict:
    """把单个候选压成结构化摘要（judge 输入）。

    候选不携带角色状态——当日边界是共享参考（judge 输入显式给出），不是
    候选专属字段。摘要只含时段结构与状态指纹，让 judge 按「候选↔边界」
    对照评审，避免共享边界冒充候选数据。
    """
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


# ---------------- 候选日程 ↔ 日界状态 连续性（确定性/启发式） ----------------
#
# 候选自身不携带日界状态：Step1 当日边界是所有候选共享的「当日开始时」
# 参考，不是候选专属字段，绝不能与自身比较。因此连续性以候选 slots 的
# name/state/时间结构为输入，对 boundary 各维度做粗粒度匹配——同一
# boundary 下不同候选会得到不同惩罚。
#
# 诚实可审计策略：
# - 文本维度（daily_theme / physical_state / unfinished_plans）做关键词
#   归一化匹配，结果用 0..1 的冲突分表达，不做精确数值换算；
# - 数值维度（energy / sleep_debt / focus / social_energy / motivation）
#   只在「耗尽/异常」区间启用粗粒度冲突规则（高消耗日程占比 vs 恢复型
#   日程占比），其余/缺失一律中性 0.5，不伪造精确度。

# 恢复类时段关键词（低消耗 / 恢复性活动）
_RECOVERY_KEYWORDS = (
    "睡眠", "午休", "小憩", "休息", "放松", "独处", "静养", "冥想",
    "打盹", "歇", "疗养", "休整", "安静", "散步",
)
# 高消耗/要求类时段关键词（任务、训练、执勤、认知、创作等）
_DEMAND_KEYWORDS = (
    "训练", "执勤", "巡逻", "战斗", "任务", "工作", "会议", "学习", "作业",
    "加班", "冲刺", "演习", "值班", "运动", "健身", "跑步", "创作", "写作",
    "备课", "研究", "项目", "高强度", "忙碌", "专注",
)
# 社交类时段关键词
_SOCIAL_KEYWORDS = (
    "社交", "聚会", "聊天", "团建", "会客", "拜访", "接待", "约会",
    "陪伴", "群聊", "见面", "出门",
)
# 低能量恢复主题关键词（期望日程以休息/恢复为主）
_LOW_THEME_KEYWORDS = (
    "低能量", "恢复", "休息", "放松", "休整", "调养", "疗愈", "悠闲",
)
# 高消耗主题关键词（期望日程有明确任务/执勤）
_HIGH_THEME_KEYWORDS = (
    "繁忙", "执勤", "任务", "高强度", "紧张", "冲刺", "专注", "战斗", "工作", "忙碌",
)
# 身体状态「不适/疲劳」关键词（期望日程避免高强度）
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


def _aggregate_demand_fraction(slots: list[dict]) -> float:
    """高消耗时段覆盖分钟数 / 全部覆盖分钟数（0..1）。"""
    total = 0.0
    demand = 0.0
    for slot in slots or []:
        minutes = _slot_minutes(slot) if isinstance(slot, dict) else None
        if not minutes:
            continue
        start, end = minutes
        length = max(0, end - start)
        if length <= 0:
            continue
        total += length
        if _slot_demand(slot) == 1:
            demand += length
    return demand / total if total > 0 else 0.0


def _aggregate_social_fraction(slots: list[dict]) -> float:
    """社交类时段覆盖分钟数 / 全部覆盖分钟数（0..1）。"""
    total = 0.0
    social = 0.0
    for slot in slots or []:
        minutes = _slot_minutes(slot) if isinstance(slot, dict) else None
        if not minutes:
            continue
        start, end = minutes
        length = max(0, end - start)
        if length <= 0:
            continue
        total += length
        text = f"{slot.get('name', '')} {slot.get('state', '')}"
        if any(keyword in text for keyword in _SOCIAL_KEYWORDS):
            social += length
    return social / total if total > 0 else 0.0


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
    """选择连续性参考日界状态。

    优先当日 Step1 边界评估产物；全 null/缺失时回退前一日 ``boundary_state``。
    """
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
    """候选日程 ↔ 日界状态 的连续性惩罚（0..1，越低越连续）。

    8 个维度（energy / sleep_debt / focus / social_energy / motivation /
    physical_state / daily_theme / unfinished_plans）等权平均：
    - 数值维度：只在耗尽/异常区间启用冲突规则，否则中性 0.5；
    - 文本维度：主题/身体状态关键词归一化、未完成计划显式安排覆盖率。
    """
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
    """无 LLM 的候选评分（确定性回退），分数越低越好。

    两个分量（权重合计 1.0）：

    1. 反重复（0.7）：与最近日程的平均结构相似度（0..1，越高越差）；
    2. 状态连续性（0.3）：候选「时段结构」与日界状态的兼容程度。连续性
       直接以候选 slots 的 name/state/时间结构对 boundary 各维度做粗粒度
       匹配（``_boundary_continuity_penalty``），因此同一 boundary 下不同
       候选会得到不同惩罚。参考状态优先使用当日 Step1 边界评估产物
       （``today_boundary``），Step1 缺失时回退前一日 ``boundary_state``。

       候选自身不携带 ``boundary_state``：Step1 当日边界是所有候选共享的
       当日初始状态，不是候选专属字段，不能与自身比较。没有可比较边界时
       连续性为中性 0.5。

       ``continuity_enabled=False`` 时（``state_continuity_enabled`` 关闭）直接
       返回反重复分量，不加入任何连续性常数项——排序只由反重复（或等价候选相关
       已启用分量）决定，避免“给所有候选加常数后声称关闭”。

    不包含覆盖完整性分量（2.0.0 计划 §2.4）：空、稀疏与完整日程公平竞争，
    不能因覆盖较少而固定落败。
    """
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
        # 状态连续性关闭：不执行 slots↔boundary 匹配，也不加 0.3×中性
        # 常数项。分数仅含反重复分量，排序完全不受边界影响。
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
    max_slots: int = 24,
) -> tuple[PlanningResult, str]:
    """调用 Provider 并返回已经过数组协议校验的规划结果和原始文本。

    2.0.0 起 Step2 统一数组协议：theme/boundary 已由 Step1 边界评估
    （``evaluate_boundary``）独立产出，不再使用对象协议。
    """
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
        PlanningResult(slots=parse_daily_schedule_json(raw_text, max_slots=max_slots)),
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


# judge 显式拿到角色状态后，必须按「候选时段结构 ↔ 角色状态」评判连续性；
# 候选摘要自身不携带边界（共享当日状态不能冒充候选专属数据）。
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
    """使用 LLM 在候选中选择最自然且不重复的一个，返回其下标。

    人设参考与最近日程参考按需进入 judge 输入（评审维度 1/3 依赖）；
    所有数据块与生成请求同风格（``<TAG>`` + JSON，尖括号转义）。
    失败（异常/非法输出）返回 -1，由调用方回退到确定性评分。
    """
    if len(candidates) < 2:
        return 0

    def _block(name: str, value: Any) -> str:
        body = json.dumps(value, ensure_ascii=False, sort_keys=True)
        body = body.replace("<", "\\u003c").replace(">", "\\u003e")
        return f"<{name}>\n{body}\n</{name}>"

    summaries = [
        _candidate_summary(candidate, index)
        for index, candidate in enumerate(candidates)
    ]
    judge_parts = []
    if str(persona_prompt or "").strip():
        judge_parts.append(
            _block("PERSONA", {"content": str(persona_prompt).strip()})
        )
    judge_parts.append(prompt_hint)
    if str(recent_schedules_text or "").strip():
        judge_parts.append(
            _block("RECENT_SCHEDULES", {"content": str(recent_schedules_text).strip()})
        )
    judge_parts.append(_block("CANDIDATES", summaries))
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
            logger.info(
                f"{tag()} 🤖 裁判选择候选 {choice}/{len(candidates)} 理由: {reason or '-'}"
            )
            return choice
        return -1
    except Exception:
        return -1
