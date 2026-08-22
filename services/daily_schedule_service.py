"""AI 每日日程生成、幂等、回退与运行时解析服务。

隔离维度为 Person：快照以 effective Persona 的 HMAC 身份为键，同一
Persona 的多个 Bot 共享同一份冻结日程；切换 Persona 后立即切换到对应
快照。Persona 视为必备身份——解析失败时不创建快照。
"""

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
    find_active_daily_slot,
    format_time,
    normalize_static_slots,
    parse_time,
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
    """排队时刻冻结的生成上下文：任务执行不再重新解析身份。

    避免「排队时 Persona A → 执行时切换到 B → 任务挂 A 的 key 却写 B 的
    快照」竞态；也保证重试全程使用同一身份与时刻。
    """

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
        self._available = True
        # 进程内身份缓存：session → (persona_hash, persona 对象)
        # （原始 UMO 与 Persona prompt 不落盘，只在内存）
        self._persona_cache: dict[str, tuple[str, Any, str]] = {}
        # 代表会话表：persona_hash → session（多 Bot 同 persona 时共享同一份快照）
        self._representative_sessions: dict[str, str] = {}
        # UMO 最后活跃时间（2.0.0 计划 §3.3 保活：一整天未再出现的 UMO 从
        # 集合中删除；仅进程内存，不持久化）
        self._session_active_at: dict[str, datetime.datetime] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def set_available(self, available: bool) -> None:
        self._available = bool(available)

    def ensure_available(self) -> bool:
        """功能在运行期由命令启用时，按需初始化快照 Store。"""
        if not self.configured_enabled():
            return False
        if self._available:
            return True
        self._available = bool(self.store.load())
        return self._available

    def _daily_config(self) -> dict:
        daily_schedule = self.config.get("daily_schedule", {})
        value = daily_schedule.get("ai_daily", {}) if isinstance(daily_schedule, dict) else {}
        return value if isinstance(value, dict) else {}

    def configured_enabled(self) -> bool:
        daily_schedule = self.config.get("daily_schedule", {})
        return bool(
            isinstance(daily_schedule, dict)
            and daily_schedule.get("enable_schedule", False)
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
        """全局运行期状态保留期（天）：顶层 ``runtime.state_retention_days``。

        只用于回收进程内的未占用日期锁等运行期状态，与 AI 快照文件自身的
        ``daily_schedule.ai_daily.retention_days`` 无关；缺省/非法回退 7。
        """
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
        """回收已过期、未锁定的日程生成锁，防止 ``_locks`` 无界增长。

        只删除日期早于 state_retention_days、且当前未被持有的锁；活动任务
        （正在生成/持锁）的锁一律保留。锁键为
        ``persona_hash:YYYY-MM-DD:tz``，日期固定在第 2 段。
        """
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
            logger.info(
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
        """时区主键片段（后端存储用，必须可靠）。

        - ZoneInfo/IANA：直接返回 IANA key（如 ``Asia/Shanghai``），可靠且可被
          Intl.DateTimeFormat 直接使用。
        - 固定 offset（无 IANA）：归一化为 ``UTC±HH:MM``（如 ``UTC+08:00``），
          这是稳定回退键——前端据此识别为固定偏移，不把它当 IANA 传给 Intl。
        - naive/系统时区不可知或无法归类：回退 ``system-local``，前端按浏览器
          本地处理。绝不把 ``CST`` 之类歧义缩写当 IANA 存储。
        """
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
        # 其余情况：仅当能被 zoneinfo 解析为 IANA 时保留（ZoneInfo.str 本身
        # 就是 IANA key）；否则一律落到稳定回退键 system-local。
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
        """解析当前 effective Persona（一次解析同时产出身份与 prompt 对象）。

        与 LLM 实际请求同源：session 强制规则 → conversation persona →
        provider 默认 persona。返回 ``(resolved_id, persona)``；解析失败或为
        无 Persona 时返回 ``("", None)``（不创建无 Persona 身份）。
        """
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

    def register_session(self, session: str, *, trigger: bool = True) -> str:
        """同步注册代表会话（仅使用已缓存的 Persona 身份）。

        身份需先经 ``register_session_async`` 解析；未缓存时返回空串，不阻塞
        当前路径（首次消息异步补生成会触发解析）。
        """
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
        """异步注册代表会话：解析 effective Persona 并缓存身份。

        每次调用都会重新解析——Persona 切换（会话规则 / conversation /
        默认人设任一变化）后立即刷新身份缓存，下一轮消息即使用新快照；
        解析为空时清除旧缓存，避免把旧 Persona 的日程注入新状态。

        ``touch_active=False`` 用于每日生成扫描（2.0.0 计划 §3.3）：扫描
        只是轮询检查点，不算 UMO「再次出现」，不刷新保活时间戳，否则
        集合会因每日扫描而永不过期。
        """
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
        """删除连续一整天未再出现的 UMO（2.0.0 计划 §3.3）。

        仅从进程内集合（身份缓存/代表会话/活跃时间）移除；不删除 Persona
        快照、日历或提醒数据。重启后集合重新建立，等待 UMO 再次出现。
        """
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
        logger.info(
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
        """2.0.0 分层模型：status=ready 即可复用，不要求任何来源层非空。

        合法空 AI 日程（``ai_slots=[]``）是有效业务结果：不得触发重复生成
        或失败记录；``slots`` 只是可重建缓存，不作为可用性判断依据。
        """
        return bool(
            isinstance(snapshot, dict)
            and snapshot.get("status") == "ready"
            and snapshot.get("persona_hash") == persona_hash
            and snapshot.get("local_date") == now.date().isoformat()
            and snapshot.get("timezone") == self._timezone_key(now)
            and snapshot.get("prompt_revision") == PROMPT_REVISION
        )

    def _render_snapshot(self, snapshot: dict, now: datetime.datetime) -> list[dict]:
        """动态渲染快照的有效时间线（本地计算，不写盘）。

        运行时边界 = 当前时刻的分钟（非 UI 权限的下一整分钟）：当前时刻
        之前的时段按已生效展示（命中判定不受秒数影响），之后按来源层动态
        合并。冻结边界整天不自动写盘推进，因此渲染前先把 executed_until
        动态推进到当前时刻（用快照旧 static 副本，只读不写盘），使已过去
        的时段与未来层连续展示，避免 [executed_until, now) 空白。
        """
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
        """渲染快照的完整全天时间线（只读，不写盘）。

        先把各来源层推进至日末（历史推进用快照旧 static_slots 副本），
        再按当前优先级渲染——结果即「该日实际发生/计划的完整时间线」，
        供最近日程反重复与昨日状态消费。
        """
        ai_priority = self._ai_priority_over_static()
        old_static = snapshot.get("static_slots", [])
        advanced = advance_executed(
            snapshot, MINUTES_PER_DAY, ai_priority
        )
        return render_effective_timeline(
            {**snapshot, **advanced}, MINUTES_PER_DAY, old_static, ai_priority
        )

    def _ai_priority_over_static(self) -> bool:
        daily = self.config.get("daily_schedule", {})
        ai_daily = daily.get("ai_daily", {}) if isinstance(daily, dict) else {}
        return bool(ai_daily.get("ai_priority_over_static", False))

    def _current_static_slots(self) -> list[dict]:
        daily = self.config.get("daily_schedule", {})
        value = daily.get("schedule_templates") if isinstance(daily, dict) else None
        result = []
        for item in value if isinstance(value, list) else []:
            if not isinstance(item, dict):
                continue
            result.append(
                {
                    "name": str(item.get("name", "") or ""),
                    "start": str(item.get("start_time", "") or ""),
                    "end": str(item.get("end_time", "") or ""),
                    "state": str(item.get("state_prompt", "") or ""),
                    "origin": "static",
                }
            )
        # 快照层只保存非跨日的规范化片段；读取旧快照时 timeline 仍会再次
        # 规范化，因此该存储边界变更保持向后兼容。
        return [
            {
                key: item[key]
                for key in ("name", "start", "end", "state", "origin")
                if key in item
            }
            for item in normalize_static_slots(result)
        ]

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
        """非阻塞提交生成；同一 persona/date 保持一个活动任务。

        排队时刻冻结完整身份上下文（``FrozenIdentity``）：任务执行与重试
        全程使用冻结的身份/时区/日期，切换 Persona 不会让 A 的任务去写 B
        的快照。

        日级熔断：本日该 Persona/日期/时区已存在持久化终态 failure 时，
        自动生成被抑制（普通消息再次进来不再重新排队），避免持续 Provider/
        LLM 失败时的调用风暴；``force=True``（手动 regenerate）可绕过。
        成功快照就绪后 ``save_ready`` 会清除 failure，熔断随之解除。
        """
        if not self.enabled():
            return False
        session = str(session or "").strip()
        persona_hash = self._identity_for(session)
        if not persona_hash:
            return False
        persona = self._identity_object_for(session)
        # 注意：排队本身不是 UMO「出现」，不刷新保活时间戳（活跃时间
        # 只由 register 路径刷新，否则每日扫描排队会让集合永不过期）
        self._representative_sessions[persona_hash] = session
        now = self.time_context.now()
        local_date = target_date or now.date()
        target_now = self._context_now_for_date(now, local_date)
        timezone = self._timezone_key(target_now)
        snapshot = self.store.get(persona_hash, local_date, timezone)
        if not force and self._snapshot_usable(snapshot, target_now, persona_hash):
            return False
        # 日级熔断：本日已有持久化终态 failure → 抑制自动生成（调用风暴防护）。
        # 先判快照可用再判 failure：同 key 同时存在 usable 快照时以快照为准。
        if not force and self.store.get_failure(persona_hash, local_date, timezone):
            return False
        task_key = self.store.snapshot_key(persona_hash, local_date, timezone)
        current = self._tasks.get(task_key)
        if current is not None and not current.done():
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
            return False
        self._tasks[task_key] = task

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
        """每日生成扫描（2.0.0 计划 §3.2/§3.3）。

        先回收一整天未活跃的 UMO，再对仍保活的 UMO 集合**逐一重新解析**
        effective Persona，按解析后的身份去重，每个 Persona 只排队一次
        生成（解析失败/为空的本轮不生成）。
        """
        self._expire_stale_sessions(self.time_context.now())
        # _session_active_at 是完整保活 UMO 集合（代表会话表只保留每 Persona 一个）
        # touch_active=False：扫描不是 UMO「再次出现」，不刷新保活时间戳
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
        return queued

    async def _generate_with_retries(
        self,
        frozen: FrozenIdentity,
        *,
        force: bool,
    ) -> GenerationResult:
        """后台生成任务：使用冻结身份执行。

        2.0.0 起重试权下移：Step1（角色状态评估）与 Step2（日程规划）的
        失败都由 generation 内部按 0/5/30s 各自重试（上限 max_attempts）；
        此处不再整体重试，避免 Step2 失败导致 Step1 被重复调用。
        """
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
        """为当前 Persona 生成当天快照；失败不覆盖旧快照。

        ``frozen`` 由后台任务传入（排队时刻冻结的身份/时区/日期）；交互路径
        （命令）传入 None，此时每次重新解析 Persona——一次解析同时得到身份
        与 prompt 对象，保证快照归属与注入内容同源。
        """
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
            # 日志诊断用：真实 Persona ID 优先，缺失时回退完整 hash
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
                generated = await self.generation.generate(
                    session=session,
                    now=now,
                    enhanced=enhanced,
                    persona=persona,
                )

                generated_at = (
                    actual_now
                    if actual_now.tzinfo is not None
                    else actual_now.astimezone()
                )
                # 提前生成次日日程时从 00:00 保留完整规划；当天补生成仍冻结
                # 已经过的分钟，不能让未来预生成时间误裁掉目标日的大部分时段。
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
                    f"slots={len(generated.slots)} provider={generated.provider_id} "
                    f"calls={generated.calls_executed} "
                    f"skipped={skipped} context_tokens={generated.context_tokens} "
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
                )
                logger.warning(
                    f"{tag()} ⚠️ AI 每日日程生成失败: persona={persona_label} "
                    f"date={local_date.isoformat()} error={exc.error_type}"
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
                )
                logger.warning(
                    f"{tag()} ⚠️ AI 每日日程未知失败: persona={persona_label} "
                    f"date={local_date.isoformat()} error={type(exc).__name__}"
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
        """提取最近 N 天快照的压缩摘要（时段边界 + 名称 + 状态指纹）。

        同一 Persona、同一时区的最近快照，不含任何身份信息；同时返回动态
        渲染后的时间线供反重复/相似度消费（2.0.0 计划 §2.2：相似度计算不走
        ``slots`` 缓存，统一由渲染入口重建）。裁剪由总预算统一处理。
        """
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
            # 反重复/相似度消费的动态时间线（含用户修改后的实际生效版本）
            rendered_snapshots.append({"slots": rendered})
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
        """回溯最近一次可用角色状态，并携带自该边界日起到昨天为止的实际时间线。

        供 Step1 边界评估使用：前一个可用 boundary + 连续实际日程 →
        当日初始角色状态。前日状态推导统一由动态渲染入口重建（2.0.0
        计划 §2.2，不读 ``slots`` 缓存）。

        - 逐天回溯（窗口 ``window_days``，缺省 7、由 recent_days 覆盖）找到
          最近一个 ``status=ready`` 且 ``boundary_state`` 非空的快照 D；
        - 窗口内找不到（断链超过窗口 / 全新开始）→ 空边界分支：boundary 为空，
          但仍携带窗口内每天的实际时间线（对 Step1 推导仍有因果价值）；
        - 某天没有 ready 快照（AI 缺失/生成失败）→ 该天实际时间线 = 静态日程；
          实际跑了什么就参考什么，不为失败的天虚构 AI 日程；
        - 有快照且日末尚未推进到 24:00 时，先按该日采用的来源层（旧
          static_slots 副本）补齐至日末；``finalize_previous=True`` 时固化写盘，
          前一天预生成次日时只使用计划视图，不提前冻结尚未真正发生的时间。
        """
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
            result = []
            for slot in slots[: self.generation.max_slots()]:
                if not isinstance(slot, dict):
                    continue
                result.append(
                    {
                        "name": str(slot.get("name", "") or "")[:24],
                        "start": str(slot.get("start", "") or "")[:5],
                        "end": str(slot.get("end", "") or "")[:5],
                        "state": " ".join(str(slot.get("state", "") or "").split())[:MAX_STATE_LENGTH],
                    }
                )
            return result

        rendered: list[tuple[datetime.date, list[dict], dict]] = []
        for day in days:
            snapshot = self.store.get(persona_hash, day, timezone)
            boundary: dict = {}
            if isinstance(snapshot, dict) and snapshot.get("status") == "ready":
                if str(snapshot.get("executed_until", "")) != "24:00":
                    # 日末尚未推进：用该日旧 static_slots 补齐至日末，
                    # 不得用最新静态配置反写历史。
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
        # 自边界日起（含边界日当天）携带实际时间线，按日期升序（D → 昨天）；
        # 空边界分支携带整个窗口（T-window → 昨天）
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
    ) -> None:
        failed_at = now if now.tzinfo is not None else now.astimezone()
        self.store.record_failure(
            persona_hash=persona_hash,
            timezone=timezone,
            local_date=local_date,
            failed_at=failed_at.isoformat(),
            error_type=error_type,
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
                # 回收过期未占用的日期生成锁（仅内存运行期状态）
                self._cleanup_stale_locks()
                target_date = current.date() + datetime.timedelta(
                    days=target_day_offset
                )
                queued = await self.queue_known_profiles(target_date=target_date)
                logger.info(
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
