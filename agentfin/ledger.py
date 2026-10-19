"""Append-only SQLite ledger. Money in integer micro-USD, timestamps in UTC.

The ledger is the single source of truth. Cost events are never updated or
deleted; corrections are written as new compensating events.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idem_key TEXT NOT NULL UNIQUE,
    ts_utc TEXT NOT NULL,              -- ISO 8601 UTC, 'YYYY-MM-DDTHH:MM:SSZ'
    ts_offset TEXT,                    -- original offset, e.g. '+05:30' (for audit)
    provider TEXT NOT NULL,
    model_raw TEXT NOT NULL,
    sku TEXT NOT NULL,                 -- provider:model
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_micro_usd INTEGER,            -- NULL => UNPRICED (no price for this SKU)
    is_correction INTEGER NOT NULL DEFAULT 0,
    task TEXT NOT NULL DEFAULT 'unattributed',
    agent TEXT NOT NULL DEFAULT 'unknown',
    team TEXT NOT NULL DEFAULT 'unassigned',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    ingested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON usage_events(ts_utc);
CREATE INDEX IF NOT EXISTS idx_events_scope ON usage_events(team, agent, task);
CREATE INDEX IF NOT EXISTS idx_events_sku ON usage_events(sku);

CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task TEXT NOT NULL,
    kind TEXT NOT NULL,                -- revenue | hours_saved | tickets | custom
    value_usd_micro INTEGER NOT NULL,  -- monetized value of the outcome
    note TEXT NOT NULL DEFAULT '',
    ts_utc TEXT NOT NULL,
    UNIQUE(task, kind, ts_utc, note)
);

CREATE TABLE IF NOT EXISTS alert_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_key TEXT NOT NULL UNIQUE,    -- e.g. 'budget:team:backend' or 'anomaly:agent:x'
    fired_at TEXT NOT NULL,
    dismissed INTEGER NOT NULL DEFAULT 0,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def parse_ts(value: Any) -> tuple[str, str | None]:
    """Normalize a timestamp to (utc_iso, original_offset).

    Accepts epoch seconds (int/float/str), ISO 8601 with or without offset,
    and 'YYYY-MM-DD HH:MM:SS'. Returns UTC ISO 'YYYY-MM-DDTHH:MM:SSZ'.
    Naive datetimes are assumed UTC (and flagged in the offset field as None).
    """
    if isinstance(value, (int, float)) or (
        isinstance(value, str) and value.strip().replace(".", "", 1).lstrip("-").isdigit()
    ):
        dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ"), None
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise ValueError(f"unparseable timestamp: {value!r}") from exc
    offset = None
    if dt.tzinfo is not None:
        offset = dt.strftime("%z")
        offset = offset[:3] + ":" + offset[3:] if offset else None
        dt = dt.astimezone(timezone.utc)
    else:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ"), offset


def idem_key_for(record: dict[str, Any]) -> str:
    """Stable content hash used when a record carries no natural idempotency key."""
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"))
    return "h:" + hashlib.sha256(canonical.encode()).hexdigest()[:32]


def _nonneg_tokens(event: dict[str, Any], key: str) -> int:
    """Token counts can't be negative — a typo'd record must not silently
    offset real spend with negative costs. Clamp to 0."""
    return max(int(event.get(key, 0) or 0), 0)


def insert_event(conn: sqlite3.Connection, event: dict[str, Any]) -> bool:
    """Insert one canonical event. Returns True if inserted, False if duplicate."""
    ts_utc, ts_offset = parse_ts(event["ts"])
    row = {
        "idem_key": event["idem_key"],
        "ts_utc": ts_utc,
        "ts_offset": ts_offset,
        "provider": event["provider"],
        "model_raw": event["model_raw"],
        "sku": f"{event['provider']}:{event['model_raw']}",
        "input_tokens": _nonneg_tokens(event, "input_tokens"),
        "cache_read_tokens": _nonneg_tokens(event, "cache_read_tokens"),
        "cache_write_tokens": _nonneg_tokens(event, "cache_write_tokens"),
        "output_tokens": _nonneg_tokens(event, "output_tokens"),
        "cost_micro_usd": event.get("cost_micro_usd"),
        "is_correction": 1 if event.get("is_correction") else 0,
        "task": event.get("task") or "unattributed",
        "agent": event.get("agent") or "unknown",
        "team": event.get("team") or "unassigned",
        "metadata_json": json.dumps(event.get("metadata") or {}),
        "ingested_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        conn.execute(
            """INSERT INTO usage_events
               (idem_key, ts_utc, ts_offset, provider, model_raw, sku,
                input_tokens, cache_read_tokens, cache_write_tokens, output_tokens,
                cost_micro_usd, is_correction, task, agent, team, metadata_json, ingested_at)
               VALUES (:idem_key, :ts_utc, :ts_offset, :provider, :model_raw, :sku,
                :input_tokens, :cache_read_tokens, :cache_write_tokens, :output_tokens,
                :cost_micro_usd, :is_correction, :task, :agent, :team, :metadata_json,
                :ingested_at)""",
            row,
        )
        return True
    except sqlite3.IntegrityError:
        return False  # duplicate idem_key — skip, no double-count


def query(
    conn: sqlite3.Connection, sql: str, params: tuple = ()
) -> list[sqlite3.Row]:
    return list(conn.execute(sql, params))
