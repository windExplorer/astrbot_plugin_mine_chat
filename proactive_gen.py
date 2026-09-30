"""主动消息的生成、投递与写回。

对应 PRD D5：主动消息不是用户输入，若只发不写回，下一轮 LLM 会完全不知道
自己说过什么。因此投递成功后必须：
  1) 写入 AstrBot 对话历史（add_message_pair）；
  2) 在下一轮注入块里带上「你刚才主动发了 X」（由 schedule_view 负责）。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import random
import re
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

IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})


# --------------------------------------------------------------------------- #
# 本地图库（主动消息配图）
# --------------------------------------------------------------------------- #
def list_image_files(directory: str) -> list[str]:
    """列出图库目录里的候选图片（返回绝对路径列表；目录不可读返回空）。"""
    directory = str(directory or "").strip()
    if not directory:
        return []
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    result: list[str] = []
    for name in names:
        full = os.path.join(directory, name)
        if not os.path.isfile(full):
            continue
        if os.path.splitext(name)[1].lower() not in IMAGE_EXTENSIONS:
            continue
        result.append(full)
    return result


def choose_image_file(files: list[str]) -> str | None:
    if not files:
        return None
    return random.choice(files)


def image_desc_from_path(path: str) -> str:
    """用文件名（去掉扩展名）作为画面线索；纯数字名视为无语义，返回空串。"""
    stem = os.path.splitext(os.path.basename(str(path)))[0].strip()
    if not stem or stem.isdigit():
        return ""
    return stem[:60]


# --------------------------------------------------------------------------- #
# 联动 ComfyUI萌绘（anima）出图
# --------------------------------------------------------------------------- #
# anima 的 companion 魔法标识（main.py SOURCE_COMPANION_PLUGIN）：命中后工具
# 返回 JSON（image_paths=服务器本地路径）且不发图，由调用方自己发。
ANIMA_SOURCE_TAG = "我会永远陪着你"
ANIMA_DRAW_TIMEOUT = 180.0


def parse_anima_result(result: Any) -> str | None:
    """解析 comfyui_draw 的返回，取第一张本地图路径；失败返回 None。

    companion source 约定：成功 → {"status": "ok", "image_paths": [本地路径...]}；
    失败 / NSFW 拦截等一律是纯文本（没有 JSON error 字段），所以解析不出
    JSON 或 status 非 ok 都按失败处理。
    """
    payload: Any = result
    if isinstance(result, str):
        try:
            payload = json.loads(result)
        except (json.JSONDecodeError, ValueError):
            return None
    if not isinstance(payload, dict):
        return None
    if str(payload.get("status") or "") != "ok":
        return None
    for path in payload.get("image_paths") or []:
        if isinstance(path, str) and path.strip():
            return path.strip()
    return None


def _resolve_draw_handler(context: Any):
    """从 AstrBot 工具管理器拿 comfyui_draw 的 handler（anima 官方跨插件契约）。"""
    manager_getter = getattr(context, "get_llm_tool_manager", None)
    if not callable(manager_getter):
        return None
    try:
        manager = manager_getter()
    except Exception:  # noqa: BLE001
        return None
    func_getter = getattr(manager, "get_func", None)
    if not callable(func_getter):
        return None
    try:
        tool = func_getter("comfyui_draw")
    except Exception:  # noqa: BLE001
        return None
    if tool is None:
        return None
    handler = getattr(tool, "handler", None) or tool
    return handler if callable(handler) else None


async def rewrite_image_prompt(
    context: Any,
    *,
    umo: str | None,
    model_id: str,
    text: str,
    style: str,
    language: str,
) -> tuple[str, bool]:
    """把中文画面描述按配置的「类型 / 语种」改写；返回 (最终描述, raw_prompt)。

    raw_prompt=True 表示描述已是最终形态（英文 Danbooru 标签），
    传给 anima 时让它跳过二次整理；改写失败回退中文原文（交由萌绘整理）。
    """
    instruction = prompts.build_prompt_rewrite(text, style, language)
    if instruction is None:
        return text, False
    try:
        rewritten = await llm_mod.chat_text(
            context,
            umo=umo,
            model_id=model_id,
            system_prompt="你是画面提示词改写助手，只输出结果本身。",
            prompt=instruction,
            temperature=0.3,
        )
    except llm_mod.LLMError as exc:  # noqa: BLE001
        logger.warning("mine_chat: 画面描述改写失败，回退中文原文: %s", exc)
        return text, False
    rewritten = rewritten.strip().strip('"').strip()
    if not rewritten:
        return text, False
    return rewritten, style == "danbooru"


async def fetch_anima_image(
    context: Any, event: Any, *, prompt: str, workflow: str = "", raw_prompt: bool = False
) -> str | None:
    """联动 ComfyUI萌绘出一张图，返回服务器本地文件路径（失败返回 None）。

    prompt 通常是一句中文画面描述（由萌绘按工作流底模整理语种与风格）；
    raw_prompt=True 时 prompt 已是最终形态（英文标签），萌绘跳过二次整理。
    event 用投递窗口最近一次真实用户消息事件（anima 侧必需）。
    """
    handler = _resolve_draw_handler(context)
    if handler is None:
        logger.info("mine_chat: 未找到 comfyui_draw 工具，ComfyUI萌绘联动不可用")
        return None
    if event is None:
        logger.info("mine_chat: 没有可用的会话事件，跳过 anima 出图")
        return None
    kwargs: dict[str, Any] = {"prompt": prompt, "source": ANIMA_SOURCE_TAG}
    if workflow.strip():
        kwargs["workflow"] = workflow.strip()
    if raw_prompt:
        kwargs["raw_prompt"] = True
    try:
        result = await asyncio.wait_for(handler(event, **kwargs), timeout=ANIMA_DRAW_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("mine_chat: anima 出图超时（>%.0fs），本次不配图", ANIMA_DRAW_TIMEOUT)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("mine_chat: anima 出图异常: %s", exc)
        return None
    path = parse_anima_result(result)
    if not path:
        logger.info("mine_chat: anima 未产出图片（%s）", str(result)[:120])
    return path


# --------------------------------------------------------------------------- #
# 联动萌萌表情包（moe_meme）取图
# --------------------------------------------------------------------------- #
_MEME_EXTS = {"png", "gif", "jpg", "jpeg", "webp"}


def meme_cache_filename(sticker_id: str, fmt: str) -> str:
    """表情缓存文件名：id 清洗 + 扩展名白名单。"""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(sticker_id or "").strip())[:64]
    if not safe:
        safe = "sticker"
    ext = str(fmt or "png").strip().lower().lstrip(".")
    if ext not in _MEME_EXTS:
        ext = "png"
    return f"{safe}.{ext}"


async def fetch_meme_image(token: str, cache_dir: str) -> str | None:
    """从萌萌表情包的数据源随机拉一张表情，落本地缓存后返回路径。

    moe_meme 没有跨插件 API，这里直接复用其数据源模块
    astrbot_plugin_moe_meme.wuwa_source（只依赖 aiohttp）。票券链约
    16 分钟有效，所以必须下载落盘后再发本地文件，绝不发远端 URL。
    """
    try:
        module = importlib.import_module("astrbot_plugin_moe_meme.wuwa_source")
    except Exception:  # noqa: BLE001 - 未安装表情包插件
        logger.info("mine_chat: 未安装 astrbot_plugin_moe_meme，表情联动不可用")
        return None
    source_cls = getattr(module, "WuwaSource", None)
    if source_cls is None:
        logger.warning("mine_chat: moe_meme 的 wuwa_source 结构变化，表情联动不可用")
        return None

    source = None
    sticker = None
    data = None
    try:
        source = source_cls(token=token or "")
        sticker = await asyncio.wait_for(source.random_sticker(None), timeout=25.0)
        data = await asyncio.wait_for(source.download(sticker), timeout=30.0)
    except Exception as exc:  # noqa: BLE001 - SourceError / 超时 / 网络错误统一降级
        logger.info("mine_chat: 表情包拉取失败: %s", exc)
        return None
    finally:
        close = getattr(source, "close", None)
        if callable(close):
            try:
                await close()
            except Exception:  # noqa: BLE001
                pass

    filename = meme_cache_filename(
        getattr(sticker, "sticker_id", ""), getattr(sticker, "fmt", "")
    )
    try:
        os.makedirs(cache_dir, exist_ok=True)
        path = os.path.join(cache_dir, filename)
        with open(path, "wb") as handle:
            handle.write(data or b"")
    except Exception as exc:  # noqa: BLE001
        logger.warning("mine_chat: 表情落盘失败: %s", exc)
        return None
    return path


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
        image_plan: dict[str, Any] | None = None,
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
                image_hint=prompts.build_image_hint(
                    str(image_plan.get("desc") or "")
                )
                if image_plan
                else "",
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
    async def deliver(
        self,
        umo: str,
        texts: list[str],
        *,
        delay: float = 1.5,
        image_path: str | None = None,
    ) -> bool:
        """发送消息；任一条未送达即视为整体失败（调用方不应写回历史）。

        ``image_path`` 非空时把图片附在最后一条消息里；图片组件构造失败就降级为纯文本。
        """
        if not texts:
            return False
        try:
            from astrbot.api.message_components import Image, Plain
            from astrbot.core.message.message_event_result import MessageChain
        except Exception as exc:  # noqa: BLE001 - 导入失败说明环境异常
            logger.error("mine_chat: 消息组件导入失败: %s", exc)
            return False

        for index, text in enumerate(texts):
            if index > 0 and delay > 0:
                await asyncio.sleep(delay)
            components: list[Any] = [Plain(text)]
            is_last = index == len(texts) - 1
            if is_last and image_path:
                try:
                    components.append(Image.fromFileSystem(image_path))
                except Exception as exc:  # noqa: BLE001 - 图坏了就发纯文本
                    logger.warning("mine_chat: 图片组件构造失败，降级纯文本: %s", exc)
            try:
                result = await self.context.send_message(
                    umo, MessageChain(components)
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
