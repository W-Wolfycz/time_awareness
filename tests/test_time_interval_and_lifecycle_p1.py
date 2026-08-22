"""P1 聚焦测试：时间区间运行语义 + initialize 失败回滚。

覆盖范围：
- 时间运行语义统一（P1 约定）：运行时午夜边界 = ``00:00``（``minute_of_day``
  只返回 0..1439）；配置 ``end=24:00`` 只是不可命中的右开端点（``1440``），
  合法但永不命中；静态跨午夜段（``start > end``）在规范化/存储边界拆成
  ``[start, 当日结束)`` 与 ``[00:00, end)`` 两个非跨日段。
- 命中 / overlap / 四层 timeline（clip / subtract / merge / advance / render）
  各路径对上述语义一致：没有一条路径接受而另一条拒绝。
- initialize() 失败回滚：scheduler / 每日任务 / Web API 任一步失败均不会
  遗留后台资源；正常生命周期（initialize → terminate）与重复 terminate
  不造成双清理。

本文件会安装一个最小 AstrBot stub 以构造 ``TimeAwarenessPlugin``（纯逻辑
部分不依赖该 stub）。
"""

import asyncio
import datetime
import sys
import types

import pytest

# ==================== 最小 AstrBot stub（仅在缺失时安装） ====================


def _identity_decorator(func):
    return func


class _CommandGroup:
    def command(self, *args, **kwargs):
        return _identity_decorator


def _install_astrbot_stubs():
    """安装构造 TimeAwarenessPlugin 所需的最小 AstrBot stub。

    ``tests/conftest.py`` 已提供 ``astrbot.api``（logger）与
    ``astrbot.api.message_components``（At/Plain）；这里补齐其余模块。
    """
    if getattr(_install_astrbot_stubs, "_installed", False):
        return
    _install_astrbot_stubs._installed = True

    api = sys.modules["astrbot.api"]

    if not hasattr(api, "AstrBotConfig"):
        class AstrBotConfig(dict):
            pass
        api.AstrBotConfig = AstrBotConfig

    if "astrbot.api.event" not in sys.modules:
        ev = types.ModuleType("astrbot.api.event")

        class AstrMessageEvent:
            pass

        class _PermissionType:
            ADMIN = "admin"

        class Filter:
            PermissionType = _PermissionType

            @staticmethod
            def on_llm_request(*a, **k):
                return _identity_decorator

            @staticmethod
            def after_message_sent(*a, **k):
                return _identity_decorator

            @staticmethod
            def command_group(*a, **k):
                return lambda _func: _CommandGroup()

            @staticmethod
            def permission_type(*a, **k):
                return _identity_decorator

            @staticmethod
            def llm_tool(*a, **k):
                return _identity_decorator

        ev.AstrMessageEvent = AstrMessageEvent
        ev.filter = Filter()
        sys.modules["astrbot.api.event"] = ev
        api.event = ev

    if "astrbot.api.provider" not in sys.modules:
        prov = types.ModuleType("astrbot.api.provider")

        class ProviderRequest:
            def __init__(self, *a, **k):
                self.system_prompt = ""
                self.extra_user_content_parts = []

        prov.ProviderRequest = ProviderRequest
        sys.modules["astrbot.api.provider"] = prov
        api.provider = prov

    if "astrbot.api.star" not in sys.modules:
        star = types.ModuleType("astrbot.api.star")

        class Context:
            pass

        class Star:
            def __init__(self, context):
                self.context = context

        class StarTools:
            @staticmethod
            def get_data_dir(name):
                # 测试中由 fixture monkeypatch 覆盖
                return "/tmp/time_awareness_test_default_data"

        star.Context = Context
        star.Star = Star
        star.StarTools = StarTools
        sys.modules["astrbot.api.star"] = star
        api.star = star
        api.StarTools = StarTools

    # ---- astrbot.core 链（astrbot_sender / daily_schedule_generation 需要） ----
    core = sys.modules.setdefault("astrbot.core", types.ModuleType("astrbot.core"))

    agent = sys.modules.setdefault("astrbot.core.agent", types.ModuleType("astrbot.core.agent"))
    core.agent = agent

    if "astrbot.core.agent.message" not in sys.modules:
        msg = types.ModuleType("astrbot.core.agent.message")

        class TextPart:
            def __init__(self, text):
                self.text = text

            def mark_as_temp(self):
                return self

        msg.TextPart = TextPart
        sys.modules["astrbot.core.agent.message"] = msg
        agent.message = msg

    if "astrbot.core.message.message_event_result" not in sys.modules:
        core_message = types.ModuleType("astrbot.core.message")
        sys.modules["astrbot.core.message"] = core_message
        core.message = core_message
        mer = types.ModuleType("astrbot.core.message.message_event_result")

        class MessageChain:
            def __init__(self, chain=None):
                self.chain = chain or []

        class MessageEventResult:
            pass

        class ResultContentType:
            LLM_RESULT = "llm_result"

        mer.MessageChain = MessageChain
        mer.MessageEventResult = MessageEventResult
        mer.ResultContentType = ResultContentType
        sys.modules["astrbot.core.message.message_event_result"] = mer
        core_message.message_event_result = mer

    if "astrbot.core.platform.astr_message_event" not in sys.modules:
        platform = types.ModuleType("astrbot.core.platform")
        sys.modules["astrbot.core.platform"] = platform
        core.platform = platform

        ame = types.ModuleType("astrbot.core.platform.astr_message_event")

        class AstrMessageEvent:
            def __init__(self, *a, **k):
                pass

        ame.AstrMessageEvent = AstrMessageEvent
        sys.modules["astrbot.core.platform.astr_message_event"] = ame
        platform.astr_message_event = ame

        abm = types.ModuleType("astrbot.core.platform.astrbot_message")

        class AstrBotMessage:
            pass

        class Group:
            pass

        class MessageMember:
            pass

        abm.AstrBotMessage = AstrBotMessage
        abm.Group = Group
        abm.MessageMember = MessageMember
        sys.modules["astrbot.core.platform.astrbot_message"] = abm
        platform.astrbot_message = abm

        ms = types.ModuleType("astrbot.core.platform.message_session")

        class MessageSession:
            def __init__(self):
                self.platform_id = ""

            @classmethod
            def from_str(cls, value):
                obj = cls()
                obj.platform_id = str(value).split(":")[0] if ":" in str(value) else str(value)
                return obj

        ms.MessageSession = MessageSession
        sys.modules["astrbot.core.platform.message_session"] = ms
        platform.message_session = ms

        mt = types.ModuleType("astrbot.core.platform.message_type")
        mt.MessageType = type("MessageType", (), {})
        sys.modules["astrbot.core.platform.message_type"] = mt
        platform.message_type = mt

    if "astrbot.core.star.star_handler" not in sys.modules:
        star_mod = types.ModuleType("astrbot.core.star")
        sys.modules["astrbot.core.star"] = star_mod
        core.star = star_mod
        sh = types.ModuleType("astrbot.core.star.star_handler")

        class EventType:
            pass

        sh.EventType = EventType
        sh.star_handlers_registry = None
        sys.modules["astrbot.core.star.star_handler"] = sh
        star_mod.star_handler = sh

    if "astrbot.core.utils.llm_metadata" not in sys.modules:
        utils = types.ModuleType("astrbot.core.utils")
        sys.modules["astrbot.core.utils"] = utils
        core.utils = utils
        lm = types.ModuleType("astrbot.core.utils.llm_metadata")
        lm.LLM_METADATAS = {}
        sys.modules["astrbot.core.utils.llm_metadata"] = lm
        utils.llm_metadata = lm

    # ---- quart（web_api.py 需要；仅在 lifecycle 中实际构造时使用） ----
    if "quart" not in sys.modules:
        quart = types.ModuleType("quart")

        class _Request:
            args = {}

            def get_json(self):
                return {}

        def jsonify(*a, **k):
            return {"__jsonify__": True}

        quart.jsonify = jsonify
        quart.request = _Request()
        sys.modules["quart"] = quart


_install_astrbot_stubs()

# ==================== 纯逻辑：时间运行语义（P1 约定） ====================

from time_awareness.domain.schedule import (  # noqa: E402
    ScheduleValidationError,
    detect_static_overlaps,
    find_active_static_slot,
    format_time,
    minute_of_day,
    normalize_static_slots,
    parse_time,
    split_cross_midnight,
)
from time_awareness.domain.timeline import (  # noqa: E402
    advance_executed,
    clip_slots,
    merge_future_layers,
    render_effective_timeline,
    subtract_slots,
)


def _m(value):
    return parse_time(str(value), allow_24=True, strict=True)


# ---- 午夜边界 = 00:00；24:00 只作右开端点 ----


def test_minute_of_day_midnight_is_zero_and_never_reaches_1440():
    assert minute_of_day(datetime.datetime(2026, 8, 11, 0, 0)) == 0
    assert minute_of_day(datetime.datetime(2026, 8, 11, 23, 59)) == 1439
    assert parse_time("24:00", allow_24=True) == 1440
    assert format_time(1440) == "24:00"
    assert format_time(0) == "00:00"


def test_split_cross_midnight_semantics():
    # 跨午夜拆两段
    assert split_cross_midnight(22 * 60, 8 * 60) == ((1320, 1440), (0, 480))
    # end=24:00 是合法右开边界 → 单段
    assert split_cross_midnight(8 * 60, 24 * 60) == ((480, 1440),)
    # 全天 [00:00, 24:00)
    assert split_cross_midnight(0, 24 * 60) == ((0, 1440),)
    # 零长度 → 空
    assert split_cross_midnight(0, 0) == ()
    # 24:00 不可作为起点（不可命中）
    with pytest.raises(ScheduleValidationError):
        split_cross_midnight(24 * 60, 24 * 60)


def test_static_hit_boundaries_23_59_00_00_and_24_00():
    night = {"name": "夜间", "start_time": "22:00", "end_time": "08:00"}
    day = {"name": "日间", "start_time": "08:00", "end_time": "24:00"}

    def at(hour, minute):
        return datetime.datetime(2026, 8, 11, hour, minute)

    # 跨午夜段：23:59 / 00:00 / 07:59 命中；08:00（右开）不命中
    assert find_active_static_slot([night], at(23, 59))["name"] == "夜间"
    assert find_active_static_slot([night], at(0, 0))["name"] == "夜间"
    assert find_active_static_slot([night], at(7, 59))["name"] == "夜间"
    assert find_active_static_slot([night], at(8, 0)) is None
    # end=24:00：23:59 命中；00:00 不命中
    assert find_active_static_slot([day], at(23, 59))["name"] == "日间"
    assert find_active_static_slot([day], at(0, 0)) is None
    # 23:59 + 24:00 结束：59 分内仍命中
    assert find_active_static_slot([day], at(23, 59)) is not None


def test_normalize_static_slots_splits_cross_midnight_at_storage_boundary():
    slots = normalize_static_slots(
        [{"name": "夜间", "start_time": "22:00", "end_time": "08:00"}]
    )
    assert [
        (s["start"], s["end"], s["_start_minute"], s["_end_minute"])
        for s in slots
    ] == [
        ("22:00", "24:00", 1320, 1440),
        ("00:00", "08:00", 0, 480),
    ]
    # 拆分后保留业务字段
    assert slots[0]["name"] == "夜间"
    assert "start_time" not in slots[0] and "end_time" not in slots[0]
    # end=24:00 → 单段
    assert [
        (s["start"], s["end"]) for s in normalize_static_slots(
            [{"start_time": "08:00", "end_time": "24:00"}]
        )
    ] == [("08:00", "24:00")]
    # 输入兼容 start/end 形态
    assert len(normalize_static_slots([{"start": "22:00", "end": "08:00"}])) == 2
    # 非法 / 零长度 / 24:00 起点 → 跳过
    assert normalize_static_slots([{"start_time": "24:00", "end_time": "08:00"}]) == []
    assert normalize_static_slots([{"start_time": "00:00", "end_time": "00:00"}]) == []
    assert normalize_static_slots([{"start_time": "bogus", "end_time": "08:00"}]) == []


def test_overlap_detection_handles_end_24_and_cross_midnight():
    overlaps = detect_static_overlaps(
        [
            {"start_time": "08:00", "end_time": "24:00"},
            {"start_time": "23:00", "end_time": "01:00"},
        ]
    )
    assert len(overlaps) == 1
    assert overlaps[0].minute == 23 * 60
    # 边界相接（08:00 左闭 / 前段右开）不算重叠
    assert detect_static_overlaps(
        [
            {"start_time": "00:00", "end_time": "08:00"},
            {"start_time": "08:00", "end_time": "24:00"},
        ]
    ) == []


# ---- 四层 timeline：跨午夜语义一致 ----


def test_timeline_clip_and_subtract_cross_midnight():
    # clip：跨午夜段裁到日内
    base = [{"name": "夜间", "start": "22:00", "end": "08:00"}]
    clipped = clip_slots(base, 23 * 60, 24 * 60)
    assert [(_m(s["start"]), _m(s["end"])) for s in clipped] == [(1380, 1440)]
    # subtract：跨午夜被挡后产出 [22:00,23:00) 与 [01:00,08:00)
    result = subtract_slots(base, [{"name": "用户", "start": "23:00", "end": "01:00"}])
    assert sorted((_m(s["start"]), _m(s["end"])) for s in result) == [(60, 480), (1320, 1380)]


def test_timeline_merge_future_layers_cross_midnight_static():
    static = normalize_static_slots(
        [{"name": "夜间", "start_time": "22:00", "end_time": "08:00", "origin": "static"}]
    )
    merged = merge_future_layers([], [], static, ai_priority=False)
    parts = [(s["origin"], s["start"], s["end"]) for s in merged]
    assert ("static", "22:00", "24:00") in parts
    assert ("static", "00:00", "08:00") in parts
    # 两段都是同一来源
    assert all(s["origin"] == "static" for s in merged)


def test_timeline_advance_executed_cross_midnight_static_frozen():
    snapshot = {
        "executed_slots": [],
        "user_slots": [],
        "ai_slots": [],
        "static_slots": [
            {"name": "夜间", "start_time": "22:00", "end_time": "08:00", "origin": "static"}
        ],
    }
    updated = advance_executed(snapshot, cutoff=23 * 60, ai_priority=False)
    # 存储边界已拆分：两段都冻结（00:00-08:00 与 22:00-23:00）
    frozen = sorted((s["_start_minute"], s["_end_minute"]) for s in updated["executed_slots"])
    assert frozen == [(0, 480), (1320, 1380)]
    assert all(s["_source_origin"] == "static" for s in updated["executed_slots"])
    # 未来层只剩 23:00-24:00
    assert [(_m(s["start"]), _m(s["end"])) for s in updated["static_slots"]] == [(1380, 1440)]


def test_timeline_render_effective_timeline_cross_midnight_static():
    snapshot = {
        "executed_until": "00:00",
        "executed_slots": [],
        "user_slots": [],
        "ai_slots": [],
    }
    current_static = [
        {"name": "夜间", "start_time": "22:00", "end_time": "08:00", "origin": "static"}
    ]
    slots = render_effective_timeline(snapshot, cutoff=0, current_static=current_static, ai_priority=False)
    parts = [(s["origin"], s["start"], s["end"]) for s in slots]
    assert ("static", "00:00", "08:00") in parts
    assert ("static", "22:00", "24:00") in parts


# ==================== 生命周期：initialize 失败回滚 + 防双清理 ====================


class _FakeContext:
    """最小可构造插件用的 Fake Context。

    故意不提供 conversation_manager / persona_manager ——
    ``resolve_effective_persona`` 会按设计降级为无 Persona，不影响初始化。
    """

    def __init__(self):
        self.registered_apis = []
        self.send_calls = []

    def get_config(self, umo=None):
        return {"timezone": "Asia/Shanghai"}

    def register_web_api(self, path, handler, methods, description):
        self.registered_apis.append((path, methods))

    async def send_message(self, session, message):
        self.send_calls.append((session, message))
        return True


def _lifecycle_config():
    return {
        "log": {},
        "timezone": "Asia/Shanghai",
        "reminder": {
            "enable_reminder": True,
            "reminder_targets": ["aiocqhttp:GroupMessage:group_demo"],
        },
        "daily_schedule": {
            "enable_schedule": True,
            "ai_daily": {
                "enabled": True,
                "generation_time": "00:05",
                "max_slots": 24,
                "retention_days": 30,
            },
            "schedule_templates": [],
            "ai_generation": {"provider_id": "", "use_persona": True, "worldview": ""},
        },
    }


@pytest.fixture
def fake_ctx(tmp_path, monkeypatch):
    from astrbot.api.star import StarTools

    monkeypatch.setattr(
        StarTools,
        "get_data_dir",
        staticmethod(lambda name: str(tmp_path)),
    )
    return _FakeContext()


def _make_plugin(ctx):
    from time_awareness.main import TimeAwarenessPlugin

    return TimeAwarenessPlugin(ctx, _lifecycle_config())


def _assert_no_background_resources(plugin):
    assert plugin._daily_scan_task is None
    assert plugin._daily_schedule_task is None
    assert plugin.scheduler._loop_task is None
    assert plugin.daily_schedule_service._tasks == {}


def test_lifecycle_normal_initialize_terminate_no_resources(fake_ctx):
    plugin = _make_plugin(fake_ctx)

    async def run():
        await plugin.initialize()
        assert plugin.web_api is not None
        await plugin.terminate()

    asyncio.run(run())
    _assert_no_background_resources(plugin)
    assert plugin.last_chat_tracker._flush_task is None


def test_lifecycle_repeated_terminate_is_idempotent(fake_ctx):
    plugin = _make_plugin(fake_ctx)

    async def run():
        await plugin.initialize()
        await plugin.terminate()
        await plugin.terminate()

    asyncio.run(run())
    _assert_no_background_resources(plugin)
    assert plugin.last_chat_tracker._flush_task is None


def test_initialize_rollback_when_scheduler_start_fails(fake_ctx):
    plugin = _make_plugin(fake_ctx)

    def boom():
        raise RuntimeError("scheduler start fail")

    plugin.scheduler.start = boom

    with pytest.raises(RuntimeError, match="scheduler start fail"):
        asyncio.run(plugin.initialize())
    _assert_no_background_resources(plugin)


def test_initialize_rollback_when_daily_schedule_step_fails(fake_ctx):
    """scheduler + 每日扫描已启动后，AI 每日日程注册步骤失败 → 全部回滚。"""
    plugin = _make_plugin(fake_ctx)
    started_before_failure = {}

    async def boom(*args, **kwargs):
        # 失败点发生在 scheduler.start() 与每日扫描创建之后
        started_before_failure["scheduler"] = plugin.scheduler._loop_task is not None
        started_before_failure["daily_scan"] = plugin._daily_scan_task is not None
        started_before_failure["daily_schedule"] = plugin._daily_schedule_task is not None
        raise RuntimeError("daily schedule step fail")

    plugin.daily_schedule_service.register_session_async = boom

    with pytest.raises(RuntimeError, match="daily schedule step fail"):
        asyncio.run(plugin.initialize())
    # 失败前确实已启动后台资源 → 回滚不是空转
    assert started_before_failure["scheduler"] is True
    assert started_before_failure["daily_scan"] is True
    assert started_before_failure["daily_schedule"] is False  # 该步尚未创建
    _assert_no_background_resources(plugin)


def test_initialize_rollback_when_web_api_registration_fails(fake_ctx, monkeypatch):
    """所有后台资源（scheduler/每日扫描/日程循环）都已启动后，Web API 失败 → 全部回滚。"""
    from time_awareness import main as main_mod

    plugin = _make_plugin(fake_ctx)
    started_before_failure = {}

    def boom(*args, **kwargs):
        # Web API 是最后一步：此前全部后台资源都应已启动
        started_before_failure["scheduler"] = plugin.scheduler._loop_task is not None
        started_before_failure["daily_scan"] = plugin._daily_scan_task is not None
        started_before_failure["daily_schedule"] = plugin._daily_schedule_task is not None
        raise RuntimeError("web api fail")

    monkeypatch.setattr(main_mod, "register_web_apis", boom)

    with pytest.raises(RuntimeError, match="web api fail"):
        asyncio.run(plugin.initialize())
    assert started_before_failure == {
        "scheduler": True,
        "daily_scan": True,
        "daily_schedule": True,
    }
    _assert_no_background_resources(plugin)
    assert plugin.web_api is None
