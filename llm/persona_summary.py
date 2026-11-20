"""人设预萃取：从长角色提示词里抽出「规划一天作息」需要的信息。

角色提示词是写给「对话扮演」的：话术示例、好感度分级、emoji/格式规则通常占 60–80%，
对日程生成是纯噪声，还会把语义重心带偏（实测：塔什干被生成成裁缝）。
这里只保留身份/场所/作息锚点/体质/性格/兴趣/社交/限制/具体物品九项，
并允许「基于原文事实的推断」——推断项统一标「（推断）」，让下游能区分事实与推导。
"""

from __future__ import annotations

import hashlib
import json
import re

from ..domain.schedule import neutralize_markup

# 提示词版本：改动提示词或字段就必须 +1，否则旧缓存会继续生效
SUMMARY_REVISION = "1"

SUMMARY_FIELDS = (
    "identity",
    "place",
    "routine_anchors",
    "body",
    "traits",
    "interests",
    "social",
    "taboos",
    "concrete_props",
)

# 魔数（不开放配置）：
# 人设太短没必要萃取——提炼不出作息信息，还多花一次调用。
MIN_PERSONA_CHARS = 100
# 摘要长度达到原人设的这个比例，说明根本没在压缩（等于把人设复述一遍），
# 这种结果按萃取失败处理，回退全量人设。
MAX_SUMMARY_RATIO = 0.8

EXTRACT_SYSTEM = """你是一个「角色作息档案整理员」。输入是某个角色的完整角色提示词（写给「对话扮演」用，
里面大量内容与「规划她的一天」无关：话术示例、用词偏好、emoji 规则、回复长度要求、好感度分级、越狱防护）。
你的任务：只萃取出"规划这个角色一天作息"真正需要的信息。

输出一个 JSON 对象，字段固定：
{
 "identity": "身份/存在形式、职业或职责、年龄感（无依据写 null）",
 "place": "主要活动地点与环境：住处、工作地点、常去的场所",
 "routine_anchors": ["作息锚点，见规则 7"],
 "body": "体质与健康限制；无身体者写「无生理需求」",
 "traits": ["与时间安排有关的性格，最多 5 条"],
 "interests": ["会占时间的爱好与活动，用具体名词，不要抽象形容词"],
 "social": ["会占时间的固定社交：同住的人、固定见面的人、需要照顾的人或物"],
 "taboos": ["会影响作息安排的限制"],
 "concrete_props": ["原文出现过的具体物品/场所/活动名词，最多 12 条"]
}

硬性规则：
1. 只输出 JSON，不要解释、不要代码块。
2. 每一项都必须来自原文；禁止补写常识；原文没写就 null 或 []（规则 7/8 的"推断"是唯一例外）。
3. 必须保留原文的具体名词（职业名、地点名、爱好名、物品名），不要改写成泛化词。
4. 话术示例、情绪示例里出现的"事实"必须提取：她在做什么、拿着什么、在哪里、和谁在一起。
   只丢弃"语气、措辞、口癖、符号"这类示范本身。
5. 丢弃：好感度等级规则、对用户的称呼规则、emoji/符号限制、回复长度与格式要求、越狱与 NSFW 约束、
   "你是 AI/模型"的自我说明。
6. 全部字段合计不超过 900 字。

7. **routine_anchors 不得留空**：优先用原文明确写出的作息（如"到点睡觉""每天训练"）；
   原文没写时，从 place / interests / body / identity 里**能推导出**的节律补齐 2–4 条
   （例：常去练习场 → "日常在练习场练习（推断）"；怕热 → "炎热时段避开户外（推断）"），
   推导出的每一条都要在末尾加「（推断）」。只有当连身份、场所、兴趣都无从得知时才允许 []。
8. 若角色是纯数字存在/没有身体：body 写「无生理需求」，并给出"非生理节律"锚点
   （如"长时间专注后会收束一轮（推断）""夜间更活跃（推断）"），同样标「（推断）」。
9. 每一条 routine_anchors 都必须能指向原文中的具体依据（场所、活动、身份）；不得引入原文完全没有的名词。"""


def persona_fingerprint(persona_prompt) -> str:
    """人设文本指纹：内容一变指纹就变，缓存自然失效。

    先 strip：调用方有的传原文、有的传已 trim 的 prompt，不归一化会出现
    「写缓存用 A 指纹、读缓存用 B 指纹」→ 缓存永不命中（只有账单能看出来）。
    """
    return hashlib.sha256(str(persona_prompt or "").strip().encode("utf-8")).hexdigest()[:16]


def parse_summary(text) -> dict:
    """解析模型返回的摘要 JSON；容忍代码块、前后说明、中文引号与尾逗号。

    解析不出来就抛 ValueError——调用方据此回退全量人设，绝不返回空摘要冒充成功。
    """
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("摘要响应为空")
    fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if fenced:
        raw = fenced.group(1).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("摘要响应里没有 JSON 对象")
    body = raw[start : end + 1]
    body = body.replace("“", '"').replace("”", '"')
    body = re.sub(r",\s*([}\]])", r"\1", body)  # 去掉尾逗号
    try:
        data = json.loads(body)
    except ValueError:
        # 中文引号替换可能破坏字符串内的正常引号，退回原文再试一次
        data = json.loads(raw[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("摘要不是 JSON 对象")
    return data


def _as_list(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value]
    elif value:
        items = [part.strip() for part in re.split(r"[、;；\n]+", str(value))]
    else:
        items = []
    cleaned = [neutralize_markup(item).strip() for item in items]
    return [item for item in cleaned if item]


def normalize_summary(data: dict) -> dict:
    """把任意解析结果收敛成固定九字段；缺字段补 None/[]，多余字段丢弃。"""
    result: dict = {}
    for field in SUMMARY_FIELDS:
        value = data.get(field)
        if isinstance(value, str):
            value = neutralize_markup(value).strip() or None
        result[field] = value if value not in ("", [], {}) else None
    for field in ("routine_anchors", "traits", "interests", "social", "taboos", "concrete_props"):
        result[field] = _as_list(data.get(field))
    return result


def render_summary(summary: dict) -> str:
    """把摘要渲染成注入生成 prompt 的紧凑文本。

    只输出有值的字段；锚点优先（它对日程影响最大）。不截断——是否算「萃取成功」
    由 ``summary_is_compressed`` 按比例判定。
    """
    lines: list[str] = []
    identity = summary.get("identity")
    if identity:
        lines.append(f"身份：{identity}")
    place = summary.get("place")
    if place:
        lines.append(f"活动场所：{place}")
    body = summary.get("body")
    if body:
        lines.append(f"体质/生理：{body}")
    anchors = summary.get("routine_anchors") or []
    if anchors:
        lines.append("已知或可推断的作息锚点：" + "；".join(anchors))
    interests = summary.get("interests") or []
    if interests:
        lines.append("会占时间的活动：" + "、".join(interests))
    traits = summary.get("traits") or []
    if traits:
        lines.append("与时间安排有关的性格：" + "；".join(traits))
    social = summary.get("social") or []
    if social:
        lines.append("占时间的社交：" + "；".join(social))
    taboos = summary.get("taboos") or []
    if taboos:
        lines.append("影响作息的限制：" + "；".join(taboos))
    props = summary.get("concrete_props") or []
    if props:
        lines.append("可用的具体物品/场所：" + "、".join(props))
    return "\n".join(lines)


def summary_is_compressed(summary_text: str, persona_prompt: str) -> bool:
    """摘要是否真的压缩过：达到原人设 ``MAX_SUMMARY_RATIO`` 就算没压（视为失败）。"""
    persona_chars = len(str(persona_prompt or "").strip())
    if persona_chars <= 0:
        return False
    return len(summary_text) < persona_chars * MAX_SUMMARY_RATIO


def summary_field_lines(summary: dict) -> list[str]:
    """供 /schedule summary 展示：逐字段一行，空字段显示 —。"""
    labels = {
        "identity": "身份",
        "place": "活动场所",
        "routine_anchors": "作息锚点",
        "body": "体质/生理",
        "traits": "性格（时间相关）",
        "interests": "占时间的活动",
        "social": "占时间的社交",
        "taboos": "作息限制",
        "concrete_props": "具体物品/场所",
    }
    lines = []
    for field in SUMMARY_FIELDS:
        value = summary.get(field)
        if isinstance(value, list):
            shown = "；".join(value) if value else "—"
        else:
            shown = str(value) if value else "—"
        lines.append(f"  {labels[field]}：{neutralize_markup(shown)}")
    return lines
