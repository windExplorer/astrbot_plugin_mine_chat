"""v1.4.0 模型解析自检：model_for 优先级 + describe_provider 来源/4.29.x coroutine bug。

纯函数驱动，不依赖 astrbot 运行时：用 AST 把 llm.py 的定义抠到带桩命名空间执行。
用法：python tests/test_model_resolution.py
"""
from __future__ import annotations

import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from test_core_logic import Checker, _QuietLogger, load_defs  # noqa: E402

import types  # noqa: E402


def _load_llm():
    ns = load_defs(
        "llm.py",
        {"model_for", "_provider_identity", "describe_provider", "resolve_provider",
         "LLMError", "_CODE_BLOCK_RE", "DEFAULT_TIMEOUT"},
        {"logger": _QuietLogger(), "asyncio": __import__("asyncio"),
         "json": __import__("json"), "re": __import__("re"), "Any": object},
    )
    return ns


class _Meta:
    def __init__(self, id, model):
        self.id = id
        self.model = model


class _Provider:
    def __init__(self, id, model):
        self._meta = _Meta(id, model)

    def meta(self):
        return self._meta

    async def text_chat(self, **kwargs):
        return None


class _Settings:
    def __init__(self, default="", schedule="", proactive=""):
        self.llm_default_model = default
        self.schedule_model = schedule
        self.proactive_model = proactive


class _Ctx:
    """模拟 context：get_provider_by_id 同步；provider_manager 上的是 async（4.29.x 的坑）。"""

    def __init__(self, by_id=None, system=None, pm_async=None):
        self._by_id = by_id or {}
        self._system = system
        pm = types.SimpleNamespace()
        if pm_async is not None:
            pm.get_provider_by_id = pm_async
        self.provider_manager = pm
        self._get_using = None
        self._get_using_async = None

    def get_provider_by_id(self, model_id):
        return self._by_id.get(model_id)

    def get_using_provider(self, umo=None):
        return self._system

    async def get_using_provider_async(self, umo=None):
        return self._system


def main():
    c = Checker()
    ns = _load_llm()
    model_for = ns["model_for"]
    describe_provider = ns["describe_provider"]

    print("\n[model_for] 优先级")
    c.equal("全空返回空(跟随系统)", model_for(_Settings(), "schedule"), "")
    c.equal("统一默认生效", model_for(_Settings(default="dm"), "schedule"), "dm")
    c.equal("日程覆盖优先于统一默认",
            model_for(_Settings(default="dm", schedule="sm"), "schedule"), "sm")
    c.equal("主动覆盖优先于统一默认",
            model_for(_Settings(default="dm", proactive="pm"), "proactive"), "pm")
    c.equal("日程不误用主动覆盖",
            model_for(_Settings(default="dm", proactive="pm"), "schedule"), "dm")
    c.equal("旧 Settings 无新字段也不炸", model_for(types.SimpleNamespace(schedule_model="x"), "schedule"), "x")

    async def _run():
        print("\n[describe_provider] 来源")
        explicit = _Provider("p1", "model-x")
        system = _Provider("p2", "model-y")
        ctx = _Ctx(by_id={"p1": explicit}, system=system)

        d = await describe_provider(ctx, umo="u1", model_id="p1")
        c.equal("显式 source", d["source"], "explicit")
        c.equal("显式 provider_id", d["provider_id"], "p1")
        c.equal("显式 model", d["model"], "model-x")
        c.check("显式拿到真 provider", d["provider"] is explicit)

        d = await describe_provider(ctx, umo="u1", model_id="")
        c.equal("跟随系统 source", d["source"], "system")
        c.equal("跟随系统 provider_id", d["provider_id"], "p2")
        c.check("跟随拿到系统 provider", d["provider"] is system)

        d = await describe_provider(_Ctx(by_id={"bad": "p1"}, system=None), model_id="bad")
        c.equal("解析异常回退系统无结果", d["source"], "none")
        c.check("无 provider", d["provider"] is None)

        # 4.29.x 的坑：provider_manager.get_provider_by_id 是 async，
        # 旧 llm.py 没 await 直接把它当 provider 用 → coroutine → .text_chat 炸。
        # 新实现只认 context.get_provider_by_id（同步），彻底绕开。
        async def async_getter(_id):
            return system

        ctx_bug = _Ctx(by_id={}, system=system, pm_async=async_getter)
        d = await describe_provider(ctx_bug, umo="u1", model_id="p2")
        c.equal("显式 id 不在同步表则回退系统", d["source"], "system")
        c.check("返回的不是 coroutine", asyncio.iscoroutine(d["provider"]) is False)

        d = await describe_provider(_Ctx(), model_id="missing")
        c.equal("找不到模型且无系统 → none", d["source"], "none")

    asyncio.run(_run())

    print(f"\nRESULT: checks={c.count} failures={len(c.failures)}")
    for f in c.failures:
        print(f"  - {f}")
    return 1 if c.failures else 0


if __name__ == "__main__":
    sys.exit(main())
