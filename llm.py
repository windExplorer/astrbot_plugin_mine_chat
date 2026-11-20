"""LLM 调用的统一入口：provider 解析、超时、错误包装、JSON 提取。

日程生成与主动消息都走这里，避免两处各自处理 provider 差异。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from astrbot.api import logger

_CODE_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

DEFAULT_TIMEOUT = 120.0


def model_for(settings: Any, purpose: str) -> str:
    """解析某用途实际该传给 resolve_provider 的 model_id。

    优先级：细分覆盖（schedule.model / proactive.model）> 统一默认 llm.default_model > 空（跟随系统）。
    这样用户既能在「配置 → 模型」里设一个全局默认，也能给日程/主动消息单独指定模型。
    """
    default = ""
    try:
        default = str(getattr(settings, "llm_default_model", "") or "").strip()
    except Exception:  # noqa: BLE001 - 老 Settings 没有该字段
        pass
    if purpose == "schedule":
        override = str(getattr(settings, "schedule_model", "") or "").strip()
    elif purpose == "proactive":
        override = str(getattr(settings, "proactive_model", "") or "").strip()
    else:
        override = ""
    return override or default


class LLMError(RuntimeError):
    """LLM 调用失败（无可用 provider / 超时 / 返回异常）。"""


def _provider_identity(provider: Any) -> tuple[str, str]:
    """取 provider 的 (id, model)，用于日志/控制台展示"实际用了哪个模型"。"""
    pid, model = "", ""
    try:
        meta = provider.meta()
        pid = str(getattr(meta, "id", "") or "")
        model = str(getattr(meta, "model", "") or "")
    except Exception:  # noqa: BLE001 - 老版本/异常 provider 兜底
        pass
    if not pid:
        pid = str(getattr(provider, "provider_id", "") or "")
    return pid, model


async def describe_provider(
    context: Any, umo: str | None = None, model_id: str = ""
) -> dict:
    """解析 provider 并返回"用了谁 + 从哪来"，日志与控制台共用同一套口径。

    Returns:
        {provider, provider_id, model, source, source_detail, requested}
        - source ∈ explicit / system / none
          explicit = 配置里显式指定的模型（细分覆盖或统一默认）
          system   = 跟随 AstrBot 当前模型（get_using_provider）
          none     = 没解析到任何 provider
        provider 为 None 时 source=none，调用方据此降级。
    """
    result: dict[str, Any] = {
        "provider": None, "provider_id": "", "model": "",
        "source": "none", "source_detail": "", "requested": str(model_id or ""),
    }

    # 1) 显式指定。注意用 context.get_provider_by_id（同步）而不是
    #    context.provider_manager.get_provider_by_id——后者在 AstrBot 4.29.x 是 async，
    #    旧代码没 await，拿到的是 coroutine，后面 .text_chat 必炸（v1.4.0 修复）。
    if model_id:
        getter = getattr(context, "get_provider_by_id", None)
        provider = None
        if callable(getter):
            try:
                provider = getter(model_id)
            except Exception as exc:  # noqa: BLE001 - 配置写错不应让插件崩
                logger.warning("mine_chat: 指定模型 %s 解析失败: %s", model_id, exc)
        # 兜底：确认拿到的是真 provider（不是 coroutine / 字符串）
        if provider is not None and hasattr(provider, "text_chat"):
            pid, model = _provider_identity(provider)
            result.update({
                "provider": provider, "provider_id": pid or str(model_id),
                "model": model, "source": "explicit", "source_detail": "配置指定",
            })
            return result
        if provider is not None and not hasattr(provider, "text_chat"):
            logger.warning(
                "mine_chat: 指定模型 %s 解析结果异常（%s），回退跟随系统模型",
                model_id, type(provider).__name__,
            )

    # 2) 跟随会话/系统当前模型（AstrBot 的 get_using_provider 口径）
    getter_async = getattr(context, "get_using_provider_async", None)
    if callable(getter_async):
        try:
            provider = await getter_async(umo)
        except Exception as exc:  # noqa: BLE001
            logger.warning("mine_chat: 按会话解析 provider 失败: %s", exc)
            provider = None
        if provider is not None and hasattr(provider, "text_chat"):
            pid, model = _provider_identity(provider)
            result.update({
                "provider": provider, "provider_id": pid, "model": model,
                "source": "system", "source_detail": "跟随会话/系统当前模型",
            })
            return result

    getter_sync = getattr(context, "get_using_provider", None)
    if callable(getter_sync):
        try:
            provider = getter_sync(umo)
        except Exception as exc:  # noqa: BLE001
            logger.warning("mine_chat: 取默认 provider 失败: %s", exc)
            provider = None
        if provider is not None and hasattr(provider, "text_chat"):
            pid, model = _provider_identity(provider)
            result.update({
                "provider": provider, "provider_id": pid, "model": model,
                "source": "system", "source_detail": "跟随系统默认模型",
            })
            return result
    return result


async def resolve_provider(context: Any, umo: str | None = None, model_id: str = "") -> Any:
    """解析要使用的 provider（兼容旧调用；新代码用 describe_provider 看来源）。"""
    return (await describe_provider(context, umo=umo, model_id=model_id))["provider"]


def _extract_text(response: Any) -> str:
    """从 LLMResponse 里尽最大努力取出纯文本。"""
    text = getattr(response, "completion_text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()

    chain = getattr(response, "result_chain", None)
    if chain is not None:
        parts: list[str] = []
        for component in getattr(chain, "chain", []) or []:
            piece = getattr(component, "text", None)
            if isinstance(piece, str) and piece:
                parts.append(piece)
        if parts:
            return "".join(parts).strip()

    raw = getattr(response, "raw_completion", None)
    if raw is not None:
        try:
            choices = getattr(raw, "choices", None)
            if choices:
                message = getattr(choices[0], "message", None)
                content = getattr(message, "content", None)
                if isinstance(content, str):
                    return content.strip()
        except Exception:  # noqa: BLE001 - 兼容不同 provider
            pass
    return ""


async def chat_text(
    context: Any,
    *,
    umo: str | None = None,
    model_id: str = "",
    system_prompt: str = "",
    prompt: str = "",
    contexts: list[dict] | None = None,
    temperature: float | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """发起一次纯文本对话，返回模型输出（已 strip）。失败抛 LLMError。"""
    desc = await describe_provider(context, umo=umo, model_id=model_id)
    provider = desc["provider"]
    if provider is None:
        raise LLMError("没有可用的对话模型，请先在 AstrBot 里配置 Provider")
    # 每次调用都记录"实际用了哪个模型 + 从哪来"，让 WebUI/日志能核对来源。
    # debug 级别避免高频日程重试刷屏；失败时下面的 warning 会带同样上下文。
    logger.debug(
        "mine_chat: 模型来源=%s(%s) provider=%s model=%s umo=%s",
        desc["source"], desc["source_detail"], desc["provider_id"] or "-",
        desc["model"] or "-", umo or "-",
    )

    kwargs: dict[str, Any] = {
        "prompt": prompt,
        "system_prompt": system_prompt,
        "contexts": contexts or [],
    }

    async def _call(**extra: Any) -> Any:
        return await asyncio.wait_for(
            provider.text_chat(**kwargs, **extra), timeout=timeout
        )

    try:
        if temperature is not None:
            try:
                response = await _call(temperature=temperature)
            except TypeError:
                # 部分 provider 不接受 temperature，退回不带该参数。
                response = await _call()
        else:
            response = await _call()
    except asyncio.TimeoutError as exc:
        raise LLMError(f"模型调用超时（>{int(timeout)}s）") from exc
    except Exception as exc:  # noqa: BLE001 - 统一包装成 LLMError 供上层降级
        raise LLMError(f"模型调用失败: {exc}") from exc

    text = _extract_text(response)
    if not text:
        raise LLMError("模型返回了空内容")
    return text


# --------------------------------------------------------------------------- #
# JSON 提取
# --------------------------------------------------------------------------- #
def extract_json(text: str) -> Any | None:
    """从模型输出里尽力提取一个 JSON 值；失败返回 None。"""
    if not text or not text.strip():
        return None
    candidates: list[str] = []

    stripped = text.strip()
    candidates.append(stripped)

    for match in _CODE_BLOCK_RE.finditer(stripped):
        block = match.group(1).strip()
        if block:
            candidates.append(block)

    first_brace = stripped.find("{")
    last_brace = stripped.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidates.append(stripped[first_brace : last_brace + 1])

    first_bracket = stripped.find("[")
    last_bracket = stripped.rfind("]")
    if first_bracket != -1 and last_bracket > first_bracket:
        candidates.append(stripped[first_bracket : last_bracket + 1])

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
    return None
