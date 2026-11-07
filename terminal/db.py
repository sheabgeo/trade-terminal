"""SQLite store. Every record the Bot sees or produces is saved here first."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL,
    ts TEXT NOT NULL,              -- bar open time, UTC ISO
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    received_at TEXT NOT NULL,
    PRIMARY KEY (symbol, ts)
);

CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    market_ts TEXT NOT NULL,
    received_at TEXT NOT NULL,
    bid REAL, ask REAL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    bar_ts TEXT,                   -- completed signal bar
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    quote_id INTEGER REFERENCES quotes(id),
    quote_age_s REAL,
    indicators TEXT NOT NULL,      -- JSON
    account TEXT NOT NULL,         -- JSON: equity, allocation, exposure, open orders
    signal INTEGER NOT NULL,       -- 1 if mechanical rule passed
    blockers TEXT NOT NULL         -- JSON list of reasons entry is blocked
);

CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,           -- symbol|strategy_version|bar_ts
    symbol TEXT NOT NULL,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL,          -- PENDING | CLAIMED | DECIDED | EXPIRED
    owner TEXT,
    claimed_at TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL UNIQUE REFERENCES candidates(id),
    action TEXT NOT NULL CHECK (action IN ('ENTER','SKIP')),
    reason TEXT NOT NULL,
    evidence TEXT NOT NULL,        -- JSON list of snapshot / source ids
    owner TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL UNIQUE REFERENCES candidates(id),   -- one entry intent per candidate
    client_order_id TEXT NOT NULL UNIQUE,                          -- stable, saved BEFORE submission
    owner TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    limit_price REAL NOT NULL,
    stop_price REAL NOT NULL,
    target_price REAL NOT NULL,
    time_exit_at TEXT NOT NULL,
    sizing TEXT NOT NULL,          -- JSON: every number behind the qty
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL,          -- PREVIEWED|SUBMITTING|SUBMITTED|UNKNOWN|FILLED|PARTIAL|NO_FILL|NOT_SENT|REJECTED|EXPIRED
    broker_order_id TEXT,
    broker_status TEXT,
    filled_qty REAL,
    filled_avg_price REAL,
    error TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id INTEGER NOT NULL UNIQUE REFERENCES intents(id),
    symbol TEXT NOT NULL,
    qty REAL NOT NULL,             -- SELLABLE qty per broker (fees can be charged in the asset)
    entry_price REAL NOT NULL,
    stop_price REAL NOT NULL,
    target_price REAL NOT NULL,
    time_exit_at TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    status TEXT NOT NULL           -- OPEN | CLOSED (supervisor, stage 5)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def log_event(conn: sqlite3.Connection, ts: str, kind: str, detail: dict) -> None:
    conn.execute("INSERT INTO events (ts, kind, detail) VALUES (?,?,?)", (ts, kind, json.dumps(detail)))
