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
3. 时间覆盖完整：条目必须连续覆盖全天 00:00-24:00，相邻条目首尾相接、不得留空档；
   入睡时段（含跨午夜，如 time="23:30"、end="07:10" 的「睡觉」）也必须是一条条目。
   条目按时间先后排列，不得重叠、不得倒序、不得出现时间倒流。
4. 每条都要写具体、可感、有画面的事，不要写「休息」「学习」「工作」这类空洞词。
5. 工作日、周末、节假日的节奏必须明显不同；不要每天都一样——尤其是一日三餐，
   不能顿顿重复同一家店、同一种食物（外卖/食堂/某道菜连着吃好几天是最容易被用户抓包的）。
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
【今天的日照边界】日出约 {sunrise}，日落约 {sunset}
（任何条目里关于天色/光线/环境的描写都必须与该条目的实际时段一致：日出之前与日落之后的条目才可写天黑、灯火、星空或月亮；日出之后、日落之前的条目**不得**出现天黑、星空、月亮、灯火陆续亮起；只有日落之后的条目才可写夜幕。跨越日出/日落的条目可以写「天色渐暗」「天慢慢亮起来」的过程，但不能写「已经完全黑了」。）

【作息参考】大约 {sleep_end} 起床，{sleep_start} 前后入睡。{night_owl_hint}
{routine_note}

【日程风格】
{style}

【禁止出现的内容】
{forbidden}

【最近几天已经安排过的活动（今天尽量避免重复）】
{recent}

【最近几天吃过的（今天的早中晚三餐必须换一批，不得与下面重复，也不要连续两天吃同一类；主食与饮品可以不换）】
{recent_foods}

【昨天临近今天的部分（用于衔接，不要重复）】
{yesterday_tail}

【条目数量】请生成 {item_min}~{item_max} 条，最后一条要覆盖到入睡时间之后。
"""


def build_plan_system() -> str:
    return PLAN_SYSTEM


# 日出/日落的月度近似值（以及时段判定、天色硬约束、时段矛盾检查）统一放在
# daylight.py —— 那里是唯一来源，本模块只负责把它给定的文本拼进模板。
def build_plan_user(
    *,
    persona: str,
    world: str,
    character: str,
    date_text: str,
    weekday_text: str,
    calendar_hint: str,
    sunrise_text: str = "",
    sunset_text: str = "",
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
    recent_foods: list[str] | None = None,
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
            "sunrise": sunrise_text or "06:20",
            "sunset": sunset_text or "18:00",
            "sleep_start": sleep_start_text,
            "sleep_end": sleep_end_text,
            "night_owl_hint": night_owl_hint,
            "routine_note": routine_note or "今天的作息与平时基本一致。",
            "style": style or "（无特别偏好）",
            "forbidden": "\n".join(f"- {line}" for line in forbidden) or "（无）",
            "recent": "\n".join(f"- {line}" for line in recent_lines) or "（无历史记录）",
            "recent_foods": "\n".join(f"- {line}" for line in (recent_foods or []))
            or "（无历史记录）",
            "yesterday_tail": "\n".join(f"- {line}" for line in yesterday_tail) or "（无）",
            "item_min": str(item_min),
            "item_max": str(item_max),
        },
    )


def plan_retry_hint(kind: str, detail: str = "") -> str:
    """生成重试时追加的纠偏提示。"""
    hints = {
        "format": "上一次的输出不是合法 JSON，无法解析。请只输出 JSON 对象本身，不要解释、不要代码块。",
        "time": "上一次的输出时间顺序或覆盖有问题（重叠、倒序、出现大于 45 分钟的空档、"
        "或没有连续覆盖全天 00:00-24:00）。请保证条目按时间从小到大排列、首尾相接不留空档，"
        "入睡时段（含跨午夜，time=\"23:30\"/end=\"07:10\" 形式）也必须是一条条目，"
        "第一条从 00:00 附近开始、最后一条到 24:00 附近结束。",
        "repeat": "上一次的输出和最近几天的日程太雷同了。请换一批明显不同的活动，让今天有新鲜感。",
        "diet": "上一次的输出里三餐和最近几天重复了。请把重复的那几顿换成别的："
        "换店、换菜系、换做法都行（在家做饭 / 点别的外卖 / 去没去过的店），"
        "不要再出现上面「最近几天吃过的」里的食物；主食和饮品可以不变。",
        "quality": "上一次的输出条目太少或过于笼统。请写得更具体，并保证条目数量达标。",
        "daylight": "上一次的输出里有条目的天色描写与该条目自己的时间对不上（例如下午的条目写「天黑了」，"
        "或深夜的条目写「阳光」）。请严格按上面的日出/日落边界改写这些条目："
        "日落之前的时段一定是天亮着的，日落之后才是夜里。",
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
6. 你的话必须与【此刻时段】一致：天还亮着就绝不能说「天黑了」「该睡了」，夜里也不能说
   「阳光很好」这类白天的话。天色、光线、睡意都按【此刻时段】与【天色铁律】来写。
7. 上下文要接得上：最近和用户聊到一半就顺着那个话题往下说，不要自顾自另起一摊；
   不要重复你最近主动发过的内容或句式（下面会给你），也不要问已经问过的问题；
   跨天的主动要像「过了那段时间的人」——别把前天的事说成刚刚发生的。
{unanswered_hint}
{extra}
只输出你要发送的内容本身，不要任何解释、不要加引号包裹。
"""

PROACTIVE_USER_TEMPLATE = """【现在的时间】{now}
（这是唯一的真实时间。你说的任何时间/时刻必须与它一致，严禁自己编造别的时间。）
{daylight_fact}
【你此刻的状态】{current_block}
【可以顺口提的小事】{seed}
【你们最近的对话】{recent_chat}
【你最近主动发过的（别再重复这些内容和句式）】{recent_proactive}{image_hint}
"""

UNANSWERED_HINT_TEMPLATE = """
注意：用户已经连续 {unanswered} 次没有回你了。这次要比平时更短、更轻，不要催促，不要追问为什么不回，
也不要表现出委屈或抱怨。
"""

IMAGE_HINT_TEMPLATE = """
【随图说明】**这条消息的最后一条**会随附一张图片（如果你用 --- 分成几条发，
照片是跟在最后一条后面的：说明照片的话必须写在最后一条里，别写在前面几条），
这张图拍到的画面是：「{image_desc}」。
这是你刚随手拍下、准备发给对方看的照片。文字要自然交代这张图的来由——
你为什么拍它、它拍的是什么（例如「刚出门看到天烧起来了，拍了一张」
「关东煮到货了，先给你看一眼」「窗台的绿萝冒了新叶，拍给你看」）
——例子只是参考，照【此刻时段】的天色来写、也照上面这句画面描述来写，
别照抄例子里的时间，更不要写这张图里没有的东西；
让文字和画面说的是同一件事，语气像平时分享照片一样随意，一两句带过即可。
不要写成照片说明文，不要用「给你看张图」「看图」这类生硬说法，
也不要出现与画面矛盾的情节（画面在睡觉却问「吵醒你了？」就是典型错误）。
"""

SLEEPING_SEND_NOTE = """
【真实场景】你此刻本应在睡觉，这条消息是你刚被手机震醒后迷迷糊糊摸出来发的：
- 语气要有没睡醒的迷糊感（半句话、省略号、含糊的口吻都可以），不是清醒地决定「发一条消息」；
- 如果随图说明提到画面里有你，那也只可能是你刚醒来迷糊中拍下的样子（睡眼惺忪、头发乱），
  绝不能是「你仍在熟睡」的画面；
- 不要问「吵醒你了？」这类话——睡着的人是你，不是对方。
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
    last_proactive: str = "",
    daylight_fact: str = "",
    recent_proactive: str = "",
    image_hint: str = "",
) -> str:
    """组装主动消息的用户侧上下文。

    recent_proactive：最近几条主动消息（带「今天 15:20 / 昨天 22:10」这类时间锚），
    跨次连续靠它；没传则退化为 last_proactive（仅最后一条，老调用方兼容）。
    image_hint：随图说明（v1.0.44 起放在用户侧末尾）。放这里而不是系统提示词，
    是因为「最后读到的东西」约束力最强——用户反馈过「文字和图片对不上」，
    而它原先夹在系统提示词里、后面还跟着一大段人格设定。
    """
    return fill(
        PROACTIVE_USER_TEMPLATE,
        {
            "now": now_text,
            "daylight_fact": daylight_fact or "",
            "current_block": current_block or "（暂时没有具体安排）",
            "seed": seed or "（这次没有特别的小事，可以说说此刻的心情或感受）",
            "recent_chat": recent_chat or "（你们最近没有说话）",
            "recent_proactive": (recent_proactive or "").strip()
            or last_proactive
            or "（这是你第一次主动找用户）",
            "image_hint": image_hint or "",
        },
    )


TIME_CORRECTION_TEMPLATE = """
【重写要求】你刚才那条消息与此刻时段矛盾：它出现了「{conflicts}」这类与现在不符的描写/说法。
现在真实的时段是：{phase_name}——{phase_light}。
请重写这条消息，只按此刻的天色、光线与状态来写；不要出现天黑、夜色、星空、月亮、
灯火、睡觉、晚安这类夜晚内容（反过来，夜里也不要写白天），也不要用「还没到晚上」这类
绕圈子的解释。其余要求不变，仍然只输出你要发送的内容本身。"""


IMAGE_TIME_CORRECTION_TEMPLATE = """
【重写要求】你刚才那行提示词与此刻时段矛盾：它出现了「{conflicts}」。
现在是 {phase_name}——{phase_light}，时间氛围只能取这一组方向：{tags}。
请重写这一行提示词，天色与光照严格按此刻时段来，不要出现与之矛盾的时间词/光照词。
其余要求不变，仍然只输出提示词本身，单行。"""


def build_image_time_correction(
    phase_name: str, phase_light: str, lighting_tags: str, conflicts: list[str]
) -> str:
    """出图提示词的时段纠偏块（追加到原指令尾部重写一次）。"""
    return fill(
        IMAGE_TIME_CORRECTION_TEMPLATE,
        {
            "conflicts": "、".join(conflicts) or "与此刻不符的描写",
            "phase_name": phase_name or "此刻",
            "phase_light": phase_light or "与此刻一致的光线",
            "tags": (lighting_tags or "").strip() or "与此刻一致的光照",
        },
    )


def build_time_correction(phase_name: str, phase_light: str, conflicts: list[str]) -> str:
    """时段矛盾后的重写指令（追加到原提示词尾部，缩短比完全重发更省上下文）。"""
    return fill(
        TIME_CORRECTION_TEMPLATE,
        {
            "conflicts": "、".join(conflicts) or "与此刻不符的描写",
            "phase_name": phase_name or "此刻",
            "phase_light": phase_light or "与此刻一致的光线",
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
- 画面的主体与动作必须**严格对应**下面的【此刻生活场景】，不许换成别的事情、
  不许添加没提到的人物/宠物/道具（这段描述还要拿去写配文，画偏了文字就跟不上）
{time_rule}- 只输出这一行标签本身，不要解释、不要句号、不要 markdown

{facts}"""

PROMPT_COMPOSE_NATURAL = """根据下面的生活场景事实，写一段绘图提示词。
要求：
- 用{language_word}写一段连贯的画面描述，包含人物、动作、场景、时间氛围与情绪
- 画风：{art_style_text}
- 画面的主体与动作必须**严格对应**下面的【此刻生活场景】，不许换成别的事情、
  不许添加没提到的人物/宠物/道具（这段描述还要拿去写配文，画偏了文字就跟不上）
{time_rule}- 只输出提示词本身，单行，不要解释、不要引号

{facts}"""

# 时间氛围是提示词里最容易「跟着文学先验跑偏」的一项（下午四点画出夜景），
# 所以单列成一条硬规则，并把允许的方向直接给死。
_TIME_RULE_TAGS = (
    "- 时间氛围必须与下面事实里的【此刻时段】完全一致（这是最关键的一条）；"
    "时间标签只能从这一组里挑 1~2 个贴合画面的：{tags}\n"
)
_TIME_RULE_NATURAL = (
    "- 画面描述里必须说清光照/天色，方向只能取这一组：{tags}"
    "（白天不许写成夜色/星空/睡前氛围，夜里不许写成阳光/白天）\n"
)


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
    now_text: str = "",
    image_kind: str = "",
    anchor_hint: str = "",
    daylight_fact: str = "",
    lighting_tags: str = "",
) -> str | None:
    """生成「出图提示词生成」的 LLM 指令；无可用场景事实时返回 None。

    提示词形态由 style（danbooru 标签 / natural 自然语言）与 language（zh/en）
    决定，画风（anime/realistic）作为硬要素写进指令——参考 moe_star_whisper
    的 D6 语义：动漫工作流配英文标签、真人工作流配中文自然语言。

    now_text / daylight_fact / lighting_tags 三者一起构成「时间一致性」约束：
    时刻是事实，daylight_fact 是代码判定的时段硬约束块，lighting_tags 是允许
    出现的光照标签方向（由 daylight.py 产出）。三者任一为空则不注入对应段落。
    """
    activity = (activity or "").strip()
    facts_lines = [f"【此刻生活场景】{activity or '日常生活的一个随意瞬间'}"]
    if (image_kind or "").strip():
        facts_lines.append(
            f"【本张图的类型】{(image_kind or '').strip()}——画面的主体与构图必须符合该类型"
        )
    if (now_text or "").strip():
        facts_lines.append(
            f"【当前真实时刻】{(now_text or '').strip()}——画面的环境、光线、天空必须与该时刻一致："
            "白天/下午就是明亮的天光，绝不能出现夜色、星空或睡眠氛围；"
            "夜里就是黑暗、夜灯、窗外夜色，绝不能出现阳光或白天的环境"
        )
    if (daylight_fact or "").strip():
        facts_lines.append((daylight_fact or "").strip())
    if (anchor_hint or "").strip():
        facts_lines.append(
            f"【角色锚点】当画面出现角色本人（自拍/出镜）时，必须严格使用以下外貌描述，"
            f"不要自行发挥长相：{(anchor_hint or '').strip()}"
        )
    if (seed or "").strip():
        facts_lines.append(f"【可分享细节】{(seed or '').strip()}")
    if (mood or "").strip():
        facts_lines.append(f"【当前心情】{(mood or '').strip()}")
    facts = "\n".join(facts_lines)

    tags = (lighting_tags or "").strip()
    if (style or "").strip().lower() == "danbooru":
        return fill(
            PROMPT_COMPOSE_TAGS,
            {
                "art_style_text": image_art_text(art_style),
                "quality_tags": image_art_quality_tags(art_style),
                "time_rule": _TIME_RULE_TAGS.format(tags=tags) if tags else "",
                "facts": facts,
            },
        )
    return fill(
        PROMPT_COMPOSE_NATURAL,
        {
            "art_style_text": image_art_text(art_style),
            "language_word": "英文" if (language or "").strip().lower() == "en" else "中文",
            "time_rule": _TIME_RULE_NATURAL.format(tags=tags) if tags else "",
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
