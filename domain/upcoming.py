"""未来 N 天预告：天数规整、日期序列与单日渲染（纯逻辑，数据源由调用方注入）。"""

from __future__ import annotations

import datetime

# 安全上限：配置写多大都不至于把每轮 prompt 撑爆
UPCOMING_MAX_DAYS = 14
UPCOMING_NO_EVENT = "无事项"
# 前三天带相对日名，模型不必自己从日期推算「明天」是哪一行
RELATIVE_DAY_LABELS = ("明天", "后天", "大后天")

_DAY_FORMAT = "%m-%d"


def normalize_upcoming_days(value) -> int:
    """把配置值规整为 0..UPCOMING_MAX_DAYS；非法/非正数归 0，超上限按安全上限截断。"""
    try:
        days = int(value)
    except (TypeError, ValueError):
        return 0
    if days <= 0:
        return 0
    return min(days, UPCOMING_MAX_DAYS)


def upcoming_dates(today: datetime.date, days: int) -> list[datetime.date]:
    """从明天起的连续 ``days`` 天；跨月/跨年按 date 运算自然延续。"""
    first = today + datetime.timedelta(days=1)
    return [first + datetime.timedelta(days=offset) for offset in range(days)]


def relative_day_label(offset: int) -> str:
    """第 ``offset`` 天（0=明天）的相对日名；超出词表返回空串。"""
    if 0 <= offset < len(RELATIVE_DAY_LABELS):
        return RELATIVE_DAY_LABELS[offset]
    return ""


def format_upcoming_day(
    day: datetime.date,
    *,
    workday_label: str,
    event_texts: list[str] | None,
    relative_label: str = "",
) -> str:
    """渲染单日一行：``明天 09-26 工作日性质：事项1、事项2``。

    ``event_texts`` 为 None 表示事项不可知（数据源关闭或未覆盖该日期），整段省略；
    空列表表示确认无事项，显式写「无事项」。
    """
    head = " ".join(
        part
        for part in (
            str(relative_label or "").strip(),
            day.strftime(_DAY_FORMAT),
            str(workday_label or "").strip(),
        )
        if part
    )
    if event_texts is None:
        return head
    texts = [str(text).strip() for text in event_texts]
    texts = [text for text in texts if text]
    body = "、".join(texts) if texts else UPCOMING_NO_EVENT
    return f"{head}：{body}"


def build_upcoming_text(*, today: datetime.date, days: int, classify, events_for_date) -> str:
    """按天拼接预告文本；``classify``/``events_for_date`` 由调用方注入，便于隔离传感器。"""
    return "\n".join(
        format_upcoming_day(
            day,
            workday_label=classify(day)[1],
            event_texts=events_for_date(day),
            relative_label=relative_day_label(offset),
        )
        for offset, day in enumerate(upcoming_dates(today, days))
    )
