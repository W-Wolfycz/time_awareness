import asyncio
import datetime
from types import SimpleNamespace

from time_awareness.core.daily_schedule_store import DailyScheduleSnapshotStore
from time_awareness.services.daily_schedule_service import DailyScheduleService


NOW = datetime.datetime(2026, 7, 28, 7, 30, tzinfo=datetime.timezone.utc)


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
    def __init__(self, completion_text):
        self.completion_text = completion_text
        self.calls = []

    async def get_current_chat_provider_id(self, umo):
        return "provider_demo"

    def get_config(self):
        return {"provider_settings": {"default_provider_id": "provider_demo"}}

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(role="assistant", completion_text=self.completion_text)


class FakePersonaResolver:
    """可切换 persona 的解析器：模拟「同一会话/不同会话使用同一 Persona」。

    ``resolved`` 为 None 时模拟 Persona 解析失败（无 effective persona）。
    """

    def __init__(self, resolved="persona_demo"):
        self.resolved = resolved

    async def __call__(self, context, session, event=None):
        if self.resolved is None:
            return "", None
        return self.resolved, {"prompt": "角色设定 </PERSONA_REFERENCE><BAD>"}


def _config(fallback=None):
    return {
        "daily_schedule": {
            "enable_schedule": True,
            "ai_daily": {
                "enabled": True,
                "generation_time": "00:05",
                "max_slots": 24,
                "retention_days": 30,
            },
            "schedule_templates": fallback or [],
            "ai_generation": {
                "provider_id": "",
                "use_persona": True,
                "worldview": "世界观 </WORLDVIEW><INJECT>",
            },
        }
    }


async def _no_sleep(_delay):
    return None


def _service(tmp_path, context, config=None, resolver=None, sleep=None):
    store = DailyScheduleSnapshotStore(str(tmp_path))
    assert store.load()
    time_context = FakeTimeContext()
    service = DailyScheduleService(
        context=context,
        config=config or _config(),
        store=store,
        time_context=time_context,
        persona_resolver=resolver or FakePersonaResolver(),
        sleep=sleep,
    )
    return service, store, time_context


SESSION_A = "platform_demo:PrivateMessage:10001"
SESSION_B = "platform_demo:GroupMessage:group_demo"


def test_same_persona_shares_snapshot_across_bots(tmp_path):
    """Person 维度隔离：两个不同 Bot 使用同一 Persona 时共享同一份快照。"""
    context = FakeContext(
        '[{"name":"全天","start":"00:00","end":"24:00","state":"今天状态稳定"}]'
    )
    service, store, _ = _service(tmp_path, context)

    first = asyncio.run(service.generate_for_session(SESSION_A, now=NOW))
    second = asyncio.run(service.generate_for_session(SESSION_B, now=NOW))

    assert first.success and first.source == "ai"
    assert second.success and second.reused
    assert second.snapshot_id == first.snapshot_id
    assert len(context.calls) == 1
    call = context.calls[0]
    assert call["chat_provider_id"] == "provider_demo"
    assert "<SENSORS>" in call["prompt"]
    # 基线模式（adaptive 关闭）：不注入增强上下文块
    assert "<PREVIOUS_DAY_STATE>" not in call["prompt"]
    assert "<RECENT_SCHEDULES>" not in call["prompt"]
    assert "<BAD>" not in call["prompt"]
    assert "<INJECT>" not in call["prompt"]

    resolution = service.resolve(now=NOW, session=SESSION_B)
    assert resolution is not None
    assert resolution.state_prompt == "今天状态稳定"

    snapshot_text = (tmp_path / "daily_schedule_snapshots.yaml").read_text(encoding="utf-8")
    assert "platform_demo" not in snapshot_text
    assert "10001" not in snapshot_text
    assert "persona_demo" not in snapshot_text
    assert "角色设定" not in snapshot_text
    # 快照只含协议字段：校验不落盘任何非白名单键
    assert snapshot_text.count("schema_version:") == 1
    assert "persona_prompt" not in snapshot_text
    assert "session" not in snapshot_text
    assert "history" not in snapshot_text


def test_different_personas_get_independent_snapshots(tmp_path):
    """同一 Bot 的不同 Persona 各自独立生成，互不复用。"""
    context = FakeContext(
        '[{"name":"全天","start":"00:00","end":"24:00","state":"状态 A"}]'
    )
    resolver = FakePersonaResolver("persona_a")
    service, _, _ = _service(tmp_path, context, resolver=resolver)

    first = asyncio.run(service.generate_for_session(SESSION_A, now=NOW))
    assert first.success

    context.completion_text = (
        '[{"name":"全天","start":"00:00","end":"24:00","state":"状态 B"}]'
    )
    resolver.resolved = "persona_b"
    second = asyncio.run(service.generate_for_session(SESSION_A, now=NOW))

    assert second.success and not second.reused
    assert second.snapshot_id != first.snapshot_id
    assert len(context.calls) == 2
    # 切换后立即读到新 Persona 的快照
    assert service.resolve(now=NOW, session=SESSION_A).state_prompt == "状态 B"
    # 同一个 Persona 再次解析仍命中缓存快照
    assert len(context.calls) == 2


def test_persona_switch_back_reads_own_snapshot(tmp_path):
    """Persona 切回 A 后读回 A 的快照，不误用 B。"""
    context = FakeContext(
        '[{"name":"全天","start":"00:00","end":"24:00","state":"A 的状态"}]'
    )
    resolver = FakePersonaResolver("persona_a")
    service, _, _ = _service(tmp_path, context, resolver=resolver)

    asyncio.run(service.generate_for_session(SESSION_A, now=NOW))
    resolver.resolved = "persona_b"
    context.completion_text = (
        '[{"name":"全天","start":"00:00","end":"24:00","state":"B 的状态"}]'
    )
    asyncio.run(service.generate_for_session(SESSION_A, now=NOW))

    resolver.resolved = "persona_a"
    # 真实入口（on_llm_request / /schedule show）在读取前都会先异步注册刷新身份
    assert asyncio.run(service.register_session_async(SESSION_A)) != ""
    snapshot = service.get_snapshot_for_session(SESSION_A, now=NOW)
    assert snapshot["ai_slots"][0]["state"] == "A 的状态"


def test_persona_unavailable_is_diagnosed_and_creates_no_snapshot(tmp_path):
    """Persona 解析失败：记为可诊断失败，不创建无 Persona 快照，不复用他人快照。"""
    context = FakeContext(
        '[{"name":"全天","start":"00:00","end":"24:00","state":"今天状态稳定"}]'
    )
    resolver = FakePersonaResolver(None)
    service, store, _ = _service(tmp_path, context, resolver=resolver)

    result = asyncio.run(service.generate_for_session(SESSION_A, now=NOW))

    assert not result.success
    assert result.error_type == "persona_unavailable"
    assert not result.retryable
    # 不创建快照文件（没有任何写入），也不留代表会话
    assert not (tmp_path / "daily_schedule_snapshots.yaml").exists()
    assert service._representative_sessions == {}


def test_generation_writes_ai_layer_and_does_not_mutate_static_config(tmp_path):
    fallback = [
        {
            "name": "睡眠",
            "start_time": "22:00",
            "end_time": "08:00",
            "state_prompt": "仍在休息",
        }
    ]
    config = _config(fallback=fallback)
    context = FakeContext(
        '[{"name":"日间","start":"08:00","end":"22:00","state":"保持清醒"}]'
    )
    service, _, _ = _service(tmp_path, context, config=config)

    result = asyncio.run(service.generate_for_session(SESSION_A, now=NOW))

    assert result.success and result.source == "ai"  # 生成层不混入静态，合并移到渲染
    assert config["daily_schedule"]["schedule_templates"] == fallback
    snapshot = service.get_snapshot_for_session(SESSION_A, now=NOW)
    assert [(slot["start"], slot["end"]) for slot in snapshot["ai_slots"]] == [
        ("08:00", "22:00"),
    ]
    # 渲染层先动态推进冻结边界到当前时刻（只读），再合并静态兜底：
    # 跨午夜静态段 22:00-08:00 在 07:30（当前时刻）合并为一段「正在执行」
    # （角色确实还在睡眠中），其余时段按来源层展示（2.0.0 展示语义）。
    rendered = service._render_snapshot(snapshot, NOW)
    assert [(slot["start"], slot["end"]) for slot in rendered] == [
        ("00:00", "08:00"),   # 静态跨午夜段（当前时刻合并，正在执行）
        ("08:00", "22:00"),   # AI
        ("22:00", "24:00"),   # 静态跨午夜段（未来部分）
    ]


def test_sparse_generation_succeeds_without_full_static_schedule(tmp_path):
    context = FakeContext(
        '[{"name":"上午","start":"08:00","end":"12:00","state":"精神清醒"}]'
    )
    service, _, _ = _service(tmp_path, context)

    result = asyncio.run(service.generate_for_session(SESSION_A, now=NOW))

    assert result.success and result.source == "ai"
    snapshot = service.get_snapshot_for_session(SESSION_A, now=NOW)
    assert [(slot["start"], slot["end"]) for slot in snapshot["ai_slots"]] == [
        ("08:00", "12:00")
    ]
    assert service.resolve(now=NOW.replace(hour=9), session=SESSION_A) is not None
    assert service.resolve(now=NOW.replace(hour=18), session=SESSION_A) is None


def test_failed_force_regeneration_preserves_old_snapshot(tmp_path):
    context = FakeContext(
        '[{"name":"全天","start":"00:00","end":"24:00","state":"旧状态"}]'
    )
    service, _, _ = _service(tmp_path, context, sleep=_no_sleep)
    first = asyncio.run(service.generate_for_session(SESSION_A, now=NOW))
    assert first.success

    context.completion_text = "not-json"
    failed = asyncio.run(service.generate_for_session(SESSION_A, force=True, now=NOW))

    assert not failed.success
    snapshot = service.get_snapshot_for_session(SESSION_A, now=NOW)
    assert snapshot["snapshot_id"] == first.snapshot_id
    assert service.resolve(now=NOW, session=SESSION_A).state_prompt == "旧状态"


def test_disabled_ai_daily_does_not_create_persona_secret(tmp_path):
    config = _config()
    config["daily_schedule"]["ai_daily"]["enabled"] = False
    context = FakeContext("")
    store = DailyScheduleSnapshotStore(str(tmp_path))
    service = DailyScheduleService(
        context=context,
        config=config,
        store=store,
        time_context=FakeTimeContext(),
        persona_resolver=FakePersonaResolver(),
    )

    assert not service.configured_enabled()
    assert service.register_session(SESSION_A) == ""
    assert list(tmp_path.iterdir()) == []


def test_runtime_enable_initializes_store_on_demand(tmp_path):
    config = _config()
    config["daily_schedule"]["enable_schedule"] = False
    store = DailyScheduleSnapshotStore(str(tmp_path))
    service = DailyScheduleService(
        context=FakeContext(""),
        config=config,
        store=store,
        time_context=FakeTimeContext(),
        persona_resolver=FakePersonaResolver(),
    )
    service.set_available(False)
    assert list(tmp_path.iterdir()) == []

    config["daily_schedule"]["enable_schedule"] = True
    assert service.ensure_available()
    assert (tmp_path / "daily_schedule_persona_secret.yaml").exists()

def test_register_session_async_caches_persona_identity(tmp_path):
    context = FakeContext(
        '[{"name":"全天","start":"00:00","end":"24:00","state":"今天状态稳定"}]'
    )
    service, _, _ = _service(tmp_path, context)

    async def _run():
        persona_hash = await service.register_session_async(SESSION_A)
        assert persona_hash.startswith("persona_")
        # 同步注册现在命中缓存
        assert service.register_session(SESSION_A, trigger=False) == persona_hash
        # 未知会话同步注册为空（尚未解析）
        assert service.register_session(SESSION_B, trigger=False) == ""

        await service.register_session_async(SESSION_B)
        # 同一事件循环内等待后台生成任务完成（跨 loop 的任务无法执行）
        pending = list(service._tasks.values())
        if pending:
            await asyncio.gather(*pending)
        assert service.get_snapshot_for_session(SESSION_A, now=NOW) is not None

    asyncio.run(_run())


def test_daily_scan_reparses_personas_and_dedupes(tmp_path):
    """2.0.0 计划 §3.2：每日扫描逐一重新解析 Persona，按身份去重排队。"""
    context = FakeContext(
        '[{"name":"全天","start":"00:00","end":"24:00","state":"今天状态稳定"}]'
    )
    resolver = FakePersonaResolver("persona_demo")
    service, _, _ = _service(tmp_path, context, resolver=resolver, sleep=_no_sleep)

    async def _run():
        # 两个会话都注册为同一 Persona
        await service.register_session_async(SESSION_A, trigger=False)
        await service.register_session_async(SESSION_B, trigger=False)
        persona_a = service._identity_for(SESSION_A)
        assert persona_a == service._identity_for(SESSION_B)

        queued = await service.queue_known_profiles()
        assert queued == 1  # 同一 Persona 只排队一次
        assert len(service._tasks) == 1

        # Persona 切换后扫描会重新解析出新身份并排队新任务
        resolver.resolved = "persona_b"
        queued2 = await service.queue_known_profiles()
        assert service._identity_for(SESSION_A) != persona_a
        assert queued2 == 1
        # 新身份（persona_b）的生成任务已排队（旧任务可能已完成后被清理）
        persona_b_hash = service._identity_for(SESSION_A)
        assert any(
            key.startswith(f"{persona_b_hash}:")
            for key in service._tasks
        )

    asyncio.run(_run())


def test_umo_keepalive_expires_stale_sessions(tmp_path):
    """2.0.0 计划 §3.3：一整天未再出现的 UMO 从进程内集合删除，快照不删。"""
    context = FakeContext(
        '[{"name":"全天","start":"00:00","end":"24:00","state":"今天状态稳定"}]'
    )
    service, _, _ = _service(tmp_path, context)

    async def _run():
        # trigger=True：注册的同时产生快照（快照是删除 UMO 也不应动到的数据）
        await service.register_session_async(SESSION_A)
        assert SESSION_A in service._session_active_at
        persona_hash = service._identity_for(SESSION_A)
        pending = list(service._tasks.values())
        if pending:
            await asyncio.gather(*pending)

        # 活跃时间推进到 25 小时前 → 过期
        stale_now = NOW + datetime.timedelta(hours=25)
        service._expire_stale_sessions(stale_now)
        assert service._identity_for(SESSION_A) == ""
        assert SESSION_A not in service._representative_sessions.values()
        assert SESSION_A not in service._session_active_at
        # 快照数据不受影响
        assert service.store.get(persona_hash, NOW.date(), "UTC") is not None

        # 重新出现后恢复注册
        await service.register_session_async(SESSION_A, trigger=False)
        assert service._identity_for(SESSION_A) == persona_hash

    asyncio.run(_run())


def test_daily_scan_does_not_touch_keepalive(tmp_path):
    """每日扫描只是轮询检查点：不刷新保活时间戳（否则集合永不过期）。"""
    context = FakeContext(
        '[{"name":"全天","start":"00:00","end":"24:00","state":"今天状态稳定"}]'
    )
    service, _, _ = _service(tmp_path, context, sleep=_no_sleep)

    async def _run():
        await service.register_session_async(SESSION_A, trigger=False)
        last_active = service._session_active_at[SESSION_A]
        assert last_active == NOW

        # 扫描发生在 12 小时后：活跃时间必须保持注册时的值，不被刷新
        service.time_context.current = NOW + datetime.timedelta(hours=12)
        await service.queue_known_profiles()
        assert service._session_active_at[SESSION_A] == NOW

        # 再过 12 小时（注册已满 24h 且期间无真实出现）→ 过期删除
        service.time_context.current = NOW + datetime.timedelta(hours=24, minutes=1)
        await service.queue_known_profiles()
        assert SESSION_A not in service._session_active_at
        assert service._identity_for(SESSION_A) == ""

    asyncio.run(_run())


# ==================== 次日读取：推进固化 / 无 AI 静态回退 ====================


def test_previous_day_state_without_ai_snapshot_uses_static_schedule(tmp_path):
    """昨日没有 AI 快照时，边界评估仍能取得实际静态日程时间线（不返回空）。"""
    fallback = [{
        "name": "静态午休",
        "start_time": "12:00",
        "end_time": "14:00",
        "state_prompt": "午休",
    }]
    context = FakeContext(
        '[{"name":"全天","start":"00:00","end":"24:00","state":"今天状态稳定"}]'
    )
    service, store, _ = _service(tmp_path, context, config=_config(fallback=fallback))
    persona_hash = store.persona_hash("persona_demo")

    state = service._previous_day_state(persona_hash, NOW)
    assert state is not None
    assert any(s["name"] == "静态午休" for s in state["actual_schedules"][-1]["slots"])
    # 无快照时不创建快照（只读，不落盘）
    assert store.get(persona_hash, NOW.date() - datetime.timedelta(days=1), "UTC") is None


def test_previous_day_state_advances_unfinished_day_to_midnight(tmp_path):
    """昨日快照日末尚未推进时，读取前用旧 static_slots 补齐至 24:00 并固化。"""
    fallback = [{
        "name": "静态午休",
        "start_time": "12:00",
        "end_time": "13:00",
        "state_prompt": "午休",
    }]
    context = FakeContext(
        '[{"name":"全天","start":"00:00","end":"24:00","state":"今天状态稳定"}]'
    )
    service, store, _ = _service(tmp_path, context, config=_config(fallback=fallback))
    persona_hash = store.persona_hash("persona_demo")
    yesterday = NOW.date() - datetime.timedelta(days=1)
    store.save_ready(
        {
            "snapshot_id": "yesterday",
            "persona_hash": persona_hash,
            "local_date": yesterday.isoformat(),
            "timezone": "UTC",
            "status": "ready",
            "executed_until": "12:00",
            "executed_slots": [
                {"name": "上午", "start": "09:00", "end": "10:00", "state": "工作",
                 "origin": "executed", "_source_origin": "ai"},
            ],
            "user_slots": [],
            "ai_slots": [
                {"name": "下午", "start": "14:00", "end": "16:00", "state": "会议", "origin": "ai"},
            ],
            "static_slots": [
                {"name": "静态午休", "start": "12:00", "end": "13:00", "state": "午休", "origin": "static"},
            ],
            "slots": [],
        },
        retention_days=30,
        today=NOW.date(),
    )

    state = service._previous_day_state(persona_hash, NOW)
    assert state is not None
    persisted = store.get(persona_hash, yesterday, "UTC")
    # 推进并固化到日末
    assert persisted["executed_until"] == "24:00"
    frozen_names = {s["name"] for s in persisted["executed_slots"]}
    assert "上午" in frozen_names and "下午" in frozen_names and "静态午休" in frozen_names
    # 边界评估输入包含昨日实际日程
    assert any(s["name"] == "下午" for s in state["actual_schedules"][-1]["slots"])


BOUNDARY_ANCHOR = {
    "energy": 44, "sleep_debt": 12, "focus": 60,
    "social_energy": 50, "motivation": 55,
    "physical_state": "状态良好", "daily_theme": "常规日",
    "unfinished_plans": [],
}


def _ready_snapshot(persona_hash, day, *, name, boundary):
    return {
        "snapshot_id": f"snap-{day.isoformat()}",
        "persona_hash": persona_hash,
        "local_date": day.isoformat(),
        "timezone": "UTC",
        "status": "ready",
        "executed_until": "24:00",
        "executed_slots": [],
        "user_slots": [],
        "ai_slots": [
            {"name": name, "start": "00:00", "end": "24:00", "state": "当日",
             "origin": "ai"},
        ],
        "static_slots": [],
        "boundary_state": boundary,
        "slots": [],
    }


def test_previous_day_state_walks_back_to_last_usable_boundary(tmp_path):
    """断链回溯：向前找到最近一个非空角色状态，并携带自该边界日起的实际时间线。"""
    context = FakeContext('[]')
    service, store, _ = _service(tmp_path, context)
    persona_hash = store.persona_hash("persona_demo")
    base = NOW.date()

    # T-3：可用边界锚点
    store.save_ready(
        _ready_snapshot(persona_hash, base - datetime.timedelta(days=3),
                        name="锚点日", boundary=BOUNDARY_ANCHOR),
        retention_days=30, today=NOW.date(),
    )
    # T-2 / T-1：AI 生成成功但边界失败（空边界）——实际日程仍应被引用
    for offset, name in ((2, "前日"), (1, "昨日")):
        store.save_ready(
            _ready_snapshot(persona_hash, base - datetime.timedelta(days=offset),
                            name=name, boundary={}),
            retention_days=30, today=NOW.date(),
        )

    state = service._previous_day_state(persona_hash, NOW, window_days=7)
    assert state["boundary_state"] == BOUNDARY_ANCHOR
    assert state["boundary_date"] == (
        base - datetime.timedelta(days=3)
    ).isoformat()
    dates = [entry["date"] for entry in state["actual_schedules"]]
    assert dates == [
        (base - datetime.timedelta(days=offset)).isoformat()
        for offset in (3, 2, 1)
    ]
    assert state["actual_schedules"][-1]["slots"][0]["name"] == "昨日"


def test_previous_day_state_empty_boundary_carries_full_window(tmp_path):
    """断链超过窗口：空边界，但仍携带窗口内每天的实际时间线。"""
    context = FakeContext('[]')
    service, store, _ = _service(tmp_path, context)
    persona_hash = store.persona_hash("persona_demo")
    base = NOW.date()

    # 窗口内只有 T-2 有一个 AI 成功但边界为空的快照；其余天无快照（静态时间线）
    store.save_ready(
        _ready_snapshot(persona_hash, base - datetime.timedelta(days=2),
                        name="前日", boundary={}),
        retention_days=30, today=NOW.date(),
    )

    state = service._previous_day_state(persona_hash, NOW, window_days=7)
    assert state["boundary_state"] == {}
    assert state["boundary_date"] == ""
    assert len(state["actual_schedules"]) == 7
    # 升序：最旧一天在前，昨天在最后
    assert state["actual_schedules"][0]["date"] == (
        base - datetime.timedelta(days=7)
    ).isoformat()
    assert state["actual_schedules"][-1]["date"] == (
        base - datetime.timedelta(days=1)
    ).isoformat()
    assert state["actual_schedules"][-2]["slots"][0]["name"] == "前日"


def test_previous_day_state_window_limits_walkback(tmp_path):
    """窗口上限：更早的可用边界不在窗口内时视为空边界。"""
    fallback = [{
        "name": "静态睡眠",
        "start_time": "23:00",
        "end_time": "07:00",
        "state_prompt": "夜已深，正在睡眠",
    }]
    context = FakeContext('[]')
    service, store, _ = _service(tmp_path, context, config=_config(fallback=fallback))
    persona_hash = store.persona_hash("persona_demo")
    base = NOW.date()

    store.save_ready(
        _ready_snapshot(persona_hash, base - datetime.timedelta(days=3),
                        name="锚点日", boundary=BOUNDARY_ANCHOR),
        retention_days=30, today=NOW.date(),
    )

    state = service._previous_day_state(persona_hash, NOW, window_days=2)
    assert state["boundary_state"] == {}
    assert state["boundary_date"] == ""
    dates = [entry["date"] for entry in state["actual_schedules"]]
    assert dates == [
        (base - datetime.timedelta(days=offset)).isoformat()
        for offset in (2, 1)
    ]
    # 窗口内无快照的天：实际时间线 = 静态日程
    assert state["actual_schedules"][-1]["slots"][0]["name"] == "静态睡眠"


def test_force_regenerate_keeps_executed_clears_user_replaces_ai(tmp_path):
    """regenerate：保留 executed、清空未执行 user_slots、替换未来 AI（§8-9）。"""
    context = FakeContext(
        '[{"name":"全天","start":"00:00","end":"24:00","state":"今天状态稳定"}]'
    )
    service, store, _ = _service(tmp_path, context, sleep=_no_sleep)
    persona_hash = store.persona_hash("persona_demo")
    store.save_ready(
        {
            "snapshot_id": "old-snap",
            "persona_hash": persona_hash,
            "local_date": NOW.date().isoformat(),
            "timezone": "UTC",
            "status": "ready",
            "executed_until": "07:30",
            "executed_slots": [
                {"name": "晨起", "start": "06:30", "end": "07:30", "state": "刚醒",
                 "origin": "executed", "_source_origin": "ai"},
            ],
            "user_slots": [
                {"name": "用户安排", "start": "10:00", "end": "11:00", "state": "待办",
                 "origin": "user"},
            ],
            "ai_slots": [
                {"name": "旧AI", "start": "10:00", "end": "12:00", "state": "旧", "origin": "ai"},
            ],
            "static_slots": [],
            "slots": [],
        },
        retention_days=30,
        today=NOW.date(),
    )

    result = asyncio.run(service.generate_for_session(SESSION_A, force=True, now=NOW))
    assert result.success
    snapshot = store.get(persona_hash, NOW.date(), "UTC")
    # 已执行时间线保留
    assert snapshot["executed_slots"][0]["name"] == "晨起"
    # 未执行用户层清空
    assert snapshot["user_slots"] == []
    # 未来 AI 替换为新生成结果（裁剪到 cutoff 07:30 之后）
    assert [s["name"] for s in snapshot["ai_slots"]] == ["全天"]
    assert snapshot["ai_slots"][0]["start"] == "07:30"


# ==================== 合法空 AI 日程 ====================


def test_empty_ai_schedule_is_ready_snapshot_and_reused(tmp_path):
    """模型返回合法空数组：快照 ready 可用，当天重复请求直接复用不重生成。"""
    context = FakeContext("[]")
    service, store, _ = _service(tmp_path, context, sleep=_no_sleep)
    first = asyncio.run(service.generate_for_session(SESSION_A, now=NOW))
    assert first.success
    snapshot = service.get_snapshot_for_session(SESSION_A, now=NOW)
    assert snapshot is not None
    assert snapshot["status"] == "ready"
    assert snapshot["ai_slots"] == []
    # 空日程是有效结果：不写入失败记录
    assert store.get_failure(
        store.persona_hash("persona_demo"), NOW.date(), "UTC"
    ) is None
    # 当天重复请求复用快照，不再调用 LLM
    calls_before = len(context.calls)
    second = asyncio.run(service.generate_for_session(SESSION_A, now=NOW))
    assert second.reused
    assert len(context.calls) == calls_before


def test_empty_ai_schedule_resolves_to_static_or_unset(tmp_path):
    """空 AI 层不阻塞解析：命中静态日程时正常注入，否则「无固定安排」。"""
    fallback = [{
        "name": "静态午休",
        "start_time": "12:00",
        "end_time": "14:00",
        "state_prompt": "午休中",
    }]
    context = FakeContext("[]")
    service, store, _ = _service(
        tmp_path, context, config=_config(fallback=fallback), sleep=_no_sleep
    )
    asyncio.run(service.generate_for_session(SESSION_A, now=NOW))
    # NOW=07:30 未命中静态段 → 「无固定安排」（resolve 返回 None，由调用方注入）
    assert service.resolve(now=NOW, session=SESSION_A) is None
    # 命中静态段的时刻（12:30）正常解析
    noon = NOW.replace(hour=12, minute=30)
    resolution = service.resolve(now=noon, session=SESSION_A)
    assert resolution is not None
    assert resolution.state_prompt == "午休中"


def test_resolve_hits_current_minute_ai_slot_at_non_whole_minute(tmp_path):
    """运行时解析边界 = 当前时刻分钟（非 UI 的下一整分钟）：10:30:24 必须
    命中 08:00-11:00 的 AI 时段，而不是被裁剪成 10:31 起导致未命中。"""
    context = FakeContext(
        '[{"name":"上午任务","start":"08:00","end":"11:00","state":"专注处理"}]'
    )
    service, store, _ = _service(tmp_path, context, sleep=_no_sleep)
    asyncio.run(service.generate_for_session(SESSION_A, now=NOW))
    now = NOW.replace(hour=10, minute=30, second=24, microsecond=123456)
    resolution = service.resolve(now=now, session=SESSION_A)
    assert resolution is not None
    assert resolution.state_prompt == "专注处理"
    assert resolution.slot_name == "上午任务"
