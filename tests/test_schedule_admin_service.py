"""人格日程管理服务测试（personas / detail / save）。"""

import asyncio
import datetime
from types import SimpleNamespace

from time_awareness.core.daily_schedule_store import DailyScheduleSnapshotStore
from time_awareness.domain.schedule import parse_time
from time_awareness.domain.schedule_edit import slot_ref
from time_awareness.services.daily_schedule_admin_service import (
    DailyScheduleAdminService,
    ScheduleNotFound,
    SchedulePersistenceError,
    ScheduleSaveConflict,
)

NOW = datetime.datetime(2026, 8, 5, 10, 0, tzinfo=datetime.timezone.utc)
DATE = NOW.date()

SLOTS = [
    {"name": "睡眠", "start": "00:00", "end": "06:00", "state": "沉睡"},
    {"name": "晨起", "start": "10:03", "end": "12:00", "state": "清醒"},
]


class FakePersonaManager:
    def __init__(self, personas):
        self.personas = personas

    async def get_all_personas(self):
        return list(self.personas)


class FakeContext:
    def __init__(self, personas):
        self.persona_manager = FakePersonaManager(personas)


class FakeDailyScheduleService:
    def __init__(self, now=NOW, fallback=None):
        self.time_context = SimpleNamespace(now=lambda: now)
        self._locks = {}
        self._retention = 30
        self.config = {
            "daily_schedule": {"schedule_templates": list(fallback or [])}
        }

    def enabled(self):
        # 与真实 DailyScheduleService.enabled() 同义：available 且已配置开启。
        return True

    def retention_days(self):
        return self._retention

    def _current_static_slots(self):
        """与真实实现同源：读 schedule_templates 转换为 static_slots。"""
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
        return result

    def _ai_priority_over_static(self):
        return False


def _store_with_snapshot(tmp_path, persona_id="persona_a", slots=None,
                         snapshot_id="snap-1", date=None, timezone="UTC",
                         ai_slots=None, user_slots=None, executed_slots=None,
                         static_slots=None):
    """构造分层快照；未传分层字段时把 slots 整体作为 ai_slots。

    slots=None → ai_slots 默认 SLOTS；slots=[] → ai_slots 显式为空。
    """
    store = DailyScheduleSnapshotStore(str(tmp_path))
    assert store.load()
    layers = {
        "executed_until": "00:00",
        "executed_slots": list(executed_slots or []),
        "user_slots": list(user_slots or []),
        "ai_slots": list(
            ai_slots if ai_slots is not None else (SLOTS if slots is None else slots)
        ),
        "static_slots": list(static_slots or []),
    }
    snapshot = {
        "snapshot_id": snapshot_id,
        "persona_hash": store.persona_hash(persona_id),
        "local_date": (date or DATE).isoformat(),
        "timezone": timezone,
        "generated_at": "2026-08-05T09:00:00+00:00",
        "source": "ai",
        "slots": slots or SLOTS,
        "provider": "provider_demo",
        "prompt_revision": "daily_schedule_v2",
        "status": "ready",
        **layers,
    }
    assert store.save_ready(snapshot, retention_days=30, today=DATE)
    return store


def _service(tmp_path, store=None, personas=None, now=NOW, fallback=None):
    store = store or _store_with_snapshot(tmp_path)
    daily = FakeDailyScheduleService(now=now, fallback=fallback)
    admin = DailyScheduleAdminService(
        context=FakeContext(
            personas if personas is not None else [SimpleNamespace(persona_id="persona_a")]
        ),
        store=store,
        daily_schedule_service=daily,
    )
    return admin, store, daily


# ==================== list_personas ====================


def test_list_personas_matches_names_via_hmac(tmp_path):
    admin, store, _ = _service(
        tmp_path,
        personas=[
            SimpleNamespace(persona_id="persona_a"),
            SimpleNamespace(persona_id="persona_unused"),
        ],
    )
    result = asyncio.run(admin.list_personas())
    assert len(result) == 1
    persona = result[0]
    assert persona["key"] == store.persona_hash("persona_a")
    assert persona["name"] == "persona_a"
    assert persona["snapshot_count"] == 1
    assert persona["latest_date"] == DATE.isoformat()
    assert persona["dates"] == [{"date": DATE.isoformat(), "timezone": "UTC"}]


def test_list_personas_unmatched_shows_hash(tmp_path):
    admin, store, _ = _service(tmp_path, personas=[])  # AstrBot 无此 persona
    result = asyncio.run(admin.list_personas())
    assert len(result) == 1
    assert result[0]["name"].startswith("未找到的人格 · ")
    assert store.persona_hash("persona_a")[:8] in result[0]["name"]


def test_list_personas_no_secret_still_enumerates(tmp_path):
    """AI 日程关闭（无 secret 文件）时仍可枚举快照。"""
    store = _store_with_snapshot(tmp_path)
    # 删除 secret 文件，模拟只读场景
    secret_path = tmp_path / "daily_schedule_persona_secret.yaml"
    secret_path.unlink()
    admin = DailyScheduleAdminService(
        context=FakeContext([]),
        store=store,
        daily_schedule_service=FakeDailyScheduleService(),
    )
    result = asyncio.run(admin.list_personas())
    assert len(result) == 1
    assert "未找到的人格" in result[0]["name"]
    # 查看页面不得创建 secret 文件
    assert not secret_path.exists()


def test_list_personas_multiple_personas(tmp_path):
    store = DailyScheduleSnapshotStore(str(tmp_path))
    assert store.load()
    for persona_id in ("p1", "p2", "p3"):
        snapshot = {
            "snapshot_id": f"snap-{persona_id}",
            "persona_hash": store.persona_hash(persona_id),
            "local_date": DATE.isoformat(),
            "timezone": "UTC",
            "status": "ready",
            "slots": SLOTS,
        }
        assert store.save_ready(snapshot, retention_days=30, today=DATE)
    admin = DailyScheduleAdminService(
        context=FakeContext([]),
        store=store,
        daily_schedule_service=FakeDailyScheduleService(),
    )
    result = asyncio.run(admin.list_personas())
    assert len(result) == 3


# ==================== get_detail ====================


def test_get_detail_returns_permissions_for_today(tmp_path):
    admin, store, _ = _service(tmp_path)
    store = _store_with_snapshot(
        tmp_path,
        executed_slots=[{**SLOTS[0], "origin": "executed", "_source_origin": "ai"}],
        ai_slots=[SLOTS[1]],
    )
    admin, store, _ = _service(tmp_path, store=store)
    detail = admin.get_detail(store.persona_hash("persona_a"), DATE, "UTC")
    assert detail is not None
    assert detail["snapshot_id"] == "snap-1"
    assert detail["server_now"] == NOW.isoformat()
    # 10:00 时：已执行段只读；AI 段始终只读（未来 AI 卡片不可直接编辑）
    slots = detail["slots"]
    assert slots[0]["origin"] == "executed"
    assert slots[0]["edit_mode"] == "finished"
    assert slots[0]["editable_fields"] == []
    assert slots[0]["source_origin"] == "ai"
    assert slots[1]["origin"] == "ai"
    assert slots[1]["edit_mode"] == "future"
    assert slots[1]["editable_fields"] == []
    # slot_ref 稳定
    assert slots[0]["slot_ref"] == slot_ref("snap-1", 0)
    assert slots[1]["slot_ref"] == slot_ref("snap-1", 1)


def test_get_detail_static_is_always_readonly(tmp_path):
    fallback_config = [{
        "name": "静态日程",
        "start_time": "10:03",
        "end_time": "12:00",
        "state_prompt": "静态状态",
    }]
    store = _store_with_snapshot(tmp_path, slots=[])
    admin, store, _ = _service(tmp_path, store=store, fallback=fallback_config)
    detail = admin.get_detail(store.persona_hash("persona_a"), DATE, "UTC")
    assert detail is not None
    assert detail["slots"][0]["origin"] == "static"
    assert detail["slots"][0]["edit_mode"] == "future"
    assert detail["slots"][0]["editable_fields"] == []


def test_get_detail_historical_date_all_readonly(tmp_path):
    admin, store, _ = _service(tmp_path)
    yesterday = DATE - datetime.timedelta(days=1)
    store.save_ready(
        {
            "snapshot_id": "snap-old",
            "persona_hash": store.persona_hash("persona_a"),
            "local_date": yesterday.isoformat(),
            "timezone": "UTC",
            "status": "ready",
            "slots": SLOTS,
            "executed_until": "24:00",
            "executed_slots": [
                {**slot, "origin": "executed", "_source_origin": "ai"}
                for slot in SLOTS
            ],
            "user_slots": [],
            "ai_slots": [],
            "static_slots": [],
        },
        retention_days=30,
        today=DATE,
    )
    detail = admin.get_detail(store.persona_hash("persona_a"), yesterday, "UTC")
    assert detail is not None
    assert all(slot["edit_mode"] == "finished" for slot in detail["slots"])


def test_get_detail_missing_snapshot_returns_none(tmp_path):
    admin, store, _ = _service(tmp_path)
    assert admin.get_detail(store.persona_hash("persona_a"), DATE + datetime.timedelta(days=1), "UTC") is None


# ==================== save ====================


def _payload_from_slots(slots, snapshot_id):
    return [
        {
            "slot_ref": slot_ref(snapshot_id, index) if slot.get("_keep") else slot.get("slot_ref", ""),
            "name": slot["name"],
            "start": slot["start"],
            "end": slot["end"],
            "state": slot["state"],
        }
        for index, slot in enumerate(slots)
    ]


def test_save_success_updates_snapshot(tmp_path):
    admin, store, daily = _service(tmp_path)
    persona_hash = store.persona_hash("persona_a")
    # 保存协议只提交用户层草稿：新增「晨起」用户段覆盖 AI（10:03→10:15）
    slots = [{"name": "晨起", "start": "10:15", "end": "12:00", "state": "清醒"}]
    payload = _payload_from_slots(slots, "snap-1")
    result = asyncio.run(admin.save(
        persona_hash=persona_hash, local_date=DATE, timezone="UTC",
        snapshot_id="snap-1", slots=payload,
    ))
    assert result["snapshot_id"]
    assert result["snapshot_id"] != "snap-1"
    updated = store.get(persona_hash, DATE, "UTC")
    assert updated["snapshot_id"] == result["snapshot_id"]
    # 用户层独立保存；AI 层原样保留（裁剪到 cutoff 后）
    assert updated["user_slots"][0]["start"] == "10:15"
    assert updated["user_slots"][0]["origin"] == "user"
    assert updated["ai_slots"][0]["name"] == "晨起"  # AI 原版未被修改
    # 渲染缓存：cutoff 前已固化 executed（睡眠段），cutoff 后用户段生效
    executed = next(s for s in updated["slots"] if s["origin"] == "executed")
    assert executed["start"] == "00:00" and executed["end"] == "06:00"
    user_rendered = next(s for s in updated["slots"] if s["origin"] == "user")
    assert user_rendered["start"] == "10:15"
    assert user_rendered["end"] == "12:00"
    assert updated["manually_edited"] is True
    assert updated["edited_at"]
    assert updated["generated_at"] == "2026-08-05T09:00:00+00:00"  # 保留
    assert updated["prompt_revision"] == "daily_schedule_v2"  # 保留（不触发补生成覆盖）


def test_save_rebuilds_static_gaps_from_config(tmp_path):
    fallback_config = [{
        "name": "静态日程",
        "start_time": "10:03",
        "end_time": "14:00",
        "state_prompt": "静态状态",
    }]
    store = _store_with_snapshot(tmp_path, slots=[], static_slots=[])
    admin, store, _ = _service(tmp_path, store=store, fallback=fallback_config)
    persona_hash = store.persona_hash("persona_a")

    first = asyncio.run(admin.save(
        persona_hash=persona_hash,
        local_date=DATE,
        timezone="UTC",
        snapshot_id="snap-1",
        slots=[{"name": "用户安排", "start": "11:00", "end": "12:00", "state": "用户状态"}],
    ))
    current = store.get(persona_hash, DATE, "UTC")
    assert [(s["start"], s["end"], s["origin"]) for s in current["slots"]] == [
        ("10:03", "11:00", "static"),
        ("11:00", "12:00", "user"),
        ("12:00", "14:00", "static"),
    ]
    # 用户段修改只提交用户层；静态日程从配置重建空档
    asyncio.run(admin.save(
        persona_hash=persona_hash,
        local_date=DATE,
        timezone="UTC",
        snapshot_id=first["snapshot_id"],
        slots=[{"name": "用户安排", "start": "11:30", "end": "12:00", "state": "用户状态"}],
    ))
    current = store.get(persona_hash, DATE, "UTC")
    assert [(s["start"], s["end"], s["origin"]) for s in current["slots"]] == [
        ("10:03", "11:30", "static"),
        ("11:30", "12:00", "user"),
        ("12:00", "14:00", "static"),
    ]


def test_save_conflict_returns_409(tmp_path):
    admin, store, _ = _service(tmp_path)
    payload = _payload_from_slots(
        [dict(s, _keep=True) for s in SLOTS], "snap-1"
    )
    # 快照被 regenerate 过（snapshot_id 变化）→ 409
    snapshot = store.get(store.persona_hash("persona_a"), DATE, "UTC")
    snapshot["snapshot_id"] = "snap-regenerated"
    assert store.save_ready(snapshot, retention_days=30, today=DATE)
    try:
        asyncio.run(admin.save(
            persona_hash=store.persona_hash("persona_a"), local_date=DATE,
            timezone="UTC", snapshot_id="snap-1", slots=payload,
        ))
        raise AssertionError("应当抛出 ScheduleSaveConflict")
    except ScheduleSaveConflict:
        pass


def test_save_rejects_user_slot_starting_before_cutoff(tmp_path):
    """用户段不得覆盖已冻结区间（start 早于冻结点被拒）。"""
    now = datetime.datetime(2026, 8, 5, 10, 30, tzinfo=datetime.timezone.utc)
    store = _store_with_snapshot(tmp_path, slots=[])
    admin, _, _ = _service(tmp_path, store=store, now=now)
    # 10:30 时冻结点为 10:31；用户段 10:00 开始 → 拒绝
    bad = [
        {"name": "临时", "start": "10:00", "end": "12:00", "state": "忙碌"},
    ]
    try:
        asyncio.run(admin.save(
            persona_hash=store.persona_hash("persona_a"), local_date=DATE,
            timezone="UTC", snapshot_id="snap-1", slots=bad,
        ))
        raise AssertionError("应当抛出 ValueError")
    except ValueError as exc:
        assert "冻结点" in str(exc)


def test_save_persistence_failure_restores_old_snapshot(tmp_path, monkeypatch):
    admin, store, _ = _service(tmp_path)
    persona_hash = store.persona_hash("persona_a")
    payload = [{"name": "晨起", "start": "10:15", "end": "12:00", "state": "清醒"}]
    monkeypatch.setattr(store, "_save", lambda: False)
    try:
        asyncio.run(admin.save(
            persona_hash=persona_hash, local_date=DATE,
            timezone="UTC", snapshot_id="snap-1", slots=payload,
        ))
        raise AssertionError("应当抛出 SchedulePersistenceError")
    except SchedulePersistenceError as exc:
        assert "持久化失败" in str(exc)
    # 内存与磁盘均保持旧快照
    snapshot = store.get(persona_hash, DATE, "UTC")
    assert snapshot["snapshot_id"] == "snap-1"
    assert not snapshot.get("manually_edited")


def test_save_not_found(tmp_path):
    admin, store, _ = _service(tmp_path)
    try:
        asyncio.run(admin.save(
            persona_hash=store.persona_hash("persona_a"),
            local_date=DATE + datetime.timedelta(days=1),
            timezone="UTC", snapshot_id="snap-1", slots=[],
        ))
        raise AssertionError("应当抛出 ScheduleNotFound")
    except ScheduleNotFound:
        pass


def test_save_persists_user_slot_in_user_layer_and_cache(tmp_path):
    """保存后用户段进入 user_slots 权威层，渲染缓存同步该段。"""
    admin, store, daily = _service(tmp_path)
    persona_hash = store.persona_hash("persona_a")
    # 新增未来用户段
    asyncio.run(admin.save(
        persona_hash=persona_hash, local_date=DATE, timezone="UTC",
        snapshot_id="snap-1",
        slots=[{"name": "晨起", "start": "10:15", "end": "12:00", "state": "清醒"}],
    ))
    snapshot = store.get(persona_hash, DATE, "UTC")
    # 用户层独立持久化，作为次日「昨日实际日程」的读取来源
    assert snapshot["user_slots"][0]["start"] == "10:15"
    assert snapshot["user_slots"][0]["origin"] == "user"
    user_rendered = next(s for s in snapshot["slots"] if s["origin"] == "user")
    assert user_rendered["start"] == "10:15"  # 渲染缓存同步


def test_save_preserves_origin_and_never_touches_static_config(tmp_path):
    """保存只改用户层：AI 原样保留、静态日程从配置重建、绝不写回配置。"""
    fallback_config = [{
        "name": "静态日程",
        "start_time": "12:30",
        "end_time": "14:00",
        "state_prompt": "静态状态",
    }]
    original_ai = [{"name": "晨起", "start": "10:03", "end": "12:00", "state": "清醒", "origin": "ai"}]
    store = _store_with_snapshot(tmp_path, slots=[], ai_slots=original_ai)
    admin, store, daily = _service(tmp_path, store=store, fallback=fallback_config)
    persona_hash = store.persona_hash("persona_a")
    # 新增用户时段（保存协议只提交用户层）
    result = asyncio.run(admin.save(
        persona_hash=persona_hash, local_date=DATE, timezone="UTC",
        snapshot_id="snap-1",
        slots=[{"name": "临时", "start": "14:00", "end": "15:00", "state": "安排"}],
    ))
    assert result["snapshot_id"]
    updated = store.get(persona_hash, DATE, "UTC")
    # AI 层原样保留（未被前端/保存流程修改；仅裁剪时附加内部分钟字段）
    assert [
        {k: s[k] for k in ("name", "start", "end", "state", "origin")}
        for s in updated["ai_slots"]
    ] == original_ai
    assert updated["user_slots"][0]["origin"] == "user"
    # 静态日程以当前配置副本保存
    assert [s["origin"] for s in updated["static_slots"]] == ["static"]
    # 渲染缓存带来源标记：AI / 静态 / 用户共存
    origins = {s.get("origin") for s in updated["slots"]}
    assert {"ai", "static", "user"} <= origins
    # 仅读取配置重建快照空档，绝不写回静态配置。
    assert not hasattr(admin, "config")
    assert daily.config["daily_schedule"]["schedule_templates"] == fallback_config


# ==================== 保存校验：重叠 / 24:00 边界（§8-6 / §8-16） ====================


def test_save_rejects_overlapping_user_slots(tmp_path):
    """用户时段互相重叠被拒绝。"""
    admin, store, _ = _service(tmp_path)
    try:
        asyncio.run(admin.save(
            persona_hash=store.persona_hash("persona_a"),
            local_date=DATE, timezone="UTC", snapshot_id="snap-1",
            slots=[
                {"name": "安排A", "start": "10:00", "end": "12:00", "state": "甲"},
                {"name": "安排B", "start": "11:00", "end": "13:00", "state": "乙"},
            ],
        ))
        raise AssertionError("应当抛出 ValueError")
    except ValueError as exc:
        assert "重叠" in str(exc)


def test_save_rejects_start_at_2400_and_accepts_end_at_2400(tmp_path):
    """start=24:00 明确拒绝；end=24:00 全链合法。"""
    admin, store, _ = _service(tmp_path)
    persona_hash = store.persona_hash("persona_a")
    try:
        asyncio.run(admin.save(
            persona_hash=persona_hash, local_date=DATE, timezone="UTC",
            snapshot_id="snap-1",
            slots=[{"name": "非法", "start": "24:00", "end": "24:00", "state": "x"}],
        ))
        raise AssertionError("应当抛出 ValueError")
    except ValueError:
        pass
    # end=24:00 合法保存
    result = asyncio.run(admin.save(
        persona_hash=persona_hash, local_date=DATE, timezone="UTC",
        snapshot_id="snap-1",
        slots=[{"name": "夜间", "start": "22:00", "end": "24:00", "state": "休息"}],
    ))
    assert result["snapshot_id"]
    updated = store.get(persona_hash, DATE, "UTC")
    assert updated["user_slots"][0]["end"] == "24:00"


# ==================== 跨冻结点用户段（§8-7 / §8-8） ====================


def test_slot_ending_exactly_at_cutoff_shows_active_but_readonly(tmp_path):
    """结束时刻恰好等于冻结点且当前分钟仍在时段内：显示「正在进行」但只读。"""
    now = datetime.datetime(2026, 8, 5, 21, 51, 30, tzinfo=datetime.timezone.utc)
    store = _store_with_snapshot(
        tmp_path,
        slots=[],
        ai_slots=[
            {"name": "洗漱准备", "start": "21:43", "end": "21:52", "state": "困意浮现",
             "origin": "ai"},
        ],
    )
    admin, store, _ = _service(tmp_path, store=store, now=now)
    detail = admin.get_detail(store.persona_hash("persona_a"), DATE, "UTC")
    slot = next(s for s in detail["slots"] if s["name"] == "洗漱准备")
    assert slot["edit_mode"] == "active"  # 时间上还没结束
    assert slot["editable_fields"] == []  # 但整体已冻结，保持只读
    assert slot["deletable"] is False


def test_future_date_detail_marks_all_slots_future(tmp_path):
    """未来日期查看：整日尚未开始，00:00 开始的时段也必须是 future 而非 active。"""
    future_date = DATE + datetime.timedelta(days=1)
    store = _store_with_snapshot(
        tmp_path,
        date=future_date,
        slots=[],
        ai_slots=[
            {"name": "深夜巡逻", "start": "00:00", "end": "04:00", "state": "警戒",
             "origin": "ai"},
            {"name": "补觉", "start": "05:00", "end": "11:00", "state": "沉睡",
             "origin": "ai"},
        ],
    )
    admin, store, _ = _service(tmp_path, store=store, now=NOW)
    detail = admin.get_detail(store.persona_hash("persona_a"), future_date, "UTC")
    for slot in detail["slots"]:
        assert slot["edit_mode"] == "future", slot["name"]
        assert slot["editable_fields"] == []  # ai 来源始终只读
        assert slot["deletable"] is False


def test_cross_cutoff_user_slot_frozen_head_and_editable_tail(tmp_path):
    """跨冻结点用户段拆为已执行前段 + 未来尾段；尾段仅可改 end 或删除。"""
    now = datetime.datetime(2026, 8, 5, 10, 0, 30, tzinfo=datetime.timezone.utc)
    store = _store_with_snapshot(
        tmp_path,
        slots=[],
        executed_slots=[
            {"name": "用户安排", "start": "09:00", "end": "10:01", "state": "待办",
             "origin": "executed", "_source_origin": "user", "user_slot_id": "u1"},
        ],
        user_slots=[
            {"name": "用户安排", "start": "10:01", "end": "12:00", "state": "待办",
             "origin": "user", "user_slot_id": "u1"},
        ],
    )
    admin, store, _ = _service(tmp_path, store=store, now=now)
    detail = admin.get_detail(store.persona_hash("persona_a"), DATE, "UTC")
    # 展示层把跨当前时刻的同源段合并为一段「正在执行」：
    # 不切成「已结束前段 + 即将开始尾段」两段（Q4）
    assert len(detail["slots"]) == 1
    merged = detail["slots"][0]
    assert merged["origin"] == "user"
    assert merged["start"] == "09:00"
    assert merged["end"] == "12:00"
    assert merged["edit_mode"] == "active"  # 正在执行
    assert merged["editable_fields"] == ["end"]  # 仅允许修改结束时间
    assert merged["deletable"] is True  # 删除只删未来部分（已执行前段固化保留）


# ==================== 跨冻结点用户尾段保存 ====================


def _store_with_user_slots(tmp_path, user_slots, now=NOW):
    """构造带 user_slot_id 的分层快照。"""
    store = _store_with_snapshot(
        tmp_path, slots=[], user_slots=user_slots,
    )
    admin, store, _ = _service(tmp_path, store=store, now=now)
    return admin, store


def test_save_cross_cutoff_tail_end_edit_advances_and_splits(tmp_path):
    """页面载入后冻结点前移：修改已有用户时段结束时间仍能保存。"""
    admin, store = _store_with_user_slots(
        tmp_path,
        user_slots=[{
            "user_slot_id": "u1", "name": "用户安排", "start": "10:00",
            "end": "12:00", "state": "待办", "origin": "user",
        }],
    )
    persona_hash = store.persona_hash("persona_a")
    # 页面载入时 10:00（cutoff=10:01），用户把 end 改为 13:00；
    # 请求到达服务端时 now=10:00:30（cutoff=10:01），payload start 仍是原值
    result = asyncio.run(admin.save(
        persona_hash=persona_hash, local_date=DATE, timezone="UTC",
        snapshot_id="snap-1", now=datetime.datetime(2026, 8, 5, 10, 0, 30,
                                                    tzinfo=datetime.timezone.utc),
        slots=[{"user_slot_id": "u1", "name": "用户安排", "start": "10:00",
                "end": "13:00", "state": "待办"}],
    ))
    assert result["snapshot_id"]
    updated = store.get(persona_hash, DATE, "UTC")
    # 已执行前段固化进 executed_slots（source_origin=user）
    frozen = [
        s for s in updated["executed_slots"]
        if s.get("_source_origin") == "user"
    ]
    assert [(parse_time(s["start"]), parse_time(s["end"], allow_24=True)) for s in frozen] == [(600, 601)]
    # 未来尾段起点推进到最新冻结点，id 稳定保留
    assert [(s["start"], s["end"]) for s in updated["user_slots"]] == [("10:01", "13:00")]
    assert updated["user_slots"][0]["user_slot_id"] == "u1"


def test_save_cross_cutoff_tail_rejects_name_or_start_edit(tmp_path):
    """跨冻结点尾段同时修改名称/开始时间被拒绝（只允许改 end）。"""
    admin, store = _store_with_user_slots(
        tmp_path,
        user_slots=[{
            "user_slot_id": "u1", "name": "用户安排", "start": "10:00",
            "end": "12:00", "state": "待办", "origin": "user",
        }],
    )
    persona_hash = store.persona_hash("persona_a")
    for bad in (
        {"user_slot_id": "u1", "name": "改名", "start": "10:00",
         "end": "13:00", "state": "待办"},
        {"user_slot_id": "u1", "name": "用户安排", "start": "09:30",
         "end": "13:00", "state": "待办"},
    ):
        try:
            asyncio.run(admin.save(
                persona_hash=persona_hash, local_date=DATE, timezone="UTC",
                snapshot_id="snap-1", now=datetime.datetime(2026, 8, 5, 10, 0, 30,
                                                            tzinfo=datetime.timezone.utc),
                slots=[bad],
            ))
            raise AssertionError("应当抛出 ValueError")
        except ValueError as exc:
            assert "仅允许修改结束时间" in str(exc)


def test_save_deleting_tail_keeps_frozen_head(tmp_path):
    """删除跨冻结点用户尾段（payload 不含该 id）只删未来部分。"""
    admin, store = _store_with_user_slots(
        tmp_path,
        user_slots=[{
            "user_slot_id": "u1", "name": "用户安排", "start": "10:00",
            "end": "12:00", "state": "待办", "origin": "user",
        }],
    )
    persona_hash = store.persona_hash("persona_a")
    asyncio.run(admin.save(
        persona_hash=persona_hash, local_date=DATE, timezone="UTC",
        snapshot_id="snap-1", now=datetime.datetime(2026, 8, 5, 10, 0, 30,
                                                    tzinfo=datetime.timezone.utc),
        slots=[],
    ))
    updated = store.get(persona_hash, DATE, "UTC")
    assert updated["user_slots"] == []  # 未来尾段已删除
    frozen = [
        s for s in updated["executed_slots"]
        if s.get("_source_origin") == "user"
    ]
    assert [(parse_time(s["start"]), parse_time(s["end"], allow_24=True)) for s in frozen] == [(600, 601)]  # 前段保留


def test_save_new_past_slot_still_rejected_with_splitting_in_place(tmp_path):
    """拆分机制存在时，新增 start < cutoff 的用户时段仍被拒绝。"""
    admin, store = _store_with_user_slots(
        tmp_path,
        user_slots=[{
            "user_slot_id": "u1", "name": "用户安排", "start": "10:00",
            "end": "12:00", "state": "待办", "origin": "user",
        }],
    )
    persona_hash = store.persona_hash("persona_a")
    try:
        asyncio.run(admin.save(
            persona_hash=persona_hash, local_date=DATE, timezone="UTC",
            snapshot_id="snap-1",
            slots=[{"name": "伪造过去段", "start": "09:00", "end": "11:00",
                    "state": "x"}],
        ))
        raise AssertionError("应当抛出 ValueError")
    except ValueError as exc:
        assert "开始时间不能早于冻结点" in str(exc)


def test_save_stale_snapshot_id_conflicts_before_splitting(tmp_path):
    """过期 snapshot_id 仍返回冲突，不因拆分逻辑绕过并发控制。"""
    admin, store = _store_with_user_slots(
        tmp_path,
        user_slots=[{
            "user_slot_id": "u1", "name": "用户安排", "start": "10:00",
            "end": "12:00", "state": "待办", "origin": "user",
        }],
    )
    persona_hash = store.persona_hash("persona_a")
    snapshot = store.get(persona_hash, DATE, "UTC")
    snapshot["snapshot_id"] = "snap-regenerated"
    assert store.save_ready(snapshot, retention_days=30, today=DATE)
    try:
        asyncio.run(admin.save(
            persona_hash=persona_hash, local_date=DATE, timezone="UTC",
            snapshot_id="snap-1",
            slots=[{"user_slot_id": "u1", "name": "用户安排", "start": "10:00",
                    "end": "13:00", "state": "待办"}],
        ))
        raise AssertionError("应当抛出 ScheduleSaveConflict")
    except ScheduleSaveConflict:
        pass


def test_save_future_slot_full_edit_with_id(tmp_path):
    """完全未来的已有用户时段（带 id）可修改全部字段。"""
    admin, store = _store_with_user_slots(
        tmp_path,
        user_slots=[{
            "user_slot_id": "u1", "name": "用户安排", "start": "14:00",
            "end": "15:00", "state": "待办", "origin": "user",
        }],
    )
    persona_hash = store.persona_hash("persona_a")
    result = asyncio.run(admin.save(
        persona_hash=persona_hash, local_date=DATE, timezone="UTC",
        snapshot_id="snap-1",
        slots=[{"user_slot_id": "u1", "name": "改后", "start": "14:30",
                "end": "16:00", "state": "已改"}],
    ))
    assert result["snapshot_id"]
    updated = store.get(persona_hash, DATE, "UTC")
    assert updated["user_slots"][0]["name"] == "改后"
    assert updated["user_slots"][0]["start"] == "14:30"
    assert updated["user_slots"][0]["end"] == "16:00"
    assert updated["user_slots"][0]["user_slot_id"] == "u1"


def test_get_detail_returns_user_slot_id_for_user_slots(tmp_path):
    """详情 DTO 必须返回 user_slot_id，前端保存才能回传稳定引用。"""
    admin, store = _store_with_user_slots(
        tmp_path,
        user_slots=[{
            "user_slot_id": "u-abc", "name": "用户安排", "start": "10:00",
            "end": "12:00", "state": "待办", "origin": "user",
        }],
    )
    detail = admin.get_detail(store.persona_hash("persona_a"), DATE, "UTC")
    user_slots = [s for s in detail["slots"] if s["origin"] == "user"]
    assert len(user_slots) == 1
    assert user_slots[0]["user_slot_id"] == "u-abc"
    # 非用户时段不携带（或为空）
    for slot in detail["slots"]:
        if slot["origin"] != "user":
            assert slot["user_slot_id"] == ""


def test_save_cross_cutoff_tail_with_rendered_start_value(tmp_path):
    """页面载入时渲染的尾段 start（=当时冻结点）在保存时冻结点已前移，
    只要 start 落在 [原 start, 最新冻结点] 内仍可只改 end 保存。"""
    admin, store = _store_with_user_slots(
        tmp_path,
        user_slots=[{
            "user_slot_id": "u1", "name": "用户安排", "start": "10:00",
            "end": "12:00", "state": "待办", "origin": "user",
        }],
    )
    persona_hash = store.persona_hash("persona_a")
    # 页面载入时 cutoff=10:01，渲染 tail start=10:01；保存时 cutoff=10:02
    result = asyncio.run(admin.save(
        persona_hash=persona_hash, local_date=DATE, timezone="UTC",
        snapshot_id="snap-1",
        now=datetime.datetime(2026, 8, 5, 10, 1, 30, tzinfo=datetime.timezone.utc),
        slots=[{"user_slot_id": "u1", "name": "用户安排", "start": "10:01",
                "end": "13:00", "state": "待办"}],  # start=页面渲染值
    ))
    assert result["snapshot_id"]
    updated = store.get(persona_hash, DATE, "UTC")
    # 尾段起点推进到最新冻结点 10:02
    assert updated["user_slots"][0]["start"] == "10:02"
    assert updated["user_slots"][0]["end"] == "13:00"
    frozen = [
        s for s in updated["executed_slots"]
        if s.get("_source_origin") == "user"
    ]
    assert [(parse_time(s["start"]), parse_time(s["end"], allow_24=True))
            for s in frozen] == [(600, 602)]


def test_save_cross_cutoff_tail_rejects_moving_start_beyond_cutoff(tmp_path):
    """把跨冻结点尾段 start 改到最新冻结点之后视为移动时段，拒绝。"""
    admin, store = _store_with_user_slots(
        tmp_path,
        user_slots=[{
            "user_slot_id": "u1", "name": "用户安排", "start": "10:00",
            "end": "12:00", "state": "待办", "origin": "user",
        }],
    )
    persona_hash = store.persona_hash("persona_a")
    try:
        asyncio.run(admin.save(
            persona_hash=persona_hash, local_date=DATE, timezone="UTC",
            snapshot_id="snap-1",
            now=datetime.datetime(2026, 8, 5, 10, 0, 30, tzinfo=datetime.timezone.utc),
            slots=[{"user_slot_id": "u1", "name": "用户安排", "start": "11:00",
                    "end": "13:00", "state": "待办"}],  # start > 新 cutoff 10:01
        ))
        raise AssertionError("应当抛出 ValueError")
    except ValueError as exc:
        assert "仅允许修改结束时间" in str(exc)


def test_delete_user_slot_removes_future_only(tmp_path):
    """删除端点：跨冻结点用户段只删未来部分，已执行前段固化保留。"""
    admin, store = _store_with_user_slots(
        tmp_path,
        user_slots=[{
            "user_slot_id": "u1", "name": "用户安排", "start": "10:00",
            "end": "12:00", "state": "待办", "origin": "user",
        }],
    )
    persona_hash = store.persona_hash("persona_a")
    now = datetime.datetime(2026, 8, 5, 10, 0, 30, tzinfo=datetime.timezone.utc)
    result = asyncio.run(admin.delete_user_slot(
        persona_hash=persona_hash, local_date=DATE, timezone="UTC",
        snapshot_id="snap-1", user_slot_id="u1", now=now,
    ))
    assert result["snapshot_id"] != "snap-1"
    updated = store.get(persona_hash, DATE, "UTC")
    # 未来尾段已删除；10:00-10:01 前段固化在 executed_slots（source=user）
    assert updated["user_slots"] == []
    frozen = [
        s for s in updated["executed_slots"]
        if s.get("_source_origin") == "user"
    ]
    assert [(parse_time(s["start"]), parse_time(s["end"], allow_24=True))
            for s in frozen] == [(600, 601)]


def test_delete_user_slot_unknown_id_and_conflict(tmp_path):
    """删除不存在的 user_slot_id → 400；snapshot_id 过期 → 409。"""
    admin, store = _store_with_user_slots(
        tmp_path,
        user_slots=[{
            "user_slot_id": "u1", "name": "用户安排", "start": "10:00",
            "end": "12:00", "state": "待办", "origin": "user",
        }],
    )
    persona_hash = store.persona_hash("persona_a")
    try:
        asyncio.run(admin.delete_user_slot(
            persona_hash=persona_hash, local_date=DATE, timezone="UTC",
            snapshot_id="snap-1", user_slot_id="ghost",
        ))
        raise AssertionError("应当抛出 ValueError")
    except ValueError as exc:
        assert "user_slot_id" in str(exc)
    try:
        asyncio.run(admin.delete_user_slot(
            persona_hash=persona_hash, local_date=DATE, timezone="UTC",
            snapshot_id="stale-id", user_slot_id="u1",
        ))
        raise AssertionError("应当抛出 ScheduleSaveConflict")
    except ScheduleSaveConflict:
        pass
