"""控制台后端路由（AstrBot 插件页的桥接 API）。

路由前缀 ``/astrbot_plugin_mine_chat``；返回信封必须遵循 AstrBot 桥接约定：
  - 成功 → ``{"status": "ok", "data": <payload>}``
  - 失败 → ``{"status": "error", "message": "..."}``
"""

from __future__ import annotations

import json
import time
from datetime import date as date_cls
from pathlib import Path
from typing import Any, Awaitable, Callable

from astrbot.api import logger

from . import schedule_view as view_mod

try:  # quart 是 AstrBot 的运行期依赖
    from quart import request
except Exception:  # pragma: no cover - 仅本地静态检查时缺失
    request = None  # type: ignore

ROUTE_PREFIX = "/astrbot_plugin_mine_chat"


# --------------------------------------------------------------------------- #
# 信封与参数
# --------------------------------------------------------------------------- #
def ok(data: Any = None) -> dict:
    return {"status": "ok", "data": data}


def err(message: str) -> dict:
    return {"status": "error", "message": message}


def _q(name: str, default: str = "") -> str:
    try:
        if request is None:
            return default
        return (request.args.get(name) or default).strip()
    except Exception:
        return default


def _qi(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(_q(name) or default)))
    except Exception:
        return default


async def _payload() -> dict:
    try:
        if request is None:
            return {}
        return await request.get_json(silent=True) or {}
    except Exception:
        return {}


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on", "是", "开")


def _load_schema() -> dict[str, Any]:
    try:
        path = Path(__file__).resolve().parent / "_conf_schema.json"
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("mine_chat: 读取 _conf_schema.json 失败: %s", exc)
        return {}


def _cast(value: Any, spec: dict[str, Any]) -> Any:
    """按 schema 的 type 转换控制台传来的值。"""
    kind = str(spec.get("type") or "string")
    if kind == "bool":
        return _as_bool(value, bool(spec.get("default")))
    if kind == "int":
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return int(spec.get("default") or 0)
    if kind == "float":
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(spec.get("default") or 0.0)
    if kind == "list":
        if isinstance(value, list):
            return [str(item) for item in value]
        if value is None:
            return list(spec.get("default") or [])
        return [part.strip() for part in str(value).split(",") if part.strip()]
    if kind in {"text", "string"}:
        return "" if value is None else str(value)
    return value


# --------------------------------------------------------------------------- #
# 公共取值
# --------------------------------------------------------------------------- #
async def _console_persona(plugin: Any, persona_id: str = "") -> str:
    if persona_id:
        return persona_id
    try:
        personas = await plugin.store.list_personas()
    except Exception:  # noqa: BLE001
        return ""
    for persona in personas:
        if int(persona.get("enabled") or 0):
            return str(persona.get("persona_id") or "")
    return str(personas[0].get("persona_id") or "") if personas else ""


async def _plan_payload(plugin: Any, persona_id: str, plan_date: str) -> dict[str, Any]:
    plan = await plugin.store.get_plan(persona_id, plan_date)
    data = plugin.schedule_service.plan_to_json(plan) or {
        "persona_id": persona_id,
        "plan_date": plan_date,
        "items": [],
        "source": None,
    }
    items = list(plan.get("items", [])) if plan else []
    data["current_text"] = view_mod.describe(items, view_mod.now_minutes_for(plan_date))
    data["now_minute"] = view_mod.now_minutes_for(plan_date)
    return data


# --------------------------------------------------------------------------- #
# 总览
# --------------------------------------------------------------------------- #
async def h_overview(plugin) -> dict:
    settings = plugin.settings()
    persona_id = await _console_persona(plugin, _q("persona_id"))
    personas = await plugin.store.list_personas()

    plan_date = await plugin.schedule_service.resolve_active_date(persona_id) if persona_id else ""
    plan_data = await _plan_payload(plugin, persona_id, plan_date) if persona_id else {}
    status = await plugin.proactive.status(persona_id) if persona_id else {}
    logs = await plugin.store.list_logs(persona_id=persona_id or None, limit=8)
    summary = await plugin.store.log_summary(persona_id=persona_id or None, since=time.time() - 7 * 86400)

    bindings = await plugin.store.list_bindings(persona_id or None)

    return ok(
        {
            "version": plugin.plugin_version,
            "enabled": settings.enabled,
            "persona_id": persona_id,
            "persona_name": plugin.resolver.persona_display_name(persona_id) if persona_id else "",
            "personas": [
                {
                    "persona_id": item.get("persona_id"),
                    "persona_name": item.get("persona_name"),
                    "enabled": bool(int(item.get("enabled") or 0)),
                }
                for item in personas
            ],
            "bindings": [
                {
                    "umo": item.get("umo"),
                    "kind": item.get("kind"),
                    "is_primary": bool(int(item.get("is_primary") or 0)),
                    "last_seen_at": item.get("last_seen_at"),
                }
                for item in bindings
            ],
            "plan": plan_data,
            "proactive": status,
            "log_summary": summary,
            "recent_logs": [_log_row(row) for row in logs],
        }
    )


def _log_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "ts": row.get("ts"),
        "ts_text": time.strftime("%m-%d %H:%M:%S", time.localtime(float(row.get("ts") or 0))),
        "decision": row.get("decision"),
        "reason": row.get("reason"),
        "umo": row.get("umo"),
        "content": row.get("content"),
    }


# --------------------------------------------------------------------------- #
# 日程
# --------------------------------------------------------------------------- #
async def h_plan(plugin) -> dict:
    persona_id = await _console_persona(plugin, _q("persona_id"))
    if not persona_id:
        return err("还没有任何人格档案，请先在私聊里对角色说一句话。")
    plan_date = _q("date") or await plugin.schedule_service.resolve_active_date(persona_id)
    data = await _plan_payload(plugin, persona_id, plan_date)
    data["persona_name"] = plugin.resolver.persona_display_name(persona_id)
    return ok(data)


async def h_plan_refresh(plugin) -> dict:
    body = await _payload()
    persona_id = await _console_persona(plugin, str(body.get("persona_id") or ""))
    if not persona_id:
        return err("缺少 persona_id")
    target = str(body.get("date") or "") or None
    plan = await plugin.schedule_service.refresh(persona_id, plan_date=target)
    if not plan:
        return err("生成失败，请查看 AstrBot 日志。")
    date_text = str(plan.get("plan_date") or target or date_cls.today().isoformat())
    data = await _plan_payload(plugin, persona_id, date_text)
    return ok(data)


async def h_plan_update(plugin) -> dict:
    body = await _payload()
    try:
        item_id = int(body.get("item_id") or 0)
    except (TypeError, ValueError):
        return err("item_id 不合法")
    if item_id <= 0:
        return err("缺少 item_id")
    await plugin.store.update_plan_item(
        item_id,
        activity=None if body.get("activity") is None else str(body.get("activity")),
        mood=None if body.get("mood") is None else str(body.get("mood")),
        message_seed=None
        if body.get("message_seed") is None
        else str(body.get("message_seed")),
    )
    return ok({"updated": item_id})


# --------------------------------------------------------------------------- #
# 人格与窗口
# --------------------------------------------------------------------------- #
async def h_personas(plugin) -> dict:
    stored = await plugin.store.list_personas()
    available = await plugin.resolver.list_available_personas()
    return ok(
        {
            "stored": [
                {
                    "persona_id": item.get("persona_id"),
                    "persona_name": item.get("persona_name"),
                    "enabled": bool(int(item.get("enabled") or 0)),
                    "created_at": item.get("created_at"),
                }
                for item in stored
            ],
            "available": available,
            "persona_override": plugin.settings().persona_override,
        }
    )


async def h_persona_save(plugin) -> dict:
    body = await _payload()
    persona_id = str(body.get("persona_id") or "").strip()
    if not persona_id:
        return err("缺少 persona_id")
    enabled = _as_bool(body.get("enabled"), True)
    await plugin.store.upsert_persona(
        persona_id, plugin.resolver.persona_display_name(persona_id), enabled
    )
    await plugin.store.set_persona_enabled(persona_id, enabled)
    return ok({"persona_id": persona_id, "enabled": enabled})


async def h_bindings(plugin) -> dict:
    persona_id = _q("persona_id")
    rows = await plugin.store.list_bindings(persona_id or None)
    return ok(
        {
            "items": [
                {
                    "umo": row.get("umo"),
                    "persona_id": row.get("persona_id"),
                    "kind": row.get("kind"),
                    "is_primary": bool(int(row.get("is_primary") or 0)),
                    "enabled": bool(int(row.get("enabled") or 0)),
                    "first_seen_at": row.get("first_seen_at"),
                    "last_seen_at": row.get("last_seen_at"),
                }
                for row in rows
            ]
        }
    )


async def h_binding_save(plugin) -> dict:
    body = await _payload()
    umo = str(body.get("umo") or "").strip()
    persona_id = str(body.get("persona_id") or "").strip()
    if not umo or not persona_id:
        return err("需要同时提供 umo 与 persona_id")
    kind = view_mod_umo_kind(umo)
    if kind == "other":
        return err("umo 格式应为 platform:FriendMessage:ID 或 platform:GroupMessage:ID")
    await plugin.store.upsert_binding(
        umo=umo,
        persona_id=persona_id,
        kind=kind,
        is_primary=_as_bool(body.get("is_primary"), False),
        enabled=_as_bool(body.get("enabled"), True),
    )
    if _as_bool(body.get("is_primary"), False):
        await plugin.store.set_primary(persona_id, umo)
    return ok({"umo": umo, "persona_id": persona_id})


async def h_binding_delete(plugin) -> dict:
    body = await _payload()
    umo = str(body.get("umo") or "").strip()
    if not umo:
        return err("缺少 umo")
    await plugin.store.delete_binding(umo)
    return ok({"deleted": umo})


def view_mod_umo_kind(umo: str) -> str:
    from .scope import umo_kind

    return umo_kind(umo)


async def h_binding_set_primary(plugin) -> dict:
    body = await _payload()
    persona_id = str(body.get("persona_id") or "").strip()
    umo = str(body.get("umo") or "").strip()
    if not persona_id:
        return err("缺少 persona_id")
    await plugin.store.set_primary(persona_id, umo or None)
    return ok({"persona_id": persona_id, "primary_umo": umo})


# --------------------------------------------------------------------------- #
# 主动消息
# --------------------------------------------------------------------------- #
async def h_proactive_state(plugin) -> dict:
    persona_id = await _console_persona(plugin, _q("persona_id"))
    if not persona_id:
        return err("还没有任何人格档案")
    data = await plugin.proactive.status(persona_id)
    data["persona_name"] = plugin.resolver.persona_display_name(persona_id)
    return ok(data)


async def h_proactive_toggle(plugin) -> dict:
    body = await _payload()
    persona_id = await _console_persona(plugin, str(body.get("persona_id") or ""))
    if not persona_id:
        return err("缺少 persona_id")
    enabled = _as_bool(body.get("enabled"), True)
    await plugin.proactive.set_enabled(persona_id, enabled)
    return ok({"persona_id": persona_id, "enabled": enabled})


async def h_proactive_now(plugin) -> dict:
    body = await _payload()
    persona_id = await _console_persona(plugin, str(body.get("persona_id") or ""))
    if not persona_id:
        return err("缺少 persona_id")
    sent, reason = await plugin.proactive.trigger_now(persona_id)
    return ok({"sent": sent, "reason": reason})


async def h_logs(plugin) -> dict:
    persona_id = _q("persona_id")
    limit = _qi("limit", 50, 1, 500)
    offset = _qi("offset", 0, 0, 100000)
    decision = _q("decision")
    reason = _q("reason")
    rows = await plugin.store.list_logs(
        persona_id=persona_id or None,
        limit=limit,
        offset=offset,
        decision=decision or None,
        reason=reason or None,
    )
    total = await plugin.store.count_logs(persona_id or None)
    return ok({"items": [_log_row(row) for row in rows], "total": total})


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
async def h_config(plugin) -> dict:
    schema = _load_schema()
    config = plugin.config or {}
    items: list[dict[str, Any]] = []
    for key, spec in schema.items():
        if not isinstance(spec, dict):
            continue
        entry: dict[str, Any] = {
            "key": key,
            "type": spec.get("type", "string"),
            "description": spec.get("description", key),
            "hint": spec.get("hint", ""),
            "default": spec.get("default"),
            "value": config.get(key, spec.get("default")),
        }
        if "options" in spec:
            entry["options"] = spec.get("options")
        if "labels" in spec:
            entry["labels"] = spec.get("labels")
        nested = spec.get("items")
        if isinstance(nested, dict):
            entry["item_options"] = nested.get("options")
            entry["item_labels"] = nested.get("labels")
        items.append(entry)
    return ok({"items": items, "version": plugin.plugin_version})


async def h_config_save(plugin) -> dict:
    body = await _payload()
    values = body.get("values")
    if not isinstance(values, dict):
        return err("请求体需要包含 values 对象")
    schema = _load_schema()
    config = plugin.config
    if config is None:
        return err("插件配置不可用")

    changed: list[str] = []
    for key, raw in values.items():
        spec = schema.get(key)
        if not isinstance(spec, dict):
            continue
        try:
            config[key] = _cast(raw, spec)
            changed.append(key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("mine_chat: 写入配置 %s 失败: %s", key, exc)
    if not changed:
        return err("没有可写入的配置项（键名不在 _conf_schema.json 中）")
    try:
        if hasattr(config, "save_config"):
            config.save_config()
    except Exception as exc:  # noqa: BLE001
        return err(f"配置已改内存但落盘失败: {exc}")
    return ok({"changed": changed})


# --------------------------------------------------------------------------- #
# 注册
# --------------------------------------------------------------------------- #
def _bind(plugin, fn: Callable[[Any], Awaitable[dict]]) -> Callable[[], Awaitable[dict]]:
    async def handler() -> dict:
        try:
            return await fn(plugin)
        except Exception as exc:  # noqa: BLE001 - 任何异常都转成信封，避免前端只见 500
            logger.exception("mine_chat: %s 处理异常", fn.__name__)
            return err(f"{fn.__name__} 失败: {exc}")

    handler.__name__ = f"minechat_{fn.__name__}"
    return handler


_ROUTES: list[tuple[str, Callable[..., Awaitable[dict]], list[str]]] = [
    ("/overview", h_overview, ["GET"]),
    ("/plan", h_plan, ["GET"]),
    ("/plan/refresh", h_plan_refresh, ["POST"]),
    ("/plan/update", h_plan_update, ["POST"]),
    ("/personas", h_personas, ["GET"]),
    ("/personas/save", h_persona_save, ["POST"]),
    ("/bindings", h_bindings, ["GET"]),
    ("/bindings/save", h_binding_save, ["POST"]),
    ("/bindings/delete", h_binding_delete, ["POST"]),
    ("/bindings/primary", h_binding_set_primary, ["POST"]),
    ("/proactive/state", h_proactive_state, ["GET"]),
    ("/proactive/toggle", h_proactive_toggle, ["POST"]),
    ("/proactive/now", h_proactive_now, ["POST"]),
    ("/logs", h_logs, ["GET"]),
    ("/config", h_config, ["GET"]),
    ("/config/save", h_config_save, ["POST"]),
]


def register(plugin) -> None:
    """把控制台路由注册到 AstrBot（由 main.initialize 调用）。"""
    context = getattr(plugin, "context", None)
    if context is None or not hasattr(context, "register_web_api"):
        logger.warning("mine_chat: 当前环境不支持 register_web_api，跳过控制台注册")
        return
    for path, fn, methods in _ROUTES:
        context.register_web_api(
            f"{ROUTE_PREFIX}{path}",
            _bind(plugin, fn),
            methods,
            f"MineChat {path}",
        )
    logger.info("mine_chat: 已注册 %s 条控制台路由（前缀 %s）", len(_ROUTES), ROUTE_PREFIX)
