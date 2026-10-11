"""工作日性质感知：实时判定工作日/周末/调休/节假日；不预生成（调休规则每年变），仅按年去重未覆盖告警。"""

import datetime

from ..log import logger, tag


class WorkdaySensor:
    """返回 (kind, label)：workday/weekend/adjusted/holiday；库未覆盖年份降级 unknown。"""

    _WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

    def __init__(self):
        # 未覆盖年份的告警按年去重：未来 N 天预告逐日调用，否则每次请求刷 N 条
        self._warned_years: set[int] = set()

    def _warn_uncovered_year(self, year: int) -> None:
        if year in self._warned_years:
            return
        self._warned_years.add(year)
        logger.warning(f"{tag()} ⚠️ chinese_calendar 未覆盖 {year} 年，降级为周X")

    def classify(self, date: datetime.date) -> tuple[str, str]:
        weekday_cn = self._WEEKDAY_CN[date.weekday()]
        try:
            import chinese_calendar as cc
            # chinese_calendar 把普通周末也算 holiday，只有带节日名才是法定节假日
            is_holiday, holiday_name = cc.get_holiday_detail(date)
            is_workday = cc.is_workday(date)
            if is_holiday and holiday_name:
                return ("holiday", f"节假日（{weekday_cn}）")
            if is_holiday:
                return ("weekend", f"周末（{weekday_cn}）")
            if is_workday and date.weekday() >= 5:
                return ("adjusted", f"调休工作日（{weekday_cn}）")
            if is_workday:
                return ("workday", f"工作日（{weekday_cn}）")
            return ("unknown", weekday_cn)
        except NotImplementedError:
            self._warn_uncovered_year(date.year)
            return ("unknown", weekday_cn)
        except ImportError:
            return ("unknown", weekday_cn)
        except Exception as e:
            logger.warning(f"{tag()} ⚠️ WorkdaySensor 判定异常 {date}: {e}")
            return ("unknown", weekday_cn)


workday_sensor = WorkdaySensor()
