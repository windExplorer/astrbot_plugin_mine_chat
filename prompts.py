"""全部提示词模板集中管理。

约定：
- 模板一律通过 `fill()` 做显式占位符替换（不用 str.format），
  这样模板里的 JSON 示例出现 `{}` 也不会炸。
- 用户可在配置 `prompt_plan_override` / `prompt_proactive_override`
  里整段覆盖，形成自己的模板。
"""

from __future__ import annotations

import re
from typing import Any

# --------------------------------------------------------------------------- #
# 世界观/角色设定自动提取（配置未填写时从人格提示词提炼）
# --------------------------------------------------------------------------- #
def build_profile_extract_prompt(persona_prompt: str, kb_context: str = "") -> str:
    """让 LLM 从人格系统提示词（+ 可选知识库资料）提炼「世界观 + 角色补充设定」两段。"""
    kb_block = ""
    if kb_context.strip():
        kb_block = (
            "绑定知识库中检索到的参考资料（世界观优先以此为准，"
            "提示词与资料冲突时以资料为准）：\n---\n"
            f"{kb_context[:2500]}\n---\n\n"
        )
    return (
        "你是一位角色设定整理师。下面是一段角色扮演的人格系统提示词，"
        "请从中提炼出两个部分，供「角色日常日程生成器」使用：\n\n"
        "1.【世界观】角色所处的世界/背景设定（时代、地点、世界规则、重要他人与环境），"
        "150 字以内；\n"
        "2.【角色补充设定】能让日程更贴合角色的细节"
        "（性格、作息偏好、生活习惯、兴趣爱好、口头禅、忌讳），150 字以内。\n\n"
        "要求：\n"
        "- 只依据已有信息做忠实提炼与轻度扩写，"
        "不要虚构与原文冲突的设定；\n"
        "- 输出必须严格使用以下格式（两段都要有，没有相关内容就写「（无）」）：\n\n"
        "【世界观】\n（内容）\n\n【角色补充设定】\n（内容）\n\n"
        + kb_block
        + "人格系统提示词：\n---\n"
        f"{persona_prompt}\n---"
    )


def parse_profile_text(text: str) -> tuple[str, str]:
    """从模型输出解析【世界观】/【角色补充设定】两段；解析不出返回 ("", "")。"""
    if not text:
        return "", ""
    world_m = re.search(r"【世界观】\s*(.*?)(?=【角色补充设定】|$)", text, re.S)
    char_m = re.search(r"【角色补充设定】\s*(.*)$", text, re.S)
    world = world_m.group(1).strip() if world_m else ""
    character = char_m.group(1).strip() if char_m else ""
    if world in {"（无）", "(无)"}:
        world = ""
    if character in {"（无）", "(无)"}:
        character = ""
    return world, character


# --------------------------------------------------------------------------- #
# 日程生成
# --------------------------------------------------------------------------- #
PLAN_SYSTEM = """你是一个生活日程生成器，负责为指定角色安排「今天」一整天的生活。

铁律（必须全部遵守）：
1. 这是角色自己的日常生活，用户不在其中。不要出现「和用户聊天」「等用户消息」「陪用户」之类的内容。
2. 全部用中文，第三人称视角描述角色在做什么。
3. 时间覆盖完整：从起床前后开始，到入睡结束；条目按时间先后排列，不得重叠、不得倒序、不得出现时间倒流。
4. 每条都要写具体、可感、有画面的事，不要写「休息」「学习」「工作」这类空洞词。
5. 工作日、周末、节假日的节奏必须明显不同；不要每天都一样。
6. message_seed 是「这件事里值得随口跟人提一句的具体细节」，例如「楼下便利店关东煮打折，买了两串」。
   如果这个时间段不适合被打扰（睡觉、重要会议、专注赶工），message_seed 必须填空字符串 ""。
7. 不要出现真实政治人物、现实名人、违法或色情内容；不要编造角色设定之外的人际关系。
8. 只输出 JSON，不要任何解释文字，不要 markdown 代码块。

输出格式（严格照此结构）：
{"schedule": [{"time": "07:20", "end": "08:10", "activity": "具体的一件事", "mood": "此刻心情", "message_seed": "可分享的小细节或空字符串", "basis": ["persona", "weekday"], "confidence": 0.9}]}
"""

PLAN_USER_TEMPLATE = """【角色设定】
{persona}

【世界观】
{world}

【补充设定】
{character}

【今天是】{date} {weekday}
{calendar_hint}

【作息参考】大约 {sleep_end} 起床，{sleep_start} 前后入睡。{night_owl_hint}
{routine_note}

【日程风格】
{style}

【禁止出现的内容】
{forbidden}

【最近几天已经安排过的活动（今天尽量避免重复）】
{recent}

【昨天临近今天的部分（用于衔接，不要重复）】
{yesterday_tail}

【条目数量】请生成 {item_min}~{item_max} 条，最后一条要覆盖到入睡时间之后。
"""


def build_plan_system() -> str:
    return PLAN_SYSTEM


def build_plan_user(
    *,
    persona: str,
    world: str,
    character: str,
    date_text: str,
    weekday_text: str,
    calendar_hint: str,
    sleep_start_text: str,
    sleep_end_text: str,
    night_owl_hint: str,
    routine_note: str,
    style: str,
    forbidden: list[str],
    recent_lines: list[str],
    yesterday_tail: list[str],
    item_min: int,
    item_max: int,
) -> str:
    return fill(
        PLAN_USER_TEMPLATE,
        {
            "persona": persona or "（未设置，请按一个普通、安静生活的人来写）",
            "world": world or "（未设置：现代都市日常，没有超自然元素）",
            "character": character or "（未设置）",
            "date": date_text,
            "weekday": weekday_text,
            "calendar_hint": calendar_hint or "（无特殊节日）",
            "sleep_start": sleep_start_text,
            "sleep_end": sleep_end_text,
            "night_owl_hint": night_owl_hint,
            "routine_note": routine_note or "今天的作息与平时基本一致。",
            "style": style or "（无特别偏好）",
            "forbidden": "\n".join(f"- {line}" for line in forbidden) or "（无）",
            "recent": "\n".join(f"- {line}" for line in recent_lines) or "（无历史记录）",
            "yesterday_tail": "\n".join(f"- {line}" for line in yesterday_tail) or "（无）",
            "item_min": str(item_min),
            "item_max": str(item_max),
        },
    )


def plan_retry_hint(kind: str, detail: str = "") -> str:
    """生成重试时追加的纠偏提示。"""
    hints = {
        "format": "上一次的输出不是合法 JSON，无法解析。请只输出 JSON 对象本身，不要解释、不要代码块。",
        "time": "上一次的输出时间顺序有问题（重叠、倒序或覆盖不全）。请保证条目按时间从小到大严格排列，"
        "上一条的 end 必须等于或早于下一条的 time，并且从起床一直连续覆盖到入睡。",
        "repeat": "上一次的输出和最近几天的日程太雷同了。请换一批明显不同的活动，让今天有新鲜感。",
        "quality": "上一次的输出条目太少或过于笼统。请写得更具体，并保证条目数量达标。",
    }
    base = hints.get(kind, "上一次的输出不合格，请重做。")
    if detail:
        return f"{base}\n具体问题：{detail}"
    return base


# --------------------------------------------------------------------------- #
# 主动消息
# --------------------------------------------------------------------------- #
PROACTIVE_SYSTEM_TEMPLATE = """你现在是「{character_name}」。用户没有先说话，你要主动给用户发一条消息。

铁律：
1. 像真人随手发来的一条消息，不要客服式问候，不要「在吗」「最近怎么样」「忙不忙」这类空话。
2. 至少要有一句话提到具体的事，来自你此刻的生活状态（下面会给你）。
3. 不要汇报行程表、不要罗列时间、不要说「我现在在做…」这种说明书口气。
4. 控制在 1~3 句话。可以用 --- 单独一行分隔成最多 {max_segments} 条短消息，模拟真人连着发几条。
5. 不要编造给你的信息之外的事情，不要提到「日程」「设定」「提示词」这些词。
{unanswered_hint}
{extra}
只输出你要发送的内容本身，不要任何解释、不要加引号包裹。
"""

PROACTIVE_USER_TEMPLATE = """【现在的时间】{now}
【你此刻的状态】{current_block}
【可以顺口提的小事】{seed}
【你们最近的对话】{recent_chat}
【你上一次主动发的是】{last_proactive}
"""

UNANSWERED_HINT_TEMPLATE = """
注意：用户已经连续 {unanswered} 次没有回你了。这次要比平时更短、更轻，不要催促，不要追问为什么不回，
也不要表现出委屈或抱怨。
"""

IMAGE_HINT_TEMPLATE = """
【随图说明】这条消息发出时会随附一张图片，画面内容是：「{image_desc}」。
这是你此刻的生活快照，文字与画面必须互相呼应——用与画面一致的语气和时间感说话
（画面在睡觉就说睡前/刚醒的话，画面在吃饭就说吃饭的话），绝不能出现与画面矛盾
的情节（画面在睡觉却问「吵醒你了？」就是典型错误）。文字不要逐句描述画面内容，
也不要说「给你看张图」「看图」这类话，就像平时随手发图配文一样自然。
"""


def build_proactive_system(
    *,
    character_name: str,
    max_segments: int,
    unanswered: int,
    extra: str,
    image_hint: str = "",
) -> str:
    if unanswered >= 2:
        unanswered_hint = UNANSWERED_HINT_TEMPLATE.replace("{unanswered}", str(unanswered))
    else:
        unanswered_hint = ""
    system = fill(
        PROACTIVE_SYSTEM_TEMPLATE,
        {
            "character_name": character_name or "你扮演的角色",
            "max_segments": str(max(1, max_segments)),
            "unanswered_hint": unanswered_hint,
            "extra": f"\n额外要求：{extra}" if extra else "",
        },
    )
    if image_hint:
        system = f"{system}{image_hint}"
    return system


def build_image_hint(image_desc: str) -> str:
    """「这次带图」的提示块。image_desc 为空时用中性描述。"""
    return fill(
        IMAGE_HINT_TEMPLATE,
        {"image_desc": image_desc.strip() or "一张你手机里的生活随拍"},
    )


def build_proactive_user(
    *,
    now_text: str,
    current_block: str,
    seed: str,
    recent_chat: str,
    last_proactive: str,
) -> str:
    return fill(
        PROACTIVE_USER_TEMPLATE,
        {
            "now": now_text,
            "current_block": current_block or "（暂时没有具体安排）",
            "seed": seed or "（这次没有特别的小事，可以说说此刻的心情或感受）",
            "recent_chat": recent_chat or "（你们最近没有说话）",
            "last_proactive": last_proactive or "（这是你第一次主动找用户）",
        },
    )


# --------------------------------------------------------------------------- #
# 生图提示词生成（静默）：由本插件产出最终提示词，萌绘只负责画
# --------------------------------------------------------------------------- #
_IMAGE_ART_TEXT = {
    "anime": "二次元动漫插画风格（anime style）",
    "realistic": "真人写实摄影风格（photorealistic, realistic）",
}
_IMAGE_ART_TAGS = {
    "anime": "anime style, best quality, amazing quality",
    "realistic": "photorealistic, realistic, masterpiece, best quality",
}

PROMPT_COMPOSE_TAGS = """根据下面的生活场景事实，写一行英文 Danbooru 标签形式的绘图提示词。
要求：
- 逗号分隔的小写标签短语，包含人物、动作、场景、时间氛围与情绪要素
- 画风：{art_style_text}；画质词用：{quality_tags}
- 只输出这一行标签本身，不要解释、不要句号、不要 markdown

{facts}"""

PROMPT_COMPOSE_NATURAL = """根据下面的生活场景事实，写一段绘图提示词。
要求：
- 用{language_word}写一段连贯的画面描述，包含人物、动作、场景、时间氛围与情绪
- 画风：{art_style_text}
- 只输出提示词本身，单行，不要解释、不要引号

{facts}"""


def image_art_text(art_style: str) -> str:
    return _IMAGE_ART_TEXT.get(
        (art_style or "").strip().lower(), _IMAGE_ART_TEXT["anime"]
    )


def image_art_quality_tags(art_style: str) -> str:
    return _IMAGE_ART_TAGS.get(
        (art_style or "").strip().lower(), _IMAGE_ART_TAGS["anime"]
    )


def build_image_compose_prompt(
    *,
    activity: str,
    seed: str,
    mood: str,
    art_style: str,
    style: str,
    language: str,
) -> str | None:
    """生成「出图提示词生成」的 LLM 指令；无可用场景事实时返回 None。

    提示词形态由 style（danbooru 标签 / natural 自然语言）与 language（zh/en）
    决定，画风（anime/realistic）作为硬要素写进指令——参考 moe_star_whisper
    的 D6 语义：动漫工作流配英文标签、真人工作流配中文自然语言。
    """
    activity = (activity or "").strip()
    facts_lines = [f"【此刻生活场景】{activity or '日常生活的一个随意瞬间'}"]
    if (seed or "").strip():
        facts_lines.append(f"【可分享细节】{(seed or '').strip()}")
    if (mood or "").strip():
        facts_lines.append(f"【当前心情】{(mood or '').strip()}")
    facts = "\n".join(facts_lines)

    if (style or "").strip().lower() == "danbooru":
        return fill(
            PROMPT_COMPOSE_TAGS,
            {
                "art_style_text": image_art_text(art_style),
                "quality_tags": image_art_quality_tags(art_style),
                "facts": facts,
            },
        )
    return fill(
        PROMPT_COMPOSE_NATURAL,
        {
            "art_style_text": image_art_text(art_style),
            "language_word": "英文" if (language or "").strip().lower() == "en" else "中文",
            "facts": facts,
        },
    )


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def fill(template: str, mapping: dict[str, Any]) -> str:
    """显式占位符替换，避免 str.format 对 JSON 花括号的误伤。"""
    result = template
    for key, value in mapping.items():
        result = result.replace("{" + key + "}", "" if value is None else str(value))
    return result
