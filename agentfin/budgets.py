"""Budgets: monthly caps per team/agent, burn-rate forecast, pre-overrun alerts.

Forecast = (spend so far this month / elapsed days) * days in month.
Alerts fire at the configured threshold fraction of the forecast — i.e. you
are told you WILL blow the budget, not that you already did.
"""

from __future__ import annotations

import calendar
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

DEFAULT_BUDGETS = """\
# agentfin budgets — monthly caps in USD.
# alert_at: fraction of *forecasted* month-end spend that triggers an alert.
budgets:
  - scope: team:platform
    monthly_usd: 500
    alert_at: 0.8
"""


def load_budgets(path: str | Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    p = Path(path)
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return data.get("budgets", []) or []


def save_budgets(path: str | Path, blist: list[dict[str, Any]]) -> None:
    """Write the budget list back to budgets.yaml (used by `budgets set`).

    Preserves nothing but the data itself — a header comment is re-added.
    Hand-edits stay valid YAML, so re-saving after manual edits is safe.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "# agentfin budgets — monthly caps in USD.\n"
        "# alert_at: fraction of *forecasted* month-end spend that triggers an alert.\n"
        + yaml.safe_dump({"budgets": blist}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _month_bounds(now: datetime) -> tuple[str, str, int, int]:
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    end = start.replace(
        day=days_in_month, hour=23, minute=59, second=59, microsecond=0
    )
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return start.strftime(fmt), end.strftime(fmt), now.day, days_in_month


BUDGET_DIMENSIONS = ("team", "agent", "task")


def invalid_scopes(budgets: list[dict[str, Any]]) -> list[str]:
    """Budget entries `check()` cannot evaluate (bad scope or amount) — these
    used to be skipped silently, which made a typo'd budget look 'ok'."""
    bad = []
    for b in budgets:
        scope = str(b.get("scope", "")) if isinstance(b, dict) else repr(b)
        dim, sep, value = scope.partition(":")
        try:
            amount_ok = float(b.get("monthly_usd")) > 0
        except (AttributeError, TypeError, ValueError):
            amount_ok = False
        if not sep or not value or dim not in BUDGET_DIMENSIONS or not amount_ok:
            bad.append(scope or "<missing scope>")
    return bad


def _spend_for_scope(
    conn: sqlite3.Connection, dimension: str, value: str, start: str, end: str
) -> tuple[int, int]:
    """(priced spend in micro-USD, number of UNPRICED events) for the window."""
    row = conn.execute(
        f"""SELECT COALESCE(SUM(cost_micro_usd), 0) AS s,
                   COALESCE(SUM(CASE WHEN cost_micro_usd IS NULL THEN 1 ELSE 0 END), 0) AS u
            FROM usage_events
            WHERE {dimension} = ? AND ts_utc >= ? AND ts_utc <= ?""",
        (value, start, end),
    ).fetchone()
    return int(row["s"] or 0), int(row["u"] or 0)


def check(
    conn: sqlite3.Connection,
    budgets: list[dict[str, Any]],
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Evaluate all budgets. Returns status dicts, alerting ones first."""
    now = now or datetime.now(timezone.utc)
    start, end, elapsed, days_in_month = _month_bounds(now)
    results = []
    for b in budgets:
        if invalid_scopes([b]):
            continue  # reported by invalid_scopes(); never evaluated as "ok"
        scope = b["scope"]
        dim, _, value = scope.partition(":")
        monthly_micro = int(round(float(b["monthly_usd"]) * 1_000_000))
        spent, unpriced = _spend_for_scope(conn, dim, value, start, end)
        daily_rate = spent / max(elapsed, 1)
        forecast = int(round(daily_rate * days_in_month))
        threshold = float(b.get("alert_at", 0.8))
        breached = forecast >= monthly_micro * threshold
        results.append(
            {
                "scope": scope,
                "monthly_usd": b["monthly_usd"],
                "spent_micro_usd": spent,
                # Spend on SKUs missing from pricing.yaml is unknown, not zero.
                "unpriced_events": unpriced,
                "forecast_micro_usd": forecast,
                "forecast_low_confidence": elapsed < 3,
                "forecast_pct_of_budget": round(forecast / monthly_micro * 100, 1)
                if monthly_micro
                else 0,
                "alert": breached,
                "alert_key": f"budget:{scope}",
                "month": start[:7],
            }
        )
    # Fire (and persist) alert state for breached budgets.
    for r in results:
        if not r["alert"]:
            continue
        conn.execute(
            """INSERT INTO alert_state (alert_key, fired_at, detail_json)
               VALUES (?, ?, ?)
               ON CONFLICT(alert_key) DO UPDATE SET
                 fired_at = excluded.fired_at, dismissed = 0,
                 detail_json = excluded.detail_json""",
            (
                r["alert_key"],
                now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                json.dumps(r),
            ),
        )
    conn.commit()
    return sorted(results, key=lambda r: (not r["alert"], -r["forecast_pct_of_budget"]))
