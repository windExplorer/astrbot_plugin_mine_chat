"""SQLite 存储层（标准库 sqlite3，无第三方依赖）。

设计要点：
- 每次操作使用短连接（WAL + busy_timeout），避免跨线程共享连接对象；
- 所有同步 sqlite 调用经 `asyncio.to_thread` 包装，不阻塞事件循环；
- 建表幂等，`meta.schema_version` 用于后续迁移；
- 所有状态按 `persona_id` 归档——同一人格的私聊/群聊窗口共享同一份生活。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable, Iterable, Sequence

SCHEMA_VERSION = 2

# 只做「加列」这类幂等迁移；新装的库 DDL 已含新列，重复 ALTER 报错时忽略。
_MIGRATIONS: tuple[str, ...] = (
    "ALTER TABLE proactive_state ADD COLUMN images_today INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE proactive_state ADD COLUMN images_date TEXT",
)

_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS persona_state (
        persona_id   TEXT PRIMARY KEY,
        persona_name TEXT,
        enabled      INTEGER NOT NULL DEFAULT 1,
        created_at   REAL NOT NULL,
        updated_at   REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS window_binding (
        umo           TEXT PRIMARY KEY,
        persona_id    TEXT NOT NULL,
        kind          TEXT NOT NULL,
        is_primary    INTEGER NOT NULL DEFAULT 0,
        enabled       INTEGER NOT NULL DEFAULT 1,
        first_seen_at REAL NOT NULL,
        last_seen_at  REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_plan (
        persona_id   TEXT NOT NULL,
        plan_date    TEXT NOT NULL,
        generated_at REAL NOT NULL,
        source       TEXT NOT NULL,
        retry_after  REAL,
        quality      REAL,
        note         TEXT,
        raw_json     TEXT,
        PRIMARY KEY (persona_id, plan_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS plan_item (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        persona_id   TEXT NOT NULL,
        plan_date    TEXT NOT NULL,
        seq          INTEGER NOT NULL,
        start_min    INTEGER NOT NULL,
        end_min      INTEGER NOT NULL,
        activity     TEXT NOT NULL,
        mood         TEXT,
        message_seed TEXT,
        basis        TEXT,
        confidence   REAL,
        UNIQUE (persona_id, plan_date, seq)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS proactive_state (
        persona_id    TEXT PRIMARY KEY,
        primary_umo   TEXT,
        next_at       REAL,
        sent_today    INTEGER NOT NULL DEFAULT 0,
        sent_date     TEXT,
        last_sent_at  REAL,
        last_message  TEXT,
        unanswered    INTEGER NOT NULL DEFAULT 0,
        last_user_at  REAL,
        enabled       INTEGER NOT NULL DEFAULT 1,
        images_today  INTEGER NOT NULL DEFAULT 0,
        images_date   TEXT,
        updated_at    REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS proactive_log (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        persona_id TEXT NOT NULL,
        ts         REAL NOT NULL,
        decision   TEXT NOT NULL,
        reason     TEXT,
        umo        TEXT,
        content    TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_plan_item_lookup ON plan_item(persona_id, plan_date, start_min)",
    "CREATE INDEX IF NOT EXISTS idx_log_persona_ts ON proactive_log(persona_id, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_binding_persona ON window_binding(persona_id)",
)


class Store:
    """薄封装的异步存储门面。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    # ------------------------------------------------------------------ #
    # 底层
    # ------------------------------------------------------------------ #
    def _connect(self):
        import sqlite3

        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    async def _run(self, func: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.to_thread(func, *args)

    # ---- 同步实现（全部在 to_thread 里执行） ---------------------------- #
    def _init_sync(self) -> None:
        conn = self._connect()
        try:
            for statement in _DDL:
                conn.execute(statement)
            conn.commit()
            # 旧库升级：v1.0.x 的 proactive_state 没有 images_today / images_date。
            # 新装的库已在 DDL 里带出这两列，ALTER 会报 duplicate，忽略即可。
            for statement in _MIGRATIONS:
                try:
                    conn.execute(statement)
                    conn.commit()
                except Exception:  # noqa: BLE001 - 列已存在
                    try:
                        conn.rollback()
                    except Exception:  # noqa: BLE001
                        pass
            current = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if current is None or str(current["value"]) != str(SCHEMA_VERSION):
                # 迁移后必须回写版本号：只 ALTER 列不记版本，下次升级无法判断基线。
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(SCHEMA_VERSION),),
                )
            conn.commit()
        finally:
            conn.close()

    def _query_sync(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def _execute_sync(self, sql: str, params: Sequence[Any] = ()) -> None:
        conn = self._connect()
        try:
            conn.execute(sql, tuple(params))
            conn.commit()
        finally:
            conn.close()

    def _executemany_sync(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        conn = self._connect()
        try:
            conn.executemany(sql, [tuple(row) for row in rows])
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def init(self) -> None:
        await self._run(self._init_sync)

    # ------------------------------------------------------------------ #
    # meta
    # ------------------------------------------------------------------ #
    async def get_meta(self, key: str, default: str | None = None) -> str | None:
        rows = await self._run(
            self._query_sync, "SELECT value FROM meta WHERE key=?", (key,)
        )
        return rows[0]["value"] if rows else default

    async def set_meta(self, key: str, value: str) -> None:
        await self._run(
            self._execute_sync,
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # ------------------------------------------------------------------ #
    # persona_state
    # ------------------------------------------------------------------ #
    async def upsert_persona(
        self, persona_id: str, persona_name: str | None = None, enabled: bool | None = None
    ) -> None:
        now = time.time()
        await self._run(
            self._execute_sync,
            """
            INSERT INTO persona_state(persona_id, persona_name, enabled, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(persona_id) DO UPDATE SET
                persona_name = COALESCE(excluded.persona_name, persona_state.persona_name),
                enabled      = COALESCE(?, persona_state.enabled),
                updated_at   = excluded.updated_at
            """,
            (
                persona_id,
                persona_name,
                1 if (enabled is None or enabled) else 0,
                now,
                now,
                None if enabled is None else (1 if enabled else 0),
            ),
        )

    async def list_personas(self) -> list[dict[str, Any]]:
        return await self._run(
            self._query_sync, "SELECT * FROM persona_state ORDER BY created_at ASC"
        )

    async def get_persona(self, persona_id: str) -> dict[str, Any] | None:
        rows = await self._run(
            self._query_sync,
            "SELECT * FROM persona_state WHERE persona_id=?",
            (persona_id,),
        )
        return rows[0] if rows else None

    async def set_persona_enabled(self, persona_id: str, enabled: bool) -> None:
        await self._run(
            self._execute_sync,
            "UPDATE persona_state SET enabled=?, updated_at=? WHERE persona_id=?",
            (1 if enabled else 0, time.time(), persona_id),
        )

    async def delete_persona(self, persona_id: str) -> None:
        conn_ops: list[tuple[str, Sequence[Any]]] = [
            ("DELETE FROM plan_item WHERE persona_id=?", (persona_id,)),
            ("DELETE FROM daily_plan WHERE persona_id=?", (persona_id,)),
            ("DELETE FROM proactive_log WHERE persona_id=?", (persona_id,)),
            ("DELETE FROM proactive_state WHERE persona_id=?", (persona_id,)),
            ("DELETE FROM window_binding WHERE persona_id=?", (persona_id,)),
            ("DELETE FROM persona_state WHERE persona_id=?", (persona_id,)),
        ]
        await self._run(self._delete_persona_sync, conn_ops)

    def _delete_persona_sync(self, ops: list[tuple[str, Sequence[Any]]]) -> None:
        conn = self._connect()
        try:
            for sql, params in ops:
                conn.execute(sql, tuple(params))
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    # window_binding
    # ------------------------------------------------------------------ #
    async def upsert_binding(
        self,
        umo: str,
        persona_id: str,
        kind: str,
        is_primary: bool = False,
        enabled: bool = True,
        touch_only: bool = False,
    ) -> None:
        now = time.time()
        await self._run(
            self._execute_sync,
            """
            INSERT INTO window_binding(umo, persona_id, kind, is_primary, enabled,
                                       first_seen_at, last_seen_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(umo) DO UPDATE SET
                persona_id   = CASE WHEN ? THEN window_binding.persona_id ELSE excluded.persona_id END,
                kind         = excluded.kind,
                is_primary   = MAX(window_binding.is_primary, excluded.is_primary),
                enabled      = CASE WHEN ? THEN window_binding.enabled ELSE excluded.enabled END,
                last_seen_at = excluded.last_seen_at
            """,
            (
                umo,
                persona_id,
                kind,
                1 if is_primary else 0,
                1 if enabled else 0,
                now,
                now,
                1 if touch_only else 0,
                1 if touch_only else 0,
            ),
        )

    async def get_binding(self, umo: str) -> dict[str, Any] | None:
        rows = await self._run(
            self._query_sync, "SELECT * FROM window_binding WHERE umo=?", (umo,)
        )
        return rows[0] if rows else None

    async def list_bindings(self, persona_id: str | None = None) -> list[dict[str, Any]]:
        if persona_id:
            return await self._run(
                self._query_sync,
                "SELECT * FROM window_binding WHERE persona_id=? ORDER BY last_seen_at DESC",
                (persona_id,),
            )
        return await self._run(
            self._query_sync, "SELECT * FROM window_binding ORDER BY last_seen_at DESC"
        )

    async def delete_binding(self, umo: str) -> None:
        await self._run(
            self._execute_sync, "DELETE FROM window_binding WHERE umo=?", (umo,)
        )

    async def set_primary(self, persona_id: str, umo: str | None) -> None:
        await self._run(self._set_primary_sync, persona_id, umo)

    def _set_primary_sync(self, persona_id: str, umo: str | None) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE window_binding SET is_primary=0 WHERE persona_id=?", (persona_id,)
            )
            if umo:
                conn.execute(
                    "UPDATE window_binding SET is_primary=1, enabled=1 WHERE umo=?", (umo,)
                )
            conn.commit()
        finally:
            conn.close()

    async def get_primary(self, persona_id: str) -> dict[str, Any] | None:
        rows = await self._run(
            self._query_sync,
            "SELECT * FROM window_binding WHERE persona_id=? AND is_primary=1 "
            "AND kind='private' ORDER BY last_seen_at DESC LIMIT 1",
            (persona_id,),
        )
        return rows[0] if rows else None

    # ------------------------------------------------------------------ #
    # daily_plan / plan_item
    # ------------------------------------------------------------------ #
    async def get_plan(self, persona_id: str, plan_date: str) -> dict[str, Any] | None:
        plans = await self._run(
            self._query_sync,
            "SELECT * FROM daily_plan WHERE persona_id=? AND plan_date=?",
            (persona_id, plan_date),
        )
        if not plans:
            return None
        items = await self._run(
            self._query_sync,
            "SELECT * FROM plan_item WHERE persona_id=? AND plan_date=? ORDER BY seq ASC",
            (persona_id, plan_date),
        )
        plan = plans[0]
        plan["items"] = items
        return plan

    async def get_plan_items(self, persona_id: str, plan_date: str) -> list[dict[str, Any]]:
        return await self._run(
            self._query_sync,
            "SELECT * FROM plan_item WHERE persona_id=? AND plan_date=? ORDER BY seq ASC",
            (persona_id, plan_date),
        )

    async def get_plan_meta(self, persona_id: str, plan_date: str) -> dict[str, Any] | None:
        rows = await self._run(
            self._query_sync,
            "SELECT * FROM daily_plan WHERE persona_id=? AND plan_date=?",
            (persona_id, plan_date),
        )
        return rows[0] if rows else None

    async def save_plan(
        self,
        persona_id: str,
        plan_date: str,
        source: str,
        items: list[dict[str, Any]],
        quality: float | None = None,
        note: str = "",
        raw_json: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        await self._run(
            self._save_plan_sync,
            persona_id,
            plan_date,
            source,
            items,
            quality,
            note,
            raw_json,
            retry_after,
        )

    def _save_plan_sync(
        self,
        persona_id: str,
        plan_date: str,
        source: str,
        items: list[dict[str, Any]],
        quality: float | None,
        note: str,
        raw_json: str | None,
        retry_after: float | None,
    ) -> None:
        now = time.time()
        conn = self._connect()
        try:
            conn.execute(
                "DELETE FROM plan_item WHERE persona_id=? AND plan_date=?",
                (persona_id, plan_date),
            )
            conn.execute(
                "DELETE FROM daily_plan WHERE persona_id=? AND plan_date=?",
                (persona_id, plan_date),
            )
            conn.execute(
                """
                INSERT INTO daily_plan(persona_id, plan_date, generated_at, source,
                                       retry_after, quality, note, raw_json)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    persona_id,
                    plan_date,
                    now,
                    source,
                    retry_after,
                    quality,
                    note,
                    raw_json,
                ),
            )
            conn.executemany(
                """
                INSERT INTO plan_item(persona_id, plan_date, seq, start_min, end_min,
                                      activity, mood, message_seed, basis, confidence)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        persona_id,
                        plan_date,
                        index,
                        int(item.get("start_min", 0)),
                        int(item.get("end_min", 0)),
                        str(item.get("activity", "")),
                        str(item.get("mood", "") or ""),
                        str(item.get("message_seed", "") or ""),
                        json.dumps(item.get("basis", []), ensure_ascii=False),
                        item.get("confidence"),
                    )
                    for index, item in enumerate(items)
                ],
            )
            conn.commit()
        finally:
            conn.close()

    async def update_plan_item(
        self,
        item_id: int,
        activity: str | None = None,
        mood: str | None = None,
        message_seed: str | None = None,
    ) -> None:
        fields: list[str] = []
        params: list[Any] = []
        if activity is not None:
            fields.append("activity=?")
            params.append(activity)
        if mood is not None:
            fields.append("mood=?")
            params.append(mood)
        if message_seed is not None:
            fields.append("message_seed=?")
            params.append(message_seed)
        if not fields:
            return
        params.append(item_id)
        await self._run(
            self._execute_sync,
            f"UPDATE plan_item SET {', '.join(fields)} WHERE id=?",
            params,
        )

    async def delete_plan(self, persona_id: str, plan_date: str) -> None:
        await self._run(self._delete_plan_sync, persona_id, plan_date)

    def _delete_plan_sync(self, persona_id: str, plan_date: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "DELETE FROM plan_item WHERE persona_id=? AND plan_date=?",
                (persona_id, plan_date),
            )
            conn.execute(
                "DELETE FROM daily_plan WHERE persona_id=? AND plan_date=?",
                (persona_id, plan_date),
            )
            conn.commit()
        finally:
            conn.close()

    async def recent_activities(
        self, persona_id: str, exclude_date: str, days: int
    ) -> list[dict[str, Any]]:
        """取最近若干天的日程活动摘要，用于避重。返回 [{plan_date, activity}]。"""
        if days <= 0:
            return []
        return await self._run(
            self._query_sync,
            "SELECT plan_date, activity FROM plan_item WHERE persona_id=? AND plan_date<? "
            "ORDER BY plan_date DESC, seq ASC LIMIT ?",
            (persona_id, exclude_date, days * 20),
        )

    async def list_plans(self, persona_id: str, limit: int = 30) -> list[dict[str, Any]]:
        return await self._run(
            self._query_sync,
            "SELECT * FROM daily_plan WHERE persona_id=? ORDER BY plan_date DESC LIMIT ?",
            (persona_id, limit),
        )

    # ------------------------------------------------------------------ #
    # proactive_state
    # ------------------------------------------------------------------ #
    _PROACTIVE_FIELDS = (
        "primary_umo",
        "next_at",
        "sent_today",
        "sent_date",
        "last_sent_at",
        "last_message",
        "unanswered",
        "last_user_at",
        "enabled",
        "images_today",
        "images_date",
    )

    async def get_proactive_state(self, persona_id: str) -> dict[str, Any]:
        rows = await self._run(
            self._query_sync,
            "SELECT * FROM proactive_state WHERE persona_id=?",
            (persona_id,),
        )
        if rows:
            state = rows[0]
        else:
            state = {"persona_id": persona_id}
        for key in self._PROACTIVE_FIELDS:
            state.setdefault(key, None)
        state.setdefault("sent_today", 0)
        state.setdefault("unanswered", 0)
        state.setdefault("images_today", 0)
        state["enabled"] = 1 if state.get("enabled") is None else int(state["enabled"])
        return state

    async def upsert_proactive_state(self, persona_id: str, **fields: Any) -> None:
        updates = {k: v for k, v in fields.items() if k in self._PROACTIVE_FIELDS}
        await self._run(self._upsert_proactive_sync, persona_id, updates)

    def _upsert_proactive_sync(self, persona_id: str, updates: dict[str, Any]) -> None:
        conn = self._connect()
        try:
            exists = conn.execute(
                "SELECT 1 FROM proactive_state WHERE persona_id=?", (persona_id,)
            ).fetchone()
            now = time.time()
            if not exists:
                conn.execute(
                    "INSERT INTO proactive_state(persona_id, updated_at) VALUES(?, ?)",
                    (persona_id, now),
                )
            if updates:
                assignments = ", ".join(f"{key}=?" for key in updates)
                params = list(updates.values()) + [now, persona_id]
                conn.execute(
                    f"UPDATE proactive_state SET {assignments}, updated_at=? "
                    "WHERE persona_id=?",
                    params,
                )
            conn.commit()
        finally:
            conn.close()

    async def list_proactive_states(self) -> list[dict[str, Any]]:
        return await self._run(self._query_sync, "SELECT * FROM proactive_state")

    async def get_next_due(self) -> float | None:
        """所有启用状态下最早的 next_at（用于调度循环算超时）。"""
        rows = await self._run(
            self._query_sync,
            "SELECT MIN(next_at) AS due FROM proactive_state "
            "WHERE enabled=1 AND next_at IS NOT NULL",
        )
        if not rows:
            return None
        return rows[0]["due"]

    # ------------------------------------------------------------------ #
    # proactive_log
    # ------------------------------------------------------------------ #
    async def add_log(
        self,
        persona_id: str,
        decision: str,
        reason: str = "",
        umo: str = "",
        content: str = "",
        ts: float | None = None,
    ) -> None:
        await self._run(
            self._execute_sync,
            "INSERT INTO proactive_log(persona_id, ts, decision, reason, umo, content) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (
                persona_id,
                ts if ts is not None else time.time(),
                decision,
                reason,
                umo,
                content[:2000],
            ),
        )

    async def list_logs(
        self,
        persona_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        reason: str | None = None,
        decision: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if persona_id:
            clauses.append("persona_id=?")
            params.append(persona_id)
        if reason:
            clauses.append("reason=?")
            params.append(reason)
        if decision:
            clauses.append("decision=?")
            params.append(decision)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([max(1, min(limit, 500)), max(0, offset)])
        return await self._run(
            self._query_sync,
            f"SELECT * FROM proactive_log {where} ORDER BY ts DESC LIMIT ? OFFSET ?",
            params,
        )

    async def count_logs(self, persona_id: str | None = None) -> int:
        if persona_id:
            rows = await self._run(
                self._query_sync,
                "SELECT COUNT(*) AS c FROM proactive_log WHERE persona_id=?",
                (persona_id,),
            )
        else:
            rows = await self._run(
                self._query_sync, "SELECT COUNT(*) AS c FROM proactive_log"
            )
        return int(rows[0]["c"]) if rows else 0

    async def purge_logs(self, keep: int) -> None:
        keep = max(10, int(keep))
        await self._run(
            self._execute_sync,
            "DELETE FROM proactive_log WHERE id NOT IN ("
            "SELECT id FROM proactive_log ORDER BY ts DESC LIMIT ?)",
            (keep,),
        )

    async def log_summary(self, persona_id: str | None = None, since: float | None = None) -> dict[str, int]:
        clauses: list[str] = []
        params: list[Any] = []
        if persona_id:
            clauses.append("persona_id=?")
            params.append(persona_id)
        if since is not None:
            clauses.append("ts>=?")
            params.append(since)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self._run(
            self._query_sync,
            f"SELECT decision, COUNT(*) AS c FROM proactive_log {where} GROUP BY decision",
            params,
        )
        return {row["decision"]: int(row["c"]) for row in rows}
