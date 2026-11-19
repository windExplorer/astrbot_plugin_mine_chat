"""跨插件桥：萌萌好感度（affinity.v1）与萌萌语音（tts.v1）。

为什么单独一个模块：这两个联动都是**可选**的，装没装、启没启、版本够不够
都不该让主功能（日程 / 主动消息）崩。集中到一个文件里，所有降级逻辑只写一遍，
上层只管拿 `None` 就当「没有这个能力」。

口径（依据两侧源码实测）：

好感度 `astrbot_plugin_moe_affinity` 有正式契约 `affinity.v1`：
    star = context.get_registered_star("astrbot_plugin_moe_affinity")
    api = star.star_cls.api          # AffinityApi
    role_id = api.role_for(persona_id)          # 同步，返回 str
    snap = await api.get(role_id, user_id)      # status=ok / not_recorded
    mood = await api.get_mood(role_id)          # axes: joy/anger/sorrow/fear (1-10)
**只读**：角色「主动发了一条」不该给自己加分——好感度是用户互动产生的。
写事件需要显式配置开启，且 source 不用 "core"（不占每日额度）。

语音 `astrbot_plugin_moe_tts`（萌萌语音）有正式契约 `tts.v1`：
    star = context.get_registered_star("astrbot_plugin_moe_tts")
    api = star.star_cls.api          # TtsApi
    api.available()                  # 同步：配了服务端 / 没冷却 / 有音色
    res = await api.synthesize(text, voice, pre_translated=True)
    # res = {ok, files:[wav路径...], error, ...}；失败 ok=False 不抛异常
    api.cleanup(path)                # 延迟清理合成产物
每一步都做版本 / 结构校验 + try/except，任何一环不对就静默降级为纯文字。
（v1.3.0 前复用的是 CosyVoice 的私有 engine；换成 tts.v1 契约后调用方零依赖对方内部实现。）
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

AFFINITY_PLUGIN = "astrbot_plugin_moe_affinity"
AFFINITY_API_VERSION = "affinity.v1"
VOICE_PLUGIN = "astrbot_plugin_moe_tts"
VOICE_API_VERSION = "tts.v1"

# 合成超时：语音服务端默认并发 1，长文本会串行排队；主动消息不等太久。
VOICE_TIMEOUT = 90.0
# 契约的 cleanup() 默认 600s 清理；这里覆盖成 120s（主动消息场景无需留那么久），
# 给平台读文件留出时间即可。
VOICE_CLEANUP_DELAY = 120.0


# --------------------------------------------------------------------------- #
# 关系档位 → 可读文案（纯函数，便于单测）
# --------------------------------------------------------------------------- #
# affinity 的档位是 6 档（阈值见其 store.py），人格提示词里常见的是 L0~L4，
# 两套编号不是一回事，所以这里同时给出「档位中文名」和「大致对应人设第几层」，
# 让模型能把外部分数落到自己的关系层上，而不是硬套。
_STAGE_HINT: dict[str, str] = {
    "stranger": "陌生。彼此还只是刚认识，她不会主动示好，也不会透露自己的生活细节。",
    "known": "眼熟。见面会点头、偶尔搭一两句话，但边界感仍很强。",
    "familiar": "熟悉。已经能自然地聊天，会记住对方随口提过的小事。",
    "close": "亲近。开始护短、会为对方破例，嘴上依旧敷衍，行动上很诚实。",
    "intimate": "亲密。可以直球、可以吃醋，撒娇藏在很短的一句话里。",
    "bonded": "羁绊。已经不需要证明什么了，沉默也不尴尬；偶尔会露出最软的一面。",
}

# 档位对「主动程度」的约束：低档位不该出现高频主动和黏人语气。
_STAGE_INITIATIVE: dict[str, str] = {
    "stranger": "刚认识，主动一次已经算破例：只提一件与自己有关的小事，不问对方在做什么。",
    "known": "偶尔主动，语气客气而短；不要追问、不要撒娇。",
    "familiar": "可以自然分享生活，偶尔反问一句；仍然不黏人。",
    "close": "主动变得自然，会想「这件事他大概会想知道」，但不说破。",
    "intimate": "可以主动找人说一句很想说的话，短、克制、不甜腻。",
    "bonded": "主动几乎不需要理由，一条「……醒了。」就够了。",
}


def stage_hint(stage: str) -> str:
    return _STAGE_HINT.get(str(stage or "").strip().lower(), "")


def stage_initiative(stage: str) -> str:
    return _STAGE_INITIATIVE.get(str(stage or "").strip().lower(), "")


@dataclass(frozen=True)
class AffinitySnapshot:
    """一次读取得到的好感度快照（不可变，便于跨协程传）。"""

    role_id: str = ""
    user_id: str = ""
    score: float = 0.0
    peak: float = 0.0
    stage: str = ""
    stage_label: str = ""
    next_stage_label: str = ""
    progress: float = 0.0
    mood_description: str = ""
    axes: dict[str, int] = field(default_factory=dict)
    mood_neutral: bool = True
    mood_enabled: bool = False

    @property
    def known(self) -> bool:
        return bool(self.role_id and self.stage)

    def relation_line(self) -> str:
        """给提示词用的「关系现状」一行。"""
        if not self.known:
            return ""
        parts = [f"{self.stage_label or self.stage}（好感度 {self.score:g}"]
        if self.next_stage_label:
            # progress 是「当前档位内的进度」，写成「已完成 x%」而不是「还差 x%」，
            # 免得把 0.8 读成「还差 80%」。
            parts.append(
                f"，向「{self.next_stage_label}」已走 {max(0.0, min(1.0, self.progress)) * 100:.0f}%"
            )
        text = "".join(parts) + "）"
        hint = stage_hint(self.stage)
        return f"{text}——{hint}" if hint else text

    def initiative_line(self) -> str:
        return stage_initiative(self.stage)

    def mood_line(self) -> str:
        """给提示词用的「此刻心情」一行；中性时不返回（避免无意义噪声）。"""
        if not self.mood_enabled or self.mood_neutral or not self.mood_description:
            return ""
        axes = self.axes or {}
        detail = "、".join(
            f"{name}{int(axes.get(key) or 0)}"
            for key, name in (
                ("joy", "愉悦"),
                ("anger", "烦躁"),
                ("sorrow", "低落"),
                ("fear", "不安"),
            )
            if int(axes.get(key) or 0) > 1
        )
        return f"{self.mood_description}（{detail}，10 分制）" if detail else self.mood_description


# --------------------------------------------------------------------------- #
# 好感度：取 API / 读快照 / 写事件
# --------------------------------------------------------------------------- #
def get_affinity_api(context: Any) -> Any | None:
    """取好感度插件的跨插件 API；取不到一律返回 None（调用方按「没有」降级）。

    四条失败路径都要分开对待：未安装 / 装了但停用 / 版本不兼容 / 宿主接口变动。
    任何一种都不该抛异常打断主动消息。
    """
    getter = getattr(context, "get_registered_star", None)
    if not callable(getter):
        return None
    try:
        star = getter(AFFINITY_PLUGIN)
    except Exception as exc:  # noqa: BLE001
        logger.debug("mine_chat: 读取好感度插件注册信息失败: %s", exc)
        return None
    if star is None:
        return None
    if not getattr(star, "activated", False):
        return None
    api = getattr(getattr(star, "star_cls", None), "api", None)
    if api is None:
        return None
    if str(getattr(api, "API_VERSION", "")) != AFFINITY_API_VERSION:
        logger.debug(
            "mine_chat: 好感度插件 API 版本不匹配（%s != %s），跳过联动",
            getattr(api, "API_VERSION", ""),
            AFFINITY_API_VERSION,
        )
        return None
    return api


def umo_user_id(umo: str) -> str:
    """从 umo 里取对方 id（私聊时就是 QQ 号；群聊取群号，仅作事件归因）。"""
    raw = str(umo or "").strip()
    if not raw:
        return ""
    return raw.split(":")[-1].strip()


async def affinity_snapshot(
    context: Any, persona_id: str, user_id: str
) -> AffinitySnapshot | None:
    """读一次关系快照 + 情绪四轴；任一环节不可用返回 None。

    注意 `get()` 在「还没建档」时返回 status="not_recorded"——那不是 0 分，
    而是「没有这段关系的数据」，此时不要合成一段「陌生」的提示词去干扰模型，
    直接返回 None 让人设自己判断。
    """
    api = get_affinity_api(context)
    if api is None or not persona_id:
        return None
    try:
        role_id = str(api.role_for(persona_id) or "")
    except Exception as exc:  # noqa: BLE001
        logger.debug("mine_chat: 好感度 role_for 失败: %s", exc)
        return None
    if not role_id:
        return None

    score = peak = 0.0
    stage = stage_label = next_stage_label = ""
    progress = 0.0
    if user_id:
        try:
            snap = await api.get(role_id, str(user_id))
        except Exception as exc:  # noqa: BLE001
            logger.debug("mine_chat: 好感度 get 失败: %s", exc)
            snap = None
        if isinstance(snap, dict) and snap.get("status") == "ok":
            score = float(snap.get("score") or 0.0)
            peak = float(snap.get("peak") or 0.0)
            stage = str(snap.get("stage") or "")
            stage_label = str(snap.get("stage_label") or "")
            next_stage_label = str(snap.get("next_stage_label") or "")
            progress = float(snap.get("progress") or 0.0)
        elif isinstance(snap, dict) and snap.get("status") == "not_recorded":
            # 没有这段关系的数据：不构造快照。情绪（按角色）可能仍有值，
            # 但没分数就没有「这是谁」的上下文，一起跳过更安全。
            return None

    mood_description = ""
    axes: dict[str, int] = {}
    neutral = True
    mood_enabled = False
    try:
        mood = await api.get_mood(role_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("mine_chat: 好感度 get_mood 失败: %s", exc)
        mood = None
    if isinstance(mood, dict):
        mood_enabled = bool(mood.get("enabled"))
        neutral = bool(mood.get("neutral", True))
        mood_description = str(mood.get("description") or "")
        raw_axes = mood.get("axes")
        if isinstance(raw_axes, dict):
            axes = {
                str(key): int(value)
                for key, value in raw_axes.items()
                if isinstance(value, (int, float))
            }

    return AffinitySnapshot(
        role_id=role_id,
        user_id=str(user_id or ""),
        score=score,
        peak=peak,
        stage=stage,
        stage_label=stage_label,
        next_stage_label=next_stage_label,
        progress=progress,
        mood_description=mood_description,
        axes=axes,
        mood_neutral=neutral,
        mood_enabled=mood_enabled,
    )


async def affinity_add_event(
    context: Any,
    persona_id: str,
    user_id: str,
    *,
    code: str,
    delta: float,
    dedupe_key: str,
    evidence: str = "",
    session_id: str = "",
) -> bool:
    """写一条好感度事件（可选能力，默认配置不开）。

    用 source="mine_chat" 而不是 "core"：不占对方的每日增减额度——
    主动消息的分数不该抢走用户真实互动的额度。
    """
    api = get_affinity_api(context)
    if api is None or not persona_id or not user_id:
        return False
    try:
        role_id = str(api.role_for(persona_id) or "")
        if not role_id:
            return False
        result = await api.add_event(
            role_id,
            str(user_id),
            source="mine_chat",
            code=code,
            delta=float(delta),
            dedupe_key=dedupe_key,
            evidence=evidence[:200],
            persona_id=persona_id,
            session_id=session_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("mine_chat: 写好感度事件失败: %s", exc)
        return False
    return isinstance(result, dict) and result.get("status") == "ok"


# --------------------------------------------------------------------------- #
# 语音：文本清洗（纯函数）+ 合成 + 发送
# --------------------------------------------------------------------------- #
_BRACKET_RE = re.compile(r"[（(【\[][^）)】\]]{0,80}[）)】\]]")
_MARKUP_RE = re.compile(r"[*_`#>~|]+")
_ELLIPSIS_RE = re.compile(r"[…\.]{2,}")
_KAOMOJI_RE = re.compile(r"[（(][^）)]{0,12}[）)]")


def clean_tts_text(text: Any) -> str:
    """把一条聊天文案清洗成适合朗读的文本。

    为什么必须做：这个插件的人格（尤其在关系较深时）会用括号写动作/神态，
    还有颜文字、分隔符 `---`、省略号堆叠。TTS 会把这些原样念出来——
    「左括号轻轻别过脸右括号」这种台词是灾难。朗读版只保留「说出口的字」。
    """
    raw = str(text or "")
    if not raw.strip():
        return ""
    # 分隔线（分段用的 ---）与 markdown 标记
    raw = raw.replace("---", "。")
    raw = _MARKUP_RE.sub("", raw)
    # 括号内的动作/神态/心理（含全角与半角），连同颜文字
    raw = _BRACKET_RE.sub("", raw)
    raw = _KAOMOJI_RE.sub("", raw)
    # 换行变句号，省略号收成一个
    raw = re.sub(r"\s*\n+\s*", "。", raw)
    raw = _ELLIPSIS_RE.sub("……", raw)
    # 连续标点收拾一下
    raw = re.sub(r"([，。！？；：、])\1+", r"\1", raw)
    raw = re.sub(r"^[，。！？；：、\s]+", "", raw)
    raw = re.sub(r"[，、；：]+$", "。", raw)
    cleaned = " ".join(raw.split())
    # 只剩标点/符号（例如整条都是表情）时视为无内容
    if not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", cleaned):
        return ""
    return cleaned[:220]


def get_voice_api(context: Any) -> Any | None:
    """取萌萌语音的 tts.v1 契约对象（star_cls.api）；不可用返回 None。

    与好感度 affinity.v1 同范式：只认契约对象，不碰对方任何内部属性，
    所以对方内部重构（engine 改名、router 改造）都不会波及这里。
    """
    getter = getattr(context, "get_registered_star", None)
    if not callable(getter):
        return None
    try:
        star = getter(VOICE_PLUGIN)
    except Exception as exc:  # noqa: BLE001
        logger.debug("mine_chat: 读取语音插件注册信息失败: %s", exc)
        return None
    if star is None or not getattr(star, "activated", False):
        return None
    api = getattr(getattr(star, "star_cls", None), "api", None)
    if api is None:
        # 老版本 moe_tts 没有对外契约——静默降级，别让用户以为插件坏了
        logger.debug("mine_chat: 语音插件未提供 tts.v1 契约，跳过语音联动")
        return None
    if getattr(api, "API_VERSION", None) != VOICE_API_VERSION:
        logger.debug("mine_chat: 语音契约版本不符（需要 %s），跳过", VOICE_API_VERSION)
        return None
    return api


# 兼容旧名字：历史代码/外部若仍引用 get_voice_plugin，这里保留别名。
get_voice_plugin = get_voice_api


def voice_available(context: Any) -> bool:
    """语音通道是否可用（装了 / 启用 / 配了服务端 / 有音色 / 没在冷却期）。"""
    api = get_voice_api(context)
    if api is None:
        return False
    try:
        return bool(api.available())
    except Exception:  # noqa: BLE001 - 契约调用异常一律当作不可用
        return False


async def synthesize(context: Any, text: str, voice: str = "") -> list[str]:
    """文本 → 本地 wav 路径列表（可能多段）；任何失败返回空列表。

    走 tts.v1 契约，零依赖对方内部实现：
    - `pre_translated=True`：文案已经定稿，不许翻译器改写（外语种音色下会被翻）；
    - 契约保证失败只回 `ok=False` 不抛异常，这里仍加 wait_for 兜底超时；
    - 产物落在对方临时目录，用完由 `schedule_cleanup` 延迟清理。
    """
    cleaned = clean_tts_text(text)
    if not cleaned:
        return []
    api = get_voice_api(context)
    if api is None:
        return []
    try:
        res = await asyncio.wait_for(
            api.synthesize(cleaned, str(voice or "").strip(), pre_translated=True),
            timeout=VOICE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("mine_chat: 语音合成超时（>%.0fs），本次不发语音", VOICE_TIMEOUT)
        return []
    except Exception as exc:  # noqa: BLE001 - 契约异常也要兜住
        logger.warning("mine_chat: 语音合成异常: %s", exc)
        return []
    if not isinstance(res, dict) or not res.get("ok"):
        error = str(res.get("error", "")) if isinstance(res, dict) else "契约返回异常"
        logger.info("mine_chat: 语音合成未成功（%s），本次不发语音", error or "未知原因")
        return []
    paths = [str(p) for p in (res.get("files") or []) if p]
    paths = [p for p in paths if os.path.isfile(p)]
    if not paths:
        logger.info("mine_chat: 语音产物为空或不存在，本次不发语音")
        return []
    return paths


def schedule_cleanup(path: str, context: Any = None) -> None:
    """延迟删除合成产物：给平台读完文件留时间。

    优先走 tts.v1 契约的 cleanup()（由对方按其清理队列管理）；拿不到契约
    就起一个延迟任务自己删——wav 落在系统临时目录，不删会一直堆积。
    """
    target = str(path or "").strip()
    if not target:
        return
    if context is not None:
        api = get_voice_api(context)
        if api is not None:
            try:
                api.cleanup(target, VOICE_CLEANUP_DELAY)
                return
            except Exception:  # noqa: BLE001 - 契约清理失败就自己删
                pass

    async def _later() -> None:
        await asyncio.sleep(VOICE_CLEANUP_DELAY)
        try:
            if os.path.isfile(target):
                os.remove(target)
        except OSError:
            pass

    try:
        asyncio.create_task(_later())
    except RuntimeError:  # 没有事件循环（测试环境）
        pass


def _message_chain():
    """MessageChain 的兼容导入（各版本位置不同）。"""
    try:
        from astrbot.api.all import MessageChain  # type: ignore

        return MessageChain
    except Exception:  # noqa: BLE001
        pass
    try:
        from astrbot.core.message.message_event_result import MessageChain  # type: ignore

        return MessageChain
    except Exception:  # noqa: BLE001
        return None


async def speak(
    context: Any, umo: str, text: str, voice: str = ""
) -> bool:
    """合成并发送一条（或多段）语音消息；任何一环失败返回 False（调用方降级为文字）。

    tts.v1 可能一次返回多段 wav（引擎按换行/长度切分），这里逐段各发一条 Record。
    任一段发送失败即视为未完全送达 → 返回 False，调用方会补发文字兜底；已发的
    语音不撤回（真人也可能先发语音再补一句文字）。
    """
    if not umo:
        return False
    paths = await synthesize(context, text, voice)
    if not paths:
        return False
    chain_cls = _message_chain()
    if chain_cls is None:
        logger.warning("mine_chat: 找不到 MessageChain，无法发送语音")
        for p in paths:
            schedule_cleanup(p, context)
        return False
    try:
        from astrbot.api.message_components import Record  # type: ignore
    except Exception as exc:  # noqa: BLE001
        logger.warning("mine_chat: 语音组件不可用: %s", exc)
        for p in paths:
            schedule_cleanup(p, context)
        return False
    all_sent = True
    for path in paths:
        try:
            result = await context.send_message(
                umo, chain_cls([Record(file=path, url=path)])
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("mine_chat: 语音发送异常: %s", exc)
            result = False
        # 无论成败都登记清理：失败时文件同样没人用了
        schedule_cleanup(path, context)
        if result is False:
            logger.warning("mine_chat: 语音未送达（未找到匹配平台）umo=%s", umo)
            all_sent = False
    return all_sent
