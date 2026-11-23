"""SQLite 持久层：班报口径定义 + 终端流水明细（同号可更正）。

并发模型：进程内一把可重入锁串行化全部读写；每个业务事务在
``with db.tx() as conn`` 内完成「读旧状态 → 校验 → 写新状态」，
因此班报换档与明细补交交错时，双方看到的都是完整已提交状态，
不会出现班报已换、明细档号仍旧的中间态。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS shift_report (
    shift_id      TEXT PRIMARY KEY,
    payload_json  TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS detail (
    shift_id     TEXT NOT NULL,
    terminal_id  TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    bucket_idx   INTEGER NOT NULL,
    value_ms     INTEGER NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (shift_id, terminal_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_detail_shift
    ON detail(shift_id, terminal_id, bucket_idx);
"""


class Database:
    def __init__(self, path: str):
        self._path = path
        # 可重入：业务事务内可安全调用带 conn 参数的读取助手
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @contextmanager
    def tx(self):
        """串行化业务事务：异常整体回滚，正常退出整体提交。"""
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ---------------------------------------------------------- 班报
    def upsert_report_conn(self, conn: sqlite3.Connection,
                           shift_id: str, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        conn.execute(
            "INSERT INTO shift_report(shift_id, payload_json) VALUES (?,?) "
            "ON CONFLICT(shift_id) DO UPDATE SET "
            "payload_json=excluded.payload_json, "
            "updated_at=datetime('now')",
            (shift_id, body))

    def get_report_conn(self, conn: sqlite3.Connection,
                        shift_id: str) -> Optional[dict]:
        row = conn.execute(
            "SELECT payload_json FROM shift_report WHERE shift_id=?",
            (shift_id,)).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def get_report(self, shift_id: str) -> Optional[dict]:
        with self._lock:
            return self.get_report_conn(self._conn, shift_id)

    # ---------------------------------------------------------- 明细
    def list_details_conn(self, conn: sqlite3.Connection,
                          shift_id: str) -> list[dict]:
        rows = conn.execute(
            "SELECT terminal_id, seq, bucket_idx, value_ms "
            "FROM detail WHERE shift_id=? "
            "ORDER BY terminal_id, seq", (shift_id,)).fetchall()
        return [dict(r) for r in rows]

    def list_details(self, shift_id: str) -> list[dict]:
        with self._lock:
            return self.list_details_conn(self._conn, shift_id)

    def replace_merged_conn(self, conn: sqlite3.Connection, shift_id: str,
                            merged: dict[tuple[str, int], tuple[int, int]],
                            stats: dict) -> dict:
        """
        按 (terminal_id, seq) 合并后的完整状态一次性落库（调用方已完成校验）。
        同号覆盖即"更正"，新键即插入。须在 db.tx() 内调用。
        """
        conn.executemany(
            "INSERT INTO detail(shift_id, terminal_id, seq, bucket_idx, "
            "value_ms) VALUES (?,?,?,?,?) "
            "ON CONFLICT(shift_id, terminal_id, seq) DO UPDATE SET "
            "bucket_idx=excluded.bucket_idx, value_ms=excluded.value_ms, "
            "updated_at=datetime('now')",
            [(shift_id, term, seq, b, v)
             for (term, seq), (b, v) in merged.items()])
        return {"stored": len(merged), **stats}

    def remap_details_conn(self, conn: sqlite3.Connection, shift_id: str,
                           remapped: list[dict]) -> None:
        """班报换档后整批改写明细档号（流水号与耗时原样保留）。

        须与班报覆盖写在同一事务内：任一步失败整体回滚，继续沿用原班报。
        """
        conn.executemany(
            "UPDATE detail SET bucket_idx=?, updated_at=datetime('now') "
            "WHERE shift_id=? AND terminal_id=? AND seq=?",
            [(it["bucket_idx"], shift_id, it["terminal_id"], it["seq"])
             for it in remapped])

    def snapshot(self, shift_id: str) -> tuple[Optional[dict], list[dict]]:
        """同一已提交状态下一次性读取班报与明细（单次加锁，互不串档）。"""
        with self._lock:
            report = self.get_report_conn(self._conn, shift_id)
            details = self.list_details_conn(self._conn, shift_id)
            return report, details

    def close(self) -> None:
        with self._lock:
            self._conn.close()
