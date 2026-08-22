import pytest

from time_awareness.llm.json_response import parse_json_response, strip_json_fence


def test_json_fence_modes_preserve_daily_strictness():
    assert strip_json_fence("```json\n[]\n```", require_closed=True) == "[]"
    assert strip_json_fence("```json\n[]", require_closed=True).startswith("```")


def test_embedded_array_is_only_accepted_in_compatibility_mode():
    with pytest.raises(ValueError):
        parse_json_response("说明：[1, 2]")
    assert parse_json_response("说明：[1, 2]", allow_embedded_array=True) == [1, 2]


def test_embedded_array_ignores_brackets_in_prose_and_strings():
    """说明文字含方括号/多数组/字符串内括号时仍能提取合法 JSON 数组。"""
    text = '说明：[示例] 结果 [{"month":1,"day":1,"text":"x","repeat":-1}]'
    assert parse_json_response(text, allow_embedded_array=True) == [
        {"month": 1, "day": 1, "text": "x", "repeat": -1}
    ]
    text2 = '前缀 [{"name":"睡眠[阶段]","start":"00:00","end":"01:00","state":"s"}]'
    assert parse_json_response(text2, allow_embedded_array=True) == [
        {"name": "睡眠[阶段]", "start": "00:00", "end": "01:00", "state": "s"}
    ]
