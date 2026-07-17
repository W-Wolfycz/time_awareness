from time_awareness.llm.schedule_generator import parse_schedule_json


def test_parse_schedule_json_accepts_cross_midnight_slot():
    result = parse_schedule_json(
        '[{"name":"睡眠","start":"22:00","end":"06:00","state":"正在休息"}]'
    )
    assert result == [
        {"name": "睡眠", "start": "22:00", "end": "06:00", "state": "正在休息"}
    ]


def test_parse_schedule_json_drops_invalid_time():
    assert parse_schedule_json(
        '[{"name":"错误","start":"25:00","end":"06:00","state":"无效"}]'
    ) is None
