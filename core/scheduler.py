"""主动提醒调度器。

调度器只负责任务队列、持久化和状态迁移，不依赖 AstrBot context；到期后的
LLM/消息处理通过 ``trigger_callback`` 注入。

v2 任务状态：

``pending`` → ``processing`` → ``sent``
                           ↘ ``retry_wait`` → ``processing``
                           ↘ ``failed``

进入 ``processing`` 前会先持久化 claim，发送成功后才确认 ``sent``。进程在 LLM
阶段崩溃时可恢复重试；进入装饰/发送阶段后会持久化 ``delivery_started``，重启时
转为 ``failed``，避免第三方装饰器已经发送部分消息后整条重复。
"""

from __future__ import annotations

import asyncio
import datetime
import os
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from ..log import logger, tag
from ..utils.time_utils import UTC, to_utc, utc_now
from ._datafile import atomic_write_yaml, load_mapping


TASKS_FILE_NAME = "reminder_tasks.yaml"
TASKS_DATA_VERSION = 2

MERGE_WINDOW_SECONDS = 60
IDLE_SLEEP_SECONDS = 300
MIN_SLEEP_SECONDS = 5
DEFAULT_MAX_ATTEMPTS = 3
RETRY_BASE_SECONDS = 60
RETRY_MAX_SECONDS = 15 * 60

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_RETRY_WAIT = "retry_wait"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"

ACTIVE_STATUSES = {STATUS_PENDING, STATUS_RETRY_WAIT}
TERMINAL_STATUSES = {STATUS_SENT, STATUS_FAILED, STATUS_CANCELLED, STATUS_EXPIRED}


@dataclass(frozen=True)
class TriggerResult:
    """到期回调返回的处理结果。

    ``retryable`` 只应在尚未产生外部发送副作用时使用。主动消息装饰链可能
    自行发送前 N-1 段，进入该阶段后的失败由上层返回不可重试结果，避免重复消息。
    """

    success: bool
    retryable: bool = False
    error: str = ""

    @classmethod
    def ok(cls) -> "TriggerResult":
        return cls(success=True)

    @classmethod
    def fail(cls, error: str, *, retryable: bool) -> "TriggerResult":
        return cls(success=False, retryable=retryable, error=str(error))


@dataclass
class Task:
    """单条提醒任务。时间字段在内存和 v2 文件中均为 UTC aware。"""

    id: str
    kind: str  # calendar | followup | user
    session: str
    fire_at: datetime.datetime
    hint: str = ""
    target_user_id: str = ""
    status: str = STATUS_PENDING
    attempts: int = 0
    last_error: str = ""
    next_retry_at: datetime.datetime | None = None
    processing_started_at: datetime.datetime | None = None
    delivery_started: bool = False
    created_at: datetime.datetime = field(default_factory=utc_now)
    # 进入终态（sent/cancelled/expired/failed）的时刻；用于按全局
    # runtime.state_retention_days 回收终态任务。旧格式无此字段时回退
    # created_at/fire_at。
    terminal_at: datetime.datetime | None = None

    def due_at(self) -> datetime.datetime:
        if self.status == STATUS_RETRY_WAIT and self.next_retry_at is not None:
            return self.next_retry_at
        return self.fire_at

    @classmethod
    def from_dict(
        cls,
        raw: dict,
        *,
        default_tz: datetime.tzinfo | None = None,
    ) -> Optional["Task"]:
        try:
            fire_at = _parse_datetime(raw["fire_at"], default_tz=default_tz)
            created_at = _parse_datetime(
                raw.get("created_at", raw["fire_at"]),
                default_tz=default_tz,
            )

            status = str(raw.get("status", "")).strip().lower()
            if not status:
                # v1 → v2：旧版 sent/given_up 标记映射为终态。
                if bool(raw.get("sent", False)):
                    status = STATUS_SENT
                elif bool(raw.get("given_up", False)):
                    status = STATUS_EXPIRED
                else:
                    status = STATUS_PENDING
            if status not in ACTIVE_STATUSES | {STATUS_PROCESSING} | TERMINAL_STATUSES:
                logger.warning(f"{tag()} ⚠️ 未知任务状态 {status!r}，回退 pending")
                status = STATUS_PENDING

            next_retry_raw = raw.get("next_retry_at")
            terminal_raw = raw.get("terminal_at")
            processing_raw = raw.get("processing_started_at")
            terminal_at = (
                _parse_datetime(terminal_raw, default_tz=default_tz)
                if terminal_raw
                else None
            )
            next_retry_at = (
                _parse_datetime(next_retry_raw, default_tz=default_tz)
                if next_retry_raw
                else None
            )
            processing_started_at = (
                _parse_datetime(processing_raw, default_tz=default_tz)
                if processing_raw
                else None
            )
            attempts = max(0, int(raw.get("attempts", 0) or 0))
            if status == STATUS_RETRY_WAIT and next_retry_at is None:
                status = STATUS_PENDING

            task_id = str(raw["id"]).strip()
            kind = str(raw["kind"]).strip()
            session = str(raw["session"]).strip()
            if not task_id or not session or kind not in {"calendar", "followup", "user"}:
                raise ValueError("任务 id/session/kind 无效")

            return cls(
                id=task_id,
                kind=kind,
                session=session,
                fire_at=fire_at,
                hint=str(raw.get("hint", "")),
                target_user_id=str(raw.get("target_user_id", "")),
                status=status,
                attempts=attempts,
                last_error=str(raw.get("last_error", "")),
                next_retry_at=next_retry_at,
                processing_started_at=processing_started_at,
                delivery_started=bool(raw.get("delivery_started", False)),
                created_at=created_at,
                terminal_at=terminal_at,
            )
        except (KeyError, TypeError, ValueError) as e:
            logger.warning(f"{tag()} ⚠️ 跳过无效任务条目: {e}")
            return None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "session": self.session,
            "fire_at": to_utc(self.fire_at).isoformat(),
            "hint": self.hint,
            "target_user_id": self.target_user_id,
            "status": self.status,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "next_retry_at": (
                to_utc(self.next_retry_at).isoformat()
                if self.next_retry_at is not None
                else None
            ),
            "processing_started_at": (
                to_utc(self.processing_started_at).isoformat()
                if self.processing_started_at is not None
                else None
            ),
            "delivery_started": self.delivery_started,
            "created_at": to_utc(self.created_at).isoformat(),
            "terminal_at": (
                to_utc(self.terminal_at).isoformat()
                if self.terminal_at is not None
                else None
            ),
        }


@dataclass
class DueGroup:
    """同一会话、同一目标用户、相邻触发时间窗口内的任务集合。

    ``target_user_id`` 与分组键一致：不同目标用户（含空目标）的个人任务
    不会被合并到同一组，避免发送阶段把任务发给错误的目标。
    """

    session: str
    tasks: list[Task]
    is_late: bool = False
    late_minutes: int = 0
    target_user_id: str = ""


def _parse_datetime(
    value,
    *,
    default_tz: datetime.tzinfo | None = None,
) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        dt = value
    else:
        dt = datetime.datetime.fromisoformat(str(value))
    return to_utc(dt, default_tz=default_tz)


class ReminderScheduler:
    """持久化提醒队列和单一 asyncio 调度循环。"""

    def __init__(
        self,
        data_dir: str,
        trigger_callback: Callable[[DueGroup], Awaitable[TriggerResult | bool | None]],
        now_provider: Optional[Callable[[], datetime.datetime]] = None,
    ):
        self.data_dir = data_dir
        self._trigger_callback = trigger_callback
        self._tasks: list[Task] = []
        self._tasks_file = os.path.join(data_dir, TASKS_FILE_NAME)
        self._loop_task: Optional[asyncio.Task] = None
        self._wakeup_event: Optional[asyncio.Event] = None
        self._stopped = True
        self._max_late_minutes_value = 60
        # 终态任务（sent/cancelled/expired/failed）内存保留期（天），
        # 由全局 runtime.state_retention_days 注入；缺省 7。
        self._state_retention_days = 7
        self._max_attempts = DEFAULT_MAX_ATTEMPTS
        self._now_provider = now_provider or (lambda: datetime.datetime.now())
        self._persistence_dirty = False

    def _raw_now(self) -> datetime.datetime:
        return self._now_provider()

    def _now_utc(self) -> datetime.datetime:
        return to_utc(self._raw_now())

    def _legacy_default_tz(self) -> datetime.tzinfo:
        now = self._raw_now()
        return now.tzinfo or datetime.datetime.now().astimezone().tzinfo or UTC

    def _to_internal_time(self, dt: datetime.datetime) -> datetime.datetime:
        return to_utc(dt, default_tz=self._legacy_default_tz())

    def set_max_late_minutes(self, minutes: int) -> None:
        # 0 = 不检查迟到/不设最大迟到窗口（不清理也不超窗过期，
        # 到期即发送，哪怕离线很久）；正数保持既有迟到容忍语义。
        self._max_late_minutes_value = max(0, int(minutes))

    def set_max_attempts(self, attempts: int) -> None:
        self._max_attempts = max(1, min(10, int(attempts)))

    def set_state_retention_days(self, days: int) -> None:
        """终态任务内存保留期（天）；非法/缺失回退 7，限 1-365。"""
        try:
            parsed = int(days)
        except (TypeError, ValueError):
            parsed = 7
        self._state_retention_days = max(1, min(365, parsed))

    def state_retention_days(self) -> int:
        """当前生效的终态任务保留期（天）。"""
        return self._state_retention_days

    # ==================== 持久化 ====================

    def load(self) -> None:
        if not os.path.exists(self._tasks_file):
            self._tasks = []
            return
        data = load_mapping(self._tasks_file)
        if data is None:
            self._tasks = []
            return

        raw_tasks = data.get("tasks", []) if isinstance(data, dict) else []
        loaded: list[Task] = []
        default_tz = self._legacy_default_tz()
        now = self._now_utc()
        recovered = 0
        for raw in raw_tasks:
            if not isinstance(raw, dict):
                continue
            task = Task.from_dict(raw, default_tz=default_tz)
            if task is None:
                continue
            if task.status == STATUS_PROCESSING:
                # 进程可能在 claim 后崩溃：恢复为可重试任务，而不是丢失。
                task.processing_started_at = None
                if task.delivery_started:
                    task.status = STATUS_FAILED
                    task.terminal_at = now
                    task.last_error = (
                        task.last_error
                        or "插件在消息装饰/发送阶段退出，为避免重复消息不自动重试"
                    )
                elif task.attempts >= self._max_attempts:
                    task.status = STATUS_FAILED
                    task.terminal_at = now
                    task.last_error = task.last_error or "插件在发送处理中退出，已达到最大重试次数"
                elif (
                    self._max_late_minutes_value > 0
                    and task.fire_at
                    < now
                    - datetime.timedelta(minutes=self._max_late_minutes_value)
                ):
                    # 崩溃恢复同样受迟到容忍窗口约束：离线过久不深夜补发。
                    # 否则处理中任务可绕过 _cleanup_expired（它只清 attempts==0
                    # 的 pending）在数小时后继续发送。max_late==0（不检查迟到）
                    # 时跳过该分支，恢复为可重试任务。
                    task.status = STATUS_EXPIRED
                    task.terminal_at = now
                    task.last_error = "崩溃恢复时已超过迟到容忍窗口"
                else:
                    task.status = STATUS_RETRY_WAIT
                    task.terminal_at = None
                    task.next_retry_at = now
                recovered += 1
            loaded.append(task)
        self._tasks = loaded
        if recovered:
            self.save()
        logger.info(
            f"{tag()} ✅ 已加载 {len(loaded)} 条提醒任务"
            + (f"，恢复 {recovered} 条中断任务" if recovered else "")
        )

    def save(self) -> bool:
        # sent/cancelled/expired 不再进入队列文件；failed 保留供诊断。
        persisted = [
            t.to_dict()
            for t in self._tasks
            if t.status not in {STATUS_SENT, STATUS_CANCELLED, STATUS_EXPIRED}
        ]
        payload = {
            "version": TASKS_DATA_VERSION,
            "last_update": utc_now().isoformat(),
            "tasks": persisted,
        }
        ok = atomic_write_yaml(
            self._tasks_file,
            payload,
            header="time_awareness 提醒任务（v2，自动生成，可手动编辑）",
        )
        if not ok:
            self._persistence_dirty = True
            logger.error(f"{tag()} ❌ 提醒任务保存失败")
        else:
            self._persistence_dirty = False
            try:
                # 任务文件含会话标识与 @ 目标 QQ 号：限制为本机用户可读
                os.chmod(self._tasks_file, 0o600)
            except OSError:
                pass  # Windows/部分挂载不支持 POSIX mode
        return ok

    # ==================== 入队 ====================

    def _new_task(self, *, kind: str, session: str, fire_at: datetime.datetime, **kwargs) -> Task:
        task = Task(
            id=uuid.uuid4().hex,
            kind=kind,
            session=session,
            fire_at=self._to_internal_time(fire_at),
            created_at=self._now_utc(),
            **kwargs,
        )
        self._tasks.append(task)
        if not self.save():
            self._tasks.remove(task)
            raise RuntimeError("提醒任务持久化失败，未加入调度队列")
        self._wake()
        return task

    def add_calendar_task(self, session: str, fire_at: datetime.datetime) -> Task:
        task = self._new_task(kind="calendar", session=session, fire_at=fire_at)
        logger.debug(f"{tag()} 📅 已入队日历提醒: session={session} fire_at={task.fire_at.isoformat()}")
        return task

    def add_followup_task(
        self,
        session: str,
        fire_at: datetime.datetime,
        hint: str,
        target_user_id: str = "",
    ) -> Task:
        task = self._new_task(
            kind="followup",
            session=session,
            fire_at=fire_at,
            hint=hint,
            target_user_id=target_user_id,
        )
        logger.debug(f"{tag()} ⏰ 已入队 LLM 后续任务: id={task.id[:8]} session={session}")
        return task

    def add_user_task(
        self,
        session: str,
        fire_at: datetime.datetime,
        hint: str,
        target_user_id: str = "",
    ) -> Task:
        task = self._new_task(
            kind="user",
            session=session,
            fire_at=fire_at,
            hint=hint,
            target_user_id=target_user_id,
        )
        logger.debug(f"{tag()} 🧑 已入队手动提醒: id={task.id[:8]} session={session}")
        return task

    # ==================== 调度循环 ====================

    def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        if self._wakeup_event is None:
            self._wakeup_event = asyncio.Event()
        self._stopped = False
        self._wakeup_event.clear()
        self._loop_task = asyncio.create_task(self._loop())
        logger.info(f"{tag()} ✅ 提醒调度循环已启动")

    async def stop(self) -> None:
        self._stopped = True
        self._wake()
        if self._loop_task is not None:
            try:
                await asyncio.wait_for(self._loop_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._loop_task.cancel()
                try:
                    await self._loop_task
                except asyncio.CancelledError:
                    pass
            self._loop_task = None
        logger.info(f"{tag()} ✅ 提醒调度循环已停止")

    def _wake(self) -> None:
        if self._wakeup_event is not None:
            self._wakeup_event.set()

    def _next_sleep(self, now: datetime.datetime) -> float:
        active = [t for t in self._tasks if t.status in ACTIVE_STATUSES]
        if not active:
            return IDLE_SLEEP_SECONDS
        next_fire = min(t.due_at() for t in active)
        return max(MIN_SLEEP_SECONDS, (next_fire - now).total_seconds())

    async def _loop(self) -> None:
        while not self._stopped:
            try:
                if self._persistence_dirty:
                    self.save()
                now = self._now_utc()
                self._cleanup_expired(now)
                self._cleanup_terminal_tasks(now)
                sleep_sec = self._next_sleep(now)
                try:
                    await asyncio.wait_for(self._wakeup_event.wait(), timeout=sleep_sec)
                    self._wakeup_event.clear()
                except asyncio.TimeoutError:
                    pass
                if self._stopped:
                    break

                now = self._now_utc()
                self._cleanup_expired(now)
                groups = self._claim_due(now)
                for group_index, group in enumerate(groups):
                    try:
                        raw_result = await self._trigger_callback(group)
                        result = self._normalize_result(raw_result)
                    except asyncio.CancelledError:
                        # _claim_due 会一次性持久化本轮所有组的 processing 状态。
                        # 当前回调被取消时，除当前组外，后续尚未开始的组也必须恢复，
                        # 否则同一个 scheduler 实例再次 start 后它们不会重新入队。
                        for claimed_group in groups[group_index:]:
                            self._recover_processing(
                                claimed_group,
                                "调度器在处理已认领任务期间被取消",
                            )
                        raise
                    except Exception as e:
                        result = TriggerResult.fail(str(e), retryable=True)
                    self._finish_group(group, result, self._now_utc())
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"{tag()} ❌ 调度循环异常: {e}")
                await asyncio.sleep(MIN_SLEEP_SECONDS)

    @staticmethod
    def _normalize_result(raw_result) -> TriggerResult:
        if isinstance(raw_result, TriggerResult):
            return raw_result
        if raw_result is False:
            return TriggerResult.fail("触发回调返回 False", retryable=True)
        return TriggerResult.ok()

    # ==================== 到期任务处理 ====================

    def _cleanup_expired(self, now: datetime.datetime) -> int:
        # max_late_minutes==0 表示“不检查迟到”：pending 不再因超窗被清理为
        # EXPIRED，到期（含离线很久）的任务一律正常发送。
        if self._max_late_minutes_value <= 0:
            return 0
        cutoff = now - datetime.timedelta(minutes=self._max_late_minutes_value)
        count = 0
        for task in self._tasks:
            if task.status != STATUS_PENDING or task.attempts:
                continue
            if task.fire_at < cutoff:
                task.status = STATUS_EXPIRED
                task.terminal_at = now
                task.last_error = "超过迟到容忍窗口"
                count += 1
                logger.warning(
                    f"{tag()} ⚠️ 任务过期丢弃: id={task.id[:8]} kind={task.kind} "
                    f"session={task.session} fire_at={task.fire_at.isoformat()}"
                )
        # 终态任务（sent/cancelled/expired/failed）的回收统一由
        # _cleanup_terminal_tasks 按 runtime.state_retention_days 执行，
        # 不再在这里硬编码 7 天。
        if count:
            self.save()
        return count

    def _cleanup_terminal_tasks(self, now: datetime.datetime) -> int:
        """按 state_retention_days 回收已过期的终态任务（只清内存与队列）。

        终态任务以 terminal_at（旧格式缺省回退 created_at/fire_at）为基准，
        超过保留期即从 _tasks 移除；活动任务（pending/retry_wait/processing）
        一律不回收。
        """
        cutoff = now - datetime.timedelta(days=self._state_retention_days)
        removed: list[Task] = []
        for task in self._tasks:
            if task.status not in TERMINAL_STATUSES:
                continue
            base = task.terminal_at or task.created_at or task.fire_at
            if base < cutoff:
                removed.append(task)
        if not removed:
            return 0
        removed_ids = {task.id for task in removed}
        self._tasks = [
            task for task in self._tasks if task.id not in removed_ids
        ]
        self.save()  # 同步移除队列文件中已到期的终态任务
        logger.debug(
            f"{tag()} 🧹 清理 {len(removed)} 个超过 {self._state_retention_days} "
            "天保留期的终态任务"
        )
        return len(removed)

    def _claim_due(self, now: datetime.datetime) -> list[DueGroup]:
        # 迟到容忍窗口是进入 claim/发送前的最后一道闸：pending 或 retry_wait
        # （含重启恢复、重试路径）一律不得绕过 max_late_minutes 补发。
        # 超窗任务进入既有的过期终态（EXPIRED）并立即落盘；EXPIRED 是终态，
        # save() 会将其从队列文件中移除（与 _cleanup_expired 同一清理语义），
        # 因此本方法不会 claim/发送这类任务。
        # max_late_minutes==0：不设迟到窗口，到期（哪怕已过很久）一律发送，
        # 不做超窗过期。
        cutoff = (
            now - datetime.timedelta(minutes=self._max_late_minutes_value)
            if self._max_late_minutes_value > 0
            else None
        )
        due: list[Task] = []
        expired_count = 0
        for task in self._tasks:
            if task.status not in ACTIVE_STATUSES or task.due_at() > now:
                continue
            if cutoff is not None and task.fire_at < cutoff:
                task.status = STATUS_EXPIRED
                task.terminal_at = now
                task.last_error = "超过迟到容忍窗口"
                task.next_retry_at = None
                expired_count += 1
                logger.warning(
                    f"{tag()} ⚠️ 超窗任务不再发送: id={task.id[:8]} kind={task.kind} "
                    f"session={task.session} fire_at={task.fire_at.isoformat()}"
                )
            else:
                due.append(task)
        if expired_count:
            self.save()
        if not due:
            return []

        # 分组键至少区分 (session, target_user_id)：同一会话中不同目标用户的
        # 个人任务不得合并成一组发送；空目标（会话广播）与非空目标相互隔离。
        # 同一目标用户仍按现有时间窗口合并。
        by_key: dict[tuple[str, str], list[Task]] = {}
        for task in due:
            by_key.setdefault((task.session, task.target_user_id), []).append(task)

        groups: list[DueGroup] = []
        for (session, target_user_id), session_tasks in by_key.items():
            session_tasks.sort(key=lambda task: task.due_at())
            cluster: list[Task] = []
            cluster_start: datetime.datetime | None = None

            def flush() -> None:
                if not cluster:
                    return
                earliest = min(task.fire_at for task in cluster)
                late_seconds = max(0.0, (now - earliest).total_seconds())
                groups.append(
                    DueGroup(
                        session=session,
                        tasks=list(cluster),
                        is_late=late_seconds > MERGE_WINDOW_SECONDS,
                        late_minutes=(int(late_seconds // 60) if late_seconds > MERGE_WINDOW_SECONDS else 0),
                        target_user_id=target_user_id,
                    )
                )

            for task in session_tasks:
                due_at = task.due_at()
                if cluster_start is None or (due_at - cluster_start).total_seconds() <= MERGE_WINDOW_SECONDS:
                    if cluster_start is None:
                        cluster_start = due_at
                    cluster.append(task)
                else:
                    flush()
                    cluster = [task]
                    cluster_start = due_at
            flush()

        snapshots = []
        for group in groups:
            for task in group.tasks:
                snapshots.append(
                    (
                        task,
                        task.status,
                        task.attempts,
                        task.processing_started_at,
                        task.next_retry_at,
                        task.delivery_started,
                    )
                )
                task.status = STATUS_PROCESSING
                task.attempts += 1
                task.processing_started_at = now
                task.next_retry_at = None
                task.delivery_started = False
        if not self.save():
            for (
                task,
                status,
                attempts,
                processing_started_at,
                next_retry_at,
                delivery_started,
            ) in snapshots:
                task.status = status
                task.attempts = attempts
                task.processing_started_at = processing_started_at
                task.next_retry_at = next_retry_at
                task.delivery_started = delivery_started
            logger.error(f"{tag()} ❌ 无法持久化任务 claim，本轮不执行发送")
            return []
        return groups

    def mark_delivery_started(self, tasks: list[Task]) -> bool:
        """在执行可能产生发送副作用的装饰链前持久化边界标记。

        保存失败时回滚标记并返回 False；调用方此时不得进入发送阶段。
        """
        changed = []
        for task in tasks:
            if task.status == STATUS_PROCESSING and not task.delivery_started:
                task.delivery_started = True
                changed.append(task)
        if not changed:
            return True
        if self.save():
            return True
        for task in changed:
            task.delivery_started = False
        return False

    def _recover_processing(self, group: DueGroup, error: str) -> None:
        now = self._now_utc()
        late_cutoff = (
            now - datetime.timedelta(minutes=self._max_late_minutes_value)
            if self._max_late_minutes_value > 0
            else None
        )
        for task in group.tasks:
            if task.status == STATUS_PROCESSING:
                can_retry = not task.delivery_started and task.attempts < self._max_attempts
                if late_cutoff is not None and can_retry and task.fire_at < late_cutoff:
                    # 取消恢复同样受迟到容忍窗口约束：过久不再补发
                    # （max_late==0 不检查迟到时跳过，一律恢复重试）
                    task.status = STATUS_EXPIRED
                    task.terminal_at = now
                    task.last_error = "超过迟到容忍窗口（调度取消恢复）"
                else:
                    task.status = STATUS_RETRY_WAIT if can_retry else STATUS_FAILED
                    task.terminal_at = now if not can_retry else None
                    task.last_error = error
                task.processing_started_at = None
                task.next_retry_at = (
                    self._now_utc() if task.status == STATUS_RETRY_WAIT else None
                )
        self.save()

    def _finish_group(self, group: DueGroup, result: TriggerResult, now: datetime.datetime) -> None:
        for task in group.tasks:
            if task.status != STATUS_PROCESSING:
                continue
            task.processing_started_at = None
            task.last_error = result.error if result.error else ""
            if result.success:
                task.status = STATUS_SENT
                task.terminal_at = now
                task.next_retry_at = None
            elif (
                result.retryable
                and not task.delivery_started
                and task.attempts < self._max_attempts
            ):
                task.status = STATUS_RETRY_WAIT
                task.terminal_at = None
                delay = min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * (2 ** (task.attempts - 1)))
                task.next_retry_at = now + datetime.timedelta(seconds=delay)
                logger.warning(
                    f"{tag()} ⚠️ 任务失败，安排第 {task.attempts + 1} 次尝试: "
                    f"id={task.id[:8]} retry_at={task.next_retry_at.isoformat()} error={task.last_error}"
                )
            else:
                task.status = STATUS_FAILED
                task.terminal_at = now
                task.next_retry_at = None
                logger.error(
                    f"{tag()} ❌ 任务最终失败: id={task.id[:8]} attempts={task.attempts} "
                    f"retryable={result.retryable} error={task.last_error}"
                )
        self.save()

    # ==================== 查询 / 管理 ====================

    def has_calendar_task_for_session_today(
        self,
        session: str,
        today: datetime.date,
        tz: datetime.tzinfo | None = None,
    ) -> bool:
        """判断该会话今天是否已有日历提醒任务（防重复入队）。

        ``today`` 与 ``fire_at`` 必须使用同一时区基准：入队方以插件时区
        创建 fire_at，因此 ``tz`` 应传插件时区；缺省回退系统本地时区
        （宿主时区与插件时区不一致时可能误判，调用方应显式传入）。
        """
        local_tz = tz or self._legacy_default_tz()
        for task in self._tasks:
            if task.kind != "calendar" or task.session != session:
                continue
            # 只有"活着"的任务才阻塞重复入队；failed 是终态（临时故障
            # 不应让当日提醒永久缺席），sent/cancelled/expired 同理。
            if task.status not in ACTIVE_STATUSES | {STATUS_PROCESSING}:
                continue
            if task.fire_at.astimezone(local_tz).date() == today:
                return True
        return False

    def all_pending(self) -> list[Task]:
        return [task for task in self._tasks if task.status in ACTIVE_STATUSES]

    def all_failed(self) -> list[Task]:
        return [task for task in self._tasks if task.status == STATUS_FAILED]

    def cancel_task(self, task_id: str):
        """取消任务。

        Returns:
            True: 已取消；"final": 任务已处于终态（幂等成功）；
            "processing": 任务正在发送，不能取消；False: 任务不存在。
        """
        for task in self._tasks:
            if task.id != task_id:
                continue
            if task.status in ACTIVE_STATUSES:
                task.status = STATUS_CANCELLED
                task.terminal_at = self._now_utc()
                task.next_retry_at = None
                task.last_error = "用户取消"
                self.save()
                self._wake()
                logger.info(f"{tag()} 🚫 任务已取消: id={task.id[:8]} session={task.session}")
                return True
            if task.status in TERMINAL_STATUSES:
                # 已 sent/failed/expired/cancelled：重复取消是幂等成功
                return "final"
            return "processing"  # processing 中不可取消（可能正在发送）
        return False

    def list_pending_detailed(
        self,
        display_tz: datetime.tzinfo | None = None,
    ) -> list[dict]:
        pending = self.all_pending()
        pending.sort(key=lambda task: task.due_at())
        display_tz = display_tz or datetime.datetime.now().astimezone().tzinfo or UTC
        return [
            {
                "id": task.id,
                "kind": task.kind,
                "session": task.session,
                "fire_at_iso": task.fire_at.astimezone(display_tz).isoformat(),
                "next_retry_at_iso": (
                    task.next_retry_at.astimezone(display_tz).isoformat()
                    if task.next_retry_at
                    else ""
                ),
                "status": task.status,
                "attempts": task.attempts,
                "hint": task.hint,
                "target_user_id": task.target_user_id,
                # 与 fire_at 同口径：转换到展示时区，避免响应内基准不一致
                "created_at_iso": task.created_at.astimezone(display_tz).isoformat(),
            }
            for task in pending
        ]

    def list_terminal_detailed(
        self,
        display_tz: datetime.tzinfo | None = None,
    ) -> list[dict]:
        """保留期内的终态任务（sent/failed/cancelled/expired），供任务页查看历史。

        与 ``_cleanup_terminal_tasks`` 同口径：以 terminal_at（缺省回退
        created_at/fire_at）为基准、``runtime.state_retention_days`` 为窗口，
        仅返回仍在保留期内的终态任务（按结束时间倒序）。
        """
        now = self._now_utc()
        cutoff = now - datetime.timedelta(days=self._state_retention_days)
        finished = []
        for task in self._tasks:
            if task.status not in TERMINAL_STATUSES:
                continue
            base = task.terminal_at or task.created_at or task.fire_at
            if base < cutoff:
                continue
            finished.append(task)
        finished.sort(
            key=lambda task: task.terminal_at or task.created_at or task.fire_at,
            reverse=True,
        )
        display_tz = display_tz or datetime.datetime.now().astimezone().tzinfo or UTC
        return [
            {
                "id": task.id,
                "kind": task.kind,
                "session": task.session,
                "fire_at_iso": task.fire_at.astimezone(display_tz).isoformat(),
                "status": task.status,
                "hint": task.hint,
                "target_user_id": task.target_user_id,
                "terminal_at_iso": (
                    (task.terminal_at or task.created_at or task.fire_at)
                    .astimezone(display_tz)
                    .isoformat()
                ),
                "last_error": str(task.last_error or "") or "",
                "created_at_iso": task.created_at.astimezone(display_tz).isoformat(),
            }
            for task in finished
        ]
