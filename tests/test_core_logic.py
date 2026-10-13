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
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config as config_mod  # noqa: E402  (不依赖 astrbot，可直接导入)
import daylight as daylight_mod  # noqa: E402  (纯标准库，同 config)


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

    # 时间一致性校验（v1.0.42）：默认开；可显式关闭；兼容旧平铺键
    c.check("时段校验默认开启", config_mod.Settings.from_config({}).proactive_time_guard)
    nested_off = config_mod.Settings.from_config({"proactive": {"time_guard": False}})
    c.check("嵌套键可关闭", not nested_off.proactive_time_guard)
    flat_off = config_mod.Settings.from_config({"proactive_time_guard": False})
    c.check("旧平铺键可关闭", not flat_off.proactive_time_guard)

    # 表情包通道（与生图独立）：开关、独立概率与计数、旧键 token 兼容
    sticker = config_mod.Settings.from_config(
        {"proactive_meme_token": "旧token", "proactive_sticker_enabled": True}
    )
    c.equal("表情包 token 旧键兼容", sticker.proactive_sticker_token, "旧token")
    c.check("表情包开关", sticker.proactive_sticker_enabled)
    c.equal("表情包独立概率", sticker.proactive_sticker_probability, 0.15)
    c.equal("表情包独立日限", sticker.proactive_sticker_max_per_day, 2)
    c.equal("生图后端不再含表情包", sticker.proactive_image_backend, "local")

    # 媒体失败降级（配图/表情拿不到图时是否仍发文字）
    c.check(
        "默认不降级（图文一致优先，整条取消）",
        not config_mod.Settings.from_config({}).proactive_media_fallback_text,
    )
    fallback = config_mod.Settings.from_config(
        {"proactive": {"media_fallback_text": True}}
    )
    c.check("降级开关可开", fallback.proactive_media_fallback_text)
    flat_fallback = config_mod.Settings.from_config(
        {"proactive_media_fallback_text": "是"}
    )
    c.check("平铺键兜底可开", flat_fallback.proactive_media_fallback_text)

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
        "_GAP_TOLERANCE_MINUTES",
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
        # v1.0.43：近似重复 + 饮食多样性（纯函数）
        "_SIMILAR_ACTIVITY",
        "_FOOD_WORDS",
        "_FOOD_WORDS_BY_LENGTH",
        "_FOOD_STAPLE_EXEMPT",
        "_normalize_activity",
        "_bigrams",
        "activity_similarity",
        "extract_foods",
        "diet_repetition",
        "meal_lines",
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
            "daylight": daylight_mod,
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

    # v1.0.43：近似重复——「同一件事换个说法」也要算重复（原来只比字符串相等）
    similar = normalize_items(
        [
            {"time": "07:00", "end": "08:00", "activity": "在厨房做简单的早饭", "message_seed": ""},
            {"time": "12:00", "end": "13:00", "activity": "中午吃了便利店的关东煮", "message_seed": ""},
            {"time": "14:00", "end": "18:00", "activity": "下午在窗边写字", "message_seed": ""},
        ]
    )
    c.check(
        "换句话说的同一件事也算重复",
        detect_repetition(
            similar,
            ["在厨房做了简单的早饭", "中午点了便利店的关东煮", "下午坐在窗边写东西"],
        ),
    )
    c.check(
        "完全无关的活动不误判",
        not detect_repetition(similar, ["在阳台浇花", "去超市买了鸡蛋"]),
    )

    # v1.0.43：饮食多样性（「每天吃的一样」是用户实测反馈的痛点）
    extract_foods = ns["extract_foods"]
    diet_repetition = ns["diet_repetition"]
    meal_lines = ns["meal_lines"]
    c.equal("抽食物词：拉面", extract_foods("中午吃了碗拉面"), {"拉面"})
    c.equal("抽食物词：长词优先（面包不拆成面）", extract_foods("早上烤了面包"), {"面包"})
    c.equal("抽食物词：多项", extract_foods("中午吃关东煮，晚上涮火锅"), {"关东煮", "火锅"})
    c.equal("无食物返回空", extract_foods("下午在窗边写字"), set())
    c.equal(
        "同一道菜跨天重复被抓到",
        diet_repetition(similar, ["中午点了便利店的关东煮", "晚上泡面"]),
        ["关东煮"],
    )
    c.equal(
        "主食每天一样不算单一",
        diet_repetition(
            [{"activity": "早上喝了粥配牛奶"}], ["早上也是粥和牛奶"]
        ),
        [],
    )
    c.equal(
        "最近没吃过就不算重复",
        diet_repetition(similar, ["在阳台浇花", "去超市买了鸡蛋"]),
        [],
    )
    c.equal(
        "餐次行只挑含食物的活动",
        meal_lines(
            [
                {"plan_date": "2026-10-12", "activity": "中午吃了拉面"},
                {"plan_date": "2026-10-12", "activity": "下午开会"},
            ]
        ),
        ["2026-10-12：中午吃了拉面"],
    )

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
            "daylight": daylight_mod,
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
    # v1.0.42：时刻要和时段一起注入（光给「16:05」，模型照样能写出「天黑了」）
    c.check(
        "注入的时刻带时段与日落边界",
        "今天日落约" in block
        and daylight_mod.phase_of(datetime.now()).name in block,
    )
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
        {"os": os, "random": random, "daylight": daylight_mod},
    )
    list_image_files = ns["list_image_files"]
    image_desc_from_path = ns["image_desc_from_path"]
    choose_image_file = ns["choose_image_file"]

    import prompts as prompts_mod

    hint = prompts_mod.build_image_hint("在厨房做饭")
    c.check("hint 带画面线索", "在厨房做饭" in hint)
    # 语义：要求文字交代拍摄来由（为什么拍/拍的什么），并禁止矛盾情节
    c.check("hint 要求交代来由", "为什么拍" in hint and "来由" in hint)
    c.check("hint 禁止矛盾情节", "矛盾" in hint)
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

    # 时段感知选图（v1.0.42）：文件名带时段线索时优先挑吻合的
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("下午的窗边.jpg", "黄昏的街.jpg", "夜景街灯.png", "IMG_0007.jpg"):
            with open(os.path.join(tmp, name), "wb") as handle:
                handle.write(b"x")
        files = list_image_files(tmp)
        all_names = {"下午的窗边.jpg", "黄昏的街.jpg", "夜景街灯.png", "IMG_0007.jpg"}
        day_picks = {os.path.basename(choose_image_file(files, "day") or "") for _ in range(30)}
        night_picks = {os.path.basename(choose_image_file(files, "night") or "") for _ in range(30)}
        c.check("白天候选 = 白天线索 + 无线索", day_picks <= {"下午的窗边.jpg", "IMG_0007.jpg"})
        c.check("白天绝不挑夜晚/黄昏线索的图", not (day_picks & {"夜景街灯.png", "黄昏的街.jpg"}))
        c.check("夜晚候选 = 夜晚线索 + 无线索", night_picks <= {"夜景街灯.png", "IMG_0007.jpg"})
        c.check("夜晚绝不挑白天线索的图", not (night_picks & {"下午的窗边.jpg"}))
        c.check(
            "无 group 时全库随机（保持旧行为）",
            os.path.basename(choose_image_file(files) or "") in all_names,
        )
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("夜景a.png", "夜景b.png"):
            with open(os.path.join(tmp, name), "wb") as handle:
                handle.write(b"x")
        only_night = list_image_files(tmp)
        c.check(
            "整批都不吻合时退回随机（不能在选图上死循环或报错）",
            os.path.basename(choose_image_file(only_night, "day") or "") in
            {"夜景a.png", "夜景b.png"},
        )
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
    c.equal("外部调用 JSON 取路径（兼容 companion 历史值）", parse_anima_result(ok_payload), "/tmp/a.png")
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

    # 跨插件契约（萌绘 v7.7.58+）：取图靠显式 external=True，source 只作来源标识。
    # 不许再借伴侣插件的口令串——那会让本插件在萌绘日志/图库里被记成别的插件。
    with open(os.path.join(ROOT, "proactive_gen.py"), "r", encoding="utf-8") as handle:
        pg_src = handle.read()
    c.equal("来源标识 = 本插件自己的名字", ns["ANIMA_SOURCE_TAG"], "萌萌日程")
    c.check("取图必须显式 external=True", '"external": True' in pg_src)
    c.check("不得再传伴侣插件的口令", "我会永远陪着你" not in pg_src)
    c.equal("点开头扩展名", meme_cache_filename("id", ".gif"), "id.gif")

    # 媒体失败降级分支必须仍在（开关打开时走纯文字，关闭时整条取消）
    with open(os.path.join(ROOT, "proactive.py"), "r", encoding="utf-8") as handle:
        pr_src = handle.read()
    c.check("降级开关被读取", "proactive_media_fallback_text" in pr_src)
    c.check("降级写 img_fallback", "img_fallback" in pr_src)
    c.check("降级写 sticker_fallback", "sticker_fallback" in pr_src)
    c.check("关闭时仍保留 media_failed", "media_failed" in pr_src)

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
    c.check("随图提示要求交代来由", "为什么拍" in hint and "矛盾" in hint)
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
    c.equal("10 月日落约 18:00", daylight_mod.sunset_for_month(10), "18:00")
    c.equal("7 月日落约 19:20", daylight_mod.sunset_for_month(7), "19:20")
    c.equal("月份越界回退 18:00", daylight_mod.sunset_for_month(13), "18:00")
    c.equal("10 月日出约 06:20", daylight_mod.sunrise_for_month(10), "06:20")
    c.equal("月份越界回退 06:20", daylight_mod.sunrise_for_month(0), "06:20")

    # 覆盖校验 v2：断档必须触发 time issue（0-12 点断档的回归测试）
    ns_v = load_defs(
        "schedule.py",
        {
            "normalize_items",
            "validate_items",
            "_to_minutes",
            "_STRICT_TIME_RE",
            "_GAP_TOLERANCE_MINUTES",
            "MIN_ITEM_MINUTES",
            "MAX_ITEM_MINUTES",
            "MIN_COVERAGE_MINUTES",
        },
        {
            "parse_hhmm": config_mod.parse_hhmm,
            "Settings": config_mod.Settings,
            "date_cls": date,
            "re": re,
            "daylight": daylight_mod,
        },
    )
    norm = ns_v["normalize_items"]
    val = ns_v["validate_items"]
    settings_default = config_mod.Settings.from_config({})

    def _mk(time_s, end_s, seed="x"):
        return norm([{"time": time_s, "end": end_s, "activity": f"活动{time_s}", "message_seed": seed}])

    afternoon_only = _mk("12:00", "14:00") + _mk("14:00", "23:00")
    issues_gap, _ = val(afternoon_only, settings_default)
    c.check("12 点起的日程判为断档（time issue）", "time" in issues_gap)
    full_day = (
        _mk("23:30", "07:10", "")
        + _mk("07:10", "12:00")
        + _mk("12:00", "18:00")
        + _mk("18:00", "23:30")
    )
    issues_full, _ = val(full_day, settings_default)
    c.check("跨午夜睡眠条目连续覆盖全天通过", "time" not in issues_full)
    small_gap = _mk("00:00", "08:00") + _mk("08:30", "14:00") + _mk("14:00", "23:59")
    issues_small, _ = val(small_gap, settings_default)
    c.check("30 分钟小空隙不算断档", "time" not in issues_small)

    # v1.0.42：天色一致性检查（传了 month 才做；10 月日落 18:00）
    def _mk_act(time_s, end_s, activity):
        return norm(
            [{"time": time_s, "end": end_s, "activity": activity, "message_seed": ""}]
        )

    afternoon_dark = (
        _mk_act("00:00", "07:10", "睡觉")
        + _mk_act("07:10", "12:00", "上午处理事情")
        + _mk_act("12:00", "16:30", "下午在窗边写字，天已经完全黑了")
        + _mk_act("16:30", "23:30", "晚上准备睡觉")
    )
    issues_day, score_day = val(afternoon_dark, settings_default, month=10)
    c.check("下午条目写「天已经完全黑了」→ daylight issue", "daylight" in issues_day)
    c.check("天色问题会扣分", score_day < 100.0)
    issues_no_month, _ = val(afternoon_dark, settings_default)
    c.check("不传 month 时跳过天色检查（兼容老调用方）", "daylight" not in issues_no_month)

    night_sun = (
        _mk_act("00:00", "07:10", "睡觉")
        + _mk_act("07:10", "12:00", "上午处理事情")
        + _mk_act("12:00", "19:00", "下午在窗边写字，傍晚出门散步")
        + _mk_act("19:00", "23:30", "夜里在阳台晒着太阳看书")
    )
    issues_night, _ = val(night_sun, settings_default, month=10)
    c.check("夜里条目写「阳光」→ daylight issue", "daylight" in issues_night)

    dusk_span = (
        _mk_act("00:00", "07:10", "睡觉")
        + _mk_act("07:10", "12:00", "上午处理事情")
        + _mk_act("12:00", "17:00", "下午在窗边写字")
        + _mk_act("17:00", "19:00", "傍晚出门散步，天色一点点暗下来，路灯陆续亮了")
        + _mk_act("19:00", "23:30", "晚上在家看书")
    )
    issues_dusk, _ = val(dusk_span, settings_default, month=10)
    c.check("跨日落的条目写「天色渐暗」不算矛盾", "daylight" not in issues_dusk)
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

    # v1.0.42：时段硬约束 + 光照标签方向（下午四点不许画出夜景）
    afternoon = datetime(2026, 10, 13, 16, 5)
    lit_prompt = compose(
        activity="在厨房做饭", seed="", mood="", art_style="anime", style="danbooru", language="zh",
        now_text="2026-10-13 16:05",
        daylight_fact=daylight_mod.lighting_fact(afternoon),
        lighting_tags=daylight_mod.lighting_tags(afternoon),
    )
    c.check(
        "出图指令带此刻时段硬约束",
        "此刻时段" in (lit_prompt or "") and "天还亮着" in (lit_prompt or ""),
    )
    c.check(
        "出图指令给死光照标签方向",
        "afternoon" in (lit_prompt or "") and "daylight" in (lit_prompt or ""),
    )
    c.check(
        "不传时段则不加时间规则（老调用方不受影响）",
        "时间标签只能从这一组里挑" not in (no_time_prompt or ""),
    )
    image_fix = prompts_mod.build_image_time_correction(
        "下午（白天）", "天还亮着", "afternoon, daylight", ["天黑", "晚安"]
    )
    c.check(
        "出图纠偏块带矛盾词与允许的光照方向",
        "天黑" in image_fix and "晚安" in image_fix and "afternoon" in image_fix,
    )
    text_fix = prompts_mod.build_time_correction("下午（白天）", "天还亮着", ["天黑"])
    c.check("文案纠偏块带时段与矛盾词", "下午（白天）" in text_fix and "天黑" in text_fix)


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

    # 生成后时段校验（v1.0.42，decision=guard）：落库码同样要三处齐备
    src_gen = open(os.path.join(root, "proactive_gen.py"), encoding="utf-8").read()
    c.check("时段校验落库码存在", '"guard"' in src_gen and '"time_conflict"' in src_gen)
    c.check("原因码 time_conflict 在 main.py 中文表", '"time_conflict"' in src_main)
    c.check("原因码 time_conflict 在前端 REASON_TEXT", "time_conflict" in src_api)


def test_daylight(c: Checker) -> None:
    """时段判定 / 时段矛盾校验 / 模块接线（v1.0.42）。"""
    print("\n[13] 时段判定与时段矛盾校验")
    dt = datetime

    # 10 月：日出 06:20 / 日落 18:00
    def key(hour, minute=0):
        return daylight_mod.phase_of(dt(2026, 10, 13, hour, minute)).key

    c.equal("16:05 → 下午", key(16, 5), "afternoon")
    c.equal("09:00 → 上午", key(9), "morning")
    c.equal("12:00 → 中午", key(12), "noon")
    c.equal("17:30 → 傍晚", key(17, 30), "dusk")
    c.equal("06:30 → 清晨", key(6, 30), "dawn")
    c.equal("04:00 → 夜晚", key(4), "night")
    c.equal("20:00 → 夜晚", key(20), "night")
    afternoon_at = dt(2026, 10, 13, 16, 5)
    afternoon = daylight_mod.phase_of(afternoon_at)
    c.check("下午不算天黑", not afternoon.is_dark and afternoon.group == "day")
    c.check("下午是白天时段（图库按 day 挑图）", afternoon.group == "day")

    fact = daylight_mod.lighting_fact(afternoon_at)
    c.check("硬约束块说明天没黑", "天没有黑" in fact and "此刻时段" in fact)
    c.check("硬约束块给出日落边界", "日落约 18:00" in fact)
    night_fact = daylight_mod.lighting_fact(dt(2026, 10, 13, 23, 0))
    c.check("夜里的硬约束块不许写白天", "阳光" in night_fact and "夜晚" in night_fact)
    c.check("光照标签：下午含 daylight", "daylight" in daylight_mod.lighting_tags(afternoon_at))
    c.check(
        "光照标签：夜里是 night 方向",
        "night" in daylight_mod.lighting_tags(dt(2026, 10, 13, 23, 0)),
    )
    c.check("时段行含中文时段名", "下午" in daylight_mod.phase_line(afternoon_at))

    # 用户实测的那一条：下午四点说「天已经完全黑了，在考虑睡不睡觉」
    hits = daylight_mod.find_time_conflicts(
        "天已经完全黑了，在考虑睡不睡觉，晚安", dt(2026, 10, 13, 16, 5)
    )
    c.check("下午写「黑了/睡觉/晚安」被判为矛盾", len(hits) >= 2, f"hits={hits}")
    c.check(
        "否认句「天还没黑」不算矛盾",
        daylight_mod.find_time_conflicts("天还没黑，再忙一会儿", dt(2026, 10, 13, 16, 5)) == [],
    )
    c.check(
        "下午的午睡说法不算矛盾",
        daylight_mod.find_time_conflicts(
            "刚睡了个午觉，在吃点心", dt(2026, 10, 13, 14, 0)
        )
        == [],
    )
    c.check(
        "夜里写「阳光」才是矛盾",
        daylight_mod.find_time_conflicts("阳光真好", dt(2026, 10, 13, 23, 0)) != [],
    )
    c.check(
        "夜里的「该睡了，晚安」不算矛盾",
        daylight_mod.find_time_conflicts("该睡了，晚安", dt(2026, 10, 13, 23, 0)) == [],
    )
    c.check(
        "傍晚对「天色渐暗」宽容",
        daylight_mod.find_time_conflicts(
            "天色渐暗，路灯都亮了", dt(2026, 10, 13, 17, 30)
        )
        == [],
    )
    c.check(
        "英文标签里的 night 也被拦下",
        daylight_mod.find_time_conflicts(
            "1girl, night, moonlight, streetlight", dt(2026, 10, 13, 16, 5)
        )
        != [],
    )
    c.check(
        "英文标签白天写法不算矛盾",
        daylight_mod.find_time_conflicts(
            "1girl, afternoon, daylight, bright", dt(2026, 10, 13, 16, 5)
        )
        == [],
    )

    # 文件名时段线索（本地图库选图用）
    c.equal("线索：下午的窗边", daylight_mod.image_phase_hint("下午的窗边.jpg"), "day")
    c.equal("线索：夜景", daylight_mod.image_phase_hint("夜景.png"), "night")
    c.equal("线索：黄昏", daylight_mod.image_phase_hint("黄昏的街.jpg"), "dusk")
    c.equal("线索：纯编号名无线索", daylight_mod.image_phase_hint("IMG_0007.jpg"), "")
    c.check("时段不接受不吻合的图", not daylight_mod.phase_accepts("day", "night"))
    c.check("无线索的图任何时段都能用", daylight_mod.phase_accepts("night", ""))

    # 接线守卫：新模块必须同时进打包清单与热重载清单
    # （漏了热重载 → 热更上来的实例永远跑旧代码，这是踩过的坑）
    with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as handle:
        src_main = handle.read()
    with open(os.path.join(ROOT, "build_zip.ps1"), encoding="utf-8") as handle:
        src_zip = handle.read()
    c.check("daylight 在 main.py 热重载清单", '"daylight"' in src_main)
    c.check("daylight.py 在 build_zip includeList", '"daylight.py"' in src_zip)
    c.check(
        "主动消息带时段硬约束",
        "lighting_fact" in open(
            os.path.join(ROOT, "proactive_gen.py"), encoding="utf-8"
        ).read(),
    )
    c.check(
        "出图链路带时段硬约束与光照标签",
        "lighting_tags" in open(
            os.path.join(ROOT, "proactive.py"), encoding="utf-8"
        ).read(),
    )


def test_continuity(c: Checker) -> None:
    """跨次连续与图文一致：时间锚、主动消息历史接线、随图说明位置（v1.0.43/44）。"""
    print("\n[14] 跨次连续与图文一致")
    import prompts as prompts_mod

    now = datetime(2026, 10, 13, 16, 5)
    c.equal(
        "今天的时间锚",
        daylight_mod.stamp_text(datetime(2026, 10, 13, 9, 30).timestamp(), now),
        "今天 09:30",
    )
    c.equal(
        "昨天的时间锚",
        daylight_mod.stamp_text(datetime(2026, 10, 12, 22, 10).timestamp(), now),
        "昨天 22:10",
    )
    c.equal(
        "前天的时间锚",
        daylight_mod.stamp_text(datetime(2026, 10, 11, 8, 0).timestamp(), now),
        "前天 08:00",
    )
    c.equal(
        "更早给日期",
        daylight_mod.stamp_text(datetime(2026, 10, 8, 20, 0).timestamp(), now),
        "10-08 20:00",
    )
    c.equal("坏时间戳返回空", daylight_mod.stamp_text("bad", now), "")
    c.equal("刚刚", daylight_mod.ago_text((now - timedelta(seconds=30)).timestamp(), now), "刚刚")
    c.equal(
        "25 分钟前",
        daylight_mod.ago_text((now - timedelta(minutes=25)).timestamp(), now),
        "25 分钟前",
    )
    c.equal(
        "3 小时前",
        daylight_mod.ago_text((now - timedelta(hours=3)).timestamp(), now),
        "3 小时前",
    )
    c.equal(
        "2 天前",
        daylight_mod.ago_text((now - timedelta(days=2)).timestamp(), now),
        "2 天前",
    )

    prompt = prompts_mod.build_proactive_user(
        now_text="2026-10-13 周二 16:05",
        current_block="16:00-18:00 把阳台的花浇了",
        seed="",
        recent_chat="（最后一条消息：今天 15:40，25 分钟前）\n用户：在吗",
        recent_proactive="今天 15:20：刚把阳台的花浇了\n昨天 22:10：我准备睡了",
    )
    c.check("最近几条主动进提示词（带时间锚）", "今天 15:20" in prompt and "昨天 22:10" in prompt)
    c.check("最近对话带时间锚", "25 分钟前" in prompt)
    c.check(
        "空历史回退「第一次主动」",
        "第一次主动"
        in prompts_mod.build_proactive_user(
            now_text="2026-10-13 周二 16:05", current_block="", seed="", recent_chat=""
        ),
    )
    c.check(
        "老调用方（只给 last_proactive）仍可用",
        "下午茶好难喝"
        in prompts_mod.build_proactive_user(
            now_text="t",
            current_block="",
            seed="",
            recent_chat="",
            last_proactive="下午茶好难喝",
        ),
    )

    # 接线守卫：历史必须真的落库、生成时真的会读；desc 与日志口径也要在
    src_proactive = open(os.path.join(ROOT, "proactive.py"), encoding="utf-8").read()
    src_gen = open(os.path.join(ROOT, "proactive_gen.py"), encoding="utf-8").read()
    src_store = open(os.path.join(ROOT, "store.py"), encoding="utf-8").read()
    src_sched = open(os.path.join(ROOT, "schedule.py"), encoding="utf-8").read()
    c.check("发送成功后记主动消息历史", "add_proactive_message" in src_proactive)
    c.check("生成时读取主动消息历史", "recent_proactive_messages" in src_gen)
    c.check("主动消息历史表建在 store 里", "proactive_message" in src_store)
    c.check("出图 desc 用中文画面摘要", "此刻是{phase.name}" in src_proactive)
    c.check("裁决日志记下实际提示词", "final_prompt[:160]" in src_proactive)
    c.check("饮食重复单独判一次", "diet_repetition" in src_sched)
    c.check("三餐避重清单进提示词", "recent_foods" in src_sched)

    # v1.0.44：图文一致收口
    with_image = prompts_mod.build_proactive_user(
        now_text="2026-10-13 周二 16:05",
        current_block="16:00-18:00 把阳台的花浇了",
        seed="",
        recent_chat="",
        image_hint="\n【随图说明】画面是：窗边的绿萝",
    )
    c.check("随图说明排在用户侧末尾", with_image.rstrip().endswith("窗边的绿萝"))
    hint_text = prompts_mod.build_image_hint("窗边的绿萝")
    c.check("随图说明点明照片跟在最后一条", "最后一条" in hint_text)
    c.check("随图说明禁止写画面里没有的东西", "没有的东西" in hint_text)

    no_image = prompts_mod.build_proactive_user(
        now_text="2026-10-13 周二 16:05",
        current_block="16:00-18:00 把阳台的花浇了",
        seed="",
        recent_chat="",
    )
    c.check("不带图时不出现随图说明", "随图说明" not in no_image)

    ns_hint = load_defs("proactive_gen.py", {"is_chinese_text"})
    is_chinese_text = ns_hint["is_chinese_text"]
    c.check("中文提示词判为中文", is_chinese_text("下午在窗边浇花，绿萝的叶子亮亮的"))
    c.check(
        "英文标签判为非中文",
        not is_chinese_text("1girl, afternoon, watering flowers, green leaves"),
    )
    c.check("空串判为非中文", not is_chinese_text("   "))
    c.check("画面线索优先用中文提示词原文", "is_chinese_text(final_prompt)" in src_proactive)
    c.check("出图文件存在性预检", "os.path.isfile(path)" in src_proactive)


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
    test_daylight(checker)
    test_continuity(checker)

    print(f"\n共 {checker.count} 项检查，失败 {len(checker.failures)} 项")
    # 纯 ASCII 结论行：脚本要在 GBK 控制台里跑（中文 pattern 会失真），
    # 自动化只看这一行就知道成不成。
    print(f"RESULT: total={checker.count} failures={len(checker.failures)}")
    if checker.failures:
        for name in checker.failures:
            print(f"  - {name}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
