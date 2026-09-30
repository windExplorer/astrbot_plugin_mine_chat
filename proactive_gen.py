"""主动消息的生成、投递与写回。

对应 PRD D5：主动消息不是用户输入，若只发不写回，下一轮 LLM 会完全不知道
自己说过什么。因此投递成功后必须：
  1) 写入 AstrBot 对话历史（add_message_pair）；
  2) 在下一轮注入块里带上「你刚才主动发了 X」（由 schedule_view 负责）。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

from astrbot.api import logger

from . import llm as llm_mod
from . import prompts
from .config import Settings, fmt_hhmm

_WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_SEGMENT_SEPARATOR = "---"
MAX_SEGMENT_CHARS = 200
USER_PLACEHOLDER = "(主动发起)"


def split_segments(text: str, max_segments: int) -> list[str]:
    """把模型输出拆成若干条短消息（按独占一行的 --- 分隔）。"""
    raw = (text or "").strip()
    if not raw:
        return []
    parts: list[str] = []
    current: list[str] = []
    for line in raw.splitlines():
        if line.strip() == _SEGMENT_SEPARATOR:
            chunk = "\n".join(current).strip()
            if chunk:
                parts.append(chunk)
            current = []
            continue
        current.append(line)
    chunk = "\n".join(current).strip()
    if chunk:
        parts.append(chunk)

    if not parts:
        parts = [raw]

    limited = parts[: max(1, max_segments)]
    result: list[str] = []
    for part in limited:
        collapsed = " ".join(part.split())
        if not collapsed:
            continue
        if len(collapsed) > MAX_SEGMENT_CHARS:
            collapsed = collapsed[:MAX_SEGMENT_CHARS].rstrip() + "…"
        result.append(collapsed)
    return result


class ProactiveComposer:
    """负责「写什么」与「怎么发出去」。"""

    def __init__(self, context: Any, store: Any, resolver: Any, settings_getter) -> None:
        self.context = context
        self.store = store
        self.resolver = resolver
        self._settings_getter = settings_getter

    # ---------------------------------------------------------------- #
    # 上下文
    # ---------------------------------------------------------------- #
    async def recent_chat(self, umo: str, limit: int) -> str:
        if limit <= 0:
            return ""
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            return ""
        try:
            cid = await manager.get_curr_conversation_id(umo)
            if not cid:
                return ""
            conversation = await manager.get_conversation(umo, cid)
        except Exception as exc:  # noqa: BLE001
            logger.debug("mine_chat: 读取最近对话失败 umo=%s: %s", umo, exc)
            return ""

        history = getattr(conversation, "history", None)
        if isinstance(history, str):
            try:
                history = json.loads(history)
            except (json.JSONDecodeError, ValueError):
                history = []
        if not isinstance(history, list):
            return ""

        lines: list[str] = []
        for record in history[-max(1, limit) :]:
            if not isinstance(record, dict):
                continue
            role = record.get("role")
            if role not in {"user", "assistant"}:
                continue
            content = record.get("content")
            if isinstance(content, list):
                content = "".join(
                    str(part.get("text") or "")
                    for part in content
                    if isinstance(part, dict)
                )
            text = " ".join(str(content or "").split())
            if not text:
                continue
            speaker = "你" if role == "assistant" else "用户"
            lines.append(f"{speaker}：{text[:200]}")
        return "\n".join(lines)

    def block_for_prompt(self, item: dict[str, Any] | None) -> str:
        if not item:
            return ""
        start = fmt_hhmm(int(item.get("start_min") or 0))
        end = fmt_hhmm(int(item.get("end_min") or 0))
        activity = str(item.get("activity") or "").strip()
        mood = str(item.get("mood") or "").strip()
        return f"{start}-{end} {activity}" + (f"（心情：{mood}）" if mood else "")

    def now_text(self) -> str:
        now = datetime.now()
        return f"{now.strftime('%Y-%m-%d')} {_WEEKDAY_NAMES[now.weekday()]} {now.strftime('%H:%M')}"

    # ---------------------------------------------------------------- #
    # 生成
    # ---------------------------------------------------------------- #
    async def generate(
        self,
        persona_id: str,
        *,
        umo: str,
        current_item: dict[str, Any] | None,
        previous_item: dict[str, Any] | None,
        seed: str,
        state: dict[str, Any],
    ) -> list[str]:
        settings: Settings = self._settings_getter()
        character_name = self.resolver.persona_display_name(persona_id)
        persona_prompt = await self.resolver.persona_prompt(persona_id)
        if len(persona_prompt) > 2000:
            persona_prompt = persona_prompt[:2000] + "…"

        recent_chat = await self.recent_chat(umo, settings.proactive_history_messages)
        unanswered = int(state.get("unanswered") or 0)
        last_proactive = str(state.get("last_message") or "")

        if settings.prompt_proactive_override.strip():
            system = prompts.fill(
                settings.prompt_proactive_override,
                {
                    "persona": persona_prompt or character_name,
                    "current_block": self.block_for_prompt(current_item),
                    "seed": seed,
                    "recent_chat": recent_chat,
                    "last_proactive": last_proactive,
                    "unanswered": str(unanswered),
                    "now": self.now_text(),
                    "extra": settings.proactive_extra_instruction,
                },
            )
        else:
            system = prompts.build_proactive_system(
                character_name=character_name,
                max_segments=settings.proactive_max_segments,
                unanswered=unanswered,
                extra=settings.proactive_extra_instruction,
            )
            if persona_prompt:
                system = f"{system}\n\n【你的人物设定】\n{persona_prompt}"

        prompt = prompts.build_proactive_user(
            now_text=self.now_text(),
            current_block=self.block_for_prompt(current_item or previous_item),
            seed=seed,
            recent_chat=recent_chat,
            last_proactive=last_proactive,
        )

        text = await llm_mod.chat_text(
            self.context,
            umo=umo,
            model_id=settings.proactive_model,
            system_prompt=system,
            prompt=prompt,
            temperature=0.9,
        )
        return split_segments(text, settings.proactive_max_segments)

    # ---------------------------------------------------------------- #
    # 投递
    # ---------------------------------------------------------------- #
    async def deliver(self, umo: str, texts: list[str], *, delay: float = 1.5) -> bool:
        """发送消息；任一条未送达即视为整体失败（调用方不应写回历史）。"""
        if not texts:
            return False
        try:
            from astrbot.api.message_components import Plain
            from astrbot.core.message.message_event_result import MessageChain
        except Exception as exc:  # noqa: BLE001 - 导入失败说明环境异常
            logger.error("mine_chat: 消息组件导入失败: %s", exc)
            return False

        for index, text in enumerate(texts):
            if index > 0 and delay > 0:
                await asyncio.sleep(delay)
            try:
                result = await self.context.send_message(
                    umo, MessageChain([Plain(text)])
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("mine_chat: 主动消息发送异常 umo=%s: %s", umo, exc)
                return False
            if result is False:
                logger.warning(
                    "mine_chat: 主动消息未送达（未找到匹配平台）umo=%s", umo
                )
                return False
        return True

    # ---------------------------------------------------------------- #
    # 写回历史
    # ---------------------------------------------------------------- #
    async def archive(self, umo: str, texts: list[str]) -> bool:
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            return False
        content = "\n".join(texts).strip()
        if not content:
            return False
        try:
            cid = await manager.get_curr_conversation_id(umo)
            if not cid:
                cid = await manager.new_conversation(umo, title="萌萌日程 主动消息")
            if not cid:
                return False
            await manager.add_message_pair(
                cid=cid,
                user_message={"role": "user", "content": USER_PLACEHOLDER},
                assistant_message={"role": "assistant", "content": content},
            )
            return True
        except Exception as exc:  # noqa: BLE001 - 写回失败不影响已发送的事实
            logger.warning("mine_chat: 主动消息写回历史失败 umo=%s: %s", umo, exc)
            return False
