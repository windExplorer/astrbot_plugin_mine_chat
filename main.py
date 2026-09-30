"""萌萌日程 —— 日程排布 × 主动消息。

让 Bot 拥有一份贴合人设与世界观的日常生活，并据此在合适时机主动找用户说话。
状态按 persona 归档，因此同一人格的私聊与群聊窗口共享同一份生活。
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from . import config as config_mod
from . import llm as llm_mod  # noqa: F401 - 供热重载签名使用
from . import prompts as prompts_mod  # noqa: F401
from . import proactive as proactive_mod
from . import proactive_gen as proactive_gen_mod  # noqa: F401
from . import schedule as schedule_mod
from . import schedule_view as view_mod
from . import scope as scope_mod
from . import store as store_mod
from . import webui_api as webui_api_mod

PLUGIN_NAME = "astrbot_plugin_mine_chat"
AUTHOR = "windExplorer"

# 依赖模块（按依赖顺序）。热更新时 watchfiles 只重载 main.py，
# 若不强制重载它们，sys.modules 里会一直是被污染的旧代码。
_RELOAD_MODULES = (
    "config",
    "store",
    "llm",
    "prompts",
    "scope",
    "schedule",
    "schedule_view",
    "proactive_gen",
    "proactive",
    "webui_api",
)


def _read_version() -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metadata.yaml")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped.startswith("version:"):
                    return stripped.split(":", 1)[1].split("#")[0].strip()
    except Exception as exc:  # noqa: BLE001
        logger.debug("mine_chat: 读取版本号失败: %s", exc)
    return "v0.0.0"


PLUGIN_VERSION = _read_version()


def _reload_dependencies() -> None:
    package = __package__ or PLUGIN_NAME
    for name in _RELOAD_MODULES:
        full = f"{package}.{name}"
        module = sys.modules.get(full)
        if module is None:
            continue
        try:
            importlib.reload(module)
        except Exception as exc:  # noqa: BLE001 - 重载失败不应阻止插件装载
            logger.warning("mine_chat: 热重载模块 %s 失败: %s", full, exc)


def _resolve_data_dir() -> tuple[str, bool]:
    """取插件专属数据目录（data/plugin_data/<插件名>）。

    失败时退回插件安装目录，绝不让「取目录失败」把插件装载整体带崩。
    注意：StarTools.initialize(context) 由 StarManager.__init__ 调用，
    早于插件实例化，所以正常运行期这里一定是可用的。
    """
    try:
        path = StarTools.get_data_dir(PLUGIN_NAME)
        if path:
            return str(path), True
    except Exception as exc:  # noqa: BLE001
        logger.warning("mine_chat: get_data_dir 失败，回退插件目录: %s", exc)
    return os.path.dirname(os.path.abspath(__file__)), False


def command_args(event: Any, name: str) -> list[str]:
    """从消息文本里取出指令后面的参数。"""
    raw = str(getattr(event, "message_str", "") or "").strip()
    text = raw.lstrip("/／!！")
    parts = [part for part in text.split() if part]
    if not parts:
        return []
    head = parts[0]
    if head == name:
        return parts[1:]
    if head.startswith(name):
        rest = head[len(name) :].strip()
        return ([rest] if rest else []) + parts[1:]
    return parts


@register(
    PLUGIN_NAME,
    AUTHOR,
    "日程排布 × 主动消息：角色有自己的生活，才会主动得更自然。",
    PLUGIN_VERSION,
)
class MineChatPlugin(Star):
    def __init__(self, context: Context, config: Any = None) -> None:
        _reload_dependencies()
        super().__init__(context, config)
        self.config = config
        self.plugin_version = PLUGIN_VERSION

        self.data_dir, data_dir_ok = _resolve_data_dir()
        if not data_dir_ok:
            logger.warning(
                "mine_chat: 未能取得插件数据目录，数据将落在插件安装目录（升级会丢失）"
            )
        self.store = store_mod.Store(os.path.join(self.data_dir, "mine_chat.db"))

        self.resolver = scope_mod.ScopeResolver(context, self.store, self.settings)
        self.schedule_service = schedule_mod.ScheduleService(
            context, self.store, self.resolver, self.settings
        )
        self.proactive = proactive_mod.ProactiveService(
            context,
            self.store,
            self.resolver,
            self.schedule_service,
            self.settings,
            default_image_dir=os.path.join(self.data_dir, "images"),
            meme_cache_dir=os.path.join(self.data_dir, "cache", "memes"),
        )

        self._terminating = False
        self._ready = False
        self._gen_pending: set[str] = set()
        self._bg_tasks: set[asyncio.Task] = set()
        self._warned: set[str] = set()

    # ------------------------------------------------------------------ #
    # 配置
    # ------------------------------------------------------------------ #
    def settings(self) -> config_mod.Settings:
        return config_mod.Settings.from_config(self.config)

    async def is_configured(self) -> bool:
        """人格已选 + 投递窗口已绑定；缺任一项插件保持未启用。"""
        settings = self.settings()
        if not settings.persona_selected():
            return False
        primary = await self.resolver.primary_umo_for(settings.active_persona)
        return bool(primary)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def initialize(self) -> None:
        self._terminating = False
        try:
            await self.store.init()
        except Exception as exc:  # noqa: BLE001
            # 刻意不 raise：数据库建不了时让本插件保持「未就绪」，
            # 而不是把一个插件的初始化失败升级成 AstrBot 启动失败。
            logger.error(
                "mine_chat: 初始化数据库失败，插件将保持停用状态（数据目录 %s）：%s",
                self.data_dir,
                exc,
            )
            return

        self._ready = True
        try:
            if config_mod.migrate_legacy_config(self.config):
                logger.info("mine_chat: 已把 v1.0.x 的平铺配置键迁移到分组结构")
        except Exception as exc:  # noqa: BLE001 - 迁移失败不影响读取（读取有兼容兜底）
            logger.warning("mine_chat: 平铺配置迁移失败: %s", exc)
        await self.proactive.start()
        try:
            webui_api_mod.register(self)
        except Exception as exc:  # noqa: BLE001 - 控制台注册失败不影响主功能
            logger.warning("mine_chat: 注册控制台路由失败: %s", exc)

        settings = self.settings()
        logger.info(
            "mine_chat: 插件已就绪 %s（数据目录 %s；日程 %s；注入 %s；主动 %s）",
            self.plugin_version,
            self.data_dir,
            "开" if settings.schedule_enabled else "关",
            (f"开/{settings.inject_mode}" if settings.inject_enabled else "关"),
            "开" if settings.proactive_enabled else "关",
        )
        if not settings.persona_selected():
            logger.warning(
                "mine_chat: 插件保持未启用——尚未在控制台「人格与窗口」页选择人格"
            )
        elif not await self.is_configured():
            logger.warning(
                "mine_chat: 插件保持未启用——人格 %s 还没有绑定投递窗口"
                "（在控制台绑定一个私聊窗口并设为投递目标）",
                settings.active_persona,
            )
        if not settings.schedule_world.strip() and not settings.schedule_character.strip():
            logger.warning(
                "mine_chat: 尚未填写「世界观 / 角色补充设定」，日程会退化成泛化的普通人生活"
            )

    async def terminate(self) -> None:
        self._terminating = True
        await self.proactive.stop()
        for task in list(self._bg_tasks):
            task.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self._bg_tasks.clear()
        logger.info("mine_chat: 插件已卸载")

    def _track(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _warn_once(self, key: str, message: str, *args: Any) -> None:
        """同类错误只告警一次，之后降为 debug。

        两个钩子（每条消息 / 每次 LLM 请求）都会跑，异常若每轮都 warning 会刷屏。
        """
        if key in self._warned:
            logger.debug(message, *args)
            return
        self._warned.add(key)
        logger.warning(message, *args)

    # ------------------------------------------------------------------ #
    # 事件：用户消息
    # ------------------------------------------------------------------ #
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_user_message(self, event: AstrMessageEvent) -> None:
        if self._terminating or not self._ready:
            return
        settings = self.settings()
        if not settings.enabled or not settings.persona_selected():
            return
        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo or scope_mod.umo_kind(umo) == "other":
            return
        try:
            persona_id = await self.resolver.resolve_persona_id(umo)
            primary = await self.resolver.primary_umo_for(persona_id)
            if not primary:
                # 人格已选但没绑投递窗口：保持未启用，不做任何后续动作。
                return
            await self.resolver.ensure_binding(
                umo, persona_id, auto_register=settings.window_auto_bind
            )
            if primary == umo:
                await self.proactive.note_user_activity(persona_id)
                # anima 出图需要一个真实的会话事件；存投递窗口最近一次。
                self.proactive.remember_event(event)
            self.proactive.kick()
            if settings.schedule_enabled:
                self._ensure_plan_background(persona_id, umo)
        except Exception as exc:  # noqa: BLE001 - 钩子异常不能影响正常回复
            self._warn_once("user_message", "mine_chat: 用户消息处理失败 umo=%s: %s", umo, exc)

    # ------------------------------------------------------------------ #
    # 事件：注入日程
    # ------------------------------------------------------------------ #
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: Any) -> None:
        if self._terminating or not self._ready:
            return
        try:
            await self._inject_schedule(event, req)
        except Exception as exc:  # noqa: BLE001
            self._warn_once("inject", "mine_chat: 注入日程失败: %s", exc)

    async def _inject_schedule(self, event: AstrMessageEvent, req: Any) -> None:
        settings = self.settings()
        if not settings.enabled or not settings.inject_enabled:
            return
        if not settings.persona_selected():
            return
        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            return
        if scope_mod.umo_kind(umo) not in settings.inject_scopes:
            return

        persona_id = await self.resolver.resolve_persona_id(umo)
        await self.resolver.ensure_binding(
            umo, persona_id, auto_register=settings.window_auto_bind
        )
        # 显式配置优先：只对已绑定到当前人格的窗口注入（auto_bind 开启时会顺手登记）。
        binding = await self.store.get_binding(umo)
        if binding is not None:
            if str(binding.get("persona_id") or "") != persona_id:
                return
            if not int(binding.get("enabled") or 0):
                return
        elif not settings.window_auto_bind:
            return
        if not await self.resolver.primary_umo_for(persona_id):
            return

        plan_date = await self.schedule_service.resolve_active_date(persona_id)
        plan = await self.store.get_plan(persona_id, plan_date)
        if not plan or not plan.get("items"):
            if settings.schedule_enabled:
                self._ensure_plan_background(persona_id, umo)
            return

        state = await self.store.get_proactive_state(persona_id)
        last_proactive = ""
        if int(state.get("unanswered") or 0) > 0:
            last_proactive = str(state.get("last_message") or "")

        block = view_mod.build_injection_block(
            items=list(plan["items"]),
            now_minutes=view_mod.now_minutes_for(plan_date),
            settings=settings,
            plan_date=plan_date,
            last_proactive=last_proactive,
            unanswered=int(state.get("unanswered") or 0),
        )
        if not block:
            return
        self._apply_injection(req, block, settings)

    @staticmethod
    def _apply_injection(req: Any, block: str, settings: config_mod.Settings) -> None:
        if settings.inject_mode == "system":
            req.system_prompt = f"{req.system_prompt or ''}\n\n{block}"
            return
        parts = getattr(req, "extra_user_content_parts", None)
        if isinstance(parts, list):
            parts.append({"type": "text", "text": block})
        else:
            req.system_prompt = f"{req.system_prompt or ''}\n\n{block}"

    def _ensure_plan_background(self, persona_id: str, umo: str) -> None:
        if persona_id in self._gen_pending or self._terminating:
            return

        async def runner() -> None:
            self._gen_pending.add(persona_id)
            try:
                await self.schedule_service.ensure_plan(persona_id, umo=umo)
            except Exception as exc:  # noqa: BLE001
                logger.warning("mine_chat: 后台生成日程失败 persona=%s: %s", persona_id, exc)
            finally:
                self._gen_pending.discard(persona_id)

        self._track(runner())

    # ------------------------------------------------------------------ #
    # 指令：/日程
    # ------------------------------------------------------------------ #
    @filter.command("日程", alias={"myplan", "我的日程"})
    async def cmd_plan(self, event: AstrMessageEvent):
        """查看当前角色今天的日程安排（管理员）"""
        if not event.is_admin():
            yield event.plain_result("这个指令只有管理员可以使用。")
            return
        if not self._ready:
            yield event.plain_result("插件未就绪（数据库初始化失败），请查看 AstrBot 日志。")
            return
        setup = await self.resolver.setup_state()
        if not setup["configured"]:
            yield event.plain_result(f"插件尚未完成配置，保持未启用：{setup['hint']}")
            return
        settings = self.settings()
        if not settings.enabled:
            yield event.plain_result("萌萌日程当前已关闭（配置项「启用插件」）。")
            return

        args = command_args(event, "日程")
        umo = getattr(event, "unified_msg_origin", "") or ""
        persona_id = await self.resolver.resolve_persona_id(umo, force=True)
        await self.resolver.ensure_binding(
            umo, persona_id, auto_register=settings.window_auto_bind
        )

        refreshing = bool(args) and args[0] in {"刷新", "重新生成", "refresh", "regen"}
        try:
            if refreshing:
                await self.schedule_service.refresh(persona_id, umo=umo)
            else:
                await self.schedule_service.ensure_plan(persona_id, umo=umo)
        except Exception as exc:  # noqa: BLE001
            logger.error("mine_chat: 指令生成日程失败 persona=%s: %s", persona_id, exc)
            yield event.plain_result(f"生成日程时出错：{exc}")
            return

        header = f"【角色】{self.resolver.persona_display_name(persona_id)}"
        if refreshing:
            header += "（已重新生成）"
        body = await self.schedule_service.describe_plan(persona_id)
        yield event.plain_result(f"{header}\n{body}")

    # ------------------------------------------------------------------ #
    # 指令：/主动
    # ------------------------------------------------------------------ #
    @filter.command("主动", alias={"myproactive"})
    async def cmd_proactive(self, event: AstrMessageEvent):
        """查看/开关主动消息（on / off / now）"""
        if not event.is_admin():
            yield event.plain_result("这个指令只有管理员可以使用。")
            return
        if not self._ready:
            yield event.plain_result("插件未就绪（数据库初始化失败），请查看 AstrBot 日志。")
            return
        setup = await self.resolver.setup_state()
        if not setup["configured"]:
            yield event.plain_result(f"插件尚未完成配置，保持未启用：{setup['hint']}")
            return
        settings = self.settings()
        if not settings.enabled:
            yield event.plain_result("萌萌日程当前已关闭（配置项「启用插件」）。")
            return

        args = command_args(event, "主动")
        umo = getattr(event, "unified_msg_origin", "") or ""
        persona_id = await self.resolver.resolve_persona_id(umo, force=True)
        await self.resolver.ensure_binding(
            umo, persona_id, auto_register=settings.window_auto_bind
        )

        action = args[0].lower() if args else ""
        if action in {"on", "开", "开启", "启用"}:
            await self.proactive.set_enabled(persona_id, True)
            yield event.plain_result("已开启主动消息。")
            return
        if action in {"off", "关", "关闭", "停用"}:
            await self.proactive.set_enabled(persona_id, False)
            yield event.plain_result("已关闭主动消息（用户再次说话前不会主动）。")
            return
        if action in {"now", "发", "立即", "触发"}:
            ok, reason = await self.proactive.trigger_now(persona_id)
            text = "已发出。" if ok else f"这次没有发出，原因：{self._reason_text(reason)}"
            yield event.plain_result(text)
            return

        status = await self.proactive.status(persona_id)
        plan_date = await self.schedule_service.resolve_active_date(persona_id)
        items = await self.store.get_plan_items(persona_id, plan_date)
        current = view_mod.describe(items, view_mod.now_minutes_for(plan_date))
        lines = [
            f"【人物】{self.resolver.persona_display_name(persona_id)}",
            f"【投递窗口】{status['primary_umo'] or '（未绑定，请先在私聊里说一句话）'}",
            f"【主动开关】{'开启' if status['enabled'] else '关闭'}"
            + ("" if settings.proactive_enabled else "（配置里已全局关闭）"),
            f"【此刻】{current}",
            f"【下一次候选】{status['next_at_text']}",
            f"【今日已发】{status['sent_today']}/{status['daily_limit'] or '不限'}",
            f"【连续未回复】{status['unanswered']}/{status['max_unanswered'] or '不限'}",
            f"【免打扰】{status['quiet_hours']}",
            "用法：/主动 on | off | now",
        ]
        yield event.plain_result("\n".join(lines))

    @staticmethod
    def _reason_text(reason: str) -> str:
        mapping = {
            "disabled": "插件或主动消息已关闭",
            "quiet_hours": "当前处于免打扰时段",
            "sleeping": "角色正在睡觉",
            "daily_limit": "今天主动次数已用完",
            "min_interval": "距离上次主动太近",
            "unanswered_limit": "用户连续未回复已达上限",
            "user_active": "用户刚说过话",
            "no_umo": "没有找到投递窗口",
            "no_seed": "当前没有可用的生活片段",
            "not_due": "还没到候选时间",
            "llm_error": "模型调用失败",
            "empty": "模型没有产出内容",
            "send_failed": "消息未送达（平台不可用）",
            "interrupted": "生成期间用户说话了，本次取消",
            "ok": "正常发出",
        }
        return mapping.get(reason, reason)


def plugin_version() -> str:
    return PLUGIN_VERSION


def utc_now_ts() -> float:
    return time.time()
