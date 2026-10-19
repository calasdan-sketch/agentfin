"""Anomaly detection on daily spend per scope.

Robust z-score (median / MAD) so one spike doesn't poison its own baseline.
Gates: minimum baseline days, minimum multiple of median, 24h cooldown per
scope. Every alert ships with probable cause (top contributors that day).
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from datetime import datetime, timezone
from typing import Any

from . import reports

MIN_BASELINE_DAYS = 14
Z_THRESHOLD = 3.5
MIN_MULTIPLE = 2.0
COOLDOWN_HOURS = 24


def robust_z(value: float, baseline: list[float]) -> float:
    med = statistics.median(baseline)
    mad = statistics.median([abs(x - med) for x in baseline])
    if mad == 0:
        return float("inf") if value != med else 0.0
    return 0.6745 * (value - med) / mad


def _cooldown_ok(conn: sqlite3.Connection, alert_key: str) -> bool:
    row = conn.execute(
        "SELECT fired_at, dismissed FROM alert_state WHERE alert_key = ?",
        (alert_key,),
    ).fetchone()
    if not row:
        return True
    if row["dismissed"]:
        return False
    fired = datetime.fromisoformat(row["fired_at"].replace("Z", "+00:00"))
    hours = (datetime.now(timezone.utc) - fired).total_seconds() / 3600
    return hours >= COOLDOWN_HOURS


def _record_alert(conn: sqlite3.Connection, alert_key: str, detail: dict) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """INSERT INTO alert_state (alert_key, fired_at, detail_json)
           VALUES (?, ?, ?)
           ON CONFLICT(alert_key) DO UPDATE SET
             fired_at = excluded.fired_at, dismissed = 0,
             detail_json = excluded.detail_json""",
        (alert_key, now, json.dumps(detail)),
    )
    conn.commit()


def detect(
    conn: sqlite3.Connection,
    dimension: str = "agent",
    days: int = 45,
    min_baseline_days: int = MIN_BASELINE_DAYS,
    z_threshold: float = Z_THRESHOLD,
    min_multiple: float = MIN_MULTIPLE,
    record: bool = True,
) -> list[dict[str, Any]]:
    """Return anomalies for the most recent complete day per scope value."""
    if dimension not in reports.DIMENSIONS:
        raise ValueError(f"unknown dimension: {dimension!r}")
    scopes = [r[0] for r in conn.execute(
        f"SELECT DISTINCT {dimension} FROM usage_events"
    ).fetchall()]
    alerts: list[dict[str, Any]] = []
    for scope in scopes:
        series = reports.daily_series(conn, dimension, scope, days=days)
        if len(series) < min_baseline_days + 1:
            continue
        *baseline_rows, today_row = series
        baseline = [float(r["cost_micro_usd"] or 0) for r in baseline_rows]
        today = float(today_row["cost_micro_usd"] or 0)
        med = statistics.median(baseline)
        if med <= 0 or today < med * min_multiple:
            continue
        z = robust_z(today, baseline)
        if abs(z) < z_threshold:
            continue
        alert_key = f"anomaly:{dimension}:{scope}"
        if not _cooldown_ok(conn, alert_key):
            continue
        contributors = reports.top_contributors(conn, today_row["day"], by="task")
        alert = {
            "scope": f"{dimension}={scope}",
            "day": today_row["day"],
            "spend_micro_usd": int(today),
            "baseline_median_micro_usd": int(med),
            "multiple": round(today / med, 2),
            "z": round(z, 2),
            "probable_cause": [
                {"task": c["dim"], "cost_micro_usd": c["cost_micro_usd"]}
                for c in contributors
            ],
        }
        if record:
            _record_alert(conn, alert_key, alert)
        alerts.append(alert)
    return sorted(alerts, key=lambda a: a["multiple"], reverse=True)


def dismiss(conn: sqlite3.Connection, alert_key: str) -> bool:
    cur = conn.execute(
        "UPDATE alert_state SET dismissed = 1 WHERE alert_key = ?", (alert_key,)
    )
    conn.commit()
    return cur.rowcount > 0


def list_alerts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT alert_key, fired_at, dismissed, detail_json FROM alert_state "
        "ORDER BY fired_at DESC"
    ).fetchall()
    return [
        {
            "alert_key": r["alert_key"],
            "fired_at": r["fired_at"],
            "dismissed": bool(r["dismissed"]),
            "detail": json.loads(r["detail_json"]),
        }
        for r in rows
    ]
