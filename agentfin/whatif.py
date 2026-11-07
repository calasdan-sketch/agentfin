"""Counterfactuals from your own price book (never from invented prices).

* `cache_savings` - what the same tokens would have cost if no prompt cache had
  been used (cache reads and writes billed as ordinary input). Positive = the
  cache saved money; negative = cache writes cost more than the reads saved.
* `model_swap`   - what the same tokens would have cost on another SKU. This is
  a price comparison only: another model may need more tokens, retries or
  produce worse results, so treat it as an upper bound on the saving.

Both walk events one at a time because tiered prices depend on each request's
prompt size. Events or SKUs without a price are counted and left out.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import pricing, reports

_FIELDS = ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens",
           "is_correction", "metadata_json")


def _events(conn: sqlite3.Connection, date_from, date_to, by: str):
    if by not in reports.DIMENSIONS:
        raise ValueError(f"unknown dimension: {by}")
    where, params = reports.time_where(date_from, date_to)
    for r in conn.execute(
        f"SELECT {by} AS k, sku, cost_micro_usd, {', '.join(_FIELDS)} "
        f"FROM usage_events WHERE {where}", params):
        ev = {f: r[f] for f in _FIELDS[:5]}
        try:
            meta = json.loads(r["metadata_json"] or "{}")
        except ValueError:
            meta = {}
        ev["cache_write_1h_tokens"] = int(meta.get("cache_write_1h_tokens") or 0) \
            if isinstance(meta, dict) else 0
        ev["service_tier"] = meta.get("service_tier") if isinstance(meta, dict) else None
        yield r["k"], r["sku"], r["cost_micro_usd"], ev


def cache_savings(conn, book, date_from=None, date_to=None, by="sku") -> dict[str, Any]:
    rows: dict[str, dict[str, int]] = {}
    skipped = 0
    for k, sku, actual, ev in _events(conn, date_from, date_to, by):
        if actual is None:
            skipped += 1
            continue
        no_cache = dict(ev)
        no_cache["input_tokens"] = ev["input_tokens"] + ev["cache_read_tokens"] \
            + ev["cache_write_tokens"]
        no_cache["cache_read_tokens"] = no_cache["cache_write_tokens"] = 0
        no_cache["cache_write_1h_tokens"] = 0
        cf = pricing.cost_micro_usd(sku, book, no_cache)
        if cf is None:
            skipped += 1
            continue
        row = rows.setdefault(k, {"actual": 0, "without_cache": 0, "cache_read_tokens": 0})
        row["actual"] += actual
        row["without_cache"] += cf
        row["cache_read_tokens"] += ev["cache_read_tokens"]
    out = [{by: k, "actual_micro_usd": v["actual"], "without_cache_micro_usd": v["without_cache"],
            "saved_micro_usd": v["without_cache"] - v["actual"],
            "cache_read_tokens": v["cache_read_tokens"]} for k, v in sorted(rows.items())]
    return {"by": by, "rows": out, "skipped_events": skipped}


def model_swap(conn, book, mapping: dict[str, str], date_from=None, date_to=None,
               by="sku") -> dict[str, Any]:
    rows: dict[str, dict[str, int]] = {}
    skipped = 0
    for k, sku, actual, ev in _events(conn, date_from, date_to, by):
        target = mapping.get(sku)
        if target is None:
            continue
        cf = pricing.cost_micro_usd(target, book, ev)
        if actual is None or cf is None:
            skipped += 1
            continue
        row = rows.setdefault(k, {"actual": 0, "swapped": 0, "events": 0})
        row["actual"] += actual
        row["swapped"] += cf
        row["events"] += 1
    out = [{by: k, "events": v["events"], "actual_micro_usd": v["actual"],
            "swapped_micro_usd": v["swapped"], "delta_micro_usd": v["swapped"] - v["actual"]}
           for k, v in sorted(rows.items())]
    return {"by": by, "rows": out, "skipped_events": skipped}
