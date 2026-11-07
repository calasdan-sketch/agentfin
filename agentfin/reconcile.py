"""Reconcile ledger totals against a provider invoice CSV.

Invoice CSV: one row per charge with a SKU column and a USD cost column.
Accepted headers (case-insensitive): ``sku`` | ``model``  and
``cost_usd`` | ``amount_usd`` | ``cost`` | ``amount``. A file with no
recognisable columns is an error, never "invoice total $0".

Reports per-SKU ledger total vs invoiced amount and variance, optionally
restricted to the invoice period (``date_from`` / ``date_to``, same rules as
``report``) because comparing a whole ledger to one month's invoice is a false
alarm. Money is summed as ``Decimal`` so cents do not drift.
"""

from __future__ import annotations

import csv
import sqlite3
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from . import reports, textsafe

SKU_COLUMNS = ("sku", "model")
COST_COLUMNS = ("cost_usd", "amount_usd", "cost", "amount")
MAX_ROWS = 5_000_000


def _pick(fieldnames: list[str], candidates: tuple[str, ...]) -> str | None:
    lowered = {(n or "").strip().lower(): n for n in fieldnames}
    for c in candidates:
        if c in lowered:
            return lowered[c]
    return None


def read_invoice(invoice_path: str | Path) -> dict[str, Decimal]:
    invoiced: dict[str, Decimal] = {}
    try:
        f = open(invoice_path, newline="", encoding="utf-8-sig")  # noqa: SIM115
    except OSError as exc:
        raise ValueError(f"cannot read {invoice_path}: {exc.strerror}") from None
    with f:
        reader = csv.DictReader(f)
        try:
            names = list(reader.fieldnames or [])
        except (UnicodeDecodeError, csv.Error) as exc:
            raise ValueError(f"{invoice_path}: not a UTF-8 CSV ({exc})") from None
        sku_col, cost_col = _pick(names, SKU_COLUMNS), _pick(names, COST_COLUMNS)
        if sku_col is None or cost_col is None:
            raise ValueError(
                f"{invoice_path}: need a SKU column ({' | '.join(SKU_COLUMNS)}) and a "
                f"USD cost column ({' | '.join(COST_COLUMNS)}); found "
                f"{[textsafe.clean(n, 30) for n in names][:8]}"
            )
        try:
            for i, row in enumerate(reader, start=2):  # header is row 1
                if i > MAX_ROWS:
                    raise ValueError(f"{invoice_path}: more than {MAX_ROWS} rows")
                sku = (row.get(sku_col) or "").strip()
                if not sku:
                    continue
                raw = (row.get(cost_col) or "").strip().replace(",", "").lstrip("$")
                try:
                    cost = Decimal(raw or "0")
                except InvalidOperation:
                    cost = None
                if cost is None or not cost.is_finite():
                    raise ValueError(
                        f"{invoice_path}: row {i}: bad {cost_col} "
                        f"{textsafe.clean(raw, 40)!r} (sku {textsafe.clean(sku, 60)!r})"
                    )
                invoiced[sku] = invoiced.get(sku, Decimal(0)) + cost
        except (UnicodeDecodeError, csv.Error) as exc:
            raise ValueError(f"{invoice_path}: not a UTF-8 CSV ({exc})") from None
    return invoiced


def reconcile(
    conn: sqlite3.Connection,
    invoice_path: str | Path,
    date_from: str | None = None,
    date_to: str | None = None,
    tolerance_usd: float = 0.01,
) -> dict[str, Any]:
    invoiced = read_invoice(invoice_path)
    tol = Decimal(str(tolerance_usd))
    where, params = reports.time_where(date_from, date_to)
    ledger_rows = conn.execute(
        f"""SELECT sku, SUM(cost_micro_usd) AS cost,
                   SUM(CASE WHEN cost_micro_usd IS NULL THEN 1 ELSE 0 END) AS unpriced
            FROM usage_events WHERE {where} GROUP BY sku""",
        params,
    ).fetchall()
    ledger_totals = {r["sku"]: Decimal(r["cost"] or 0) / 1_000_000 for r in ledger_rows}
    unpriced = {r["sku"]: r["unpriced"] for r in ledger_rows if r["unpriced"]}
    lines = []
    for sku in sorted(set(invoiced) | set(ledger_totals)):
        inv = invoiced.get(sku, Decimal(0))
        led = ledger_totals.get(sku, Decimal(0))
        variance = led - inv
        if sku not in ledger_totals:
            where_only = "invoice only"
        elif sku not in invoiced:
            where_only = "ledger only"
        else:
            where_only = ""
        lines.append(
            {
                "sku": textsafe.clean(sku, 120),
                "ledger_usd": float(round(led, 4)),
                "invoiced_usd": float(round(inv, 4)),
                "variance_usd": float(round(variance, 4)),
                "variance_pct": float(round(variance / inv * 100, 2)) if inv else None,
                # An SKU with unpriced events can never be called a match: its
                # ledger total is missing money.
                "match": abs(variance) <= tol and sku not in unpriced,
                "unpriced_events": unpriced.get(sku, 0),
                "note": where_only,
            }
        )
    total_variance = sum(Decimal(str(row["variance_usd"])) for row in lines)
    return {
        "lines": sorted(lines, key=lambda row: abs(row["variance_usd"]), reverse=True),
        "total_variance_usd": float(round(total_variance, 4)),
        "all_match": all(row["match"] for row in lines),
        "tolerance_usd": float(tol),
    }
