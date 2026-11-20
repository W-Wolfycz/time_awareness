"""人设预萃取服务：缓存优先，缺失时调用一次 LLM；失败一律回退全量人设并留下 WARNING。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ..core.persona_summary_store import PersonaSummaryStore
from ..domain.schedule import ai_daily_config
from ..llm.persona_summary import (
    EXTRACT_SYSTEM,
    MIN_PERSONA_CHARS,
    SUMMARY_REVISION,
    normalize_summary,
    parse_summary,
    persona_fingerprint,
    render_summary,
    summary_field_lines,
    summary_is_compressed,
)
from .adaptive_policy import AdaptiveGenerationPolicy
from ..llm.daily_schedule_generator import request_kwargs
from ..log import logger, tag


@dataclass(frozen=True)
class SummaryResult:
    """一次「取摘要」的结果；``text`` 为空表示要用全量人设。"""

    status: str
    text: str = ""
    summary: dict | None = None
    model: str = ""
    error: str = ""
    fingerprint: str = ""
    saved: bool = True  # 缓存是否落盘；False 时本次仍用摘要，但下一轮会重萃

    @property
    def usable(self) -> bool:
        return bool(self.text)


class PersonaSummaryService:
    def __init__(
        self,
        *,
        context,
        config: dict,
        store: PersonaSummaryStore,
        concurrency_gate=None,
    ):
        self.context = context
        self.config = config
        self.store = store
        self._gate = concurrency_gate

    # ---------------- 配置 ----------------

    def _ai_daily(self) -> dict:
        return ai_daily_config(self.config)

    def enabled(self) -> bool:
        return bool(self._ai_daily().get("persona_summary_enabled", False))


    def provider_id(self, fallback: str) -> str:
        """萃取用的模型：留空跟随主对话模型。

        链路是 ``persona_summary_provider_id`` → ``ai_daily.provider_id`` →
        AstrBot 全局默认 Provider。单独配置是刻意的：生成用 flash、萃取用 pro
        是实测最优组合，两者思考档位需求不同（萃取开思考只会慢 10 倍且无收益）。
        """
        value = str(self._ai_daily().get("persona_summary_provider_id", "") or "").strip()
        return value or str(fallback or "").strip()

    # ---------------- 主流程 ----------------

    async def ensure(
        self,
        persona_prompt: str,
        *,
        fallback_provider_id: str,
        persona_label: str = "",
        force: bool = False,
    ) -> SummaryResult:
        """取（必要时新建）摘要。任何失败都返回 ``usable=False``，由调用方回退全量人设。"""
        prompt = str(persona_prompt or "").strip()
        if not self.enabled():
            return SummaryResult(status="disabled")
        if not prompt:
            return SummaryResult(status="empty_persona")
        if len(prompt) < MIN_PERSONA_CHARS:
            # 人设太短：提炼不出作息信息，直接用全量人设
            return SummaryResult(
                status="too_short",
                error=f"人设仅 {len(prompt)} 字（< {MIN_PERSONA_CHARS}）",
            )
        fingerprint = persona_fingerprint(prompt)
        if not force:
            cached = self.store.get(fingerprint, SUMMARY_REVISION)
            if cached:
                return SummaryResult(
                    status="cache",
                    text=render_summary(cached),
                    summary=cached,
                    fingerprint=fingerprint,
                )
        model = self.provider_id(fallback_provider_id)
        if not model:
            logger.warning(f"{tag()} ⚠️ 人设萃取跳过：AI 每日日程未配置可用模型")
            return SummaryResult(status="no_provider", fingerprint=fingerprint)

        error = ""
        configured = AdaptiveGenerationPolicy.from_config(self.config).effective().max_attempts
        attempts = max(1, configured)
        for attempt in range(1, attempts + 1):
            try:
                raw = await self._call_llm(model, prompt, max_attempts=configured)
                summary = normalize_summary(parse_summary(raw))
                text = render_summary(summary)
                if not text:
                    raise ValueError("解析后的摘要为空")
                if not summary_is_compressed(text, prompt):
                    # 没压缩 = 只是把原人设复述一遍，这种摘要没有价值
                    raise ValueError(
                        f"摘要未压缩（{len(text)} 字 / 原人设 {len(prompt)} 字）"
                    )
                saved = self.store.put(
                    fingerprint,
                    summary=summary,
                    revision=SUMMARY_REVISION,
                    model=model,
                    chars=len(text),
                    persona_label=persona_label,
                )
                if not saved:
                    logger.warning(
                        f"{tag()} ⚠️ 人设摘要写入失败：本次仍用摘要生成，"
                        "但下一轮会重新萃取（检查数据目录权限/磁盘）"
                    )
                logger.info(
                    f"{tag()} 🧬 人设摘要已生成: model={model} 字数={len(text)} "
                    f"锚点={len(summary.get('routine_anchors') or [])}"
                )
                return SummaryResult(
                    status="created",
                    text=text,
                    summary=summary,
                    model=model,
                    fingerprint=fingerprint,
                    saved=saved,
                )
            except Exception as exc:  # 网络/解析/校验，一律回退，不阻断生成
                error = f"{type(exc).__name__}: {str(exc)[:120]}"
                logger.warning(
                    f"{tag()} ⚠️ 人设萃取失败（第 {attempt}/{attempts} 次）: {error}"
                )
                if attempt < attempts:
                    await asyncio.sleep(1.5)
        logger.warning(
            f"{tag()} ⚠️ 人设萃取放弃，本次改用全量人设生成日程"
            f"（persona={persona_label or '-'}）"
        )
        return SummaryResult(status="failed", model=model, error=error, fingerprint=fingerprint)

    async def _call_llm(
        self, model: str, persona_prompt: str, *, max_attempts: int = 0
    ) -> str:
        held = False
        if self._gate is not None:
            policy = AdaptiveGenerationPolicy.from_config(self.config).effective()
            await self._gate.acquire_wait(policy.max_concurrent_llm)
            held = True
        try:
            response = await self.context.llm_generate(
                chat_provider_id=model,
                prompt=f"以下是角色提示词原文：\n\n{persona_prompt}",
                system_prompt=EXTRACT_SYSTEM,
                **request_kwargs(max_attempts),
            )
        finally:
            if held:
                await self._gate.release()
        if not response or getattr(response, "role", "") != "assistant":
            raise ValueError("LLM 响应角色异常")
        text = (getattr(response, "completion_text", "") or "").strip()
        if not text:
            raise ValueError("LLM 返回空内容")
        return text

    # ---------------- 展示 ----------------

    def describe(self, persona_prompt: str) -> str:
        """``/schedule summary`` 的正文：命中则展示字段，未命中给出下一步。"""
        prompt = str(persona_prompt or "").strip()
        if not prompt:
            return "无法取到当前人设文本（未配置 Persona，或 use_persona 已关闭）。"
        fingerprint = persona_fingerprint(prompt)
        entry = self.store.entry(fingerprint)
        if not entry:
            return (
                f"当前人设还没有摘要缓存（人设 {len(prompt)} 字）。\n"
                "下次生成日程时会自动萃取一次；也可以现在执行 /schedule summary_rebuild。"
            )
        summary = entry.get("summary") or {}
        stale = str(entry.get("revision", "")) != SUMMARY_REVISION
        summary_chars = max(1, int(entry.get("chars", 0) or 1))
        head = [
            f"[人设摘要 · {entry.get('chars', 0)} 字 · 模型 {entry.get('model') or '-'}"
            f" · {entry.get('created_at', '-')}]",
            f"原始人设 {len(prompt)} 字 → 摘要压缩比 {len(prompt) / summary_chars:.1f}×",
        ]
        if stale:
            head.append(
                f"⚠️ 该摘要由提示词版本 {entry.get('revision')} 生成，当前版本 {SUMMARY_REVISION}，"
                "下次生成会自动重萃"
            )
        return "\n".join(head + summary_field_lines(summary))
