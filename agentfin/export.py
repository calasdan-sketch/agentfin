"""Export the ledger: FOCUS-aligned CSV for FinOps tools, or raw events.

`focus` writes one row per (UTC day, SKU, task, agent, team) using column
names from the FinOps Open Cost and Usage Specification (FOCUS,
https://focus.finops.org). It is *aligned with*, not certified against, the
spec: agentfin knows token usage and list-price cost, not your invoice IDs,
billing accounts or negotiated discounts, so those columns are omitted and
ListCost = EffectiveCost = BilledCost. FOCUS requires BilledCost, so a group
containing unpriced events is written with cost 0 and `x_Unpriced = true`, and
`agentfin export` prints a warning naming how many rows that affected.

All text cells are neutralized against spreadsheet formula injection
(a task named `=HYPERLINK(...)` is written as `'=HYPERLINK(...)`).
"""

from __future__ import annotations

import calendar
import csv
import json
import re
import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta
from typing import IO, Any

from . import reports

FOCUS_COLUMNS = [
    "BillingPeriodStart", "BillingPeriodEnd", "ChargePeriodStart", "ChargePeriodEnd",
    "ChargeCategory", "ChargeDescription", "ServiceCategory", "ServiceName",
    "ProviderName", "PublisherName", "SkuId", "ConsumedQuantity", "ConsumedUnit",
    "ListCost", "EffectiveCost", "BilledCost", "BillingCurrency", "Tags",
    "x_InputTokens", "x_CacheReadTokens", "x_CacheWriteTokens", "x_OutputTokens",
    "x_Events", "x_Unpriced",
]

EVENT_COLUMNS = [
    "id", "ts_utc", "provider", "model_raw", "sku", "input_tokens",
    "cache_read_tokens", "cache_write_tokens", "output_tokens", "cost_micro_usd",
    "is_correction", "task", "agent", "team", "metadata_json", "idem_key",
]

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def safe_cell(value: Any) -> Any:
    """Prefix text that a spreadsheet would execute as a formula (OWASP CSV injection).

    Plain numbers (e.g. a negative correction "-1.500000") are left intact."""
    # Leading whitespace is checked too: some spreadsheets trim before evaluating.
    if (isinstance(value, str) and value.lstrip().startswith(_FORMULA_PREFIXES)
            and not _NUMBER.fullmatch(value)):
        return "'" + value
    return value


def _usd(micro: int | None) -> str:
    return "" if micro is None else f"{micro / 1_000_000:.6f}"


def _iso(d: date) -> str:
    return f"{d.isoformat()}T00:00:00Z"


def focus_rows(
    conn: sqlite3.Connection, date_from: str | None = None, date_to: str | None = None
) -> Iterator[dict[str, Any]]:
    where, params = reports.time_where(date_from, date_to)
    cur = conn.execute(
        f"""SELECT substr(ts_utc, 1, 10) AS day, provider, model_raw, sku,
                   task, agent, team, COUNT(*) AS events,
                   SUM(input_tokens) AS inp, SUM(cache_read_tokens) AS cr,
                   SUM(cache_write_tokens) AS cw, SUM(output_tokens) AS outp,
                   SUM(cost_micro_usd) AS cost,
                   SUM(CASE WHEN cost_micro_usd IS NULL THEN 1 ELSE 0 END) AS unpriced
            FROM usage_events WHERE {where}
            GROUP BY day, provider, model_raw, sku, task, agent, team
            ORDER BY day, sku, task, agent, team""",
        params,
    )
    for r in cur:
        day = date.fromisoformat(r["day"])
        last = calendar.monthrange(day.year, day.month)[1]
        tokens = (r["inp"] or 0) + (r["cr"] or 0) + (r["cw"] or 0) + (r["outp"] or 0)
        # FOCUS requires BilledCost. Groups containing unpriced events get 0 and
        # x_Unpriced=true (the CLI also warns) — filter on x_Unpriced before totals.
        cost = 0 if r["unpriced"] else (r["cost"] or 0)
        yield {
            "BillingPeriodStart": _iso(day.replace(day=1)),
            "BillingPeriodEnd": _iso(day.replace(day=last) + timedelta(days=1)),
            "ChargePeriodStart": _iso(day),
            "ChargePeriodEnd": _iso(day + timedelta(days=1)),
            "ChargeCategory": "Usage",
            "ChargeDescription": f"{tokens} tokens of {r['model_raw']}",
            "ServiceCategory": "AI and Machine Learning",
            "ServiceName": r["provider"],
            "ProviderName": r["provider"],
            "PublisherName": r["provider"],
            "SkuId": r["sku"],
            "ConsumedQuantity": tokens,
            "ConsumedUnit": "Tokens",
            "ListCost": _usd(cost),
            "EffectiveCost": _usd(cost),
            "BilledCost": _usd(cost),
            "BillingCurrency": "USD",
            "Tags": json.dumps(
                {"task": r["task"], "agent": r["agent"], "team": r["team"]},
                sort_keys=True,
            ),
            "x_InputTokens": r["inp"] or 0,
            "x_CacheReadTokens": r["cr"] or 0,
            "x_CacheWriteTokens": r["cw"] or 0,
            "x_OutputTokens": r["outp"] or 0,
            "x_Events": r["events"],
            "x_Unpriced": "true" if r["unpriced"] else "false",
        }


def event_rows(
    conn: sqlite3.Connection, date_from: str | None = None, date_to: str | None = None
) -> Iterator[dict[str, Any]]:
    where, params = reports.time_where(date_from, date_to)
    cols = ", ".join(EVENT_COLUMNS)
    for r in conn.execute(
        f"SELECT {cols} FROM usage_events WHERE {where} ORDER BY ts_utc, id", params
    ):
        yield dict(r)


def write_csv(rows: Iterator[dict[str, Any]], columns: list[str], fh: IO[str]) -> int:
    writer = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    n = 0
    for row in rows:
        writer.writerow({k: safe_cell(v) for k, v in row.items()})
        n += 1
    return n


def write_jsonl(rows: Iterator[dict[str, Any]], fh: IO[str]) -> int:
    n = 0
    for row in rows:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
        n += 1
    return n
