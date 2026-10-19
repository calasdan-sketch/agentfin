"""Reconcile ledger totals against a provider invoice CSV.

Invoice CSV columns: sku, cost_usd  (sku = provider:model).
Reports per-SKU ledger total vs invoiced amount and variance — the trust
anchor that proves agentfin's numbers match the real bill.
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from typing import Any


def reconcile(
    conn: sqlite3.Connection, invoice_path: str | Path
) -> dict[str, Any]:
    invoiced: dict[str, float] = {}
    with open(invoice_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, start=2):  # header is row 1
            sku = (row.get("sku") or "").strip()
            if not sku:
                continue
            raw_cost = row.get("cost_usd", 0) or 0
            try:
                cost = float(raw_cost)
            except (TypeError, ValueError):
                raise ValueError(
                    f"{invoice_path}: row {i}: bad cost_usd {raw_cost!r} "
                    f"(sku {sku!r})"
                ) from None
            invoiced[sku] = invoiced.get(sku, 0.0) + cost
    ledger_rows = conn.execute(
        """SELECT sku, SUM(cost_micro_usd) AS cost FROM usage_events
           GROUP BY sku"""
    ).fetchall()
    ledger_totals = {r["sku"]: (r["cost"] or 0) / 1_000_000 for r in ledger_rows}
    lines = []
    for sku in sorted(set(invoiced) | set(ledger_totals)):
        inv = invoiced.get(sku, 0.0)
        led = ledger_totals.get(sku, 0.0)
        variance = led - inv
        lines.append(
            {
                "sku": sku,
                "ledger_usd": round(led, 4),
                "invoiced_usd": round(inv, 4),
                "variance_usd": round(variance, 4),
                "variance_pct": round(variance / inv * 100, 2) if inv else None,
                "match": abs(variance) < 0.01,
            }
        )
    total_variance = sum(row["variance_usd"] for row in lines)
    return {
        "lines": sorted(lines, key=lambda row: abs(row["variance_usd"]), reverse=True),
        "total_variance_usd": round(total_variance, 4),
        "all_match": all(row["match"] for row in lines),
    }
