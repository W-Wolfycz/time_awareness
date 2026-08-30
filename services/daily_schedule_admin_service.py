"""人格日程人工编辑的服务层（快照查询 + 受限编辑）。

职责：
- 生成 Persona 下拉信息（快照枚举 + AstrBot Persona 名称 HMAC 匹配）；
- 生成时段 Card 的服务端编辑权限（R/C 由服务端计算，不信任浏览器）；
- 对保存请求做完整校验并原子替换快照。

与生成服务共用 ``DailyScheduleService._locks``（进程内），避免编辑与
regenerate 并发竞态；Web API 不直接修改 Store。
"""

from __future__ import annotations

import asyncio
import datetime
import uuid
from typing import Any

from ..core.daily_schedule_store import DailyScheduleSnapshotStore
from ..domain.schedule import parse_time, format_time, ScheduleValidationError
from ..domain.timeline import (
    advance_executed,
    merge_active_slot_fragments,
    next_minute_cutoff,
    render_effective_timeline,
)
from ..domain.schedule_edit import (
    MAX_NAME_LENGTH,
    MAX_SLOTS_PER_SNAPSHOT,
    MAX_STATE_LENGTH,
    day_minute_of,
    slot_edit_state,
    slot_ref,
    strip_internal_fields,
)
from ..log import logger, tag
from ..utils.time_utils import to_timezone_key
from .daily_schedule_service import DailyScheduleService


class ScheduleSaveConflict(Exception):
    """快照已被重新生成或由其他页面修改（HTTP 409）。"""


class ScheduleNotFound(Exception):
    """快照不存在或已被 retention 清理（HTTP 404）。"""


class SchedulePersistenceError(Exception):
    """校验已通过但快照持久化失败——服务端故障（HTTP 500）。"""


class DailyScheduleAdminService:
    """人格日程查看与编辑服务。"""

    def __init__(
        self,
        *,
        context: Any,
        store: DailyScheduleSnapshotStore,
        daily_schedule_service: DailyScheduleService,
    ):
        self.context = context
        self.store = store
        self.daily_schedule_service = daily_schedule_service

    # ==================== Persona 列表 ====================

    async def list_personas(self) -> list[dict]:
        """枚举全部有 ready 快照的 Persona。

        通过 AstrBot 当前 Persona 列表（``persona_id``）重新计算 HMAC 与
        快照匹配，显示真实名称；已删除/改名/无法匹配的显示
        「未找到的人格 · {hash 前 8 位}」。不新增任何落盘数据。
        """
        self.store.load_readonly()
        grouped = self.store.snapshots_by_persona()

        name_by_hash: dict[str, str] = {}
        try:
            personas = await self.context.persona_manager.get_all_personas()
            for persona in personas:
                persona_id = str(getattr(persona, "persona_id", "") or "").strip()
                if not persona_id:
                    continue
                try:
                    name_by_hash[self.store.persona_hash(persona_id)] = persona_id
                except RuntimeError:
                    pass  # 无 secret：全部按未匹配处理
        except Exception as exc:
            logger.warning(f"{tag()} ⚠️ Persona 列表查询失败（按 hash 显示）: {exc}")

        result = []
        for persona_hash, entries in grouped.items():
            dates = sorted(
                {(entry["local_date"], entry["timezone"]) for entry in entries}
            )
            name = name_by_hash.get(persona_hash)
            result.append(
                {
                    "key": persona_hash,
                    "name": name or f"未找到的人格 · {persona_hash[:8]}",
                    "snapshot_count": len(entries),
                    "dates": [{"date": d, "timezone": tz} for d, tz in dates],
                    "latest_date": dates[-1][0] if dates else "",
                }
            )
        result.sort(key=lambda item: item["name"])
        return result

    # ==================== 详情 ====================

    def _edit_window(self, local_date: datetime.date, now: datetime.datetime) -> tuple[int, int]:
        """按目标日期计算 (now_minute, cutoff_minute)。

        ``now`` 为插件时区的 aware datetime，其日期即 local_date 的基准：

        - 快照日期 == 今天：冻结至服务端下一整分钟（C = next_minute_cutoff）；
        - 历史日期：整日只读（now=cutoff=1440，所有时段落入已结束）；
        - 未来日期：整日可编辑（now=cutoff=0，所有时段落入未来）。
        """
        if local_date == now.date():
            now_minute = day_minute_of(now)
            cutoff = next_minute_cutoff(now)
            return now_minute, cutoff
        if local_date < now.date():
            return 1440, 1440
        return 0, 0

    def get_detail(
        self,
        persona_hash: str,
        local_date: datetime.date,
        timezone: str,
        *,
        now: datetime.datetime | None = None,
    ) -> dict | None:
        """返回快照详情（含每段的编辑权限）；快照不存在返回 None。"""
        self.store.load_readonly()
        snapshot = self.store.get(persona_hash, local_date, timezone)
        if snapshot is None or snapshot.get("status") != "ready":
            return None
        now = now or self.daily_schedule_service.time_context.now()
        now = to_timezone_key(now, str(snapshot.get("timezone", timezone) or timezone))
        now_minute, cutoff_minute = self._edit_window(local_date, now)
        editing_enabled = self.daily_schedule_service.enabled()

        slots = self._render_slots(snapshot, now, local_date)
        slots.sort(key=lambda s: str(s.get("start", "")))
        detailed_slots = []
        for index, slot in enumerate(slots):
            try:
                start = parse_time(str(slot.get("start", "")), allow_24=False, strict=True)
                end = parse_time(str(slot.get("end", "")), allow_24=True, strict=True)
            except Exception:
                start, end = 0, 0
            state = slot_edit_state(start, end, now_minute, cutoff_minute)
            # 展示层修正：结束时刻恰好等于冻结点 C、且当前分钟仍在时段内时，
            # 时段在时间上还没结束（下一分钟才到 C），不能提前标「已结束」。
            # 编辑权限与可删性仍按 state 计算（该时段整体已进入冻结区）。
            display_mode = state.mode
            if (
                state.mode == "finished"
                and end == cutoff_minute
                and now_minute < end
            ):
                display_mode = "active"
            origin = str(slot.get("origin", "ai") or "ai")
            source_origin = str(slot.get("source_origin", origin) or origin)
            if not editing_enabled or origin in {"ai", "static"}:
                editable_fields = []
            elif origin == "executed":
                editable_fields = []
            elif state.mode == "active":
                editable_fields = ["end"]
            else:
                editable_fields = list(state.editable_fields)
            detailed_slots.append(
                {
                    "slot_ref": slot_ref(str(snapshot.get("snapshot_id", "")), index),
                    # user_slot_id 只在用户段有意义（executed 固化段残留的内部
                    # 字段不外泄，前端仅对 origin=user 段回传）
                    "user_slot_id": (
                        str(slot.get("user_slot_id", "") or "")
                        if origin == "user" else ""
                    ),
                    "name": str(slot.get("name", "") or ""),
                    "start": str(slot.get("start", "") or ""),
                    "end": str(slot.get("end", "") or ""),
                    "state": str(slot.get("state", "") or ""),
                    "origin": origin,
                    "edit_mode": display_mode,
                    "editable_fields": editable_fields,
                    "deletable": bool(
                        editing_enabled
                        and origin == "user"
                        and state.mode in {"future", "active", "imminent"}
                    ),
                    "source_origin": source_origin,
                }
            )

        return {
            "persona_hash": str(snapshot.get("persona_hash", "")),
            "snapshot_id": str(snapshot.get("snapshot_id", "")),
            "local_date": str(snapshot.get("local_date", "")),
            "timezone": str(snapshot.get("timezone", "")),
            "source": str(snapshot.get("source", "")),
            "generated_at": str(snapshot.get("generated_at", "")),
            "manually_edited": bool(snapshot.get("manually_edited", False)),
            "edited_at": str(snapshot.get("edited_at", "")) or None,
            "theme": snapshot.get("theme") or {},
            "boundary_state": snapshot.get("boundary_state") or {},
            # 前端以此为锚点显示插件实际时区的实时钟，并计算新增时段默认值。
            "server_now": (
                now if now.tzinfo is not None else now.astimezone()
            ).isoformat(),
            "cutoff": format_time(cutoff_minute),
            "editing_enabled": editing_enabled,
            "slots": detailed_slots,
        }

    def _current_static(self) -> list[dict]:
        return self.daily_schedule_service._current_static_slots()

    def _ai_priority(self) -> bool:
        return self.daily_schedule_service._ai_priority_over_static()

    def _render_slots(self, snapshot: dict, now: datetime.datetime, local_date: datetime.date) -> list[dict]:
        if local_date != now.date():
            cutoff = 1440 if local_date < now.date() else 0
        else:
            cutoff = next_minute_cutoff(now)
        # 渲染前先把冻结边界推进到当前截止点（只读，不写盘）：已结束前段
        # 以 executed 展示（跨冻结点用户段的固化前段因此可见），未来层从
        # cutoff 起动态合并，避免 [executed_until, cutoff) 空白。
        snapshot = dict(snapshot)
        if cutoff > 0 and str(snapshot.get("executed_until", "")) != "24:00":
            snapshot.update(advance_executed(snapshot, cutoff, self._ai_priority()))
        rendered = render_effective_timeline(
            snapshot, cutoff, self._current_static(), self._ai_priority()
        )
        # 当前正在进行的时段不切成两半（详情页显示「正在进行」而非
        # 「已结束 + 即将开始」两段）
        now_minute = day_minute_of(now)
        return merge_active_slot_fragments(rendered, now_minute)

    # ==================== 保存 ====================

    async def save(
        self,
        *,
        persona_hash: str,
        local_date: datetime.date,
        timezone: str,
        snapshot_id: str,
        slots: list,
        now: datetime.datetime | None = None,
    ) -> dict:
        """保存整日草稿；成功返回新 snapshot_id。

        Raises:
            ScheduleNotFound: 快照不存在（404）。
            ScheduleSaveConflict: snapshot_id 不一致（409）。
            ValueError: 校验失败（错误列表在 message 中）。
        """
        if not self.daily_schedule_service.enabled():
            raise ValueError("AI 每日日程未启用，当前快照为只读")
        self.store.load_readonly()
        now = now or self.daily_schedule_service.time_context.now()
        lock_key = self.store.snapshot_key(persona_hash, local_date, timezone)
        lock = self.daily_schedule_service._locks.setdefault(lock_key, asyncio.Lock())

        async with lock:
            snapshot = self.store.get(persona_hash, local_date, timezone)
            if snapshot is None or snapshot.get("status") != "ready":
                raise ScheduleNotFound("日程快照不存在或已被清理，请刷新")
            if str(snapshot.get("snapshot_id", "")) != str(snapshot_id or ""):
                raise ScheduleSaveConflict("日程已被重新生成或由其他页面修改，请刷新")
            now = to_timezone_key(
                now,
                str(snapshot.get("timezone", timezone) or timezone),
            )

            # 2.0.0 基线：user_slots 均携带 user_slot_id（无 id 的提交一律视为
            # 新增，不存在旧数据补发路径）；编辑/删除都按 id 定位。
            existing_user = snapshot.get("user_slots", [])

            _, cutoff_minute = self._edit_window(local_date, now)
            advanced = advance_executed(
                snapshot, cutoff_minute, self._ai_priority()
            )
            user_slots = self._validate_user_slots(
                slots,
                cutoff_minute,
                existing_user,
                snapshot.get("executed_slots", []),
            )

            updated = dict(snapshot)
            updated.update(advanced)
            updated["snapshot_id"] = uuid.uuid4().hex
            updated["user_slots"] = user_slots
            updated["static_slots"] = self._current_static()
            updated["slots"] = strip_internal_fields(render_effective_timeline(
                updated, cutoff_minute, updated["static_slots"], self._ai_priority()
            ))
            updated["manually_edited"] = True
            updated["edited_at"] = (
                now if now.tzinfo is not None else now.astimezone()
            ).isoformat()
            # 保留 generated_at / theme / boundary_state / source / prompt_revision

            if not self.store.save_ready(
                updated,
                retention_days=self.daily_schedule_service.retention_days(),
                today=local_date,
            ):
                raise SchedulePersistenceError("日程修改已通过校验，但快照持久化失败")
            logger.debug(
                f"{tag()} ✍️ 人格日程已人工修改: persona={persona_hash} "
                f"date={local_date.isoformat()} slots={len(updated['slots'])}"
            )
            return {"snapshot_id": updated["snapshot_id"]}

    async def delete_user_slot(
        self,
        *,
        persona_hash: str,
        local_date: datetime.date,
        timezone: str,
        snapshot_id: str,
        user_slot_id: str,
        now: datetime.datetime | None = None,
    ) -> dict:
        """删除一个用户时段（只删未来部分；已执行前段永久保留）。

        与 ``save`` 共用快照锁与 ``snapshot_id`` 并发保护；删除语义与
        「保存时省略 user_slot_id」一致，但独立成显式端点——前端可走
        保存折叠（现状）或本端点（显式意图），未来 backup/审计路径
        也能从本方法抽取同一删除逻辑。

        Raises:
            ScheduleNotFound: 快照不存在（404）。
            ScheduleSaveConflict: snapshot_id 不一致（409）。
            ValueError: user_slot_id 不存在（400）。
        """
        if not self.daily_schedule_service.enabled():
            raise ValueError("AI 每日日程未启用，当前快照为只读")
        self.store.load_readonly()
        now = now or self.daily_schedule_service.time_context.now()
        lock_key = self.store.snapshot_key(persona_hash, local_date, timezone)
        lock = self.daily_schedule_service._locks.setdefault(lock_key, asyncio.Lock())

        async with lock:
            snapshot = self.store.get(persona_hash, local_date, timezone)
            if snapshot is None or snapshot.get("status") != "ready":
                raise ScheduleNotFound("日程快照不存在或已被清理，请刷新")
            if str(snapshot.get("snapshot_id", "")) != str(snapshot_id or ""):
                raise ScheduleSaveConflict("日程已被重新生成或由其他页面修改，请刷新")
            now = to_timezone_key(
                now,
                str(snapshot.get("timezone", timezone) or timezone),
            )

            user_slot_id = str(user_slot_id or "").strip()
            existing = snapshot.get("user_slots", [])
            if not any(
                isinstance(slot, dict)
                and str(slot.get("user_slot_id", "") or "") == user_slot_id
                for slot in existing
            ):
                raise ValueError("用户时段不存在（user_slot_id 未找到），请刷新后重试")

            _, cutoff_minute = self._edit_window(local_date, now)
            # 先推进冻结边界：跨冻结点段的已执行前段固化进 executed_slots，
            # 再从裁剪后的未来层移除目标段——删除永不动已执行历史。
            advanced = advance_executed(snapshot, cutoff_minute, self._ai_priority())
            remaining = [
                slot
                for slot in advanced.get("user_slots", [])
                if str(slot.get("user_slot_id", "") or "") != user_slot_id
            ]

            updated = dict(snapshot)
            updated.update(advanced)
            updated["snapshot_id"] = uuid.uuid4().hex
            updated["user_slots"] = remaining
            updated["static_slots"] = self._current_static()
            updated["slots"] = strip_internal_fields(render_effective_timeline(
                updated, cutoff_minute, updated["static_slots"], self._ai_priority()
            ))
            updated["manually_edited"] = True
            updated["edited_at"] = (
                now if now.tzinfo is not None else now.astimezone()
            ).isoformat()

            if not self.store.save_ready(
                updated,
                retention_days=self.daily_schedule_service.retention_days(),
                today=local_date,
            ):
                raise SchedulePersistenceError("删除已通过校验，但快照持久化失败")
            logger.debug(
                f"{tag()} 🗑️ 人格日程用户时段已删除: persona={persona_hash} "
                f"date={local_date.isoformat()} slot={user_slot_id}"
            )
            return {"snapshot_id": updated["snapshot_id"]}

    def _validate_user_slots(
        self,
        payload: list,
        cutoff: int,
        existing: list,
        executed: list | None = None,
    ) -> list[dict]:
        """保存协议只接受用户层；AI/static/executed 永不由前端提交。

        身份定位：
        - ``user_slot_id``：服务端签发并持久化的稳定引用；不存在的 id 拒绝；
        - 无 id 的段一律视为新增（2.0.0 基线无旧数据，不存在按名称/状态
          猜测对应关系的过渡路径）。

        已有段按冻结点三态处理：
        - ``end <= cutoff``：已结束，拒绝修改；
        - ``start >= cutoff``：完全未来，可改全部字段（start 仍须 >= cutoff）；
        - 跨冻结点：已执行前段由 ``advance_executed`` 固化，payload 尾段
          起点强制为最新 ``cutoff``，且只允许修改 ``end``（name/state/start
          与原有不一致即拒绝）。

        快照中未被 payload 引用的用户段视为删除：未来部分移除，已执行前段
        已在 ``executed_slots`` 中永久保留。
        """
        if not isinstance(payload, list):
            raise ValueError("user_slots 必须是数组")
        if len(payload) > MAX_SLOTS_PER_SNAPSHOT:
            raise ValueError(f"用户时段数量超过上限 {MAX_SLOTS_PER_SNAPSHOT}")

        existing_by_id: dict[str, dict] = {}
        for slot in existing if isinstance(existing, list) else []:
            slot_id = str(slot.get("user_slot_id", "") or "").strip()
            if slot_id:
                existing_by_id[slot_id] = slot

        original_start_by_id: dict[str, int] = {}
        for slot in executed if isinstance(executed, list) else []:
            if not isinstance(slot, dict):
                continue
            slot_id = str(slot.get("user_slot_id", "") or "").strip()
            source = slot.get(
                "_source_origin",
                slot.get("source_origin", slot.get("origin")),
            )
            if not slot_id or source != "user":
                continue
            try:
                start = parse_time(str(slot.get("start", "")), strict=True)
            except ScheduleValidationError:
                continue
            original_start_by_id[slot_id] = min(
                start,
                original_start_by_id.get(slot_id, start),
            )

        result = []
        errors = []
        used_ids: set[str] = set()
        for index, item in enumerate(payload, 1):
            label = f"第 {index} 个用户时段"
            if not isinstance(item, dict):
                errors.append(f"{label}不是对象"); continue
            slot_id = str(item.get("user_slot_id", "") or "").strip()
            name = str(item.get("name", "") or "").strip()
            state = str(item.get("state", "") or "").strip()
            if not name: errors.append(f"{label}：名称不能为空")
            if not state: errors.append(f"{label}：状态描述不能为空")
            try:
                start = parse_time(str(item.get("start", "")), strict=True)
                end = parse_time(str(item.get("end", "")), allow_24=True, strict=True)
            except ScheduleValidationError as exc:
                errors.append(f"{label}：{exc}"); continue
            if start >= end:
                errors.append(f"{label}：开始时间必须早于结束时间"); continue

            matched = None
            is_new = True
            if slot_id:
                if slot_id in used_ids:
                    errors.append(f"{label}：时段引用重复"); continue
                matched = existing_by_id.get(slot_id)
                if matched is None:
                    errors.append(
                        f"{label}：无法匹配已有用户时段（可能已被重新生成），请刷新"
                    )
                    continue
                used_ids.add(slot_id)
                is_new = False
            else:
                matched = None  # 无 id 段一律视为新增
                is_new = True

            if is_new:
                # 新增用户时段：不得借用拆分逻辑创建过去时段
                if start < cutoff:
                    errors.append(
                        f"{label}：开始时间不能早于冻结点 {format_time(cutoff)}"
                        "（页面停留期间冻结点可能已前移，请刷新后重试）"
                    )
                    continue
                result.append({
                    "name": name[:MAX_NAME_LENGTH],
                    "start": format_time(start),
                    "end": format_time(end),
                    "state": state[:MAX_STATE_LENGTH],
                    "origin": "user",
                    "user_slot_id": uuid.uuid4().hex,
                })
                continue

            # 已有用户时段（快照内定位成功，按 user_slot_id）
            original = matched
            slot_id = slot_id or uuid.uuid4().hex
            orig_name = str(original.get("name", "") or "").strip()
            orig_state = str(original.get("state", "") or "").strip()
            orig_start = parse_time(str(original.get("start", "")), strict=True)
            orig_start = min(orig_start, original_start_by_id.get(slot_id, orig_start))
            orig_end = parse_time(str(original.get("end", "")), allow_24=True, strict=True)
            if orig_end <= cutoff:
                errors.append(
                    f"{label}「{name or orig_name}」：该时段已结束，不允许修改"
                    "（页面停留期间冻结点可能已前移，请刷新后重试）"
                )
                continue
            if orig_start > cutoff:
                # 完全未来：全部字段可改，但开始时间不得移入冻结区
                if start < cutoff:
                    errors.append(
                        f"{label}：开始时间不能早于冻结点 {format_time(cutoff)}"
                        "（页面停留期间冻结点可能已前移，请刷新后重试）"
                    )
                    continue
                result.append({
                    "name": name[:MAX_NAME_LENGTH],
                    "start": format_time(start),
                    "end": format_time(end),
                    "state": state[:MAX_STATE_LENGTH],
                    "origin": "user",
                    "user_slot_id": slot_id or uuid.uuid4().hex,
                })
                continue
            # 跨冻结点：已执行前段已固化，尾段仅允许修改结束时间
            # start 合法来源只有「原 start」与「页面载入时的渲染裁剪值」
            # （两者都落在 [orig_start, 最新 cutoff] 内）；改到范围外视为
            # 移动时段本身，拒绝。
            if name != orig_name or state != orig_state:
                errors.append(
                    f"{label}「{orig_name}」：跨冻结点时段仅允许修改结束时间"
                )
                continue
            if not (orig_start <= start <= cutoff):
                errors.append(
                    f"{label}「{orig_name}」：跨冻结点时段仅允许修改结束时间"
                )
                continue
            if end <= cutoff:
                errors.append(
                    f"{label}：结束时间必须晚于冻结点 {format_time(cutoff)}"
                    "（页面停留期间冻结点可能已前移，请刷新后重试）"
                )
                continue
            result.append({
                "name": name[:MAX_NAME_LENGTH],
                "start": format_time(cutoff),  # 尾段起点推进到最新冻结点
                "end": format_time(end),
                "state": state[:MAX_STATE_LENGTH],
                "origin": "user",
                "user_slot_id": slot_id,
            })

        result.sort(key=lambda x: parse_time(x["start"], strict=True))
        for previous, current in zip(result, result[1:]):
            if parse_time(current["start"], strict=True) < parse_time(previous["end"], allow_24=True, strict=True):
                errors.append(f"用户日程重叠：{current['start']}-{current['end']} 与前一个用户时段冲突")
        if errors:
            raise ValueError("；".join(errors))
        return result
