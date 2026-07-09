"""
工作日性质感知——实时判定今天是工作日/周末/调休工作日/节假日。

与 ``builtin_events`` 的 ``legal_holiday`` 分类**职责分离**：

- ``legal_holiday`` 是「事件清单」（今天是 X 节），进 ``<TODAY_EVENTS>``
- ``WorkdaySensor`` 是「日历性质元信息」（今天是工作日/周末），进 ``<WORKDAY_STATE>``

无状态：``chinese_calendar`` 实时查，不需要 regen/跨年/store 机制——
工作日判定每年都不同（调休规则可能调整），用预生成反而会过期。
"""

import datetime

from ..log import logger, tag


class WorkdaySensor:
    """判定日期性质。

    返回 ``(kind, label)``：

    - ``('workday', '工作日（周X）')``
    - ``('weekend', '周末（周X）')``
    - ``('adjusted', '调休工作日（周X）')``  ← 周末但需上班
    - ``('holiday', '节假日（周X）')``       ← 法定节假日（不重复节日名，由 ``<TODAY_EVENTS>`` 提供）
    - ``('unknown', '周X')``                 ← ``chinese_calendar`` 未覆盖年份时的降级
    """

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
