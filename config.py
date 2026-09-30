"""配置读取与默认值归一。

唯一权威默认值在 `_conf_schema.json`；本模块只做类型归一与取值范围收敛，
避免用户把配置页手工改坏之后（字符串塞进 int、空值塞进 list）插件直接崩。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_TIME_RE = re.compile(r"^\s*(\d{1,2})\s*[:：]\s*(\d{1,2})")


# --------------------------------------------------------------------------- #
# 基础类型归一
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# 时间解析
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# 配置快照
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Settings:
    """一次读取得到的配置快照（不可变，便于跨协程传递）。"""

    # 基础
    enabled: bool = True
    persona_override: str = ""
    primary_umo: str = ""
    window_auto_bind: bool = True

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

    # 主动消息配图（本地图库模式）
    proactive_image_enabled: bool = False
    proactive_image_dir: str = ""
    proactive_image_probability: float = 0.25
    proactive_image_max_per_day: int = 2

    # 提示词覆盖
    prompt_plan_override: str = ""
    prompt_proactive_override: str = ""

    # 其他
    log_retention: int = 2000

    # ------------------------------------------------------------------ #
    @classmethod
    def from_config(cls, raw: Any) -> Settings:
        get = raw.get if hasattr(raw, "get") else (lambda key, default=None: default)

        item_min = to_int(get("schedule_item_min", 6), 6, 2, 24)
        item_max = to_int(get("schedule_item_max", 14), 14, 2, 48)
        if item_max < item_min:
            item_max = item_min

        scopes = to_list(get("inject_scopes", ["private", "group"]), ["private", "group"])
        scopes = [scope for scope in scopes if scope in {"private", "group"}]
        if not scopes:
            scopes = ["private", "group"]

        mode = to_str(get("inject_mode", "tail"), "tail")
        if mode not in {"tail", "system"}:
            mode = "tail"

        forbidden_raw = to_str(get("schedule_forbidden", ""))
        forbidden = [line.strip(" -·\t") for line in forbidden_raw.splitlines() if line.strip()]

        return cls(
            enabled=to_bool(get("enabled", True), True),
            persona_override=to_str(get("persona_override", "")),
            primary_umo=to_str(get("primary_umo", "")),
            window_auto_bind=to_bool(get("window_auto_bind", True), True),
            inject_scopes=tuple(scopes),
            inject_enabled=to_bool(get("inject_enabled", True), True),
            inject_mode=mode,
            inject_max_chars=to_int(get("inject_max_chars", 600), 600, 120, 4000),
            inject_lookback_minutes=to_int(get("inject_lookback_minutes", 90), 90, 0, 720),
            inject_lookahead_minutes=to_int(get("inject_lookahead_minutes", 180), 180, 0, 1440),
            inject_include_seed=to_bool(get("inject_include_seed", True), True),
            schedule_enabled=to_bool(get("schedule_enabled", True), True),
            schedule_time_min=parse_hhmm(get("schedule_time", "07:30"), "07:30"),
            schedule_item_min=item_min,
            schedule_item_max=item_max,
            schedule_sleep_start_min=parse_hhmm(get("schedule_sleep_start", "23:30"), "23:30"),
            schedule_sleep_end_min=parse_hhmm(get("schedule_sleep_end", "07:30"), "07:30"),
            schedule_allow_night_owl=to_bool(get("schedule_allow_night_owl", False), False),
            schedule_avoid_days=to_int(get("schedule_avoid_days", 3), 3, 0, 14),
            schedule_max_retry=to_int(get("schedule_max_retry", 2), 2, 0, 5),
            schedule_style=to_str(
                get(
                    "schedule_style",
                    "日常向：以真实、细碎、有呼吸感的生活节奏为主，避免戏剧化和过分精彩的一天。",
                )
            ),
            schedule_world=to_str(get("schedule_world", "")),
            schedule_character=to_str(get("schedule_character", "")),
            schedule_forbidden=forbidden,
            schedule_model=to_str(get("schedule_model", "")),
            schedule_temperature=to_float(get("schedule_temperature", 1.0), 1.0, 0.0, 2.0),
            proactive_enabled=to_bool(get("proactive_enabled", True), True),
            proactive_interval_seconds=to_int(
                get("proactive_interval_seconds", 60), 60, 30, 3600
            ),
            proactive_daily_limit=to_int(get("proactive_daily_limit", 6), 6, 0, 100),
            proactive_min_interval_minutes=to_int(
                get("proactive_min_interval_minutes", 45), 45, 0, 1440
            ),
            proactive_max_unanswered=to_int(get("proactive_max_unanswered", 4), 4, 0, 100),
            proactive_quiet_start_min=parse_hhmm(get("proactive_quiet_start", "23:00"), "23:00"),
            proactive_quiet_end_min=parse_hhmm(get("proactive_quiet_end", "08:30"), "08:30"),
            proactive_user_active_cooldown_minutes=to_int(
                get("proactive_user_active_cooldown_minutes", 10), 10, 0, 720
            ),
            proactive_skip_sleeping=to_bool(get("proactive_skip_sleeping", True), True),
            proactive_seed_window_minutes=to_int(
                get("proactive_seed_window_minutes", 90), 90, 5, 720
            ),
            proactive_jitter_minutes=to_int(get("proactive_jitter_minutes", 20), 20, 0, 180),
            proactive_max_segments=to_int(get("proactive_max_segments", 3), 3, 1, 5),
            proactive_segment_delay=to_float(
                get("proactive_segment_delay_seconds", 1.5), 1.5, 0.0, 10.0
            ),
            proactive_history_messages=to_int(
                get("proactive_history_messages", 10), 10, 0, 50
            ),
            proactive_model=to_str(get("proactive_model", "")),
            proactive_extra_instruction=to_str(get("proactive_extra_instruction", "")),
            proactive_image_enabled=to_bool(get("proactive_image_enabled", False), False),
            proactive_image_dir=to_str(get("proactive_image_dir", "")),
            proactive_image_probability=to_float(
                get("proactive_image_probability", 0.25), 0.25, 0.0, 1.0
            ),
            proactive_image_max_per_day=to_int(
                get("proactive_image_max_per_day", 2), 2, 0, 50
            ),
            prompt_plan_override=to_str(get("prompt_plan_override", "")),
            prompt_proactive_override=to_str(get("prompt_proactive_override", "")),
            log_retention=to_int(get("log_retention", 2000), 2000, 100, 100000),
        )

    def quiet_now(self, now_minute: int) -> bool:
        return in_time_window(
            now_minute, self.proactive_quiet_start_min, self.proactive_quiet_end_min
        )


# --------------------------------------------------------------------------- #
# 配置写回
# --------------------------------------------------------------------------- #
def schema_defaults(schema: dict[str, Any] | None) -> dict[str, Any]:
    """从 _conf_schema.json 抽出 key -> default 映射（给控制台表单用）。"""
    result: dict[str, Any] = {}
    for key, spec in (schema or {}).items():
        if isinstance(spec, dict) and "default" in spec:
            result[key] = spec["default"]
    return result


def save_value(config: Any, key: str, value: Any) -> bool:
    """写回单个配置键并落盘。返回是否成功。"""
    if config is None or not hasattr(config, "save_config"):
        return False
    try:
        config[key] = value
        config.save_config()
        return True
    except Exception:
        return False


def save_values(config: Any, values: dict[str, Any]) -> bool:
    """批量写回配置并落盘。"""
    if config is None or not hasattr(config, "save_config"):
        return False
    try:
        for key, value in values.items():
            config[key] = value
        config.save_config()
        return True
    except Exception:
        return False
