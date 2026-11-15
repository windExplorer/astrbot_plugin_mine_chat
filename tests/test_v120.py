"""v1.2.0 生活逻辑层自检：季节 / 天气 / 生活面 / 矛盾扫描。

用法：
    python tests/test_v120.py        # 退出码 0 即全部通过

这一版解决的是「日程无聊 + 前后矛盾」。矛盾扫描是启发式词表，最怕两件事：
**误报**（把好日程拖去兜底模板）和**漏报**（写了等于没写）。所以这里既测
「该报的报」，也测「不该报的不报」。
"""

from __future__ import annotations

import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for path in (ROOT, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

from test_core_logic import Checker, load_defs  # noqa: E402

import life  # noqa: E402  (纯标准库，可直接导入)


def _item(start: int, end: int, activity: str, seed: str = "") -> dict:
    return {
        "start_min": start,
        "end_min": end,
        "activity": activity,
        "message_seed": seed,
        "mood": "",
    }


# --------------------------------------------------------------------------- #
# 1. 季节与天气
# --------------------------------------------------------------------------- #
def test_season_weather(c: Checker) -> None:
    print("\n[1] 季节与天气")
    c.equal("1 月是冬", life.season_of(1), "冬")
    c.equal("4 月是春", life.season_of(4), "春")
    c.equal("7 月是夏", life.season_of(7), "夏")
    c.equal("11 月是秋", life.season_of(11), "秋")
    c.equal("越界月份回退", life.season_of(99), "春")
    c.check("11 月描述含深秋", "深秋" in life.season_line(11), life.season_line(11))
    c.check("季节行含体感", "体感" not in life.season_line(1) and life.season_line(1))

    first = life.weather_for("霜岛绫", "2026-11-16", "cold")
    again = life.weather_for("霜岛绫", "2026-11-16", "cold")
    c.equal("天气确定性", first, again)
    c.check("天气有标签", bool(first.label) and bool(first.desc) and bool(first.feel))
    c.check("天气行可读", "：" in first.line(), first.line())

    # 寒带冬季应当明显更容易下雪（60 天里至少 15 天）
    snowy = 0
    for day in range(1, 61):
        month = 1 if day <= 31 else 2
        date = f"2026-{month:02d}-{day if day <= 31 else day - 31:02d}"
        if life.weather_for("霜岛绫", date, "cold").kind == "snow":
            snowy += 1
    c.check("寒带冬天常下雪", snowy >= 15, f"snowy={snowy}")

    c.equal("雪国判为寒带", life.region_of("她住在雪国的小镇，冬天很长"), "cold")
    c.equal("南方判为温暖", life.region_of("南方的海边城市，夏天炎热潮湿"), "warm")
    c.equal("无提示判为温带", life.region_of("普通城市，一个上班族"), "temperate")


# --------------------------------------------------------------------------- #
# 2. 矛盾扫描
# --------------------------------------------------------------------------- #
def test_scan(c: Checker) -> None:
    print("\n[2] 矛盾扫描")
    # 反季
    winter_watermelon = [_item(840, 900, "在冰箱里翻出一块冰镇西瓜")]
    c.check(
        "冬天吃冰镇西瓜被抓",
        bool(life.scan(winter_watermelon, month=1, season="冬")),
    )
    c.check(
        "冬天喝热可可没问题",
        not life.scan([_item(840, 900, "煮了一杯热可可，加了很多糖")], month=1, season="冬"),
    )

    # 天气
    c.check(
        "晴天写打伞被抓",
        bool(life.scan([_item(600, 660, "下雨了，撑着伞去楼下")], month=11, weather_kind="clear")),
    )
    c.check(
        "下雪天晒被子被抓",
        bool(life.scan([_item(600, 660, "把被子抱到阳台晒被子")], month=1, weather_kind="snow")),
    )
    c.check(
        "阴天在家看书没问题",
        not life.scan([_item(600, 660, "窝在沙发上把旧书翻了一半")], month=11, weather_kind="overcast"),
    )

    # 睡眠时段
    c.check(
        "凌晨三点在公司开会被抓",
        bool(
            life.scan(
                [_item(180, 240, "在公司开周会")],
                month=11,
                sleep_start_min=23 * 60 + 30,
                sleep_end_min=7 * 60 + 30,
            )
        ),
    )
    c.check(
        "凌晨三点起来喝水没问题",
        not life.scan(
            [_item(180, 190, "起来喝了口水又躺回去")],
            month=11,
            sleep_start_min=23 * 60 + 30,
            sleep_end_min=7 * 60 + 30,
        ),
    )
    c.check(
        "下午在上班没问题",
        not life.scan(
            [_item(840, 1080, "在公司上班")],
            month=11,
            sleep_start_min=23 * 60 + 30,
            sleep_end_min=7 * 60 + 30,
        ),
    )

    # 基调
    c.check(
        "「几乎不出门」却去超市被抓",
        bool(life.scan([_item(600, 700, "去超市买菜")], month=11, pace_kind="indoor")),
    )
    c.check(
        "「几乎不出门」下楼取快递没问题",
        not life.scan(
            [_item(600, 620, "下楼取了个快递，风比想的冷")], month=11, pace_kind="indoor"
        ),
    )
    c.check(
        "「出门走动」却没有外出被抓",
        bool(life.scan([_item(600, 700, "在屋里整理衣柜")], month=11, pace_kind="out")),
    )
    c.check(
        "「出门走动」有外出就没问题",
        not life.scan(
            [_item(600, 700, "出门去了一趟菜市场，回来时手冻僵了")],
            month=11,
            pace_kind="out",
        ),
    )

    # 状态断线
    notes = ["感冒还没好，嗓子哑"]
    c.check(
        "昨天感冒今天全无交代被抓",
        bool(life.scan([_item(600, 700, "在窗边看书")], month=11, status_notes=notes)),
    )
    c.check(
        "今天还在咳嗽就算延续",
        not life.scan([_item(600, 700, "咳得厉害，只喝了点热水")], month=11, status_notes=notes),
    )
    c.check(
        "今天明确好了也算交代",
        not life.scan([_item(600, 700, "感冒好多了，终于想吃饭")], month=11, status_notes=notes),
    )
    c.check(
        "没有遗留状态不报",
        not life.scan([_item(600, 700, "在窗边看书")], month=11, status_notes=[]),
    )

    # 问题数量上限
    many = [_item(600 + i * 30, 630 + i * 30, "去超市买菜还下着雨") for i in range(10)]
    c.check(
        "问题数量有上限",
        len(life.scan(many, month=11, weather_kind="clear", pace_kind="indoor")) <= 6,
    )


# --------------------------------------------------------------------------- #
# 3. 生活面
# --------------------------------------------------------------------------- #
def test_categories(c: Checker) -> None:
    print("\n[3] 生活面归类")
    c.equal("发呆归休闲", life.category_of("在窗边发呆"), "休闲")
    c.equal("吃午饭归用餐", life.category_of("吃午饭，面有点咸"), "用餐")
    c.equal("上班归工作", life.category_of("去公司上班"), "工作")
    c.equal("收拾归家务", life.category_of("把房间收拾了一遍"), "家务")
    c.equal("空文本归其他", life.category_of(""), "其他")

    items = [
        _item(480, 540, "吃早饭"),
        _item(540, 720, "在电脑前写报告"),
        _item(720, 780, "把地拖了"),
        _item(1380, 1440, "睡觉"),
    ]
    found = life.categories_of(items)
    c.check("覆盖用餐/工作/家务", {"用餐", "工作", "家务"} <= found, str(found))
    c.check("睡眠不计入生活面", "睡眠" not in found)
    c.equal("缺失的必须有项", life.missing_categories(items, ["休闲"]), ["休闲"])
    c.equal("已覆盖则不缺", life.missing_categories(items, ["家务"]), [])
    c.equal("没要求就不缺", life.missing_categories(items, None), [])

    picks = life.pick_required_categories("霜岛绫", "2026-11-16")
    c.check("每天挑 1~2 个生活面", 1 <= len(picks) <= 2, str(picks))
    c.equal(
        "确定性",
        picks,
        life.pick_required_categories("霜岛绫", "2026-11-16"),
    )
    avoided = life.pick_required_categories("霜岛绫", "2026-11-16", ["家务", "休闲", "身体"])
    c.check(
        "避开最近要求过的", avoided == ["外出"], str(avoided)
    )


# --------------------------------------------------------------------------- #
# 4. flavor 轮换与 state_after 清洗
# --------------------------------------------------------------------------- #
def test_flavor_rotation(c: Checker) -> None:
    print("\n[4] 主线轮换与状态清洗")
    ns = load_defs(
        "schedule.py",
        {"daily_flavor", "_PACE_TIERS", "_THEME_POOL", "_clean_state_notes", "_STATE_NOTE_MAX"},
        {"random": random, "re": __import__("re")},
    )
    flavor = ns["daily_flavor"]
    clean = ns["_clean_state_notes"]

    base = flavor("霜岛绫", "2026-11-16", ["热可可"], [])
    c.check("有基调类别", base["pace_kind"] in {"normal", "slow", "chaotic", "out", "indoor"},
            base["pace_kind"])
    c.check("有主线类别", bool(base["theme_category"]), str(base))
    c.equal("同一天确定性", base, flavor("霜岛绫", "2026-11-16", ["热可可"], []))

    # 把最近三天用过的类别全排掉 → 退回全池，仍要能选出结果
    all_categories = sorted({entry[0] for entry in ns["_THEME_POOL"]})
    fallback = flavor("霜岛绫", "2026-11-16", [], [], all_categories)
    c.check("类别全用尽时退回全池", bool(fallback["theme"]), str(fallback))

    # 只排掉一个类别 → 不能选中它
    used = [base["theme_category"]]
    rotated = flavor("霜岛绫", "2026-12-01", [], [], used)
    c.check("避开最近用过的类别", rotated["theme_category"] != base["theme_category"],
            f"{rotated['theme_category']} == {base['theme_category']}")

    c.equal("字符串切成多条", len(clean("感冒还没好；菜还剩半颗")), 2)
    c.equal("过短的丢掉", clean(["啊", "灯坏了没修"]), ["灯坏了没修"])
    c.equal("非列表返回空", clean(12345), [])
    c.equal("空数组", clean([]), [])
    c.check("最多四条", len(clean(["a1", "b2", "c3", "d4", "e5"])) == 4)
    c.check("单条截断", all(len(note) <= 40 for note in clean(["字" * 200])))


# --------------------------------------------------------------------------- #
# 5. 提示词模板占位符全覆盖
# --------------------------------------------------------------------------- #
def test_prompt_placeholders(c: Checker) -> None:
    print("\n[5] 日程提示词占位符")
    ns = load_defs(
        "prompts.py",
        {"PLAN_USER_TEMPLATE", "fill", "build_plan_user"},
    )
    build = ns["build_plan_user"]
    body = build(
        persona="她是霜岛绫",
        world="雪国小镇",
        character="独居",
        date_text="2026年11月16日",
        weekday_text="周一",
        calendar_hint="今天是工作日。",
        sunrise_text="06:40",
        sunset_text="17:05",
        sleep_start_text="23:30",
        sleep_end_text="07:30",
        night_owl_hint="深夜必须处于睡眠状态。",
        routine_note="今天的作息与平时基本一致。",
        style="",
        forbidden=["不要写工作压力"],
        recent_lines=["2026-11-15：在窗边看书"],
        yesterday_tail=["22:10 洗漱"],
        item_min=10,
        item_max=18,
        recent_foods=["面"],
        pace="慢半拍的一天——动作更慢",
        theme="整理旧物时翻到某样东西",
        life_items=["热可可", "旧书"],
        avoid_items=["吵闹人群"],
        relation_block="【你们的关系】熟悉。",
        season_line="深秋（11 月）：凉爽干燥。",
        weather_line="晴：天气晴，光线很亮（体感温和）",
        required_categories=["休闲", "家务"],
        status_notes=["感冒还没好"],
    )
    # fill() 是纯字符串替换：漏传一个键就会把 {xxx} 原样留在提示词里，
    # 模型看到的是占位符而不是内容。这条守卫拦的就是「加了段落忘加参数」。
    leftover = [part for part in body.split("{") if "}" in part]
    c.check("没有残留占位符", not leftover, f"leftover={leftover[:3]}")
    c.check("天气进了提示词", "天气晴" in body)
    c.check("季节进了提示词", "深秋" in body)
    c.check("必须覆盖的生活面进了提示词", "休闲、家务" in body)
    c.check("遗留状态进了提示词", "感冒还没好" in body)
    c.check("不完美要求进了提示词", "半途而废" in body)


# --------------------------------------------------------------------------- #
# 6. 主动消息读上下文时的清洗
# --------------------------------------------------------------------------- #
def test_history_cleanup(c: Checker) -> None:
    print("\n[6] 主动消息的对话摘要清洗")
    ns = load_defs(
        "proactive_gen.py",
        {
            "clean_history_line",
            "strip_injected_block",
            "USER_PLACEHOLDER",
            "USER_ECHO_PLACEHOLDER",
            "_INJECTION_MARKERS",
            "_ECHO_PREFIX",
            "_HISTORY_TEXT_LIMIT",
        },
    )
    clean = ns["clean_history_line"]
    strip = ns["strip_injected_block"]
    placeholder = ns["USER_PLACEHOLDER"]
    echo = ns["USER_ECHO_PLACEHOLDER"]

    dirty = "今天好冷啊\n【现在的时间】2026-11-16 14:35\n【你此刻】在窗边看书（心情：安静）\n以上是你自己的生活设定"
    c.equal(
        "注入块被裁掉",
        strip(dirty),
        "今天好冷啊",
    )
    c.equal("用户原话保留", clean("user", dirty), "用户：今天好冷啊")
    c.equal("角色发言保留", clean("assistant", "嗯，雪下了一天"), "你：嗯，雪下了一天")
    c.equal("主动发起占位渲染成旁白", clean("user", placeholder), "（你主动发了下面这条）")
    c.equal("回填占位渲染成旁白", clean("user", echo), "（你此前主动发过下面这条）")
    c.equal("纯注入记录被丢弃", clean("user", "【现在的时间】2026-11-16 14:35"), "")
    c.equal("空内容被丢弃", clean("assistant", "   "), "")
    c.check("长文本被截断", len(clean("user", "字" * 500)) <= 305)
    c.equal("普通短句不误伤", clean("user", "我刚回来"), "用户：我刚回来")


def main() -> int:
    c = Checker()
    test_season_weather(c)
    test_scan(c)
    test_categories(c)
    test_flavor_rotation(c)
    test_prompt_placeholders(c)
    test_history_cleanup(c)
    print(f"\n共 {c.count} 项断言，失败 {len(c.failures)} 项")
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
