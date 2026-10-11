"""未来 N 天预告：天数规整、日期序列与注入接线。

预告文本只作为临时 prompt 片段存在，运行期没有人会去读它——写错了不报错、界面也正常，
只表现为模型答错「明天/后天」的安排，所以这里守的是这几类静默失效。
"""

import datetime

from time_awareness.core.calendar_store import CalendarStore
from time_awareness.domain.upcoming import (
    UPCOMING_MAX_DAYS,
    normalize_upcoming_days,
    upcoming_dates,
)
from time_awareness.services.time_context_service import TimeContextService
from time_awareness.services.time_facts_service import TimeFactsCollector
from time_awareness.tests._shared import FakeLastChatTracker

NOW = datetime.datetime(2026, 9, 25, 23, 30, tzinfo=datetime.timezone.utc)
EVENT = {"year": 2026, "month": 9, "day": 26, "text": "公司团建", "repeat": 0}
BUILTIN_EVENT = {
    "source": "builtin",
    "category": "legal_holiday",
    "year": 2026,
    "month": 9,
    "day": 27,
    "text": "中秋节假期",
}


class _Workday:
    """固定判定（偶数日=节假日），避免测试依赖 chinese_calendar 的年份覆盖。"""

    def classify(self, day):
        kind = "workday" if day.day % 2 else "holiday"
        return (kind, f"{'工作日' if day.day % 2 else '节假日'}（周{'一二三四五六日'[day.weekday()]}）")


class _FlakyWorkday(_Workday):
    """9-27 抛异常，验证单日降级不会让整块预告消失。"""

    def classify(self, day):
        if day.day == 27:
            raise RuntimeError("sensor boom")
        return super().classify(day)


def _collector(config, store=None, workday=None):
    return TimeFactsCollector(
        config=config,
        astrbot_config_provider=lambda: None,
        calendar_store=store or CalendarStore(),
        last_chat_tracker=FakeLastChatTracker(),
        now_provider=lambda: NOW,
        workday=workday or _Workday(),
    )


def test_normalize_upcoming_days_clamps_to_safety_cap():
    # 上限失效=每轮 prompt 无限膨胀；非法值失效=用户填了却没生效
    assert normalize_upcoming_days(0) == 0
    assert normalize_upcoming_days(-3) == 0
    assert normalize_upcoming_days(None) == 0
    assert normalize_upcoming_days("x") == 0
    assert normalize_upcoming_days(3) == 3
    assert normalize_upcoming_days(UPCOMING_MAX_DAYS + 1) == UPCOMING_MAX_DAYS


def test_upcoming_dates_start_tomorrow_and_cross_year():
    # 差一天/跨年错位都只会让模型答错「明天」，不会抛错
    assert upcoming_dates(datetime.date(2026, 9, 25), 2) == [
        datetime.date(2026, 9, 26),
        datetime.date(2026, 9, 27),
    ]
    assert upcoming_dates(datetime.date(2026, 12, 30), 3) == [
        datetime.date(2026, 12, 31),
        datetime.date(2027, 1, 1),
        datetime.date(2027, 1, 2),
    ]


def test_upcoming_text_reads_configured_days_and_events():
    # 配置键读错/事件没拼进去/相对日名错位：注入照常发出但内容为空或指错天，无可见症状
    store = CalendarStore()
    store.set_events([EVENT])
    config = {
        "time_awareness": {"upcoming_days": 4},
        "calendar": {"enable_calendar": True, "builtin_event_categories": []},
    }
    lines = _collector(config, store).upcoming_text(now=NOW).splitlines()
    assert lines == [
        "明天 09-26 节假日（周六）：公司团建",
        "后天 09-27 工作日（周日）：无事项",
        "大后天 09-28 节假日（周一）：无事项",
        "09-29 工作日（周二）：无事项",
    ]
    assert _collector({"time_awareness": {}}, store).upcoming_text(now=NOW) == ""


def test_upcoming_reports_builtin_events_and_withholds_incomplete_years():
    # 年份不完整时写「无事项」=把「不知道」当成「没有」，模型会断言元旦没安排
    store = CalendarStore()
    store.set_builtin_events([BUILTIN_EVENT])
    config = {
        "time_awareness": {"upcoming_days": 2},
        "calendar": {"enable_calendar": True, "builtin_event_categories": ["legal_holidays"]},
    }
    assert _collector(config, store).upcoming_text(now=NOW).splitlines() == [
        "明天 09-26 节假日（周六）：无事项",
        "后天 09-27 工作日（周日）：中秋节假期",
    ]
    # 跨年：库只有 2026，2027 的行整段省略事项
    year_end = NOW.replace(month=12, day=31)
    assert _collector(config, store).upcoming_text(now=year_end).splitlines() == [
        "明天 01-01 工作日（周五）",
        "后天 01-02 节假日（周六）",
    ]
    # 只生成了部分启用分类（chinese_calendar 未覆盖该年 → 无法定节假日）同样不完整
    partial = CalendarStore()
    partial.set_builtin_events([{**BUILTIN_EVENT, "year": 2027, "month": 1, "day": 1}])
    partial_config = {
        "time_awareness": {"upcoming_days": 1},
        "calendar": {
            "enable_calendar": True,
            "builtin_event_categories": ["legal_holidays", "political"],
        },
    }
    assert _collector(partial_config, partial).upcoming_text(now=year_end).splitlines() == [
        "明天 01-01 工作日（周五）",
    ]


def test_upcoming_withholds_events_when_calendar_disabled():
    # 绕过日历总开关=未启用的日历数据泄漏进 prompt；关闭时也不能凭空声称「无事项」
    store = CalendarStore()
    store.set_events([EVENT])
    config = {"time_awareness": {"upcoming_days": 1}, "calendar": {"enable_calendar": False}}
    assert _collector(config, store).upcoming_text(now=NOW) == "明天 09-26 节假日（周六）"


def test_upcoming_survives_a_failing_sensor_day():
    # 传感器单日抛错若连累整块标签消失，用户只看到「配置开了却没生效」
    config = {
        "time_awareness": {"upcoming_days": 3},
        "calendar": {"enable_calendar": True, "builtin_event_categories": []},
    }
    lines = _collector(config, workday=_FlakyWorkday()).upcoming_text(now=NOW).splitlines()
    assert lines == [
        "明天 09-26 节假日（周六）：无事项",
        "后天 09-27：无事项",
        "大后天 09-28 节假日（周一）：无事项",
    ]


def test_dynamic_context_renders_upcoming_tag():
    # 标签名/渲染分支写错：配置项看着生效，实际什么都没注入
    store = CalendarStore()
    store.set_events([EVENT])
    config = {
        "time_awareness": {"upcoming_days": 1, "time_sensors": []},
        "calendar": {"enable_calendar": True, "builtin_event_categories": []},
    }

    def _service(cfg):
        return TimeContextService(
            config=cfg,
            astrbot_config_provider=lambda: None,
            calendar_store=store,
            last_chat_tracker=FakeLastChatTracker(),
            facts_collector=_collector(cfg, store),
        )

    text = _service(config).build_dynamic(now=NOW, include_last_chat=False)
    assert "<UPCOMING_DAYS>明天 09-26 节假日（周六）：公司团建</UPCOMING_DAYS>" in text
    off = _service({"calendar": {"enable_calendar": True}})
    assert "<UPCOMING_DAYS>" not in off.build_dynamic(now=NOW, include_last_chat=False)
