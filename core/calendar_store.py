"""
时间表（日历事项）内存存储

仿 ``runtime_data`` 的单例模式，仅持有「时间表事项」的内存副本与纯逻辑（匹配、
拼接），不做任何文件 IO、也不依赖 AstrBot context。

设计目标（单一真相源）：
- ``CalendarStore`` 是「当天事项」取值逻辑的唯一来源，占位符解析与
  ``CalendarManager`` 均从本单例读取。
- 文件读写、CRUD 与校验由 ``core/calendar_manager.py`` 负责，写入后回填本单例。

事项数据结构（单条）::

    {
        "id": "<uuid hex>",   # 稳定标识，供编辑/删除定位
        "year": 2026,          # 基准年（首次生效年份，也用于显示）
        "month": 1,            # 1-12
        "day": 1,              # 1-31（闰日 2-29 仅在闰年匹配）
        "text": "元旦",        # 事项描述
        "repeat": 0            # 重复规则，见下方常量
    }

重复规则（``repeat`` 取值语义）：
- ``REPEAT_NONE``（0）：仅在「基准年」当天生效。
- 1..4：基准年 + 之后 N 年，共 ``N + 1`` 年生效。
- ``REPEAT_FOREVER``（-1）：每年重复（永久），忽略年份，仅按月-日匹配。
"""

from ..log import logger, tag


REPEAT_NONE = 0
REPEAT_FOREVER = -1
MAX_FINITE_REPEAT = 4

_MONTH_MAX_DAYS = {
    1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30,
    7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31,
}

MAX_EVENT_TEXT_LENGTH = 200
MAX_EVENTS = 2000


def valid_month_day(month: int, day: int) -> bool:
    """校验 (month, day) 是否为合法的「年年可重复」日期。"""
    if month not in _MONTH_MAX_DAYS:
        return False
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
    """时间表事项内存存储（单例）"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
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
        """判断事件在指定公历日期是否生效。

        builtin 事件已按公历 year 生成，year 必须精确匹配；
        user 事件保留 repeat 规则（每年/连续N年/仅当年）。
        """
        if event.get("month") != month or event.get("day") != day:
            return False
        if event.get("source") == "builtin":
            return event.get("year") == year
        return event_active_in_year(event, year)

    def events_for_date(self, year: int, month: int, day: int, include_builtin: bool = True) -> list:
        """返回指定日期生效的事项（用户 + 内置合并去重）。

        去重规则：相同 (month, day, text) 只保留先入队的那条，user 优先于 builtin。
        返回顺序：user 事件（插入序）→ builtin 事件（生成序），去重后的。
        """
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
        """返回指定月份生效的事项（按日期升序，同日保持顺序）。"""
        # 复用 events_for_date 拿每天的列表，避免去重逻辑漂移
        result = []
        seen_keys = set()
        for day in range(1, 32):
            day_events = self.events_for_date(year, month, day, include_builtin=include_builtin)
            for e in day_events:
                key = (e.get("day", 0), e.get("text", ""), e.get("source", ""))
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                result.append(e)
        return result

    def today_text(self, now, separator: str = "、", empty_text: str = "", include_builtin: bool = True) -> str:
        """拼接「今天」的所有事项文本。"""
        events = self.events_for_date(now.year, now.month, now.day, include_builtin=include_builtin)
        texts = [str(e.get("text", "")).strip() for e in events]
        texts = [text for text in texts if text]
        if not texts:
            return empty_text
        return separator.join(texts)


calendar_store = CalendarStore()
