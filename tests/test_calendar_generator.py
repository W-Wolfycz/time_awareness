from time_awareness.llm.calendar_generator import parse_generated_events


def test_parse_generated_events_rejects_invalid_month_day():
    assert parse_generated_events('[{"month": 13, "day": 1, "text": "bad"}]') == []
    # 2 月 30 日等不符合该月实际天数的日期直接拒绝，不再静默丢弃
    assert parse_generated_events('[{"month": 2, "day": 30, "text": "bad"}]') == []
    assert parse_generated_events('[{"month": 4, "day": 31, "text": "bad"}]') == []
    # 2 月 29 日在无年份上下文下按「年年可重复」口径允许
    assert parse_generated_events('[{"month": 2, "day": 29, "text": "闰日纪念"}]') == [
        {"month": 2, "day": 29, "text": "闰日纪念", "repeat": -1}
    ]


def test_parse_generated_events_accepts_json_code_block():
    result = parse_generated_events('```json\n[{"month": 7, "day": 16, "text": "演示日", "repeat": -1}]\n```')
    assert result == [{"month": 7, "day": 16, "text": "演示日", "repeat": -1}]
