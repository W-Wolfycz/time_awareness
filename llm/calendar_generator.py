"""
AI 时间表生成器

根据用户给出的主题/世界观提示词（如「异世界/魔法/节日」），发起一次 LLM 调用，
让 AI 输出一整套贴合主题的日历事项（JSON 数组），解析后交由调用方预览 / 落盘。

设计：
- 纯解析逻辑（``parse_generated_events``）不依赖 AstrBot context，便于单测。
- LLM 调用封装在 ``generate_calendar_events`` 中，失败不抛出而是返回 ``None``。
"""

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
    """从 LLM 返回文本中解析出事项列表（JSON 数组）。

    兼容：纯 JSON 数组；```json ... ``` 代码块；数组前后带有多余说明文字。
    """
    if not response_text or not isinstance(response_text, str):
        return None

    try:
        data = parse_json_response(response_text, allow_embedded_array=True)
    except ValueError as exc:
        logger.warning(
            f"{tag()} ⚠️ AI 生成时间表 JSON 解析失败: {exc}, "
            f"原文: {response_text}"
        )
        return None

    if isinstance(data, dict):
        data = data.get("events")
    if not isinstance(data, list):
        logger.warning(f"{tag()} ⚠️ AI 生成时间表结果不是数组")
        return None

    events = []
    for raw in data:
        event = _coerce_event(raw)
        if event is not None:
            events.append(event)
    return events


def build_system_prompt(current_year: int, max_events: int) -> str:
    """组装 AI 生成的系统提示词：内置指令 + 运行时约束（年份、数量上限）。

    系统提示词的指令部分硬编码在 constants.DEFAULT_AI_GENERATE_SYSTEM_PROMPT，
    不接受用户配置；用户只通过命令行参数或 calendar.ai_generate_worldview 提供世界观设定。
    """
    suffix = (
        f"\n\n当前年份为 {current_year}（如需填写基准年请使用该年份）。"
        f"本次最多生成 {max_events} 条事项，请勿超过。"
    )
    return DEFAULT_AI_GENERATE_SYSTEM_PROMPT + suffix


async def generate_calendar_events(
    context,
    provider_id: str,
    user_prompt: str,
    system_prompt: str,
    current_year: int,
    max_events: int = DEFAULT_MAX_GENERATE,
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
        logger.warning(f"{tag()} ⚠️ AI 生成时间表 LLM 响应异常: {llm_response}")
        return None

    response_text = llm_response.completion_text
    if not response_text:
        logger.warning(f"{tag()} ⚠️ AI 生成时间表 LLM 返回空响应")
        return None

    logger.debug(f"{tag()} AI 生成时间表原始响应: {response_text}")

    events = parse_generated_events(response_text)
    if events is None:
        return None

    if len(events) > max_events:
        logger.warning(f"{tag()} ⚠️ AI 生成时间表超过上限 {max_events}，已截断")
        events = events[:max_events]
    for event in events:
        event.setdefault("year", current_year)

    logger.info(f"{tag()} ✅ AI 生成时间表成功，共 {len(events)} 条事项")
    return events
