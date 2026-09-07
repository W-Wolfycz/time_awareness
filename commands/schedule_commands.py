"""单日日程命令应用服务。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

from ..domain.schedule import (
    find_active_daily_slot,
    find_active_static_slot,
    sort_static_slots,
)
from ..log import logger, tag


class ScheduleCommands:
    def __init__(
        self,
        *,
        context: Any,
        config: dict,
        daily_schedule_service,
        now_provider: Callable[[], Any],
        save_config: Callable[[], None],
        check_overlap: Callable[[], None],
    ):
        self.context = context
        self.config = config
        self.daily_schedule_service = daily_schedule_service
        self._now = now_provider
        self._save_config = save_config
        self._check_overlap = check_overlap

    @staticmethod
    def help_text() -> str:
        return (
            "[time_awareness 日程表命令]\n"
            "/schedule show  — 查看当前 Persona 今天实际使用的日程\n"
            "/schedule static  — 查看静态日程表\n"
            "/schedule regenerate  — 重新生成当前 Persona 的今日 AI 日程（管理员）\n"
            "/schedule help  — 显示本帮助\n"
            "\n"
            "说明：\n"
            "- 命中时段的 state_prompt 通过 <SCHEDULE_STATE> 标签注入到当前轮 user 消息末尾\n"
            "- 开启 AI 每日日程后，按 Persona + 本地日期生成冻结快照（同一 Persona 的多个 Bot 共享）；未生成或失败时自动使用静态日程\n"
            "- 未启用日程表感知或未命中任何时段时返回「无固定安排（按人设自然演绎）」\n"
            "- 静态时段请在 time_awareness Plugin Page 的「静态日程」页面编辑\n"
            "\n"
            "时间格式：HH:MM（24 小时制）\n"
            "- end_time < start_time 视为跨午夜（如 22:00→08:00）\n"
            "- 时段按左闭右开 [start_time, end_time) 匹配：开始时刻计入，结束时刻不计入\n"
            "\n"
            "示例：\n"
            "/schedule show  — 查看今天实际命中的 AI/静态日程\n"
            "/schedule regenerate  — 立即重做今天的 AI 日程\n"
            "静态日程请在 time_awareness Plugin Page 编辑；AI 自动化请开启 daily_schedule.ai_daily"
        )

    @staticmethod
    def _preview(value: str, limit: int = 40) -> str:
        return value if len(value) <= limit else value[: limit - 3] + "..."

    def _static_slot_line(self, slot: dict, active: dict | None = None) -> str:
        name = str(slot.get("name", "") or "").strip()
        state = str(slot.get("state_prompt", "") or "").strip() or "(无状态描述)"
        marker = " ← 当前" if slot is active else ""
        return (
            f"  {slot.get('start_time', '')}-{slot.get('end_time', '')}{marker}  "
            f"{f'[{name}] ' if name else ''}{self._preview(state)}"
        )

    def show(self, session: str) -> str:
        daily_schedule = self.config.get("daily_schedule", {})
        if not isinstance(daily_schedule, dict):
            daily_schedule = {}
        enabled = bool(daily_schedule.get("enable_schedule", False))
        # 同步注册仅使用已缓存身份；未缓存时本轮回退静态日程，下次消息补生成。
        self.daily_schedule_service.register_session(session)
        now = self._now()
        snapshot = self.daily_schedule_service.get_snapshot_for_session(session, now=now)
        if snapshot:
            # 动态渲染有效时间线（不依赖 slots 缓存时效）
            slots = self.daily_schedule_service._render_snapshot(snapshot, now)
            source_label = "AI" if snapshot.get("source") == "ai" else "AI + 静态日程"
            active = find_active_daily_slot(slots, now)
            lines = [
                f"[今日日程 {now.date().isoformat()} · {source_label} · {len(slots)} 段]",
                f"生成时间：{snapshot.get('generated_at', '-')}",
            ]
            for slot in slots:
                name = str(slot.get("name", "") or "").strip()
                start = str(slot.get("start", "") or "").strip()
                end = str(slot.get("end", "") or "").strip()
                state = str(slot.get("state", "") or "").strip() or "(无状态描述)"
                marker = " ← 当前" if slot is active else ""
                lines.append(
                    f"  {start}-{end}{marker}  "
                    f"{f'[{name}] ' if name else ''}{self._preview(state)}"
                )
            return "\n".join(lines)

        slots = daily_schedule.get("schedule_templates") or []
        slots = [item for item in slots if isinstance(item, dict)] if isinstance(slots, list) else []
        if not slots:
            ai_status = (
                "；今日 AI 快照正在生成或尚不可用"
                if self.daily_schedule_service.enabled()
                else ""
            )
            return (
                f"[今日日程] 无可用时段（感知{'已启用' if enabled else '未启用'}{ai_status}）。\n"
                "请在 time_awareness Plugin Page 的「静态日程」页面添加时段。"
            )

        active = find_active_static_slot(slots, now)
        lines = [f"[今日日程 {now.date().isoformat()} · 静态日程 · {len(slots)} 段]"]
        if self.daily_schedule_service.enabled():
            lines.append("今日 AI 快照尚未就绪；已自动回退静态日程。")
            failure = self.daily_schedule_service.get_failure_for_session(session, now=now)
            if failure:
                detail = str(failure.get("detail", "") or "").strip()
                lines.append(
                    f"最近生成状态：{failure.get('error_type', 'unknown')}"
                    + (f" · {detail}" if detail else "")
                )
        for slot in sort_static_slots(slots):
            lines.append(self._static_slot_line(slot, active))
        return "\n".join(lines)

    def static(self) -> str:
        daily_schedule = self.config.get("daily_schedule", {})
        if not isinstance(daily_schedule, dict):
            daily_schedule = {}
        slots = daily_schedule.get("schedule_templates") or []
        slots = [item for item in slots if isinstance(item, dict)] if isinstance(slots, list) else []
        if not slots:
            return "[静态日程] 共 0 条。可在 time_awareness Plugin Page 添加静态日程时段。"
        lines = [f"[静态日程 共 {len(slots)} 条；AI 不会覆盖此配置]"]
        for slot in sort_static_slots(slots):
            lines.append(self._static_slot_line(slot))
        return "\n".join(lines)

    async def regenerate(self, session: str, *, event=None) -> AsyncIterator[str]:
        if not self.daily_schedule_service.enabled():
            logger.debug(f"{tag()} /schedule regenerate 被拒：AI 每日日程未启用")
            yield (
                "AI 每日日程尚未启用。请同时开启 daily_schedule.enable_schedule "
                "和 daily_schedule.ai_daily.enabled。"
            )
            return
        await self.daily_schedule_service.register_session_async(
            session, trigger=False, event=event
        )
        logger.debug(f"{tag()} /schedule regenerate 开始: session={session}")
        persona_name = await self.daily_schedule_service.resolve_session_persona_name(
            session, event=event
        )
        if persona_name:
            yield f"正在重新生成 Persona「{persona_name}」的今日 AI 日程，请稍候……"
        else:
            yield "正在重新生成当前 Persona 的今日 AI 日程，请稍候……"
        result = await self.daily_schedule_service.generate_for_session(session, force=True)
        if result.success:
            source_label = "纯 AI" if result.source == "ai" else "AI + 静态日程"
            logger.info(
                f"{tag()} /schedule regenerate 完成: slots={result.slot_count} source={result.source}"
            )
            yield (
                f"✅ 今日 AI 日程已更新：{result.slot_count} 段，来源 {source_label}。\n"
                "使用 /schedule show 查看当前结果。"
            )
            return
        old_snapshot = self.daily_schedule_service.get_snapshot_for_session(session)
        suffix = "旧快照仍保留。" if old_snapshot else "当前继续使用静态日程。"
        logger.warning(
            f"{tag()} /schedule regenerate 失败: error={result.error_type} message={result.message}"
        )
        yield f"生成失败：{result.message or result.error_type}。{suffix}"
