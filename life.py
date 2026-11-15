"""生活逻辑层：季节 / 天气 / 生活面 / 矛盾扫描。

为什么单独一个模块：日程「前后矛盾」和「无聊」是两个不同的问题，但都只能在
**代码侧**真正解决——提示词写在生成之前，模型照样会写出「冬天下雪又晒被子」
「说好一整天不出门，下午去了超市」。这里做四件事：

1. 给每天一份**确定性的季节与天气事实**（seed = persona + 日期），
   同一天多次重试不漂移，跨天必然不同，人物「在同一天气里」生活；
2. 把「今天必须覆盖的生活面」变成可校验的要求（避免一整天只有工作和吃饭）；
3. 扫描**高置信矛盾**：反季、天气冲突、睡眠时段里的清醒活动、
   基调与外出冲突、身体状态断线——只报能明确判定的，宁可漏报不误报，
   否则会一路重试到兜底模板，比矛盾更糟；
4. 提供生活面归类，供「最近几天写过什么」做轮换。

纯标准库，不依赖 astrbot 运行时（测试里用 AST 抠函数直接跑）。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

# --------------------------------------------------------------------------- #
# 季节
# --------------------------------------------------------------------------- #
_SEASON_BY_MONTH = {
    12: "冬", 1: "冬", 2: "冬",
    3: "春", 4: "春", 5: "春",
    6: "夏", 7: "夏", 8: "夏",
    9: "秋", 10: "秋", 11: "秋",
}
_SEASON_DETAIL = {
    12: "深冬", 1: "隆冬", 2: "冬末",
    3: "初春", 4: "仲春", 5: "暮春",
    6: "初夏", 7: "盛夏", 8: "夏末",
    9: "初秋", 10: "深秋", 11: "深秋",
}
_SEASON_FEEL = {
    "冬": "冷，出门要穿厚外套；白昼短，天黑得早",
    "春": "转暖但反复，早晚仍有凉意",
    "夏": "热，日照长，白天不宜久待户外",
    "秋": "凉爽干燥，早晚温差大，天黑得比夏天早",
}


def season_of(month: int) -> str:
    """月份 → 季节（12/1/2 冬，3/4/5 春，6/7/8 夏，9/10/11 秋）。"""
    try:
        return _SEASON_BY_MONTH.get(int(month), "春")
    except (TypeError, ValueError):
        return "春"


def season_label(month: int) -> str:
    try:
        return _SEASON_DETAIL.get(int(month), "平常时节")
    except (TypeError, ValueError):
        return "平常时节"


def season_line(month: int) -> str:
    """「深秋（11 月）：凉爽干燥……」这样的完整一句。"""
    season = season_of(month)
    detail = season_label(month)
    return f"{detail}（{int(month)} 月）：{_SEASON_FEEL.get(season, '')}。"


# 反季词表：只在**该季节不可能**的强信号上报警。
# 「围巾」「热饮」这类边缘物象故意不收，避免误伤。
_SEASON_CONFLICT: dict[str, tuple[str, ...]] = {
    "冬": ("蝉鸣", "蝉声", "西瓜", "凉席", "短袖", "短裤", "冰镇", "冰淇淋",
           "雪糕", "空调开", "电风扇", "蚊子", "中暑"),
    "夏": ("羽绒服", "毛衣", "棉袄", "暖气", "积雪", "雪地", "结了冰", "呵气成霜",
           "冻手", "手套"),
}


# --------------------------------------------------------------------------- #
# 天气
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Weather:
    """一天的天气事实（不可变，便于跨函数传）。"""

    kind: str          # snow / rain / overcast / clear / wind
    label: str         # 小雪 / 阴雨 / 阴天 / 晴 / 大风
    desc: str          # 可直接拼进提示词的一句话
    feel: str          # 冷 / 凉 / 温和 / 热

    def line(self) -> str:
        return f"{self.label}：{self.desc}（体感{self.feel}）"


_WEATHER_TEXT: dict[str, tuple[str, str, str]] = {
    "snow": ("小雪", "断断续续下着小雪，风里带着湿冷，路面有薄薄一层白", "冷"),
    "rain": ("阴雨", "一直在下细雨，路面湿滑，收伞时袖口会湿", "凉"),
    "overcast": ("阴天", "天色发灰，一整天没怎么见到太阳", "凉"),
    "clear": ("晴", "天气晴，光线很亮，影子很清楚", "温和"),
    "wind": ("大风", "风一直没停，窗户有响动，走在路上要压住衣角", "冷"),
}

# (season -> [(kind, weight)])
_WEATHER_WEIGHT: dict[str, tuple[tuple[str, float], ...]] = {
    "冬": (("snow", 0.30), ("overcast", 0.32), ("clear", 0.26), ("wind", 0.12)),
    "春": (("rain", 0.28), ("overcast", 0.28), ("clear", 0.36), ("wind", 0.08)),
    "夏": (("rain", 0.24), ("clear", 0.46), ("overcast", 0.22), ("wind", 0.08)),
    "秋": (("clear", 0.40), ("overcast", 0.28), ("rain", 0.24), ("wind", 0.08)),
}
_COLD_REGION_WEIGHT = (("snow", 0.52), ("overcast", 0.24), ("clear", 0.14), ("wind", 0.10))
_WARM_REGION_WINTER = (("rain", 0.40), ("overcast", 0.30), ("clear", 0.28), ("wind", 0.02))

_COLD_HINTS = ("雪国", "北方", "严寒", "零下", "终年积雪", "积雪不化", "冬天很长",
               "寒带", "极北", "边境小镇")
_WARM_HINTS = ("南方", "热带", "海边", "沿海", "潮湿", "炎热", "亚热带", "海岛", "梅雨")


def region_of(persona_text: str) -> str:
    """从人格/世界观文本判断气候带：cold / warm / temperate。

    只是决定天气权重的倾向，判错也不致命（冬天总归是冬天）。
    """
    text = str(persona_text or "")[:4000]
    cold = sum(1 for word in _COLD_HINTS if word in text)
    warm = sum(1 for word in _WARM_HINTS if word in text)
    if cold > warm and cold > 0:
        return "cold"
    if warm > cold and warm > 0:
        return "warm"
    return "temperate"


def _pick_weighted(rng: random.Random, table: tuple[tuple[str, float], ...]) -> str:
    roll = rng.random()
    acc = 0.0
    for kind, weight in table:
        acc += weight
        if roll < acc:
            return kind
    return table[-1][0]


def weather_for(persona_id: str, plan_date: str, region: str = "temperate") -> Weather:
    """确定性天气：同一天同一角色永远得到同一个天气。"""
    try:
        month = int(str(plan_date).split("-")[1])
    except (IndexError, ValueError):
        month = 1
    season = season_of(month)
    rng = random.Random(f"weather:{persona_id}:{plan_date}")
    table = _WEATHER_WEIGHT.get(season, _WEATHER_WEIGHT["春"])
    if region == "cold" and season == "冬":
        table = _COLD_REGION_WEIGHT
    elif region == "warm" and season == "冬":
        table = _WARM_REGION_WINTER
    kind = _pick_weighted(rng, table)
    label, desc, feel = _WEATHER_TEXT.get(kind, _WEATHER_TEXT["overcast"])
    if season == "夏" and kind == "clear":
        feel = "热"
    elif season == "冬" and kind in {"snow", "wind"}:
        feel = "冷"
    return Weather(kind=kind, label=label, desc=desc, feel=feel)


# 天气与条目内容的硬冲突：下雪/下雨却写晒被子，晴天却写打伞下雪。
_WEATHER_CONFLICT: dict[str, tuple[str, ...]] = {
    "snow": ("晒被子", "晾被子", "晾在阳台", "擦汗", "开空调", "凉席", "蝉鸣", "烈日"),
    "rain": ("晒被子", "晾被子", "晾晒", "擦汗", "凉席", "阳光晒"),
    "clear": ("下雪", "下雨", "雨伞", "打伞", "雨衣", "湿透", "雨声", "积雪", "雪地"),
    "overcast": ("晒太阳", "阳光很好", "烈日", "晒被子"),
    "wind": ("一丝风都没有", "无风", "风平浪静"),
}


# --------------------------------------------------------------------------- #
# 时段
# --------------------------------------------------------------------------- #
# 睡眠时段里不该出现的「清醒活动」：只收工作/采购/正餐这些强信号。
# 喝水、上厕所、看手机这类半夜会做的事故意不收。
_AWAKE_WORDS = (
    "上班", "公司", "开会", "客户", "加班", "上学", "上课", "自习",
    "超市", "菜市场", "买菜", "逛街", "午饭", "晚饭", "早餐",
)
_SLEEP_WORDS = ("睡", "入眠", "床", "梦", "赖床", "补觉", "睡眠")


def _hhmm(minutes: Any) -> str:
    try:
        value = int(minutes) % (24 * 60)
    except (TypeError, ValueError):
        return "??:??"
    return f"{value // 60:02d}:{value % 60:02d}"


def _in_window(start: int, end: int, window_start: int, window_end: int) -> bool:
    """半开区间 [start, end) 是否完全落在 [window_start, window_end) 内（支持跨午夜）。"""
    if window_start == window_end:
        return False

    def _shift(value: int) -> int:
        return value + 24 * 60 if value < window_start else value

    ws = window_start
    we = _shift(window_end) if window_end <= window_start else window_end
    return _shift(start) >= ws and _shift(end - 1) <= we


def time_conflicts(
    items: list[dict[str, Any]], sleep_start_min: int, sleep_end_min: int
) -> list[str]:
    """睡眠时段里出现工作/采购/正餐 → 「半夜在上班」这类逻辑断裂。"""
    problems: list[str] = []
    for item in items:
        try:
            start = int(item.get("start_min") or 0)
            end = int(item.get("end_min") or 0)
        except (TypeError, ValueError):
            continue
        activity = str(item.get("activity") or "")
        if any(word in activity for word in _SLEEP_WORDS):
            continue
        if not _in_window(start, end, sleep_start_min, sleep_end_min):
            continue
        hit = next((word for word in _AWAKE_WORDS if word in activity), "")
        if hit:
            problems.append(
                f"{_hhmm(start)}-{_hhmm(end)}「{activity}」落在睡觉时段里（作息是 "
                f"{_hhmm(sleep_start_min)} 睡、{_hhmm(sleep_end_min)} 起），"
                f"「{hit}」和睡觉冲突"
            )
    return problems


# --------------------------------------------------------------------------- #
# 生活面（品类）
# --------------------------------------------------------------------------- #
# 顺序有意义：先匹配更具体的类别，命中即返回。
_CATEGORY_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("睡眠", ("睡", "入眠", "赖床", "补觉", "午睡", "梦")),
    ("用餐", ("吃", "饭", "早餐", "午餐", "晚餐", "夜宵", "做饭", "煮", "外卖",
              "食堂", "零食", "下厨", "面包", "面", "汤")),
    ("工作", ("上班", "公司", "开会", "工作", "写", "改稿", "赶工", "客户", "加班",
              "上课", "自习", "作业", "复习", "报告", "资料", "画稿", "剪辑", "课")),
    ("家务", ("收拾", "打扫", "洗衣", "晾", "擦", "整理", "倒垃圾", "洗碗", "拖地",
              "拖", "清理", "采购", "补货", "修")),
    ("外出", ("出门", "上街", "街上", "超市", "菜市场", "地铁", "公交", "散步",
              "路", "店", "商场", "公园", "车站", "走远")),
    ("社交", ("邻居", "朋友", "同事", "家人", "电话", "同学", "打招呼", "客人",
              "串门", "消息")),
    ("身体", ("洗澡", "洗头", "泡", "面膜", "剪指甲", "拉伸", "理发", "换衣",
              "照镜子", "暖手")),
    ("休闲", ("发呆", "看窗外", "刷", "游戏", "听歌", "看书", "看剧", "电影",
              "画", "晒太阳", "猫", "音乐", "翻", "抬头", "靠", "躺", "喝茶",
              "咖啡", "热可可", "旧书", "雪")),
)


def category_of(text: Any) -> str:
    """一条活动属于哪个生活面（命中第一个类别）。"""
    value = str(text or "")
    if not value:
        return "其他"
    for name, words in _CATEGORY_WORDS:
        if any(word in value for word in words):
            return name
    return "其他"


def categories_of(items: list[dict[str, Any]]) -> set[str]:
    """整天覆盖到哪些生活面（不含睡眠，睡眠人人都有）。"""
    found: set[str] = set()
    for item in items:
        name = category_of(item.get("activity"))
        if name and name != "睡眠":
            found.add(name)
    return found


def missing_categories(
    items: list[dict[str, Any]], required: list[str] | None
) -> list[str]:
    """要求的「今天必须有」的生活面里，哪些缺失。"""
    if not required:
        return []
    present = categories_of(items)
    return [name for name in required if name not in present]


# 可选生活面池：从里面挑「今天必须有」的一到两项，避免每天都是同样的要求。
# 刻意**不含「社交」**：日程铁律禁止编造设定之外的人际关系，把「社交」设成硬要求
# 会逼模型去写「和闺蜜聊了一下午」这类凭空人物。与外面世界的接触改由
# 「至少一条和外面世界有关（快递 / 邻居 / 街上的动静）」这条软要求承担。
_OPTIONAL_CATEGORIES = ("家务", "休闲", "身体", "外出")


def pick_required_categories(
    persona_id: str, plan_date: str, recent: list[str] | None = None
) -> list[str]:
    """今天必须有到 1~2 个生活面（避开最近几天已经要求过的）。"""
    rng = random.Random(f"categories:{persona_id}:{plan_date}")
    pool = [name for name in _OPTIONAL_CATEGORIES if name not in (recent or [])]
    if not pool:
        pool = list(_OPTIONAL_CATEGORIES)
    count = 1 if rng.random() < 0.45 else 2
    return rng.sample(pool, min(count, len(pool)))


# --------------------------------------------------------------------------- #
# 身体状态连续性
# --------------------------------------------------------------------------- #
_ILLNESS_WORDS = ("感冒", "发烧", "生病", "不舒服", "咳嗽", "咳", "头疼", "胃", "痛",
                  "伤", "过敏", "鼻塞", "嗓子", "难受")
_RECOVER_WORDS = ("好了", "好多了", "退了", "痊愈", "恢复", "不难受", "没事了",
                  "吃完药", "停药", "消了", "活过来")


def status_continuity(
    status_notes: list[str] | None, items: list[dict[str, Any]]
) -> list[str]:
    """昨天留下的身体状态，今天必须「延续」或「交代结束」。

    只对疾病类状态报警：模型自己写了「感冒还没好」，第二天整天一个字不提，
    就是最典型的跨天矛盾。物品类（冰箱里的菜）不判，避免误伤。
    """
    texts = " ".join(
        f"{item.get('activity') or ''} {item.get('message_seed') or ''}"
        for item in (items or [])
    )
    if any(word in texts for word in _RECOVER_WORDS):
        return []
    problems: list[str] = []
    for note in status_notes or []:
        if not any(word in note for word in _ILLNESS_WORDS):
            continue
        if any(word in texts for word in _ILLNESS_WORDS):
            continue
        problems.append(
            f"昨天留下「{note}」，今天所有条目都没有提到它——"
            f"要么延续（还没好），要么在一条里明确交代已经好了"
        )
    return problems[:2]


# --------------------------------------------------------------------------- #
# 基调（外出 / 不出门）
# --------------------------------------------------------------------------- #
_OUTDOOR_WORDS = ("出门", "上街", "街上", "去超市", "去菜市场", "坐公交", "坐地铁",
                  "去商场", "去公园", "车站", "走了很远", "去了趟", "走到")
_NEAR_HOME_WORDS = ("楼下", "门口", "阳台", "小区", "窗边", "走廊", "楼道", "附近")


def pace_conflicts(items: list[dict[str, Any]], pace_kind: str) -> list[str]:
    """基调与是否出门必须一致（「几乎不出门的一天」却跑去超市 = 自相矛盾）。"""
    texts = [
        (item.get("activity") or "", str(item.get("message_seed") or ""))
        for item in (items or [])
    ]
    if pace_kind == "indoor":
        for activity, seed in texts:
            combined = f"{activity} {seed}"
            if not any(word in combined for word in _OUTDOOR_WORDS):
                continue
            if any(word in combined for word in _NEAR_HOME_WORDS):
                continue
            return [f"今天的基调是「几乎不出门」，但「{activity}」描述了一次外出"]
        return []
    if pace_kind == "out":
        if any(
            any(word in combined for word in _OUTDOOR_WORDS)
            for combined in (f"{a} {s}" for a, s in texts)
        ):
            return []
        return ["今天的基调是「出门走动的一天」，但没有任何一条写外出"]
    return []


# --------------------------------------------------------------------------- #
# 总入口
# --------------------------------------------------------------------------- #
def scan(
    items: list[dict[str, Any]],
    *,
    month: int,
    weather_kind: str = "",
    season: str = "",
    pace_kind: str = "",
    sleep_start_min: int = 0,
    sleep_end_min: int = 0,
    status_notes: list[str] | None = None,
) -> list[str]:
    """扫描高置信矛盾，返回人类可读的问题描述（最多 6 条）。

    只报能明确判定的问题：反季、天气冲突、睡眠时段清醒活动、基调冲突、
    身体状态断线。宁可漏报也不误报——误报会把生成拖进「永远重试 → 兜底模板」。
    """
    season = season or season_of(month)
    problems: list[str] = []
    season_words = _SEASON_CONFLICT.get(season, ())
    weather_words = _WEATHER_CONFLICT.get(weather_kind, ())

    for item in items or []:
        activity = str(item.get("activity") or "")
        if not activity:
            continue
        combined = f"{activity} {item.get('message_seed') or ''}"
        stamp = _hhmm(item.get("start_min"))
        for word in season_words:
            if word in combined:
                problems.append(f"{stamp}「{activity}」里的「{word}」与{season}季不符")
                break
        for word in weather_words:
            if word in combined:
                problems.append(
                    f"{stamp}「{activity}」里的「{word}」与今天的天气（{weather_kind}）冲突"
                )
                break
        if len(problems) >= 4:
            break

    problems.extend(time_conflicts(items or [], sleep_start_min, sleep_end_min))
    problems.extend(pace_conflicts(items or [], pace_kind))
    problems.extend(status_continuity(status_notes, items or []))
    return problems[:6]
