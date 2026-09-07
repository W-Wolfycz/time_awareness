"""LLM JSON 响应的共用代码块与数组提取。"""

from __future__ import annotations

import json
import re
from typing import Any


def strip_json_fence(text: str, *, require_closed: bool = False) -> str:
    cleaned = str(text or "").strip()
    if not cleaned.startswith("```"):
        return cleaned
    lines = cleaned.splitlines()
    closed = len(lines) >= 2 and lines[-1].strip().startswith("```")
    if require_closed and not closed:
        return cleaned
    lines = lines[1:]
    if closed:
        lines = lines[:-1]
    return "\n".join(lines).strip()


def parse_json_response(
    text: str,
    *,
    allow_embedded_array: bool = False,
    require_closed_fence: bool = False,
) -> Any:
    """解析 JSON；兼容模式用 raw_decode 从每个 [ 位置提取嵌入的 JSON 数组。"""
    cleaned = strip_json_fence(text, require_closed=require_closed_fence)
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError) as first_error:
        if not allow_embedded_array:
            raise ValueError("响应不是合法 JSON") from first_error
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\[", cleaned):
            try:
                value, _ = decoder.raw_decode(cleaned, match.start())
                return value
            except (json.JSONDecodeError, ValueError):
                continue
        raise ValueError("响应中的 JSON 数组无效") from first_error


def json_block(name: str, value) -> str:
    """把 dict 序列化为 XML 数据块：<NAME>\n{json}\n</NAME>（尖括号转义防注入）。"""
    body = json.dumps(value, ensure_ascii=False, sort_keys=True)
    body = body.replace("<", "\\u003c").replace(">", "\\u003e")
    return f"<{name}>\n{body}\n</{name}>"
