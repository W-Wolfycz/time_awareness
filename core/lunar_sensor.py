"""农历感知——实时计算今日农历日期。

与 ``workday_sensor`` 同型：无状态单例，``lunar_python`` 实时查。
配置开关：``time_awareness.lunar_state_enabled``（默认开）。

返回格式：

- ``describe``：用于 LLM 注入，``农历{干支年}{生肖}年{中文月}月{中文日}``，例 ``农历丙午马年六月初五``
- ``describe_detailed``：用于 WebUI，拆成 ``lunar_year_chip``（``丙午🐴年``）/ ``lunar_month_day``（``五月廿六``），
  其中生肖转 emoji 方便 chip 内呈现
"""

import datetime

from ..log import logger, tag


# 生肖字符 → emoji 映射（lunar_python.getYearShengXiao 返回中文生肖）
_SX_EMOJI = {
    "鼠": "🐭", "牛": "🐮", "虎": "🐯", "兔": "🐰",
    "龙": "🐉", "蛇": "🐍", "马": "🐴", "羊": "🐑",
    "猴": "🐒", "鸡": "🐔", "狗": "🐶", "猪": "🐷",
}


class LunarSensor:
    """返回今日农历中文描述。"""

    def describe(self, date: datetime.date) -> str:
        """LLM 注入用的完整农历字符串。"""
        try:
            from lunar_python import Solar
        except ImportError:
            logger.warning(f"{tag()} ⚠️ lunar_python 库未安装，农历感知降级")
            return ""
        try:
            solar = Solar.fromYmd(date.year, date.month, date.day)
            lunar = solar.getLunar()
            gz = lunar.getYearInGanZhi()
            sx = lunar.getYearShengXiao()
            month_cn = lunar.getMonthInChinese()
            day_cn = lunar.getDayInChinese()
            return f"农历{gz}{sx}年{month_cn}月{day_cn}"
        except Exception as e:
            logger.warning(f"{tag()} ⚠️ LunarSensor 计算异常 {date}: {e}")
            return ""

    def describe_detailed(self, date: datetime.date) -> dict:
        """WebUI 用的结构化农历数据。

        返回 dict：

        - ``lunar_year_chip``：``{干支}{生肖 emoji}年``，例 ``丙午🐴年``
        - ``lunar_month_day``：``{中文月}月{中文日}``，例 ``五月廿六``

        异常或库未安装时返回 ``{"lunar_year_chip": "", "lunar_month_day": ""}``，
        前端据此省略 chip。
        """
        empty = {"lunar_year_chip": "", "lunar_month_day": ""}
        try:
            from lunar_python import Solar
        except ImportError:
            return empty
        try:
            solar = Solar.fromYmd(date.year, date.month, date.day)
            lunar = solar.getLunar()
            gz = lunar.getYearInGanZhi()
            sx = lunar.getYearShengXiao()
            sx_emoji = _SX_EMOJI.get(sx, sx)  # 未命中映射则保留原字符
            month_cn = lunar.getMonthInChinese()
            day_cn = lunar.getDayInChinese()
            return {
                "lunar_year_chip": f"{gz}{sx_emoji}年",
                "lunar_month_day": f"{month_cn}月{day_cn}",
            }
        except Exception as e:
            logger.warning(f"{tag()} ⚠️ LunarSensor.describe_detailed 异常 {date}: {e}")
            return empty


lunar_sensor = LunarSensor()  # 模块级单例
