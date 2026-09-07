"""时间表内存存储：仅持事项副本与匹配/拼接纯逻辑，不做 IO；repeat 语义见 normalize_repeat。"""

import calendar as _cal

from ..log import logger, tag


REPEAT_NONE = 0
REPEAT_FOREVER = -1
MAX_FINITE_REPEAT = 4

_MONTH_MAX_DAYS = {
    1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30,
    7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31,
}


def _month_day_count(year: int, month: int) -> int:
    """指定年份月份的实际天数（2 月按闰年）。"""
    try:
        return _cal.monthrange(int(year), int(month))[1]
    except (TypeError, ValueError):
        return _MONTH_MAX_DAYS.get(int(month), 30)

MAX_EVENT_TEXT_LENGTH = 200
MAX_EVENTS = 2000


def valid_month_day(month: int, day: int, year: int | None = None) -> bool:
    """校验 (month, day)；year 提供时按实际天数（非闰年 2 月拒 29），缺省按可重复上限。"""
    if month not in _MONTH_MAX_DAYS:
        return False
    if year is not None:
        return 1 <= day <= _month_day_count(year, month)
    return 1 <= day <= _MONTH_MAX_DAYS[month]


def normalize_repeat(repeat) -> int:
    """将任意输入规整为合法的 repeat 取值。非法 / 越界值回退为 ``REPEAT_NONE``。"""
    try:
        value = int(repeat)
    except (TypeError, ValueError):
        return REPEAT_NONE
    if value == REPEAT_FOREVER:
        return REPEAT_FOREVER
    if 0 <= value <= MAX_FINITE_REPEAT:
        return value
    return REPEAT_NONE


def event_active_in_year(event: dict, year: int) -> bool:
    """判断事项在指定年份是否生效（按 repeat 规则）。"""
    repeat = normalize_repeat(event.get("repeat", REPEAT_NONE))
    if repeat == REPEAT_FOREVER:
        return True
    try:
        base_year = int(event.get("year"))
    except (TypeError, ValueError):
        return False
    return base_year <= year <= base_year + repeat


class CalendarStore:
    """时间表事项内存存储。"""

    def __init__(self):
        self.events: list = []  # 用户自定义事件（calendar_data.yaml）
        self.builtin_events: list = []  # 内置现实日历事件（builtin_events.yaml）
        logger.debug(f"{tag()} CalendarStore 初始化完成")

    def set_events(self, events: list) -> None:
        """整体替换用户事项列表（调用方应已完成校验/规整）。"""
        self.events = list(events) if isinstance(events, list) else []

    def set_builtin_events(self, events: list) -> None:
        """整体替换内置事项列表。"""
        self.builtin_events = list(events) if isinstance(events, list) else []

    def clear(self) -> None:
        self.events = []
        self.builtin_events = []

    @staticmethod
    def _event_active_on(event: dict, year: int, month: int, day: int) -> bool:
        """判断事件在指定公历日期是否生效；builtin 精确匹配 year，user 按 repeat 规则。"""
        if event.get("month") != month or event.get("day") != day:
            return False
        if event.get("source") == "builtin":
            return event.get("year") == year
        return event_active_in_year(event, year)

    def events_for_date(self, year: int, month: int, day: int, include_builtin: bool = True) -> list:
        """指定日期生效事项；按 (month,day,text) 去重，user 优先于 builtin。"""
        result: list = []
        seen: set = set()

        def _try_add(e: dict) -> None:
            key = (e.get("month"), e.get("day"), str(e.get("text", "")).strip())
            if key in seen:
                return
            seen.add(key)
            result.append(e)

        for e in self.events:
            if self._event_active_on(e, year, month, day):
                _try_add(e)

        if include_builtin:
            for e in self.builtin_events:
                if self._event_active_on(e, year, month, day):
                    _try_add(e)
        return result

    def events_for_month(self, year: int, month: int, include_builtin: bool = True) -> list:
        """指定月份生效事项（日期升序）；只迭代当月实际天数，无幽灵日期。"""
        # 复用 events_for_date 拿每天的列表，避免去重逻辑漂移
        result = []
        seen_keys = set()
        for day in range(1, _month_day_count(year, month) + 1):
            day_events = self.events_for_date(year, month, day, include_builtin=include_builtin)
            for e in day_events:
                key = (e.get("day", 0), e.get("text", ""), e.get("source", ""))
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                result.append(e)
        return result

    def today_text(self, now, separator: str = "、", include_builtin: bool = True) -> str:
        """拼接「今天」的所有事项文本；无事项返回空串。"""
        events = self.events_for_date(now.year, now.month, now.day, include_builtin=include_builtin)
        texts = [str(e.get("text", "")).strip() for e in events]
        texts = [text for text in texts if text]
        if not texts:
            return ""
        return separator.join(texts)
