"""Outcomes & ROI: tag tasks with monetized business outcomes, cost-per-outcome.

Outcome kinds: revenue (dollars earned), hours_saved (converted at an hourly
rate from config), tickets (converted at a per-ticket value), custom.
ROI = value / cost. A task with outcomes but no cost is reported with
infinite ROI flagged explicitly, never as a crash.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

KIND_DEFAULTS = {"hours_saved": 75.0, "tickets": 25.0}  # USD per unit


def tag_outcome(
    conn: sqlite3.Connection,
    task: str,
    kind: str,
    value: float,
    note: str = "",
    ts: str | None = None,
    hourly_rate_usd: float = 75.0,
    ticket_value_usd: float = 25.0,
) -> None:
    """Record an outcome. For hours_saved/tickets, `value` is in natural units
    and converted to USD; for revenue/custom, `value` is already USD."""
    ts = ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if kind == "hours_saved":
        usd = value * hourly_rate_usd
    elif kind == "tickets":
        usd = value * ticket_value_usd
    else:
        usd = value
    conn.execute(
        """INSERT OR IGNORE INTO outcomes (task, kind, value_usd_micro, note, ts_utc)
           VALUES (?, ?, ?, ?, ?)""",
        (task, kind, int(round(usd * 1_000_000)), note, ts),
    )
    conn.commit()


def roi_report(
    conn: sqlite3.Connection,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    """Cost-per-outcome per task, ROI ratio, worst-first ordering."""
    where, params = "1=1", []
    if date_from:
        where += " AND ts_utc >= ?"
        params.append(date_from)
    if date_to:
        where += " AND ts_utc <= ?"
        params.append(date_to)
    costs = {
        r["task"]: r["cost"]
        for r in conn.execute(
            f"""SELECT task, SUM(cost_micro_usd) AS cost FROM usage_events
                WHERE {where} GROUP BY task""",
            params,
        ).fetchall()
    }
    owhere, oparams = "1=1", []
    if date_from:
        owhere += " AND ts_utc >= ?"
        oparams.append(date_from)
    if date_to:
        owhere += " AND ts_utc <= ?"
        oparams.append(date_to)
    values = {
        r["task"]: r["value"]
        for r in conn.execute(
            f"""SELECT task, SUM(value_usd_micro) AS value FROM outcomes
                WHERE {owhere} GROUP BY task""",
            oparams,
        ).fetchall()
    }
    rows = []
    for task in sorted(set(costs) | set(values)):
        cost = costs.get(task) or 0
        value = values.get(task) or 0
        roi = (value / cost) if cost else (float("inf") if value else 0.0)
        rows.append(
            {
                "task": task,
                "cost_micro_usd": cost,
                "value_micro_usd": value,
                "roi": roi,
                "cost_per_dollar": round(cost / value, 4) if value else None,
            }
        )
    return sorted(
        rows,
        key=lambda r: (r["roi"] == float("inf"), r["roi"]),
    )
