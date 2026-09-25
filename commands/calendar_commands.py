"""日历命令应用服务（不依赖 AstrMessageEvent；Star Handler 只负责参数与回复文本转换）。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

from ..core.calendar_manager import CalendarManager
from ..core.calendar_store import REPEAT_FOREVER
from ..llm.calendar_generator import (
    DEFAULT_MAX_GENERATE,
    build_system_prompt,
    generate_calendar_events,
)
from ..services.adaptive_policy import AdaptiveConcurrencyGate
from .calendar_helpers import (
    category_short_tag,
    describe_repeat,
    find_event_by_short_id,
    parse_category_input,
    parse_event_date,
    parse_year_month,
)


class CalendarCommands:
    def __init__(
        self,
        *,
        context: Any,
        config: dict,
        calendar_store,
        calendar_manager,
        builtin_manager,
        now_provider: Callable[[], Any],
        enabled_builtin_categories: Callable[[], list[str]],
        concurrency_gate: AdaptiveConcurrencyGate | None = None,
    ):
        self.context = context
        self.config = config
        self.calendar_store = calendar_store
        self.calendar_manager = calendar_manager
        self.builtin_manager = builtin_manager
        self._now = now_provider
        self._enabled_builtin_categories = enabled_builtin_categories
        self._llm_gate = concurrency_gate

    @staticmethod
    def help_text() -> str:
        return (
            "[time_awareness 命令]\n"
            "/calendar show [YYYY-MM]  — 列出指定月份（默认本月）的事项\n"
            "/calendar add <日期> [重复] <标题>  — 新增事项（管理员）\n"
            "/calendar del <id>  — 删除用户事项（管理员；内置事件不允许删除，请用配置开关）\n"
            "/calendar create  — 按世界观替换全部自定义事项，内置事件不变（管理员）\n"
            "/calendar export  — 输出当前数据为 YAML（管理员）\n"
            "/calendar import [replace]  — 回复一条 YAML 文本导入（管理员；默认合并，加 replace 清空后导入）\n"
            "/calendar builtin_regen  — 重新生成当年内置现实事件（管理员）\n"
            "/calendar builtin_list [分类]  — 列出当年所有内置事件（可按「法定/传统/节气/政治/国际」过滤）\n"
            "/calendar help  — 显示本帮助\n"
            "\n"
            "日期格式：\n"
            "- YYYY-MM-DD（如 2026-06-24）\n"
            "- MM-DD（如 06-24，自动补当前年份）\n"
            "\n"
            "重复参数（可选）：\n"
            "- 0  = 仅当年（默认）\n"
            "- 1-4 = 从基准年起连续 N+1 年\n"
            "- 9  = 永久每年重复\n"
            "\n"
            "示例：\n"
            "/calendar add 2026-06-24 测试事件\n"
            "/calendar add 06-24 9 每年生日\n"
            "/calendar del abc12345  — 按 id 删除用户事件\n"
            "\n"
            "内置现实日历事件（默认开法定/传统/节气，关政治/国际）：\n"
            "- 法定节假日（含调休）、传统农历节日、二十四节气\n"
            "- 政治纪念日、国际/西方节日\n"
            "- 黄历开关位于 time_awareness 的「时间传感器」多选（勾选黄历）\n"
            "- 跨年自动重新生成"
        )

    def show(self, month: str = "") -> str:
        now = self._now()
        year, mon = now.year, now.month
        if month:
            parsed = parse_year_month(month)
            if parsed is None:
                return f"格式错误：{month}，应为 YYYY-MM 或 YYYY-MM-DD。"
            year, mon = parsed

        events = self.calendar_store.events_for_month(
            year,
            mon,
            include_builtin=bool(self._enabled_builtin_categories()),
        )
        if not events:
            return f"{year}-{mon:02d} 无事项。"

        lines = [f"[{year}-{mon:02d} 共 {len(events)} 条]"]
        for event in events:
            day = event.get("day", 0)
            text = event.get("text", "")
            if event.get("source") == "builtin":
                lines.append(
                    f"  {mon:02d}-{day:02d} {text} "
                    f"[{category_short_tag(event.get('category', ''))}]"
                )
            else:
                lines.append(
                    f"  #{event.get('id', '')[:8]} {mon:02d}-{day:02d} "
                    f"{text}{describe_repeat(event.get('repeat', 0))}"
                )
        return "\n".join(lines)

    def add(self, date_str: str, repeat_or_title: str, title: str) -> str:
        if not date_str:
            return (
                "用法：/calendar add <日期> [重复] <标题>\n"
                "日期：YYYY-MM-DD 或 MM-DD；重复：0(默认)/1-4/9(永久)\n"
                "示例：/calendar add 2026-06-24 测试事件\n"
                "      /calendar add 06-24 9 每年生日"
            )

        repeat = 0
        if title:
            try:
                repeat_value = int(repeat_or_title)
            except ValueError:
                return f"重复参数必须是整数（0/1-4/9），收到：{repeat_or_title}"
            if repeat_value == 9:
                repeat = REPEAT_FOREVER
            elif 0 <= repeat_value <= 4:
                repeat = repeat_value
            else:
                return f"重复参数越界：{repeat_value}（应为 0/1-4/9）"
            final_title = title
        elif repeat_or_title:
            # 第二个参数可能是「忘了写标题的重复参数」：纯数字且取值合法时明确报错而非静默建数字标题
            try:
                repeat_value = int(repeat_or_title)
            except ValueError:
                repeat_value = -1
            if repeat_value == 9 or 0 <= repeat_value <= 4:
                return (
                    f"检测到重复参数 {repeat_or_title} 但缺少标题。用法："
                    "/calendar add <日期> [重复] <标题>"
                )
            final_title = repeat_or_title
        else:
            return "缺少标题。用法：/calendar add <日期> [重复] <标题>"

        parsed = parse_event_date(date_str)
        if parsed is None:
            return (
                f"日期格式无法解析：{date_str}\n支持 YYYY-MM-DD 或 MM-DD\n"
                "示例：/calendar add 06-24 测试事件"
            )
        month, day, year = parsed
        if year is None:
            year = self._now().year
        event = self.calendar_manager.add_event(
            {
                "year": year,
                "month": month,
                "day": day,
                "text": final_title,
                "repeat": repeat,
            }
        )
        if event is None:
            return "添加失败，请检查参数或日志。"
        return (
            f"已添加 #{event['id'][:8]}：{event['text']}"
            f"（{event['year']}-{event['month']:02d}-{event['day']:02d}"
            f"{describe_repeat(event['repeat'])}）"
        )

    def delete(self, event_id: str) -> str:
        if not event_id:
            return (
                "用法：/calendar del <id> 或 /calendar del <文本>\n"
                "id 从 /calendar show 获取（可只输入前 8 位）；也可直接输入完整事件文本"
            )
        target = find_event_by_short_id(self.calendar_store, event_id)
        if target is None:
            return f"未找到 id 为 {event_id} 的事项。"
        if target.get("source") == "builtin":
            return (
                f"内置事件「{target.get('text', '')}」不允许单条删除。\n"
                "如需关闭整类，请在配置里取消勾选 calendar.builtin_event_categories 对应分类；"
                "如需批量定制，请直接编辑 builtin_events.yaml。"
            )
        if self.calendar_manager.delete_event(target["id"]):
            return f"已删除：{target.get('text', '')}"
        return "删除失败，请检查日志。"

    async def generate(self, session: str) -> AsyncIterator[str]:
        calendar_config = self.config.get("calendar", {})
        if not isinstance(calendar_config, dict):
            calendar_config = {}
        theme = str(calendar_config.get("ai_generate_worldview", "") or "").strip()
        if not theme:
            yield "未配置世界观。请先在配置 calendar.ai_generate_worldview 填写主题/世界观设定后重试。"
            return
        provider_id = str(calendar_config.get("ai_generate_provider_id", "") or "").strip()
        if not provider_id:
            try:
                provider_id = str(
                    await self.context.get_current_chat_provider_id(session) or ""
                ).strip()
            except Exception as exc:
                yield f"无法确定当前会话 Provider：{exc}"
                return
        if not provider_id:
            yield "无法确定当前会话 Provider。"
            return

        now = self._now()
        yield f"正在调用 AI 生成「{theme}」主题日历，请稍候……"
        try:
            max_events = int(calendar_config.get("ai_generate_max_events", DEFAULT_MAX_GENERATE) or DEFAULT_MAX_GENERATE)
        except (TypeError, ValueError):
            max_events = DEFAULT_MAX_GENERATE
        max_events = max(1, min(200, max_events))
        held = False
        try:
            if self._llm_gate is not None:
                held = await self._llm_gate.acquire_configured(self.config)
                if not held:
                    yield "插件全局 LLM 并发等待超时，请稍后重试。"
                    return
            events = await generate_calendar_events(
                self.context,
                provider_id=provider_id,
                user_prompt=theme,
                system_prompt=build_system_prompt(now.year, max_events),
                current_year=now.year,
            )
        finally:
            if held:
                await self._llm_gate.release()
        if not events:
            yield "AI 生成失败或返回为空，请检查 provider 配置与日志。"
            return
        count = self.calendar_manager.import_events(events, mode="replace")
        if count < 0:
            yield "生成成功但写入文件失败，请检查日志。"
            return
        yield (
            f"✅ 已重新生成 {count} 条自定义事项（主题：{theme}）。"
            "原有自定义事项已替换；内置现实事件不受影响。使用 /calendar show 查看全部。"
        )

    def export_yaml(self) -> str:
        if not self.calendar_store.events:
            return "当前时间表为空。"
        return self.calendar_manager.export_yaml()

    def import_yaml(self, content: str, replace: bool = False) -> str:
        """导入 YAML；默认 merge 补充，``replace=True`` 时先清空再导入（备份恢复）。"""
        if not content:
            return (
                "请回复一条 YAML 文本消息后发送 /calendar import [replace]。\n"
                "（YAML 顶层可为数组，或含 events 字段的映射；默认合并，"
                "加 replace 参数则先清空现有自定义事项再导入）"
            )
        raw_events = CalendarManager.parse_import_content(content)
        if raw_events is None:
            return "YAML 解析失败，请检查格式（顶层为数组或含 events 字段的映射）。"
        count = self.calendar_manager.import_events(
            raw_events, mode="replace" if replace else "merge"
        )
        if count < 0:
            return "解析成功但写入文件失败，请检查日志。"
        action = "替换为" if replace else "合并导入"
        return f"✅ 已{action} {count} 条事项。"

    def builtin_regenerate(self) -> str:
        now = self._now()
        categories = self._enabled_builtin_categories()
        if not categories:
            return "未启用任何内置事件分类。请在配置中勾选至少一个内置事件分类（calendar.builtin_event_categories）。"
        count = self.builtin_manager.regenerate(now.year, categories)
        if count < 0:
            return "重新生成失败，请检查日志。"
        data = self.builtin_manager.load_raw()
        self.calendar_store.set_builtin_events(data.get("events") or [])
        return (
            f"✅ 已重新生成 {now.year} 年内置事件：{count} 条\n"
            f"启用分类：{', '.join(categories)}"
        )

    def builtin_list(self, category: str = "") -> str:
        now = self._now()
        events = list(self.builtin_manager.load_raw().get("events") or [])
        category_filter = parse_category_input(category)
        if category_filter:
            events = [item for item in events if item.get("category") == category_filter]
        if not events:
            return (
                f"无内置事件数据（year={now.year}，category={category or '全部'}）。\n"
                "如需重新生成：/calendar builtin_regen"
            )
        events.sort(key=lambda item: (item.get("month", 0), item.get("day", 0)))
        lines = [f"[{now.year} 内置事件 {len(events)} 条]"]
        for item in events:
            lines.append(
                f"  {item.get('month', 0):02d}-{item.get('day', 0):02d} "
                f"{item.get('text', '')} [{category_short_tag(item.get('category', ''))}]"
            )
        return "\n".join(lines)
