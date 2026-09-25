"""农历感知：实时计算今日农历；无状态单例，lunar_python 实时查。"""

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

    def collect(self, date: datetime.date) -> tuple[str, dict]:
        """一次计算同时返回 LLM 文本与 WebUI 结构化字段。"""
        empty = {"lunar_year_chip": "", "lunar_month_day": ""}
        try:
            from lunar_python import Solar
        except ImportError:
            logger.warning(f"{tag()} ⚠️ lunar_python 库未安装，农历感知降级")
            return "", empty
        try:
            solar = Solar.fromYmd(date.year, date.month, date.day)
            lunar = solar.getLunar()
            gz = lunar.getYearInGanZhi()
            sx = lunar.getYearShengXiao()
            month_cn = lunar.getMonthInChinese()
            day_cn = lunar.getDayInChinese()
            sx_emoji = _SX_EMOJI.get(sx, sx)
            return (
                f"农历{gz}{sx}年{month_cn}月{day_cn}",
                {
                    "lunar_year_chip": f"{gz}{sx_emoji}年",
                    "lunar_month_day": f"{month_cn}月{day_cn}",
                },
            )
        except Exception as e:
            logger.warning(f"{tag()} ⚠️ LunarSensor 计算异常 {date}: {e}")
            return "", empty

lunar_sensor = LunarSensor()  # 模块级单例
