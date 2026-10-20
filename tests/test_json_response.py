"""llm/json_response.py 的代码块剥离与数组提取测试。"""


from time_awareness.llm.json_response import json_block


def test_json_block_escapes_angle_brackets():
    block = json_block("NAME", {"x": "<tag>"})
    assert "<NAME>" in block and "</NAME>" in block
    assert "\\u003ctag\\u003e" in block
