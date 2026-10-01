"""纯函数自检：不依赖 astrbot 运行时。

用法：
    python tests/test_core_logic.py        # 退出码 0 即全部通过

设计：schedule / proactive / schedule_view / scope 这些模块顶部 import 了
astrbot.api.logger，本机装不了完整 AstrBot，于是用 AST 从源文件里原样抠出
纯函数定义再 exec —— 测的仍然是磁盘上的真代码，而不是复制品。
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import random
import re
import sys
import tempfile
from datetime import date, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config as config_mod  # noqa: E402  (不依赖 astrbot，可直接导入)


class _QuietLogger:
    """吞掉日志的自检用 logger（被测函数内部 logger.info/warning 静默）。"""

    def __getattr__(self, name):
        def _sink(*args, **kwargs):
            pass

        return _sink


# --------------------------------------------------------------------------- #
# 从源码抠定义
# --------------------------------------------------------------------------- #
def load_defs(path: str, names: set[str], extra_ns: dict | None = None) -> dict:
    with open(os.path.join(ROOT, path), "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)

    picked = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in names:
                picked.append(node)
        elif isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if any(name in names for name in targets):
                picked.append(node)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id in names:
                picked.append(node)

    module = ast.Module(body=picked, type_ignores=[])
    ast.fix_missing_locations(module)

    namespace: dict = dict(extra_ns or {})
    exec(compile(module, path, "exec"), namespace)
    return namespace


class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.count = 0

    def check(self, label: str, condition: bool, detail: str = "") -> None:
        self.count += 1
        if condition:
            print(f"  [ok] {label}")
        else:
            print(f"  [FAIL] {label} {detail}")
            self.failures.append(label)

    def equal(self, label: str, actual, expected) -> None:
        self.check(label, actual == expected, f"-> actual={actual!r} expected={expected!r}")


# --------------------------------------------------------------------------- #
def test_config_tools(c: Checker) -> None:
    print("\n[1] config 时间工具")
    c.equal("parse_hhmm('07:30')", config_mod.parse_hhmm("07:30"), 450)
    c.equal("parse_hhmm('7:30')", config_mod.parse_hhmm("7:30"), 450)
    c.equal("parse_hhmm 非法回退", config_mod.parse_hhmm("乱写", "08:00"), 480)
    c.equal("fmt_hhmm(450)", config_mod.fmt_hhmm(450), "07:30")
    c.equal("fmt_hhmm 跨天取模", config_mod.fmt_hhmm(1500), "01:00")
    c.check("跨天窗口 23:00-08:30 命中 02:00", config_mod.in_time_window(120, 1380, 510))
    c.check("跨天窗口 23:00-08:30 不命中 12:00", not config_mod.in_time_window(720, 1380, 510))
    c.check("普通窗口命中", config_mod.in_time_window(600, 540, 720))
    c.check("start==end 视为不生效", not config_mod.in_time_window(600, 600, 600))


def test_settings(c: Checker) -> None:
    print("\n[2] Settings 归一")
    settings = config_mod.Settings.from_config({})
    c.equal("默认生成时间 07:30", settings.schedule_time_min, 450)
    c.equal("默认每日上限", settings.proactive_daily_limit, 6)
    c.equal("默认静默起点 23:00", settings.proactive_quiet_start_min, 1380)
    c.equal("默认静默终点 08:30", settings.proactive_quiet_end_min, 510)
    c.check("默认注入范围含私聊群聊", set(settings.inject_scopes) == {"private", "group"})

    dirty = config_mod.Settings.from_config(
        {
            "schedule_item_min": "9",
            "schedule_item_max": "4",
            "inject_scopes": ["private", "bogus"],
            "inject_mode": "nonsense",
            "schedule_time": "7:05",
            "schedule_forbidden": "- 不要写加班\n- 不要提到猫\n",
            "proactive_interval_seconds": 1,
        }
    )
    c.equal("item_max 被夹到 >= item_min", dirty.schedule_item_max, 9)
    c.equal("非法 scope 被剔除", dirty.inject_scopes, ("private",))
    c.equal("非法 inject_mode 回退 tail", dirty.inject_mode, "tail")
    c.equal("宽松时间格式解析", dirty.schedule_time_min, 425)
    c.equal("forbidden 按行拆解", dirty.schedule_forbidden, ["不要写加班", "不要提到猫"])
    c.equal("心跳下限 30s", dirty.proactive_interval_seconds, 30)

    # 显式配置驱动：active_persona 是启用前提，且兼容旧键 persona_override
    unselected = config_mod.Settings.from_config({})
    c.equal("未选人格", unselected.active_persona, "")
    c.check("未选人格 → 未启用", not unselected.persona_selected())
    legacy = config_mod.Settings.from_config({"persona_override": "旧人格"})
    c.equal("兼容旧键 persona_override", legacy.active_persona, "旧人格")
    c.check("旧键也视为已选", legacy.persona_selected())
    selected = config_mod.Settings.from_config({"active_persona": "小满"})
    c.check("新键生效", selected.persona_selected())

    # 表情包通道（与生图独立）：开关、独立概率与计数、旧键 token 兼容
    sticker = config_mod.Settings.from_config(
        {"proactive_meme_token": "旧token", "proactive_sticker_enabled": True}
    )
    c.equal("表情包 token 旧键兼容", sticker.proactive_sticker_token, "旧token")
    c.check("表情包开关", sticker.proactive_sticker_enabled)
    c.equal("表情包独立概率", sticker.proactive_sticker_probability, 0.15)
    c.equal("表情包独立日限", sticker.proactive_sticker_max_per_day, 2)
    c.equal("生图后端不再含表情包", sticker.proactive_image_backend, "local")

    # 嵌套分组读取（v1.0.4 起的配置结构）
    nested = config_mod.Settings.from_config(
        {"persona": {"active": "嵌套人格", "auto_bind": True}, "advanced": {"log_retention": 500}}
    )
    c.equal("嵌套 persona.active", nested.active_persona, "嵌套人格")
    c.equal("嵌套 persona.auto_bind", nested.window_auto_bind, True)
    c.equal("嵌套 advanced.log_retention", nested.log_retention, 500)

    # 自动迁移：平铺键 → 嵌套结构（幂等，旧键清除，新键已有值时不覆盖）
    flat: dict = {"inject_lookback_minutes": 60, "schedule_time": "08:15", "window_auto_bind": True}
    c.check("迁移发生了变更", config_mod.migrate_legacy_config(flat))
    c.equal("迁移后 inject.lookback", flat["inject"]["lookback_minutes"], 60)
    c.equal("迁移后 schedule.time", flat["schedule"]["time"], "08:15")
    c.equal("迁移后 persona.auto_bind", flat["persona"]["auto_bind"], True)
    c.check("旧键已清除", "inject_lookback_minutes" not in flat)
    migrated = config_mod.Settings.from_config(flat)
    c.equal("迁移后 Settings 生效", migrated.inject_lookback_minutes, 60)
    c.equal("迁移后时间解析", migrated.schedule_time_min, 8 * 60 + 15)
    c.check("幂等：再迁移无变更", not config_mod.migrate_legacy_config(flat))
    preexisting: dict = {"persona": {"active": "新值"}, "persona_override": "旧值"}
    config_mod.migrate_legacy_config(preexisting)
    c.equal("新键已有值时旧键不覆盖", preexisting["persona"]["active"], "新值")


def load_schedule():
    names = {
        "MIN_ITEM_MINUTES",
        "MAX_ITEM_MINUTES",
        "MIN_COVERAGE_MINUTES",
        "_FIXED_HOLIDAYS",
        "_STRICT_TIME_RE",
        "_to_minutes",
        "_ROUTINE_SLEEP_TIERS",
        "_ROUTINE_WAKE_TIERS",
        "_routine_offset",
        "daily_routine",
        "normalize_items",
        "validate_items",
        "detect_repetition",
        "fallback_items",
        "calendar_hint",
    }
    return load_defs(
        "schedule.py",
        names,
        {
            "parse_hhmm": config_mod.parse_hhmm,
            "Settings": config_mod.Settings,
            "date_cls": date,
            "re": re,
            "random": random,
        },
    )


def test_schedule(c: Checker) -> None:
    print("\n[3] 日程解析与校验")
    ns = load_schedule()
    normalize_items = ns["normalize_items"]
    validate_items = ns["validate_items"]
    detect_repetition = ns["detect_repetition"]
    fallback_items = ns["fallback_items"]
    calendar_hint = ns["calendar_hint"]

    raw = [
        {"time": "07:30", "end": "08:10", "activity": "起床吃早饭", "mood": "迷糊", "message_seed": "面包烤糊了"},
        {"time": "08:10", "end": "12:00", "activity": "上班", "mood": "一般", "message_seed": ""},
        {"time": "23:30", "end": "06:30", "activity": "睡觉", "mood": "", "message_seed": ""},
        {"time": "bad", "end": "09:00", "activity": "坏条目"},
        {"time": "10:00", "end": "10:02", "activity": "太短"},
        {"time": "09:00", "end": "09:00", "activity": "零长度"},
        "not-a-dict",
    ]
    items = normalize_items(raw)
    c.equal("坏条目被过滤后剩 3 条", len(items), 3)
    c.check("按开始时间排序", items[0]["start_min"] == 450)
    sleep_item = [item for item in items if item["activity"] == "睡觉"][0]
    c.equal("跨天 end 被补 1440", sleep_item["end_min"], 24 * 60 + 390)

    settings = config_mod.Settings.from_config({})
    issues, score = validate_items(items, settings)
    c.check("条目不足触发 quality", "quality" in issues, f"issues={issues}")
    c.check("质量分被扣减", score < 100)

    overlap_items = [
        {"start_min": 420, "end_min": 600, "activity": "a", "mood": "", "message_seed": "", "basis": [], "confidence": 0.9},
        {"start_min": 500, "end_min": 900, "activity": "b", "mood": "", "message_seed": "", "basis": [], "confidence": 0.9},
    ]
    issues2, _ = validate_items(overlap_items, settings)
    c.check("重叠被判为 time 问题", "time" in issues2, f"issues={issues2}")

    full = normalize_items(
        [
            {"time": "07:00", "end": "07:40", "activity": "起床", "message_seed": "水"},
            {"time": "07:40", "end": "12:00", "activity": "上午的事", "message_seed": "事"},
            {"time": "12:00", "end": "13:00", "activity": "午饭", "message_seed": "饭"},
            {"time": "13:00", "end": "18:00", "activity": "下午的事", "message_seed": "事"},
            {"time": "18:00", "end": "20:00", "activity": "晚饭与休息", "message_seed": "饭"},
            {"time": "20:00", "end": "23:30", "activity": "晚上", "message_seed": "晚"},
            {"time": "23:30", "end": "07:00", "activity": "睡觉", "message_seed": ""},
        ]
    )
    issues3, score3 = validate_items(full, settings)
    c.check("完整一天无 time 问题", "time" not in issues3, f"issues={issues3}")
    c.check("完整一天质量分达标", score3 >= 70, f"score={score3}")

    c.check("避重命中", detect_repetition(full, ["上午的事", "午饭", "晚上"]))
    c.check("避重未命中", not detect_repetition(full, ["别的", "无关"]))

    fallback = fallback_items(settings)
    c.check("兜底条目数 >= 4", len(fallback) >= 4, f"n={len(fallback)}")
    monotonic = all(
        fallback[i + 1]["start_min"] >= fallback[i]["end_min"] - 1
        for i in range(len(fallback) - 1)
    )
    c.check("兜底时序单调不重叠", monotonic)

    c.equal("周六判定", calendar_hint(date(2026, 10, 3)), "今天是周末。")
    c.equal("国庆判定", calendar_hint(date(2026, 10, 1)), "今天是国庆节。")
    c.equal("工作日判定", calendar_hint(date(2026, 9, 30)), "今天是工作日。")


def test_proactive(c: Checker) -> None:
    print("\n[4] 主动候选时间计算")
    ns = load_defs(
        "proactive.py",
        {"_MIN_OFFSET_MINUTES", "compute_next_offset_minutes"},
        {"Settings": config_mod.Settings, "is_sleeping": _is_sleeping_stub},
    )
    compute = ns["compute_next_offset_minutes"]
    settings = config_mod.Settings.from_config({})
    items = [
        {"start_min": 600, "end_min": 720, "activity": "上午的事"},
        {"start_min": 720, "end_min": 780, "activity": "午饭"},
        {"start_min": 1380, "end_min": 1860, "activity": "睡觉"},
    ]
    c.equal("当前 10:00 → 下一个片段 12:00 距 120 分钟", compute(items, 600, settings), 120)
    c.equal("当前 12:10 → 跳过睡眠段，用尾部兜底", compute(items, 730, settings), max(30, 1860 - 730 + 30))
    c.equal("空日程给 60 分钟", compute([], 700, settings), 60)


def _is_sleeping_stub(item) -> bool:
    if not item:
        return False
    return any(key in str(item.get("activity") or "") for key in ("睡", "入眠"))


def test_schedule_view(c: Checker) -> None:
    print("\n[5] 片段定位与注入文案")
    ns = load_defs(
        "schedule_view.py",
        {
            "_WEEKDAY_NAMES",
            "_SLEEP_KEYWORDS",
            "now_minutes_for",
            "locate",
            "is_sleeping",
            "block_text",
            "build_injection_block",
            "describe",
        },
        {
            "Settings": config_mod.Settings,
            "fmt_hhmm": config_mod.fmt_hhmm,
            "datetime": datetime,
            "date_cls": date,
        },
    )
    locate = ns["locate"]
    build_injection_block = ns["build_injection_block"]
    is_sleeping = ns["is_sleeping"]

    items = [
        {"start_min": 780, "end_min": 840, "activity": "午饭", "mood": "还行", "message_seed": "面太咸了"},
        {"start_min": 840, "end_min": 1080, "activity": "下午写东西", "mood": "专注", "message_seed": "卡在一个bug上"},
        {"start_min": 1380, "end_min": 1860, "activity": "睡觉", "mood": "", "message_seed": ""},
    ]
    slots = locate(items, 900)
    c.equal("当前片段命中下午", slots["current"]["activity"], "下午写东西")
    c.equal("上一条是午饭", slots["previous"]["activity"], "午饭")
    c.equal("下一条是睡觉", slots["upcoming"]["activity"], "睡觉")

    gap_slots = locate(items, 1200)
    c.check("空隙时 current 为空", gap_slots["current"] is None)
    c.equal("空隙时 previous 为下午", gap_slots["previous"]["activity"], "下午写东西")
    c.equal("空隙时 upcoming 为睡觉", gap_slots["upcoming"]["activity"], "睡觉")

    c.check("睡眠识别", is_sleeping(items[2]))
    c.check("非睡眠识别", not is_sleeping(items[0]))

    settings = config_mod.Settings.from_config({})
    block = build_injection_block(
        items=items,
        now_minutes=900,
        settings=settings,
        plan_date="2026-09-30",
        last_proactive="下午茶好难喝",
        unanswered=1,
    )
    c.check("注入含当前片段", "下午写东西" in block)
    c.check("注入含刚才", "午饭" in block)
    c.check("注入含碎片", "卡在一个bug上" in block)
    c.check("注入含上次主动", "下午茶好难喝" in block)
    c.check("注入含免责声明", "不是用户的经历" in block)
    c.check("注入长度受限", len(block) <= settings.inject_max_chars + 1, f"len={len(block)}")

    tight = config_mod.Settings.from_config({"inject_max_chars": 120})
    short_block = build_injection_block(items=items, now_minutes=900, settings=tight)
    c.check("窄限额被截断", len(short_block) <= 121, f"len={len(short_block)}")

    c.equal("空日程不注入", build_injection_block(items=[], now_minutes=0, settings=settings), "")


def test_scope_and_segments(c: Checker) -> None:
    print("\n[6] umo 解析与消息拆条")
    scope_ns = load_defs("scope.py", {"parse_umo", "umo_kind", "build_umo", "_INVALID_PERSONA_IDS"})
    parse_umo = scope_ns["parse_umo"]
    umo_kind = scope_ns["umo_kind"]

    c.equal("私聊 umo", parse_umo("aiocqhttp:FriendMessage:10001"), ("aiocqhttp", "private", "10001"))
    c.equal("群聊 umo", parse_umo("aiocqhttp:GroupMessage:20002")[1], "group")
    c.equal("平台名带 group 不影响判定", umo_kind("mygroup: FriendMessage:1"), "private")
    c.equal("非法 umo", umo_kind("bad-format"), "other")

    gen_ns = load_defs("proactive_gen.py", {"split_segments", "_SEGMENT_SEPARATOR", "MAX_SEGMENT_CHARS"})
    split_segments = gen_ns["split_segments"]

    c.equal("单条不拆", split_segments("在干嘛呢", 3), ["在干嘛呢"])
    c.equal(
        "按 --- 拆条",
        split_segments("刚吃完饭\n---\n有点撑", 3),
        ["刚吃完饭", "有点撑"],
    )
    c.equal(
        "超出上限被截断",
        split_segments("一\n---\n二\n---\n三\n---\n四", 2),
        ["一", "二"],
    )
    long_text = "字" * 300
    parts = split_segments(long_text, 3)
    c.check("超长单条被截断", len(parts) == 1 and len(parts[0]) <= 201, f"len={len(parts[0]) if parts else 0}")
    c.equal("空输入返回空", split_segments("   ", 3), [])


def test_images(c: Checker) -> None:
    print("\n[7] 主动消息配图")
    ns = load_defs(
        "proactive_gen.py",
        {
            "IMAGE_EXTENSIONS",
            "list_image_files",
            "choose_image_file",
            "image_desc_from_path",
        },
        {"os": os, "random": random},
    )
    list_image_files = ns["list_image_files"]
    image_desc_from_path = ns["image_desc_from_path"]
    choose_image_file = ns["choose_image_file"]

    import prompts as prompts_mod

    hint = prompts_mod.build_image_hint("在厨房做饭")
    c.check("hint 带画面线索", "在厨房做饭" in hint)
    # 新语义：不再要求「完全别提图」，而是要求文字与画面一致呼应、禁止矛盾情节
    c.check("hint 要求图文呼应", "呼应" in hint and "矛盾" in hint)
    fallback_hint = prompts_mod.build_image_hint("   ")
    c.check("空描述回退中性说法", "生活随拍" in fallback_hint)

    c.equal("文件名作画面线索", image_desc_from_path("X:/库/做饭.png"), "做饭")
    c.equal("纯数字名视为无语义", image_desc_from_path("20260930.jpg"), "")
    c.equal("空名", image_desc_from_path("  .png"), "")

    with tempfile.TemporaryDirectory() as tmp:
        for name in ("a.jpg", "b.PNG", "c.txt", "d.gif", "e.webp", "f"):
            with open(os.path.join(tmp, name), "wb") as handle:
                handle.write(b"x")
        os.makedirs(os.path.join(tmp, "sub"), exist_ok=True)
        with open(os.path.join(tmp, "sub", "g.png"), "wb") as handle:
            handle.write(b"x")

        files = list_image_files(tmp)
        names = sorted(os.path.basename(path) for path in files)
        c.equal("只收图片扩展名且不递归", names, ["a.jpg", "b.PNG", "d.gif", "e.webp"])
        c.check("choose 返回候选之一", os.path.basename(choose_image_file(files) or "") in names)
        c.equal("目录不存在返回空", list_image_files(os.path.join(tmp, "nope")), [])
        c.equal("空目录返回空", list_image_files(tmp) == [], False)
    c.equal("空路径返回空", list_image_files(""), [])


def test_config_paths(c: Checker) -> None:
    print("\n[8] 配置点路径解析与写回")
    ns = load_defs("webui_api.py", {"_resolve_spec", "_set_by_path"})
    resolve = ns["_resolve_spec"]
    set_by_path = ns["_set_by_path"]

    schema = {
        "enabled": {"type": "bool", "default": True},
        "persona": {"type": "object", "items": {"active": {"type": "string"}}},
    }
    c.check("组内点路径命中", resolve(schema, "persona.active") == {"type": "string"})
    c.equal("顶层键命中", resolve(schema, "enabled"), {"type": "bool", "default": True})
    c.check("组内不存在的键", resolve(schema, "persona.nope") is None)
    c.check("不存在的组", resolve(schema, "nope") is None)
    c.check("对标量键取子路径", resolve(schema, "enabled.x") is None)

    target: dict = {"enabled": True, "persona": {"active": "旧"}}
    c.check("写组内键", set_by_path(target, "persona.active", "新"))
    c.equal("组内值已更新", target["persona"]["active"], "新")
    c.check("写顶层键", set_by_path(target, "enabled", False))
    c.equal("顶层值已更新", target["enabled"], False)
    empty: dict = {}
    c.check("组不存在时自动建组", set_by_path(empty, "proactive.model", "m1"))
    c.equal("自动建组生效", empty["proactive"]["model"], "m1")
    c.check("非法层级拒绝", not set_by_path(empty, "a.b.c", 1))


def test_routine(c: Checker) -> None:
    print("\n[9] 每日作息浮动（偶尔熬夜赖床）")
    ns = load_schedule()
    daily_routine = ns["daily_routine"]
    base_start, base_end = 23 * 60 + 30, 7 * 60 + 30

    first = daily_routine(base_start, base_end, random.Random("routine:p:2026-10-01"))
    second = daily_routine(base_start, base_end, random.Random("routine:p:2026-10-01"))
    c.equal("同一天（同种子）作息一致", first["sleep_start"], second["sleep_start"])
    c.equal("同一天 note 一致", first["note"], second["note"])
    other = daily_routine(base_start, base_end, random.Random("routine:p:2026-10-02"))
    c.check(
        "不同日期作息大概率不同",
        first["sleep_start"] != other["sleep_start"]
        or first["sleep_end"] != other["sleep_end"],
    )

    # 60 个种子：偏移都落在档位范围内，note 与偏移档位严格一致
    ok = True
    for seed in range(60):
        r = daily_routine(base_start, base_end, random.Random(seed))
        shift, wake = r["sleep_shift"], r["wake_shift"]
        if not (-60 <= shift <= 140 and -40 <= wake <= 100):
            c.check(f"seed{seed} 偏移越界 (sleep={shift}, wake={wake})", False)
            ok = False
            break
        expect: list[str] = []
        if shift >= 70:
            expect.append("熬夜")
        elif shift >= 20:
            expect.append("比平时晚")
        elif shift <= -20:
            expect.append("比较早")
        if wake >= 50:
            expect.append("懒觉")
        elif wake >= 15:
            expect.append("赖床")
        elif wake <= -15:
            expect.append("比平时早")
        for word in expect:
            if word not in r["note"]:
                c.check(f"seed{seed} note 缺「{word}」（note={r['note']}）", False)
                ok = False
        for word in ("熬夜", "比平时晚", "比较早", "懒觉", "赖床", "比平时早"):
            if word in r["note"] and word not in expect:
                c.check(f"seed{seed} note 多出「{word}」（note={r['note']}）", False)
                ok = False
    if ok:
        c.check("60 个种子的偏移范围与 note 档位全部一致", True)

    # 兜底模板支持浮动作息，骨架仍单调不重叠
    settings = config_mod.Settings.from_config({})
    routine = daily_routine(base_start, base_end, random.Random(7))
    items = ns["fallback_items"](settings, routine["sleep_start"], routine["sleep_end"])
    monotonic = all(
        items[i + 1]["start_min"] >= items[i]["end_min"] - 1
        for i in range(len(items) - 1)
    )
    c.check("浮动作息下兜底骨架单调不重叠", monotonic)
    c.check("浮动作息下兜底骨架非空", len(items) >= 4)


def test_image_backends(c: Checker) -> None:
    print("\n[10] 配图后端（anima / 表情包）")
    ns = load_defs(
        "proactive_gen.py",
        {
            "ANIMA_SOURCE_TAG",
            "ANIMA_DRAW_TIMEOUT",
            "parse_anima_result",
            "_MEME_EXTS",
            "meme_cache_filename",
            "fetch_meme_image_via_plugin",
        },
        {"json": json, "re": re, "asyncio": asyncio, "logger": _QuietLogger()},
    )
    parse_anima_result = ns["parse_anima_result"]
    meme_cache_filename = ns["meme_cache_filename"]
    via_plugin = ns["fetch_meme_image_via_plugin"]

    # 跨插件取图：moe_meme 装了/没装/停用/旧版本/API 抛异常
    class _Inst:
        def __init__(self, path="/tmp/s.png", fail=False):
            self.path, self.fail = path, fail
            self.args = None

        async def api_random_sticker_path(self, character=""):
            self.args = character
            if self.fail:
                raise RuntimeError("boom")
            return self.path

    class _Meta:
        def __init__(self, inst=None, activated=True):
            self.star_cls, self.activated = inst, activated

    class _Ctx:
        def __init__(self, meta=None, boom=False):
            self.meta, self.boom = meta, boom

        def get_registered_star(self, name):
            if self.boom:
                raise RuntimeError("host changed")
            return self.meta

    def _run(ctx):
        return asyncio.run(via_plugin(ctx))

    def _unpack(ctx):
        path, url, note, no_api = asyncio.run(via_plugin(ctx))
        return path, url, note, no_api

    path, url, note, no_api = _unpack(_Ctx(_Meta(_Inst())))
    c.equal("跨插件取图：命中返回路径", path, "/tmp/s.png")
    c.check("命中时说明为空", note == "" and no_api is False)
    inst = _Inst()
    _unpack(_Ctx(_Meta(inst)))
    c.equal("跨插件取图默认全库随机（character 空串）", inst.args, "")
    path, url, note, no_api = _unpack(_Ctx(None))
    c.check("未安装：路径空 + 标记不可用", path is None and no_api and "未安装" in note)
    path, url, note, no_api = _unpack(_Ctx(_Meta(_Inst(), activated=False)))
    c.check("插件停用：标记不可用", path is None and no_api and "停用" in note)
    path, url, note, no_api = _unpack(_Ctx(_Meta(type("_NoApi", (), {})())))
    c.check("旧版本无 API：标记不可用", path is None and no_api and "v0.2.0" in note)
    path, url, note, no_api = _unpack(_Ctx(_Meta(_Inst(fail=True))))
    c.check("API 抛异常：不算不可用，说明带异常", path is None and not no_api and "boom" in note)
    class _EmptyDict:
        async def api_random_sticker(self, character=""):
            return {"path": None, "url": "", "character": "", "sticker_id": ""}

    path, url, note, no_api = _unpack(_Ctx(_Meta(_EmptyDict())))
    c.check("API 返回空：不算不可用", path is None and not no_api and "返回空" in note)
    path, url, note, no_api = _unpack(_Ctx(_Meta(_Inst()), boom=True))
    c.check("宿主接口异常：不算不可用", path is None and not no_api)

    ok_payload = json.dumps({"status": "ok", "image_paths": ["/tmp/a.png"]})
    c.equal("companion JSON 取路径", parse_anima_result(ok_payload), "/tmp/a.png")
    c.equal("dict 形态同样可解析", parse_anima_result({"status": "ok", "image_paths": ["x.png"]}), "x.png")
    c.check(
        "多张取第一张",
        parse_anima_result({"status": "ok", "image_paths": ["a.png", "b.png"]}) == "a.png",
    )
    c.check(
        "失败纯文本（无 JSON）返回 None",
        parse_anima_result("本次生图失败。请用一句话简短向用户说明生成遇到问题即可。") is None,
    )
    c.check("status 非 ok 返回 None", parse_anima_result({"status": "error"}) is None)
    c.check("空路径列表返回 None", parse_anima_result({"status": "ok", "image_paths": []}) is None)
    c.check("垃圾文本返回 None", parse_anima_result("not json at all") is None)

    c.equal("表情文件名清洗", meme_cache_filename("abc/123:x", "GIF"), "abc_123_x.gif")
    c.equal("非法扩展回退 png", meme_cache_filename("id1", "bmp"), "id1.png")
    c.equal("空 id 兜底", meme_cache_filename("", "png"), "sticker.png")
    c.equal("点开头扩展名", meme_cache_filename("id", ".gif"), "id.gif")

    # 绘图提示词生成（静默，含画风与语种/类型约束）
    import prompts as prompts_mod

    # 世界观/角色设定自动提取的解析
    sample = "【世界观】\n近未来都市，AI 与人类共存。\n\n【角色补充设定】\n喜欢熬夜画画。\n"
    w, ch = prompts_mod.parse_profile_text(sample)
    c.equal("世界观段解析", w, "近未来都市，AI 与人类共存。")
    c.equal("角色段解析", ch, "喜欢熬夜画画。")
    w, ch = prompts_mod.parse_profile_text("【世界观】\n（无）\n【角色补充设定】\n（无）")
    c.check("占位（无）视为空", w == "" and ch == "")
    w, ch = prompts_mod.parse_profile_text("垃圾输出没有标记")
    c.check("无标记输出解析为空", w == "" and ch == "")

    compose = prompts_mod.build_image_compose_prompt
    tags_prompt = compose(
        activity="在图书馆靠窗的位置看书",
        seed="那家店的拿铁太甜了",
        mood="专注",
        art_style="anime",
        style="danbooru",
        language="zh",
    )
    c.check("danbooru 指令含标签要求", tags_prompt is not None and "Danbooru" in tags_prompt)
    hint = prompts_mod.build_image_hint("女孩在床上闭眼沉睡")
    c.check("随图提示带画面描述", "闭眼沉睡" in hint)
    c.check("随图提示要求图文呼应", "呼应" in hint and "矛盾" in hint)
    fallback_hint = prompts_mod.build_image_hint("")
    c.check("无描述回退中性描述", "生活随拍" in fallback_hint)
    c.check("danbooru 指令含动漫画风", "anime style" in (tags_prompt or ""))
    c.check("场景事实已注入", "图书馆" in (tags_prompt or "") and "拿铁" in (tags_prompt or ""))
    real_prompt = compose(
        activity="在公司开周会",
        seed="",
        mood="犯困",
        art_style="realistic",
        style="natural",
        language="zh",
    )
    c.check("真人画风指令含写实要求", "photorealistic" in (real_prompt or ""))
    c.check("真人默认中文", "中文" in (real_prompt or ""))
    en_prompt = compose(
        activity="看书", seed="", mood="", art_style="anime", style="natural", language="en"
    )
    c.check("英文自然语言指令", "英文" in (en_prompt or ""))
    c.check("心情与碎片可选", "犯困" in (real_prompt or "") and "拿铁" not in (en_prompt or ""))
    timed_prompt = compose(
        activity="在床上睡觉", seed="", mood="困", art_style="anime", style="natural", language="zh",
        now_text="2026-10-01 周四 04:46",
    )
    c.check("出图指令带真实时刻", timed_prompt is not None and "04:46" in (timed_prompt or ""))
    c.check("出图指令强制环境光线一致", "光线" in (timed_prompt or "") and "绝不能" in (timed_prompt or ""))
    # 跨模块签名一致性（v1.0.26 曾漏改 proactive_gen 侧签名导致运行期 TypeError）
    import inspect as _inspect
    from pathlib import Path as _Path

    _pg_src = _Path(ROOT, "proactive_gen.py").read_text(encoding="utf-8")
    c.check(
        "proactive_gen.compose_image_prompt 支持 image_kind/anchor_hint/now_text",
        all(k in _pg_src for k in ("image_kind", "anchor_hint", "now_text")),
    )
    c.check(
        "proactive.py 调用 compose_image_prompt 传 anchor_hint",
        "anchor_hint=await" in _Path(ROOT, "proactive.py").read_text(encoding="utf-8"),
    )
    kind_prompt = compose(
        activity="在厨房做饭", seed="", mood="", art_style="anime", style="danbooru", language="zh",
        image_kind="眼前的食物或饮品（画面无人物）",
        anchor_hint="银发红瞳少女",
    )
    c.check("出图指令带图类型", "食物或饮品" in (kind_prompt or ""))
    c.check("出图指令带角色锚点", "银发红瞳少女" in (kind_prompt or ""))
    c.check("无锚点不含锚点段", "角色锚点" not in (tags_prompt or ""))
    no_time_prompt = compose(
        activity="看书", seed="", mood="", art_style="anime", style="natural", language="zh",
    )
    c.check("不传时间不含时刻段", no_time_prompt is not None and "当前真实时刻" not in (no_time_prompt or ""))


def test_channel_reason_codes(c: Checker) -> None:
    """配图/表情裁决原因码：proactive.py 落库码 ↔ main.py 中文表 ↔ 前端 REASON_TEXT。"""
    print("\n[12] 配图/表情原因码三处一致性")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src_proactive = open(os.path.join(root, "proactive.py"), encoding="utf-8").read()
    src_main = open(os.path.join(root, "main.py"), encoding="utf-8").read()
    src_api = open(
        os.path.join(root, "webui-src", "src", "api.ts"), encoding="utf-8"
    ).read()

    # 只抽 _record 调用里相邻的 decision+reason 字面量对，
    # 避免把 status() 状态字典的 "sticker_enabled" 之类配置键误当原因码。
    codes = sorted(
        {
            m.group(2)
            for m in re.finditer(r'"(img|sticker)",\s*"((?:img|sticker)_[a-z]+)"', src_proactive)
        }
    )
    c.check("proactive.py 里能抽出原因码", len(codes) >= 10)
    for code in codes:
        c.check(f"原因码 {code} 在 main.py 中文表", f'"{code}"' in src_main)
        c.check(f"原因码 {code} 在前端 REASON_TEXT", code in src_api)


def test_injection_gate(c: Checker) -> None:
    print("\n[11] 注入资格判定")
    ns = load_defs(
        "schedule_view.py",
        {"injection_reject_reason"},
        {
            "Settings": config_mod.Settings,
            "fmt_hhmm": config_mod.fmt_hhmm,
            "datetime": datetime,
            "date_cls": date,
        },
    )
    gate = ns["injection_reject_reason"]

    private_only = config_mod.Settings.from_config({"inject_scopes": ["private"]})
    both = config_mod.Settings.from_config({})
    binding = {"persona_id": "小满", "enabled": 1}

    c.equal(
        "主窗口无条件注入（即使绑定表没有记录）",
        gate(
            umo_kind_str="private",
            is_primary=True,
            primary_umo="p:FriendMessage:1",
            binding=None,
            persona_id="小满",
            settings=private_only,
        ),
        "",
    )
    c.check(
        "绑定匹配的窗口可注入",
        gate(
            umo_kind_str="private",
            is_primary=False,
            primary_umo="p:FriendMessage:1",
            binding=binding,
            persona_id="小满",
            settings=private_only,
        )
        == "",
    )
    c.check(
        "绑定了其他人格拒绝",
        gate(
            umo_kind_str="private",
            is_primary=False,
            primary_umo="p:FriendMessage:1",
            binding={"persona_id": "别人", "enabled": 1},
            persona_id="小满",
            settings=private_only,
        )
        != "",
    )
    c.check(
        "绑定停用拒绝",
        gate(
            umo_kind_str="private",
            is_primary=False,
            primary_umo="p:FriendMessage:1",
            binding={"persona_id": "小满", "enabled": 0},
            persona_id="小满",
            settings=private_only,
        )
        != "",
    )
    c.check(
        "未绑定且 auto_bind 关拒绝（v1.0.3 引入的老问题）",
        gate(
            umo_kind_str="private",
            is_primary=False,
            primary_umo="p:FriendMessage:1",
            binding=None,
            persona_id="小满",
            settings=private_only,
        )
        != "",
    )
    auto = config_mod.Settings.from_config(
        {"inject_scopes": ["private"], "window_auto_bind": True}
    )
    c.check(
        "未绑定但 auto_bind 开可注入",
        gate(
            umo_kind_str="private",
            is_primary=False,
            primary_umo="p:FriendMessage:1",
            binding=None,
            persona_id="小满",
            settings=auto,
        )
        == "",
    )
    c.check(
        "群聊不在注入范围拒绝",
        gate(
            umo_kind_str="group",
            is_primary=False,
            primary_umo="p:FriendMessage:1",
            binding=binding,
            persona_id="小满",
            settings=private_only,
        )
        != "",
    )
    c.check(
        "群聊在范围且为主窗口可注入",
        gate(
            umo_kind_str="group",
            is_primary=True,
            primary_umo="p:GroupMessage:2",
            binding=None,
            persona_id="小满",
            settings=both,
        )
        == "",
    )
    c.check(
        "没有投递窗口拒绝",
        gate(
            umo_kind_str="private",
            is_primary=False,
            primary_umo=None,
            binding=None,
            persona_id="小满",
            settings=private_only,
        )
        != "",
    )


def main() -> int:
    checker = Checker()
    test_config_tools(checker)
    test_settings(checker)
    test_schedule(checker)
    test_proactive(checker)
    test_schedule_view(checker)
    test_scope_and_segments(checker)
    test_images(checker)
    test_config_paths(checker)
    test_routine(checker)
    test_image_backends(checker)
    test_channel_reason_codes(checker)
    test_injection_gate(checker)

    print(f"\n共 {checker.count} 项检查，失败 {len(checker.failures)} 项")
    if checker.failures:
        for name in checker.failures:
            print(f"  - {name}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
