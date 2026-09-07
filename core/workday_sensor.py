"""工作日性质感知：实时判定工作日/周末/调休/节假日；无状态（调休规则每年变，不预生成）。"""

import datetime

from ..log import logger, tag


class WorkdaySensor:
    """返回 (kind, label)：workday/weekend/adjusted/holiday；库未覆盖年份降级 unknown。"""

    _WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

    def classify(self, date: datetime.date) -> tuple[str, str]:
        weekday_cn = self._WEEKDAY_CN[date.weekday()]
        try:
            import chinese_calendar as cc
            is_hol = cc.is_holiday(date)
            is_workday = cc.is_workday(date)
            if is_hol:
                return ("holiday", f"节假日（{weekday_cn}）")
            if is_workday and date.weekday() >= 5:
                return ("adjusted", f"调休工作日（{weekday_cn}）")
            if is_workday:
                return ("workday", f"工作日（{weekday_cn}）")
            return ("weekend", f"周末（{weekday_cn}）")
        except NotImplementedError:
            logger.warning(f"{tag()} ⚠️ chinese_calendar 未覆盖 {date.year}，降级为周X")
            return ("unknown", weekday_cn)
        except ImportError:
            return ("unknown", weekday_cn)
        except Exception as e:
            logger.warning(f"{tag()} ⚠️ WorkdaySensor 判定异常 {date}: {e}")
            return ("unknown", weekday_cn)


workday_sensor = WorkdaySensor()
