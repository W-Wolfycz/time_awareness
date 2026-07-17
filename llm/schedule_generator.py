"""AI 单日日程表生成与输出解析。"""

import json
import re
from typing import Any

from ..constants import DEFAULT_SCHEDULE_GENERATE_SYSTEM_PROMPT
from ..log import logger, tag


def parse_schedule_json(text: str) -> list[dict] | None:
    """解析日程表 JSON，返回规整后的 name/start/end/state 列表。"""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines)

    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        match = re.search(r"\[.*\]", cleaned, re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except (json.JSONDecodeError, ValueError):
            return None

    if not isinstance(data, list) or not data:
        return None

    result = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        start = str(item.get("start", "")).strip()
        end = str(item.get("end", "")).strip()
        state = str(item.get("state", "")).strip()
        if not start or not end or not state:
            continue
        if not _valid_hhmm(start) or not _valid_hhmm(end):
            continue
        result.append({"name": name, "start": start, "end": end, "state": state})
    return result or None


def _valid_hhmm(value: str) -> bool:
    try:
        hour, minute = value.split(":")
        return 0 <= int(hour) <= 23 and 0 <= int(minute) <= 59
    except (ValueError, AttributeError):
        return False


async def generate_schedule(
    context: Any,
    *,
    theme: str,
    provider_id: str,
):
    """调用 LLM 生成日程表；异常由调用方转成用户可见消息。"""
    kwargs = {
        "prompt": theme,
        "system_prompt": DEFAULT_SCHEDULE_GENERATE_SYSTEM_PROMPT,
    }
    kwargs["chat_provider_id"] = provider_id
    response = await context.llm_generate(**kwargs)
    if not response or response.role != "assistant":
        logger.warning(f"{tag()} ⚠️ AI 日程表响应异常: {response}")
        return None, ""
    raw_text = (response.completion_text or "").strip()
    return parse_schedule_json(raw_text), raw_text
