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

from . import bus as bus_mod
from . import kb as kb_mod
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
    setup = await plugin.resolver.setup_state()
    persona_id = _q("persona_id") or setup["persona_id"]
    if not persona_id:
        persona_id = await _console_persona(plugin)
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
            "configured": setup["configured"],
            "setup_hint": setup["hint"],
            "active_persona": setup["persona_id"],
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
    bus_mod.notify()
    return ok({"updated": item_id})


# --------------------------------------------------------------------------- #
# 人格与窗口
# --------------------------------------------------------------------------- #
async def h_personas(plugin) -> dict:
    stored = await plugin.store.list_personas()
    available = await plugin.resolver.list_available_personas()
    setup = await plugin.resolver.setup_state()
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
            "active_persona": setup["persona_id"],
            "setup": setup,
        }
    )


async def h_persona_activate(plugin) -> dict:
    """把某个人格设为当前人格（写配置 active_persona）。

    人格必须是 AstrBot 人格列表里真实存在的，防止手滑选了个不存在的名字。
    """
    body = await _payload()
    persona_id = str(body.get("persona_id") or "").strip()
    if not persona_id:
        return err("缺少 persona_id")
    available = {
        item.get("persona_id")
        for item in await plugin.resolver.list_available_personas()
    }
    if available and persona_id not in available:
        return err(f"人格「{persona_id}」不在 AstrBot 人格列表里")
    config = plugin.config
    if config is None or not hasattr(config, "save_config"):
        return err("插件配置不可用")
    bucket = config.get("persona") if isinstance(config.get("persona"), dict) else {}
    bucket["active"] = persona_id
    config["persona"] = bucket
    try:
        config.save_config()
    except Exception as exc:  # noqa: BLE001
        return err(f"配置写回失败: {exc}")
    await plugin.store.upsert_persona(
        persona_id, plugin.resolver.persona_display_name(persona_id), True
    )
    setup = await plugin.resolver.setup_state()
    return ok(
        {
            "active_persona": persona_id,
            "configured": setup["configured"],
            "hint": setup["hint"],
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
def _spec_to_item(path: str, key: str, spec: dict[str, Any], value: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "path": path,
        "key": key,
        "type": spec.get("type", "string"),
        "description": spec.get("description", key),
        "hint": spec.get("hint", ""),
        "default": spec.get("default"),
        "value": value,
    }
    if "widget" in spec:
        entry["widget"] = spec.get("widget")
    if "options" in spec:
        entry["options"] = spec.get("options")
    if "labels" in spec:
        entry["labels"] = spec.get("labels")
    nested = spec.get("items")
    if isinstance(nested, dict):
        entry["item_options"] = nested.get("options")
        entry["item_labels"] = nested.get("labels")
    return entry


def _resolve_spec(schema: dict[str, Any], path: str) -> dict[str, Any] | None:
    """按 'group.key' 点路径在嵌套 schema 里找到该键的定义。"""
    parts = path.split(".")
    node = schema.get(parts[0]) if isinstance(schema, dict) else None
    if not isinstance(node, dict):
        return None
    for part in parts[1:]:
        items = node.get("items")
        if not isinstance(items, dict) or part not in items:
            return None
        node = items[part]
    return node if isinstance(node, dict) and "type" in node else None


def _set_by_path(config: Any, path: str, value: Any) -> bool:
    parts = path.split(".")
    if len(parts) == 1:
        config[parts[0]] = value
        return True
    if len(parts) != 2:
        return False
    bucket = config.get(parts[0])
    if not isinstance(bucket, dict):
        bucket = {}
    bucket[parts[1]] = value
    config[parts[0]] = bucket
    return True


async def h_config(plugin) -> dict:
    """按 schema 的分组结构返回配置。

    控制台与 AstrBot 内置配置页共用同一份 `_conf_schema.json`：
    这里的分组直接由 schema 派生，前端不再手工维护分区表。
    """
    schema = _load_schema()
    config = plugin.config or {}
    groups: list[dict[str, Any]] = []
    for name, spec in schema.items():
        if not isinstance(spec, dict) or "type" not in spec:
            continue
        if spec.get("type") == "object":
            bucket = config.get(name) if isinstance(config.get(name), dict) else {}
            items = [
                _spec_to_item(
                    f"{name}.{key}", key, child, bucket.get(key, child.get("default"))
                )
                for key, child in (spec.get("items") or {}).items()
                if isinstance(child, dict) and "type" in child
            ]
        else:
            items = [
                _spec_to_item(name, name, spec, config.get(name, spec.get("default")))
            ]
        groups.append(
            {
                "name": name,
                "description": spec.get("description", name),
                "hint": spec.get("hint", ""),
                "items": items,
            }
        )
    return ok({"groups": groups, "version": plugin.plugin_version})


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
    for path, raw in values.items():
        spec = _resolve_spec(schema, str(path))
        if spec is None:
            continue
        try:
            if not _set_by_path(config, str(path), _cast(raw, spec)):
                continue
            changed.append(str(path))
        except Exception as exc:  # noqa: BLE001
            logger.warning("mine_chat: 写入配置 %s 失败: %s", path, exc)
    if not changed:
        return err("没有可写入的配置项（键名不在 _conf_schema.json 中）")
    try:
        if hasattr(config, "save_config"):
            config.save_config()
    except Exception as exc:  # noqa: BLE001
        return err(f"配置已改内存但落盘失败: {exc}")
    return ok({"changed": changed})


async def h_providers(plugin) -> dict:
    """列出 AstrBot 已加载的对话模型（模型选择下拉的数据源）。

    AstrBot 里一个「提供商」绑定一个模型，所以一项 = 一个可选模型，
    显示成「提供商ID · 模型名」。只列已加载的：填了不存在的 id，
    AstrBot 会直接放弃那次 LLM 请求。
    """
    context = plugin.context
    providers: list[Any] = []
    get_all = getattr(context, "get_all_providers", None)
    if callable(get_all):
        try:
            providers = list(get_all() or [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("mine_chat: 读取提供商列表失败: %s", exc)

    default_id = ""
    get_default = getattr(context, "get_using_provider_async", None)
    if callable(get_default):
        default_provider = None
        try:
            default_provider = await get_default(None)
        except Exception:  # noqa: BLE001
            default_provider = None
        if default_provider is not None:
            try:
                default_id = str(getattr(default_provider.meta(), "id", "") or "")
            except Exception:  # noqa: BLE001
                default_id = ""

    items: list[dict[str, Any]] = []
    for provider in providers:
        try:
            meta = provider.meta()
        except Exception:  # noqa: BLE001
            continue
        provider_id = str(getattr(meta, "id", "") or "")
        if not provider_id:
            continue
        model = str(getattr(meta, "model", "") or "")
        items.append(
            {
                "id": provider_id,
                "model": model,
                "label": f"{provider_id} · {model}" if model else provider_id,
                "is_default": bool(default_id and provider_id == default_id),
            }
        )
    items.sort(key=lambda item: (not item["is_default"], item["label"]))
    return ok({"items": items, "default_id": default_id})


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


async def h_ui_last_page_get(plugin) -> dict:
    """读取控制台上次停留的页面（iframe 沙箱无法持久化，存服务端）。"""
    page = ""
    try:
        page = str(await plugin.store.get_meta("console_last_page") or "")
    except Exception as exc:  # noqa: BLE001
        logger.debug("mine_chat: 读取上次页面失败: %s", exc)
    return ok({"page": page})


async def h_ui_last_page_set(plugin) -> dict:
    body = await _payload()
    page = str(body.get("page") or "").strip()
    try:
        await plugin.store.set_meta("console_last_page", page)
    except Exception as exc:  # noqa: BLE001
        return err(f"保存失败: {exc}")
    return ok({"page": page})


async def h_events(plugin) -> dict:
    """长轮询：挂住请求直到全局版本号前进或超时。

    前端持有版本号循环调用；任何状态变化（发送主动消息/日程生成/世界观
    提取等）都会让挂着的请求立刻返回，页面随即拉取自己的数据刷新。
    """
    since = int(_q("since", "-1") or -1)
    timeout = _qi("timeout", 25, 5, 60)
    version = await bus_mod.wait_version(since, float(timeout))
    return ok({"version": version, "changed": version > since})


async def h_world(plugin) -> dict:
    """世界观设定页数据：手动配置 + 自动提取缓存 + 知识库绑定与候选。"""
    persona_id = await _console_persona(plugin, _q("persona_id"))
    if not persona_id:
        return err("还没有可用人格（先和角色私聊一句，或到「人格与窗口」页设置）")
    settings = plugin.settings()
    auto = None
    try:
        cached = await plugin.store.get_meta(f"auto_profile:{persona_id}")
        if cached:
            data = json.loads(cached)
            auto = {
                "world": str(data.get("world") or ""),
                "character": str(data.get("character") or ""),
                "ts": float(data.get("ts") or 0.0),
            }
    except Exception as exc:  # noqa: BLE001
        logger.debug("mine_chat: 读取自动世界观缓存失败: %s", exc)
    try:
        kb_name = await plugin.store.get_persona_kb(persona_id)
    except Exception:  # noqa: BLE001
        kb_name = None
    kb_options = await kb_mod.list_kb_names(plugin.context)
    return ok(
        {
            "persona_id": persona_id,
            "manual_world": settings.schedule_world,
            "manual_character": settings.schedule_character,
            "auto": auto,
            "kb_name": kb_name,
            "kb_options": kb_options,
            "anchor_anime": settings.proactive_image_anchor_anime,
            "anchor_realistic": settings.proactive_image_anchor_realistic,
        }
    )


async def h_world_save(plugin) -> dict:
    """保存手动世界观/角色补充设定（写入配置 schedule.world / schedule.character）。"""
    body = await _payload()
    persona_id = await _console_persona(plugin, str(body.get("persona_id") or ""))
    if not persona_id:
        return err("缺少 persona_id")
    world = str(body.get("world") or "").strip()
    character = str(body.get("character") or "").strip()
    group = plugin.config.get("schedule")
    if not isinstance(group, dict):
        group = {}
        plugin.config["schedule"] = group
    group["world"] = world
    group["character"] = character

    def _set_group_key(group_name: str, key: str, value: str) -> None:
        sub = plugin.config.get(group_name)
        if not isinstance(sub, dict):
            sub = {}
            plugin.config[group_name] = sub
        sub[key] = value

    anchor_anime = body.get("anchor_anime")
    if anchor_anime is not None:
        _set_group_key("proactive", "image_anchor_anime", str(anchor_anime).strip())
    anchor_realistic = body.get("anchor_realistic")
    if anchor_realistic is not None:
        _set_group_key(
            "proactive", "image_anchor_realistic", str(anchor_realistic).strip()
        )
    try:
        plugin.config.save_config()
    except Exception as exc:  # noqa: BLE001
        return err(f"配置已改内存但落盘失败: {exc}")
    bus_mod.notify()
    return ok({"world": world, "character": character})


async def h_world_kb_save(plugin) -> dict:
    """绑定/解绑人格的知识库（kb_name 传空 = 解绑）。"""
    body = await _payload()
    persona_id = await _console_persona(plugin, str(body.get("persona_id") or ""))
    if not persona_id:
        return err("缺少 persona_id")
    kb_name = str(body.get("kb_name") or "").strip()
    if kb_name:
        options = await kb_mod.list_kb_names(plugin.context)
        if kb_name not in options:
            return err(f"知识库 {kb_name} 不存在或未加载")
    await plugin.store.set_persona_kb(persona_id, kb_name)
    plugin.schedule_service.invalidate_profile(persona_id)
    bus_mod.notify()
    return ok({"kb_name": kb_name})


async def h_world_rebuild(plugin) -> dict:
    """强制重新提炼世界观/角色设定（检索绑定知识库 + LLM，耗时可达数十秒）。"""
    body = await _payload()
    persona_id = await _console_persona(plugin, str(body.get("persona_id") or ""))
    if not persona_id:
        return err("缺少 persona_id")
    world, character = await plugin.schedule_service.rebuild_profile(persona_id)
    bus_mod.notify()
    return ok({"world": world, "character": character})


_ROUTES: list[tuple[str, Callable[..., Awaitable[dict]], list[str]]] = [
    ("/ui/last-page", h_ui_last_page_get, ["GET"]),
    ("/ui/last-page", h_ui_last_page_set, ["POST"]),
    ("/events", h_events, ["GET"]),
    ("/world", h_world, ["GET"]),
    ("/world/save", h_world_save, ["POST"]),
    ("/world/kb/save", h_world_kb_save, ["POST"]),
    ("/world/rebuild", h_world_rebuild, ["POST"]),
    ("/overview", h_overview, ["GET"]),
    ("/plan", h_plan, ["GET"]),
    ("/plan/refresh", h_plan_refresh, ["POST"]),
    ("/plan/update", h_plan_update, ["POST"]),
    ("/personas", h_personas, ["GET"]),
    ("/personas/activate", h_persona_activate, ["POST"]),
    ("/personas/save", h_persona_save, ["POST"]),
    ("/bindings", h_bindings, ["GET"]),
    ("/bindings/save", h_binding_save, ["POST"]),
    ("/bindings/delete", h_binding_delete, ["POST"]),
    ("/bindings/primary", h_binding_set_primary, ["POST"]),
    ("/proactive/state", h_proactive_state, ["GET"]),
    ("/proactive/toggle", h_proactive_toggle, ["POST"]),
    ("/proactive/now", h_proactive_now, ["POST"]),
    ("/logs", h_logs, ["GET"]),
    ("/providers", h_providers, ["GET"]),
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
