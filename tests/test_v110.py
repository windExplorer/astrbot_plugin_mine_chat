"""v1.1.0 新增能力的纯函数自检：不依赖 astrbot 运行时。

用法：
    python tests/test_v110.py        # 退出码 0 即全部通过

覆盖四块：
1. 语音文本清洗（bridge.clean_tts_text）——括号动作/颜文字/分隔符不能念出来；
2. 人格提示词的生活向裁剪（prompts.extract_life_profile / extract_chat_profile）
   与喜好/雷区提取——这是「日程不贴人设」的根因所在地；
3. 每日基调与主线（schedule.daily_flavor）——确定性 + 跨天不同；
4. 主动消息上下文回填（proactive_gen.ProactiveComposer.ensure_echo_in_context）
   与注入块（schedule_view.build_injection_block）——这是「她答非所问、说没发过」
   的修复点，必须锁住行为。

沿用 test_core_logic 的 AST 抠函数思路：测的是磁盘上的真代码，不是复制品。
"""

from __future__ import annotations

import os
import random
import sys
import time
import types
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from test_core_logic import Checker, _QuietLogger, load_defs  # noqa: E402

import config as config_mod  # noqa: E402
import daylight as daylight_mod  # noqa: E402


# --------------------------------------------------------------------------- #
# 1. 语音文本清洗
# --------------------------------------------------------------------------- #
def test_tts_text(c: Checker) -> None:
    print("\n[1] 语音文本清洗")
    ns = load_defs(
        "bridge.py",
        {
            "clean_tts_text",
            "_BRACKET_RE",
            "_MARKUP_RE",
            "_ELLIPSIS_RE",
            "_KAOMOJI_RE",
        },
        {"re": __import__("re")},
    )
    clean = ns["clean_tts_text"]

    c.equal(
        "括号里的动作不念出来",
        clean("（轻轻别过脸）没睡。"),
        "没睡。",
    )
    c.equal(
        "半角括号同样处理",
        clean("(把杯子放下) …水凉了。"),
        "…水凉了。",
    )
    c.check(
        "分段符号不念成「横线横线横线」",
        "---" not in clean("下雨了\n---\n刚醒"),
    )
    c.check(
        "多段合成一句，保留内容",
        "下雨了" in clean("下雨了\n---\n刚醒") and "刚醒" in clean("下雨了\n---\n刚醒"),
    )
    c.check("markdown 星号被清掉", "*" not in clean("**今天**有点冷"))
    c.equal("纯颜文字视为无内容", clean("（＞﹏＜）"), "")
    c.equal("空串", clean(""), "")
    c.check("长度有上限", len(clean("啊" * 500)) <= 220)


# --------------------------------------------------------------------------- #
# 2. 人格提示词裁剪 / 偏好提取
# --------------------------------------------------------------------------- #
_PERSONA = """你是霜岛绫（Shimajima Aya），18岁，独居雪国女孩，父母在外地工作。

【外貌】
纯黑长直发，深棕眼眸。

【作息】
昼伏夜出，凌晨三四点睡、中午前后起。

【性格底色】
疏离、慢热、怕麻烦、边界感极强。

【消息发送设定（QQ/聊天场景）】
- 一条消息只说一到两句话。
- 分段铁律：必须用空行把每句话分开。

【纯文本与格式铁律】
- 不用任何 markdown。

【喜好】雪、安静、热可可、旧书、夜间城市、猫

【雷区】吵闹人群、被突然触碰、被迫社交

【关系解锁（核心：随信任加深逐层变化）】
- L0 陌生人：极简回应。

【画图 / 拍照 / 自拍 / 发图片 —— 必须使用 comfyui-draw 技能】
- 一律走技能。
"""


def test_persona_profile(c: Checker) -> None:
    print("\n[2] 人格提示词裁剪与偏好提取")
    ns = load_defs(
        "prompts.py",
        {
            "split_persona_sections",
            "extract_life_profile",
            "extract_chat_profile",
            "extract_preferences",
            "extract_life_profile",
            "_LIFE_SECTION_KEEP",
            "_LIFE_SECTION_DROP",
            "_CHAT_SECTION_DROP",
            "_SECTION_RE",
            "_section_kept",
            "_section_items",
            "_SPLIT_WORDS_RE",
        },
        {"re": __import__("re")},
    )
    life = ns["extract_life_profile"]
    chat = ns["extract_chat_profile"]
    prefs = ns["extract_preferences"]

    profile = life(_PERSONA)
    c.check("保留身份首段", "独居雪国女孩" in profile)
    c.check("保留作息", "昼伏夜出" in profile)
    c.check("保留性格底色", "疏离" in profile)
    c.check("保留喜好", "热可可" in profile)
    c.check("保留雷区", "吵闹人群" in profile)
    c.check("丢掉消息发送设定", "分段铁律" not in profile)
    c.check("丢掉格式铁律", "不用任何 markdown" not in profile)
    c.check("丢掉关系解锁表", "L0 陌生人" not in profile)
    c.check("丢掉画图流程", "comfyui" not in profile.lower())

    chat_profile = chat(_PERSONA)
    c.check("主动消息版保留分段铁律", "分段铁律" in chat_profile)
    c.check("主动消息版保留格式铁律", "不用任何 markdown" in chat_profile)
    c.check("主动消息版仍丢掉关系解锁", "L0 陌生人" not in chat_profile)
    c.check("主动消息版仍丢掉画图流程", "comfyui" not in chat_profile.lower())

    likes, dislikes = prefs(_PERSONA)
    c.check("喜好解析出热可可", "热可可" in likes, f"likes={likes}")
    c.check("喜好解析出猫", "猫" in likes, f"likes={likes}")
    c.check("雷区解析出吵闹人群", "吵闹人群" in dislikes, f"dislikes={dislikes}")

    # 没有分段结构时退化到原文截断，不能返回空
    c.check("无分段结构时退回原文", "随便一段没有小标题的设定" in life("随便一段没有小标题的设定"))
    c.equal("空输入", life(""), "")


# --------------------------------------------------------------------------- #
# 3. 每日基调 / 主线
# --------------------------------------------------------------------------- #
def test_daily_flavor(c: Checker) -> None:
    print("\n[3] 每日基调与主线")
    ns = load_defs(
        "schedule.py",
        {"daily_flavor", "_PACE_TIERS", "_THEME_POOL"},
        {"random": random},
    )
    flavor = ns["daily_flavor"]
    likes = ["热可可", "旧书", "猫", "雪"]
    dislikes = ["吵闹人群", "被突然触碰"]

    first = flavor("霜岛绫", "2026-11-15", likes, dislikes)
    again = flavor("霜岛绫", "2026-11-15", likes, dislikes)
    c.equal("同一天确定性一致", first, again)
    c.check("基调非空", bool(first["pace"]))
    c.check("主线非空", bool(first["theme"]))
    c.check("喜好物取自角色喜好", set(first["life_items"]) <= set(likes), str(first["life_items"]))
    c.check("喜好物有 2~3 项", 2 <= len(first["life_items"]) <= 3, str(first["life_items"]))
    c.check("雷区进了 avoid", "吵闹人群" in first["avoid_items"])

    # 连续 14 天：主线不能天天一样（同一套主线池，随机抽也不该全撞）
    themes = {flavor("霜岛绫", f"2026-11-{day:02d}", likes, dislikes)["theme"] for day in range(1, 15)}
    c.check("两周内主线有明显变化", len(themes) >= 6, f"distinct={len(themes)}")
    paces = {flavor("霜岛绫", f"2026-12-{day:02d}", likes, dislikes)["pace"] for day in range(1, 15)}
    c.check("两周内基调不止一种", len(paces) >= 2, f"distinct={len(paces)}")

    empty = flavor("无喜好角色", "2026-11-15", [], [])
    c.equal("没有喜好时喜好物为空", empty["life_items"], [])


# --------------------------------------------------------------------------- #
# 4. 主动消息回填与注入块
# --------------------------------------------------------------------------- #
class _FakeStore:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    async def recent_proactive_messages(self, persona_id, limit=1):
        self.calls += 1
        return self.rows


class _FakeReq:
    def __init__(self, contexts=None):
        self.contexts = contexts


def test_echo_in_context(c: Checker) -> None:
    print("\n[4] 主动消息上下文回填")
    ns = load_defs(
        "proactive_gen.py",
        {"ProactiveComposer", "_norm_text", "USER_ECHO_PLACEHOLDER", "USER_PLACEHOLDER"},
        {"logger": _QuietLogger(), "time": time},
    )
    ensure = ns["ProactiveComposer"].__dict__["ensure_echo_in_context"]
    norm = ns["_norm_text"] if "_norm_text" in ns else None
    if norm is None:
        norm = ns["ProactiveComposer"].__dict__["_norm_text"].__func__
    placeholder = ns["USER_ECHO_PLACEHOLDER"]

    class _Fake:
        def __init__(self, rows):
            self.store = _FakeStore(rows)

        _norm_text = staticmethod(norm)

    now = time.time()
    composer = _Fake([{"ts": now - 120, "content": "楼下的关东煮到货了，买了两串"}])
    ensure_bound = types.MethodType(ensure, composer)

    req = _FakeReq([])
    added = __import__("asyncio").run(ensure_bound(req, "霜岛绫", 360))
    c.check("缺失时会补进上下文", bool(added))
    c.equal("补了 user 占位 + assistant 原话", [m["role"] for m in req.contexts], ["user", "assistant"])
    generator = __import__("asyncio").run(ensure_bound(req, "霜岛绫", 360))
    c.equal("已在上下文时不重复补", generator, "")
    c.check("占位明确写的不是对方说的", placeholder in str(req.contexts[0]["content"]))
    c.check("补进去的是原话", "关东煮" in str(req.contexts[1]["content"]))

    # 已有一条内容相同的 assistant 消息 → 不再补
    req2 = _FakeReq([{"role": "assistant", "content": "楼下的关东煮到货了，买了两串"}])
    c.equal("历史里已有则跳过", __import__("asyncio").run(ensure_bound(req2, "霜岛绫", 360)), "")
    c.equal("跳过时不改上下文长度", len(req2.contexts), 1)

    # 超出窗口 → 不补
    old = _Fake([{"ts": now - 10 * 3600, "content": "很久以前说过的话"}])
    old_bound = types.MethodType(ensure, old)
    req3 = _FakeReq([])
    c.equal("超出时间窗不补", __import__("asyncio").run(old_bound(req3, "霜岛绫", 360)), "")
    c.equal("超出时间窗不动上下文", len(req3.contexts), 0)

    # 窗口设为 0 → 直接关掉
    zero = _Fake([{"ts": now, "content": "刚刚说的"}])
    zero_bound = types.MethodType(ensure, zero)
    req4 = _FakeReq([])
    c.equal("窗口 0 视为关闭", __import__("asyncio").run(zero_bound(req4, "霜岛绫", 0)), "")
    c.equal("关闭时不查库", zero.store.calls, 0)

    # contexts 不是 list（None）时能自愈
    none_req = _FakeReq(None)
    c.check("contexts 为 None 也能补", bool(__import__("asyncio").run(ensure_bound(none_req, "霜岛绫", 360))))
    c.check("contexts 被修正成 list", isinstance(none_req.contexts, list) and len(none_req.contexts) == 2)

    # 拆行存储的内容也能被识别（archive 存的是多段拼接）
    multiline = _Fake([{"ts": now, "content": "下雨了\n---\n刚醒"}])
    ml_bound = types.MethodType(ensure, multiline)
    req5 = _FakeReq([{"role": "assistant", "content": "下雨了 --- 刚醒"}])
    c.equal("多段内容归一化后能识别", __import__("asyncio").run(ml_bound(req5, "霜岛绫", 360)), "")

    # 连发两次主动：两条都缺时都补上（按时间正序），最多两条
    twice = _Fake([
        {"ts": now - 60, "content": "刚醒，头有点沉"},
        {"ts": now - 3600, "content": "楼下的关东煮到货了"},
    ])
    twice_bound = types.MethodType(ensure, twice)
    req6 = _FakeReq([])
    __import__("asyncio").run(twice_bound(req6, "霜岛绫", 360))
    c.equal("两条都缺时补两对消息", len(req6.contexts), 4)
    c.check("按时间正序（先旧后新）", "关东煮" in str(req6.contexts[1]["content"]))
    c.check("较新的一条在后", "头有点沉" in str(req6.contexts[3]["content"]))

    # 其中一条已在上下文 → 只补缺的那条
    partial = _Fake([
        {"ts": now - 60, "content": "刚醒，头有点沉"},
        {"ts": now - 3600, "content": "楼下的关东煮到货了"},
    ])
    partial_bound = types.MethodType(ensure, partial)
    req7 = _FakeReq([{"role": "assistant", "content": "楼下的关东煮到货了"}])
    __import__("asyncio").run(partial_bound(req7, "霜岛绫", 360))
    c.equal("只补缺的那条", len(req7.contexts), 3)
    c.check("补的是缺的那条", "头有点沉" in str(req7.contexts[2]["content"]))


def test_injection_echo(c: Checker) -> None:
    print("\n[5] 注入块记住上一次主动消息")
    ns = load_defs(
        "schedule_view.py",
        {
            "build_injection_block",
            "locate",
            "block_text",
            "is_sleeping",
            "_WEEKDAY_NAMES",
            "_SLEEP_KEYWORDS",
            "now_minutes_for",
        },
        {
            "Settings": config_mod.Settings,
            "fmt_hhmm": config_mod.fmt_hhmm,
            "datetime": datetime,
            "date_cls": datetime,
            "daylight": daylight_mod,
            "time": time,
        },
    )
    build = ns["build_injection_block"]
    settings = config_mod.Settings.from_config({})
    items = [
        {"start_min": 780, "end_min": 1080, "activity": "下午在窗边看旧书", "mood": "安静",
         "message_seed": "书页里夹着一张旧纸条"},
    ]
    now = time.time()

    fresh = build(
        items=items,
        now_minutes=900,
        settings=settings,
        last_proactive="楼下的关东煮到货了，买了两串",
        unanswered=0,
        last_proactive_at=now - 600,
        now_ts=now,
    )
    c.check("用户已回复但窗口内仍注入", "关东煮" in fresh)
    c.check("明确标注是她自己发的", "你自己发出去的原话" in fresh)
    c.check("明确禁止否认", "绝不要说" in fresh and "没发过" in fresh)

    stale = build(
        items=items,
        now_minutes=900,
        settings=settings,
        last_proactive="很久以前发的",
        unanswered=0,
        last_proactive_at=now - 24 * 3600,
        now_ts=now,
    )
    c.check("超过窗口不再注入", "很久以前发的" not in stale)

    pending = build(
        items=items,
        now_minutes=900,
        settings=settings,
        last_proactive="很久以前发的",
        unanswered=2,
        last_proactive_at=now - 24 * 3600,
        now_ts=now,
    )
    c.check("未回复时无视窗口照样注入", "很久以前发的" in pending)

    no_ts = build(
        items=items,
        now_minutes=900,
        settings=settings,
        last_proactive="没有时间戳的消息",
        unanswered=1,
    )
    c.check("没有时间戳时按未回复兜底", "没有时间戳的消息" in no_ts)

    closed = config_mod.Settings.from_config({"inject": {"echo_window_minutes": 0}})
    off = build(
        items=items,
        now_minutes=900,
        settings=closed,
        last_proactive="窗口关掉后的消息",
        unanswered=0,
        last_proactive_at=now - 60,
        now_ts=now,
    )
    c.check("窗口 0 = 不再注入", "窗口关掉后的消息" not in off)


# --------------------------------------------------------------------------- #
# 6. 好感度快照文案
# --------------------------------------------------------------------------- #
def test_affinity_copy(c: Checker) -> None:
    print("\n[6] 好感度快照文案")
    ns = load_defs(
        "bridge.py",
        {"AffinitySnapshot", "stage_hint", "stage_initiative", "umo_user_id", "_STAGE_HINT", "_STAGE_INITIATIVE"},
        {"dataclass": __import__("dataclasses").dataclass,
         "field": __import__("dataclasses").field},
    )
    Snapshot = ns["AffinitySnapshot"]
    snap = Snapshot(
        role_id="霜岛绫",
        user_id="10001",
        score=320.0,
        stage="familiar",
        stage_label="熟悉",
        next_stage_label="亲近",
        progress=0.2,
        mood_description="心情不错",
        axes={"joy": 6, "anger": 1, "sorrow": 1, "fear": 1},
        mood_neutral=False,
        mood_enabled=True,
    )
    relation = snap.relation_line()
    c.check("关系行含档位名", "熟悉" in relation)
    c.check("关系行含分数", "320" in relation, relation)
    c.check("关系行给出人设层提示", "记住" in relation, relation)
    c.check("主动分寸非空", bool(snap.initiative_line()))
    mood = snap.mood_line()
    c.check("心情行含描述", "心情不错" in mood, mood)
    c.check("心情行含轴值", "愉悦" in mood, mood)

    neutral = Snapshot(role_id="r", stage="known", stage_label="眼熟", mood_enabled=True, mood_neutral=True,
                      mood_description="心情平静")
    c.equal("中性心情不注入", neutral.mood_line(), "")
    c.equal("没有角色时不拼关系行", Snapshot().relation_line(), "")

    c.equal("umo 取对方 id", ns["umo_user_id"]("aiocqhttp:FriendMessage:10001"), "10001")
    c.equal("空 umo", ns["umo_user_id"](""), "")


def main() -> int:
    c = Checker()
    test_tts_text(c)
    test_persona_profile(c)
    test_daily_flavor(c)
    test_echo_in_context(c)
    test_injection_echo(c)
    test_affinity_copy(c)
    print(f"\n共 {c.count} 项断言，失败 {c.failures.__len__()} 项")
    if c.failures:
        print("失败项：")
        for name in c.failures:
            print(f"  - {name}")
        print("RESULT: FAIL")
        return 1
    print("RESULT: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
