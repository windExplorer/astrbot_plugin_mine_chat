"""时段判定：把「现在几点」变成模型无法自由发挥的硬事实。

为什么单独一个模块（v1.0.42）：
用户实测反馈——下午四点收到的主动消息里写「天已经完全黑了，在考虑睡不睡觉」，
配图也是夜景。根因不是「提示词里没有时间」（v1.0.25 就把【现在的时间】写进
提示词了），而是**时刻只是一串数字**：「16:00」对模型没有约束力，它照样按
文学先验把「一天快结束了 / 傍晚」写进下午的片段；出图提示词同样如此。

真正管用的做法是把时刻翻译成模型无法回避的事实：此刻是白天还是夜里、
光线什么状态、离天黑还有多久、哪些词此刻出现就是错。于是本模块产出：

- ``phase_of(now)``      时段对象（名称 / 光照 / 白天-黄昏-夜晚分组）；
- ``phase_line(now)``    一行式「日期 周X HH:MM —— 时段（光照）」；
- ``lighting_fact(now)`` 写进提示词的中文硬约束块；
- ``lighting_tags(now)`` 出图提示词应带的光照标签；
- ``find_time_conflicts(text, now)`` 生成后校验：文案里出现了与此刻时段矛盾的
  词就返回它们，调用方据此重写一次（模型层不可靠，代码层兜底）；
- ``plan_conflicts(items, month)``   日程生成期校验（条目窗口 vs 描写时段）；
- ``image_phase_hint`` / ``phase_accepts`` 本地图库按文件名时段线索选图。

日出/日落用北半球中纬度（约北纬 30°）的月度近似值：它只是给模型的天色锚点，
不是天文历——±20 分钟的误差足以拦住「下午四点天就黑了」这类明显矛盾。

本模块只依赖标准库，且不做任何相对导入：自检脚本可以 `import daylight`
直接跑真代码（同 config.py 的做法）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

_HHMM_RE = re.compile(r"^\s*(\d{1,2})\s*[:：]\s*(\d{1,2})")

# 北半球中纬度月度近似日出/日落（当地时间）。只作天色锚点，不是天文历。
_MONTHLY_SUNRISE: dict[int, str] = {
    1: "07:10", 2: "06:50", 3: "06:20", 4: "05:50",
    5: "05:30", 6: "05:15", 7: "05:20", 8: "05:40",
    9: "06:00", 10: "06:20", 11: "06:45", 12: "07:05",
}
_MONTHLY_SUNSET: dict[int, str] = {
    1: "17:40", 2: "18:10", 3: "18:30", 4: "18:45",
    5: "19:00", 6: "19:20", 7: "19:20", 8: "19:00",
    9: "18:30", 10: "18:00", 11: "17:40", 12: "17:30",
}

# 时段元数据：key -> (中文名, 光照描述, 分组, 是否已天黑)
#   分组 group 只用于本地图库选图（day / dusk / night）。
_PHASE_META: dict[str, tuple[str, str, str, bool]] = {
    "dawn": ("清晨（天刚亮）", "光线偏暗而柔和", "day", False),
    "morning": ("上午（白天）", "光线明亮", "day", False),
    "noon": ("中午（白天）", "光线充足", "day", False),
    "afternoon": ("下午（白天）", "天还亮着，光线明亮", "day", False),
    "dusk": ("傍晚（日落前后）", "天色正在变暗但还没全黑", "dusk", False),
    "night": ("夜晚（天已经黑了）", "黑暗、夜灯与窗外夜色", "night", True),
}

_NOON_START = 11 * 60 + 30
_NOON_END = 13 * 60 + 30


# --------------------------------------------------------------------------- #
# 时间文本工具
# --------------------------------------------------------------------------- #
def _hhmm_to_minutes(text: str, fallback: int) -> int:
    match = _HHMM_RE.match(str(text or ""))
    if not match:
        return fallback
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return fallback
    return hour * 60 + minute


def sunrise_for_month(month: int) -> str:
    """返回某月的近似日出时刻（HH:MM）；月份越界回退 06:20。"""
    return _MONTHLY_SUNRISE.get(month, "06:20")


def sunset_for_month(month: int) -> str:
    """返回某月的近似日落时刻（HH:MM）；月份越界回退 18:00。"""
    return _MONTHLY_SUNSET.get(month, "18:00")


def stamp_text(ts: float, now: datetime | None = None) -> str:
    """时间戳 → 带相对日期的人话：`今天 15:20` / `昨天 22:10` / `10-11 09:30`。

    为什么需要它（v1.0.43）：主动消息要「跨次连续」，模型必须知道上一次主动
    是什么时候说的——只丢一句纯文本，它分不清那是 2 小时前还是 3 天前，
    于是容易把隔了两天的话当成刚刚说的，或者把同一件事反复讲。
    """
    current = now or datetime.now()
    try:
        moment = datetime.fromtimestamp(float(ts))
    except (TypeError, ValueError, OSError):
        return ""
    delta_days = (current.date() - moment.date()).days
    if delta_days <= 0:
        label = "今天"
    elif delta_days == 1:
        label = "昨天"
    elif delta_days == 2:
        label = "前天"
    else:
        label = moment.strftime("%m-%d")
    return f"{label} {moment.strftime('%H:%M')}"


def ago_text(ts: float, now: datetime | None = None) -> str:
    """时间戳 → 距今多久：`刚刚` / `25 分钟前` / `3 小时前` / `2 天前`。"""
    current = now or datetime.now()
    try:
        moment = datetime.fromtimestamp(float(ts))
    except (TypeError, ValueError, OSError):
        return ""
    minutes = int((current - moment).total_seconds() // 60)
    if minutes < 1:
        return "刚刚"
    if minutes < 60:
        return f"{minutes} 分钟前"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} 小时前"
    return f"{hours // 24} 天前"


# --------------------------------------------------------------------------- #
# 时段判定
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Phase:
    """一次时段判定的结果（不可变，便于跨模块传递与断言）。"""

    key: str          # dawn / morning / noon / afternoon / dusk / night
    name: str         # 中文时段名（提示词直接用）
    light: str        # 光照描述
    group: str        # day / dusk / night（图库选图用）
    is_dark: bool     # 是否已经天黑
    sunrise_min: int
    sunset_min: int

    @property
    def is_day(self) -> bool:
        return not self.is_dark and self.group == "day"


def phase_of(now: datetime) -> Phase:
    """判定 ``now`` 落在哪个时段。

    边界用当月日出/日落推出（各留 60 分钟过渡带）：
    日出前 1 小时到日出后 1 小时 = 清晨；日落前 1 小时到日落后 1 小时 = 傍晚；
    其余白天按钟点分上午 / 中午 / 下午；两端为夜晚。
    """
    sunrise = _hhmm_to_minutes(sunrise_for_month(now.month), 6 * 60 + 20)
    sunset = _hhmm_to_minutes(sunset_for_month(now.month), 18 * 60)
    minute = now.hour * 60 + now.minute

    dawn_start, dawn_end = sunrise - 60, sunrise + 60
    dusk_start, dusk_end = sunset - 60, sunset + 60

    if dawn_start <= minute < dawn_end:
        key = "dawn"
    elif dawn_end <= minute < _NOON_START:
        key = "morning"
    elif _NOON_START <= minute < _NOON_END:
        key = "noon"
    elif _NOON_END <= minute < dusk_start:
        key = "afternoon"
    elif dusk_start <= minute < dusk_end:
        key = "dusk"
    else:
        key = "night"

    name, light, group, is_dark = _PHASE_META[key]
    return Phase(
        key=key,
        name=name,
        light=light,
        group=group,
        is_dark=is_dark,
        sunrise_min=sunrise,
        sunset_min=sunset,
    )


def phase_line(now: datetime) -> str:
    """一行式时段描述：`2026-10-13 周二 16:05 —— 下午（白天），光线明亮`。"""
    phase = phase_of(now)
    stamp = f"{now.strftime('%Y-%m-%d')} {_WEEKDAYS[now.weekday()]} {now.strftime('%H:%M')}"
    return f"{stamp} —— {phase.name}，{phase.light}"


def _minutes_text(minutes: int) -> str:
    minutes = int(minutes) % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _distance_to_sunset(now: datetime, phase: Phase) -> str:
    """离日落还有多久（白天用）/ 已经入夜多久（夜晚用）。"""
    minute = now.hour * 60 + now.minute
    if phase.is_dark:
        after = minute - phase.sunset_min
        if minute < phase.sunset_min:  # 凌晨（过了午夜）
            after = minute + 24 * 60 - phase.sunset_min
        return f"（已经入夜约 {after // 60} 小时 {after % 60} 分钟）"
    if phase.key == "dusk":
        return "（日落前后，天色正在变暗）"
    if phase.key == "dawn":
        return "（天刚亮，太阳刚出来）"
    remain = phase.sunset_min - minute
    if remain < 0:
        remain = 0
    return f"（距离日落还有约 {remain // 60} 小时 {remain % 60} 分钟，天没有黑）"


_DAY_CONSTRAINT = (
    "现在不是夜晚：天还亮着，不许出现「天黑 / 夜色 / 星空 / 月亮 / 灯火 / "
    "该睡了 / 晚安 / 睡前」这类夜晚描写与睡意——你的一天远没到尾声。"
    "只有日落之后才可以写夜晚。"
)
_DAWN_CONSTRAINT = (
    "现在是清晨（天刚亮）：可以写晨光、早起、清晨的凉意；"
    "但不许写「天黑透了 / 深夜 / 半夜 / 星空 / 满天星」这类深夜描写。"
)
_DUSK_CONSTRAINT = (
    "现在是日落前后的过渡时段：可以写「天色渐暗 / 天边泛红 / 夕阳」；"
    "但不许写「天已经全黑 / 深夜 / 半夜 / 满天星」——还没到夜里。"
)
_NIGHT_CONSTRAINT = (
    "现在是夜晚（天已经黑了）：可以写夜色、灯光、困意与睡前的事；"
    "但不许出现「阳光 / 大太阳 / 白天 / 清晨 / 天亮」这类白天描写。"
)


def lighting_fact(now: datetime) -> str:
    """写进提示词的中文硬约束块（三行：此刻时段 / 天色边界 / 天色铁律）。"""
    phase = phase_of(now)
    if phase.is_dark:
        constraint = _NIGHT_CONSTRAINT
    elif phase.key == "dusk":
        constraint = _DUSK_CONSTRAINT
    elif phase.key == "dawn":
        constraint = _DAWN_CONSTRAINT
    else:
        constraint = _DAY_CONSTRAINT
    boundary = (
        f"【今天的天色边界】日出约 {_minutes_text(phase.sunrise_min)}、"
        f"日落约 {_minutes_text(phase.sunset_min)}"
        f"{_distance_to_sunset(now, phase)}"
    )
    return "\n".join(
        [f"【此刻时段】{phase_line(now)}", boundary, f"【天色铁律】{constraint}"]
    )


_LIGHTING_TAGS: dict[str, str] = {
    "dawn": "dawn, sunrise, soft warm light, early morning",
    "morning": "morning, daytime, bright sunlight, clear sky",
    "noon": "noon, daytime, bright, sunlight",
    "afternoon": "afternoon, daytime, daylight, bright, clear sky",
    "dusk": "dusk, sunset, golden hour, warm dim light",
    "night": "night, dark sky, moonlight, indoor lamp light",
}


def lighting_tags(now: datetime) -> str:
    """当前时段对应的英文光照标签（出图提示词必须带上其中一个方向）。"""
    return _LIGHTING_TAGS.get(phase_of(now).key, _LIGHTING_TAGS["afternoon"])


# --------------------------------------------------------------------------- #
# 生成后校验：文案里的时段矛盾
# --------------------------------------------------------------------------- #
# 文案层（主动消息正文 / 出图提示词）：命中即判为与此刻时段矛盾。
# 宁可偶尔误报——代价只是多花一次模型调用重写，远好过发出一条「下午四点天黑了」。
_NIGHT_MARKERS: tuple[str, ...] = (
    # 「黑了 / 全黑 / 黑透」是关键：用户收到的原话是「天已经完全黑了」，
    # 它并不含「天黑」这个子串——只列「天黑」会漏掉最常见的那句。
    "黑了", "全黑", "黑透", "天暗", "暗下来", "天黑", "夜幕", "夜色", "入夜",
    "夜里", "深夜", "半夜", "夜深", "星空", "星星", "满天星", "月亮", "月光",
    # 刻意不收「睡了 / 熬夜 / 失眠」：「刚睡了个午觉」「昨晚熬夜」都是下午的
    # 合法说法（「睡觉 / 该睡 / 准备睡 / 晚安」已经能覆盖真正的睡前描写）。
    "路灯", "灯火", "霓虹", "熄灯", "晚安", "睡觉", "入睡", "睡前",
    "该睡", "准备睡", "要睡",
    "night", "midnight", "moonlight", "moon", "starry", "starlit",
    "streetlight", "street light", "lamp light", "lamplight", "city lights",
)
_DAY_MARKERS: tuple[str, ...] = (
    # 只收「光线/天色就是白天」的断言，不收「白天 / 清晨 / 傍晚 / 天亮」这类
    # 时间词——夜里说「白天忙忘了」「清晨就出门了」都是正常的回溯说法，
    # 拿来判矛盾只会白重写一次。
    "阳光", "太阳", "日出", "晨光", "天亮了", "已经天亮", "天大亮",
    "现在是白天", "大白天", "晴空", "蓝天",
    "sunlight", "sunny", "sun", "daytime", "blue sky", "daylight", "noon",
)
# 日程层：只有「无可辩驳」的时段描写才触发整份日程重做（误报代价高得多）。
_STRONG_NIGHT_MARKERS: tuple[str, ...] = (
    "黑了", "全黑", "黑透", "天黑", "夜幕", "夜色", "深夜", "半夜", "星空",
    "满天星", "月亮", "月光",
)
_STRONG_DAY_MARKERS: tuple[str, ...] = (
    "阳光", "太阳", "日出", "晨光", "大太阳", "晴空万里",
)
# 傍晚（日落前后 ±1 小时）本来就该有夜色将至的过程，只拦「确实还早」的描写。
_DUSK_FORBIDDEN: tuple[str, ...] = ("深夜", "半夜", "星空", "满天星", "夜深")

# 否定前缀：命中词前 6 个字里出现这些，说明模型是在否认而不是在描写
# （「天还没黑」「没有夜色」「别熬夜」），不算矛盾。
_NEGATION_PREFIXES: tuple[str, ...] = (
    "还没", "尚未", "不是", "不到", "没有", "别", "不算", "不至于",
)


def _is_negated(prefix: str) -> bool:
    if prefix.endswith("没"):
        return True
    return any(word in prefix for word in _NEGATION_PREFIXES)


def _scan(haystack: str, markers: Iterable[str]) -> list[str]:
    """在 haystack 里找 markers，返回按出现顺序去重的命中词（跳过被否定的）。"""
    found: list[str] = []
    for marker in markers:
        start = 0
        while True:
            index = haystack.find(marker, start)
            if index < 0:
                break
            prefix = haystack[max(0, index - 6) : index]
            if not _is_negated(prefix):
                if marker not in found:
                    found.append(marker)
                break
            start = index + len(marker)
    return found


def find_time_conflicts(text: str, now: datetime) -> list[str]:
    """文案与此刻时段的矛盾词（空列表 = 一致）。

    白天命中夜晚描写，或夜里命中白天描写，都算矛盾；傍晚（过渡时段）只拦
    明显还早的深夜描写。中英文都扫（出图提示词可能是英文标签）。
    """
    if not text:
        return []
    phase = phase_of(now)
    low = text.lower()
    if phase.is_dark:
        return _scan(low, _DAY_MARKERS)
    if phase.key == "dusk":
        return _scan(low, _DUSK_FORBIDDEN)
    if phase.key == "dawn":
        return _scan(low, _STRONG_NIGHT_MARKERS)
    return _scan(low, _NIGHT_MARKERS)


# --------------------------------------------------------------------------- #
# 日程生成期校验：条目窗口 vs 条目描写
# --------------------------------------------------------------------------- #
def plan_conflicts(items: list[dict[str, Any]], month: int) -> list[tuple[int, str]]:
    """找出「描写时段与条目自身时段矛盾」的日程条目。

    返回 [(条目序号, 命中词), ...]。判断只用条目的窗口：
    - 整段都在「日落前 1 小时」之前：出现强夜晚词 = 矛盾（下午四点写天黑）；
    - 整段都在「日落后 1 小时」之后：出现强白天词 = 矛盾（深夜写阳光）；
    - 跨越日出/日落的条目（含跨午夜的睡眠段）不判——那正是「天色渐暗」的
      合法写法。
    """
    sunset = _hhmm_to_minutes(sunset_for_month(month), 18 * 60)
    sunrise = _hhmm_to_minutes(sunrise_for_month(month), 6 * 60 + 20)
    conflicts: list[tuple[int, str]] = []
    for index, item in enumerate(items or []):
        activity = str(item.get("activity") or "")
        if not activity:
            continue
        start = int(item.get("start_min") or 0)
        end = int(item.get("end_min") or 0)
        if end <= sunset - 60:
            # 整段都在日落前 1 小时之前：只有「天亮着」的写法才对
            hits = _scan(activity, _STRONG_NIGHT_MARKERS)
        elif start >= sunset + 60:
            # 整段都在日落后 1 小时之后：只有夜里的写法才对
            hits = _scan(activity, _STRONG_DAY_MARKERS)
        else:
            # 跨越日出/日落的条目（含跨午夜睡眠段、紧贴日出）：不判——
            # 「天色渐暗」「睡到天亮」这类描写本来就是合法的
            hits = []
        if hits:
            conflicts.append((index, hits[0]))
    return conflicts


# --------------------------------------------------------------------------- #
# 本地图库：按文件名里的时段线索选图
# --------------------------------------------------------------------------- #
_DUSK_NAME_HINTS: tuple[str, ...] = (
    "黄昏", "傍晚", "夕阳", "日落", "晚霞", "余晖", "落日", "dusk", "sunset", "golden",
)
_DAY_NAME_HINTS: tuple[str, ...] = (
    "清晨", "早晨", "早上", "晨", "早", "上午", "中午", "正午", "午后", "下午",
    "白天", "阳光", "晴", "蓝天", "白云", "日照",
    "day", "sun", "morning", "noon", "sky",
)
_NIGHT_NAME_HINTS: tuple[str, ...] = (
    "夜", "晚", "灯", "月", "星", "黑暗", "night", "moon", "star", "lamp",
)


def image_phase_hint(filename: str) -> str:
    """从文件名猜这张图的时段：返回 day / dusk / night / ""（无线索）。"""
    name = str(filename or "").strip().lower()
    if not name:
        return ""
    for word in _DUSK_NAME_HINTS:
        if word in name:
            return "dusk"
    for word in _DAY_NAME_HINTS:
        if word in name:
            return "day"
    for word in _NIGHT_NAME_HINTS:
        if word in name:
            return "night"
    return ""


def phase_accepts(group: str, hint: str) -> bool:
    """当前时段（group）能不能用一张带 ``hint`` 线索的照片。"""
    if not hint:
        return True
    return hint == group
