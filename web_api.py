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
- GET  /time_awareness/schedules/personas  有快照的 Persona 列表
- GET  /time_awareness/schedules/detail    某日快照详情（含编辑权限）
- POST /time_awareness/schedules/save      保存人格日程用户层草稿
- POST /time_awareness/schedules/delete    删除用户时段（未来部分）
- GET  /time_awareness/static-schedules 静态日程配置
- POST /time_awareness/static-schedules/save 保存静态日程配置
- GET  /time_awareness/config/schema    配置只读（schema 元信息 + 当前值）

统一响应信封：``{success: bool, ...data | error: str}``
"""

import json
import os
from datetime import date, datetime, timedelta

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

async def _get_json_body():
    """读取 POST JSON 请求体；非法 JSON 返回 None（调用方按参数错误 400 处理）。

    Quart 的 ``request.get_json()`` 对非 JSON 内容抛 ``BadRequest``，若不
    拦截会落入各端点最外层 ``except Exception`` 误报 500。
    """
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


# ==================== 控制器 ====================


class TimeAwarenessWebApi:
    """Plugin Page 的显式 Web API 控制器。"""

    def __init__(self, context, plugin):
        self.context = context
        self.plugin = plugin
        self.scheduler = plugin.scheduler
        self.config = plugin.config
        self.calendar_store = plugin.calendar_store
        self.schedule_admin = plugin.daily_schedule_admin
        self.static_schedule_config = getattr(plugin, "static_schedule_config", None)

    async def get_about(self):
        try:
            meta = _read_metadata()
            return _ok(
                name=str(meta.get("name", PLUGIN_NAME)),
                version=str(meta.get("version", "")),
                display_name=str(meta.get("display_name", "")),
                author=str(meta.get("author", "")),
            )
        except Exception as exc:
            return _internal_error(exc)

    async def get_dashboard_stats(self):
        try:
            now = get_now(self.config, self.plugin._astrbot_config())
            pending = self.scheduler.all_pending()
            failed = self.scheduler.all_failed()
            week_cutoff = now + timedelta(days=7)
            # 「近 7 天任务」只统计尚未到期的未来任务；已超时（迟到窗口内
            # 未派发）的任务不计入，避免指标失真
            near_7d = sum(
                1
                for task in pending
                if now <= coerce_to_aware(task.due_at(), now) <= week_cutoff
            )
            next_task_display = "无"
            if pending:
                next_task = min(
                    pending,
                    key=lambda task: coerce_to_aware(task.due_at(), now),
                )
                delta = (
                    coerce_to_aware(next_task.due_at(), now) - now
                ).total_seconds()
                next_task_display = "已到期" if delta < 0 else _relative_time(delta)
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
                "task_pending_total": len(pending),
                "task_calendar_pending": sum(1 for task in pending if task.kind == "calendar"),
                "task_followup_pending": sum(1 for task in pending if task.kind == "followup"),
                "task_user_pending": sum(1 for task in pending if task.kind == "user"),
                "task_retry_wait": sum(1 for task in pending if task.status == "retry_wait"),
                "task_failed_total": len(failed),
                "this_month_event_count": len(month_events),
                "near_7d_task_count": near_7d,
                "reminder_enabled": bool(
                    _safe_group_config(self.config, "reminder").get("enable_reminder", False)
                ),
                "calendar_enabled": bool(
                    _safe_group_config(self.config, "calendar").get("enable_calendar", False)
                ),
                "next_task_display": next_task_display,
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
        except Exception as exc:
            return _internal_error(exc)

    async def get_calendar_month(self):
        try:
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
        except Exception as exc:
            return _internal_error(exc)

    async def get_tasks_list(self):
        try:
            display_tz = get_tz(self.config, self.plugin._astrbot_config())
            tasks = self.scheduler.list_pending_detailed(display_tz=display_tz)
            finished = self.scheduler.list_terminal_detailed(display_tz=display_tz)
            return _ok(
                tasks=tasks,
                total=len(tasks),
                finished=finished,
                retention_days=self.scheduler.state_retention_days(),
            )
        except Exception as exc:
            return _internal_error(exc)

    async def cancel_task(self):
        try:
            data = await _get_json_body()
            if not isinstance(data, dict):
                return _err("请求体必须是 JSON 对象", 400)
            task_id = str(data.get("task_id", "")).strip()
            if not task_id:
                return _err("task_id 不能为空", 400)
            result = self.scheduler.cancel_task(task_id)
            if result == "final":
                # 已终态（sent/failed/expired/cancelled）：幂等成功而非 404
                return _ok(cancelled=False, already_final=True, task_id=task_id)
            if result == "processing":
                return _err("任务正在发送，当前无法取消", 409)
            if not result:
                return _err("任务未找到", 404)
            return _ok(cancelled=True, task_id=task_id)
        except Exception as exc:
            return _internal_error(exc)

    async def get_tasks_options(self):
        try:
            now = get_now(self.config, self.plugin._astrbot_config())
            timezone = get_tz(self.config, self.plugin._astrbot_config())
            return _ok(
                sessions=self.plugin.reminder_service.targets(),
                now_iso=now.isoformat(),
                # 真实绝对毫秒：前端用它把「无时区字符串」按浏览器本地时区
                # 解释产生的偏移校正掉（浏览器时区 ≠ 插件时区时不误拒）
                server_now_ms=int(now.timestamp() * 1000),
                tz_label=(
                    str(timezone)
                    if timezone is not None
                    else "系统本地（未配置时区）"
                ),
                default_fire_at_local=(now + timedelta(minutes=10)).strftime(
                    "%Y-%m-%dT%H:%M"
                ),
            )
        except Exception as exc:
            return _internal_error(exc)

    async def create_task(self):
        try:
            data = await _get_json_body()
            if not isinstance(data, dict):
                return _err("请求体必须是 JSON 对象", 400)
            session = str(data.get("session", "")).strip()
            fire_at_local = str(data.get("fire_at_local", "")).strip()
            hint = str(data.get("hint", "")).strip()
            target_user_id = str(data.get("target_user_id", "")).strip()
            try:
                naive = datetime.fromisoformat(fire_at_local)
            except ValueError:
                return _err("fire_at_local 格式错误，需 YYYY-MM-DDTHH:MM", 400)
            timezone = get_tz(self.config, self.plugin._astrbot_config())
            fire_at = naive.replace(tzinfo=timezone) if timezone is not None else naive
            try:
                task_id = await self.plugin.create_external_task(
                    session=session,
                    fire_at=fire_at,
                    hint=hint,
                    target_user_id=target_user_id,
                    source="webui",
                )
            except ValueError as exc:
                return _err(str(exc), 400)
            logger.info(
                f"{tag()} 🧑 WebUI 创建手动提醒: id={task_id} "
                f"session={session} fire_at={fire_at.isoformat()}"
            )
            return _ok(task_id=task_id, fire_at_iso=fire_at.isoformat())
        except Exception as exc:
            return _internal_error(exc)

    async def get_schedules_personas(self):
        """列出有 ready 快照的 Persona（HMAC 匹配名称，未匹配显示 hash）。"""
        try:
            personas = await self.schedule_admin.list_personas()
            return _ok(personas=personas)
        except Exception as exc:
            return _internal_error(exc)

    async def get_schedules_detail(self):
        """返回某 Persona 某日快照详情（含每段编辑权限与服务端时间基准）。"""
        try:
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
        except Exception as exc:
            return _internal_error(exc)

    async def save_schedule(self):
        """保存人格日程整日草稿（服务端重算权限，409 防并发覆盖）。"""
        try:
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
            except ScheduleNotFound:
                return _err("日程快照不存在或已被清理，请刷新", 404)
            except ScheduleSaveConflict:
                return _err("日程已被重新生成或由其他页面修改，请刷新", 409)
            except SchedulePersistenceError as exc:
                # 服务端故障（写盘失败），不是客户端校验错误
                logger.error(f"{tag()} ❌ 日程快照持久化失败: {exc}")
                return _err("服务器保存失败，请重试", 500)
            except ValueError as exc:
                return _err(str(exc) or "校验失败")
            return _ok(**result)
        except Exception as exc:
            return _internal_error(exc)

    async def delete_schedule_slot(self):
        """删除一个用户时段（只删未来部分；已执行前段永久保留）。

        显式删除端点：与「保存时省略 user_slot_id」同一删除语义，但意图
        可见、可审计，未来 backup/审计路径可复用。
        """
        try:
            body = await _get_json_body()
            if not isinstance(body, dict):
                return _err("请求体必须是 JSON 对象")
            persona_hash = str(body.get("persona_hash") or "").strip()
            date_text = str(body.get("date") or "").strip()
            timezone = str(body.get("timezone") or "").strip()
            snapshot_id = str(body.get("snapshot_id") or "").strip()
            user_slot_id = str(body.get("user_slot_id") or "").strip()
            if not persona_hash or not date_text or not snapshot_id or not user_slot_id:
                return _err("缺少 persona_hash/date/snapshot_id/user_slot_id")
            try:
                local_date = date.fromisoformat(date_text)
            except ValueError:
                return _err("date 格式必须为 YYYY-MM-DD")
            try:
                result = await self.schedule_admin.delete_user_slot(
                    persona_hash=persona_hash,
                    local_date=local_date,
                    timezone=timezone,
                    snapshot_id=snapshot_id,
                    user_slot_id=user_slot_id,
                )
            except ScheduleNotFound:
                return _err("日程快照不存在或已被清理，请刷新", 404)
            except ScheduleSaveConflict:
                return _err("日程已被重新生成或由其他页面修改，请刷新", 409)
            except SchedulePersistenceError as exc:
                logger.error(f"{tag()} ❌ 日程快照持久化失败: {exc}")
                return _err("服务器删除失败，请重试", 500)
            except ValueError as exc:
                return _err(str(exc) or "校验失败")
            return _ok(**result)
        except Exception as exc:
            return _internal_error(exc)

    async def get_static_schedules(self):
        """读取静态日程配置及乐观并发 revision。"""
        try:
            if self.static_schedule_config is None:
                return _err("静态日程配置服务不可用", 503)
            return _ok(**self.static_schedule_config.get())
        except Exception as exc:
            return _internal_error(exc)

    async def save_static_schedules(self):
        """保存静态日程；配置落盘失败回滚，revision 冲突返回 409。"""
        try:
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
            return _ok(**result)
        except Exception as exc:
            return _internal_error(exc)

    async def get_config_schema(self):
        try:
            groups = []
            for group_key, group_definition in _read_conf_schema().items():
                if not isinstance(group_definition, dict):
                    continue
                items = group_definition.get("items", {})
                if not isinstance(items, dict):
                    continue
                group_config = _safe_group_config(self.config, group_key)
                fields = []
                for field_key, field_definition in items.items():
                    if not isinstance(field_definition, dict):
                        continue
                    if field_definition.get("invisible"):
                        continue
                    default = field_definition.get("default")
                    fields.append(
                        {
                            "key": field_key,
                            "type": field_definition.get("type", ""),
                            "description": field_definition.get("description", ""),
                            "hint": field_definition.get("hint", ""),
                            "default": default,
                            "value": group_config.get(field_key, default),
                        }
                    )
                groups.append(
                    {
                        "key": group_key,
                        "description": group_definition.get("description", ""),
                        "fields": fields,
                    }
                )
            return _ok(groups=groups)
        except Exception as exc:
            return _internal_error(exc)

    def register(self) -> None:
        endpoints = (
            ("about", self.get_about, ["GET"], "获取插件版本信息"),
            ("schedules/personas", self.get_schedules_personas, ["GET"], "获取有快照的 Persona 列表"),
            ("schedules/detail", self.get_schedules_detail, ["GET"], "获取某日快照详情（含编辑权限）"),
            ("schedules/save", self.save_schedule, ["POST"], "保存人格日程整日草稿"),
            ("schedules/delete", self.delete_schedule_slot, ["POST"], "删除用户时段（未来部分）"),
            ("static-schedules", self.get_static_schedules, ["GET"], "获取静态日程配置"),
            ("static-schedules/save", self.save_static_schedules, ["POST"], "保存静态日程配置"),
            ("dashboard/stats", self.get_dashboard_stats, ["GET"], "获取概览统计"),
            ("calendar/month", self.get_calendar_month, ["GET"], "获取月视图事件"),
            ("tasks/list", self.get_tasks_list, ["GET"], "获取 pending 任务列表"),
            ("tasks/options", self.get_tasks_options, ["GET"], "获取新增任务选项（白名单会话/时区）"),
            ("tasks/create", self.create_task, ["POST"], "新增手动提醒任务"),
            ("tasks/cancel", self.cancel_task, ["POST"], "取消任务"),
            ("config/schema", self.get_config_schema, ["GET"], "获取配置（只读）"),
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
    """构造并注册控制器；鉴权沿用 AstrBot Plugin Page。"""
    controller = TimeAwarenessWebApi(context, plugin)
    controller.register()
    return controller
