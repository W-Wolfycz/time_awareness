from time_awareness.llm.calendar_generator import parse_generated_events


def test_parse_generated_events_rejects_invalid_month_day():
    assert parse_generated_events('[{"month": 13, "day": 1, "text": "bad"}]') == []


def test_parse_generated_events_accepts_json_code_block():
    result = parse_generated_events('```json\n[{"month": 7, "day": 16, "text": "演示日", "repeat": -1}]\n```')
    assert result == [{"month": 7, "day": 16, "text": "演示日", "repeat": -1}]
