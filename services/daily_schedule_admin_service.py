"""人格日程人工编辑的服务层（快照查询 + 受限编辑）；与生成服务共用快照锁避免并发竞态。"""

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
        """枚举全部有 ready 快照的 Persona；无法匹配的按 hash 前 8 位显示。"""
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
        """按目标日期计算 (now_minute, cutoff_minute)：今天=下一整分钟，历史=1440 只读，未来=0 可编辑。"""
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
            # 结束时刻恰好等于冻结点 C 且当前分钟仍在时段内时，不能提前标「已结束」；权限仍按 state。
            display_mode = state.mode
            if (
                state.mode == "finished"
                and end == cutoff_minute
                and now_minute < end
            ):
                display_mode = "active"
            origin = str(slot.get("origin", "ai") or "ai")
            source_origin = str(slot.get("source_origin", origin) or origin)
            if not editing_enabled or origin in {"ai", "static", "executed"}:
                editable_fields = []
            elif state.mode == "active":
                editable_fields = ["end"]
            else:
                editable_fields = list(state.editable_fields)
            detailed_slots.append(
                {
                    "slot_ref": slot_ref(str(snapshot.get("snapshot_id", "")), index),
                    # user_slot_id 只在用户段有意义（executed 固化段内部字段不外泄）
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
        # 渲染前把冻结边界推进到当前截止点（只读）：已结束前段以 executed 展示，未来层从 cutoff 起合并。
        snapshot = dict(snapshot)
        if cutoff > 0 and str(snapshot.get("executed_until", "")) != "24:00":
            snapshot.update(advance_executed(snapshot, cutoff, self._ai_priority()))
        rendered = render_effective_timeline(
            snapshot, cutoff, self._current_static(), self._ai_priority()
        )
        # 当前正在进行的时段不切成两半（详情页显示「正在进行」）。
        now_minute = day_minute_of(now)
        return merge_active_slot_fragments(rendered, now_minute)

    def _require_editable_snapshot(
        self, persona_hash: str, local_date: datetime.date, timezone: str,
        snapshot_id: str, now: datetime.datetime,
    ) -> tuple[dict, datetime.datetime]:
        """读取并校验可编辑快照；不存在/冲突抛对应异常，返回 (snapshot, 目标时区 now)。"""
        snapshot = self.store.get(persona_hash, local_date, timezone)
        if snapshot is None or snapshot.get("status") != "ready":
            raise ScheduleNotFound("日程快照不存在或已被清理，请刷新")
        if str(snapshot.get("snapshot_id", "")) != str(snapshot_id or ""):
            raise ScheduleSaveConflict("日程已被重新生成或由其他页面修改，请刷新")
        return snapshot, to_timezone_key(
            now, str(snapshot.get("timezone", timezone) or timezone)
        )

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
        """保存整日草稿；成功返回新 snapshot_id（不存在/冲突/校验失败抛对应异常）。"""
        if not self.daily_schedule_service.enabled():
            raise ValueError("AI 每日日程未启用，当前快照为只读")
        self.store.load_readonly()
        now = now or self.daily_schedule_service.time_context.now()
        lock_key = self.store.snapshot_key(persona_hash, local_date, timezone)
        lock = self.daily_schedule_service._locks.setdefault(lock_key, asyncio.Lock())

        async with lock:
            snapshot, now = self._require_editable_snapshot(
                persona_hash, local_date, timezone, snapshot_id, now
            )

            # user_slots 均携带 user_slot_id；无 id 提交视为新增，编辑/删除按 id 定位。
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

    def _validate_user_slots(
        self,
        payload: list,
        cutoff: int,
        existing: list,
        executed: list | None = None,
    ) -> list[dict]:
        """保存协议只接受用户层；按 user_slot_id 定位，按冻结点三态校验，未引用段视为删除。"""
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
            # 跨冻结点：尾段仅允许修改结束时间；start 只接受原 start 或渲染裁剪值。
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
