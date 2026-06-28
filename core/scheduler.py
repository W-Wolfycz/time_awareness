"""
主动提醒调度器

负责管理两类延迟触发任务：
- ``calendar``：当日日历事项到点提醒
- ``followup``：LLM 通过 ``schedule_followup`` 工具安排的"X 分钟后再说"

设计要点：
- 单 asyncio 后台循环 + 可中断睡眠（配置改动/新任务入队立即唤醒）
- 任务持久化到 ``reminder_tasks.yaml``（重启不丢）
- 到期任务按 session 分组合并（同 session 的多个任务合并成一组，减少消息条数）
- 调度器本身**不**调用发送/LLM API——通过 ``trigger_callback`` 委托给 main.py，
  保持本模块对 AstrBot context 的解耦

迟到处理：
- 任务 ``fire_at`` 已过但仍在 ``max_late_minutes`` 窗口内 → 仍触发，DueGroup.is_late=True
- 超过窗口 → 标 ``given_up=True`` 丢弃（log warning，用户可见）
"""

import asyncio
import datetime
import os
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from ..log import logger, tag

from ._datafile import atomic_write_yaml, load_mapping


TASKS_FILE_NAME = "reminder_tasks.yaml"
TASKS_DATA_VERSION = 1

# 同 session 合并窗口：同 session 任意两条任务 fire_at 相差在该窗口内则合并
MERGE_WINDOW_SECONDS = 60

# 无任务时的兜底睡眠
IDLE_SLEEP_SECONDS = 300

# 最小睡眠（避免忙等）
MIN_SLEEP_SECONDS = 5


@dataclass
class Task:
    """单条提醒任务。"""

    id: str
    kind: str  # "calendar" | "followup"
    session: str
    fire_at: datetime.datetime
    hint: str = ""
    target_user_id: str = ""  # followup 群聊场景的 at 目标（承诺发起人）
    sent: bool = False
    given_up: bool = False
    created_at: datetime.datetime = field(default_factory=datetime.datetime.now)

    @classmethod
    def from_dict(cls, raw: dict) -> Optional["Task"]:
        try:
            return cls(
                id=str(raw["id"]),
                kind=str(raw["kind"]),
                session=str(raw["session"]),
                fire_at=datetime.datetime.fromisoformat(str(raw["fire_at"])),
                hint=str(raw.get("hint", "")),
                target_user_id=str(raw.get("target_user_id", "")),
                sent=bool(raw.get("sent", False)),
                given_up=bool(raw.get("given_up", False)),
                created_at=datetime.datetime.fromisoformat(
                    str(raw.get("created_at", raw["fire_at"]))
                ),
            )
        except (KeyError, ValueError) as e:
            logger.warning(f"{tag()} ⚠️ 跳过无效任务条目: {e}")
            return None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "session": self.session,
            "fire_at": self.fire_at.isoformat(),
            "hint": self.hint,
            "target_user_id": self.target_user_id,
            "sent": self.sent,
            "given_up": self.given_up,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class DueGroup:
    """同一 session 在同一时间窗口内到期的任务集合。"""

    session: str
    tasks: list  # list[Task]，至少一条
    is_late: bool = False
    late_minutes: int = 0


class ReminderScheduler:
    """调度循环 + 任务队列（持久化）。"""

    def __init__(
        self,
        data_dir: str,
        trigger_callback: Callable[[DueGroup], Awaitable[None]],
        now_provider: Optional[Callable[[], datetime.datetime]] = None,
    ):
        """``now_provider`` 用于注入与 ``fire_at`` 同时区的 now。

        ``fire_at`` 通常由 main.py 的 ``get_now()`` 生成——配置了时区时是
        aware datetime。若 scheduler 内部用裸 ``datetime.now()``（naive），
        两者比较会抛 ``TypeError: can't compare offset-naive and offset-aware``。
        默认回退到 ``datetime.now()`` 兼容旧调用方。
        """
        self.data_dir = data_dir
        self._trigger_callback = trigger_callback
        self._tasks: list[Task] = []
        self._tasks_file = os.path.join(data_dir, TASKS_FILE_NAME)
        self._loop_task: Optional[asyncio.Task] = None
        self._wakeup_event: Optional[asyncio.Event] = None  # 延迟到事件循环里创建
        self._stopped = True
        self._max_late_minutes_value: int = 60
        self._now_provider = now_provider or (lambda: datetime.datetime.now())

    def _now(self) -> datetime.datetime:
        """与 fire_at 同时区的当前时间。"""
        return self._now_provider()

    # ==================== 配置 ====================

    def set_max_late_minutes(self, minutes: int) -> None:
        """配置变更时更新（影响过期判定）。"""
        self._max_late_minutes_value = max(1, int(minutes))

    # ==================== 持久化 ====================

    def load(self) -> None:
        """启动时从 YAML 加载任务列表。"""
        if not os.path.exists(self._tasks_file):
            self._tasks = []
            return
        data = load_mapping(self._tasks_file)
        if data is None:
            self._tasks = []
            return
        raw_tasks = data.get("tasks", []) if isinstance(data, dict) else []
        loaded = []
        for raw in raw_tasks:
            t = Task.from_dict(raw)
            if t is not None:
                loaded.append(t)
        self._tasks = loaded
        logger.info(f"{tag()} ✅ 已加载 {len(loaded)} 条提醒任务")

    def save(self) -> bool:
        """原子性写入任务列表到 YAML（仅持久化未完成项）。"""
        pending = [t.to_dict() for t in self._tasks if not t.sent and not t.given_up]
        payload = {
            "version": TASKS_DATA_VERSION,
            "last_update": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "tasks": pending,
        }
        ok = atomic_write_yaml(
            self._tasks_file,
            payload,
            header="time_awareness 提醒任务（自动生成，可手动编辑）",
        )
        if not ok:
            logger.error(f"{tag()} ❌ 提醒任务保存失败")
        return ok

    # ==================== 入队 ====================

    def add_calendar_task(
        self,
        session: str,
        fire_at: datetime.datetime,
    ) -> Task:
        """入队当日提醒任务（每 session 每天一条；事件内容在 fire 时动态查询）。"""
        task = Task(
            id=uuid.uuid4().hex,
            kind="calendar",
            session=session,
            fire_at=fire_at,
        )
        self._tasks.append(task)
        self.save()
        self._wake()
        logger.debug(
            f"{tag()} 📅 已入队日历提醒: session={session} "
            f"fire_at={fire_at.isoformat()}"
        )
        return task

    def add_followup_task(
        self,
        session: str,
        fire_at: datetime.datetime,
        hint: str,
        target_user_id: str = "",
    ) -> Task:
        task = Task(
            id=uuid.uuid4().hex,
            kind="followup",
            session=session,
            fire_at=fire_at,
            hint=hint,
            target_user_id=target_user_id,
        )
        self._tasks.append(task)
        self.save()
        self._wake()
        logger.debug(
            f"{tag()} ⏰ 已入队 LLM 后续任务: session={session} "
            f"fire_at={fire_at.isoformat()} target_user_id={target_user_id or '-'}"
        )
        return task

    # ==================== 调度循环 ====================

    def start(self) -> None:
        """启动后台调度循环（幂等）。"""
        if self._loop_task is not None and not self._loop_task.done():
            return
        if self._wakeup_event is None:
            self._wakeup_event = asyncio.Event()
        self._stopped = False
        self._wakeup_event.clear()
        self._loop_task = asyncio.create_task(self._loop())
        logger.info(f"{tag()} ✅ 提醒调度循环已启动")

    async def stop(self) -> None:
        """停止调度循环（等待当前 tick 完成）。"""
        self._stopped = True
        self._wake()
        if self._loop_task is not None:
            try:
                await asyncio.wait_for(self._loop_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._loop_task.cancel()
            self._loop_task = None
        logger.info(f"{tag()} ✅ 提醒调度循环已停止")

    def _wake(self) -> None:
        """唤醒调度循环（新任务入队或停止时调用）。"""
        if self._wakeup_event is not None:
            self._wakeup_event.set()

    async def _loop(self) -> None:
        """主循环：算到下次任务的时间 → 可中断睡眠 → 处理到期。"""
        while not self._stopped:
            try:
                now = self._now()
                # 先清理过期任务
                self._cleanup_expired(now)

                # 计算下次触发时间
                pending = [t for t in self._tasks if not t.sent and not t.given_up]
                if not pending:
                    sleep_sec = IDLE_SLEEP_SECONDS
                else:
                    next_fire = min(t.fire_at for t in pending)
                    delta = (next_fire - now).total_seconds()
                    sleep_sec = max(MIN_SLEEP_SECONDS, delta)

                # 可中断睡眠
                try:
                    await asyncio.wait_for(
                        self._wakeup_event.wait(), timeout=sleep_sec
                    )
                    # 被唤醒（新任务入队 / 配置变化 / 停止）
                    self._wakeup_event.clear()
                except asyncio.TimeoutError:
                    pass  # 到时正常醒来

                if self._stopped:
                    break

                # 处理到期任务
                now = self._now()
                groups = self._pop_due(now)
                for group in groups:
                    try:
                        await self._trigger_callback(group)
                    except Exception as e:
                        logger.error(
                            f"{tag()} ❌ 触发任务失败 session={group.session}: {e}"
                        )
            except Exception as e:
                logger.error(f"{tag()} ❌ 调度循环异常: {e}")
                await asyncio.sleep(MIN_SLEEP_SECONDS)

    # ==================== 到期任务处理 ====================

    def _cleanup_expired(self, now: datetime.datetime) -> int:
        """标记超过迟到窗口的任务为 given_up。返回丢弃数量。"""
        max_late = self._max_late_minutes_value
        cutoff = now - datetime.timedelta(minutes=max_late)
        count = 0
        for t in self._tasks:
            if t.sent or t.given_up:
                continue
            if t.fire_at < cutoff:
                t.given_up = True
                count += 1
                logger.warning(
                    f"{tag()} ⚠️ 任务过期丢弃: id={t.id[:8]} kind={t.kind} "
                    f"session={t.session} fire_at={t.fire_at.isoformat()} "
                    f"(超出 {max_late} 分钟容忍窗口)"
                )
        if count > 0:
            self.save()
        return count

    def _pop_due(self, now: datetime.datetime) -> list:
        """取出所有到期未处理的任务，按 session 分组合并。

        - 同 session 的多个任务合并到一个 DueGroup（不分 kind，简化逻辑）
        - is_late = 是否有任何任务超过 fire_at 已 60 秒以上
        - 标记所有取出任务 sent=True，并持久化

        返回 list[DueGroup]
        """
        due = [
            t for t in self._tasks
            if not t.sent and not t.given_up and t.fire_at <= now
        ]
        if not due:
            return []

        by_session: dict[str, list[Task]] = {}
        for t in due:
            by_session.setdefault(t.session, []).append(t)

        groups = []
        for session, tasks in by_session.items():
            tasks.sort(key=lambda t: t.fire_at)
            earliest = tasks[0].fire_at
            is_late = (now - earliest).total_seconds() > MERGE_WINDOW_SECONDS
            late_minutes = (
                int((now - earliest).total_seconds() // 60) if is_late else 0
            )
            groups.append(
                DueGroup(
                    session=session,
                    tasks=tasks,
                    is_late=is_late,
                    late_minutes=late_minutes,
                )
            )
            for t in tasks:
                t.sent = True

        self.save()
        return groups

    # ==================== 日历扫描辅助 ====================

    def has_calendar_task_for_session_today(
        self, session: str, today: datetime.date
    ) -> bool:
        """检查某 session 在今天是否已入队日历提醒（避免重复入队）。"""
        for t in self._tasks:
            if (
                t.kind == "calendar"
                and t.session == session
                and t.fire_at.date() == today
                and not t.given_up
            ):
                return True
        return False

    def all_pending(self) -> list:
        """调试用：列出所有未处理任务（已 sent/given_up 的不算）。"""
        return [t for t in self._tasks if not t.sent and not t.given_up]

    def cancel_task(self, task_id: str) -> bool:
        """按 id 取消未触发任务（标记 given_up=True 并持久化，唤醒调度循环）。

        唤醒的目的是让调度循环重新计算下次睡眠——否则它会为这条已取消
        的任务守候到原 fire_at 才醒来。

        Returns:
            True=已取消；False=未找到/已 sent/已 given_up。
        """
        for t in self._tasks:
            if t.id == task_id and not t.sent and not t.given_up:
                t.given_up = True
                self.save()
                self._wake()
                logger.info(
                    f"{tag()} 🚫 任务已取消: id={t.id[:8]} kind={t.kind} "
                    f"session={t.session} fire_at={t.fire_at.isoformat()}"
                )
                return True
        return False

    def list_pending_detailed(self) -> list:
        """返回 pending 任务的序列化 dict 列表（按 fire_at 升序）。

        供 webui 任务页消费，每条字段：
        - id: 任务 uuid hex
        - kind: "calendar" | "followup"
        - session: UMO
        - fire_at_iso: 触发时间 iso 字符串（aware datetime，前端原样展示）
        - hint: followup 的承诺内容（calendar 为空）
        - target_user_id: followup 群聊的 at 目标（calendar 为空）
        - created_at_iso: 入队时间 iso 字符串
        """
        pending = self.all_pending()
        pending.sort(key=lambda t: t.fire_at)
        return [
            {
                "id": t.id,
                "kind": t.kind,
                "session": t.session,
                "fire_at_iso": t.fire_at.isoformat(),
                "hint": t.hint,
                "target_user_id": t.target_user_id,
                "created_at_iso": t.created_at.isoformat(),
            }
            for t in pending
        ]
