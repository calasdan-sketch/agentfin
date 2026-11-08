"""Static HTML dashboard export — one self-contained file, no server."""

from __future__ import annotations

import html
import sqlite3
from datetime import datetime, timezone

from . import __author__, __version__, anomalies, budgets, outcomes, pricing, reports


def _usd(micro) -> str:
    return pricing.format_usd(micro)


def generate(
    conn: sqlite3.Connection,
    budget_list: list[dict],
    days: int = 30,
    title: str = "agentfin dashboard",
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    tot = reports.totals(conn)
    by_team = reports.summary(conn, "team")
    by_agent = reports.summary(conn, "agent")
    by_task = reports.summary(conn, "task")
    series = reports.daily_series(conn, days=days)
    anoms = anomalies.detect(conn, record=False)
    bstat = budgets.check(conn, budget_list)
    roi = outcomes.roi_report(conn)

    def table(headers, rows):
        th = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
        tr = "".join(
            "<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in r) + "</tr>"
            for r in rows
        )
        return f"<table><tr>{th}</tr>{tr}</table>"

    max_day = max((r["cost_micro_usd"] or 0) for r in series) if series else 1
    bars = "".join(
        "<div class='bar' "
        f"style='height:{(r['cost_micro_usd'] or 0) / max_day * 100:.1f}%'"
        f" title='{r['day']}: {_usd(r['cost_micro_usd'])}'></div>"
        for r in series
    )

    def agg_rows(rows):
        return [
            (r["dim"], _usd(r["cost_micro_usd"]), f"{r['events']:,}",
             "⚠ unpriced" if r["unpriced_events"] else "")
            for r in rows
        ]

    anom_rows = [
        (
            a["scope"],
            a["day"],
            _usd(a["spend_micro_usd"]),
            f"{a['multiple']}x",
            ", ".join(c["task"] for c in a["probable_cause"][:3]),
        )
        for a in anoms
    ]
    budget_rows = [
        (
            b["scope"],
            f"${b['monthly_usd']:,.0f}",
            _usd(b["spent_micro_usd"]),
            _usd(b["forecast_micro_usd"]),
            f"{b['forecast_pct_of_budget']}%",
            "🔴 ALERT" if b["alert"] else "🟢 ok",
        )
        for b in bstat
    ]
    roi_rows = [
        (
            r["task"],
            _usd(r["cost_micro_usd"]),
            _usd(r["value_micro_usd"]),
            "∞" if r["roi"] == float("inf") else f"{r['roi']:.2f}x",
        )
        for r in roi
    ]

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#1a1a1a}}
h1{{font-size:1.6rem}}h2{{margin-top:2rem;font-size:1.2rem;border-bottom:2px solid #eee;padding-bottom:.3rem}}
table{{border-collapse:collapse;width:100%;margin:.5rem 0}}th,td{{border:1px solid #ddd;padding:.4rem .6rem;text-align:left;font-size:.9rem}}
th{{background:#f5f5f5}}.kpi{{display:flex;gap:2rem;margin:1rem 0}}.kpi div{{font-size:1.4rem;font-weight:700}}
.kpi span{{display:block;font-size:.75rem;font-weight:400;color:#666}}.bars{{display:flex;align-items:flex-end;gap:2px;height:120px}}
.bar{{flex:1;background:#2563eb;min-width:3px}}.muted{{color:#666;font-size:.85rem}}
</style></head><body>
<h1>💰 {html.escape(title)}</h1>
<p class="muted">Generated {now} · agentfin {__version__} · {html.escape(__author__)}</p>
<h2>Totals</h2>
<div class="kpi">
<div>{_usd(tot['cost_micro_usd'])}<span>total spend</span></div>
<div>{tot['events']:,}<span>events</span></div>
<div>{(tot['tokens'] or 0):,}<span>tokens</span></div>
<div>{tot['unpriced_events'] or 0:,}<span>unpriced events</span></div>
</div>
<h2>Spend trend (last {days} days)</h2>
<div class="bars">{bars}</div>
<h2>Budgets</h2>
{table(["Scope","Budget","Spent MTD","Forecast","% of budget","Status"],budget_rows) if budget_rows else "<p class='muted'>No budgets configured.</p>"}
<h2>Anomalies</h2>
{table(["Scope","Day","Spend","Multiple","Probable cause"],anom_rows) if anom_rows else "<p class='muted'>No anomalies detected.</p>"}
<h2>ROI — cost per outcome</h2>
{table(["Task","Cost","Outcome value","ROI"],roi_rows) if roi_rows else "<p class='muted'>No outcomes tagged yet.</p>"}
<h2>By team</h2>
{table(["Team","Cost","Events",""],agg_rows(by_team))}
<h2>By agent</h2>
{table(["Agent","Cost","Events",""],agg_rows(by_agent))}
<h2>By task</h2>
{table(["Task","Cost","Events",""],agg_rows(by_task))}
</body></html>
"""
