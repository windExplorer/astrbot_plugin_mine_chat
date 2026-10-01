"""日程的「读侧」：定位当前/前后片段，并渲染成注入对话的文案。

纯函数为主，便于单测：输入条目列表 + 当前分钟数，输出文案。
跨天处理：调用方需先把 now 换算成「相对该日程起点」的分钟数
（见 `now_minutes_for`），这样 23:30-06:30 的睡眠片段才能正确命中。
"""

from __future__ import annotations

from datetime import date as date_cls, datetime
from typing import Any

from .config import Settings, fmt_hhmm


def injection_reject_reason(
    *,
    umo_kind_str: str,
    is_primary: bool,
    primary_umo: str | None,
    binding: dict[str, Any] | None,
    persona_id: str,
    settings: Settings,
) -> str:
    """判断某窗口当前是否应注入日程；返回拒绝原因（空串 = 允许注入）。

    规则（v1.0.9 起）：
    - **主窗口（主动消息投递目标）无条件注入**——它本来就是启用条件的一部分，
      用户配置了它就意味着期望日程在这个窗口生效（即使绑定表里没有这条记录，
      例如投递窗口是靠配置 primary_umo 指定的）；
    - 其他窗口：已绑定到当前人格的注入；未绑定的窗口需要 auto_bind 开启
      （调用方会先登记再判定）。
    """
    if umo_kind_str not in settings.inject_scopes:
        return f"窗口类型 {umo_kind_str or 'unknown'} 不在注入范围内（inject.scopes）"
    if not primary_umo:
        return "插件还没有可用的投递窗口（人格与窗口未配置完整）"
    if is_primary:
        return ""
    if binding is not None:
        if str(binding.get("persona_id") or "") != persona_id:
            return "该窗口绑定的是其他人格的日程"
        if not int(binding.get("enabled") or 0):
            return "该窗口的绑定已被停用"
        return ""
    if not settings.window_auto_bind:
        return "该窗口未绑定到当前人格，且「自动登记窗口绑定」未开启"
    return ""

_WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

_SLEEP_KEYWORDS = ("睡", "入眠", "梦乡", "歇下")


def now_minutes_for(plan_date: str | None, now: datetime | None = None) -> int:
    """把当前时间换算成相对日程起点的分钟数（跨天时加 1440 的倍数）。"""
    current = now or datetime.now()
    base = current.hour * 60 + current.minute
    if not plan_date:
        return base
    try:
        target = datetime.strptime(plan_date, "%Y-%m-%d").date()
    except ValueError:
        return base
    delta_days = (current.date() - target).days
    return base + max(0, delta_days) * 24 * 60


def locate(
    items: list[dict[str, Any]], now_minutes: int
) -> dict[str, dict[str, Any] | None]:
    """定位当前片段、刚结束的片段、即将开始的片段。"""
    result: dict[str, dict[str, Any] | None] = {
        "current": None,
        "previous": None,
        "upcoming": None,
    }
    if not items:
        return result

    ordered = sorted(items, key=lambda item: int(item.get("start_min") or 0))
    current_index = -1
    for index, item in enumerate(ordered):
        start = int(item.get("start_min") or 0)
        end = int(item.get("end_min") or 0)
        if start <= now_minutes < end:
            current_index = index
            break

    if current_index >= 0:
        result["current"] = ordered[current_index]
        if current_index > 0:
            result["previous"] = ordered[current_index - 1]
        if current_index + 1 < len(ordered):
            result["upcoming"] = ordered[current_index + 1]
        return result

    # 落在片段之间的空隙：前一条当作「刚才」，后一条当作「稍后」。
    previous: dict[str, Any] | None = None
    upcoming: dict[str, Any] | None = None
    for item in ordered:
        if int(item.get("end_min") or 0) <= now_minutes:
            previous = item
        elif upcoming is None:
            upcoming = item
            break
    result["previous"] = previous
    result["upcoming"] = upcoming
    return result


def is_sleeping(item: dict[str, Any] | None) -> bool:
    if not item:
        return False
    activity = str(item.get("activity") or "")
    return any(keyword in activity for keyword in _SLEEP_KEYWORDS)


def block_text(item: dict[str, Any]) -> str:
    start = fmt_hhmm(int(item.get("start_min") or 0))
    end = fmt_hhmm(int(item.get("end_min") or 0))
    activity = str(item.get("activity") or "").strip()
    mood = str(item.get("mood") or "").strip()
    return f"{start}-{end} {activity}" + (f"（心情：{mood}）" if mood else "")


def build_injection_block(
    *,
    items: list[dict[str, Any]],
    now_minutes: int,
    settings: Settings,
    plan_date: str | None = None,
    last_proactive: str = "",
    unanswered: int = 0,
    now: datetime | None = None,
) -> str:
    """渲染注入到对话上下文的日程块；无可用信息时返回空串。"""
    if not items:
        return ""

    current = now or datetime.now()
    slots = locate(items, now_minutes)
    current_item = slots["current"]
    previous_item = slots["previous"]
    upcoming_item = slots["upcoming"]

    lines: list[str] = []
    stamp = f"{current.strftime('%Y-%m-%d')} {_WEEKDAY_NAMES[current.weekday()]} {current.strftime('%H:%M')}"
    lines.append(f"【现在的时间】{stamp}")

    if current_item is not None:
        lines.append(f"【你此刻】{block_text(current_item)}")
    else:
        lines.append("【你此刻】（这段时间没有特别安排，属于空白时间）")

    if previous_item is not None:
        age = now_minutes - int(previous_item.get("end_min") or 0)
        if 0 <= age <= settings.inject_lookback_minutes:
            lines.append(f"【刚才】{block_text(previous_item)}")

    if upcoming_item is not None:
        delta = int(upcoming_item.get("start_min") or 0) - now_minutes
        if 0 <= delta <= settings.inject_lookahead_minutes:
            minutes_text = f"（约 {delta} 分钟后）" if delta > 0 else "（马上）"
            lines.append(f"【稍后】{block_text(upcoming_item)}{minutes_text}")

    if settings.inject_include_seed:
        seeds: list[str] = []
        for item in (current_item, previous_item):
            if not item:
                continue
            seed = str(item.get("message_seed") or "").strip()
            if seed and seed not in seeds:
                seeds.append(seed)
        if seeds:
            lines.append("【可以顺口提的】" + "；".join(seeds))

    if last_proactive:
        text = last_proactive.strip().replace("\n", " ")
        if len(text) > 120:
            text = text[:120] + "…"
        extra = "（用户还没回你）" if unanswered > 0 else ""
        lines.append(f"【你刚才主动发的】{text}{extra}")

    if is_sleeping(current_item):
        lines.append("【状态】你现在正在睡觉。用户的消息算是把你吵醒，回复可以带一点迷糊和困意。")

    lines.append(
        "以上是你自己的生活设定，不是用户的经历。用户不问就不要主动汇报细节；"
        "被问到再自然地说出来，不要像念行程表。"
    )

    block = "\n".join(lines)
    limit = settings.inject_max_chars
    if limit and len(block) > limit:
        block = block[:limit].rstrip() + "…"
    return block


def describe_structured(items: list[dict[str, Any]], now_minutes: int) -> dict[str, Any]:
    """控制台用的结构化现状：当前条目 + 之后两条，供前端 tag 化渲染。"""

    def pack(item: dict[str, Any] | None) -> dict[str, str] | None:
        if not item:
            return None
        return {
            "time_text": f"{fmt_hhmm(int(item['start_min']))}-{fmt_hhmm(int(item['end_min']))}",
            "activity": str(item.get("activity") or ""),
            "mood": str(item.get("mood") or ""),
        }

    slots = locate(items, now_minutes)
    current = pack(slots["current"])
    upcoming: list[dict[str, str]] = []
    for item in sorted(
        (it for it in items if int(it["end_min"]) > now_minutes),
        key=lambda it: int(it["start_min"]),
    ):
        if current and int(item["start_min"]) < now_minutes:
            continue  # 当前条目自身
        packed = pack(item)
        if packed:
            upcoming.append(packed)
        if len(upcoming) >= 2:
            break
    return {"current": current, "upcoming": upcoming}


def describe(items: list[dict[str, Any]], now_minutes: int) -> str:
    """给控制台/指令用的一句话现状描述。"""
    slots = locate(items, now_minutes)
    current_item = slots["current"]
    if not current_item:
        return "（现在没有安排）"
    text = block_text(current_item)
    upcoming_item = slots["upcoming"]
    if upcoming_item:
        text += f" → 之后：{block_text(upcoming_item)}"
    return text


def today_date_text() -> str:
    return date_cls.today().isoformat()
