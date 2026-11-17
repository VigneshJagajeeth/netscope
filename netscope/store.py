"""Tiny SQLite store for diagnostic run history."""

from __future__ import annotations

import json
import os
import sqlite3
import threading

DB_PATH = os.environ.get("NETSCOPE_DB", "/data/netscope.db")
_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    path = DB_PATH
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    except OSError:
        path = os.path.join("/tmp", "netscope.db")
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS runs (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               ts TEXT NOT NULL,
               tool TEXT NOT NULL,
               target TEXT,
               ok INTEGER,
               summary TEXT,
               result TEXT NOT NULL
           )"""
    )
    return conn


_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = _connect()
    return _conn


def save(result: dict) -> int:
    with _lock:
        cur = conn().execute(
            "INSERT INTO runs (ts, tool, target, ok, summary, result) VALUES (?, ?, ?, ?, ?, ?)",
            (
                result.get("timestamp"),
                result.get("tool", "unknown"),
                str(result.get("target", "")),
                1 if result.get("ok") else 0,
                result.get("summary", ""),
                json.dumps(result, default=str),
            ),
        )
        conn().commit()
        return cur.lastrowid


def recent(limit: int = 50, tool: str | None = None) -> list[dict]:
    q = "SELECT id, ts, tool, target, ok, summary FROM runs"
    args: list = []
    if tool:
        q += " WHERE tool = ?"
        args.append(tool)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(limit, 500)))
    with _lock:
        return [dict(r) | {"ok": bool(r["ok"])} for r in conn().execute(q, args)]


def get(run_id: int) -> dict | None:
    with _lock:
        row = conn().execute("SELECT result FROM runs WHERE id = ?", (run_id,)).fetchone()
    return json.loads(row["result"]) if row else None


def clear() -> int:
    with _lock:
        n = conn().execute("DELETE FROM runs").rowcount
        conn().commit()
        return n
