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
import re
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


_THOUSANDS = re.compile(r"^\d{1,3}(,\d{3})+(\.\d+)?$")
_EU_THOUSANDS = re.compile(r"^\d{1,3}(\.\d{3})+(,\d+)?$")


def parse_amount(raw: str, delimiter: str = ",") -> Decimal:
    """Parse one money cell exactly. Accepts ``$1,234.50``, ``(12.50)`` (accounting
    negative), ``-3``, and - only in a ``;`` or tab separated file - the decimal
    comma (``1.234,50`` or ``12,5``). Anything ambiguous raises ValueError, because
    reading ``1,50`` as 150 would be a silent 100x error."""
    t = raw.strip().replace("\u00a0", "").replace(" ", "")
    neg = False
    if t.startswith("(") and t.endswith(")"):
        neg, t = True, t[1:-1]
    if t.startswith("-"):
        neg, t = (not neg), t[1:]
    t = t.lstrip("$")
    if t.startswith("-"):
        neg, t = (not neg), t[1:]
    if not t:
        return Decimal(0)
    if "," in t:
        if _THOUSANDS.match(t) and delimiter == ",":
            t = t.replace(",", "")
        elif delimiter != "," and _EU_THOUSANDS.match(t):
            t = t.replace(".", "").replace(",", ".")
        elif delimiter != "," and re.fullmatch(r"\d+,\d{1,2}", t):
            t = t.replace(",", ".")
        else:
            raise ValueError("ambiguous thousands/decimal separator")
    try:
        val = Decimal(t)
    except InvalidOperation:
        raise ValueError("not a number") from None
    if not val.is_finite():
        raise ValueError("not a finite number")
    return -val if neg else val


def _open_invoice(invoice_path):
    """Open a CSV as text: UTF-8 (with or without BOM) or UTF-16 (PowerShell 5)."""
    with open(invoice_path, "rb") as raw:
        head = raw.read(2)
    enc = "utf-16" if head in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
    return open(invoice_path, newline="", encoding=enc)  # noqa: SIM115


def _delimiter(first_line: str) -> str:
    """Comma unless the header line clearly uses ; or tab (Excel in many locales)."""
    counts = {d: first_line.count(d) for d in (",", ";", "\t")}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] > 0 else ","


def read_invoice(invoice_path: str | Path) -> dict[str, Decimal]:
    invoiced: dict[str, Decimal] = {}
    try:
        f = _open_invoice(invoice_path)
    except OSError as exc:
        raise ValueError(f"cannot read {invoice_path}: {exc.strerror}") from None
    with f:
        try:
            first = f.readline()
            f.seek(0)
        except UnicodeDecodeError as exc:
            raise ValueError(f"{invoice_path}: not a UTF-8 CSV ({exc})") from None
        delim = _delimiter(first)
        reader = csv.DictReader(f, delimiter=delim)
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
                raw = (row.get(cost_col) or "").strip()
                try:
                    cost = parse_amount(raw, delim)
                except ValueError as exc:
                    raise ValueError(
                        f"{invoice_path}: row {i}: bad {cost_col} "
                        f"{textsafe.clean(raw, 40)!r} ({exc}; sku {textsafe.clean(sku, 60)!r})"
                    ) from None
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
