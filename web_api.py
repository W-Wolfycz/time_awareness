"""
time_awareness Web API

向 AstrBot 注册插件 REST API，供 Plugin Pages 调用。

端点清单：
- GET  /time_awareness/about            版本号
- GET  /time_awareness/dashboard/stats  概览统计
- GET  /time_awareness/calendar/month   月视图事件（builtin + custom 分组）
- GET  /time_awareness/tasks/list       pending 任务列表
- POST /time_awareness/tasks/cancel     取消任务
- GET  /time_awareness/tasks/options    新增任务选项
- POST /time_awareness/tasks/create     新增手动任务
- GET  /time_awareness/config/schema    配置只读（schema 元信息 + 当前值）

统一响应信封：``{success: bool, ...data | error: str}``
"""

import json
import os
from datetime import datetime, timedelta

import yaml
from quart import jsonify, request

from astrbot.api import logger

from .log import tag
from .utils.time_utils import coerce_to_aware, get_now, get_tz


PLUGIN_NAME = "time_awareness"

_CONF_SCHEMA_CACHE: dict | None = None


# ==================== 静态资源读取 ====================

def _plugin_root() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _read_conf_schema() -> dict:
    """读取并缓存 _conf_schema.json。"""
    global _CONF_SCHEMA_CACHE
    if _CONF_SCHEMA_CACHE is None:
        path = os.path.join(_plugin_root(), "_conf_schema.json")
        try:
            with open(path, encoding="utf-8") as f:
                _CONF_SCHEMA_CACHE = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"{tag()} ⚠️ 读取 _conf_schema.json 失败: {e}")
            _CONF_SCHEMA_CACHE = {}
    return _CONF_SCHEMA_CACHE


def _read_metadata() -> dict:
    path = os.path.join(_plugin_root(), "metadata.yaml")
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


# ==================== 响应助手 ====================

def _ok(**data):
    return jsonify({"success": True, **data})


def _err(msg: str, status: int = 400):
    logger.warning(f"{tag()} ⚠️ Web API 返回 {status}: {msg}")
    return jsonify({"success": False, "error": msg}), status


def _internal_error(e: Exception):
    logger.error(f"{tag()} ❌ Web API 内部错误: {e}")
    return jsonify({"success": False, "error": "服务器内部错误"}), 500


def _safe_group_config(config, group_key: str) -> dict:
    """从 plugin.config 取分组字典（容错：config 不一定是 dict 或分组缺失）。"""
    if not isinstance(config, dict):
        return {}
    sub = config.get(group_key, {})
    return sub if isinstance(sub, dict) else {}


def _relative_time(delta_sec: float) -> str:
    """把秒差转为人类可读的相对时间（用于「下个任务 X 后」）。"""
    if delta_sec < 60:
        return "即将"
    if delta_sec < 3600:
        return f"{int(delta_sec // 60)} 分钟后"
    if delta_sec < 86400:
        return f"{int(delta_sec // 3600)} 小时后"
    return f"{int(delta_sec // 86400)} 天后"


# ==================== 注册入口 ====================

def register_web_apis(context, plugin) -> None:
    """注册所有 Web API。

    plugin 需暴露：
    - plugin.scheduler: ReminderScheduler 实例
    - plugin.config: AstrBotConfig
    - plugin._astrbot_config(): AstrBot 主配置（用于时区解析）

    鉴权继承 AstrBot 主 webui 登录态，本插件不额外处理。
    """
    scheduler = plugin.scheduler
    config = plugin.config
    calendar_store = plugin.calendar_store

    # ==================== 端点：about ====================

    async def get_about():
        try:
            meta = _read_metadata()
            return _ok(
                name=str(meta.get("name", PLUGIN_NAME)),
                version=str(meta.get("version", "")),
                display_name=str(meta.get("display_name", "")),
                author=str(meta.get("author", "")),
            )
        except Exception as e:
            return _internal_error(e)

    # ==================== 端点：dashboard/stats ====================

    async def get_dashboard_stats():
        try:
            now = get_now(plugin.config, plugin._astrbot_config())
            pending = scheduler.all_pending()
            failed = scheduler.all_failed()

            week_cutoff = now + timedelta(days=7)
            # 任务 fire_at 与 now 可能 aware/naive 不一致（用户中途改时区配置）
            # 用 coerce_to_aware 规整后再比较
            near_7d = sum(
                1 for t in pending
                if coerce_to_aware(t.due_at(), now) <= week_cutoff
            )

            next_task_display = "无"
            if pending:
                next_t = min(pending, key=lambda t: coerce_to_aware(t.due_at(), now))
                delta_sec = (coerce_to_aware(next_t.due_at(), now) - now).total_seconds()
                if delta_sec < 0:
                    next_task_display = "已到期"
                else:
                    next_task_display = _relative_time(delta_sec)

            month_events = calendar_store.events_for_month(
                now.year, now.month, include_builtin=True
            )

            # 时间感知面板数据（hero card 用）
            # 失败时降级为空字典，前端按字段缺失处理
            try:
                time_info = plugin.build_dashboard_time_info()
            except Exception as e:
                logger.warning(f"{tag()} ⚠️ build_dashboard_time_info 失败: {e}")
                time_info = {}

            stats = {
                "custom_event_total": len(calendar_store.events),
                "builtin_event_total": len(calendar_store.builtin_events),
                "task_pending_total": len(pending),
                "task_calendar_pending": sum(1 for t in pending if t.kind == "calendar"),
                "task_followup_pending": sum(1 for t in pending if t.kind == "followup"),
                "task_user_pending": sum(1 for t in pending if t.kind == "user"),
                "task_retry_wait": sum(1 for t in pending if t.status == "retry_wait"),
                "task_failed_total": len(failed),
                "this_month_event_count": len(month_events),
                "near_7d_task_count": near_7d,
                "reminder_enabled": bool(_safe_group_config(config, "reminder").get("enable_reminder", False)),
                "calendar_enabled": bool(_safe_group_config(config, "calendar").get("enable_calendar", False)),
                "next_task_display": next_task_display,
                # 时间感知面板字段（v1.7.0 新增）
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
        except Exception as e:
            return _internal_error(e)

    # ==================== 端点：calendar/month ====================

    async def get_calendar_month():
        try:
            year_str = (request.args.get("year") or "").strip()
            month_str = (request.args.get("month") or "").strip()
            try:
                year = int(year_str)
                month = int(month_str)
                if not (1 <= month <= 12) or not (1970 <= year <= 9999):
                    raise ValueError("out of range")
            except (ValueError, TypeError):
                return _err("year/month 参数无效（year=1970-9999, month=1-12）", 400)

            custom = calendar_store.events_for_month(year, month, include_builtin=False)
            builtin = [
                e for e in calendar_store.builtin_events
                if e.get("year") == year and e.get("month") == month
            ]
            return _ok(year=year, month=month, builtin=builtin, custom=custom)
        except Exception as e:
            return _internal_error(e)

    # ==================== 端点：tasks/list ====================

    async def get_tasks_list():
        try:
            tasks = scheduler.list_pending_detailed(
                display_tz=get_tz(plugin.config, plugin._astrbot_config())
            )
            return _ok(tasks=tasks, total=len(tasks))
        except Exception as e:
            return _internal_error(e)

    # ==================== 端点：tasks/cancel ====================

    async def cancel_task():
        try:
            data = await request.get_json()
            task_id = str((data or {}).get("task_id", "")).strip()
            if not task_id:
                return _err("task_id 不能为空", 400)
            ok = scheduler.cancel_task(task_id)
            if not ok:
                return _err("任务未找到或已处理", 404)
            return _ok(cancelled=True, task_id=task_id)
        except Exception as e:
            return _internal_error(e)

    # ==================== 端点：tasks/options ====================

    async def get_tasks_options():
        try:
            sessions = plugin._reminder_targets()
            now = get_now(plugin.config, plugin._astrbot_config())
            tz = get_tz(plugin.config, plugin._astrbot_config())
            tz_label = str(tz) if tz is not None else "系统本地（未配置时区）"
            # datetime-local 默认值：当前时间 + 10 分钟（naive 截到分钟）
            default_local = (now + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M")
            return _ok(
                sessions=sessions,
                now_iso=now.isoformat(),
                tz_label=tz_label,
                default_fire_at_local=default_local,
            )
        except Exception as e:
            return _internal_error(e)

    # ==================== 端点：tasks/create ====================

    async def create_task():
        try:
            data = await request.get_json() or {}
            session = str(data.get("session", "")).strip()
            fire_at_local = str(data.get("fire_at_local", "")).strip()
            hint = str(data.get("hint", "")).strip()
            target_user_id = str(data.get("target_user_id", "")).strip()

            # datetime-local 格式：YYYY-MM-DDTHH:MM
            try:
                naive = datetime.fromisoformat(fire_at_local)
            except ValueError:
                return _err("fire_at_local 格式错误，需 YYYY-MM-DDTHH:MM", 400)

            # 时区 localize（tz=None 时保持 naive，与系统 now 同源）
            tz = get_tz(plugin.config, plugin._astrbot_config())
            fire_at = naive.replace(tzinfo=tz) if tz is not None else naive

            try:
                task_id = await plugin.create_external_task(
                    session=session,
                    fire_at=fire_at,
                    hint=hint,
                    target_user_id=target_user_id,
                    source="webui",
                )
            except ValueError as e:
                return _err(str(e), 400)
            logger.info(
                f"{tag()} 🧑 WebUI 创建手动提醒: id={task_id[:8]} session={session} "
                f"fire_at={fire_at.isoformat()}"
            )
            return _ok(task_id=task_id, fire_at_iso=fire_at.isoformat())
        except Exception as e:
            return _internal_error(e)

    # ==================== 端点：config/schema（只读） ====================

    async def get_config_schema():
        try:
            schema = _read_conf_schema()
            groups = []
            for group_key, group_def in schema.items():
                if not isinstance(group_def, dict):
                    continue
                items = group_def.get("items", {})
                if not isinstance(items, dict):
                    continue
                group_config = _safe_group_config(config, group_key)
                fields = []
                for field_key, field_def in items.items():
                    if not isinstance(field_def, dict):
                        continue
                    default = field_def.get("default")
                    value = group_config.get(field_key, default)
                    fields.append({
                        "key": field_key,
                        "type": field_def.get("type", ""),
                        "description": field_def.get("description", ""),
                        "hint": field_def.get("hint", ""),
                        "default": default,
                        "value": value,
                    })
                groups.append({
                    "key": group_key,
                    "description": group_def.get("description", ""),
                    "fields": fields,
                })
            return _ok(groups=groups)
        except Exception as e:
            return _internal_error(e)

    # ==================== 注册 ====================

    context.register_web_api(
        f"/{PLUGIN_NAME}/about", get_about, ["GET"], "获取插件版本信息"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/dashboard/stats", get_dashboard_stats, ["GET"], "获取概览统计"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/calendar/month", get_calendar_month, ["GET"], "获取月视图事件"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/tasks/list", get_tasks_list, ["GET"], "获取 pending 任务列表"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/tasks/options", get_tasks_options, ["GET"], "获取新增任务选项（白名单会话/时区）"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/tasks/create", create_task, ["POST"], "新增手动提醒任务"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/tasks/cancel", cancel_task, ["POST"], "取消任务"
    )
    context.register_web_api(
        f"/{PLUGIN_NAME}/config/schema", get_config_schema, ["GET"], "获取配置（只读）"
    )

    logger.info(f"{tag()} ✅ Web API 已注册（共 8 个端点）")
