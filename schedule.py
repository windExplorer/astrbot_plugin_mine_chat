"""日程生成：提示词组装 → LLM → 解析 → 时序校验 → 分级重试 → 规则兜底。

对应 PRD D3：一次 LLM 调用产出全天，代码侧严格校验；宁可日程平庸，
也不能没有日程（否则注入与主动消息全部失效）。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from datetime import date as date_cls, datetime, timedelta
from typing import Any

from astrbot.api import logger

from . import bus as bus_mod
from . import kb as kb_mod
from . import llm as llm_mod
from . import prompts
from .config import Settings, fmt_hhmm, parse_hhmm

MIN_ITEM_MINUTES = 10
MAX_ITEM_MINUTES = 600
MIN_COVERAGE_MINUTES = 18 * 60
RETRY_BACKOFF_SECONDS = 15 * 60

# 只覆盖公历固定节日；农历节日（春节/端午/中秋等）无法用固定日期推断，故不处理。
_FIXED_HOLIDAYS: dict[tuple[int, int], str] = {
    (1, 1): "元旦",
    (2, 14): "情人节",
    (3, 8): "妇女节",
    (5, 1): "劳动节",
    (6, 1): "儿童节",
    (10, 1): "国庆节",
    (12, 24): "平安夜",
    (12, 25): "圣诞节",
}
_WEEKDAY_NAMES = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


# --------------------------------------------------------------------------- #
# 纯函数：解析与校验（便于单测）
# --------------------------------------------------------------------------- #
_STRICT_TIME_RE = re.compile(r"^\s*(\d{1,2})\s*[:：]\s*(\d{1,2})\s*$")


def _to_minutes(value: Any) -> int | None:
    """严格解析 HH:MM；解析不出来返回 None（坏条目直接丢弃）。

    注意不能用 config.parse_hhmm 的「宽松回退」语义：那会把 'bad' 当成 00:00，
    于是一条垃圾数据会伪装成「从零点开始的活动」混进日程。
    """
    if not isinstance(value, str):
        return None
    match = _STRICT_TIME_RE.match(value)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if not (0 <= minute <= 59):
        return None
    if hour == 24:
        return 24 * 60 if minute == 0 else None
    if not (0 <= hour <= 23):
        return None
    return hour * 60 + minute


def normalize_items(raw_items: Any, *, limit: int = 48) -> list[dict[str, Any]]:
    """把模型返回的条目归一成内部结构（不抛异常，坏条目直接丢）。"""
    if not isinstance(raw_items, list):
        return []
    items: list[dict[str, Any]] = []
    for entry in raw_items[:limit]:
        if not isinstance(entry, dict):
            continue
        start = _to_minutes(entry.get("time"))
        end = _to_minutes(entry.get("end"))
        activity = str(entry.get("activity") or "").strip()
        if start is None or end is None or not activity:
            continue
        if end <= start:
            # 跨天（例如 23:30 -> 06:30）；等于的情况视为无效。
            if end == start:
                continue
            end += 24 * 60
        if not (MIN_ITEM_MINUTES <= end - start <= MAX_ITEM_MINUTES):
            continue
        mood = str(entry.get("mood") or "").strip()[:40]
        seed = str(entry.get("message_seed") or "").strip()[:80]
        basis_raw = entry.get("basis")
        basis = [str(x)[:20] for x in basis_raw[:6]] if isinstance(basis_raw, list) else []
        try:
            confidence = float(entry.get("confidence"))
        except (TypeError, ValueError):
            confidence = None
        items.append(
            {
                "start_min": start,
                "end_min": end,
                "activity": activity[:120],
                "mood": mood,
                "message_seed": seed,
                "basis": basis,
                "confidence": confidence,
            }
        )
    items.sort(key=lambda item: item["start_min"])
    return items


def validate_items(items: list[dict[str, Any]], settings: Settings) -> tuple[list[str], float]:
    """返回 (问题标签列表, 质量分 0~100)。"""
    if not items:
        return ["quality"], 0.0

    issues: list[str] = []
    if len(items) < settings.schedule_item_min:
        issues.append("quality")

    overlap = False
    for previous, current in zip(items, items[1:]):
        if current["start_min"] < previous["end_min"] - 1:
            overlap = True
            break
    if overlap:
        issues.append("time")

    coverage = items[-1]["end_min"] - items[0]["start_min"]
    if coverage < MIN_COVERAGE_MINUTES:
        issues.append("time")
    if items[0]["start_min"] > 15 * 60:
        issues.append("time")
    if items[-1]["end_min"] < 20 * 60:
        issues.append("time")

    score = 100.0
    if "quality" in issues:
        score -= 25.0
    if "time" in issues:
        score -= 30.0
    gaps = 0
    for previous, current in zip(items, items[1:]):
        if current["start_min"] - previous["end_min"] > 90:
            gaps += 1
    score -= min(20.0, gaps * 5.0)
    if not any(item["message_seed"] for item in items):
        score -= 10.0
    return issues, max(0.0, score)


def detect_repetition(items: list[dict[str, Any]], recent_activities: list[str]) -> bool:
    """今天与最近几天的活动是否高度雷同。"""
    if not recent_activities or not items:
        return False
    recent_set = {text.strip() for text in recent_activities if text and text.strip()}
    if not recent_set:
        return False
    repeated = sum(1 for item in items if item["activity"] in recent_set)
    return repeated >= 3


def fallback_items(
    settings: Settings,
    sleep_start_min: int | None = None,
    sleep_end_min: int | None = None,
) -> list[dict[str, Any]]:
    """规则兜底模板：作息驱动的一日骨架，保证永远有日程可用。

    作息可传入当天的浮动值（不传则用配置基准）。
    """
    wake = settings.schedule_sleep_end_min if sleep_end_min is None else sleep_end_min
    sleep = settings.schedule_sleep_start_min if sleep_start_min is None else sleep_start_min
    if sleep <= wake:
        sleep += 24 * 60

    def at(offset: int) -> int:
        return (wake + offset) % (24 * 60)

    plan: list[tuple[int, int, str, str, str]] = [
        (wake - 20, wake + 25, "刚醒，赖了会儿床才起来", "有点迷糊", ""),
        (wake + 25, wake + 70, "洗漱完做了简单的早饭", "慢慢清醒过来", ""),
        (wake + 70, wake + 220, "处理上午的日常事务", "还算专注", "上午的事比预想的多一点"),
        (wake + 220, wake + 280, "吃了午饭，休息一会儿", "有点困", "中午随便找了家店吃饭"),
        (wake + 280, wake + 460, "下午的例行安排", "还算平稳", "下午的时间过得有点慢"),
        (wake + 460, wake + 520, "吃晚饭", "放松下来", "晚饭吃得比平时多一点"),
        (wake + 520, sleep - 60, "晚上的自由时间", "比较放松", "晚上翻到个挺有意思的东西"),
        (sleep - 60, sleep, "洗漱、收拾，准备睡觉", "困了", ""),
    ]
    items: list[dict[str, Any]] = []
    for start, end, activity, mood, seed in plan:
        start %= 24 * 60
        end = end if end > 24 * 60 else end % (24 * 60)
        if end <= start:
            end += 24 * 60
        if end - start < MIN_ITEM_MINUTES:
            continue
        items.append(
            {
                "start_min": start,
                "end_min": end,
                "activity": activity,
                "mood": mood,
                "message_seed": seed,
                "basis": ["fallback"],
                "confidence": 0.4,
            }
        )
    items.sort(key=lambda item: item["start_min"])
    return items


def calendar_hint(target: date_cls) -> str:
    name = _FIXED_HOLIDAYS.get((target.month, target.day))
    if name:
        return f"今天是{name}。"
    if target.weekday() >= 5:
        return "今天是周末。"
    return "今天是工作日。"


# --------------------------------------------------------------------------- #
# 每日作息浮动：偶尔熬夜、偶尔赖床
# --------------------------------------------------------------------------- #
# 入睡/起床的偏移档位：(概率, 最小偏移分钟, 最大偏移分钟)；概率按顺序累计。
# 负值 = 早睡 / 早起，正值 = 晚睡 / 赖床。
_ROUTINE_SLEEP_TIERS: tuple[tuple[float, int, int], ...] = (
    (0.55, -20, 20),    # 正常波动
    (0.30, 20, 70),     # 晚睡一点
    (0.10, 70, 140),    # 熬夜
    (0.05, -60, -20),   # 早睡
)
_ROUTINE_WAKE_TIERS: tuple[tuple[float, int, int], ...] = (
    (0.55, -20, 20),    # 正常波动
    (0.30, 15, 50),     # 赖床
    (0.10, 50, 100),    # 睡懒觉
    (0.05, -40, -15),   # 早起
)


def _routine_offset(rng: random.Random, tiers: tuple[tuple[float, int, int], ...]) -> int:
    roll = rng.random()
    acc = 0.0
    for probability, low, high in tiers:
        acc += probability
        if roll < acc:
            return int(rng.uniform(low, high))
    return int(rng.uniform(tiers[-1][1], tiers[-1][2]))


def daily_routine(sleep_start_min: int, sleep_end_min: int, rng: random.Random) -> dict[str, Any]:
    """决定「今天」的实际作息：围绕配置基准浮动，偶尔熬夜和赖床。

    用确定性种子的 rng（调用方建议 `random.Random(f"routine:{persona}:{date}")`），
    同一天结果稳定：多次生成 / 分级重试之间提示词不会漂移，重装后也一致。
    返回 {sleep_start, sleep_end, sleep_shift, wake_shift, note}（分钟数已取模当天）。
    """
    sleep_shift = _routine_offset(rng, _ROUTINE_SLEEP_TIERS)
    wake_shift = _routine_offset(rng, _ROUTINE_WAKE_TIERS)

    notes: list[str] = []
    if sleep_shift >= 70:
        notes.append("昨晚熬夜了")
    elif sleep_shift >= 20:
        notes.append("昨晚睡得比平时晚")
    elif sleep_shift <= -20:
        notes.append("昨晚睡得比较早")
    if wake_shift >= 50:
        notes.append("今天睡了个懒觉")
    elif wake_shift >= 15:
        notes.append("今天有点赖床")
    elif wake_shift <= -15:
        notes.append("今天起得比平时早")

    return {
        "sleep_start": (sleep_start_min + sleep_shift) % (24 * 60),
        "sleep_end": (sleep_end_min + wake_shift) % (24 * 60),
        "sleep_shift": sleep_shift,
        "wake_shift": wake_shift,
        "note": "，".join(notes),
    }


# --------------------------------------------------------------------------- #
# 服务
# --------------------------------------------------------------------------- #
class ScheduleService:
    def __init__(self, context: Any, store: Any, resolver: Any, settings_getter) -> None:
        self.context = context
        self.store = store
        self.resolver = resolver
        self._settings_getter = settings_getter
        self._locks: dict[str, asyncio.Lock] = {}
        # 自动提取的世界观/角色设定（成功 7 天，失败 30 分钟内不重复尝试）
        self._profile_memo: dict[str, tuple[float, str, str]] = {}

    # ---------------------------------------------------------------- #
    def _lock_for(self, persona_id: str) -> asyncio.Lock:
        lock = self._locks.get(persona_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[persona_id] = lock
        return lock

    def today_text(self, target: date_cls | None = None) -> str:
        return (target or date_cls.today()).strftime("%Y-%m-%d")

    async def resolve_active_date(self, persona_id: str) -> str:
        """当前应该生效的日程日期（未到生成时间则沿用昨天的）。"""
        settings = self._settings_getter()
        now = datetime.now()
        today = now.date()
        if now.hour * 60 + now.minute >= settings.schedule_time_min:
            return today.isoformat()
        yesterday = (today - timedelta(days=1)).isoformat()
        if await self.store.get_plan_meta(persona_id, yesterday):
            return yesterday
        return today.isoformat()

    # ---------------------------------------------------------------- #
    async def ensure_plan(
        self, persona_id: str, *, umo: str | None = None, force: bool = False
    ) -> dict[str, Any] | None:
        """懒生成：需要时生成并落库；已有可用日程则直接返回。"""
        settings = self._settings_getter()
        if not settings.enabled:
            return None

        plan_date = await self.resolve_active_date(persona_id)

        async with self._lock_for(persona_id):
            meta = await self.store.get_plan_meta(persona_id, plan_date)
            if meta and not force:
                if not self._needs_regenerate(meta):
                    return await self.store.get_plan(persona_id, plan_date)
            if not settings.schedule_enabled and not force:
                return await self.store.get_plan(persona_id, plan_date)
            try:
                return await self._generate(persona_id, plan_date, umo=umo)
            except Exception as exc:  # noqa: BLE001 - 生成失败也不能让调用方崩
                logger.error("mine_chat: 生成日程失败 persona=%s: %s", persona_id, exc)
                return await self.store.get_plan(persona_id, plan_date)

    def _needs_regenerate(self, meta: dict[str, Any]) -> bool:
        source = str(meta.get("source") or "")
        if source not in {"fallback", "llm_retry"}:
            return False
        retry_after = meta.get("retry_after")
        return bool(retry_after) and time.time() >= float(retry_after)

    async def refresh(
        self, persona_id: str, *, umo: str | None = None, plan_date: str | None = None
    ) -> dict[str, Any] | None:
        """强制重新生成指定日期（默认今天）的日程。"""
        async with self._lock_for(persona_id):
            target = plan_date or self.today_text()
            try:
                return await self._generate(persona_id, target, umo=umo, source="manual")
            except Exception as exc:  # noqa: BLE001
                logger.error("mine_chat: 强制生成日程失败 persona=%s: %s", persona_id, exc)
                return await self.store.get_plan(persona_id, target)

    # ---------------------------------------------------------------- #
    async def _generate(
        self,
        persona_id: str,
        plan_date: str,
        *,
        umo: str | None = None,
        source: str = "llm",
    ) -> dict[str, Any]:
        settings = self._settings_getter()
        items: list[dict[str, Any]] = []
        issues: list[str] = []
        quality = 0.0
        final_source = source
        raw_text = ""
        last_problem = ""

        # 当天实际作息：以日期为种子做确定性浮动（偶尔熬夜 / 赖床），
        # 同一天多次生成与分级重试之间保持一致。
        routine = daily_routine(
            settings.schedule_sleep_start_min,
            settings.schedule_sleep_end_min,
            random.Random(f"routine:{persona_id}:{plan_date}"),
        )

        for attempt in range(0, max(0, settings.schedule_max_retry) + 1):
            prompt = await self._build_prompt(
                persona_id, plan_date, hint=last_problem, routine=routine
            )
            system = (
                settings.prompt_plan_override.strip()
                if settings.prompt_plan_override.strip()
                else prompts.build_plan_system()
            )
            try:
                raw_text = await llm_mod.chat_text(
                    self.context,
                    umo=umo,
                    model_id=settings.schedule_model,
                    system_prompt=system,
                    prompt=prompt,
                    temperature=settings.schedule_temperature,
                )
            except llm_mod.LLMError as exc:
                logger.warning(
                    "mine_chat: 日程生成第 %s 次调用失败 persona=%s: %s",
                    attempt + 1,
                    persona_id,
                    exc,
                )
                last_problem = prompts.plan_retry_hint("format", str(exc))
                final_source = "llm_retry"
                continue

            payload = llm_mod.extract_json(raw_text)
            raw_items = payload.get("schedule") if isinstance(payload, dict) else payload
            items = normalize_items(raw_items)
            issues, quality = validate_items(items, settings)

            if not items:
                last_problem = prompts.plan_retry_hint("format")
                final_source = "llm_retry"
                continue
            if "time" in issues:
                last_problem = prompts.plan_retry_hint("time")
                final_source = "llm_retry"
                continue
            recent = await self._recent_activities(persona_id, plan_date, settings)
            if detect_repetition(items, [row["activity"] for row in recent]):
                last_problem = prompts.plan_retry_hint("repeat")
                final_source = "llm_retry"
                continue
            if "quality" in issues:
                last_problem = prompts.plan_retry_hint("quality")
                final_source = "llm_retry"
                continue
            break

        if not items:
            items = fallback_items(
                settings, routine["sleep_start"], routine["sleep_end"]
            )
            _, quality = validate_items(items, settings)
            final_source = "fallback"
            logger.info("mine_chat: 日程走兜底模板 persona=%s date=%s", persona_id, plan_date)

        retry_after = (
            time.time() + RETRY_BACKOFF_SECONDS if final_source in {"fallback", "llm_retry"} else None
        )
        await self.store.save_plan(
            persona_id=persona_id,
            plan_date=plan_date,
            source=final_source,
            items=items,
            quality=quality,
            note=",".join(issues),
            raw_json=raw_text[:20000],
            retry_after=retry_after,
        )
        bus_mod.notify()  # 日程生成完成 → 控制台立刻刷新
        logger.info(
            "mine_chat: 日程已生成 persona=%s date=%s source=%s items=%s quality=%.0f",
            persona_id,
            plan_date,
            final_source,
            len(items),
            quality,
        )
        plan = await self.store.get_plan(persona_id, plan_date)
        return plan or {"persona_id": persona_id, "plan_date": plan_date, "items": items}

    # ---------------------------------------------------------------- #
    _PROFILE_TTL_SECONDS = 7 * 24 * 3600.0
    _PROFILE_FAIL_TTL_SECONDS = 30 * 60.0

    async def _resolve_world_character(
        self, persona_id: str, settings: Settings, *, force: bool = False
    ) -> tuple[str, str]:
        """世界观/角色设定：配置 > 自动提取缓存 > 现场从人格提示词（+绑定知识库）提炼。

        用户没填配置时，用 LLM 从人格系统提示词提炼一份并缓存（meta 表，
        7 天刷新）。若该人格绑定了知识库，检索相关资料一并作为提炼依据。
        force=True（控制台「重新提取」）跳过全部缓存，生成后覆盖缓存。
        提取失败只在内存里记 30 分钟，之后有机会重试，但不写库（不把失败固化）。
        """
        world = settings.schedule_world.strip()
        character = settings.schedule_character.strip()
        if world or character:
            return world, character

        if not force:
            memo = self._profile_memo.get(persona_id)
            if memo:
                ts, memo_world, memo_character = memo
                ttl = (
                    self._PROFILE_TTL_SECONDS
                    if (memo_world or memo_character)
                    else self._PROFILE_FAIL_TTL_SECONDS
                )
                if time.time() - ts < ttl:
                    return memo_world, memo_character

            key = f"auto_profile:{persona_id}"
            try:
                cached = await self.store.get_meta(key)
            except Exception:  # noqa: BLE001
                cached = None
            if cached:
                try:
                    data = json.loads(cached)
                    ts = float(data.get("ts") or 0.0)
                    if time.time() - ts < self._PROFILE_TTL_SECONDS:
                        memo_world = str(data.get("world") or "")
                        memo_character = str(data.get("character") or "")
                        self._profile_memo[persona_id] = (ts, memo_world, memo_character)
                        return memo_world, memo_character
                except (ValueError, TypeError):
                    pass

        persona_prompt = await self.resolver.persona_prompt(persona_id)
        if not persona_prompt.strip():
            self._profile_memo[persona_id] = (time.time(), "", "")
            return "", ""

        kb_context = ""
        try:
            kb_name = await self.store.get_persona_kb(persona_id)
        except Exception:  # noqa: BLE001
            kb_name = None
        if kb_name:
            kb_context = await kb_mod.retrieve_kb(
                self.context, kb_name, persona_prompt[:300]
            )
            if kb_context:
                logger.info(
                    "mine_chat: 已检索知识库 %s 辅助世界观提取 persona=%s（%d 字）",
                    kb_name,
                    persona_id,
                    len(kb_context),
                )
        try:
            text = await llm_mod.chat_text(
                self.context,
                model_id=settings.schedule_model,
                system_prompt="你是一位角色设定整理师，只输出被要求格式的内容。",
                prompt=prompts.build_profile_extract_prompt(
                    persona_prompt[:3000], kb_context
                ),
                temperature=0.4,
                timeout=90.0,
            )
        except llm_mod.LLMError as exc:
            logger.warning(
                "mine_chat: 自动提取世界观失败（%s 分钟内不重试）persona=%s: %s",
                int(self._PROFILE_FAIL_TTL_SECONDS / 60),
                persona_id,
                exc,
            )
            self._profile_memo[persona_id] = (time.time(), "", "")
            return "", ""
        world, character = prompts.parse_profile_text(text)
        if not world and not character:
            logger.warning(
                "mine_chat: 自动提取世界观为空 persona=%s（人格提示词可能不含设定信息），"
                "建议在控制台「世界观」页手动填写或绑定知识库",
                persona_id,
            )
            self._profile_memo[persona_id] = (time.time(), "", "")
            return "", ""
        now_ts = time.time()
        try:
            await self.store.set_meta(
                f"auto_profile:{persona_id}",
                json.dumps(
                    {"world": world, "character": character, "ts": now_ts},
                    ensure_ascii=False,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("mine_chat: 缓存自动世界观失败: %s", exc)
        self._profile_memo[persona_id] = (now_ts, world, character)
        logger.info(
            "mine_chat: 已从人格提示词自动提取世界观/角色设定 persona=%s"
            "（world %d 字 / character %d 字，7 天后刷新；配置页可覆盖）",
            persona_id,
            len(world),
            len(character),
        )
        return world, character

    async def rebuild_profile(self, persona_id: str) -> tuple[str, str]:
        """控制台「重新提取」：强制重新生成世界观/角色设定并覆盖缓存。"""
        settings = self._settings_getter()
        self._profile_memo.pop(persona_id, None)
        world, character = await self._resolve_world_character(
            persona_id, settings, force=True
        )
        return world, character

    def invalidate_profile(self, persona_id: str) -> None:
        """清掉内存里的提取缓存（换绑知识库后让下次生成走重新提炼）。"""
        self._profile_memo.pop(persona_id, None)

    async def _build_prompt(
        self,
        persona_id: str,
        plan_date: str,
        *,
        hint: str = "",
        routine: dict[str, Any] | None = None,
    ) -> str:
        settings = self._settings_getter()
        persona_prompt = await self.resolver.persona_prompt(persona_id)
        if len(persona_prompt) > 3000:
            persona_prompt = persona_prompt[:3000] + "…"

        try:
            target = datetime.strptime(plan_date, "%Y-%m-%d").date()
        except ValueError:
            target = date_cls.today()

        recent_rows = await self._recent_activities(persona_id, plan_date, settings)
        grouped: dict[str, list[str]] = {}
        for row in recent_rows:
            grouped.setdefault(str(row.get("plan_date") or ""), []).append(
                str(row.get("activity") or "")
            )
        recent_lines: list[str] = []
        for day, activities in grouped.items():
            recent_lines.append(f"{day}：" + "；".join(activities[:10]))

        yesterday_tail = await self._yesterday_tail(persona_id, target)

        night_owl_hint = (
            "角色作息偏晚，可以适当熬夜，但深夜仍应安排休息。"
            if settings.schedule_allow_night_owl
            else "深夜必须处于睡眠状态，除非角色设定明确是夜班。"
        )

        world, character = await self._resolve_world_character(persona_id, settings)
        body = prompts.build_plan_user(
            persona=persona_prompt,
            world=world,
            character=character,
            date_text=target.strftime("%Y年%m月%d日"),
            weekday_text=_WEEKDAY_NAMES[target.weekday()],
            calendar_hint=calendar_hint(target),
            sunset_text=prompts.sunset_for_month(target.month),
            sleep_start_text=fmt_hhmm(
                routine["sleep_start"] if routine else settings.schedule_sleep_start_min
            ),
            sleep_end_text=fmt_hhmm(
                routine["sleep_end"] if routine else settings.schedule_sleep_end_min
            ),
            night_owl_hint=night_owl_hint,
            routine_note=(
                f"今天的实际作息：{routine['note']}。请按这个作息排今天的日程。"
                if routine and routine.get("note")
                else "今天的作息与平时基本一致。"
            ),
            style=settings.schedule_style,
            forbidden=settings.schedule_forbidden,
            recent_lines=recent_lines,
            yesterday_tail=yesterday_tail,
            item_min=settings.schedule_item_min,
            item_max=settings.schedule_item_max,
        )
        if hint:
            body = f"{body}\n\n【上一次重做原因】\n{hint}\n"
        return body

    async def _recent_activities(
        self, persona_id: str, plan_date: str, settings: Settings
    ) -> list[dict[str, Any]]:
        if settings.schedule_avoid_days <= 0:
            return []
        try:
            return await self.store.recent_activities(
                persona_id, plan_date, settings.schedule_avoid_days
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("mine_chat: 读取历史活动失败: %s", exc)
            return []

    async def _yesterday_tail(self, persona_id: str, target: date_cls) -> list[str]:
        yesterday = (target - timedelta(days=1)).isoformat()
        items = await self.store.get_plan_items(persona_id, yesterday)
        if not items:
            return []
        lines: list[str] = []
        for item in items[-3:]:
            lines.append(
                f"{fmt_hhmm(int(item['start_min']))}-{fmt_hhmm(int(item['end_min']))} {item['activity']}"
            )
        return lines

    # ---------------------------------------------------------------- #
    async def describe_plan(self, persona_id: str, plan_date: str | None = None) -> str:
        """把某天日程渲染成可读文本（指令 / 控制台用）。"""
        target = plan_date or await self.resolve_active_date(persona_id)
        plan = await self.store.get_plan(persona_id, target)
        if not plan or not plan.get("items"):
            return f"{target} 还没有日程。"
        lines = [
            f"【{target}】来源 {plan.get('source')}，质量 {plan.get('quality') or 0:.0f} 分"
        ]
        now_minute = datetime.now().hour * 60 + datetime.now().minute
        for item in plan["items"]:
            start = int(item["start_min"])
            end = int(item["end_min"])
            marker = " ← 此刻" if start <= now_minute < end else ""
            seed = f"｜碎片：{item['message_seed']}" if item.get("message_seed") else ""
            mood = f"｜{item['mood']}" if item.get("mood") else ""
            lines.append(
                f"{fmt_hhmm(start)}-{fmt_hhmm(end)} {item['activity']}{mood}{seed}{marker}"
            )
        return "\n".join(lines)

    def plan_to_json(self, plan: dict[str, Any] | None) -> dict[str, Any] | None:
        if not plan:
            return None
        return {
            "persona_id": plan.get("persona_id"),
            "plan_date": plan.get("plan_date"),
            "source": plan.get("source"),
            "quality": plan.get("quality"),
            "note": plan.get("note"),
            "generated_at": plan.get("generated_at"),
            "items": [
                {
                    "id": item.get("id"),
                    "seq": item.get("seq"),
                    "start_min": item.get("start_min"),
                    "end_min": item.get("end_min"),
                    "start_text": fmt_hhmm(int(item.get("start_min") or 0)),
                    "end_text": fmt_hhmm(int(item.get("end_min") or 0)),
                    "activity": item.get("activity"),
                    "mood": item.get("mood"),
                    "message_seed": item.get("message_seed"),
                    "confidence": item.get("confidence"),
                }
                for item in plan.get("items", [])
            ],
        }
