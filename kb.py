"""知识库只读访问：列举与检索。

AstrBot v4 的知识库管理器挂在 context.kb_manager 上：
- 列举：kb_manager.kb_insts（已加载实例）/ kb_manager.kb_db.list_kbs()
- 检索：kb_manager.retrieve(query, kb_names, top_m_final=..) ->
  {"context_text": str, "results": [...]} | {} | None

本模块对宿主结构变化全程兜底：拿不到管理器或检索失败都返回空，
调用方按「没有知识库资料」继续走。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger


async def list_kb_names(context: Any) -> list[str]:
    """列出可用的知识库名称（排序去重）。宿主不可用时返回空列表。"""
    manager = getattr(context, "kb_manager", None)
    if manager is None:
        return []
    names: list[str] = []
    insts = getattr(manager, "kb_insts", None)
    if isinstance(insts, dict):
        for helper in insts.values():
            name = str(getattr(getattr(helper, "kb", None), "kb_name", "") or "")
            if name:
                names.append(name)
    if not names:
        db = getattr(manager, "kb_db", None)
        lister = getattr(db, "list_kbs", None)
        if callable(lister):
            try:
                for record in await lister():
                    name = str(getattr(record, "kb_name", "") or "")
                    if name:
                        names.append(name)
            except Exception as exc:  # noqa: BLE001
                logger.debug("mine_chat: 列举知识库失败: %s", exc)
    return sorted(set(names))


async def retrieve_kb(context: Any, kb_name: str, query: str, top_m: int = 6) -> str:
    """检索指定知识库，返回整理好的上下文文本；失败/无结果返回空串。"""
    manager = getattr(context, "kb_manager", None)
    retrieve = getattr(manager, "retrieve", None) if manager is not None else None
    if not callable(retrieve) or not kb_name.strip():
        return ""
    try:
        payload = await retrieve(
            query, [kb_name.strip()], top_m_final=max(1, min(top_m, 10))
        )
    except Exception as exc:  # noqa: BLE001 - 检索失败不阻塞主流程
        logger.warning("mine_chat: 知识库 %s 检索失败: %s", kb_name, exc)
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("context_text") or "")
