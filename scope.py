"""作用域解析：umo ↔ persona 映射、人格内容获取、窗口绑定登记。

核心设计（对应 PRD D1）：
- **状态按 persona 归档**：日程、节流、未回复计数全挂 persona，因此同一人格的
  私聊窗口与群聊窗口天然共享同一份生活，不会自相矛盾。
- **投递按 umo**：主动消息只发往该 persona 的主窗口（v1 固定私聊）。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger

CACHE_TTL_SECONDS = 300.0
_INVALID_PERSONA_IDS = {"", "[%None]", "None", "none", "null"}


# --------------------------------------------------------------------------- #
# umo 工具
# --------------------------------------------------------------------------- #
def parse_umo(umo: str) -> tuple[str, str, str]:
    """拆分 umo 为 (platform_id, kind, target_id)。

    kind 取值 private / group / other。平台名或目标 id 里出现 'group' 字样
    不影响判定——只看类型段（这是踩过的坑）。
    """
    raw = str(umo or "").strip()
    parts = raw.split(":", 2)
    if len(parts) != 3:
        return "", "other", raw
    platform_id, message_type, target_id = parts
    lowered = message_type.strip().lower()
    if lowered == "groupmessage":
        kind = "group"
    elif lowered == "friendmessage":
        kind = "private"
    else:
        kind = "other"
    return platform_id.strip(), kind, target_id.strip()


def umo_kind(umo: str) -> str:
    return parse_umo(umo)[1]


def is_private_umo(umo: str) -> bool:
    return umo_kind(umo) == "private"


def build_umo(platform_id: str, kind: str, target_id: str) -> str:
    message_type = "GroupMessage" if kind == "group" else "FriendMessage"
    return f"{platform_id}:{message_type}:{target_id}"


def _clean_persona_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text in _INVALID_PERSONA_IDS:
        return None
    return text


# --------------------------------------------------------------------------- #
# 解析器
# --------------------------------------------------------------------------- #
class ScopeResolver:
    """负责人格解析、人格内容读取与窗口绑定登记。"""

    def __init__(self, context: Any, store: Any, settings_getter) -> None:
        self.context = context
        self.store = store
        self._settings_getter = settings_getter
        self._cache: dict[str, tuple[str, float]] = {}

    # ---------------------------------------------------------------- #
    # 人格解析
    # ---------------------------------------------------------------- #
    async def resolve_persona_id(self, umo: str, *, force: bool = False) -> str:
        """解析某个窗口当前生效的人格 id（4 级优先级，命中即止）。"""
        settings = self._settings_getter()

        override = _clean_persona_id(getattr(settings, "active_persona", ""))
        if override:
            return override

        now = time.time()
        if not force:
            cached = self._cache.get(umo)
            if cached and now - cached[1] < CACHE_TTL_SECONDS:
                return cached[0]

        persona_id = await self._resolve_from_conversation(umo)
        if not persona_id:
            persona_id = await self._resolve_from_astrbot(umo)
        if not persona_id:
            persona_id = "default"

        self._cache[umo] = (persona_id, now)
        return persona_id

    def invalidate(self, umo: str | None = None) -> None:
        if umo is None:
            self._cache.clear()
        else:
            self._cache.pop(umo, None)

    async def _resolve_from_conversation(self, umo: str) -> str | None:
        """读取该窗口当前对话绑定的人格（最贴近「窗口绑定的人格」）。"""
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            return None
        try:
            cid = await manager.get_curr_conversation_id(umo)
            if not cid:
                return None
            conversation = await manager.get_conversation(umo, cid)
        except Exception as exc:  # noqa: BLE001 - 解析失败就往下走
            logger.debug("mine_chat: 读取会话人格失败 umo=%s: %s", umo, exc)
            return None
        return _clean_persona_id(getattr(conversation, "persona_id", None))

    async def _resolve_from_astrbot(self, umo: str) -> str | None:
        """走 AstrBot 官方优先级（会话规则 > 会话记录 > 提供商默认人格）。"""
        manager = getattr(self.context, "persona_manager", None)
        resolver = getattr(manager, "resolve_selected_persona", None)
        if not callable(resolver):
            return None
        platform_id = parse_umo(umo)[0]
        try:
            conversation_persona_id = await self._resolve_from_conversation(umo)
            result = await resolver(
                umo=umo,
                conversation_persona_id=conversation_persona_id,
                platform_name=platform_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("mine_chat: resolve_selected_persona 失败 umo=%s: %s", umo, exc)
            return None
        if isinstance(result, (tuple, list)) and result:
            return _clean_persona_id(result[0])
        return None

    # ---------------------------------------------------------------- #
    # 人格内容
    # ---------------------------------------------------------------- #
    def persona_display_name(self, persona_id: str) -> str:
        """人格名即 persona_id（AstrBot 的 personas_v3 以 name 索引）。"""
        manager = getattr(self.context, "persona_manager", None)
        getter = getattr(manager, "get_persona_v3_by_id", None)
        if callable(getter):
            try:
                persona = getter(persona_id)
            except Exception:  # noqa: BLE001
                persona = None
            if isinstance(persona, dict):
                name = persona.get("name")
                if isinstance(name, str) and name.strip():
                    return name.strip()
        return persona_id

    async def persona_prompt(self, persona_id: str) -> str:
        """取人格的系统提示词正文（优先内存 personas_v3，其次数据库）。"""
        manager = getattr(self.context, "persona_manager", None)
        if manager is None:
            return ""

        getter = getattr(manager, "get_persona_v3_by_id", None)
        if callable(getter):
            try:
                persona = getter(persona_id)
            except Exception:  # noqa: BLE001
                persona = None
            if isinstance(persona, dict):
                prompt = persona.get("prompt")
                if isinstance(prompt, str) and prompt.strip():
                    return prompt.strip()

        async_getter = getattr(manager, "get_persona", None)
        if callable(async_getter):
            try:
                persona = await async_getter(persona_id)
            except Exception:  # noqa: BLE001 - 人格不存在会抛 ValueError
                return ""
            prompt = getattr(persona, "system_prompt", None)
            if isinstance(prompt, str):
                return prompt.strip()
        return ""

    async def list_available_personas(self) -> list[dict[str, Any]]:
        """列出 AstrBot 里可用的人格（供控制台下拉框）。"""
        manager = getattr(self.context, "persona_manager", None)
        if manager is None:
            return []
        getter = getattr(manager, "get_all_personas", None)
        result: list[dict[str, Any]] = []
        if callable(getter):
            try:
                personas = await getter()
            except Exception as exc:  # noqa: BLE001
                logger.debug("mine_chat: get_all_personas 失败: %s", exc)
                personas = []
            for persona in personas or []:
                persona_id = getattr(persona, "persona_id", None)
                if not isinstance(persona_id, str) or not persona_id.strip():
                    continue
                result.append(
                    {
                        "persona_id": persona_id.strip(),
                        "system_prompt": (getattr(persona, "system_prompt", "") or "")[:400],
                    }
                )
        if not result:
            for persona in getattr(manager, "personas_v3", []) or []:
                if isinstance(persona, dict):
                    persona_id = _clean_persona_id(persona.get("name"))
                    if persona_id:
                        result.append(
                            {
                                "persona_id": persona_id,
                                "system_prompt": (persona.get("prompt") or "")[:400],
                            }
                        )
        return result

    # ---------------------------------------------------------------- #
    # 窗口绑定
    # ---------------------------------------------------------------- #
    async def ensure_binding(
        self, umo: str, persona_id: str, *, auto_register: bool = True, touch: bool = True
    ) -> dict[str, Any] | None:
        """登记/更新窗口绑定，并保证该 persona 有一个主窗口。"""
        if not umo or not persona_id:
            return None
        kind = umo_kind(umo)
        if kind == "other":
            return None

        settings = self._settings_getter()
        configured_primary = str(getattr(settings, "primary_umo", "") or "").strip()

        existing = await self.store.get_binding(umo)
        if existing is None:
            if not auto_register:
                return None
            is_primary = False
            if configured_primary:
                is_primary = umo == configured_primary
            elif kind == "private":
                is_primary = await self.store.get_primary(persona_id) is None
            await self.store.upsert_binding(
                umo=umo,
                persona_id=persona_id,
                kind=kind,
                is_primary=is_primary,
                enabled=True,
            )
        else:
            existing_persona = str(existing.get("persona_id") or "")
            if existing_persona != persona_id and auto_register:
                # 窗口换了人格：跟随更新，但主窗口标记不自动抢（交给配置/控制台）。
                await self.store.upsert_binding(
                    umo=umo,
                    persona_id=persona_id,
                    kind=kind,
                    is_primary=False,
                    enabled=True,
                )
            elif touch:
                await self.store.upsert_binding(
                    umo=umo,
                    persona_id=existing_persona or persona_id,
                    kind=kind,
                    is_primary=False,
                    enabled=True,
                    touch_only=True,
                )

        # 配置显式指定投递窗口时，让它成为主窗口。
        if configured_primary and umo == configured_primary:
            await self.store.set_primary(persona_id, umo)

        # 该人格此前没有任何主窗口时，把首个私聊窗口提升为主窗口。
        if kind == "private" and not configured_primary:
            if await self.store.get_primary(persona_id) is None:
                await self.store.set_primary(persona_id, umo)

        await self.store.upsert_persona(persona_id, self.persona_display_name(persona_id))
        return await self.store.get_binding(umo)

    async def primary_umo_for(self, persona_id: str) -> str | None:
        """确定该人格的主动消息投递窗口。"""
        settings = self._settings_getter()
        configured = str(getattr(settings, "primary_umo", "") or "").strip()
        if configured:
            return configured
        binding = await self.store.get_primary(persona_id)
        if binding:
            return str(binding.get("umo") or "") or None
        # 兜底：取该人格最近活跃的私聊窗口
        for item in await self.store.list_bindings(persona_id):
            if item.get("kind") == "private" and int(item.get("enabled") or 0) == 1:
                return str(item.get("umo") or "") or None
        return None

    async def shared_personas_for(self, umo: str) -> str | None:
        """该窗口共享哪个人格的日程（即绑定关系）。"""
        binding = await self.store.get_binding(umo)
        if binding:
            return str(binding.get("persona_id") or "") or None
        return None

    async def setup_state(self) -> dict[str, Any]:
        """配置完成度：人格已选 + 投递窗口可用，两者都满足插件才正常运行。

        缺任一项都视为「未启用」——钩子、调度、指令里的业务动作全部短路。
        """
        settings = self._settings_getter()
        persona_id = _clean_persona_id(getattr(settings, "active_persona", ""))
        if not persona_id:
            return {
                "configured": False,
                "persona_id": "",
                "persona_name": "",
                "primary_umo": "",
                "missing": "persona",
                "hint": "还没有选择人格：请在本页「当前人格」处选择并保存。",
            }
        primary = await self.primary_umo_for(persona_id)
        if not primary:
            return {
                "configured": False,
                "persona_id": persona_id,
                "persona_name": self.persona_display_name(persona_id),
                "primary_umo": "",
                "missing": "primary",
                "hint": "人格已选，但还没有投递窗口：请在下方绑定一个私聊窗口"
                "（platform:FriendMessage:QQ号）并勾选「设为主窗口」。",
            }
        return {
            "configured": True,
            "persona_id": persona_id,
            "persona_name": self.persona_display_name(persona_id),
            "primary_umo": primary,
            "missing": "",
            "hint": "",
        }
