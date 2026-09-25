"""time_awareness Web API：向 AstrBot 注册插件 REST API，统一信封 {success, ...}。"""

import os
from datetime import date
from functools import wraps

import yaml
from quart import jsonify, request

from astrbot.api import logger

from .log import tag
from .services.daily_schedule_admin_service import (
    ScheduleNotFound,
    SchedulePersistenceError,
    ScheduleSaveConflict,
)
from .services.static_schedule_config_service import (
    StaticScheduleConfigConflict,
    StaticScheduleConfigPersistenceError,
)
from .utils.time_utils import get_now


PLUGIN_NAME = "time_awareness"


def _plugin_root() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _read_metadata() -> dict:
    path = os.path.join(_plugin_root(), "metadata.yaml")
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


async def _get_json_body():
    """读取 POST JSON 请求体；非法 JSON 返回 None（避免误报 500）。"""
    try:
        return await request.get_json()
    except Exception:
        return None


def _ok(**data):
    return jsonify({"success": True, **data})


def _err(msg: str, status: int = 400):
    logger.warning(f"{tag()} ⚠️ Web API 返回 {status}: {msg}")
    return jsonify({"success": False, "error": msg}), status


def _internal_error(e: Exception):
    logger.error(f"{tag()} ❌ Web API 内部错误: {e}")
    return jsonify({"success": False, "error": "服务器内部错误"}), 500


def _guarded(func):
    """统一兜底：捕获未处理异常并返回 500 信封（等价于旧的 try/except 外壳）。"""

    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as exc:
            return _internal_error(exc)

    return wrapper


def _schedule_admin_error(exc, persist_msg: str):
    if isinstance(exc, ScheduleNotFound):
        return _err("日程快照不存在或已被清理，请刷新", 404)
    if isinstance(exc, ScheduleSaveConflict):
        return _err("日程已被重新生成或由其他页面修改，请刷新", 409)
    if isinstance(exc, SchedulePersistenceError):
        logger.error(f"{tag()} ❌ 日程快照持久化失败: {exc}")
        return _err(persist_msg, 500)
    return _err(str(exc) or "校验失败")


def _safe_group_config(config, group_key: str) -> dict:
    if not isinstance(config, dict):
        return {}
    sub = config.get(group_key, {})
    return sub if isinstance(sub, dict) else {}


class TimeAwarenessWebApi:
    def __init__(self, context, plugin):
        self.context = context
        self.plugin = plugin
        self.config = plugin.config
        self.calendar_store = plugin.calendar_store
        self.schedule_admin = plugin.daily_schedule_admin
        self.static_schedule_config = getattr(plugin, "static_schedule_config", None)

    @_guarded
    async def get_about(self):
        meta = _read_metadata()
        return _ok(
            name=str(meta.get("name", PLUGIN_NAME)),
            version=str(meta.get("version", "")),
            display_name=str(meta.get("display_name", "")),
            author=str(meta.get("author", "")),
        )

    @_guarded
    async def get_dashboard_stats(self):
        now = get_now(self.config, self.plugin._astrbot_config())
        month_events = self.calendar_store.events_for_month(
            now.year,
            now.month,
            include_builtin=True,
        )
        try:
            time_info = self.plugin.time_context.build_dashboard_time_info()
        except Exception as exc:
            logger.warning(f"{tag()} ⚠️ build_dashboard_time_info 失败: {exc}")
            time_info = {}
        stats = {
            "custom_event_total": len(self.calendar_store.events),
            "builtin_event_total": len(self.calendar_store.builtin_events),
            "this_month_event_count": len(month_events),
            "calendar_enabled": bool(
                _safe_group_config(self.config, "calendar").get("enable_calendar", False)
            ),
            "now_iso": time_info.get("now_iso", ""),
            "now_display": time_info.get("now_display", ""),
            "time_display": time_info.get("time_display", ""),
            "tz_gmt": time_info.get("tz_gmt", ""),
            "date_display": time_info.get("date_display", ""),
            "greeting": time_info.get("greeting", ""),
            "weekday_display": time_info.get("weekday_display", ""),
            "workday_kind": time_info.get("workday_kind", "unknown"),
            "workday_label": time_info.get("workday_label", ""),
            "lunar_display": time_info.get("lunar_display", ""),
            "lunar_year_chip": time_info.get("lunar_year_chip", ""),
            "lunar_month_day": time_info.get("lunar_month_day", ""),
            "almanac_display": time_info.get("almanac_display", ""),
            "almanac_gz_day": time_info.get("almanac_gz_day", ""),
            "almanac_chong_sha": time_info.get("almanac_chong_sha", ""),
            "almanac_yi": time_info.get("almanac_yi", ""),
            "almanac_ji": time_info.get("almanac_ji", ""),
            "lunar_enabled": time_info.get("lunar_enabled", False),
            "almanac_enabled": time_info.get("almanac_enabled", False),
        }
        return _ok(stats=stats)

    @_guarded
    async def get_calendar_month(self):
        try:
            year = int((request.args.get("year") or "").strip())
            month = int((request.args.get("month") or "").strip())
            if not (1 <= month <= 12 and 1970 <= year <= 9999):
                raise ValueError("out of range")
        except (ValueError, TypeError):
            return _err("year/month 参数无效（year=1970-9999, month=1-12）", 400)
        calendar_runtime = getattr(self.plugin, "calendar_runtime", None)
        if calendar_runtime is not None:
            calendar_runtime.ensure_builtin_fresh()
        enabled_categories = set(
            self.plugin.time_context.enabled_builtin_categories()
        )
        events = self.calendar_store.events_for_month(
            year,
            month,
            include_builtin=bool(enabled_categories),
        )
        custom = [event for event in events if event.get("source") != "builtin"]
        builtin = [
            event
            for event in events
            if event.get("source") == "builtin"
            and event.get("category") in enabled_categories
        ]
        return _ok(year=year, month=month, builtin=builtin, custom=custom)

    @_guarded
    async def get_schedules_personas(self):
        personas = await self.schedule_admin.list_personas()
        return _ok(personas=personas)

    @_guarded
    async def get_schedules_detail(self):
        persona_hash = (request.args.get("persona_hash") or "").strip()
        date_text = (request.args.get("date") or "").strip()
        timezone = (request.args.get("timezone") or "").strip()
        if not persona_hash or not date_text:
            return _err("缺少 persona_hash 或 date 参数")
        try:
            local_date = date.fromisoformat(date_text)
        except ValueError:
            return _err("date 格式必须为 YYYY-MM-DD")
        detail = self.schedule_admin.get_detail(persona_hash, local_date, timezone)
        if detail is None:
            return _err("日程快照不存在或已被清理，请刷新", 404)
        return _ok(detail=detail)

    @_guarded
    async def save_schedule(self):
        body = await _get_json_body()
        if not isinstance(body, dict):
            return _err("请求体必须是 JSON 对象")
        persona_hash = str(body.get("persona_hash") or "").strip()
        date_text = str(body.get("date") or "").strip()
        timezone = str(body.get("timezone") or "").strip()
        snapshot_id = str(body.get("snapshot_id") or "").strip()
        slots = body.get("user_slots")
        if not persona_hash or not date_text or not snapshot_id:
            return _err("缺少 persona_hash/date/snapshot_id")
        if not isinstance(slots, list):
            return _err("user_slots 必须是数组")
        try:
            local_date = date.fromisoformat(date_text)
        except ValueError:
            return _err("date 格式必须为 YYYY-MM-DD")
        try:
            result = await self.schedule_admin.save(
                persona_hash=persona_hash,
                local_date=local_date,
                timezone=timezone,
                snapshot_id=snapshot_id,
                slots=slots,
            )
        except (
            ScheduleNotFound,
            ScheduleSaveConflict,
            SchedulePersistenceError,
            ValueError,
        ) as exc:
            # 服务端故障（写盘失败）等错误统一映射，消息由 helper 按异常类型区分
            return _schedule_admin_error(exc, "服务器保存失败，请重试")
        logger.info(
            f"{tag()} 🖊️ WebUI 保存人格日程: persona_hash={persona_hash[:8]}… "
            f"date={date_text} user_slots={len(slots)} snapshot_id={snapshot_id}"
        )
        return _ok(**result)

    @_guarded
    async def get_static_schedules(self):
        if self.static_schedule_config is None:
            return _err("静态日程配置服务不可用", 503)
        return _ok(**self.static_schedule_config.get())

    @_guarded
    async def save_static_schedules(self):
        body = await _get_json_body()
        if not isinstance(body, dict):
            return _err("请求体必须是 JSON 对象")
        revision = str(body.get("revision") or "").strip()
        slots = body.get("slots")
        if not revision:
            return _err("缺少 revision")
        if not isinstance(slots, list):
            return _err("slots 必须是数组")
        if self.static_schedule_config is None:
            return _err("静态日程配置服务不可用", 503)
        try:
            result = await self.static_schedule_config.save(
                revision=revision,
                slots=slots,
            )
        except StaticScheduleConfigConflict:
            return _err("静态日程已被其他页面修改，请刷新", 409)
        except StaticScheduleConfigPersistenceError as exc:
            logger.error(f"{tag()} ❌ 静态日程配置持久化失败: {exc}")
            return _err("静态日程配置保存失败，请重试", 500)
        except (TypeError, ValueError) as exc:
            return _err(str(exc) or "校验失败")
        logger.info(
            f"{tag()} 🧱 WebUI 保存静态日程: slots={len(slots)} revision={revision}"
        )
        return _ok(**result)

    async def weather_test(self):
        sensor = getattr(self.plugin, "weather_sensor", None)
        if sensor is None or not sensor.enabled():
            return _err("随机天气未启用：请在主配置页开启 daily_schedule.ai_daily.random_weather_enabled")
        try:
            ok, message = await sensor.validate()
        except Exception as exc:
            return _err(f"查询失败: {type(exc).__name__}: {exc}")
        if not ok:
            return _err(message)
        current = await sensor.current_weather()
        return _ok(
            ok=True,
            message=message,
            weather={
                "text_day": str(current.get("text_day", "") or ""),
                "text_night": str(current.get("text_night", "") or ""),
                "temp_min": current.get("temp_min"),
                "temp_max": current.get("temp_max"),
                "variation": str(current.get("variation", "") or ""),
            },
        )

    def register(self) -> None:
        endpoints = (
            ("about", self.get_about, ["GET"], "获取插件版本信息"),
            ("schedules/personas", self.get_schedules_personas, ["GET"], "获取有快照的 Persona 列表"),
            ("schedules/detail", self.get_schedules_detail, ["GET"], "获取某日快照详情（含编辑权限）"),
            ("schedules/save", self.save_schedule, ["POST"], "保存人格日程整日草稿"),
            ("static-schedules", self.get_static_schedules, ["GET"], "获取静态日程配置"),
            ("static-schedules/save", self.save_static_schedules, ["POST"], "保存静态日程配置"),
            ("dashboard/stats", self.get_dashboard_stats, ["GET"], "获取概览统计"),
            ("calendar/month", self.get_calendar_month, ["GET"], "获取月视图事件"),
            ("weather/test", self.weather_test, ["GET"], "查看今日随机天气"),
        )
        for path, handler, methods, description in endpoints:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/{path}",
                handler,
                methods,
                description,
            )
        logger.info(f"{tag()} ✅ Web API 已注册（共 {len(endpoints)} 个端点）")


def register_web_apis(context, plugin) -> TimeAwarenessWebApi:
    controller = TimeAwarenessWebApi(context, plugin)
    controller.register()
    return controller
