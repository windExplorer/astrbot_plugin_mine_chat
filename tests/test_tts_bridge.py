"""v1.3.0 语音联动切到 tts.v1 契约的自检（纯函数，不依赖 astrbot 运行时）。

沿用 test_core_logic 的 AST 抠函数思路：直接把 bridge.py 里的语音相关函数抽到
带桩的命名空间执行，验证：
1. 只认 moe_tts 的 star_cls.api + tts.v1 版本，任何一环缺失都安全降级；
2. synthesize 走契约拿 files（多段），失败/超时/异常都返回空列表，不外抛；
3. speak 逐段发 Record，任一段失败都返回 False 让上层补文字兜底。

用法：python tests/test_tts_bridge.py
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

PASS_LABEL = "tts_bridge"


def _load_bridge():
    """加载 bridge 的语音函数，注入桩依赖（logger/os/asyncio/常量/MessageChain）。"""
    ns = load_defs(
        "bridge.py",
        {
            "VOICE_PLUGIN", "VOICE_API_VERSION", "VOICE_TIMEOUT", "VOICE_CLEANUP_DELAY",
            "clean_tts_text", "_BRACKET_RE", "_MARKUP_RE", "_ELLIPSIS_RE", "_KAOMOJI_RE",
            "get_voice_api", "voice_available", "synthesize", "schedule_cleanup",
            "speak", "_message_chain",
        },
        {
            "logger": _QuietLogger(),
            "os": os,
            "asyncio": asyncio,
            "re": __import__("re"),
            "Any": object,
            "time": __import__("time"),
        },
    )
    # get_voice_plugin 是 get_voice_api 的别名赋值（ast.Assign），load_defs 已加载；
    # 确认常量正确，防止测试跑在过期代码上。
    return ns


class _FakeApi:
    def __init__(self, *, ok=True, files=None, error="", available=True,
                 version="tts.v1", raise_exc=False):
        self.API_VERSION = version
        self._ok = ok
        self._files = files or []
        self._error = error
        self._available = available
        self._raise = raise_exc
        self.cleaned = []
        self.calls = []

    def available(self):
        return self._available

    async def synthesize(self, text, voice="", **kwargs):
        self.calls.append((text, voice, kwargs))
        if self._raise:
            raise RuntimeError("boom")
        return {"ok": self._ok, "files": self._files, "error": self._error}

    def cleanup(self, path, delay=None):
        self.cleaned.append((path, delay))


class _FakeStar:
    def __init__(self, api, activated=True):
        self.activated = activated
        self.star_cls = types.SimpleNamespace(api=api)


def _context(star):
    def get_registered_star(name):
        return star if name == "astrbot_plugin_moe_tts" else None
    return types.SimpleNamespace(get_registered_star=get_registered_star)


def main():
    c = Checker()
    ns = _load_bridge()
    print(f"\n[{PASS_LABEL}] 常量")
    c.equal("语音插件标识", ns["VOICE_PLUGIN"], "astrbot_plugin_moe_tts")
    c.equal("契约版本", ns["VOICE_API_VERSION"], "tts.v1")
    get_voice_api = ns["get_voice_api"]
    voice_available = ns["voice_available"]
    synthesize = ns["synthesize"]

    print(f"\n[{PASS_LABEL}] get_voice_api 契约校验")
    api = _FakeApi()
    c.check("有契约返回 api", get_voice_api(_context(_FakeStar(api))) is api)
    c.check("没装返回 None", get_voice_api(_context(None)) is None)
    c.check("未激活返回 None", get_voice_api(_context(_FakeStar(api, activated=False))) is None)
    c.check("无 api 返回 None", get_voice_api(_context(_FakeStar(None))) is None)
    c.check("版本不符返回 None",
            get_voice_api(_context(_FakeStar(_FakeApi(version="tts.v0")))) is None)
    c.check("无 get_registered_star 返回 None", get_voice_api(object()) is None)

    print(f"\n[{PASS_LABEL}] voice_available")
    c.check("available=True", voice_available(_context(_FakeStar(_FakeApi(available=True)))) is True)
    c.check("available=False", voice_available(_context(_FakeStar(_FakeApi(available=False)))) is False)
    c.check("契约缺失 False", voice_available(_context(None)) is False)

    print(f"\n[{PASS_LABEL}] synthesize 走契约")
    # 用真实临时文件，让 os.path.isfile 校验通过
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p1 = os.path.join(td, "a.wav")
        p2 = os.path.join(td, "b.wav")
        for p in (p1, p2):
            with open(p, "wb") as f:
                f.write(b"RIFF")

        async def _run():
            fake = _FakeApi(ok=True, files=[p1, p2])
            paths = await synthesize(_context(_FakeStar(fake)), "（别过脸）你好呀", voice="萌1")
            c.equal("多段路径全返回", paths, [p1, p2])
            c.equal("调用一次契约", len(fake.calls), 1)
            c.equal("朗读文本已清洗", fake.calls[0][0], "你好呀")
            c.equal("音色透传", fake.calls[0][1], "萌1")
            c.check("pre_translated=True", fake.calls[0][2].get("pre_translated") is True)

            fail = _FakeApi(ok=False, files=[], error="排队超时")
            c.equal("合成失败返回空", await synthesize(_context(_FakeStar(fail)), "你好"), [])

            exc = _FakeApi(raise_exc=True)
            c.equal("契约异常返回空不外抛", await synthesize(_context(_FakeStar(exc)), "你好"), [])

            empty_text = _FakeApi(ok=True, files=[p1])
            c.equal("纯符号文案直接跳过", await synthesize(_context(_FakeStar(empty_text)), "（……）"), [])

            no_api = await synthesize(_context(None), "你好")
            c.equal("无契约返回空", no_api, [])

        asyncio.run(_run())

    print(f"\n[{PASS_LABEL}] 汇总")
    print(f"  CHECKS={c.count} FAILS={len(c.failures)}")
    if c.failures:
        for f in c.failures:
            print(f"    - {f}")
    return 1 if c.failures else 0


if __name__ == "__main__":
    sys.exit(main())
