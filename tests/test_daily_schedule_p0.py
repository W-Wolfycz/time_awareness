"""P0-3：AI 每日日程失败熔断（日级）与 Step2 重试退避（0/5/30s）。

覆盖：
- 本日持久化终态 failure 后，自动 queue 被抑制，普通消息不再重新排队（调用风暴防护）；
- 不同日期 / 不同身份不受熔断影响，仍可生成；
- 成功快照不产生 failure、不会被误熔断；手动 regenerate / force 可绕过自动熔断并清除 failure；
- Step2 主规划重试按 0/5/30s 退避执行（注入 recording sleep，不真实等待）。
"""

import asyncio
import datetime
import json
from types import SimpleNamespace

from time_awareness.core.daily_schedule_store import DailyScheduleSnapshotStore
from time_awareness.services.daily_schedule_generation import _retry_delay_seconds
from time_awareness.services.daily_schedule_service import DailyScheduleService


NOW = datetime.datetime(2026, 8, 11, 7, 30, tzinfo=datetime.timezone.utc)
TOMORROW = NOW + datetime.timedelta(days=1)
SESSION_A = "platform_demo:PrivateMessage:10001"
SESSION_B = "platform_demo:GroupMessage:group_demo"

SLOTS_JSON = json.dumps(
    [
        {"name": "晨起", "start": "06:00", "end": "09:00", "state": "刚醒，慵懒"},
        {"name": "入夜", "start": "20:00", "end": "24:00", "state": "放松，倦意渐浓"},
    ],
    ensure_ascii=False,
)


class FakeTimeContext:
    def __init__(self, now=NOW):
        self.current = now

    def now(self):
        return self.current

    def build_generation_sensor_snapshot(self, *, now=None):
        now = now or self.current
        return {
            "workday": {
                "enabled": True,
                "available": True,
                "as_of": now.isoformat(),
                "value": "工作日",
            }
        }


class FakeContext:
    """按序消费 plan_responses；耗尽后返回非法 JSON（模拟持续失败）。

    可选 ``log`` 共享事件列表：记录每次 LLM 调用，供退避顺序断言。
    """

    def __init__(self, plan_responses=None, *, log=None):
        self.plan_responses = list(plan_responses or [])
        self.plan_calls = []
        self.log = log
        self.provider_umos = []
        self.provider = SimpleNamespace(
            provider_config={"max_context_tokens": 0},
            get_model=lambda: "model_demo",
        )

    def get_provider_by_id(self, provider_id):
        return self.provider if provider_id == "provider_demo" else None

    async def get_current_chat_provider_id(self, umo):
        self.provider_umos.append(umo)
        return "provider_demo"

    def get_config(self):
        return {"provider_settings": {"default_provider_id": "provider_demo"}}

    async def llm_generate(self, **kwargs):
        if self.log is not None:
            self.log.append(("llm", None))
        self.plan_calls.append(kwargs)
        if self.plan_responses:
            text = self.plan_responses.pop(0)
        else:
            text = "not-json"
        return SimpleNamespace(role="assistant", completion_text=text)


class FakePersonaResolver:
    def __init__(self, resolved="persona_demo"):
        self.resolved = resolved

    async def __call__(self, context, session, event=None):
        return self.resolved, {"prompt": "角色设定"}


class RecordingSleep:
    def __init__(self, log):
        self._log = log

    async def __call__(self, delay):
        self._log.append(("sleep", float(delay)))


async def _no_sleep(_delay):
    return None


def _config():
    return {
        "daily_schedule": {
            "enable_schedule": True,
            "ai_daily": {
                "enabled": True,
                "generation_time": "00:05",
                "max_slots": 24,
                "retention_days": 30,
            },
            "schedule_templates": [],
            "ai_generation": {
                "provider_id": "",
                "use_persona": True,
                "worldview": "世界观",
            },
        }
    }


def _service(tmp_path, context, *, resolver=None, sleep=None, time_context=None):
    store = DailyScheduleSnapshotStore(str(tmp_path))
    assert store.load()
    tc = time_context or FakeTimeContext()
    service = DailyScheduleService(
        context=context,
        config=_config(),
        store=store,
        time_context=tc,
        persona_resolver=resolver or FakePersonaResolver(),
        sleep=sleep,
    )
    return service, store, tc


# ==================== 日级熔断 ====================


def test_terminal_failure_suppresses_auto_queue(tmp_path):
    """本日出现持久化终态 failure 后，自动 queue 被抑制（调用风暴防护）。"""
    context = FakeContext()  # 持续非法输出 → 终态失败
    service, store, _ = _service(tmp_path, context, sleep=_no_sleep)

    async def _run():
        await service.register_session_async(SESSION_A, trigger=False)
        result = await service.generate_for_session(SESSION_A, now=NOW)
        assert not result.success
        persona_hash = store.persona_hash("persona_demo")
        assert store.get_failure(persona_hash, NOW.date(), "UTC") is not None

        calls_after_failure = len(context.plan_calls)
        # 再次自动 queue：被日级熔断抑制
        assert service.queue_generation(SESSION_A) is False
        assert not service._tasks
        # 模拟“新一条普通消息”的注册路径：同样不会 spawn 新生成任务
        await service.register_session_async(SESSION_A, trigger=True)
        assert not service._tasks
        assert len(context.plan_calls) == calls_after_failure
        return True

    assert asyncio.run(_run())


def test_queue_generation_force_bypasses_breaker(tmp_path):
    """queue_generation(force=True) 绕过自动熔断，成功后 failure 清除。"""
    context = FakeContext(["not-json"] * 3 + [SLOTS_JSON])
    service, store, _ = _service(tmp_path, context, sleep=_no_sleep)

    async def _run():
        await service.register_session_async(SESSION_A, trigger=False)
        result = await service.generate_for_session(SESSION_A, now=NOW)
        assert not result.success
        persona_hash = store.persona_hash("persona_demo")
        assert store.get_failure(persona_hash, NOW.date(), "UTC") is not None
        assert service.queue_generation(SESSION_A) is False  # 自动被抑制

        assert service.queue_generation(SESSION_A, force=True) is True
        await asyncio.gather(*list(service._tasks.values()))
        snapshot = store.get(persona_hash, NOW.date(), "UTC")
        assert snapshot is not None and snapshot["status"] == "ready"
        # 成功后 failure 被清除，熔断解除
        assert store.get_failure(persona_hash, NOW.date(), "UTC") is None
        return True

    assert asyncio.run(_run())


def test_manual_regenerate_command_bypasses_daily_breaker(tmp_path):
    """手动 regenerate 入口（generate_for_session(force=True)）可绕过自动熔断。"""
    context = FakeContext(["not-json"] * 3 + [SLOTS_JSON])
    service, store, _ = _service(tmp_path, context, sleep=_no_sleep)

    async def _run():
        await service.register_session_async(SESSION_A, trigger=False)
        assert not (await service.generate_for_session(SESSION_A, now=NOW)).success
        persona_hash = store.persona_hash("persona_demo")
        assert store.get_failure(persona_hash, NOW.date(), "UTC") is not None

        result = await service.generate_for_session(SESSION_A, now=NOW, force=True)
        assert result.success
        assert store.get_failure(persona_hash, NOW.date(), "UTC") is None
        return True

    assert asyncio.run(_run())


def test_different_date_bypasses_daily_breaker(tmp_path):
    """熔断按 (Persona, 日期, 时区) 作用：新的一天不受昨日 failure 影响。"""
    context = FakeContext(["not-json"] * 3 + [SLOTS_JSON])
    service, store, tc = _service(tmp_path, context, sleep=_no_sleep)

    async def _run():
        await service.register_session_async(SESSION_A, trigger=False)
        assert not (await service.generate_for_session(SESSION_A, now=NOW)).success
        persona_hash = store.persona_hash("persona_demo")
        assert store.get_failure(persona_hash, NOW.date(), "UTC") is not None
        assert service.queue_generation(SESSION_A) is False  # 今日被抑制

        # 次日：可正常自动生成
        tc.current = TOMORROW
        assert service.queue_generation(SESSION_A) is True
        await asyncio.gather(*list(service._tasks.values()))
        snapshot = store.get(persona_hash, TOMORROW.date(), "UTC")
        assert snapshot is not None and snapshot["status"] == "ready"
        return True

    assert asyncio.run(_run())


def test_different_identity_bypasses_daily_breaker(tmp_path):
    """熔断按 Persona 隔离：一个 Persona 失败不影响另一个 Persona。"""
    context = FakeContext(["not-json"] * 3 + [SLOTS_JSON])
    resolver = FakePersonaResolver("persona_a")
    service, store, _ = _service(tmp_path, context, resolver=resolver, sleep=_no_sleep)

    async def _run():
        await service.register_session_async(SESSION_A, trigger=False)
        assert not (await service.generate_for_session(SESSION_A, now=NOW)).success
        persona_a = store.persona_hash("persona_a")
        assert store.get_failure(persona_a, NOW.date(), "UTC") is not None

        # 切换到 persona_b：不受 persona_a 的 failure 影响
        resolver.resolved = "persona_b"
        await service.register_session_async(SESSION_B, trigger=False)
        assert service.queue_generation(SESSION_B) is True
        await asyncio.gather(*list(service._tasks.values()))
        persona_b = store.persona_hash("persona_b")
        assert store.get(persona_b, NOW.date(), "UTC") is not None
        assert store.get_failure(persona_b, NOW.date(), "UTC") is None
        return True

    assert asyncio.run(_run())


def test_success_does_not_engage_breaker(tmp_path):
    """成功快照不产生 failure：不被误熔断（自动 queue 仅因快照可用而复用）。"""
    context = FakeContext([SLOTS_JSON])
    service, store, _ = _service(tmp_path, context, sleep=_no_sleep)

    async def _run():
        await service.register_session_async(SESSION_A, trigger=False)
        result = await service.generate_for_session(SESSION_A, now=NOW)
        assert result.success
        persona_hash = store.persona_hash("persona_demo")
        assert store.get_failure(persona_hash, NOW.date(), "UTC") is None
        # 快照可用 → 自动 queue 复用，不 spawn 新任务（也未被熔断）
        assert service.queue_generation(SESSION_A) is False
        assert not service._tasks
        return True

    assert asyncio.run(_run())


# ==================== Step2 重试退避 ====================


def test_retry_backoff_order_and_delays(tmp_path):
    """初始规划连续失败时：按 0/5/30s 退避执行 3 次，sleep 与调用交错。"""
    log = []
    context = FakeContext(log=log)  # 全部非法输出 → 3 次尝试均失败
    recording_sleep = RecordingSleep(log)
    service, _, _ = _service(tmp_path, context, sleep=recording_sleep)

    async def _run():
        await service.register_session_async(SESSION_A, trigger=False)
        return await service.generate_for_session(SESSION_A, now=NOW)

    result = asyncio.run(_run())
    assert not result.success
    assert result.error_type == "invalid_output"
    assert result.retryable is True
    assert result.calls_executed == 3
    # 顺序：第 1 次调用不等待 → sleep(5) → 第 2 次 → sleep(30) → 第 3 次
    assert [kind for kind, _ in log] == [
        "llm", "sleep", "llm", "sleep", "llm",
    ]
    assert [delay for kind, delay in log if kind == "sleep"] == [5.0, 30.0]


def test_retry_delay_helper():
    """退避辅助函数：0/5/30s 序列，超出封顶 30s。"""
    assert _retry_delay_seconds(0) == 0.0
    assert _retry_delay_seconds(1) == 5.0
    assert _retry_delay_seconds(2) == 30.0
    assert _retry_delay_seconds(3) == 30.0
    assert _retry_delay_seconds(10) == 30.0
