"""Aggregations: per agent/task/team/model, time ranges, trends."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from . import ledger

DIMENSIONS = {"agent", "task", "team", "model_raw", "provider", "sku"}


def scope_filter(
    dimension: str | None, value: str | None
) -> tuple[str, tuple]:
    if dimension is not None and dimension not in DIMENSIONS:
        raise ValueError(f"unknown dimension: {dimension}")
    if dimension and value:
        return f"AND {dimension} = ?", (value,)
    return "", ()


_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def time_where(
    date_from: str | None, date_to: str | None, column: str = "ts_utc"
) -> tuple[str, list[str]]:
    """SQL fragment for an inclusive [from, to] window over UTC ISO timestamps.

    A bare date for `date_to` (YYYY-MM-DD) means *through the end of that day*
    (previously it silently dropped every event on that day after 00:00:00).
    Anything else is normalized via `ledger.parse_ts`, so offsets work and
    garbage raises ValueError instead of producing a silent string compare.
    """
    where, params = "1=1", []
    if date_from:
        raw = f"{date_from}T00:00:00Z" if _DATE_ONLY.match(date_from) else date_from
        start = ledger.parse_ts(raw)[0]
        where += f" AND {column} >= ?"
        params.append(start)
    if date_to:
        if _DATE_ONLY.match(date_to):
            ledger.parse_ts(f"{date_to}T00:00:00Z")  # validate
            where += f" AND substr({column}, 1, 10) <= ?"
            params.append(date_to)
        else:
            where += f" AND {column} <= ?"
            params.append(ledger.parse_ts(date_to)[0])
    return where, params


def summary(
    conn: sqlite3.Connection,
    by: str,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    if by not in DIMENSIONS:
        raise ValueError(f"unknown dimension: {by}")
    where, params = time_where(date_from, date_to)
    rows = conn.execute(
        f"""SELECT {by} AS dim,
                   COALESCE(SUM(CASE WHEN is_correction = 0 THEN 1 ELSE 0 END), 0) AS events,
                   SUM(input_tokens) AS input_tokens,
                   SUM(cache_read_tokens) AS cache_read_tokens,
                   SUM(cache_write_tokens) AS cache_write_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(cost_micro_usd) AS cost_micro_usd,
                   SUM(CASE WHEN cost_micro_usd IS NULL THEN 1 ELSE 0 END) AS unpriced_events
            FROM usage_events WHERE {where}
            GROUP BY {by} ORDER BY cost_micro_usd DESC NULLS LAST""",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def totals(conn: sqlite3.Connection, date_from=None, date_to=None) -> dict[str, Any]:
    where, params = time_where(date_from, date_to)
    row = conn.execute(
        f"""SELECT COALESCE(SUM(CASE WHEN is_correction = 0 THEN 1 ELSE 0 END), 0) AS events,
                   SUM(input_tokens + cache_read_tokens + cache_write_tokens
                       + output_tokens) AS tokens,
                   SUM(cost_micro_usd) AS cost_micro_usd,
                   SUM(CASE WHEN cost_micro_usd IS NULL THEN 1 ELSE 0 END)
                       AS unpriced_events
            FROM usage_events WHERE {where}""",
        params,
    ).fetchone()
    return dict(row)


def daily_series(
    conn: sqlite3.Connection,
    dimension: str | None = None,
    value: str | None = None,
    days: int = 30,
) -> list[dict[str, Any]]:
    extra, params = scope_filter(dimension, value)
    rows = conn.execute(
        f"""SELECT substr(ts_utc, 1, 10) AS day,
                   SUM(cost_micro_usd) AS cost_micro_usd,
                   COALESCE(SUM(CASE WHEN is_correction = 0 THEN 1 ELSE 0 END), 0) AS events
            FROM usage_events
            WHERE ts_utc >= date('now', ? || ' days') {extra}
            GROUP BY day ORDER BY day""",
        (f"-{days}", *params),
    ).fetchall()
    return [dict(r) for r in rows]


def top_contributors(
    conn: sqlite3.Connection,
    day: str,
    by: str = "agent",
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Who drove spend on a given day — probable-cause for anomalies."""
    if by not in DIMENSIONS:
        raise ValueError(f"unknown dimension: {by}")
    rows = conn.execute(
        f"""SELECT {by} AS dim, SUM(cost_micro_usd) AS cost_micro_usd,
                   COALESCE(SUM(CASE WHEN is_correction = 0 THEN 1 ELSE 0 END), 0) AS events
            FROM usage_events
            WHERE substr(ts_utc, 1, 10) = ?
            GROUP BY {by} ORDER BY cost_micro_usd DESC NULLS LAST LIMIT ?""",
        (day, limit),
    ).fetchall()
    return [dict(r) for r in rows]
