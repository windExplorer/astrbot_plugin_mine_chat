"""主动消息的调度与闸门链。

对应 PRD D2 / FR-4：
- 心跳循环（默认 60s）+ 事件唤醒（用户说话后立即重算）；
- 主动时机由日程片段切换驱动（`next_at` = 下一个片段起点 + 抖动），
  而不是固定间隔随机，这样「角色刚做完一件事」才来找人；
- 有序闸门链，每一步都返回原因码并落库，用户可自查「为什么它不说话」。
"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import date as date_cls, datetime, timedelta
from typing import Any

from astrbot.api import logger

from . import llm as llm_mod
from .config import Settings
from .proactive_gen import (
    ProactiveComposer,
    choose_image_file,
    fetch_anima_image,
    fetch_meme_image,
    image_desc_from_path,
    list_image_files,
)
from .schedule_view import is_sleeping, locate, now_minutes_for

_SKIP_LOG_THROTTLE_SECONDS = 600.0
_MIN_OFFSET_MINUTES = 5


def compute_next_offset_minutes(
    items: list[dict[str, Any]], now_minutes: int, settings: Settings
) -> int:
    """下一个主动候选时间点距离现在多少分钟（基于日程片段起点）。"""
    ordered = sorted(items, key=lambda item: int(item.get("start_min") or 0))
    for item in ordered:
        start = int(item.get("start_min") or 0)
        if start <= now_minutes:
            continue
        if settings.proactive_skip_sleeping and is_sleeping(item):
            continue
        return max(_MIN_OFFSET_MINUTES, start - now_minutes)

    if ordered:
        tail_end = int(ordered[-1].get("end_min") or 0)
        return max(30, tail_end - now_minutes + 30)
    return 60


class ProactiveService:
    def __init__(
        self,
        context: Any,
        store: Any,
        resolver: Any,
        schedule: Any,
        settings_getter,
        default_image_dir: str = "",
        meme_cache_dir: str = "",
    ) -> None:
        self.context = context
        self.store = store
        self.resolver = resolver
        self.schedule = schedule
        self._settings_getter = settings_getter
        self.composer = ProactiveComposer(context, store, resolver, settings_getter)

        self._task: asyncio.Task | None = None
        self._kick_event = asyncio.Event()
        self._terminating = False
        self._locks: dict[str, asyncio.Lock] = {}
        self._skip_log_at: dict[tuple[str, str], float] = {}
        self._default_image_dir = default_image_dir
        self._meme_cache_dir = meme_cache_dir
        self._image_dir_warned = False
        self._last_event: Any = None

    def remember_event(self, event: Any) -> None:
        """记录投递窗口最近一次真实用户消息事件（anima 出图需要 event）。"""
        if event is not None:
            self._last_event = event

    # ---------------------------------------------------------------- #
    # 生命周期
    # ---------------------------------------------------------------- #
    async def start(self) -> None:
        self._terminating = False
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._scheduler_loop())

    async def stop(self) -> None:
        self._terminating = True
        self._kick_event.set()
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - 清理阶段不应再抛出去
            logger.debug("mine_chat: 调度循环回收时出现异常: %s", exc)

    def kick(self) -> None:
        """事件唤醒：让循环立刻醒来重算（用户说话、指令触发时调用）。"""
        self._kick_event.set()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ---------------------------------------------------------------- #
    # 循环
    # ---------------------------------------------------------------- #
    async def _scheduler_loop(self) -> None:
        logger.info("mine_chat: 主动消息调度循环已启动")
        try:
            while not self._terminating:
                try:
                    timeout = await self._next_timeout()
                    try:
                        await asyncio.wait_for(self._kick_event.wait(), timeout=timeout)
                    except asyncio.TimeoutError:
                        pass
                    self._kick_event.clear()
                    if self._terminating:
                        break
                    await self._run_cycle()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 单轮异常不能打死循环
                    logger.error("mine_chat: 调度循环异常: %s", exc)
                    await asyncio.sleep(5)
        finally:
            logger.info("mine_chat: 主动消息调度循环已退出")

    async def _next_timeout(self) -> float:
        settings: Settings = self._settings_getter()
        base = float(settings.proactive_interval_seconds)
        try:
            due = await self.store.get_next_due()
        except Exception as exc:  # noqa: BLE001
            logger.debug("mine_chat: 读取下次到期时间失败: %s", exc)
            return base
        if due is None:
            return base
        return max(5.0, min(base, float(due) - time.time()))

    async def _run_cycle(self) -> None:
        settings: Settings = self._settings_getter()
        if not settings.enabled or not settings.proactive_enabled:
            return
        # 显式配置驱动：未选人格（或人格为空）时调度器什么都不做。
        active_persona = str(getattr(settings, "active_persona", "") or "").strip()
        if not active_persona:
            return
        try:
            personas = await self.store.list_personas()
        except Exception as exc:  # noqa: BLE001
            logger.error("mine_chat: 读取人格列表失败: %s", exc)
            return

        now = time.time()
        for persona in personas:
            if self._terminating:
                return
            persona_id = str(persona.get("persona_id") or "")
            if not persona_id or not int(persona.get("enabled") or 0):
                continue
            if persona_id != active_persona:
                continue
            lock = self._locks.get(persona_id)
            if lock is not None and lock.locked():
                continue
            try:
                state = await self.store.get_proactive_state(persona_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("mine_chat: 读取主动状态失败 persona=%s: %s", persona_id, exc)
                continue
            if not self._runtime_enabled(state):
                continue
            next_at = state.get("next_at")
            if next_at is None:
                await self._reschedule(persona_id)
                continue
            if now < float(next_at):
                continue
            try:
                await self.check_and_send(persona_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("mine_chat: 主动消息处理异常 persona=%s: %s", persona_id, exc)

    # ---------------------------------------------------------------- #
    # 对外入口
    # ---------------------------------------------------------------- #
    def _lock_for(self, persona_id: str) -> asyncio.Lock:
        lock = self._locks.get(persona_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[persona_id] = lock
        return lock

    @staticmethod
    def _runtime_enabled(state: dict[str, Any]) -> bool:
        value = state.get("enabled")
        return True if value is None else int(value) == 1

    async def check_and_send(
        self, persona_id: str, *, manual: bool = False
    ) -> tuple[bool, str]:
        settings: Settings = self._settings_getter()
        state = await self.store.get_proactive_state(persona_id)
        if not self._runtime_enabled(state):
            return False, "disabled"

        ok, reason, info = await self._check_gates(persona_id, state, settings, manual=manual)
        if not ok:
            await self._after_reject(persona_id, state, reason, info, settings)
            return False, reason

        lock = self._lock_for(persona_id)
        async with lock:
            fresh = await self.store.get_proactive_state(persona_id)
            ok2, reason2, info2 = await self._check_gates(
                persona_id, fresh, settings, manual=True
            )
            if not ok2:
                await self._after_reject(persona_id, fresh, reason2, info2, settings)
                return False, reason2
            return await self._do_send(persona_id, fresh, info2, settings)

    async def trigger_now(self, persona_id: str) -> tuple[bool, str]:
        """手动立即触发（仍受开关/静默/睡眠等闸门约束）。"""
        return await self.check_and_send(persona_id, manual=True)

    async def note_user_activity(self, persona_id: str) -> None:
        """用户说话后：解冻未回复计数并重排下一次主动。"""
        try:
            await self.store.upsert_proactive_state(
                persona_id, last_user_at=time.time(), unanswered=0
            )
            await self._reschedule(persona_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("mine_chat: 记录用户活跃失败 persona=%s: %s", persona_id, exc)

    async def set_enabled(self, persona_id: str, enabled: bool) -> None:
        await self.store.upsert_proactive_state(
            persona_id, enabled=1 if enabled else 0
        )
        if enabled:
            await self._reschedule(persona_id)
        self.kick()

    async def is_enabled(self, persona_id: str) -> bool:
        state = await self.store.get_proactive_state(persona_id)
        return self._runtime_enabled(state)

    # ---------------------------------------------------------------- #
    # 闸门链
    # ---------------------------------------------------------------- #
    async def _check_gates(
        self,
        persona_id: str,
        state: dict[str, Any],
        settings: Settings,
        *,
        manual: bool = False,
    ) -> tuple[bool, str, dict[str, Any]]:
        info: dict[str, Any] = {}

        if not settings.enabled or not settings.proactive_enabled:
            return False, "disabled", info

        now_dt = datetime.now()
        now = time.time()
        now_minute = now_dt.hour * 60 + now_dt.minute

        if settings.quiet_now(now_minute):
            return False, "quiet_hours", info

        umo = str(state.get("primary_umo") or "").strip()
        if not umo:
            umo = await self.resolver.primary_umo_for(persona_id) or ""
        info["umo"] = umo
        if not umo:
            return False, "no_umo", info

        plan_date = await self.schedule.resolve_active_date(persona_id)
        plan = await self.store.get_plan(persona_id, plan_date)
        items = list(plan.get("items", [])) if plan else []
        now_minutes = now_minutes_for(plan_date, now_dt)
        slots = locate(items, now_minutes)
        current = slots.get("current")
        previous = slots.get("previous")
        info.update(
            {
                "plan_date": plan_date,
                "items": items,
                "now_minutes": now_minutes,
                "current": current,
                "previous": previous,
            }
        )

        if settings.proactive_skip_sleeping and is_sleeping(current):
            return False, "sleeping", info
        if not manual and current is None and previous is None:
            return False, "no_seed", info

        today = now_dt.date().isoformat()
        sent_today = (
            int(state.get("sent_today") or 0) if state.get("sent_date") == today else 0
        )
        info["sent_today"] = sent_today
        if settings.proactive_daily_limit > 0 and sent_today >= settings.proactive_daily_limit:
            return False, "daily_limit", info

        last_sent = state.get("last_sent_at")
        if last_sent:
            elapsed_minutes = (now - float(last_sent)) / 60.0
            if elapsed_minutes < settings.proactive_min_interval_minutes:
                info["retry_in_minutes"] = (
                    settings.proactive_min_interval_minutes - elapsed_minutes
                )
                return False, "min_interval", info

        unanswered = int(state.get("unanswered") or 0)
        info["unanswered"] = unanswered
        if (
            settings.proactive_max_unanswered > 0
            and unanswered >= settings.proactive_max_unanswered
        ):
            return False, "unanswered_limit", info

        last_user = state.get("last_user_at")
        if last_user:
            elapsed_minutes = (now - float(last_user)) / 60.0
            if elapsed_minutes < settings.proactive_user_active_cooldown_minutes:
                info["retry_in_minutes"] = (
                    settings.proactive_user_active_cooldown_minutes - elapsed_minutes
                )
                return False, "user_active", info

        if not manual:
            next_at = state.get("next_at")
            if next_at is not None and now < float(next_at):
                return False, "not_due", info

        info["seed"] = self._pick_seed(current, previous, now_minutes, settings)
        return True, "ok", info

    def _pick_seed(
        self,
        current: dict[str, Any] | None,
        previous: dict[str, Any] | None,
        now_minutes: int,
        settings: Settings,
    ) -> str:
        window = settings.proactive_seed_window_minutes
        if current:
            seed = str(current.get("message_seed") or "").strip()
            age = now_minutes - int(current.get("start_min") or 0)
            if seed and 0 <= age <= window:
                return seed
        if previous:
            seed = str(previous.get("message_seed") or "").strip()
            age = now_minutes - int(previous.get("end_min") or 0)
            if seed and 0 <= age <= window:
                return seed
        return ""

    # ---------------------------------------------------------------- #
    # 发送
    # ---------------------------------------------------------------- #
    async def _do_send(
        self,
        persona_id: str,
        state: dict[str, Any],
        info: dict[str, Any],
        settings: Settings,
    ) -> tuple[bool, str]:
        umo = str(info.get("umo") or "")
        if not umo:
            await self._record(persona_id, "skip", "no_umo", "", throttle=True)
            await self._reschedule(persona_id)
            return False, "no_umo"

        before_user_at = state.get("last_user_at")
        image_plan = await self._plan_image(settings, state, info)

        try:
            texts = await self.composer.generate(
                persona_id,
                umo=umo,
                current_item=info.get("current"),
                previous_item=info.get("previous"),
                seed=str(info.get("seed") or ""),
                state=state,
                image_plan=image_plan,
            )
        except llm_mod.LLMError as exc:
            logger.warning("mine_chat: 主动消息生成失败 persona=%s: %s", persona_id, exc)
            await self._record(persona_id, "error", "llm_error", umo)
            await self._reschedule(persona_id)
            return False, "llm_error"
        except Exception as exc:  # noqa: BLE001
            logger.error("mine_chat: 主动消息生成异常 persona=%s: %s", persona_id, exc)
            await self._record(persona_id, "error", "llm_error", umo)
            await self._reschedule(persona_id)
            return False, "llm_error"

        if not texts:
            await self._record(persona_id, "error", "empty", umo)
            await self._reschedule(persona_id)
            return False, "empty"

        # 生成期间用户说话了 → 本次作废（避免打断进行中的对话）。
        fresh = await self.store.get_proactive_state(persona_id)
        if (fresh.get("last_user_at") or 0) != (before_user_at or 0):
            await self._record(persona_id, "skip", "interrupted", umo, throttle=False)
            await self._reschedule(persona_id)
            return False, "interrupted"

        image_path = str(image_plan.get("path") or "") if image_plan else ""
        delivered = await self.composer.deliver(
            umo,
            texts,
            delay=settings.proactive_segment_delay,
            image_path=image_path or None,
        )
        if not delivered:
            await self._record(persona_id, "error", "send_failed", umo)
            await self._reschedule(persona_id)
            return False, "send_failed"

        await self.composer.archive(umo, texts)

        now_dt = datetime.now()
        today = now_dt.date().isoformat()
        sent_today = (
            int(fresh.get("sent_today") or 0) if fresh.get("sent_date") == today else 0
        )
        content = "\n".join(texts)
        extra_updates: dict[str, Any] = {}
        if image_plan and image_path:
            extra_updates["images_today"] = (
                int(fresh.get("images_today") or 0) + 1
                if fresh.get("images_date") == today
                else 1
            )
            extra_updates["images_date"] = today
        await self.store.upsert_proactive_state(
            persona_id,
            primary_umo=umo,
            sent_today=sent_today + 1,
            sent_date=today,
            last_sent_at=time.time(),
            last_message=content,
            unanswered=int(fresh.get("unanswered") or 0) + 1,
            **extra_updates,
        )
        await self._record(persona_id, "send", "ok", umo, content)
        logger.info(
            "mine_chat: 已主动发送 persona=%s umo=%s segments=%s",
            persona_id,
            umo,
            len(texts),
        )
        await self._reschedule(persona_id)
        return True, "ok"

    def _warn_image(self, message_text: str) -> None:
        """配图相关告警只提示一次，避免每次掷骰都刷屏。"""
        if self._image_dir_warned:
            logger.debug("mine_chat: %s", message_text)
            return
        self._image_dir_warned = True
        logger.warning("mine_chat: %s", message_text)

    async def _plan_image(
        self,
        settings: Settings,
        state: dict[str, Any],
        info: dict[str, Any],
    ) -> dict[str, Any] | None:
        """决定这次主动消息要不要带图、用哪个后端拿图（生成前调用）。

        掷骰与每日上限对所有后端一致；anima 出图耗时较长（最长 180s），
        拉图期间用户插嘴由 _do_send 现有的 last_user_at 检测兜住。
        """
        if not settings.proactive_image_enabled:
            return None
        if settings.proactive_image_probability <= 0:
            return None
        if random.random() > settings.proactive_image_probability:
            return None

        now_dt = datetime.now()
        today = now_dt.date().isoformat()
        used = (
            int(state.get("images_today") or 0) if state.get("images_date") == today else 0
        )
        limit = settings.proactive_image_max_per_day
        if limit > 0 and used >= limit:
            return None

        backend = (settings.proactive_image_backend or "local").strip().lower()

        if backend == "anima":
            event = self._last_event
            if event is None:
                self._warn_image(
                    "配图方式为 ComfyUI萌绘，但还没有任何用户消息事件可复用——"
                    "先和角色聊一句再开启配图"
                )
                return None
            current = info.get("current") or info.get("previous") or {}
            activity = str(current.get("activity") or "").strip()
            seed = str(info.get("seed") or "").strip()
            prompt = activity or "日常生活的一个随意瞬间"
            if seed and seed not in prompt:
                prompt = f"{prompt}，{seed}"
            path = await fetch_anima_image(
                self.context,
                event,
                prompt=prompt,
                workflow=settings.proactive_image_workflow,
            )
            if not path:
                return None
            return {"kind": "anima", "path": path, "desc": ""}

        if backend == "meme":
            path = await fetch_meme_image(
                settings.proactive_meme_token, self._meme_cache_dir
            )
            if not path:
                return None
            return {"kind": "meme", "path": path, "desc": ""}

        # 本地图库（默认后端）
        directory = str(settings.proactive_image_dir or "").strip() or self._default_image_dir
        files = list_image_files(directory)
        if not files:
            self._warn_image(
                f"配图方式为本地图库，但图库目录为空或不可读（{directory or '未配置'}）——"
                "把图片放进去即可，无需重启"
            )
            return None
        path = choose_image_file(files)
        if not path:
            return None
        return {"kind": "local", "path": path, "desc": image_desc_from_path(path)}

    # ---------------------------------------------------------------- #
    # 排期
    # ---------------------------------------------------------------- #
    async def _reschedule(
        self, persona_id: str, *, offset_minutes: int | None = None
    ) -> None:
        settings: Settings = self._settings_getter()
        try:
            plan_date = await self.schedule.resolve_active_date(persona_id)
            plan = await self.store.get_plan(persona_id, plan_date)
            items = list(plan.get("items", [])) if plan else []
            now_minutes = now_minutes_for(plan_date)
            if offset_minutes is None:
                base = compute_next_offset_minutes(items, now_minutes, settings)
                offset = base + random.randint(0, max(0, settings.proactive_jitter_minutes))
            else:
                offset = offset_minutes
            next_at = time.time() + max(1, offset) * 60
            next_at = self._clamp_out_of_quiet(next_at, settings)
            await self.store.upsert_proactive_state(persona_id, next_at=next_at)
        except Exception as exc:  # noqa: BLE001
            logger.debug("mine_chat: 重排主动时间失败 persona=%s: %s", persona_id, exc)

    @staticmethod
    def _clamp_out_of_quiet(next_ts: float, settings: Settings) -> float:
        """把候选时间推出免打扰时段。"""
        moment = datetime.fromtimestamp(next_ts)
        minute = moment.hour * 60 + moment.minute
        if not settings.quiet_now(minute):
            return next_ts
        delta = (settings.proactive_quiet_end_min - minute) % (24 * 60)
        if delta <= 0:
            delta = 24 * 60
        return next_ts + delta * 60

    async def _after_reject(
        self,
        persona_id: str,
        state: dict[str, Any],
        reason: str,
        info: dict[str, Any],
        settings: Settings,
    ) -> None:
        """被拒绝后调整排期，避免每 60 秒重复走到同一个闸门。"""
        if reason in {"not_due", "dispatched"}:
            return

        if reason == "quiet_hours":
            await self._reschedule(persona_id, offset_minutes=None)
            return
        if reason in {"daily_limit", "unanswered_limit"}:
            tomorrow = datetime.now() + timedelta(days=1)
            wake = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
                minutes=settings.proactive_quiet_end_min
            )
            await self.store.upsert_proactive_state(persona_id, next_at=wake.timestamp())
            await self._record(persona_id, "skip", reason, info.get("umo", ""), throttle=True)
            return
        if reason in {"min_interval", "user_active"}:
            retry_in = float(info.get("retry_in_minutes") or 5.0)
            await self._reschedule(persona_id, offset_minutes=max(1, int(retry_in) + 1))
            return
        if reason == "sleeping":
            await self._reschedule(persona_id)
            return

        await self._reschedule(persona_id)
        await self._record(persona_id, "skip", reason, info.get("umo", ""), throttle=True)

    # ---------------------------------------------------------------- #
    # 日志
    # ---------------------------------------------------------------- #
    async def _record(
        self,
        persona_id: str,
        decision: str,
        reason: str,
        umo: str = "",
        content: str = "",
        *,
        throttle: bool = False,
    ) -> None:
        if throttle:
            key = (persona_id, reason)
            now = time.time()
            if now - self._skip_log_at.get(key, 0.0) < _SKIP_LOG_THROTTLE_SECONDS:
                return
            self._skip_log_at[key] = now
        try:
            await self.store.add_log(persona_id, decision, reason, umo, content)
        except Exception as exc:  # noqa: BLE001
            logger.debug("mine_chat: 写入裁决日志失败: %s", exc)

    # ---------------------------------------------------------------- #
    # 状态视图（控制台 / 指令）
    # ---------------------------------------------------------------- #
    async def status(self, persona_id: str) -> dict[str, Any]:
        settings: Settings = self._settings_getter()
        state = await self.store.get_proactive_state(persona_id)
        now_dt = datetime.now()
        today = now_dt.date().isoformat()
        sent_today = (
            int(state.get("sent_today") or 0) if state.get("sent_date") == today else 0
        )
        images_today = (
            int(state.get("images_today") or 0) if state.get("images_date") == today else 0
        )
        next_at = state.get("next_at")
        next_text = "-"
        if next_at:
            next_text = datetime.fromtimestamp(float(next_at)).strftime("%m-%d %H:%M")
        return {
            "persona_id": persona_id,
            "enabled": self._runtime_enabled(state),
            "primary_umo": state.get("primary_umo") or "",
            "next_at": next_at,
            "next_at_text": next_text,
            "sent_today": sent_today,
            "daily_limit": settings.proactive_daily_limit,
            "images_today": images_today,
            "image_max_per_day": settings.proactive_image_max_per_day,
            "image_enabled": settings.proactive_image_enabled,
            "unanswered": int(state.get("unanswered") or 0),
            "max_unanswered": settings.proactive_max_unanswered,
            "last_sent_at": state.get("last_sent_at"),
            "last_message": state.get("last_message") or "",
            "last_user_at": state.get("last_user_at"),
            "quiet_hours": f"{settings.proactive_quiet_start_min // 60:02d}:"
            f"{settings.proactive_quiet_start_min % 60:02d}-"
            f"{settings.proactive_quiet_end_min // 60:02d}:"
            f"{settings.proactive_quiet_end_min % 60:02d}",
        }

    @staticmethod
    def today_text() -> str:
        return date_cls.today().isoformat()
