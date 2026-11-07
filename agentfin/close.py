"""Month-end close: freeze a month's spend, detect anything that changes it later,
and produce a journal for the books.

A close records, for one calendar month in one time zone, the event count, total
cost, the highest event id seen, a SHA-256 digest of every event in the window,
the sha256 of the price book, and the FX rates you supplied. Later,
`verify` tells a controller exactly why a closed month no longer matches:

* late events  - rows ingested after the close whose timestamp falls in the month
* altered rows - the digest over the originally-closed rows changed (a repriced
                 event, an edited or deleted row)

Only FX rates the user passes in are ever used; agentfin does not fetch rates.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import IO, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import export, money, textsafe


class CloseError(ValueError):
    pass


def month_window(month: str, tz: str = "UTC") -> tuple[str, str, str]:
    """(utc_start_inclusive, utc_end_exclusive, last_local_date) for 'YYYY-MM' in `tz`."""
    try:
        y, m = (int(x) for x in month.split("-"))
        zone = timezone.utc if tz.upper() == "UTC" else ZoneInfo(tz)
        start = datetime(y, m, 1, tzinfo=zone)
        end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=zone)
    except (ValueError, ZoneInfoNotFoundError, OverflowError):
        raise CloseError(f"bad month {month!r} (use YYYY-MM) or time zone {tz!r}") from None
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    last_day = (end - timedelta(days=1)).date().isoformat()
    return (start.astimezone(timezone.utc).strftime(fmt),
            end.astimezone(timezone.utc).strftime(fmt), last_day)


def parse_fx(pairs: Iterable[str]) -> dict[str, str]:
    """['CAD=1.3712'] -> {'CAD': '1.3712'} (units of currency per 1 USD, kept as text)."""
    out: dict[str, str] = {}
    for p in pairs:
        cur, sep, rate = p.partition("=")
        cur = cur.strip().upper()
        try:
            r = Decimal(rate.strip())
        except InvalidOperation:
            r = Decimal(0)
        if (not sep or len(cur) != 3 or not cur.isalpha() or not r.is_finite()
                or not Decimal("0.000001") <= r <= Decimal("1000000")):
            raise CloseError(
                f"bad FX {p!r}: use CCY=rate with a rate between 0.000001 and 1000000, "
                "e.g. CAD=1.37")
        out[cur] = str(r)
    return out


_COLS = ("id, idem_key, ts_utc, sku, input_tokens, cache_read_tokens, cache_write_tokens, "
         "output_tokens, cost_micro_usd, is_correction")


def _digest(conn: sqlite3.Connection, start: str, end: str, upto_id: int | None) -> tuple[
        str, int, int, int, int]:
    """(digest, rows, max_id, cost_micro_usd, unpriced) over the window, optionally
    only rows with id <= upto_id."""
    h = hashlib.sha256()
    rows = cost = unpriced = max_id = 0
    sql = f"SELECT {_COLS} FROM usage_events WHERE ts_utc >= ? AND ts_utc < ?"
    params: list[Any] = [start, end]
    if upto_id is not None:
        sql += " AND id <= ?"
        params.append(upto_id)
    for r in conn.execute(sql + " ORDER BY id", params):
        h.update("|".join("NULL" if v is None else str(v) for v in tuple(r)).encode() + b"\n")
        rows += 1
        max_id = max(max_id, r["id"])
        if r["cost_micro_usd"] is None:
            unpriced += 1
        else:
            cost += r["cost_micro_usd"]
    return h.hexdigest(), rows, max_id, cost, unpriced


def price_book_sha(path: str | Path | None) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest() if path else "none"
    except OSError:
        return "none"


def run_close(
    conn: sqlite3.Connection, month: str, tz: str = "UTC", fx: dict[str, str] | None = None,
    pricing_path: str | Path | None = None, allow_unpriced: bool = False, note: str = "",
    supersede: bool = False, now: datetime | None = None,
) -> dict[str, Any]:
    start, end, _ = month_window(month, tz)
    now = now or datetime.now(timezone.utc)
    # Hold the write lock while the digest is computed and stored, so an ingest
    # running in another process cannot slip a row in between.
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        return _run_close(conn, month, tz, start, end, fx, pricing_path, allow_unpriced,
                          note, supersede, now)
    except BaseException:
        conn.rollback()
        raise


def _run_close(conn, month, tz, start, end, fx, pricing_path, allow_unpriced, note,
               supersede, now) -> dict[str, Any]:
    if end > now.strftime("%Y-%m-%dT%H:%M:%SZ"):
        raise CloseError(f"{month} has not ended in {tz} yet; a close needs a finished month")
    digest, rows, max_id, cost, unpriced = _digest(conn, start, end, None)
    if rows == 0:
        raise CloseError(f"no events in {month} ({tz}); nothing to close")
    if unpriced and not allow_unpriced:
        raise CloseError(
            f"{unpriced} UNPRICED event(s) in {month}: add the missing prices and run "
            "`agentfin reprice`, or pass --allow-unpriced to close with them excluded")
    prev = conn.execute("SELECT MAX(version) v FROM closes WHERE month = ?", (month,)).fetchone()
    version = (prev["v"] or 0) + 1
    if prev["v"] and not supersede:
        raise CloseError(f"{month} is already closed (version {prev['v']}); "
                         "use --supersede to record a new version")
    rec = {
        "month": month, "version": version, "tz": tz, "window_start": start, "window_end": end,
        "events": rows, "max_event_id": max_id, "cost_micro_usd": cost,
        "unpriced_events": unpriced, "digest": digest,
        "price_book_sha": price_book_sha(pricing_path), "fx_json": json.dumps(fx or {}),
        "closed_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "note": textsafe.clean(note, 500),
    }
    conn.execute(
        f"INSERT INTO closes ({', '.join(rec)}) VALUES ({', '.join(':' + k for k in rec)})", rec)
    conn.commit()
    return rec


def latest_close(conn: sqlite3.Connection, month: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM closes WHERE month = ? ORDER BY version DESC LIMIT 1", (month,)
    ).fetchone()


def verify(conn: sqlite3.Connection, month: str) -> dict[str, Any]:
    c = latest_close(conn, month)
    if c is None:
        raise CloseError(f"{month} has not been closed (run `agentfin close run {month}`)")
    digest, rows, _, cost, _ = _digest(conn, c["window_start"], c["window_end"],
                                       c["max_event_id"])
    late = conn.execute(
        """SELECT COUNT(*) n, COALESCE(SUM(cost_micro_usd), 0) cost,
                  SUM(CASE WHEN cost_micro_usd IS NULL THEN 1 ELSE 0 END) unpriced
           FROM usage_events WHERE ts_utc >= ? AND ts_utc < ? AND id > ?""",
        (c["window_start"], c["window_end"], c["max_event_id"])).fetchone()
    altered = digest != c["digest"]
    return {
        "month": month, "version": c["version"], "ok": not altered and late["n"] == 0,
        "closed_at": c["closed_at"], "closed_cost_micro_usd": c["cost_micro_usd"],
        "altered": altered, "rows_now": rows, "rows_at_close": c["events"],
        "cost_now_micro_usd": cost, "late_events": late["n"],
        "late_cost_micro_usd": late["cost"], "late_unpriced": late["unpriced"] or 0,
    }


JOURNAL_COLUMNS = ["JournalNo", "JournalDate", "AccountName", "Debits", "Credits",
                   "Description", "Name", "Currency"]


def journal_rows(
    conn: sqlite3.Connection, month: str, config: dict[str, Any], currency: str = "USD",
) -> list[dict[str, Any]]:
    """Balanced journal for a closed month: one debit per team (cost center), one
    credit to the accrual account. Refuses if the month drifted since the close."""
    v = verify(conn, month)
    if not v["ok"]:
        raise CloseError(
            f"{month} no longer matches its close ({v['late_events']} late event(s), "
            f"altered={v['altered']}); run `agentfin close verify {month}` and re-close "
            "with --supersede")
    c = latest_close(conn, month)
    currency = currency.upper()
    fx = json.loads(c["fx_json"] or "{}")
    if currency == "USD":
        rate = Decimal(1)
    elif currency in fx:
        rate = Decimal(fx[currency])
    else:
        raise CloseError(f"no {currency} FX rate was recorded at close; "
                         f"re-close with --supersede --fx {currency}=<rate>")
    by_team = {
        r["team"]: r["cost"] for r in conn.execute(
            """SELECT team, SUM(cost_micro_usd) cost FROM usage_events
               WHERE ts_utc >= ? AND ts_utc < ? AND id <= ? AND cost_micro_usd IS NOT NULL
               GROUP BY team ORDER BY team""",
            (c["window_start"], c["window_end"], c["max_event_id"]))
    }
    parts = money.convert_parts(by_team, rate)
    accounts = config.get("gl_accounts") or {}
    if not isinstance(accounts, dict):
        raise CloseError("config.yaml: gl_accounts must be a mapping of team -> account name")
    expense_default = str(config.get("gl_expense_default") or "AI usage expense")
    accrual = str(config.get("gl_accrual") or "Accrued AI usage")
    _, _, last_day = month_window(month, c["tz"])
    no = f"AF-{month.replace('-', '')}-v{c['version']}"
    rows: list[dict[str, Any]] = []
    total = Decimal(0)
    for team, amount in parts.items():
        total += amount
        acct = accounts.get(team) or expense_default
        rows.append({
            "JournalNo": no, "JournalDate": last_day,
            "AccountName": textsafe.clean(acct), "Debits": f"{amount:.2f}", "Credits": "",
            "Description": f"AI usage {month} list-price cost",
            "Name": textsafe.clean(team), "Currency": currency,
        })
    rows.append({
        "JournalNo": no, "JournalDate": last_day, "AccountName": textsafe.clean(accrual),
        "Debits": "", "Credits": f"{total:.2f}",
        "Description": f"AI usage {month} accrual", "Name": "", "Currency": currency,
    })
    return rows


def write_journal(rows: list[dict[str, Any]], fh: IO[str]) -> int:
    w = csv.DictWriter(fh, fieldnames=JOURNAL_COLUMNS, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: export.safe_cell(v) for k, v in r.items()})
    return len(rows)
