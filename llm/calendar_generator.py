"""AI 时间表生成器：按主题提示词调用 LLM 输出日历事项 JSON 数组。"""

from typing import Optional

from ..log import logger, tag

from ..constants import DEFAULT_AI_GENERATE_SYSTEM_PROMPT
from .json_response import parse_json_response


DEFAULT_MAX_GENERATE = 40
_MAX_EVENT_TEXT_LENGTH = 200

# 无年份上下文时按「年年可重复」口径校验各月天数上限（与 valid_month_day 一致）
_MONTH_MAX_DAYS = {4: 30, 6: 30, 9: 30, 11: 30, 2: 29}


def _coerce_event(raw: dict) -> Optional[dict]:
    """将单条原始事项粗规整为 ``{month, day, text, repeat}``。"""
    if not isinstance(raw, dict):
        return None

    text = str(raw.get("text", "")).strip()
    if not text:
        return None
    if len(text) > _MAX_EVENT_TEXT_LENGTH:
        text = text[:_MAX_EVENT_TEXT_LENGTH]

    try:
        month = int(raw.get("month"))
        day = int(raw.get("day"))
    except (TypeError, ValueError):
        return None
    if not (1 <= month <= 12 and 1 <= day <= _MONTH_MAX_DAYS.get(month, 31)):
        return None

    try:
        repeat = int(raw.get("repeat", -1))
    except (TypeError, ValueError):
        repeat = -1

    event = {"month": month, "day": day, "text": text, "repeat": repeat}

    year = raw.get("year")
    if year is not None:
        try:
            event["year"] = int(year)
        except (TypeError, ValueError):
            pass

    return event


def parse_generated_events(response_text: str) -> Optional[list]:
    """从 LLM 返回文本中解析出事项列表（JSON 数组）。"""
    if not response_text or not isinstance(response_text, str):
        return None

    try:
        data = parse_json_response(response_text, allow_embedded_array=True)
    except ValueError as exc:
        # 原文属正文类诊断，已由下方 DEBUG 记录，WARNING 只留异常形态
        logger.warning(f"{tag()} ⚠️ AI 生成时间表 JSON 解析失败: {exc}")
        return None

    if isinstance(data, dict):
        data = data.get("events")
    if not isinstance(data, list):
        logger.warning(f"{tag()} ⚠️ AI 生成时间表结果不是数组")
        return None

    return [event for raw in data if (event := _coerce_event(raw)) is not None]


def build_system_prompt(current_year: int, max_events: int) -> str:
    """组装 AI 生成的系统提示词：内置指令 + 运行时约束（年份、数量上限）。"""
    suffix = (
        f"\n\n当前年份为 {current_year}（如需填写基准年请使用该年份）。"
        f"本次生成事项数量以 {max_events} 条为期望（约这个数，可多可少，覆盖四季即可）。"
    )
    return DEFAULT_AI_GENERATE_SYSTEM_PROMPT + suffix


async def generate_calendar_events(
    context,
    provider_id: str,
    user_prompt: str,
    system_prompt: str,
    current_year: int,
) -> Optional[list]:
    """发起 LLM 调用，根据主题提示词生成时间表事项。"""
    user_prompt = (user_prompt or "").strip()
    if not user_prompt:
        logger.warning(f"{tag()} ⚠️ AI 生成时间表缺少主题提示词")
        return None

    try:
        llm_response = await context.llm_generate(
            chat_provider_id=provider_id,
            prompt=user_prompt,
            system_prompt=system_prompt,
        )
    except Exception as e:
        logger.error(f"{tag()} ❌ AI 生成时间表 LLM 调用失败: {e}")
        return None

    if not llm_response or llm_response.role != "assistant":
        logger.warning(
            f"{tag()} ⚠️ AI 生成时间表 LLM 响应异常: "
            f"type={type(llm_response).__name__} "
            f"role={getattr(llm_response, 'role', '')}"
        )
        return None

    response_text = llm_response.completion_text
    if not response_text:
        logger.warning(f"{tag()} ⚠️ AI 生成时间表 LLM 返回空响应")
        return None

    logger.debug(f"{tag()} AI 生成时间表原始响应: {response_text}")

    events = parse_generated_events(response_text)
    if events is None:
        return None

    # max_events 是期望值而非硬上限：只保留固定防失控安全上限
    if len(events) > 400:
        logger.warning(f"{tag()} ⚠️ AI 生成时间表超过安全上限 400，已截断")
        events = events[:400]
    for event in events:
        event.setdefault("year", current_year)

    logger.info(f"{tag()} ✅ AI 生成时间表成功，共 {len(events)} 条事项")
    return events
