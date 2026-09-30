"""store.py 的建库与迁移自检（真实 sqlite，不依赖 astrbot 运行时）。

用法：
    python tests/test_store_migration.py    # 退出码 0 即全部通过

覆盖三个场景：
  1) 全新库：建表 + proactive_state 新列（images_today / images_date）写读；
  2) v1 旧库：手工造一个没有新列的旧库，跑 init() 触发 ALTER 迁移后可写新列；
  3) 重复 init：幂等，不会把已迁移的库改坏。
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import store as store_mod  # noqa: E402


def make_v1_db(path: str) -> None:
    """手工造一个 v1 结构的旧库（proactive_state 没有 images_* 列）。"""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE proactive_state (
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
                updated_at    REAL
            );
            INSERT INTO meta(key, value) VALUES('schema_version', '1');
            INSERT INTO proactive_state(persona_id, enabled) VALUES('小满', 1);
            """
        )
        conn.commit()
    finally:
        conn.close()


class Checker:
    def __init__(self) -> None:
        self.count = 0
        self.failures: list[str] = []

    def equal(self, label: str, actual, expected) -> None:
        self.count += 1
        if actual == expected:
            print(f"  [ok] {label}")
        else:
            print(f"  [FAIL] {label} -> actual={actual!r} expected={expected!r}")
            self.failures.append(label)

    def check(self, label: str, condition: bool) -> None:
        self.count += 1
        print(f"  [ok] {label}" if condition else f"  [FAIL] {label}")
        if not condition:
            self.failures.append(label)


async def run(tmp: str, c: Checker) -> None:
    # ---- 场景 1：全新库 -------------------------------------------------
    print("\n[1] 全新库")
    fresh = store_mod.Store(os.path.join(tmp, "new.db"))
    await fresh.init()
    await fresh.upsert_proactive_state("小满", images_today=2, images_date="2026-09-30")
    state = await fresh.get_proactive_state("小满")
    c.equal("images_today 写读", int(state["images_today"]), 2)
    c.equal("images_date 写读", state["images_date"], "2026-09-30")

    # ---- 场景 2：v1 旧库迁移 -------------------------------------------
    print("\n[2] v1 旧库迁移（ALTER ADD COLUMN）")
    old_path = os.path.join(tmp, "old.db")
    make_v1_db(old_path)
    migrated = store_mod.Store(old_path)
    await migrated.init()
    state = await migrated.get_proactive_state("小满")
    c.equal("迁移后原有数据保留", state["enabled"], 1)
    await migrated.upsert_proactive_state("小满", images_today=1, images_date="2026-09-30")
    state = await migrated.get_proactive_state("小满")
    c.equal("迁移后可写新列", int(state["images_today"]), 1)

    # ---- 场景 3：重复 init 幂等 ----------------------------------------
    print("\n[3] 重复 init 幂等")
    await migrated.init()
    state = await migrated.get_proactive_state("小满")
    c.equal("数据未被破坏", int(state["images_today"]), 1)
    version = await migrated.get_meta("schema_version")
    c.equal("schema_version 已更新", version, "2")

    # ---- 附带：日志写入与清理 ------------------------------------------
    print("\n[4] 日志")
    await migrated.add_log("小满", "send", "ok", "aiocqhttp:FriendMessage:1", "早呀")
    rows = await migrated.list_logs(persona_id="小满", limit=10)
    c.equal("日志可写可读", len(rows), 1)
    await migrated.purge_logs(keep=10)
    total = await migrated.count_logs("小满")
    c.equal("purge 后仍在保留期内", total, 1)


def main() -> int:
    checker = Checker()
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(run(tmp, checker))
    print(f"\n共 {checker.count} 项检查，失败 {len(checker.failures)} 项")
    if checker.failures:
        for name in checker.failures:
            print(f"  - {name}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
