"""web_api.py 的路由注册与错误分支测试（quart 桩）。"""

import asyncio

from time_awareness.tests._shared import FakeContext, install_astrbot_stubs

install_astrbot_stubs()

import time_awareness.web_api as wapi  # noqa: E402
from time_awareness.services.daily_schedule_admin_service import ScheduleNotFound  # noqa: E402
from time_awareness.web_api import TimeAwarenessWebApi, register_web_apis  # noqa: E402

_EXPECTED = {
    ("/time_awareness/about", "GET"),
    ("/time_awareness/schedules/personas", "GET"),
    ("/time_awareness/schedules/detail", "GET"),
    ("/time_awareness/schedules/save", "POST"),
    ("/time_awareness/static-schedules", "GET"),
    ("/time_awareness/static-schedules/save", "POST"),
    ("/time_awareness/dashboard/stats", "GET"),
    ("/time_awareness/calendar/month", "GET"),
    ("/time_awareness/weather/test", "GET"),
}


class _FakePlugin:
    def __init__(self, schedule_admin=None):
        self.config = {}
        self.calendar_store = None
        self.daily_schedule_admin = schedule_admin
        self.static_schedule_config = None
        self.calendar_runtime = None


def _controller(plugin=None):
    ctx = FakeContext()
    return ctx, TimeAwarenessWebApi(ctx, plugin or _FakePlugin())


def test_all_endpoints_registered():
    ctx = FakeContext()
    register_web_apis(ctx, _FakePlugin())
    registered = {(p, m) for p, _, methods, _ in ctx.web_routes for m in methods}
    assert registered == _EXPECTED and len(ctx.web_routes) == 9


def test_get_about_returns_metadata():
    resp = asyncio.run(_controller()[1].get_about())
    assert resp == {
        "success": True,
        "name": "time_awareness",
        "version": "v2.3.0",
        "display_name": "TimeAwareness",
        "author": "Wolfycz",
    }


def test_get_calendar_month_rejects_bad(monkeypatch):
    controller = _controller()[1]
    monkeypatch.setattr(wapi.request, "args", {"year": "2026", "month": "13"})
    resp, status = asyncio.run(controller.get_calendar_month())
    assert status == 400 and resp["success"] is False


def test_save_schedule_maps_not_found(monkeypatch):
    class _RaisingAdmin:
        async def save(self, **kwargs):
            raise ScheduleNotFound("gone")

    controller = _controller(_FakePlugin(schedule_admin=_RaisingAdmin()))[1]

    async def body():
        return {"persona_hash": "ph", "date": "2026-08-11", "snapshot_id": "snap", "user_slots": []}

    monkeypatch.setattr(wapi.request, "get_json", body)
    resp, status = asyncio.run(controller.save_schedule())
    assert status == 404 and "日程快照不存在或已被清理" in resp["error"]
