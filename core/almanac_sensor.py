"""黄历感知——实时计算当日干支/冲煞/宜忌。

从 builtin_events 剥离（v1.7.0 重大迁移）：

- 旧路径：``gen_almanac`` 一年生成 365 条 → ``builtin_events.yaml`` → ``<TODAY_EVENTS>``
- 新路径：``AlmanacSensor`` 实时计算 → ``<ALMANAC_STATE>``（独立标签）

职责分离：

- 「事件」分类（节日/节气等）是「今天是 X 节」，进 ``<TODAY_EVENTS>``
- 黄历是「日历性质元信息」（每日都有一条，性质稳定），进独立 ``<ALMANAC_STATE>``

无状态：``lunar_python`` 实时查，不需要 regen/跨年/store 机制，与 ``workday_sensor`` 同型。
配置开关：``time_awareness.almanac_enabled``。
"""

import datetime

from ..log import logger, tag


class AlmanacSensor:
    """返回当日黄历文本。

    格式：``黄历 {干支}日 冲{生肖}煞{方位} 宜:{...} 忌:{...}``
    例：``黄历 辛未日 冲牛煞西 宜:嫁娶、祭祀、祈福、求嗣 忌:安葬、开生坟、行丧``

    失败/库未安装返回空串（调用方据此省略 XML 标签）。
    """

    def describe(self, date: datetime.date) -> str:
        try:
            from lunar_python import Solar
        except ImportError:
            logger.warning(f"{tag()} ⚠️ lunar_python 库未安装，黄历感知降级")
            return ""
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
            return f"黄历 {gz}日 冲{chong}煞{sha} 宜:{yi_text} 忌:{ji_text}"
        except Exception as e:
            logger.warning(f"{tag()} ⚠️ AlmanacSensor 计算异常 {date}: {e}")
            return ""

    def describe_detailed(self, date: datetime.date) -> dict:
        """WebUI 用的拆分字段。

        返回 dict：

        - ``gz_day``：``{干支}日``，例 ``乙酉日``
        - ``chong_sha``：``冲{生肖}煞{方位}``，例 ``冲兔煞东``
        - ``yi``：宜事项 ``、`` 拼接，取前 3 项避免 chip 过长；无则空串
        - ``ji``：忌事项 ``、`` 拼接，取前 3 项；无则空串

        异常或库未安装时返回全空 dict，前端据此省略整行 chip。
        """
        empty = {"gz_day": "", "chong_sha": "", "yi": "", "ji": ""}
        try:
            from lunar_python import Solar
        except ImportError:
            return empty
        try:
            solar = Solar.fromYmd(date.year, date.month, date.day)
            lunar = solar.getLunar()
            gz = lunar.getDayInGanZhi()
            chong = lunar.getDayChongShengXiao()
            sha = lunar.getDaySha()
            yi_list = lunar.getDayYi() or []
            ji_list = lunar.getDayJi() or []
            return {
                "gz_day": f"{gz}日",
                "chong_sha": f"冲{chong}煞{sha}",
                "yi": "、".join(yi_list[:3]),
                "ji": "、".join(ji_list[:3]),
            }
        except Exception as e:
            logger.warning(f"{tag()} ⚠️ AlmanacSensor.describe_detailed 异常 {date}: {e}")
            return empty


almanac_sensor = AlmanacSensor()  # 模块级单例，无状态可直接用
