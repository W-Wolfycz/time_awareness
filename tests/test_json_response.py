"""llm/json_response.py 的代码块剥离与数组提取测试。"""

import pytest

from time_awareness.llm.json_response import json_block, parse_json_response, strip_json_fence


def test_strip_json_fence():
    assert strip_json_fence("{}") == "{}"
    assert strip_json_fence("```json\n{}") == "{}"
    assert strip_json_fence("```json\n{}\n```") == "{}"
    assert strip_json_fence("```json\n{}", require_closed=True) == "```json\n{}"


def test_parse_json_response_valid_and_invalid():
    assert parse_json_response('{"a": 1}') == {"a": 1}
    with pytest.raises(ValueError, match="响应不是合法 JSON"):
        parse_json_response("not json")


def test_parse_json_response_embedded_array():
    assert parse_json_response("前置说明\n[1, 2, 3]\n后置", allow_embedded_array=True) == [1, 2, 3]


def test_parse_json_response_require_closed_fence():
    with pytest.raises(ValueError):
        parse_json_response("```json\n[1]", require_closed_fence=True)


def test_json_block_escapes_angle_brackets():
    block = json_block("NAME", {"x": "<tag>"})
    assert "<NAME>" in block and "</NAME>" in block
    assert "\\u003ctag\\u003e" in block
