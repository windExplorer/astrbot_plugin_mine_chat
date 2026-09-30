"""配置读取与默认值归一。

配置结构（v1.0.4 起与 `_conf_schema.json` 一一对应，均为嵌套分组）：
`persona` / `inject` / `schedule` / `proactive` / `prompt` / `advanced`，
顶层仅 `enabled` 总开关。

本模块只做类型归一与取值范围收敛，避免配置被手工改坏后（字符串塞进 int、
空值塞进 list）插件直接崩；同时兼容 v1.0.x 的平铺键并在启动时自动迁移。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

_TIME_RE = re.compile(r"^\s*(\d{1,2})\s*[:：]\s*(\d{1,2})")

_logger = logging.getLogger("mine_chat.config")
_key_divergence_warned: set[str] = set()


def _warn_key_divergence(legacy_key: str, legacy: Any, nested_key: str, nested: Any) -> None:
    """平铺键与嵌套键并存且值不一致时告警（同一键对只告警一次，防每条消息刷屏）。"""
    pair = f"{legacy_key}|{nested_key}"
    if pair in _key_divergence_warned:
        return
    _key_divergence_warned.add(pair)
    _logger.warning(
        "mine_chat: 配置键 %s=%r 与嵌套 %s=%r 不一致，实际生效的是嵌套值——"
        "请到插件配置页核对（多半是手改配置文件时改到了顶层旧键）",
        legacy_key,
        legacy,
        nested_key,
        nested,
    )


def to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y", "是"}
    return default


def to_int(
    value: Any, default: int = 0, minimum: int | None = None, maximum: int | None = None
) -> int:
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def to_float(
    value: Any,
    default: float = 0.0,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def to_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return "\n".join(str(item).strip() for item in value if str(item).strip())
    return str(value).strip()


def to_list(value: Any, default: list[str] | None = None) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, tuple):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        parts = re.split(r"[,，\s]+", value.strip())
        return [part for part in parts if part]
    return list(default or [])


def parse_hhmm(value: Any, fallback: str = "00:00") -> int:
    """把 'HH:MM' 解析成当天分钟数 0..1439。解析失败时用 fallback。"""
    for candidate in (value, fallback):
        if candidate is None:
            continue
        match = _TIME_RE.match(str(candidate))
        if not match:
            continue
        hour = int(match.group(1))
        minute = int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour * 60 + minute
        if hour == 24 and minute == 0:
            return 0
    return 0


def fmt_hhmm(minutes: int) -> str:
    """分钟数 -> 'HH:MM'（自动对 24 小时取模）。"""
    minutes = int(minutes) % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def in_time_window(now_minute: int, start_minute: int, end_minute: int) -> bool:
    """判断当前分钟是否落在 [start, end) 内，支持跨天（start > end）。"""
    now_minute %= 24 * 60
    start_minute %= 24 * 60
    end_minute %= 24 * 60
    if start_minute == end_minute:
        return False
    if start_minute < end_minute:
        return start_minute <= now_minute < end_minute
    return now_minute >= start_minute or now_minute < end_minute


def _group_of(raw: Any, name: str) -> dict[str, Any]:
    """取配置里的嵌套分组；缺失或不是 dict 时返回空 dict。"""
    value = raw.get(name) if hasattr(raw, "get") else None
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class Settings:
    """一次读取得到的配置快照（不可变，便于跨协程传递）。

    属性名是插件的内部稳定接口；配置结构重组只影响 `from_config`
    的取值路径，不影响下游模块。
    """

    # 基础
    enabled: bool = True
    active_persona: str = ""
    primary_umo: str = ""
    window_auto_bind: bool = False

    # 注入
    inject_scopes: tuple[str, ...] = ("private", "group")
    inject_enabled: bool = True
    inject_mode: str = "tail"
    inject_max_chars: int = 600
    inject_lookback_minutes: int = 90
    inject_lookahead_minutes: int = 180
    inject_include_seed: bool = True

    # 日程
    schedule_enabled: bool = True
    schedule_time_min: int = 7 * 60 + 30
    schedule_item_min: int = 6
    schedule_item_max: int = 14
    schedule_sleep_start_min: int = 23 * 60 + 30
    schedule_sleep_end_min: int = 7 * 60 + 30
    schedule_allow_night_owl: bool = False
    schedule_avoid_days: int = 3
    schedule_max_retry: int = 2
    schedule_style: str = ""
    schedule_world: str = ""
    schedule_character: str = ""
    schedule_forbidden: list[str] = field(default_factory=list)
    schedule_model: str = ""
    schedule_temperature: float = 1.0

    # 主动
    proactive_enabled: bool = True
    proactive_interval_seconds: int = 60
    proactive_daily_limit: int = 6
    proactive_min_interval_minutes: int = 45
    proactive_max_unanswered: int = 4
    proactive_quiet_start_min: int = 23 * 60
    proactive_quiet_end_min: int = 8 * 60 + 30
    proactive_user_active_cooldown_minutes: int = 10
    proactive_skip_sleeping: bool = True
    proactive_seed_window_minutes: int = 90
    proactive_jitter_minutes: int = 20
    proactive_max_segments: int = 3
    proactive_segment_delay: float = 1.5
    proactive_history_messages: int = 10
    proactive_model: str = ""
    proactive_extra_instruction: str = ""

    # 主动消息生图（本地图库 / ComfyUI萌绘联动）——「我此刻生活的画面」
    proactive_image_enabled: bool = False
    proactive_image_backend: str = "local"
    proactive_image_dir: str = ""
    proactive_image_workflow: str = ""
    proactive_image_prompt_style: str = "natural"
    # 角色锚点：画面出镜角色本人时注入的外貌描述（动/真两套；空则尝试知识库）
    proactive_image_anchor_anime: str = ""
    proactive_image_anchor_realistic: str = ""
    proactive_image_prompt_language: str = "zh"
    proactive_image_art_style: str = "anime"
    proactive_image_negative_prompt: str = ""
    proactive_image_probability: float = 0.25
    proactive_image_max_per_day: int = 2

    # 主动消息表情包（萌萌表情包联动）——「情绪反应贴图」，与生图独立计数
    proactive_sticker_enabled: bool = False
    proactive_sticker_token: str = ""
    proactive_sticker_probability: float = 0.15
    proactive_sticker_max_per_day: int = 2

    # 提示词覆盖
    prompt_plan_override: str = ""
    prompt_proactive_override: str = ""

    # 其他
    log_retention: int = 2000

    @classmethod
    def from_config(cls, raw: Any) -> Settings:
        """读取配置（嵌套分组优先，v1.0.x 平铺键兜底）。"""
        get = raw.get if hasattr(raw, "get") else (lambda key, default=None: default)
        persona = _group_of(raw, "persona")
        inject = _group_of(raw, "inject")
        schedule = _group_of(raw, "schedule")
        proactive = _group_of(raw, "proactive")
        prompt = _group_of(raw, "prompt")
        advanced = _group_of(raw, "advanced")

        def pick(
            group: dict[str, Any], group_key: str, legacy_key: str, default: Any
        ) -> Any:
            """优先取嵌套组内键；为 None 时回退 v1.0.x 的平铺键。"""
            value = group.get(group_key)
            if value is not None:
                legacy = get(legacy_key)
                if legacy is not None and legacy != value:
                    # 只在控制台/AstrBot 配置页之外的途径（如手改配置文件）改到
                    # 平铺键时会出现：两处并存且不一致，实际生效的是嵌套值，
                    # 不提示的话用户会以为自己的修改没保存。
                    _warn_key_divergence(legacy_key, legacy, group_key, value)
                return value
            value = get(legacy_key)
            return default if value is None else value

        item_min = to_int(pick(schedule, "item_min", "schedule_item_min", 6), 6, 2, 24)
        item_max = to_int(pick(schedule, "item_max", "schedule_item_max", 14), 14, 2, 48)
        if item_max < item_min:
            item_max = item_min

        scopes = to_list(
            pick(inject, "scopes", "inject_scopes", ["private", "group"]),
            ["private", "group"],
        )
        scopes = [scope for scope in scopes if scope in {"private", "group"}]
        if not scopes:
            scopes = ["private", "group"]

        mode = to_str(pick(inject, "mode", "inject_mode", "tail"), "tail")
        if mode not in {"tail", "system"}:
            mode = "tail"

        forbidden_raw = to_str(pick(schedule, "forbidden", "schedule_forbidden", ""))
        forbidden = [line.strip(" -·\t") for line in forbidden_raw.splitlines() if line.strip()]

        return cls(
            enabled=to_bool(get("enabled", True), True),
            active_persona=to_str(pick(persona, "active", "active_persona", ""))
            or to_str(get("persona_override", "")),
            primary_umo=to_str(pick(persona, "primary_umo", "primary_umo", "")),
            window_auto_bind=to_bool(
                pick(persona, "auto_bind", "window_auto_bind", False), False
            ),
            inject_scopes=tuple(scopes),
            inject_enabled=to_bool(pick(inject, "enabled", "inject_enabled", True), True),
            inject_mode=mode,
            inject_max_chars=to_int(
                pick(inject, "max_chars", "inject_max_chars", 600), 600, 120, 4000
            ),
            inject_lookback_minutes=to_int(
                pick(inject, "lookback_minutes", "inject_lookback_minutes", 90), 90, 0, 720
            ),
            inject_lookahead_minutes=to_int(
                pick(inject, "lookahead_minutes", "inject_lookahead_minutes", 180), 180, 0, 1440
            ),
            inject_include_seed=to_bool(
                pick(inject, "include_seed", "inject_include_seed", True), True
            ),
            schedule_enabled=to_bool(pick(schedule, "enabled", "schedule_enabled", True), True),
            schedule_time_min=parse_hhmm(
                pick(schedule, "time", "schedule_time", "07:30"), "07:30"
            ),
            schedule_item_min=item_min,
            schedule_item_max=item_max,
            schedule_sleep_start_min=parse_hhmm(
                pick(schedule, "sleep_start", "schedule_sleep_start", "23:30"), "23:30"
            ),
            schedule_sleep_end_min=parse_hhmm(
                pick(schedule, "sleep_end", "schedule_sleep_end", "07:30"), "07:30"
            ),
            schedule_allow_night_owl=to_bool(
                pick(schedule, "night_owl", "schedule_allow_night_owl", False), False
            ),
            schedule_avoid_days=to_int(
                pick(schedule, "avoid_days", "schedule_avoid_days", 3), 3, 0, 14
            ),
            schedule_max_retry=to_int(
                pick(schedule, "max_retry", "schedule_max_retry", 2), 2, 0, 5
            ),
            schedule_style=to_str(
                pick(
                    schedule,
                    "style",
                    "schedule_style",
                    "日常向：以真实、细碎、有呼吸感的生活节奏为主，避免戏剧化和过分精彩的一天。",
                )
            ),
            schedule_world=to_str(pick(schedule, "world", "schedule_world", "")),
            schedule_character=to_str(pick(schedule, "character", "schedule_character", "")),
            schedule_forbidden=forbidden,
            schedule_model=to_str(pick(schedule, "model", "schedule_model", "")),
            schedule_temperature=to_float(
                pick(schedule, "temperature", "schedule_temperature", 1.0), 1.0, 0.0, 2.0
            ),
            proactive_enabled=to_bool(
                pick(proactive, "enabled", "proactive_enabled", True), True
            ),
            proactive_interval_seconds=to_int(
                pick(proactive, "interval_seconds", "proactive_interval_seconds", 60),
                60,
                30,
                3600,
            ),
            proactive_daily_limit=to_int(
                pick(proactive, "daily_limit", "proactive_daily_limit", 6), 6, 0, 100
            ),
            proactive_min_interval_minutes=to_int(
                pick(proactive, "min_interval_minutes", "proactive_min_interval_minutes", 45),
                45,
                0,
                1440,
            ),
            proactive_max_unanswered=to_int(
                pick(proactive, "max_unanswered", "proactive_max_unanswered", 4), 4, 0, 100
            ),
            proactive_quiet_start_min=parse_hhmm(
                pick(proactive, "quiet_start", "proactive_quiet_start", "23:00"), "23:00"
            ),
            proactive_quiet_end_min=parse_hhmm(
                pick(proactive, "quiet_end", "proactive_quiet_end", "08:30"), "08:30"
            ),
            proactive_user_active_cooldown_minutes=to_int(
                pick(
                    proactive,
                    "user_active_cooldown",
                    "proactive_user_active_cooldown_minutes",
                    10,
                ),
                10,
                0,
                720,
            ),
            proactive_skip_sleeping=to_bool(
                pick(proactive, "skip_sleeping", "proactive_skip_sleeping", True), True
            ),
            proactive_seed_window_minutes=to_int(
                pick(proactive, "seed_window", "proactive_seed_window_minutes", 90), 90, 5, 720
            ),
            proactive_jitter_minutes=to_int(
                pick(proactive, "jitter", "proactive_jitter_minutes", 20), 20, 0, 180
            ),
            proactive_max_segments=to_int(
                pick(proactive, "max_segments", "proactive_max_segments", 3), 3, 1, 5
            ),
            proactive_segment_delay=to_float(
                pick(proactive, "segment_delay", "proactive_segment_delay_seconds", 1.5),
                1.5,
                0.0,
                10.0,
            ),
            proactive_history_messages=to_int(
                pick(proactive, "history_messages", "proactive_history_messages", 10),
                10,
                0,
                50,
            ),
            proactive_model=to_str(pick(proactive, "model", "proactive_model", "")),
            proactive_extra_instruction=to_str(
                pick(proactive, "extra_instruction", "proactive_extra_instruction", "")
            ),
            proactive_image_enabled=to_bool(
                pick(proactive, "image_enabled", "proactive_image_enabled", False), False
            ),
            proactive_image_backend=to_str(
                pick(proactive, "image_backend", "proactive_image_backend", "local"), "local"
            ),
            proactive_image_dir=to_str(pick(proactive, "image_dir", "proactive_image_dir", "")),
            proactive_image_workflow=to_str(
                pick(proactive, "image_workflow", "proactive_image_workflow", "")
            ),
            proactive_image_prompt_style=to_str(
                pick(
                    proactive,
                    "image_prompt_style",
                    "proactive_image_prompt_style",
                    "natural",
                ),
                "natural",
            ),
            proactive_image_prompt_language=to_str(
                pick(
                    proactive,
                    "image_prompt_language",
                    "proactive_image_prompt_language",
                    "zh",
                ),
                "zh",
            ),
            proactive_image_art_style=to_str(
                pick(
                    proactive,
                    "image_art_style",
                    "proactive_image_art_style",
                    "anime",
                ),
                "anime",
            ),
            proactive_image_anchor_anime=to_str(
                pick(proactive, "image_anchor_anime", "proactive_image_anchor_anime", "")
            ),
            proactive_image_anchor_realistic=to_str(
                pick(
                    proactive,
                    "image_anchor_realistic",
                    "proactive_image_anchor_realistic",
                    "",
                )
            ),
            proactive_image_negative_prompt=to_str(
                pick(
                    proactive,
                    "image_negative_prompt",
                    "proactive_image_negative_prompt",
                    "",
                )
            ),
            proactive_image_probability=to_float(
                pick(proactive, "image_probability", "proactive_image_probability", 0.25),
                0.25,
                0.0,
                1.0,
            ),
            proactive_image_max_per_day=to_int(
                pick(proactive, "image_max_per_day", "proactive_image_max_per_day", 2),
                2,
                0,
                50,
            ),
            proactive_sticker_enabled=to_bool(
                pick(proactive, "sticker_enabled", "proactive_sticker_enabled", False),
                False,
            ),
            proactive_sticker_token=to_str(
                pick(proactive, "sticker_token", "proactive_sticker_token", "")
                or get("proactive_meme_token")
            ),
            proactive_sticker_probability=to_float(
                pick(proactive, "sticker_probability", "proactive_sticker_probability", 0.15),
                0.15,
                0.0,
                1.0,
            ),
            proactive_sticker_max_per_day=to_int(
                pick(proactive, "sticker_max_per_day", "proactive_sticker_max_per_day", 2),
                2,
                0,
                50,
            ),
            prompt_plan_override=to_str(pick(prompt, "plan", "prompt_plan_override", "")),
            prompt_proactive_override=to_str(
                pick(prompt, "proactive", "prompt_proactive_override", "")
            ),
            log_retention=to_int(
                pick(advanced, "log_retention", "log_retention", 2000), 2000, 100, 100000
            ),
        )

    def quiet_now(self, now_minute: int) -> bool:
        return in_time_window(
            now_minute, self.proactive_quiet_start_min, self.proactive_quiet_end_min
        )

    def persona_selected(self) -> bool:
        """是否已选择人格（选人格是启用插件的必要条件之一）。"""
        return bool(self.active_persona.strip())


# --------------------------------------------------------------------------- #
# 旧配置迁移
# --------------------------------------------------------------------------- #
# v1.0.x 平铺键 -> 新嵌套结构的映射：(旧键, 新组, 新键)
_LEGACY_KEY_MAP: tuple[tuple[str, str, str], ...] = (
    ("active_persona", "persona", "active"),
    ("persona_override", "persona", "active"),
    ("primary_umo", "persona", "primary_umo"),
    ("window_auto_bind", "persona", "auto_bind"),
    ("inject_enabled", "inject", "enabled"),
    ("inject_mode", "inject", "mode"),
    ("inject_scopes", "inject", "scopes"),
    ("inject_max_chars", "inject", "max_chars"),
    ("inject_lookback_minutes", "inject", "lookback_minutes"),
    ("inject_lookahead_minutes", "inject", "lookahead_minutes"),
    ("inject_include_seed", "inject", "include_seed"),
    ("schedule_enabled", "schedule", "enabled"),
    ("schedule_time", "schedule", "time"),
    ("schedule_sleep_start", "schedule", "sleep_start"),
    ("schedule_sleep_end", "schedule", "sleep_end"),
    ("schedule_allow_night_owl", "schedule", "night_owl"),
    ("schedule_item_min", "schedule", "item_min"),
    ("schedule_item_max", "schedule", "item_max"),
    ("schedule_avoid_days", "schedule", "avoid_days"),
    ("schedule_max_retry", "schedule", "max_retry"),
    ("schedule_model", "schedule", "model"),
    ("schedule_temperature", "schedule", "temperature"),
    ("schedule_style", "schedule", "style"),
    ("schedule_world", "schedule", "world"),
    ("schedule_character", "schedule", "character"),
    ("schedule_forbidden", "schedule", "forbidden"),
    ("proactive_enabled", "proactive", "enabled"),
    ("proactive_interval_seconds", "proactive", "interval_seconds"),
    ("proactive_daily_limit", "proactive", "daily_limit"),
    ("proactive_min_interval_minutes", "proactive", "min_interval_minutes"),
    ("proactive_max_unanswered", "proactive", "max_unanswered"),
    ("proactive_quiet_start", "proactive", "quiet_start"),
    ("proactive_quiet_end", "proactive", "quiet_end"),
    ("proactive_user_active_cooldown_minutes", "proactive", "user_active_cooldown"),
    ("proactive_skip_sleeping", "proactive", "skip_sleeping"),
    ("proactive_seed_window_minutes", "proactive", "seed_window"),
    ("proactive_jitter_minutes", "proactive", "jitter"),
    ("proactive_max_segments", "proactive", "max_segments"),
    ("proactive_segment_delay_seconds", "proactive", "segment_delay"),
    ("proactive_history_messages", "proactive", "history_messages"),
    ("proactive_model", "proactive", "model"),
    ("proactive_extra_instruction", "proactive", "extra_instruction"),
    ("proactive_image_enabled", "proactive", "image_enabled"),
    ("proactive_image_dir", "proactive", "image_dir"),
    ("proactive_image_probability", "proactive", "image_probability"),
    ("proactive_image_max_per_day", "proactive", "image_max_per_day"),
    ("proactive_meme_token", "proactive", "sticker_token"),
    ("prompt_plan_override", "prompt", "plan"),
    ("prompt_proactive_override", "prompt", "proactive"),
    ("log_retention", "advanced", "log_retention"),
)


def migrate_legacy_config(config: Any) -> bool:
    """把 v1.0.x 的平铺配置键搬进嵌套分组（幂等）。

    只有新键尚未设置时才采用旧值（用户在控制台的最新改动优先）。
    搬完删掉旧键并落盘。返回是否发生了变更。
    """
    if config is None or not hasattr(config, "get"):
        return False
    changed = False
    for legacy_key, group_name, new_key in _LEGACY_KEY_MAP:
        if legacy_key not in config:
            continue
        legacy_value = config.get(legacy_key)
        bucket = config.get(group_name)
        if not isinstance(bucket, dict):
            bucket = {}
        if bucket.get(new_key) is None and legacy_value is not None:
            bucket[new_key] = legacy_value
            config[group_name] = bucket
            changed = True
        config.pop(legacy_key, None)
        changed = True
    if changed and hasattr(config, "save_config"):
        try:
            config.save_config()
        except Exception:  # noqa: BLE001
            return False
    return changed


def save_value(config: Any, key: str, value: Any) -> bool:
    """写回单个顶层配置键并落盘。返回是否成功。"""
    if config is None or not hasattr(config, "save_config"):
        return False
    try:
        config[key] = value
        config.save_config()
        return True
    except Exception:  # noqa: BLE001
        return False
