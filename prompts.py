"""全部提示词模板集中管理。

约定：
- 模板一律通过 `fill()` 做显式占位符替换（不用 str.format），
  这样模板里的 JSON 示例出现 `{}` 也不会炸。
- 用户可在配置 `prompt_plan_override` / `prompt_proactive_override`
  里整段覆盖，形成自己的模板。
"""

from __future__ import annotations

from typing import Any

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
【随图说明】这条消息发出时会随附一张图片，图片内容大致是「{image_desc}」。
这是你随手分享的图。文字里不要描述图片内容，也不要说「给你看张图」「看图」这类话，
就像平时发消息一样自然说话即可。
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
# 工具
# --------------------------------------------------------------------------- #
def fill(template: str, mapping: dict[str, Any]) -> str:
    """显式占位符替换，避免 str.format 对 JSON 花括号的误伤。"""
    result = template
    for key, value in mapping.items():
        result = result.replace("{" + key + "}", "" if value is None else str(value))
    return result
