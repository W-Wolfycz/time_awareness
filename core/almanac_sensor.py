"""黄历感知：实时计算当日干支/冲煞/宜忌；无状态，lunar_python 实时查。"""

import datetime

from ..log import logger, tag


class AlmanacSensor:
    """当日黄历文本（黄历 {干支}日 冲{生肖}煞{方位} 宜:... 忌:...）；失败返回空串。"""

    def collect(self, date: datetime.date) -> tuple[str, dict]:
        """一次计算同时返回 LLM 文本与 WebUI 结构化字段。"""
        empty = {"gz_day": "", "chong_sha": "", "yi": "", "ji": ""}
        try:
            from lunar_python import Solar
        except ImportError:
            logger.warning(f"{tag()} ⚠️ lunar_python 库未安装，黄历感知降级")
            return "", empty
        try:
            solar = Solar.fromYmd(date.year, date.month, date.day)
            lunar = solar.getLunar()
            gz = lunar.getDayInGanZhi()
            chong = lunar.getDayChongShengXiao()
            sha = lunar.getDaySha()
            yi = lunar.getDayYi()
            ji = lunar.getDayJi()
            yi_text = "、".join(yi[:4]) if yi else "无"
            ji_text = "、".join(ji[:4]) if ji else "无"
            return (
                f"黄历 {gz}日 冲{chong}煞{sha} 宜:{yi_text} 忌:{ji_text}",
                {
                    "gz_day": f"{gz}日",
                    "chong_sha": f"冲{chong}煞{sha}",
                    "yi": "、".join((yi or [])[:3]),
                    "ji": "、".join((ji or [])[:3]),
                },
            )
        except Exception as e:
            logger.warning(f"{tag()} ⚠️ AlmanacSensor 计算异常 {date}: {e}")
            return "", empty

almanac_sensor = AlmanacSensor()  # 模块级单例，无状态可直接用
