"""Our own order service store (Valio has no order API) + audit log, in SQLite."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

from order_service import config

_lock = threading.RLock()
_con: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
  order_id TEXT PRIMARY KEY, buyer_id TEXT NOT NULL, status TEXT NOT NULL,
  created_at TEXT, updated_at TEXT, need TEXT, lines TEXT, total_eur REAL,
  flags TEXT, policy_reasons TEXT, summary TEXT, cancel_until TEXT,
  decided_by TEXT, decided_at TEXT, decision_note TEXT, check_notes TEXT
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, buyer_id TEXT, order_id TEXT,
  actor TEXT, event TEXT, message TEXT, data TEXT
);
CREATE TABLE IF NOT EXISTS offer_log (
  ref TEXT PRIMARY KEY, buyer_id TEXT, created_at TEXT, product_code TEXT, need_digest TEXT, offer TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_buyer ON orders(buyer_id);
CREATE INDEX IF NOT EXISTS idx_audit_buyer ON audit(buyer_id);
"""

JSON_COLS = {"need", "lines", "flags", "policy_reasons", "check_notes", "data", "offer"}


def con() -> sqlite3.Connection:
    global _con
    with _lock:
        if _con is None:
            config.ORDERS_DB.parent.mkdir(parents=True, exist_ok=True)
            _con = sqlite3.connect(config.ORDERS_DB, check_same_thread=False)
            _con.row_factory = sqlite3.Row
            _con.executescript(SCHEMA)
        return _con


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in JSON_COLS & d.keys():
        d[k] = json.loads(d[k]) if d[k] else None
    return d


def execute(sql: str, args: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        cur = con().execute(sql, args)
        con().commit()
        return cur


def query(sql: str, args: tuple = ()) -> list[dict]:
    with _lock:
        return [row(r) for r in con().execute(sql, args).fetchall()]


def audit(event: str, message: str, *, buyer_id: str | None, actor: str, order_id: str | None = None, data: dict | None = None) -> None:
    execute("INSERT INTO audit (ts, buyer_id, order_id, actor, event, message, data) VALUES (?,?,?,?,?,?,?)",
            (now_iso(), buyer_id, order_id, actor, event, message, json.dumps(data or {}, ensure_ascii=False, default=str)))


def reset() -> None:
    """Dev/demo only: wipe orders and audit."""
    with _lock:
        con().executescript("DELETE FROM orders; DELETE FROM audit; DELETE FROM offer_log;")
        con().commit()
