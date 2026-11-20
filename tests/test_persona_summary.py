"""人设预萃取的静默失效面：缓存不命中会反复烧调用、失败必须显式回退而不是空摘要冒充成功。"""

import asyncio
import json
import pathlib

import pytest

from time_awareness.core.persona_summary_store import PersonaSummaryStore
from time_awareness.llm.persona_summary import (
    SUMMARY_REVISION,
    normalize_summary,
    parse_summary,
    persona_fingerprint,
    render_summary,
)
from time_awareness.services.daily_schedule_generation import DailyScheduleGeneration
from time_awareness.services.persona_summary_service import PersonaSummaryService

PERSONA = (
    "## 0. 规则\n不要用 emoji，不要堆符号。\n"
    "## 1. 自我认知\n示例角色：夜班灯塔看守，怕热。\n"
    "## 2. 外貌\n深色外套、围巾、帆布包。\n"
    "## 3. 心理机制\n嘴硬行动派，心情不好会一个人待着。\n"
    "## 4. 话术示例\n“灯还亮着。”“别碰我的记录本。”\n"
    "## 5. 好感度\n等级 0 失望 → 等级 5 挚爱。\n"
    "## 6. 人际关系\n同事甲、岸边渔民。\n"
    "## 7. 执行约束\n不越狱、不暴露设定。\n"
)
SUMMARY_JSON = json.dumps(
    {
        "identity": "驱逐领舰",
        "place": "港区、练习场",
        "routine_anchors": ["日常在练习场练习（推断）"],
        "body": "怕热",
        "traits": ["嘴硬"],
        "interests": ["翻旧邮件"],
        "social": ["和同志酱同住"],
        "taboos": ["不能久站"],
        "concrete_props": ["沙发", "邮件"],
    },
    ensure_ascii=False,
)


class _Resp:
    def __init__(self, text, role="assistant"):
        self.completion_text = text
        self.role = role


class _Ctx:
    """记录调用次数；可切换为失败，模拟 provider 异常。"""

    def __init__(self, *, reply=SUMMARY_JSON, fail=False):
        self.calls = 0
        self.reply = reply
        self.fail = fail

    async def llm_generate(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider boom")
        return _Resp(self.reply)


def _config(*, enabled=True, provider="", max_attempts=3):
    return {
        "runtime": {"llm_max_attempts": max_attempts},
        "daily_schedule": {
            "ai_daily": {
                "enabled": True,
                "provider_id": "gen-model@none",
                "persona_summary_enabled": enabled,
                "persona_summary_provider_id": provider,
            }
        },
    }


def _service(tmp_path, ctx, **kwargs):
    store = PersonaSummaryStore(str(tmp_path))
    store.load()
    return PersonaSummaryService(context=ctx, config=_config(**kwargs), store=store), store


def test_fingerprint_ignores_surrounding_whitespace():
    # 写缓存与读缓存若对「是否 strip」不一致，缓存永不命中，只有账单能看出来
    assert persona_fingerprint("  " + PERSONA + "\n") == persona_fingerprint(PERSONA)


def test_parse_summary_tolerates_fence_and_trailing_comma():
    # 解析过严会把"能用的摘要"判成失败，白烧一次调用
    fenced = f"好的，结果如下：\n```json\n{SUMMARY_JSON}\n```"
    assert parse_summary(fenced)["identity"] == "驱逐领舰"
    trailing = '{"identity": "x", "traits": ["y",],}'
    assert parse_summary(trailing)["traits"] == ["y"]
    with pytest.raises(ValueError):
        parse_summary("完全不是 JSON")


def test_render_summary_keeps_anchors_without_truncation():
    summary = normalize_summary(json.loads(SUMMARY_JSON))
    text = render_summary(summary)
    assert "作息锚点" in text and "练习场" in text


def test_summary_must_actually_compress():
    """摘要达到原人设 80% 就算没压（只是复述），按失败处理——否则白花一次调用。"""
    from time_awareness.llm.persona_summary import MAX_SUMMARY_RATIO, summary_is_compressed

    persona = "人" * 1000
    assert summary_is_compressed("短" * 100, persona) is True
    assert summary_is_compressed("长" * int(1000 * MAX_SUMMARY_RATIO), persona) is False
    assert summary_is_compressed("长" * (int(1000 * MAX_SUMMARY_RATIO) - 1), persona) is True
    assert summary_is_compressed("任何内容", "") is False


def test_cache_hit_avoids_second_llm_call(tmp_path):
    # 缓存不生效 = 每天重新萃取一次，只有账单能看出来
    async def scenario():
        ctx = _Ctx()
        service, store = _service(tmp_path, ctx)
        first = await service.ensure(PERSONA, fallback_provider_id="gen-model@none")
        second = await service.ensure(PERSONA, fallback_provider_id="gen-model@none")
        return ctx, first, second, store

    ctx, first, second, store = asyncio.run(scenario())
    assert ctx.calls == 1
    assert first.status == "created" and second.status == "cache"
    assert store.get(persona_fingerprint(PERSONA), SUMMARY_REVISION) is not None
    # 重新加载后仍然命中（落盘有效）
    reloaded = PersonaSummaryStore(str(tmp_path))
    reloaded.load()
    assert reloaded.get(persona_fingerprint(PERSONA), SUMMARY_REVISION) is not None


def test_persona_or_revision_change_invalidates_cache(tmp_path):
    async def scenario():
        ctx = _Ctx()
        service, store = _service(tmp_path, ctx)
        await service.ensure(PERSONA, fallback_provider_id="m")
        changed_persona = PERSONA + "\n新增一句：每天要吃药。"
        await service.ensure(changed_persona, fallback_provider_id="m")
        return ctx, store

    ctx, store = asyncio.run(scenario())
    assert ctx.calls == 2  # 人设改了必须重萃
    # 提示词版本不匹配视为未命中
    assert store.get(persona_fingerprint(PERSONA), "0") is None


def test_service_returns_failed_instead_of_empty_summary(tmp_path):
    # 失败必须让调用方回退全量人设；返回空串冒充成功会让人设整段丢失
    async def scenario():
        ctx = _Ctx(fail=True)
        service, _ = _service(tmp_path, ctx)
        return await service.ensure(PERSONA, fallback_provider_id="m")

    result = asyncio.run(scenario())
    assert result.status == "failed" and not result.usable
    assert "provider boom" in result.error


def test_follows_generation_provider_unless_overridden(tmp_path):
    async def scenario():
        ctx = _Ctx()
        follow, _ = _service(tmp_path, ctx)
        override, _ = _service(tmp_path, ctx, provider="extract-model@none")
        return follow.provider_id("gen@none"), override.provider_id("gen@none")

    assert asyncio.run(scenario()) == ("gen@none", "extract-model@none")


def test_generation_uses_summary_and_falls_back_on_failure(tmp_path):
    """生成侧接线：有摘要用摘要、失败用全量——这两条都不能静默走错分支。"""

    class _Persona:
        prompt = PERSONA

    def build(summary_service):
        gen = DailyScheduleGeneration(
            context=type("C", (), {"llm_generate": None})(),
            config=_config(),
            time_context=None,
            persona_resolver=None,
        )
        gen.set_persona_summary_service(summary_service)
        return gen

    async def scenario():
        ok_ctx = _Ctx()
        ok_service, _ = _service(tmp_path / "ok", ok_ctx)
        ok_prompt = await build(ok_service)._resolve_persona_prompt("s", _Persona())

        bad_ctx = _Ctx(fail=True)
        bad_service, _ = _service(tmp_path / "bad", bad_ctx)
        bad_prompt = await build(bad_service)._resolve_persona_prompt("s", _Persona())

        off_service, _ = _service(tmp_path / "off", _Ctx(), enabled=False)
        off_prompt = await build(off_service)._resolve_persona_prompt("s", _Persona())
        return ok_prompt, bad_prompt, off_prompt

    ok_prompt, bad_prompt, off_prompt = asyncio.run(scenario())
    assert "作息锚点" in ok_prompt and len(ok_prompt) < len(PERSONA) + 400
    assert bad_prompt == PERSONA.strip()  # 回退全量（extract 会 strip）
    assert off_prompt == PERSONA.strip()  # 关闭时不动人设
    assert "不要用 emoji" in bad_prompt


def test_summary_report_explains_missing_cache(tmp_path):
    async def scenario():
        service, _ = _service(tmp_path, _Ctx())
        return service.describe(PERSONA), service.describe(PERSONA + "改动")

    missing_before, still_missing = asyncio.run(scenario())
    assert "还没有摘要缓存" in missing_before
    assert "还没有摘要缓存" in still_missing


def test_short_persona_skips_extraction(tmp_path):
    """人设过短时不该花一次调用——提炼不出作息信息，还得回退全量人设。"""
    async def scenario():
        ctx = _Ctx()
        service, _ = _service(tmp_path, ctx)
        short = "人设" * 5
        result = await service.ensure(short, fallback_provider_id="m")
        return ctx, result

    ctx, result = asyncio.run(scenario())
    assert result.status == "too_short" and not result.usable
    assert ctx.calls == 0


def test_uncompressed_summary_is_treated_as_failure(tmp_path):
    """模型把原人设复述回来时，必须判失败回退全量人设，不能把废摘要存进缓存。"""

    class _VerboseCtx(_Ctx):
        async def llm_generate(self, **kwargs):
            self.calls += 1
            # 返回一份「没压缩」的摘要：所有字段都塞满原文
            return _Resp(
                json.dumps(
                    {
                        "identity": "很长的身份描述" * 40,
                        "place": "很长的场所描述" * 40,
                        "routine_anchors": ["锚点" * 40],
                        "body": "体质" * 40,
                        "traits": ["性格" * 40],
                        "interests": ["兴趣" * 40],
                        "social": ["社交" * 40],
                        "taboos": ["限制" * 40],
                        "concrete_props": ["物件" * 40],
                    },
                    ensure_ascii=False,
                )
            )

    async def scenario(attempts):
        ctx = _VerboseCtx()
        service, store = _service(tmp_path / str(attempts), ctx, max_attempts=attempts)
        result = await service.ensure(PERSONA, fallback_provider_id="m")
        return ctx, result, store

    # 重试次数完全跟随 runtime.llm_max_attempts（配 2 就打 2 次、配 1 就打 1 次）
    ctx, result, store = asyncio.run(scenario(2))
    assert ctx.calls == 2
    assert not result.usable
    assert "未压缩" in result.error
    assert store.entry(persona_fingerprint(PERSONA)) is None

    ctx1, result1, _ = asyncio.run(scenario(1))
    assert ctx1.calls == 1
    assert not result1.usable


def test_zero_attempts_still_runs_once(tmp_path):
    """max_attempts=0 表示「跟随 AstrBot 默认重试」，不是「一次都不打」——循环至少要跑一次。"""

    async def scenario():
        ctx = _Ctx(fail=True)
        service, _ = _service(tmp_path, ctx, max_attempts=0)
        result = await service.ensure(PERSONA, fallback_provider_id="m")
        return ctx, result

    ctx, result = asyncio.run(scenario())
    assert ctx.calls == 1
    assert not result.usable


def test_store_evicts_oldest_beyond_cap(tmp_path):
    """条目上限失效 = 人设每改一次就多一条、永不回收（磁盘静默增长）。"""
    from time_awareness.core.persona_summary_store import MAX_ENTRIES

    store = PersonaSummaryStore(str(tmp_path))
    store.load()
    for i in range(MAX_ENTRIES + 1):
        store.put(f"fp{i:03d}", summary={"identity": str(i)}, revision="1", model="m", chars=1)

    reloaded = PersonaSummaryStore(str(tmp_path))
    reloaded.load()
    assert len(reloaded._entries) == MAX_ENTRIES
    assert reloaded.entry("fp000") is None  # 最旧的被淘汰
    assert reloaded.entry(f"fp{MAX_ENTRIES:03d}") is not None  # 最新的保留


def test_cache_write_failure_is_reported(tmp_path, monkeypatch):
    """写盘失败被当成成功 → 每轮都重新萃取（只有账单能看出来），必须显式标记 saved=False。"""
    from time_awareness.core import persona_summary_store as store_mod

    monkeypatch.setattr(store_mod, "atomic_write_yaml", lambda *a, **k: False)

    async def scenario():
        ctx = _Ctx()
        service, store = _service(tmp_path, ctx)
        result = await service.ensure(PERSONA, fallback_provider_id="m")
        return result, store

    result, _store = asyncio.run(scenario())
    assert result.usable is True  # 本次仍能用摘要
    assert result.saved is False  # 但明确标记没落盘

    # 磁盘是真相：内存里留着，重启后仍是未命中（所以必须报出来）
    reloaded = PersonaSummaryStore(str(tmp_path))
    reloaded.load()
    assert reloaded.entry(persona_fingerprint(PERSONA)) is None


def test_persona_summary_disabled_by_default(tmp_path):
    """默认关闭（opt-in）：配置里没有该键时不该花一次萃取调用。

    同时锁住 schema 与代码的默认值一致——两边不一致时用户勾选的开关会被静默忽略。
    """
    import json as _json

    schema = _json.loads(
        (pathlib.Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
            encoding="utf-8"
        )
    )
    schema_default = schema["daily_schedule"]["items"]["ai_daily"]["items"][
        "persona_summary_enabled"
    ]["default"]
    assert schema_default is False

    async def scenario():
        ctx = _Ctx()
        service = PersonaSummaryService(
            context=ctx,
            config={"daily_schedule": {"ai_daily": {}}},
            store=None,
        )
        assert service.enabled() is False
        result = await service.ensure(PERSONA, fallback_provider_id="m")
        return ctx, result

    ctx, result = asyncio.run(scenario())
    assert result.status == "disabled" and not result.usable
    assert ctx.calls == 0  # 没有任何 LLM 调用
