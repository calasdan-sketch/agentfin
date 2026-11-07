"""Aggregations: per agent/task/team/model, time ranges, trends."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


BUCKETS = ("day", "week", "month", "session")


def _bucket_label(minute_utc: str, by: str, tz) -> str:
    """Label for the UTC minute 'YYYY-MM-DDTHH:MM' in `tz`: its local date, ISO
    week (Monday, as the date it starts) or month."""
    try:
        local = datetime.strptime(minute_utc, "%Y-%m-%dT%H:%M").replace(
            tzinfo=timezone.utc).astimezone(tz)
    except (ValueError, OverflowError):
        raise ValueError(
            f"the ledger holds an event with an invalid date {minute_utc!r}; "
            "`agentfin doctor` counts such events") from None
    day = local.date()
    if by == "week":
        day = day - timedelta(days=day.weekday())
    return day.strftime("%Y-%m") if by == "month" else day.isoformat()


def summary_bucketed(
    conn: sqlite3.Connection,
    by: str,
    date_from: str | None = None,
    date_to: str | None = None,
    tz: str = "UTC",
) -> list[dict[str, Any]]:
    """Like `summary`, for time buckets (day / week / month, cut in `tz`, so a
    late-evening Chicago request lands on the Chicago day) or `session` (Claude
    Code session id). Rows are in time order for buckets, cost order for session.
    Grouped by UTC minute in SQL, then re-labelled, so a 100k-event ledger stays
    fast and half-hour zones like Asia/Kolkata are right."""
    if by not in BUCKETS:
        raise ValueError(f"unknown bucket: {by}")
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError(f"unknown time zone {tz!r}") from None
    # Bare dates mean whole days in `tz` (not UTC days), so `--from 2026-10-01
    # --tz America/Chicago` starts at Chicago midnight.
    if date_from and _DATE_ONLY.match(date_from):
        start = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=zone)
        date_from = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if date_to and _DATE_ONLY.match(date_to):
        end = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=zone) + timedelta(days=1)
        date_to = (end.astimezone(timezone.utc) - timedelta(seconds=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    where, params = time_where(date_from, date_to)
    sums = """
        COALESCE(SUM(CASE WHEN is_correction = 0 THEN 1 ELSE 0 END), 0) AS events,
        SUM(input_tokens) AS input_tokens, SUM(cache_read_tokens) AS cache_read_tokens,
        SUM(cache_write_tokens) AS cache_write_tokens, SUM(output_tokens) AS output_tokens,
        SUM(cost_micro_usd) AS cost_micro_usd,
        SUM(CASE WHEN cost_micro_usd IS NULL THEN 1 ELSE 0 END) AS unpriced_events"""
    if by == "session":
        try:
            conn.execute("SELECT json_extract('{}', '$.a')")
        except sqlite3.OperationalError:
            raise ValueError("--by session needs SQLite with JSON support") from None
        rows = conn.execute(
            f"""SELECT COALESCE(json_extract(metadata_json, '$.session_id'),
                                '(no session id)') AS dim, {sums}
                FROM usage_events WHERE {where} GROUP BY dim
                ORDER BY cost_micro_usd DESC NULLS LAST""", params).fetchall()
        return [dict(r) for r in rows]
    rows = conn.execute(
        f"""SELECT substr(ts_utc, 1, 16) AS minute, {sums}
            FROM usage_events WHERE {where} GROUP BY minute""", params).fetchall()
    merged: dict[str, dict[str, Any]] = {}
    for r in rows:
        label = _bucket_label(r["minute"], by, zone)
        acc = merged.setdefault(label, {"dim": label, "events": 0, "input_tokens": 0,
                                        "cache_read_tokens": 0, "cache_write_tokens": 0,
                                        "output_tokens": 0, "cost_micro_usd": 0,
                                        "unpriced_events": 0})
        for k in ("events", "input_tokens", "cache_read_tokens", "cache_write_tokens",
                  "output_tokens", "unpriced_events"):
            acc[k] += r[k] or 0
        acc["cost_micro_usd"] += r["cost_micro_usd"] or 0
    return [merged[k] for k in sorted(merged)]


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
