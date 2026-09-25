"""日历命令的纯解析与展示助手。"""

from ..core.builtin_events import (
    CATEGORY_INTERNATIONAL,
    CATEGORY_LEGAL,
    CATEGORY_POLITICAL,
    CATEGORY_SOLAR_TERM,
    CATEGORY_TRADITIONAL,
)
from ..core.calendar_store import REPEAT_FOREVER, REPEAT_NONE


def describe_repeat(repeat: int) -> str:
    if repeat == REPEAT_FOREVER:
        return "（每年）"
    if repeat == REPEAT_NONE:
        return ""
    return f"（连续 {repeat + 1} 年）"


def category_short_tag(category: str) -> str:
    return {
        CATEGORY_LEGAL: "法定",
        CATEGORY_TRADITIONAL: "传统",
        CATEGORY_SOLAR_TERM: "节气",
        CATEGORY_POLITICAL: "政治",
        CATEGORY_INTERNATIONAL: "国际",
    }.get(category, "内")


def find_event_by_short_id(store, short: str) -> dict | None:
    short = (short or "").strip().lower()
    if not short:
        return None
    events = list(store.events) + list(store.builtin_events)
    for event in events:
        full = str(event.get("id", "")).lower()
        if full == short or full.startswith(short):
            return event
    for event in events:
        if str(event.get("text", "")).strip().lower() == short:
            return event
    return None


def parse_year_month(text: str) -> tuple | None:
    text = (text or "").strip()
    try:
        parts = text.split("-")
        if len(parts) >= 2:
            year = int(parts[0])
            month = int(parts[1])
            if 1 <= month <= 12 and 1970 <= year <= 9999:
                return year, month
    except ValueError:
        pass
    return None


def parse_event_date(text: str) -> tuple | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        parts = text.split("-")
        if len(parts) == 3:
            return int(parts[1]), int(parts[2]), int(parts[0])
        if len(parts) == 2:
            return int(parts[0]), int(parts[1]), None
    except ValueError:
        pass
    return None


def parse_category_input(text: str) -> str:
    normalized = (text or "").strip().lower()
    return {
        "法定": CATEGORY_LEGAL,
        "法定节假日": CATEGORY_LEGAL,
        "legal": CATEGORY_LEGAL,
        "传统": CATEGORY_TRADITIONAL,
        "traditional": CATEGORY_TRADITIONAL,
        "节气": CATEGORY_SOLAR_TERM,
        "solar": CATEGORY_SOLAR_TERM,
        "solar_term": CATEGORY_SOLAR_TERM,
        "政治": CATEGORY_POLITICAL,
        "政治纪念": CATEGORY_POLITICAL,
        "political": CATEGORY_POLITICAL,
        "国际": CATEGORY_INTERNATIONAL,
        "international": CATEGORY_INTERNATIONAL,
        "西方": CATEGORY_INTERNATIONAL,
    }.get(normalized, "")
