"""Showback / chargeback: spread shared and unattributed spend over the teams that
caused the attributed spend.

`direct` is what a team's own events cost. The pool (events tagged `unassigned`
by default) is split in proportion to each team's direct spend, with exact
integer arithmetic: the loaded totals always sum to the ledger total.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import money, reports


def allocate(
    conn: sqlite3.Connection, date_from: str | None = None, date_to: str | None = None,
    pool: str = "unassigned", by: str = "team",
) -> dict[str, Any]:
    if by not in reports.DIMENSIONS:
        raise ValueError(f"unknown dimension: {by}")
    where, params = reports.time_where(date_from, date_to)
    rows = conn.execute(
        f"""SELECT {by} AS k, SUM(cost_micro_usd) AS cost,
                   SUM(CASE WHEN cost_micro_usd IS NULL THEN 1 ELSE 0 END) AS unpriced
            FROM usage_events WHERE {where} GROUP BY {by} ORDER BY {by}""", params).fetchall()
    direct = {r["k"]: int(r["cost"] or 0) for r in rows if r["k"] != pool}
    pool_cost = next((int(r["cost"] or 0) for r in rows if r["k"] == pool), 0)
    unpriced = sum(int(r["unpriced"] or 0) for r in rows)
    positive = {k: v for k, v in direct.items() if v > 0}
    if pool_cost and not positive:
        raise ValueError(f"nothing to allocate {pool!r} across: no other {by} has priced spend")
    shares = money.apportion(pool_cost, positive) if pool_cost else {}
    out = [{by: k, "direct_micro_usd": v, "allocated_micro_usd": shares.get(k, 0),
            "loaded_micro_usd": v + shares.get(k, 0)} for k, v in sorted(direct.items())]
    return {"by": by, "pool": pool, "pool_micro_usd": pool_cost, "rows": out,
            "total_micro_usd": sum(r["loaded_micro_usd"] for r in out),
            "unpriced_events": unpriced}
