"""
内置现实日历事件生成器

按 6 个分类生成常用的现实日历事件：

- ``legal_holiday``：法定节假日（含调休），数据源 ``chinese_calendar`` 库
- ``traditional``：传统农历节日（元宵、七夕、重阳、腊八、除夕等）
- ``political``：政治纪念日（建党、建军、抗战胜利等，固定公历）
- ``international``：国际/西方节日（情人节、圣诞节、母亲节、复活节等）
- ``solar_term``：二十四节气
- ``almanac``：黄历（每日干支/冲煞/宜忌，一年 365/366 条）

设计要点：

- **跨年再生稳定 ID**：每条事件 ID 为 ``builtin:{category}:{MM-DD}:{text}``，
  同一年内唯一，跨 regen 稳定，便于黑名单按 ID 记录。
- **正日子 vs 假期**：法定节假日的正日子当天显示 ``X 节``，相邻调休日显示
  ``X 节假期``。归属判定直接用 ``chinese_calendar.get_holiday_detail`` 返回的
  ``holiday_name``（连休场景下「先休后过节」也能正确归属）。
- **节气跨年问题**：``Lunar.getJieQiTable`` 返回的表可能跨公历年（农历年跨公历），
  本模块改用 ``Solar.getLunar().getJieQi()`` 逐日判定，只取当年节气。
- **黄历的量级**：与其他「特定日期事件」分类不同，黄历每日一条（365+ 条/年），
  默认关闭，按需启用；text 截断宜/忌前 4 项避免过长。
"""

import datetime
from typing import Optional

from ..log import logger, tag



# ==================== 类目常量 ====================

CATEGORY_LEGAL = "legal_holiday"
CATEGORY_TRADITIONAL = "traditional"
CATEGORY_POLITICAL = "political"
CATEGORY_INTERNATIONAL = "international"
CATEGORY_SOLAR_TERM = "solar_term"
CATEGORY_ALMANAC = "almanac"

ALL_CATEGORIES = (
    CATEGORY_LEGAL,
    CATEGORY_TRADITIONAL,
    CATEGORY_POLITICAL,
    CATEGORY_INTERNATIONAL,
    CATEGORY_SOLAR_TERM,
    CATEGORY_ALMANAC,
)


# ==================== 事件清单 ====================

# 法定节假日英文名 → 中文名
# chinese_calendar 库返回的 holiday_name 是英文
LEGAL_NAME_CN = {
    "New Year's Day": "元旦",
    "Spring Festival": "春节",
    "Tomb-sweeping Day": "清明节",
    "Labour Day": "劳动节",
    "Dragon Boat Festival": "端午节",
    "Mid-autumn Festival": "中秋节",
    "National Day": "国庆节",
}

# 传统节日（农历月、日；除夕特殊处理）
TRADITIONAL_LUNAR = [
    ("元宵节", 1, 15),
    ("龙抬头", 2, 2),
    ("上巳节", 3, 3),
    ("七夕节", 7, 7),
    ("中元节", 7, 15),
    ("重阳节", 9, 9),
    ("寒衣节", 10, 1),
    ("下元节", 10, 15),
    ("腊八节", 12, 8),
    ("北方小年", 12, 23),
    ("南方小年", 12, 24),
]

# 政治纪念日（公历）
POLITICAL_DATES = [
    ("植树节", 3, 12),
    ("五四青年节", 5, 4),
    ("教师节", 9, 10),
    ("建党节", 7, 1),
    ("建军节", 8, 1),
    ("抗战胜利纪念日", 9, 3),
    ("烈士纪念日", 9, 30),
    ("国家公祭日", 12, 13),
]

# 国际/西方节日（固定公历日期）
INTERNATIONAL_FIXED = [
    ("情人节", 2, 14),
    ("国际妇女节", 3, 8),
    ("愚人节", 4, 1),
    ("国际儿童节", 6, 1),
    ("万圣节前夜", 10, 31),
    ("万圣节", 11, 1),
    ("平安夜", 12, 24),
    ("圣诞节", 12, 25),
]

# 国际/西方节日（按月第 N 个周X）
# (名称, 月, 周几, 第几个)
INTERNATIONAL_COMPUTED = [
    ("母亲节", 5, "Sunday", 2),
    ("父亲节", 6, "Sunday", 3),
    ("感恩节", 11, "Thursday", 4),
]

# 二十四节气
SOLAR_TERMS = [
    "立春", "雨水", "惊蛰", "春分", "清明", "谷雨",
    "立夏", "小满", "芒种", "夏至", "小暑", "大暑",
    "立秋", "处暑", "白露", "秋分", "寒露", "霜降",
    "立冬", "小雪", "大雪", "冬至", "小寒", "大寒",
]


# ==================== 工具函数 ====================

def _make_event(year: int, month: int, day: int, text: str, category: str) -> dict:
    """构造 builtin 事件 dict。

    ID 跨 regen 稳定（同年同 category+月日+文本一致），便于黑名单按 ID 记录。
    """
    return {
        "id": f"builtin:{category}:{month:02d}-{day:02d}:{text}",
        "category": category,
        "year": year,
        "month": month,
        "day": day,
        "text": text,
        "source": "builtin",
    }


def _lunar_to_solar(year: int, lunar_month: int, lunar_day: int, *, silent: bool = False) -> Optional[datetime.date]:
    """农历转公历（非闰月）。失败返回 None。

    silent=True 用于「探索性调用」（如除夕先试 30 再试 29），失败是预期路径，不打 warning。
    """
    try:
        from lunar_python import Lunar
        lunar = Lunar.fromYmd(year, lunar_month, lunar_day)
        solar = lunar.getSolar()
        return datetime.date(solar.getYear(), solar.getMonth(), solar.getDay())
    except Exception as e:
        if not silent:
            logger.warning(f"{tag()} ⚠️ 农历 {year}/{lunar_month}/{lunar_day} 转公历失败: {e}")
        return None


def _nth_weekday_of_month(year: int, month: int, weekday_name: str, n: int) -> datetime.date:
    """计算 year 年 month 月第 n 个 weekday_name 的日期。

    weekday_name: "Monday".."Sunday"。n 从 1 开始。
    """
    weekdays = {
        "Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3,
        "Friday": 4, "Saturday": 5, "Sunday": 6,
    }
    target_wd = weekdays[weekday_name]
    d = datetime.date(year, month, 1)
    while d.weekday() != target_wd:
        d += datetime.timedelta(days=1)
    d += datetime.timedelta(weeks=n - 1)
    return d


def _gauss_easter(year: int) -> datetime.date:
    """高斯算法计算复活节（西方教会）。

    返回公历日期。适用于 1583 年之后的年份。
    """
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return datetime.date(year, month, day)


# ==================== 分类生成器 ====================

def gen_legal_holidays(year: int) -> list:
    """生成法定节假日事件（含调休）。

    遍历整年，对每个 ``chinese_calendar`` 判定为 holiday 的日期，根据
    ``holiday_name`` 区分：
    - 正日子（如国庆 10/1、中秋 农历8/15）→ ``X 节``
    - 非正日子但属于该节日调休 → ``X 节假期``

    春节正日子为农历正月初一至初三（共 3 天）；国庆正日子为 10/1-10/3。
    其他节日正日子单天。中秋/端午按农历算，清明按节气算。
    """
    try:
        import chinese_calendar as cc
    except ImportError:
        logger.warning(f"{tag()} ⚠️ chinese_calendar 库未安装，跳过法定节假日生成")
        return []

    # 计算各节日正日子
    sf_first = _lunar_to_solar(year, 1, 1)
    sf_second = _lunar_to_solar(year, 1, 2)
    sf_third = _lunar_to_solar(year, 1, 3)
    dq = _lunar_to_solar(year, 5, 5)  # 端午
    ma = _lunar_to_solar(year, 8, 15)  # 中秋
    qingming_dates = _find_solar_term_date(year, "清明")

    main_day_map = {
        "New Year's Day": {datetime.date(year, 1, 1)},
        "Spring Festival": {d for d in [sf_first, sf_second, sf_third] if d is not None},
        "Tomb-sweeping Day": set(qingming_dates),
        "Labour Day": {datetime.date(year, 5, 1)},
        "Dragon Boat Festival": {dq} if dq else set(),
        "Mid-autumn Festival": {ma} if ma else set(),
        "National Day": {datetime.date(year, 10, 1), datetime.date(year, 10, 2), datetime.date(year, 10, 3)},
    }

    events = []
    d = datetime.date(year, 1, 1)
    end = datetime.date(year, 12, 31)
    while d <= end:
        try:
            is_hol, name = cc.get_holiday_detail(d)
        except NotImplementedError:
            # chinese_calendar 未覆盖该年份
            logger.warning(f"{tag()} ⚠️ chinese_calendar 未覆盖 {year} 年，跳过法定节假日")
            return []
        except Exception:
            is_hol, name = False, None

        if is_hol and name:
            cn = LEGAL_NAME_CN.get(name)
            if cn is None:
                # 库返回了未识别的节日名（如特殊纪念日），跳过
                d += datetime.timedelta(days=1)
                continue
            is_main = d in main_day_map.get(name, set())
            text = cn if is_main else f"{cn}假期"
            events.append(_make_event(year, d.month, d.day, text, CATEGORY_LEGAL))
        d += datetime.timedelta(days=1)
    return events


def gen_traditional(year: int) -> list:
    """生成传统农历节日事件（公历落在 year 年的）。

    农历腊月（12 月）的节日公历通常落到下一年的 1-2 月，要拿「公历 year 年」
    的腊八/小年/除夕，需要取「农历 year-1 年」的腊月。本函数同时尝试农历
    year 与 year-1 两个年份，取公历年份匹配 year 的。
    """
    events = []
    for text, lm, ld in TRADITIONAL_LUNAR:
        for lunar_year in (year, year - 1):
            solar = _lunar_to_solar(lunar_year, lm, ld)
            if solar is not None and solar.year == year:
                events.append(_make_event(year, solar.month, solar.day, text, CATEGORY_TRADITIONAL))
                break

    # 除夕：腊月最后一天（29 或 30），同样尝试两个农历年份
    # 30 是大月尝试、29 是小月兜底——都是预期路径，用 silent 避免误报 warning
    for lunar_year in (year, year - 1):
        for last_day in (30, 29):
            solar = _lunar_to_solar(lunar_year, 12, last_day, silent=True)
            if solar is not None and solar.year == year:
                events.append(_make_event(year, solar.month, solar.day, "除夕", CATEGORY_TRADITIONAL))
                break
        else:
            continue
        break  # 找到一个就停
    return events


def gen_political(year: int) -> list:
    """生成政治纪念日事件。"""
    return [
        _make_event(year, m, d, text, CATEGORY_POLITICAL)
        for text, m, d in POLITICAL_DATES
    ]


def gen_international(year: int) -> list:
    """生成国际/西方节日事件（固定 + 计算 + 复活节）。"""
    events = []
    for text, m, d in INTERNATIONAL_FIXED:
        events.append(_make_event(year, m, d, text, CATEGORY_INTERNATIONAL))
    for text, m, weekday, n in INTERNATIONAL_COMPUTED:
        d = _nth_weekday_of_month(year, m, weekday, n)
        events.append(_make_event(year, d.month, d.day, text, CATEGORY_INTERNATIONAL))
    # 复活节
    try:
        easter = _gauss_easter(year)
        events.append(_make_event(year, easter.month, easter.day, "复活节", CATEGORY_INTERNATIONAL))
    except Exception as e:
        logger.warning(f"{tag()} ⚠️ 复活节计算失败 {year}: {e}")
    return events


def gen_solar_terms(year: int) -> list:
    """生成年内二十四节气事件。

    用 ``Solar.getLunar().getJieQi()`` 逐日判定，避免 ``getJieQiTable`` 的跨年混淆。
    每年 365 天循环开销小，可接受。
    """
    try:
        from lunar_python import Solar
    except ImportError:
        logger.warning(f"{tag()} ⚠️ lunar_python 库未安装，跳过节气生成")
        return []

    target = set(SOLAR_TERMS)
    events = []
    d = datetime.date(year, 1, 1)
    end = datetime.date(year, 12, 31)
    while d <= end:
        solar = Solar.fromYmd(d.year, d.month, d.day)
        lunar = solar.getLunar()
        jq = lunar.getJieQi()
        if jq and jq in target:
            events.append(_make_event(year, d.month, d.day, jq, CATEGORY_SOLAR_TERM))
        d += datetime.timedelta(days=1)
    return events


def gen_almanac(year: int) -> list:
    """生成黄历每日事件（一年 365/366 条）。

    每日一条，包含干支日、冲煞（生肖+方位）、宜、忌。信息量较大但每日才
    一条，注入 ``{calendar_today}`` 时可控（约 40 字/天）。

    - ``getDayInGanZhi()`` → 干支日（如 "辛未"）
    - ``getDayChongShengXiao()`` → 冲的生肖（"牛" 比 "丑" 直观）
    - ``getDaySha()`` → 煞方（"西"）
    - ``getDayYi()`` / ``getDayJi()`` → 宜 / 忌列表（取前 4 项截断，避免 text 过长）
    """
    try:
        from lunar_python import Solar
    except ImportError:
        logger.warning(f"{tag()} ⚠️ lunar_python 库未安装，跳过黄历生成")
        return []

    events = []
    d = datetime.date(year, 1, 1)
    end = datetime.date(year, 12, 31)
    while d <= end:
        solar = Solar.fromYmd(d.year, d.month, d.day)
        lunar = solar.getLunar()
        gz = lunar.getDayInGanZhi()
        chong = lunar.getDayChongShengXiao()
        sha = lunar.getDaySha()
        yi = lunar.getDayYi()
        ji = lunar.getDayJi()
        yi_text = "、".join(yi[:4]) if yi else "无"
        ji_text = "、".join(ji[:4]) if ji else "无"
        text = f"黄历 {gz}日 冲{chong}煞{sha} 宜:{yi_text} 忌:{ji_text}"
        events.append(_make_event(year, d.month, d.day, text, CATEGORY_ALMANAC))
        d += datetime.timedelta(days=1)
    return events


def _find_solar_term_date(year: int, term_name: str) -> list:
    """返回 year 年 term_name 节气对应的公历日期列表（一般 1 个，闰节等情况可能多个）。"""
    try:
        from lunar_python import Solar
    except ImportError:
        return []
    dates = []
    d = datetime.date(year, 1, 1)
    end = datetime.date(year, 12, 31)
    while d <= end:
        solar = Solar.fromYmd(d.year, d.month, d.day)
        if solar.getLunar().getJieQi() == term_name:
            dates.append(d)
        d += datetime.timedelta(days=1)
    return dates


# ==================== 总入口 ====================

# 分类 → 生成函数
_GENERATORS = {
    CATEGORY_LEGAL: gen_legal_holidays,
    CATEGORY_TRADITIONAL: gen_traditional,
    CATEGORY_POLITICAL: gen_political,
    CATEGORY_INTERNATIONAL: gen_international,
    CATEGORY_SOLAR_TERM: gen_solar_terms,
    CATEGORY_ALMANAC: gen_almanac,
}


def generate_for_year(year: int, enabled_categories: list) -> list:
    """生成指定年份、指定分类的内置事件列表。

    Args:
        year: 公历年份
        enabled_categories: 启用的分类列表（来自配置开关）

    Returns:
        事件 dict 列表，按 (month, day, category) 排序
    """
    all_events = []
    for cat in enabled_categories:
        gen = _GENERATORS.get(cat)
        if gen is None:
            continue
        try:
            events = gen(year)
            all_events.extend(events)
            logger.debug(f"{tag()} 内置[{cat}] 生成 {len(events)} 条")
        except Exception as e:
            logger.error(f"{tag()} ❌ 内置[{cat}] 生成失败: {e}")

    # 排序：月、日、分类优先级（法定 > 传统 > 节气 > 政治 > 国际 > 黄历）
    priority = {
        CATEGORY_LEGAL: 0,
        CATEGORY_TRADITIONAL: 1,
        CATEGORY_SOLAR_TERM: 2,
        CATEGORY_POLITICAL: 3,
        CATEGORY_INTERNATIONAL: 4,
        CATEGORY_ALMANAC: 5,
    }
    all_events.sort(key=lambda e: (e["month"], e["day"], priority.get(e["category"], 9), e["text"]))

    # 清明既是节气又是法定节假日，是唯一的语义重叠点。
    # 当两类同时启用时，剔除节气「清明」（保留更正式的法定「清明节」）。
    if CATEGORY_LEGAL in enabled_categories and CATEGORY_SOLAR_TERM in enabled_categories:
        legal_qingming = {
            (e["month"], e["day"])
            for e in all_events
            if e["category"] == CATEGORY_LEGAL and e["text"] == "清明节"
        }
        if legal_qingming:
            all_events = [
                e for e in all_events
                if not (
                    e["category"] == CATEGORY_SOLAR_TERM
                    and e["text"] == "清明"
                    and (e["month"], e["day"]) in legal_qingming
                )
            ]
    return all_events
