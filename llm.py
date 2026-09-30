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


class LLMError(RuntimeError):
    """LLM 调用失败（无可用 provider / 超时 / 返回异常）。"""


async def resolve_provider(context: Any, umo: str | None = None, model_id: str = "") -> Any:
    """解析要使用的 provider。

    优先级：显式 model_id > 该会话当前 provider > 全局默认 provider。
    """
    manager = getattr(context, "provider_manager", None)

    if model_id and manager is not None:
        getter = getattr(manager, "get_provider_by_id", None)
        if callable(getter):
            try:
                provider = getter(model_id)
            except Exception as exc:  # noqa: BLE001 - 配置写错不应让插件崩
                logger.warning("mine_chat: 指定模型 %s 解析失败: %s", model_id, exc)
                provider = None
            if provider is not None:
                return provider

    getter_async = getattr(context, "get_using_provider_async", None)
    if callable(getter_async):
        try:
            provider = await getter_async(umo)
        except Exception as exc:  # noqa: BLE001
            logger.warning("mine_chat: 按会话解析 provider 失败: %s", exc)
            provider = None
        if provider is not None:
            return provider

    getter_sync = getattr(context, "get_using_provider", None)
    if callable(getter_sync):
        try:
            return getter_sync(umo)
        except Exception as exc:  # noqa: BLE001
            logger.warning("mine_chat: 取默认 provider 失败: %s", exc)
    return None


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
    provider = await resolve_provider(context, umo=umo, model_id=model_id)
    if provider is None:
        raise LLMError("没有可用的对话模型，请先在 AstrBot 里配置 Provider")

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
