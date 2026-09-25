"""AI 每日日程生成、幂等、回退与运行时解析服务（以 Persona 为隔离维度）。"""

from __future__ import annotations

import asyncio
import datetime
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..domain.schedule import (
    MINUTES_PER_DAY,
    ScheduleValidationError,
    ai_daily_config,
    ai_priority_over_static,
    find_active_daily_slot,
    format_time,
    parse_time,
    static_slots_from_config,
)
from ..domain.timeline import (
    advance_executed,
    clip_slots,
    merge_active_slot_fragments,
    next_minute_cutoff,
    render_effective_timeline,
)
from ..domain.schedule_edit import MAX_STATE_LENGTH, day_minute_of
from ..integrations.persona_resolver import resolve_effective_persona
from .adaptive_policy import AdaptiveGenerationPolicy, EnhancedContext
from .adaptive_policy import AdaptiveConcurrencyGate
from .daily_schedule_generation import (
    PROMPT_REVISION,
    DailyScheduleGeneration,
    DailyScheduleGenerationError,
    exception_detail,
)
from ..log import logger, tag


@dataclass(frozen=True)
class ScheduleResolution:
    state_prompt: str
    slot_name: str
    source: str
    snapshot_id: str


@dataclass(frozen=True)
class GenerationResult:
    success: bool
    source: str = ""
    slot_count: int = 0
    snapshot_id: str = ""
    reused: bool = False
    error_type: str = ""
    message: str = ""
    retryable: bool = False
    calls_executed: int = 0


@dataclass(frozen=True)
class FrozenIdentity:
    """排队时刻冻结的生成上下文（身份/时区/日期），避免执行时 Persona 切换竞态。"""

    session: str
    persona_hash: str
    persona: Any
    timezone: str
    local_date: datetime.date
    # 实际发起生成的墙钟时间；``local_date`` 可以是次日（-HH:MM 预生成）。
    now: datetime.datetime
    # 真实 Persona ID（仅用于日志诊断；快照仍只存 hash，不落盘原始 ID）
    persona_id: str = ""


class DailyScheduleService:
    """以 Persona + 本地日期为作用域维护冻结的 AI 日程快照。"""

    def __init__(
        self,
        *,
        context: Any,
        config: dict,
        store,
        time_context,
        persona_resolver: Callable[..., Awaitable[tuple[str, Any | None]]] = resolve_effective_persona,
        generation_service: DailyScheduleGeneration | None = None,
        concurrency_gate: AdaptiveConcurrencyGate | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        weather_forecast_provider: Callable[[datetime.date], Awaitable[dict | None]] | None = None,
    ):
        self.context = context
        self.config = config
        self.store = store
        self.time_context = time_context
        self._persona_resolver = persona_resolver
        self.generation = generation_service or DailyScheduleGeneration(
            context=context,
            config=config,
            time_context=time_context,
            persona_resolver=persona_resolver,
            concurrency_gate=concurrency_gate,
            sleep=sleep,
        )
        self._sleep = sleep
        self._weather_forecast_provider = weather_forecast_provider
        self._available = True
        # 进程内身份缓存：session → (persona_hash, persona, persona_id)；原始 UMO/prompt 不落盘
        self._persona_cache: dict[str, tuple[str, Any, str]] = {}
        # 代表会话表：persona_hash → session（多 Bot 同 persona 时共享同一份快照）
        self._representative_sessions: dict[str, str] = {}
        # UMO 最后活跃时间（保活：一整天未出现的 UMO 删除；仅进程内存）
        self._session_active_at: dict[str, datetime.datetime] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def set_available(self, available: bool) -> None:
        self._available = bool(available)

    def _daily_schedule_config(self) -> dict:
        """daily_schedule 配置块；非 dict 时回退空 dict。"""
        daily = self.config.get("daily_schedule", {}) if isinstance(self.config, dict) else {}
        return daily if isinstance(daily, dict) else {}

    def _daily_config(self) -> dict:
        return ai_daily_config(self.config)

    def configured_enabled(self) -> bool:
        daily_schedule = self._daily_schedule_config()
        return bool(
            daily_schedule.get("enable_schedule", False)
            and self._daily_config().get("enabled", False)
        )

    def enabled(self) -> bool:
        return bool(self._available and self.configured_enabled())

    def max_slots(self) -> int:
        return self.generation.max_slots()

    def retention_days(self) -> int:
        try:
            value = int(self._daily_config().get("retention_days", 30) or 30)
        except (TypeError, ValueError):
            value = 30
        return max(1, min(365, value))

    def state_retention_days(self) -> int:
        """全局运行期状态保留期（天）；缺省/非法回退 7，与 AI 快照文件自身 retention_days 无关。"""
        runtime = self.config.get("runtime", {})
        raw_value = (
            runtime.get("state_retention_days", 7)
            if isinstance(runtime, dict)
            else 7
        )
        if raw_value is None:
            raw_value = 7
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            value = 7
        return max(1, min(365, value))

    def _cleanup_stale_locks(self) -> int:
        """回收已过期、未锁定的日程生成锁，防止 ``_locks`` 无界增长；活动锁一律保留。"""
        now = self.time_context.now()
        threshold = now.date() - datetime.timedelta(days=self.state_retention_days())
        stale: list[str] = []
        for key, lock in self._locks.items():
            if lock.locked():
                continue
            parts = key.split(":")
            if len(parts) < 3:
                continue
            try:
                lock_date = datetime.date.fromisoformat(parts[1])
            except ValueError:
                continue
            if lock_date < threshold:
                stale.append(key)
        for key in stale:
            self._locks.pop(key, None)
        if stale:
            logger.debug(
                f"{tag()} AI 每日日程：回收 {len(stale)} 个过期未占用的生成锁"
            )
        return len(stale)

    _UTC_ALIASES = {"UTC", "Etc/UTC", "GMT", "UCT", "Z", "UTC+00:00", "UTC±00:00"}
    # 固定偏移形如 UTC+08:00 / UTC-03:00 / UTC+05:30；统一归一化到 UTC±HH:MM。
    _FIXED_OFFSET_RE = re.compile(r"^UTC([+-])(\d{1,2}):?(\d{2})?$")

    @classmethod
    def _normalize_fixed_offset(cls, name: str) -> str | None:
        """把 Python 固定 offset 的 ``UTC+08:00`` 形式归一化，非法返回 None。"""
        m = cls._FIXED_OFFSET_RE.match(name.strip())
        if not m:
            return None
        sign = m.group(1)
        hours = int(m.group(2))
        minutes = int(m.group(3) or 0)
        if hours > 14 or minutes > 59:
            return None
        if hours == 0 and minutes == 0:
            return "UTC"
        return f"UTC{sign}{hours:02d}:{minutes:02d}"

    @classmethod
    def _timezone_key(cls, now: datetime.datetime) -> str:
        """时区主键片段：IANA 直接返回，固定 offset 归一化为 UTC±HH:MM，无法归类回退 system-local。"""
        if now.tzinfo is not None:
            name = str(now.tzinfo)
        else:
            try:
                name = str(now.astimezone().tzinfo or "system-local")
            except Exception:
                return "system-local"
        if name in cls._UTC_ALIASES:
            return "UTC"
        normalized = cls._normalize_fixed_offset(name)
        if normalized is not None:
            return normalized
        # 仅当能被 zoneinfo 解析为 IANA 时保留，否则落到 system-local。
        if name and name != "system-local":
            try:
                from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
                ZoneInfo(name)
                return name
            except (ZoneInfoNotFoundError, KeyError, ValueError, TypeError):
                pass
        return "system-local"

    # ==================== Person 维度身份 ====================

    async def _resolve_persona(
        self,
        session: str,
        event: Any = None,
    ) -> tuple[str, Any | None]:
        """解析当前 effective Persona，返回 ``(resolved_id, persona)``；失败返回 ``("", None)``。"""
        try:
            return await self._persona_resolver(self.context, session, event)
        except Exception as exc:
            logger.warning(
                f"{tag()} ⚠️ AI 日程 Persona 解析失败（不生成无 Persona 快照）: "
                f"{type(exc).__name__}"
            )
            return "", None

    def _identity_for(self, session: str) -> str:
        """同步取进程内缓存的 persona_hash；未解析过返回空串。"""
        cached = self._persona_cache.get(str(session or "").strip())
        return cached[0] if cached else ""

    def _identity_id_for(self, session: str) -> str:
        """同步取进程内缓存的真实 Persona ID（日志诊断用）；未解析过返回空串。"""
        cached = self._persona_cache.get(str(session or "").strip())
        return cached[2] if cached else ""

    def _identity_object_for(self, session: str) -> Any | None:
        """同步取进程内缓存的 persona 对象；未解析过返回 None。"""
        cached = self._persona_cache.get(str(session or "").strip())
        return cached[1] if cached else None

    async def resolve_session_persona_name(
        self,
        session: str,
        *,
        event: Any = None,
    ) -> str:
        """直接解析当前会话生效的 Persona 显示名（不走缓存）；失败返回空串。"""
        resolved_id, persona = await self._resolve_persona(session, event)
        if isinstance(persona, dict):
            name = str(persona.get("name", "") or "").strip()
        else:
            name = str(getattr(persona, "name", "") or "").strip()
        return name or str(resolved_id or "").strip()

    def register_session(self, session: str, *, trigger: bool = True) -> str:
        """同步注册代表会话（仅用已缓存身份）；未缓存返回空串，不阻塞。"""
        if not self.enabled():
            return ""
        session = str(session or "").strip()
        persona_hash = self._identity_for(session)
        if not persona_hash:
            return ""
        self._mark_session_active(session)
        self._representative_sessions[persona_hash] = session
        if trigger:
            self.queue_generation(session)
        return persona_hash

    async def register_session_async(
        self,
        session: str,
        *,
        trigger: bool = True,
        event: Any = None,
        touch_active: bool = True,
    ) -> str:
        """异步注册代表会话：重新解析并缓存身份；touch_active=False 用于每日扫描（不刷新保活）。"""
        if not self.enabled():
            return ""
        session = str(session or "").strip()
        if not session:
            return ""
        resolved_id, persona = await self._resolve_persona(session, event)
        resolved_id = str(resolved_id or "").strip()
        if not resolved_id:
            self._drop_stale_identity(session)
            return ""
        try:
            persona_hash = self.store.persona_hash(resolved_id)
        except Exception as exc:
            logger.warning(f"{tag()} ⚠️ AI 日程 Persona 身份哈希失败: {type(exc).__name__}")
            return ""
        self._persona_cache[session] = (persona_hash, persona, resolved_id)
        if touch_active:
            self._mark_session_active(session)
        self._representative_sessions[persona_hash] = session
        if trigger:
            self.queue_generation(session)
        return persona_hash

    def _mark_session_active(self, session: str, now: datetime.datetime | None = None) -> None:
        """登记/刷新 UMO 最后活跃时间（保活依据，仅进程内存）。"""
        session = str(session or "").strip()
        if not session:
            return
        self._session_active_at[session] = now or self.time_context.now()

    def _expire_stale_sessions(self, now: datetime.datetime) -> None:
        """删除一整天未活跃的 UMO；仅清进程内集合，不删快照/日历数据。"""
        threshold = now - datetime.timedelta(days=1)
        stale = [
            session
            for session, last_active in self._session_active_at.items()
            if last_active < threshold
        ]
        if not stale:
            return
        for session in stale:
            self._persona_cache.pop(session, None)
            self._session_active_at.pop(session, None)
        for persona_hash in [
            key for key, session in self._representative_sessions.items()
            if session in stale
        ]:
            self._representative_sessions.pop(persona_hash, None)
        logger.debug(
            f"{tag()} AI 每日日程：清理 {len(stale)} 个一整天未活跃的会话"
        )

    def _drop_stale_identity(self, session: str) -> None:
        """清除某会话缓存的旧 Persona 身份（Persona 已解析为空/失效时调用）。"""
        cached = self._persona_cache.pop(session, None)
        if cached:
            logger.debug(
                f"{tag()} AI 日程：Persona 身份失效，已清除缓存 {cached[2] or cached[0]}"
            )

    def _snapshot_usable(
        self,
        snapshot: dict | None,
        now: datetime.datetime,
        persona_hash: str,
    ) -> bool:
        """status=ready 即可复用；合法空 AI 日程是有效结果，不触发重复生成。"""
        return bool(
            isinstance(snapshot, dict)
            and snapshot.get("status") == "ready"
            and snapshot.get("persona_hash") == persona_hash
            and snapshot.get("local_date") == now.date().isoformat()
            and snapshot.get("timezone") == self._timezone_key(now)
            and snapshot.get("prompt_revision") == PROMPT_REVISION
        )

    def _render_snapshot(self, snapshot: dict, now: datetime.datetime) -> list[dict]:
        """动态渲染快照有效时间线（只读）：边界=当前分钟，渲染前把 executed_until 推进到 now。"""
        current_static = self._current_static_slots()
        ai_priority = self._ai_priority_over_static()
        now_minute = day_minute_of(now)
        snapshot = dict(snapshot)
        fully_executed = str(snapshot.get("executed_until", "")) == "24:00"
        if not fully_executed:
            snapshot.update(advance_executed(snapshot, now_minute, ai_priority))
        rendered = render_effective_timeline(
            snapshot,
            MINUTES_PER_DAY if fully_executed else now_minute,
            current_static,
            ai_priority,
        )
        # 当前正在进行的时段不切成两半（「正在执行」而非「已结束+即将开始」）
        return merge_active_slot_fragments(rendered, now_minute)

    def _render_full_day(self, snapshot: dict) -> list[dict]:
        """渲染快照完整全天时间线（只读）：推进至日末后按当前优先级渲染。"""
        ai_priority = self._ai_priority_over_static()
        old_static = snapshot.get("static_slots", [])
        advanced = advance_executed(
            snapshot, MINUTES_PER_DAY, ai_priority
        )
        return render_effective_timeline(
            {**snapshot, **advanced}, MINUTES_PER_DAY, old_static, ai_priority
        )

    def _ai_priority_over_static(self) -> bool:
        return ai_priority_over_static(self.config)

    def _current_static_slots(self) -> list[dict]:
        return static_slots_from_config(self.config)

    def get_snapshot_for_session(self, session: str, *, now=None) -> dict | None:
        if not self.enabled():
            return None
        now = now or self.time_context.now()
        persona_hash = self._identity_for(session)
        if not persona_hash:
            return None
        snapshot = self.store.get(persona_hash, now.date(), self._timezone_key(now))
        return snapshot if self._snapshot_usable(snapshot, now, persona_hash) else None

    def get_failure_for_session(self, session: str, *, now=None) -> dict | None:
        now = now or self.time_context.now()
        persona_hash = self._identity_for(session)
        return (
            self.store.get_failure(persona_hash, now.date(), self._timezone_key(now))
            if persona_hash
            else None
        )

    def today_schedule_summary(self, session: str, *, now=None) -> str | None:
        """今日全天安排摘要（每段 HH:MM-HH:MM 名称）；无内容返回 None。"""
        if not self.enabled() and not self._has_static_templates():
            return None
        now = now or self.time_context.now()
        snapshot = self.get_snapshot_for_session(session, now=now)
        slots = self._render_snapshot(snapshot, now) if snapshot else self._static_summary_slots()
        if not slots:
            return None
        lines = []
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            start = str(slot.get("start", "") or "").strip()
            end = str(slot.get("end", "") or "").strip()
            name = str(slot.get("name", "") or "").strip()[:24]
            if not start or not end:
                continue
            lines.append(f"{start}-{end} {name}" if name else f"{start}-{end}")
        return " | ".join(lines) or None

    def _has_static_templates(self) -> bool:
        templates = self._daily_schedule_config().get("schedule_templates")
        return bool(isinstance(templates, list) and templates)

    def _static_summary_slots(self) -> list[dict]:
        templates = self._daily_schedule_config().get("schedule_templates")
        return [
            {
                "start": str(s.get("start_time", "") or ""),
                "end": str(s.get("end_time", "") or ""),
                "name": str(s.get("name", "") or ""),
            }
            for s in templates
            if isinstance(s, dict)
        ]

    def resolve(self, *, now: datetime.datetime, session: str) -> ScheduleResolution | None:
        """同步解析当前有效时间线；缺失或无效时由调用方使用静态日程。"""
        snapshot = self.get_snapshot_for_session(session, now=now)
        if not snapshot:
            return None
        slots = self._render_snapshot(snapshot, now)
        slot = find_active_daily_slot(slots, now)
        if slot is None:
            return None
        state = str(slot.get("state", "")).strip()
        if not state:
            return None
        return ScheduleResolution(
            state_prompt=state,
            slot_name=str(slot.get("name", "")).strip(),
            source=str(snapshot.get("source", "ai")),
            snapshot_id=str(snapshot.get("snapshot_id", "")),
        )

    @staticmethod
    def _context_now_for_date(
        now: datetime.datetime,
        target_date: datetime.date,
    ) -> datetime.datetime:
        """构造目标日期的生成上下文时间；预生成时从 00:00 开始完整规划。"""
        if target_date == now.date():
            return now
        return datetime.datetime.combine(
            target_date,
            datetime.time.min,
            tzinfo=now.tzinfo,
        )

    def queue_generation(
        self,
        session: str,
        *,
        force: bool = False,
        target_date: datetime.date | None = None,
    ) -> bool:
        """非阻塞提交生成；同一 persona/date 一个活动任务；日级熔断时自动生成被抑制，force 可绕过。"""
        if not self.enabled():
            logger.debug(f"{tag()} 跳过排队：AI 每日日程未启用")
            return False
        session = str(session or "").strip()
        persona_hash = self._identity_for(session)
        if not persona_hash:
            logger.debug(f"{tag()} 跳过排队：Persona 未解析（空身份） session={session}")
            return False
        persona = self._identity_object_for(session)
        # 排队不是 UMO「出现」，不刷新保活时间戳（只由 register 路径刷新）。
        self._representative_sessions[persona_hash] = session
        now = self.time_context.now()
        local_date = target_date or now.date()
        target_now = self._context_now_for_date(now, local_date)
        timezone = self._timezone_key(target_now)
        snapshot = self.store.get(persona_hash, local_date, timezone)
        if not force and self._snapshot_usable(snapshot, target_now, persona_hash):
            logger.debug(
                f"{tag()} 跳过排队：当日快照已可用 "
                f"persona={self._identity_id_for(session)} date={local_date.isoformat()}"
            )
            return False
        # 日级熔断：已有终态 failure 时抑制自动生成；先判快照可用再判 failure。
        if not force and self.store.get_failure(persona_hash, local_date, timezone):
            logger.debug(
                f"{tag()} 跳过排队：本日已熔断（存在终态 failure） "
                f"persona={self._identity_id_for(session)} date={local_date.isoformat()}"
            )
            return False
        task_key = self.store.snapshot_key(persona_hash, local_date, timezone)
        current = self._tasks.get(task_key)
        if current is not None and not current.done():
            logger.debug(
                f"{tag()} 跳过排队：已有进行中的生成任务 "
                f"persona={self._identity_id_for(session)} date={local_date.isoformat()}"
            )
            return False
        frozen = FrozenIdentity(
            session=session,
            persona_hash=persona_hash,
            persona_id=self._identity_id_for(session),
            persona=persona,
            timezone=timezone,
            local_date=local_date,
            now=now,
        )
        try:
            task = asyncio.create_task(self._generate_with_retries(frozen, force=force))
        except RuntimeError:
            logger.debug(f"{tag()} 排队失败：事件循环不可用（RuntimeError）")
            return False
        self._tasks[task_key] = task
        logger.debug(
            f"{tag()} 已排队生成: persona={frozen.persona_id} "
            f"date={local_date.isoformat()} force={force}"
        )

        def _done(done_task: asyncio.Task) -> None:
            if self._tasks.get(task_key) is done_task:
                self._tasks.pop(task_key, None)
            if not done_task.cancelled():
                try:
                    done_task.exception()
                except Exception:
                    pass

        task.add_done_callback(_done)
        return True

    async def queue_known_profiles(
        self,
        *,
        target_date: datetime.date | None = None,
    ) -> int:
        """每日生成扫描：回收不活跃 UMO 后逐一重新解析 Persona 并去重排队。"""
        self._expire_stale_sessions(self.time_context.now())
        # _session_active_at 是完整保活集合；touch_active=False：扫描不刷新保活时间戳
        sessions = list(self._session_active_at)
        personas: dict[str, str] = {}  # persona_hash → 代表会话
        for session in sessions:
            persona_hash = await self.register_session_async(
                session, trigger=False, touch_active=False
            )
            if persona_hash:
                personas.setdefault(persona_hash, session)
        queued = 0
        for persona_hash, session in personas.items():
            if self.queue_generation(session, target_date=target_date):
                queued += 1
        logger.debug(
            f"{tag()} 每日生成扫描: 保活会话 {len(sessions)} → "
            f"解析出 {len(personas)} 个 Persona，排队 {queued} 个"
        )
        return queued

    async def _generate_with_retries(
        self,
        frozen: FrozenIdentity,
        *,
        force: bool,
    ) -> GenerationResult:
        """后台生成任务：使用冻结身份执行；重试已下移到 generation 内部。"""
        return await self.generate_for_session(frozen=frozen, force=force)

    async def generate_for_session(
        self,
        session: str = "",
        *,
        force: bool = False,
        now: datetime.datetime | None = None,
        event: Any = None,
        frozen: FrozenIdentity | None = None,
    ) -> GenerationResult:
        """为当前 Persona 生成当天快照；frozen 由后台任务传入，否则每次重新解析 Persona。"""
        if not self.enabled():
            return GenerationResult(
                False,
                error_type="disabled",
                message="AI 每日日程尚未启用",
                retryable=False,
            )
        session = str(session or "").strip()
        if frozen is not None:
            session = frozen.session
            persona_hash = frozen.persona_hash
            persona = frozen.persona
            actual_now = frozen.now
            local_date = frozen.local_date
            timezone = frozen.timezone
            now = self._context_now_for_date(actual_now, local_date)
            persona_label = frozen.persona_id or persona_hash
        else:
            # 每次生成前重新解析 Persona：切换后立即对应到新身份
            resolved_id, persona = await self._resolve_persona(session, event)
            resolved_id = str(resolved_id or "").strip()
            if not resolved_id:
                self._drop_stale_identity(session)
                return GenerationResult(
                    False,
                    error_type="persona_unavailable",
                    message="无法解析当前 Persona。AI 日程以 Persona 为隔离维度，请为会话配置生效人设后再试",
                    retryable=False,
                )
            try:
                persona_hash = self.store.persona_hash(resolved_id)
            except Exception as exc:
                logger.warning(
                    f"{tag()} ⚠️ AI 日程 Persona 身份哈希失败: {type(exc).__name__}"
                )
                return GenerationResult(
                    False,
                    error_type="persona_unavailable",
                    message="无法解析当前 Persona。AI 日程以 Persona 为隔离维度，请为会话配置生效人设后再试",
                    retryable=False,
                )
            persona_label = resolved_id or persona_hash
            self._persona_cache[session] = (persona_hash, persona, resolved_id)
            self._mark_session_active(session)
            self._representative_sessions[persona_hash] = session
            now = now or self.time_context.now()
            actual_now = now
            local_date = now.date()
            timezone = self._timezone_key(now)
        lock_key = self.store.snapshot_key(persona_hash, local_date, timezone)
        lock = self._locks.setdefault(lock_key, asyncio.Lock())

        async with lock:
            existing = self.store.get(persona_hash, local_date, timezone)
            if not force and self._snapshot_usable(existing, now, persona_hash):
                return GenerationResult(
                    True,
                    source=str(existing.get("source", "ai")),
                    slot_count=len(self._render_snapshot(existing, now)),
                    snapshot_id=str(existing.get("snapshot_id", "")),
                    reused=True,
                )

            try:
                enhanced = await self._build_enhanced_context(
                    persona_hash=persona_hash,
                    now=now,
                    finalize_previous=local_date <= actual_now.date(),
                )
                forecast = None
                if self._weather_forecast_provider is not None:
                    try:
                        forecast = await self._weather_forecast_provider(local_date)
                    except Exception:
                        forecast = None
                generated = await self.generation.generate(
                    session=session,
                    now=now,
                    enhanced=enhanced,
                    persona=persona,
                    forecast=forecast,
                )

                generated_at = (
                    actual_now
                    if actual_now.tzinfo is not None
                    else actual_now.astimezone()
                )
                # 次日预生成从 00:00 保留完整规划；当天补生成仍冻结已过去分钟。
                cutoff = (
                    next_minute_cutoff(actual_now)
                    if local_date == actual_now.date()
                    else 0
                )
                current_static = self._current_static_slots()
                ai_priority = self._ai_priority_over_static()
                # 分层：先推进冻结边界（executed 固化、各未来层裁剪）
                base = dict(existing or {}) if isinstance(existing, dict) else {}
                if base and base.get("executed_until"):
                    advanced = advance_executed(
                        base, cutoff, ai_priority
                    )
                else:
                    # 首次创建：cutoff 之前严格冻结为空
                    advanced = {
                        "executed_slots": [],
                        "executed_until": format_time(cutoff),
                        "user_slots": [],
                        "ai_slots": [],
                        "static_slots": [],
                    }
                snapshot = {
                    "snapshot_id": uuid.uuid4().hex,
                    "local_date": local_date.isoformat(),
                    "timezone": timezone,
                    "persona_hash": persona_hash,
                    "generated_at": generated_at.isoformat(),
                    "source": generated.source,
                    "executed_until": advanced["executed_until"],
                    "executed_slots": advanced["executed_slots"],
                    # 用户覆盖层：force（regenerate）清空；普通补生成保留
                    "user_slots": [] if force else advanced["user_slots"],
                    # 新 AI 只从 cutoff 之后生效，不回填已冻结的过去部分
                    "ai_slots": clip_slots(generated.slots, cutoff, 24 * 60),
                    # 静态日程副本：关键操作时采用（推进历史用旧副本）
                    "static_slots": current_static,
                    "provider": generated.provider_id,
                    "prompt_revision": PROMPT_REVISION,
                    "status": "ready",
                }
                # 存储恒全维度：无论评估成功与否都写入（失败为全 null 种子）
                snapshot["boundary_state"] = generated.boundary_state
                # slots 仅作为兼容/UI缓存；权威来源仍是四个分层字段。
                snapshot["slots"] = render_effective_timeline(
                    snapshot, cutoff, current_static, ai_priority
                )
                if not self.store.save_ready(
                    snapshot,
                    retention_days=self.retention_days(),
                    today=actual_now.date(),
                ):
                    raise DailyScheduleGenerationError(
                        "persistence_error",
                        "AI 日程已经生成，但快照持久化失败",
                        retryable=True,
                        calls_executed=generated.calls_executed,
                    )
                skipped = ",".join(generated.skipped) or "-"
                logger.info(
                    f"{tag()} ✅ AI 每日日程就绪: persona={persona_label} "
                    f"date={local_date.isoformat()} source={generated.source} "
                    f"slots={len(generated.slots)}"
                )
                logger.debug(
                    f"{tag()} 生成细节: provider={generated.provider_id} "
                    f"calls={generated.calls_executed} skipped={skipped} "
                    f"context_tokens={generated.context_tokens} "
                    f"boundary_state={'yes' if generated.boundary_state else 'no'}"
                )
                return GenerationResult(
                    True,
                    source=generated.source,
                    slot_count=len(generated.slots),
                    snapshot_id=snapshot["snapshot_id"],
                    calls_executed=generated.calls_executed,
                )
            except DailyScheduleGenerationError as exc:
                self._record_failure(
                    persona_hash,
                    local_date,
                    actual_now,
                    exc.error_type,
                    timezone,
                    detail=exc.detail,
                )
                logger.warning(
                    f"{tag()} ⚠️ AI 每日日程生成失败: persona={persona_label} "
                    f"date={local_date.isoformat()} error={exc.error_type}"
                    + (f" detail={exc.detail}" if exc.detail else "")
                )
                return GenerationResult(
                    False,
                    error_type=exc.error_type,
                    message=exc.user_message,
                    retryable=exc.retryable,
                    calls_executed=exc.calls_executed,
                )
            except Exception as exc:
                self._record_failure(
                    persona_hash,
                    local_date,
                    actual_now,
                    type(exc).__name__,
                    timezone,
                    detail=exception_detail(exc),
                )
                logger.warning(
                    f"{tag()} ⚠️ AI 每日日程未知失败: persona={persona_label} "
                    f"date={local_date.isoformat()} error={type(exc).__name__} "
                    f"detail={exception_detail(exc)}"
                )
                return GenerationResult(
                    False,
                    error_type="internal_error",
                    message="AI 每日日程内部处理失败",
                    retryable=True,
                )

    # ==================== 自适应增强上下文（阶段 C） ====================

    async def _build_enhanced_context(
        self,
        *,
        persona_hash: str,
        now: datetime.datetime,
        finalize_previous: bool = True,
    ) -> EnhancedContext:
        """采集增强上下文原始结构；Token 裁剪由 generation 层统一完成。"""
        policy = await self.generation.policy()
        if not policy.enabled:
            return EnhancedContext()
        skipped: list[str] = []
        recent_text = ""
        recent_snapshots: tuple[dict, ...] = ()
        if policy.use_recent_schedules:
            recent_text, recent_snapshots = self._recent_schedules_text(
                persona_hash, policy, now
            )
            if not recent_text:
                skipped.append("recent_schedules:no_data")

        previous_day_state = None
        if policy.state_continuity_enabled:
            previous_day_state = self._previous_day_state(
                persona_hash,
                now,
                finalize_previous=finalize_previous,
                window_days=(
                    policy.recent_days if policy.recent_days > 0 else 7
                ),
            )

        return EnhancedContext(
            recent_schedules_text=recent_text,
            recent_snapshots=recent_snapshots,
            previous_day_state=previous_day_state,
            skipped=tuple(skipped),
        )

    def _recent_schedules_text(
        self,
        persona_hash: str,
        policy: AdaptiveGenerationPolicy,
        now: datetime.datetime,
    ) -> tuple[str, tuple[dict, ...]]:
        """提取最近 N 天快照压缩摘要；同时返回动态渲染时间线供反重复消费。"""
        date = now.date()
        timezone = self._timezone_key(now)
        snapshots: list[dict] = []
        for offset in range(1, min(policy.recent_days, 30) + 1):
            snapshot = self.store.get(
                persona_hash,
                date - datetime.timedelta(days=offset),
                timezone,
            )
            if isinstance(snapshot, dict) and snapshot.get("status") == "ready":
                snapshots.append(snapshot)
        if not snapshots:
            return "", ()
        entries = []
        rendered_snapshots: list[dict] = []
        for snapshot in snapshots:
            # 动态渲染全天时间线，不依赖 slots 缓存时效
            rendered = self._render_full_day(snapshot)
            slots = [
                {
                    "name": str(slot.get("name", "") or "")[:24],
                    "start": str(slot.get("start", "") or ""),
                    "end": str(slot.get("end", "") or ""),
                    "state_fingerprint": " ".join(str(slot.get("state", "")).split())[:40],
                }
                for slot in rendered
                if isinstance(slot, dict)
            ]
            entries.append({"date": str(snapshot.get("local_date", "")), "slots": slots})
            # 反重复消费的动态时间线（含用户修改后的生效版本），携带 boundary_state/local_date。
            rendered_snapshots.append(
                {
                    "slots": rendered,
                    "boundary_state": snapshot.get("boundary_state") or {},
                    "local_date": str(snapshot.get("local_date", "") or ""),
                }
            )
        return (
            json.dumps(entries, ensure_ascii=False, sort_keys=True),
            tuple(rendered_snapshots),
        )

    def _previous_day_state(
        self,
        persona_hash: str,
        now: datetime.datetime,
        *,
        finalize_previous: bool = True,
        window_days: int = 7,
    ) -> dict | None:
        """回溯最近可用角色状态及其边界日起的实际时间线；窗口内无边界走空边界分支。"""
        timezone = self._timezone_key(now)
        window_days = max(1, min(365, int(window_days or 7)))
        days = [
            now.date() - datetime.timedelta(days=offset)
            for offset in range(1, window_days + 1)
        ]

        def _schedule_of(snap: dict | None) -> list[dict]:
            snap = snap or {}
            slots = snap.get("slots")
            if not isinstance(slots, list):
                return []
            return [
                {
                    "name": str(slot.get("name", "") or "")[:24],
                    "start": str(slot.get("start", "") or "")[:5],
                    "end": str(slot.get("end", "") or "")[:5],
                    "state": " ".join(str(slot.get("state", "") or "").split())[:MAX_STATE_LENGTH],
                }
                for slot in slots[: self.generation.max_slots()]
                if isinstance(slot, dict)
            ]

        rendered: list[tuple[datetime.date, list[dict], dict]] = []
        for day in days:
            snapshot = self.store.get(persona_hash, day, timezone)
            boundary: dict = {}
            if isinstance(snapshot, dict) and snapshot.get("status") == "ready":
                if str(snapshot.get("executed_until", "")) != "24:00":
                    # 日末未推进：用该日旧 static_slots 补齐，不得用最新静态配置反写历史。
                    ai_priority = self._ai_priority_over_static()
                    old_static = snapshot.get("static_slots", [])
                    advanced = advance_executed(
                        snapshot, MINUTES_PER_DAY, ai_priority
                    )
                    snapshot = {**snapshot, **advanced}
                    snapshot["slots"] = render_effective_timeline(
                        snapshot, MINUTES_PER_DAY, old_static, ai_priority
                    )
                    if finalize_previous:
                        self.store.save_ready(
                            snapshot,
                            retention_days=self.retention_days(),
                            today=day,
                        )
                timeline = _schedule_of({"slots": self._render_full_day(snapshot)})
                boundary = snapshot.get("boundary_state")
                if not isinstance(boundary, dict):
                    boundary = {}
            else:
                # 无快照：实际生效的是静态日程时间线
                timeline = _schedule_of({"slots": self._current_static_slots()})
            rendered.append((day, timeline, boundary))

        # 最近一次可用边界：rendered 顺序为 昨天 → 更早，取最靠前的非空 boundary
        anchor_index = next(
            (
                index
                for index, (_, _, boundary) in enumerate(rendered)
                if boundary
            ),
            None,
        )
        # 自边界日起按日期升序携带实际时间线；空边界分支携带整个窗口。
        if anchor_index is not None:
            selected = reversed(rendered[: anchor_index + 1])
        else:
            selected = reversed(rendered)
        actual_schedules = [
            {"date": day.isoformat(), "slots": timeline}
            for day, timeline, _ in selected
        ]
        boundary_state = (
            rendered[anchor_index][2] if anchor_index is not None else {}
        )
        if not boundary_state and not any(
            item["slots"] for item in actual_schedules
        ):
            return None
        return {
            "boundary_state": boundary_state,
            "boundary_date": (
                rendered[anchor_index][0].isoformat()
                if anchor_index is not None
                else ""
            ),
            "actual_schedules": actual_schedules,
        }

    def _record_failure(
        self,
        persona_hash: str,
        local_date: datetime.date,
        now: datetime.datetime,
        error_type: str,
        timezone: str = "",
        detail: str = "",
    ) -> None:
        failed_at = now if now.tzinfo is not None else now.astimezone()
        self.store.record_failure(
            persona_hash=persona_hash,
            timezone=timezone,
            local_date=local_date,
            failed_at=failed_at.isoformat(),
            error_type=error_type,
            detail=detail,
            retention_days=self.retention_days(),
            today=now.date(),
        )

    @staticmethod
    def _parse_generation_time(value: str) -> tuple[int, int, int]:
        """返回 ``(hour, minute, target_day_offset)``；前导 ``-`` 表示次日。"""
        raw = str(value or "00:05").strip()
        target_day_offset = 1 if raw.startswith("-") else 0
        if target_day_offset:
            raw = raw[1:].strip()
        try:
            minute_of_day = parse_time(raw, strict=False)
            hour, minute = divmod(minute_of_day, 60)
            return hour, minute, target_day_offset
        except ScheduleValidationError:
            pass
        return 0, 5, 0

    async def run_daily_loop(self) -> None:
        """按配置时刻生成当天快照，或在 ``-HH:MM`` 时预生成次日快照。"""
        try:
            while True:
                now = self.time_context.now()
                hour, minute, target_day_offset = self._parse_generation_time(
                    self._daily_config().get("generation_time", "00:05")
                )
                next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if next_run <= now:
                    next_run += datetime.timedelta(days=1)
                await self._sleep(max(1.0, (next_run - now).total_seconds()))
                current = self.time_context.now()
                self.store.cleanup(
                    retention_days=self.retention_days(),
                    today=current.date(),
                )
                self._cleanup_stale_locks()
                target_date = current.date() + datetime.timedelta(
                    days=target_day_offset
                )
                queued = await self.queue_known_profiles(target_date=target_date)
                logger.debug(
                    f"{tag()} 🗓️ AI 每日日程定时扫描完成: "
                    f"target_date={target_date.isoformat()} queued={queued}"
                )
        except asyncio.CancelledError:
            logger.info(f"{tag()} AI 每日日程循环已取消")

    async def close(self) -> None:
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
