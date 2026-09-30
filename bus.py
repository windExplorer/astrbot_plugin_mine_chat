"""轻量进程内事件总线：控制台「准实时更新」用。

工作方式：
- 后端任何值得让控制台知道的状态变化（发送了主动消息、日程生成/更新、
  世界观提取/换绑、新增裁决日志……）调用 `notify()`，全局版本号 +1；
- 前端持有版本号发长轮询请求 `GET /events?since=<v>`，服务端在
  `wait_version()` 里挂住请求直到版本号前进或超时（约 25s）；
- 版本号一变，所有挂着的请求立刻返回 → 页面亚秒级感知变化。

这比定时轮询好在：变化发生即刻推送、空闲时页面只挂一个不占流量的
挂起请求，而不是每隔 N 秒无条件拉一次全量数据。
"""

from __future__ import annotations

import asyncio
import time

_version = 0
_condition: asyncio.Condition | None = None


def _cond() -> asyncio.Condition:
    global _condition
    if _condition is None:
        _condition = asyncio.Condition()
    return _condition


def current_version() -> int:
    return _version


def notify() -> None:
    """状态变化时调用（线程安全：notify 自身只改整数与唤醒等待者）。"""
    global _version
    _version += 1
    cond = _cond()

    async def _wake() -> None:
        async with cond:
            cond.notify_all()

    try:
        asyncio.get_running_loop().create_task(_wake())
    except RuntimeError:
        # 没有事件循环（极端情况）：版本号已前进，长轮询会因超时返回
        pass


async def wait_version(since: int, timeout: float) -> int:
    """挂起直到版本号超过 since 或超时；返回当前版本号。"""
    cond = _cond()
    deadline = time.monotonic() + max(1.0, min(timeout, 60.0))
    async with cond:
        while _version <= since:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(cond.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                break
    return _version
