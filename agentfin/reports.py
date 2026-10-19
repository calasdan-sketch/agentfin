"""Aggregations: per agent/task/team/model, time ranges, trends."""

from __future__ import annotations

import sqlite3
from typing import Any

DIMENSIONS = {"agent", "task", "team", "model_raw", "provider", "sku"}


def scope_filter(
    dimension: str | None, value: str | None
) -> tuple[str, tuple]:
    if dimension is not None and dimension not in DIMENSIONS:
        raise ValueError(f"unknown dimension: {dimension}")
    if dimension and value:
        return f"AND {dimension} = ?", (value,)
    return "", ()


def summary(
    conn: sqlite3.Connection,
    by: str,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    if by not in DIMENSIONS:
        raise ValueError(f"unknown dimension: {by}")
    where, params = "1=1", []
    if date_from:
        where += " AND ts_utc >= ?"
        params.append(date_from)
    if date_to:
        where += " AND ts_utc <= ?"
        params.append(date_to)
    rows = conn.execute(
        f"""SELECT {by} AS dim,
                   COUNT(*) AS events,
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
    where, params = "1=1", []
    if date_from:
        where += " AND ts_utc >= ?"
        params.append(date_from)
    if date_to:
        where += " AND ts_utc <= ?"
        params.append(date_to)
    row = conn.execute(
        f"""SELECT COUNT(*) AS events,
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
                   COUNT(*) AS events
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
                   COUNT(*) AS events
            FROM usage_events
            WHERE substr(ts_utc, 1, 10) = ?
            GROUP BY {by} ORDER BY cost_micro_usd DESC NULLS LAST LIMIT ?""",
        (day, limit),
    ).fetchall()
    return [dict(r) for r in rows]
