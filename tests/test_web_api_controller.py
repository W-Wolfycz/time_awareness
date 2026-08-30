import importlib
import asyncio
import sys
import types

from time_awareness.core.calendar_store import CalendarStore


def test_web_api_registration_uses_controller_bound_methods(monkeypatch):
    quart = types.ModuleType("quart")
    quart.jsonify = lambda value: value
    quart.request = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "quart", quart)
    sys.modules.pop("time_awareness.web_api", None)
    module = importlib.import_module("time_awareness.web_api")

    class Context:
        def __init__(self):
            self.routes = []

        def register_web_api(self, path, handler, methods, description):
            self.routes.append((path, handler, methods, description))

    plugin = types.SimpleNamespace(
        scheduler=object(),
        config={},
        calendar_store=CalendarStore(),
        daily_schedule_admin=object(),
    )
    context = Context()
    controller = module.register_web_apis(context, plugin)

    assert len(context.routes) == 15
    assert all(route[1].__self__ is controller for route in context.routes)
    assert {route[0] for route in context.routes} == {
        "/time_awareness/about",
        "/time_awareness/dashboard/stats",
        "/time_awareness/calendar/month",
        "/time_awareness/tasks/list",
        "/time_awareness/tasks/options",
        "/time_awareness/tasks/create",
        "/time_awareness/tasks/cancel",
        "/time_awareness/config/schema",
        "/time_awareness/schedules/personas",
        "/time_awareness/schedules/detail",
        "/time_awareness/schedules/save",
        "/time_awareness/schedules/delete",
        "/time_awareness/static-schedules",
        "/time_awareness/static-schedules/save",
        "/time_awareness/weather/test",
    }


def test_schedule_detail_parses_iso_date_with_runtime_datetime_import(monkeypatch):
    """详情 API 在真实运行时应使用 date.fromisoformat，而不是 datetime.date。"""
    quart = types.ModuleType("quart")
    quart.jsonify = lambda value: value
    quart.request = types.SimpleNamespace(
        args={
            "persona_hash": "persona_demo",
            "date": "2026-08-06",
            "timezone": "Asia/Shanghai",
        }
    )
    monkeypatch.setitem(sys.modules, "quart", quart)
    sys.modules.pop("time_awareness.web_api", None)
    module = importlib.import_module("time_awareness.web_api")

    class ScheduleAdmin:
        def get_detail(self, persona_hash, local_date, timezone):
            assert persona_hash == "persona_demo"
            assert local_date.isoformat() == "2026-08-06"
            assert timezone == "Asia/Shanghai"
            return {"snapshot_id": "snapshot_demo"}

    plugin = types.SimpleNamespace(
        scheduler=object(),
        config={},
        calendar_store=CalendarStore(),
        daily_schedule_admin=ScheduleAdmin(),
    )
    controller = module.TimeAwarenessWebApi(None, plugin)
    response = asyncio.run(controller.get_schedules_detail())
    assert response == {"success": True, "detail": {"snapshot_id": "snapshot_demo"}}


def test_schedule_detail_passthroughs_user_slot_id(monkeypatch):
    """详情 API 原样透传 user_slot_id，前端保存才能回传稳定引用。"""
    quart = types.ModuleType("quart")
    quart.jsonify = lambda value: value
    quart.request = types.SimpleNamespace(
        args={
            "persona_hash": "persona_demo",
            "date": "2026-08-06",
            "timezone": "Asia/Shanghai",
        }
    )
    monkeypatch.setitem(sys.modules, "quart", quart)
    sys.modules.pop("time_awareness.web_api", None)
    module = importlib.import_module("time_awareness.web_api")

    class ScheduleAdmin:
        def get_detail(self, persona_hash, local_date, timezone):
            return {
                "snapshot_id": "snapshot_demo",
                "slots": [
                    {"origin": "user", "user_slot_id": "u-abc",
                     "start": "10:01", "end": "12:00"},
                    {"origin": "ai", "user_slot_id": "",
                     "start": "13:00", "end": "14:00"},
                ],
            }

    plugin = types.SimpleNamespace(
        scheduler=object(),
        config={},
        calendar_store=CalendarStore(),
        daily_schedule_admin=ScheduleAdmin(),
    )
    controller = module.TimeAwarenessWebApi(None, plugin)
    response = asyncio.run(controller.get_schedules_detail())
    slots = response["detail"]["slots"]
    user_slots = [s for s in slots if s["origin"] == "user"]
    assert user_slots[0]["user_slot_id"] == "u-abc"
    assert all(s["user_slot_id"] == "" for s in slots if s["origin"] != "user")


def test_schedule_save_forwards_user_slot_id_to_admin(monkeypatch):
    """保存 API 接收 user_slots 并把 user_slot_id 原样转发给 AdminService。"""
    quart = types.ModuleType("quart")
    quart.jsonify = lambda value: value

    class _Request:
        async def get_json(self):
            return {
                "persona_hash": "persona_demo",
                "date": "2026-08-06",
                "timezone": "Asia/Shanghai",
                "snapshot_id": "snap-1",
                "user_slots": [
                    {"user_slot_id": "u-abc", "name": "用户安排",
                     "start": "10:01", "end": "13:00", "state": "待办"},
                ],
            }

    quart.request = _Request()
    monkeypatch.setitem(sys.modules, "quart", quart)
    sys.modules.pop("time_awareness.web_api", None)
    module = importlib.import_module("time_awareness.web_api")

    captured = {}

    class ScheduleAdmin:
        async def save(self, **kwargs):
            captured.update(kwargs)
            return {"snapshot_id": "snap-2"}

    plugin = types.SimpleNamespace(
        scheduler=object(),
        config={},
        calendar_store=CalendarStore(),
        daily_schedule_admin=ScheduleAdmin(),
    )
    controller = module.TimeAwarenessWebApi(None, plugin)
    response = asyncio.run(controller.save_schedule())
    assert response == {"success": True, "snapshot_id": "snap-2"}
    # AdminService 收到完整用户层（含 user_slot_id），用于稳定引用定位
    assert captured["persona_hash"] == "persona_demo"
    assert captured["local_date"].isoformat() == "2026-08-06"
    assert captured["snapshot_id"] == "snap-1"
    assert captured["slots"][0]["user_slot_id"] == "u-abc"
    assert captured["slots"][0]["start"] == "10:01"


def _load_module(monkeypatch, request_args=None, request_body=None):
    """加载 web_api 模块（fake quart 支持 args 与 get_json）。"""
    quart = types.ModuleType("quart")

    class _Request:
        def __init__(self):
            self.args = request_args or {}
            self._body = request_body

        async def get_json(self):
            return self._body

    quart.jsonify = lambda value: value
    quart.request = _Request()
    monkeypatch.setitem(sys.modules, "quart", quart)
    sys.modules.pop("time_awareness.web_api", None)
    return importlib.import_module("time_awareness.web_api")


def _make_controller(module, schedule_admin=None):
    plugin = types.SimpleNamespace(
        scheduler=object(),
        config={},
        calendar_store=CalendarStore(),
        daily_schedule_admin=schedule_admin or object(),
    )
    return module.TimeAwarenessWebApi(None, plugin)


def test_delete_schedule_slot_forwards_and_maps_errors(monkeypatch):
    """schedules/delete 端点：正常转发 + 404/409/500 错误映射。"""
    module = _load_module(
        monkeypatch,
        request_body={
            "persona_hash": "persona_demo",
            "date": "2026-08-06",
            "timezone": "UTC",
            "snapshot_id": "snap-1",
            "user_slot_id": "u1",
        },
    )

    class ScheduleAdmin:
        async def delete_user_slot(self, **kwargs):
            assert kwargs["persona_hash"] == "persona_demo"
            assert kwargs["user_slot_id"] == "u1"
            return {"snapshot_id": "snap-2"}

    controller = _make_controller(module, ScheduleAdmin())
    response = asyncio.run(controller.delete_schedule_slot())
    assert response == {"success": True, "snapshot_id": "snap-2"}

    # 404：快照不存在
    class NotFoundAdmin:
        async def delete_user_slot(self, **kwargs):
            raise module.ScheduleNotFound("不存在")

    controller = _make_controller(module, NotFoundAdmin())
    response = asyncio.run(controller.delete_schedule_slot())
    assert response[0]["success"] is False and response[1] == 404

    # 409：snapshot_id 冲突
    class ConflictAdmin:
        async def delete_user_slot(self, **kwargs):
            raise module.ScheduleSaveConflict("冲突")

    controller = _make_controller(module, ConflictAdmin())
    response = asyncio.run(controller.delete_schedule_slot())
    assert response[1] == 409

    # 500：持久化失败（服务端故障）
    class PersistAdmin:
        async def delete_user_slot(self, **kwargs):
            raise module.SchedulePersistenceError("写盘失败")

    controller = _make_controller(module, PersistAdmin())
    response = asyncio.run(controller.delete_schedule_slot())
    assert response[1] == 500

    # 400：校验失败（user_slot_id 不存在）
    class InvalidAdmin:
        async def delete_user_slot(self, **kwargs):
            raise ValueError("user_slot_id 未找到")

    controller = _make_controller(module, InvalidAdmin())
    response = asyncio.run(controller.delete_schedule_slot())
    assert response[1] == 400


def test_delete_schedule_slot_rejects_bad_request(monkeypatch):
    """缺参 / 非法日期 / 非法 JSON 一律 400。"""
    module = _load_module(
        monkeypatch,
        request_body={"persona_hash": "persona_demo"},  # 缺其余参数
    )
    controller = _make_controller(module)
    response = asyncio.run(controller.delete_schedule_slot())
    assert response[1] == 400

    # 非法 JSON（get_json 抛 BadRequest 类异常 → None → 400）
    module2 = _load_module(monkeypatch, request_body=None)
    controller2 = _make_controller(module2)
    response2 = asyncio.run(controller2.delete_schedule_slot())
    assert response2[1] == 400
