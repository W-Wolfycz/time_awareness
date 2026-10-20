"""「AI 改写日程」工具后端：把 LLM 的工具调用落到快照用户改写层。

复用 WebUI 同一套服务端校验、冻结点与并发锁（``DailyScheduleAdminService``），
只额外负责：可用性/权限判定、日期解析、草稿构造与给模型的可读结果。
"""

from __future__ import annotations

import datetime
from typing import Any, Callable

from ..domain.schedule import MINUTES_PER_DAY, ai_daily_config, neutralize_markup
from ..domain.schedule_edit import (
    SLOT_WRITE_ACTIONS,
    apply_slot_edit,
    resolve_target_date,
)
from ..log import logger, tag
from .daily_schedule_admin_service import (
    ScheduleNotFound,
    SchedulePersistenceError,
    ScheduleSaveConflict,
)


TOOL_NAME = "daily_schedule"
TOOL_ARG_NAMES = ("action", "date", "user_slot_id", "name", "start", "end", "state")
ACTIONS = ("list",) + SLOT_WRITE_ACTIONS

_ACTION_LABELS = {"add": "新增时段", "update": "修改时段", "delete": "删除时段"}

_STATUS_PASSED = "已过"
_STATUS_EDITABLE = "可改"
_STATUS_OVERWRITABLE = "可覆写"

_STATE_PREVIEW_LENGTH = 30

TOOL_DESCRIPTION = """查看或改写角色自己的日程（今天/明天）。

list 返回的每段都带状态：
- 已过：时间已经过去，不能改；
- 可改：可以 update / delete，要带段上的 id；
- 可覆写：不能直接改，但可以用 add 在同一时间段盖住。

写入时若起点刚好被冻结点越过（请求在路上耽搁了），会自动从冻结点之后开始，不报错。

用户明确要调整行程、或对话里确实发生了影响当天/次日安排的事时才写；state 写角色在该时段的状态与语气
（第一人称、可感的现场感），不是给用户的提醒或待办；name 是短标签。一次只处理本轮提到的变化，不要重排
整天，也不要为同一时间段重复写入。写入后以角色口吻自然回应，不要复述日程表或内部机制；被拒绝时不要
重复调用、也不要声称已改。"""

TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(ACTIONS),
            "description": "list=查看日程；add=新增一段；update=修改一段；delete=删除一段",
        },
        "date": {
            "type": "string",
            "description": "目标日期：留空=今天，可填 今天/明天/后天 或 YYYY-MM-DD",
        },
        "user_slot_id": {
            "type": "string",
            "description": "update/delete 必填：list 里「可改」段的 id（其余段没有 id）",
        },
        "name": {
            "type": "string",
            "description": "时段短标签（不超过 24 字）；add 必填，update 选填",
        },
        "start": {
            "type": "string",
            "description": "开始时间 HH:MM；add 必填，update 选填",
        },
        "end": {
            "type": "string",
            "description": "结束时间 HH:MM（可填 24:00，须晚于 start）；add 必填，update 选填",
        },
        "state": {
            "type": "string",
            "description": "该时段的状态描述（不超过 500 字）；add 必填，update 选填",
        },
    },
    "required": ["action"],
}


class DailyScheduleToolService:
    """「AI 改写日程」工具的服务层。"""

    def __init__(
        self,
        *,
        config: dict,
        admin_service,
        daily_schedule_service,
        now_provider: Callable[[], Any] | None = None,
    ):
        self.config = config
        self.admin_service = admin_service
        self.daily_schedule_service = daily_schedule_service
        self._now_provider = now_provider

    # ==================== 可用性 ====================

    def allow_ai_edit(self) -> bool:
        return bool(ai_daily_config(self.config).get("allow_ai_edit", False))

    def edit_requires_admin(self) -> bool:
        return bool(ai_daily_config(self.config).get("edit_requires_admin", False))

    def available(self) -> bool:
        """本工具自身的前置条件：改写开关打开且 AI 每日日程可用（否则没有可改写的快照）。

        注入侧还要求 `time_awareness.time_guidance_enabled`（见 `main.py`），
        目标日期是否存在快照则在每次调用时判定。
        """
        return self.allow_ai_edit() and self.daily_schedule_service.enabled()

    def _now(self):
        if self._now_provider is not None:
            return self._now_provider()
        return self.daily_schedule_service.time_context.now()

    @staticmethod
    def _is_admin(event) -> bool:
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    # ==================== 渲染 ====================

    @staticmethod
    def _safe(value) -> str:
        """工具结果直接进模型上下文（不经 ``build_dynamic``），快照文本必须自己中和。"""
        return neutralize_markup(str(value or ""))

    @staticmethod
    def _slot_status(slot: dict) -> str:
        """每段一个状态：已过（动不了）/ 可改（update·delete）/ 可覆写（只能 add 盖住）。

        模型不需要知道分层、冻结点这些内部概念，只按这个状态决定动作；跨冻结点的段只允许改结束时间，
        由服务端用错误消息纠正，不在状态里再分叉。
        """
        origin = str(slot.get("origin", "") or "")
        if origin == "executed" or str(slot.get("edit_mode", "") or "") == "finished":
            return _STATUS_PASSED
        if origin != "user":
            return _STATUS_OVERWRITABLE
        slot_id = neutralize_markup(str(slot.get("user_slot_id", "") or ""))
        return f"{_STATUS_EDITABLE} id={slot_id}" if slot_id else _STATUS_EDITABLE

    def render_day(self, detail: dict | None, target: datetime.date, now) -> str:
        """把快照详情渲染成给模型看的紧凑日程表（每段带一个状态）。"""
        if detail is None:
            return f"[{target.isoformat()} 日程] 快照已不可读"
        slots = [slot for slot in (detail.get("slots") or []) if isinstance(slot, dict)]
        lines = [
            f"[{target.isoformat()} 日程 · {len(slots)} 段 · 现在 {now.strftime('%H:%M')} · "
            f"冻结点 {self._safe(detail.get('cutoff', ''))}]",
        ]
        if not slots:
            lines.append("  （当天没有时段）")
        for slot in slots:
            state = self._safe(slot.get("state", ""))
            preview = (
                state
                if len(state) <= _STATE_PREVIEW_LENGTH
                else state[:_STATE_PREVIEW_LENGTH] + "…"
            )
            lines.append(
                f"  {self._safe(slot.get('start', ''))}-{self._safe(slot.get('end', ''))} "
                f"{self._slot_status(slot)} [{self._safe(slot.get('name', ''))}] {preview}".rstrip()
            )
        return "\n".join(lines)

    # ==================== 工具入口 ====================

    async def handle(
        self,
        *,
        session: str,
        event=None,
        action: str = "",
        date: str = "",
        user_slot_id: str = "",
        name: str = "",
        start: str = "",
        end: str = "",
        state: str = "",
    ) -> str:
        """执行一次工具调用；已知失败均转成给模型的可读结果（调用方另加异常兜底）。"""
        if not self.available():
            return "当前未启用 AI 每日日程，无法改写日程；不要声称已经修改。"

        act = str(action or "").strip().lower()
        if act not in ACTIONS:
            return f"action 非法，只能是：{'、'.join(ACTIONS)}。本次未做任何修改。"
        # 只读查看不设门槛（模型本来就能看到今日安排摘要）；写入才受管理员开关约束
        requires_admin = act in SLOT_WRITE_ACTIONS and self.edit_requires_admin()
        if requires_admin and not self._is_admin(event):
            logger.info(f"{tag(event)} 🚫 AI 改写日程被拒：发送者不是管理员")
            return "已拒绝：仅管理员可以改写日程。不要声称已经修改，也不要解释内部机制。"

        session = str(session or "").strip()
        if not session:
            return "缺少会话信息，本次未做任何修改。"

        now = self._now()
        target = resolve_target_date(date, now.date())
        if target is None:
            return (
                f"日期无法识别（今天是 {now.date().isoformat()}）："
                "可填 今天/明天/后天 或 YYYY-MM-DD。本次未做任何修改。"
            )
        if target < now.date():
            return "过去的日期不能改写。本次未做任何修改。"

        try:
            persona_hash = await self.daily_schedule_service.register_session_async(
                session, trigger=False, event=event
            )
        except Exception as exc:
            logger.warning(
                f"{tag(event)} ⚠️ AI 改写日程 Persona 解析异常: {type(exc).__name__}"
            )
            persona_hash = ""
        if not persona_hash:
            return "无法确定当前会话的 Persona，本次未做任何修改。"

        timezone = self.daily_schedule_service._timezone_key(now)
        detail = self.admin_service.get_detail(persona_hash, target, timezone, now=now)
        if detail is None:
            return (
                f"{target.isoformat()} 没有可改写的日程快照"
                "（当天快照尚未生成，或只启用了静态日程）。不要声称已经修改。"
            )
        if act == "list":
            return self.render_day(detail, target, now)
        if not detail.get("editing_enabled", False):
            return "AI 每日日程未启用，日程当前只读。本次未做任何修改。"

        editable = self.admin_service.get_editable_state(
            persona_hash, target, timezone, now=now
        )
        if editable is None:
            return f"{target.isoformat()} 的日程快照已失效，本次未做任何修改。"
        if editable["cutoff"] >= MINUTES_PER_DAY:
            # 日末最后一分钟：冻结点已到 24:00，没有任何合法的新起点
            return f"{target.isoformat()} 已到日末（冻结点 24:00），无法再改写。本次未做任何修改。"

        draft, error = apply_slot_edit(
            editable["user_slots"],
            editable["cutoff"],
            action=act,
            user_slot_id=user_slot_id,
            name=name,
            start=start,
            end=end,
            state=state,
        )
        if error:
            # 现阶段的 error 全是本插件常量，但入口统一中和，避免将来某条分支回显快照文本
            return f"本次未做任何修改：{neutralize_markup(str(error))}"

        try:
            await self.admin_service.save(
                persona_hash=persona_hash,
                local_date=target,
                timezone=timezone,
                snapshot_id=editable["snapshot_id"],
                slots=draft,
                now=now,
            )
        except ScheduleNotFound:
            return "日程快照已不存在，本次改写未生效。"
        except ScheduleSaveConflict:
            return "日程刚被重新生成或改动，本次改写未生效；请先重新查看再试。"
        except SchedulePersistenceError:
            logger.error(f"{tag(event)} ❌ AI 改写日程保存失败（持久化异常）")
            return "保存失败，本次改写未生效。"
        except ValueError as exc:
            # 服务端校验消息可能回显快照里的旧名称，同样要中和后再给模型
            return f"本次改写未生效：{neutralize_markup(str(exc))}"

        logger.info(
            f"{tag(event)} ✍️ AI 改写日程: action={act} date={target.isoformat()} "
            f"persona={persona_hash[:8]}… slots={len(draft)}"
        )
        refreshed = self.admin_service.get_detail(persona_hash, target, timezone, now=now)
        head = f"已{_ACTION_LABELS.get(act, act)}（{target.isoformat()}）。最新日程："
        return f"{head}\n{self.render_day(refreshed, target, now)}"
