"""时笺：时间感知 + 智能日历 + 主动提醒。"""

import asyncio
import datetime
import os

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.message import TextPart

from .commands.calendar_commands import CalendarCommands
from .commands.schedule_commands import ScheduleCommands
from .constants import DEFAULT_TIME_GUIDANCE_PROMPT
from .log import configure as configure_log
from .log import logger, tag
from .core.builtin_manager import BuiltinManager
from .core.calendar_manager import CalendarManager
from .core.calendar_store import CalendarStore
from .core.daily_schedule_store import DailyScheduleSnapshotStore
from .core.last_chat_tracker import LastChatTracker
from .core.scheduler import DueGroup, ReminderScheduler, TriggerResult
from .core.weather_sensor import WeatherSensor
from .domain.schedule import detect_static_overlaps
from .integrations.astrbot_sender import AstrBotMessageSender
from .integrations.chat_memory_history import is_chat_memory_takeover_applied
from .services.time_context_service import TimeContextService
from .services.daily_schedule_service import DailyScheduleService
from .services.adaptive_policy import (
    AdaptiveConcurrencyGate,
    AdaptiveGenerationPolicy,
)
from .services.calendar_runtime_service import CalendarRuntimeService
from .services.daily_schedule_admin_service import DailyScheduleAdminService
from .services.reminder_service import ReminderService
from .services.static_schedule_config_service import StaticScheduleConfigService
from .utils.time_utils import get_now
from .web_api import register_web_apis

PLUGIN_DATA_DIR_NAME = "time_awareness"


class TimeAwarenessPlugin(Star):
    """时笺 + 智能日历 + 主动提醒。"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}

        # 日志前缀带 bot 实例标识：顶层全局配置项
        configure_log(log_with_bot_id=bool(self.config.get("log_with_bot_id", False)))

        self._check_schedule_overlap()

        try:
            self.data_dir = str(StarTools.get_data_dir(PLUGIN_DATA_DIR_NAME))
        except Exception as e:
            base = os.path.join(os.getcwd(), "data", "plugin_data", PLUGIN_DATA_DIR_NAME)
            os.makedirs(base, exist_ok=True)
            self.data_dir = base
            logger.warning(
                f"{tag()} ⚠️ StarTools.get_data_dir 不可用，回退到 {base}: {e}"
            )

        self.calendar_store = CalendarStore()
        self.calendar_manager = CalendarManager(self.data_dir, store=self.calendar_store)
        self.builtin_manager = BuiltinManager(self.data_dir)
        self.last_chat_tracker = LastChatTracker(self.data_dir)
        self.weather_sensor = WeatherSensor(
            config=self.config,
            data_dir=self.data_dir,
            # 以插件时区取「今天」，与 local_date 同源（避免系统时区差一天）
            now_provider=lambda: get_now(self.config, self._astrbot_config()),
        )
        self.time_context = TimeContextService(
            config=self.config,
            astrbot_config_provider=self._astrbot_config,
            calendar_store=self.calendar_store,
            last_chat_tracker=self.last_chat_tracker,
        )
        # 插件级唯一 LLM 闸门：AI 日程、主动提醒与 /calendar create 共用。
        self.llm_gate = AdaptiveConcurrencyGate(
            AdaptiveGenerationPolicy.from_config(self.config).max_concurrent_llm
        )
        self.daily_schedule_store = DailyScheduleSnapshotStore(self.data_dir)
        self.daily_schedule_service = DailyScheduleService(
            context=self.context,
            config=self.config,
            store=self.daily_schedule_store,
            time_context=self.time_context,
            concurrency_gate=self.llm_gate,
            weather_forecast_provider=self.weather_sensor.daily_forecast,
        )
        self.time_context.set_daily_schedule_service(self.daily_schedule_service)
        # 人格日程人工编辑服务（WebUI 查询/编辑快照，与生成服务共用锁）
        self.daily_schedule_admin = DailyScheduleAdminService(
            context=self.context,
            store=self.daily_schedule_store,
            daily_schedule_service=self.daily_schedule_service,
        )
        # now_provider 用与 fire_at 同时区的 now，避免 aware/naive 比较报错
        self.scheduler = ReminderScheduler(
            self.data_dir,
            self._on_due,
            now_provider=lambda: get_now(self.config, self._astrbot_config()),
        )
        self.calendar_runtime = CalendarRuntimeService(
            config=self.config,
            calendar_store=self.calendar_store,
            builtin_manager=self.builtin_manager,
            scheduler=self.scheduler,
            time_context=self.time_context,
            targets_provider=lambda: self.reminder_service.targets(),
        )
        self.message_sender = AstrBotMessageSender(self.context)
        self.reminder_service = ReminderService(
            context=self.context,
            config=self.config,
            scheduler=self.scheduler,
            time_context=self.time_context,
            message_sender=self.message_sender,
            last_chat_tracker=self.last_chat_tracker,
            astrbot_config_provider=self._astrbot_config,
            ensure_builtin_fresh=self.calendar_runtime.ensure_builtin_fresh,
            concurrency_gate=self.llm_gate,
        )
        self.calendar_commands = CalendarCommands(
            context=self.context,
            config=self.config,
            calendar_store=self.calendar_store,
            calendar_manager=self.calendar_manager,
            builtin_manager=self.builtin_manager,
            now_provider=self.time_context.now,
            enabled_builtin_categories=self.time_context.enabled_builtin_categories,
            concurrency_gate=self.llm_gate,
        )
        self.schedule_commands = ScheduleCommands(
            context=self.context,
            config=self.config,
            daily_schedule_service=self.daily_schedule_service,
            now_provider=self.time_context.now,
            save_config=self._try_save_config,
            check_overlap=self._check_schedule_overlap,
        )
        self.static_schedule_config = StaticScheduleConfigService(
            config=self.config,
            save_config=self._try_save_config,
            check_overlap=self._check_schedule_overlap,
        )
        self.calendar_runtime.apply_reminder_config()
        self._daily_scan_task: asyncio.Task | None = None
        # AI 每日日程在配置时刻生成；与 reminder 开关独立
        self._daily_schedule_task: asyncio.Task | None = None
        self.web_api = None

        logger.info(f"{tag()} 插件已初始化，数据目录: {self.data_dir}")

    async def initialize(self):
        """加载数据 + 启动调度；失败时逆序回滚后重抛。"""
        self.calendar_manager.load()
        self.last_chat_tracker.load()
        daily_store_ready = False
        if self.daily_schedule_service.configured_enabled():
            daily_store_ready = self.daily_schedule_store.load()
        self.daily_schedule_service.set_available(daily_store_ready)

        self.calendar_runtime.initialize_builtin_events()
        self.calendar_runtime.apply_reminder_config()
        self.scheduler.load()

        reminder_cfg = self.config.get("reminder", {})
        if not isinstance(reminder_cfg, dict):
            reminder_cfg = {}
        try:
            if reminder_cfg.get("enable_reminder", False):
                # 启动先扫今天事项入队（错过当日提醒时刻仍可补发）
                self.calendar_runtime.scan_and_enqueue_today_events()
                self.scheduler.start()
                self._daily_scan_task = asyncio.create_task(
                    self.calendar_runtime.run_daily_loop()
                )

            if self.daily_schedule_service.enabled():
                # reminder_targets 是启动阶段唯一安全可得的代表会话；UMO 仅存进程
                for session in self.reminder_service.targets():
                    await self.daily_schedule_service.register_session_async(
                        session, trigger=False
                    )
                await self.daily_schedule_service.queue_known_profiles()
                self._daily_schedule_task = asyncio.create_task(
                    self.daily_schedule_service.run_daily_loop()
                )

            # 注册 Plugin Pages 后端 API（鉴权继承 AstrBot 主 webui）
            self.web_api = register_web_apis(self.context, self)
        except BaseException:
            logger.warning(f"{tag()} ⚠️ 初始化失败，按已启动资源逆序回滚")
            await self._shutdown_resources()
            raise

        if self.weather_sensor.enabled():
            asyncio.create_task(self._announce_random_weather())
        logger.info(f"{tag()} ✅ 初始化完成")

    async def _announce_random_weather(self) -> None:
        try:
            ok, message = await self.weather_sensor.validate()
        except Exception as e:
            logger.debug(f"{tag()} 随机天气获取异常: {e}")
            return
        if ok:
            logger.info(f"{tag()} 🌦️ 今日随机天气：{message}")
        else:
            logger.debug(f"{tag()} 随机天气未启用")

    async def _cancel_background_task(self, task, message: str) -> None:
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"{tag()} ⚠️ {message}: {e}")

    async def _shutdown_resources(self) -> None:
        """逆序停止后台资源；幂等，供 initialize 回滚与 terminate 共用。"""
        await self._cancel_background_task(self._daily_schedule_task, "AI 每日日程 task 停止异常")
        self._daily_schedule_task = None
        try:
            await self.daily_schedule_service.close()
        except Exception as e:
            logger.warning(f"{tag()} ⚠️ AI 每日日程生成任务停止异常: {e}")
        await self._cancel_background_task(self._daily_scan_task, "每日扫描 task 停止异常")
        self._daily_scan_task = None
        try:
            await self.scheduler.stop()
        except Exception as e:
            logger.warning(f"{tag()} ⚠️ 调度器停止异常: {e}")
        try:
            await self.last_chat_tracker.close()
        except Exception as e:
            logger.warning(f"{tag()} ⚠️ 上次对话时间刷盘异常: {e}")

    async def terminate(self):
        await self._shutdown_resources()
        logger.info(f"{tag()} ✅ 已终止")

    async def create_external_task(
        self,
        session: str,
        fire_at: datetime.datetime,
        hint: str,
        target_user_id: str = "",
        source: str = "",
    ) -> str:
        """供其他插件调用的任务创建 API（kind=user），委托 reminder_service。"""
        return self.reminder_service.create_external_task(
            session=session,
            fire_at=fire_at,
            hint=hint,
            target_user_id=target_user_id,
            source=source,
        )

    async def _on_due(self, group: DueGroup) -> TriggerResult:
        task_summary = ",".join(f"{t.kind}:{t.id}" for t in group.tasks)
        late_info = f" late={group.late_minutes}min" if group.is_late else ""
        logger.info(
            f"{tag()} 🔔 触发到期任务: session={group.session} "
            f"tasks={len(group.tasks)} [{task_summary}]{late_info}"
        )
        await self.daily_schedule_service.register_session_async(group.session)
        return await self.reminder_service.deliver(
            session=group.session,
            tasks=group.tasks,
            is_late=group.is_late,
            late_minutes=group.late_minutes,
        )

    def _astrbot_config(self):
        try:
            return self.context.get_config()
        except Exception:
            return None

    def _try_save_config(self) -> bool:
        """落盘 self.config（AstrBotConfig.save_config）；失败仅 warning。"""
        save_fn = getattr(self.config, "save_config", None)
        if not callable(save_fn):
            logger.debug(f"{tag()} self.config 无 save_config 方法，跳过落盘")
            return False
        try:
            save_fn()
            logger.debug(f"{tag()} 配置已落盘")
            return True
        except Exception as e:
            logger.warning(f"{tag()} ⚠️ 配置落盘失败（不致命，下次启动会重跑）: {e}")
            return False

    def _check_schedule_overlap(self) -> None:
        """检测 schedule_templates 时段重叠，仅 warning（跨午夜展开后比较）。"""
        daily_schedule = self.config.get("daily_schedule", {})
        if not isinstance(daily_schedule, dict):
            daily_schedule = {}
        slots = daily_schedule.get("schedule_templates") or []
        if not isinstance(slots, list) or len(slots) < 2:
            return
        for overlap in detect_static_overlaps(slots):
            first = slots[overlap.first_index]
            second = slots[overlap.second_index]
            logger.warning(
                f"{tag()} ⚠️ 日程表时段重叠："
                f"slot#{overlap.second_index}({second.get('start_time')}-{second.get('end_time')}) "
                f"与 slot#{overlap.first_index}("
                f"{first.get('start_time')}-{first.get('end_time')}) "
                f"在 {overlap.minute // 60:02d}:{overlap.minute % 60:02d} "
                f"重叠（行为：取列表中靠前的时段）"
            )

    # -150 须晚于 chat_memory(-100) 且早于终结器；改动前核对全部 on_llm_request priority。
    @filter.on_llm_request(priority=-150)
    async def inject_time_context(self, event: AstrMessageEvent, req: ProviderRequest):
        """时间引导注入：静态规则进 system_prompt，动态数据进 extra_user_content_parts(temp)。"""
        # AI 日程注册不依赖 time_guidance 开关，避免关闭引导使日程对新会话失效
        umo = event.unified_msg_origin
        if self.daily_schedule_service.enabled():
            try:
                await self.daily_schedule_service.register_session_async(
                    umo, event=event
                )
            except Exception as e:
                logger.warning(f"{tag(event)} ⚠️ AI 日程会话注册失败: {e}")

        if not self.time_context.time_guidance_enabled():
            return
        try:
            existing = (getattr(req, "system_prompt", "") or "").strip()
            rules = DEFAULT_TIME_GUIDANCE_PROMPT
            # 幂等：重复处理时不再叠加规则文本，保护 prompt 前缀缓存
            if rules not in existing:
                req.system_prompt = (
                    f"{existing.rstrip()}\n\n{rules}" if existing else rules
                )
            # 记录用户消息时间；单独 try 避免 record 失败拖垮 dyn 注入
            if self.time_context.last_chat_enabled() and umo:
                try:
                    self.last_chat_tracker.record_user(
                        umo, get_now(self.config, self._astrbot_config())
                    )
                except Exception as e:
                    logger.warning(f"{tag(event)} ⚠️ record_user 失败: {e}")
            # 动态注入隔离：任一传感器异常不影响整体
            try:
                weather_text = None
                if self.weather_sensor.enabled():
                    try:
                        current = await self.weather_sensor.current_weather()
                        if current:
                            weather_text = self.weather_sensor.render_current(current)
                    except Exception as e:
                        logger.debug(f"{tag(event)} 天气实况查询失败: {e}")
                schedule_summary = None
                try:
                    schedule_summary = self.daily_schedule_service.today_schedule_summary(umo)
                except Exception as e:
                    logger.debug(f"{tag(event)} 今日安排摘要获取失败: {e}")
                dyn = self.time_context.build_dynamic(
                    session=umo,
                    include_last_chat=not is_chat_memory_takeover_applied(event),
                    weather_text=weather_text,
                    schedule_summary=schedule_summary,
                )
                req.extra_user_content_parts.append(TextPart(text=dyn).mark_as_temp())
            except Exception as e:
                logger.warning(f"{tag(event)} ⚠️ 动态时间上下文构建失败: {e}")
        except Exception as e:
            logger.error(f"{tag(event)} ❌ on_llm_request 注入失败: {e}")

    @filter.after_message_sent()
    async def _record_ai_sent_time(self, event: AstrMessageEvent):
        """发送后记录时间用于 <LAST_CHAT>；命令消息不计入。"""
        if not self.time_context.last_chat_enabled():
            return
        try:
            original = getattr(event.message_obj, "message_str", "") or ""
            if original.strip().startswith("/"):
                return
        except Exception:
            pass
        try:
            umo = event.unified_msg_origin
            if not umo:
                return
            self.last_chat_tracker.record_ai(
                umo, get_now(self.config, self._astrbot_config())
            )
        except Exception as e:
            logger.warning(f"{tag(event)} ⚠️ 记录 AI 发送时间失败: {e}")

    @filter.command_group("calendar")
    def calendar_group(self):
        """日历管理命令组入口。"""

    @calendar_group.command("help")
    async def cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result(self.calendar_commands.help_text())

    @calendar_group.command("show")
    async def cmd_list(self, event: AstrMessageEvent, month: str = ""):
        yield event.plain_result(self.calendar_commands.show(month))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @calendar_group.command("add")
    async def cmd_add(self, event: AstrMessageEvent, date_str: str = "", repeat_or_title: str = "", title: str = ""):
        yield event.plain_result(
            self.calendar_commands.add(date_str, repeat_or_title, title)
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @calendar_group.command("del")
    async def cmd_delete(self, event: AstrMessageEvent, event_id: str = ""):
        yield event.plain_result(self.calendar_commands.delete(event_id))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @calendar_group.command("create")
    async def cmd_generate(self, event: AstrMessageEvent):
        async for text in self.calendar_commands.generate(event.unified_msg_origin or ""):
            yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @calendar_group.command("export")
    async def cmd_export(self, event: AstrMessageEvent):
        yield event.plain_result(self.calendar_commands.export_yaml())

    @filter.permission_type(filter.PermissionType.ADMIN)
    @calendar_group.command("import")
    async def cmd_import(self, event: AstrMessageEvent, replace: str = ""):
        content = ""
        try:
            reply = getattr(event.message_obj, "reply", None)
            if reply:
                chain = getattr(reply, "chain", None) or getattr(reply, "message_chain", None)
                if chain:
                    for c in chain:
                        text = getattr(c, "text", None) or (c if isinstance(c, str) else "")
                        if text:
                            content += text
        except Exception:
            content = ""

        yield event.plain_result(
            self.calendar_commands.import_yaml(
                content, replace=str(replace or "").strip().lower() == "replace"
            )
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @calendar_group.command("builtin_regen")
    async def cmd_builtin_regen(self, event: AstrMessageEvent):
        yield event.plain_result(self.calendar_commands.builtin_regenerate())

    @filter.permission_type(filter.PermissionType.ADMIN)
    @calendar_group.command("builtin_list")
    async def cmd_builtin_list(self, event: AstrMessageEvent, category: str = ""):
        yield event.plain_result(self.calendar_commands.builtin_list(category))

    @filter.command_group("schedule")
    def schedule_group(self):
        """单日日程表管理命令组入口（时段状态感知）。"""

    @schedule_group.command("help")
    async def schedule_help(self, event: AstrMessageEvent):
        yield event.plain_result(self.schedule_commands.help_text())

    @schedule_group.command("show")
    async def schedule_show(self, event: AstrMessageEvent):
        session = event.unified_msg_origin or ""
        if session:
            await self.daily_schedule_service.register_session_async(session, event=event)
        yield event.plain_result(self.schedule_commands.show(session))

    @schedule_group.command("static")
    async def schedule_static(self, event: AstrMessageEvent):
        yield event.plain_result(self.schedule_commands.static())

    @filter.permission_type(filter.PermissionType.ADMIN)
    @schedule_group.command("regenerate")
    async def schedule_regenerate(self, event: AstrMessageEvent):
        async for text in self.schedule_commands.regenerate(
            event.unified_msg_origin or "",
            event=event,
        ):
            yield event.plain_result(text)

    @filter.llm_tool(name="schedule_followup")
    async def schedule_followup_tool(
        self,
        event: AstrMessageEvent,
        delay_minutes: int,
        hint: str,
    ) -> str:
        """
        安排一个稍后触发的主动消息：到点后你会再次被唤醒，并收到当时的时间、角色状态与本条备忘，
        以便延续当前的状态和话题。

        触发场景：
        - 用户明确要求「X 分钟后提醒我」「晚点再聊」等
        - 你和用户约定了稍后继续某事（如「等我下」「一会儿告诉你」）

        不要触发的场景：
        - 用户没明确要求时主动定时（避免打扰）
        - 已完成的事或闲聊中无事可约

        Args:
            delay_minutes(int): 几分钟后触发。范围 1-1440（最多 24 小时）。
            hint(string): 给「未来的自己」的备忘。必填。写清楚约定了什么、
                要继续什么、以什么口吻回应。
        """
        reminder_cfg = self.config.get("reminder", {})
        if not isinstance(reminder_cfg, dict):
            reminder_cfg = {}
        if not reminder_cfg.get("enable_reminder", False):
            return "失败：主动提醒功能未启用"
        if not reminder_cfg.get("followup_tool_enabled", True):
            return "失败：schedule_followup 工具已被管理员禁用"

        try:
            delay = int(delay_minutes)
        except (TypeError, ValueError):
            return f"失败：delay_minutes 必须是整数，收到 {delay_minutes!r}"
        if delay < 1 or delay > 1440:
            return f"失败：delay_minutes 越界（应为 1-1440，收到 {delay}）"

        hint = (hint or "").strip()
        if not hint:
            return "失败：hint 不能为空"
        if len(hint) > 500:
            return "失败：hint 长度不能超过 500 字"

        session = event.unified_msg_origin or ""
        if session not in self.reminder_service.targets():
            return "失败：当前会话不在提醒白名单内"

        now = get_now(self.config, self._astrbot_config())
        fire_at = now + datetime.timedelta(minutes=delay)
        target_uid = event.get_sender_id() or ""
        self.scheduler.add_followup_task(
            session=session,
            fire_at=fire_at,
            hint=hint,
            target_user_id=target_uid,
        )
        hint_preview = f"（备忘：{hint}）"
        time_str = fire_at.strftime('%H:%M')
        return (
            f"已安排 {delay} 分钟后（{time_str}）触发主动消息{hint_preview}。\n"
            f"本轮无需向用户复述本次工具调用——若刚才已和用户说过安排，直接返回空回复即可；若未说过，简短告知一声即可。"
        )
