"""Adapter base: raw records -> canonical events -> priced, tagged, inserted."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from typing import Any

from .. import attribution, ledger, pricing, redact


class IngestContext:
    def __init__(
        self,
        conn: sqlite3.Connection,
        pricing_cfg: dict[str, dict[str, float]],
        rules_cfg: dict[str, Any],
        redact_keys: set[str] | None = None,
        no_redact: bool = False,
    ):
        self.conn = conn
        self.pricing_cfg = pricing_cfg
        self.rules_cfg = rules_cfg
        self.redact_keys = redact_keys
        self.no_redact = no_redact
        self.inserted = 0
        self.duplicates = 0
        self.unpriced = 0
        self.revised = 0

    def emit(self, raw: dict[str, Any]) -> None:
        event = dict(raw)
        event.setdefault("idem_key", ledger.idem_key_for(event))
        meta = event.get("metadata") or {}
        if not self.no_redact:
            meta = redact.redact(meta, self.redact_keys)
        if event.get("cache_write_1h_tokens"):
            # Kept for audit and so `reprice` can bill 1h writes at their own rate.
            meta = {**meta, "cache_write_1h_tokens": int(event["cache_write_1h_tokens"])}
        event["metadata"] = meta
        # Some sources (e.g. calas-gateway) are the system of record for their
        # own cost and ship it pre-computed; that always wins over re-pricing.
        gateway_cost = event.pop("_gateway_cost_micro", None)
        if gateway_cost is not None:
            event["cost_micro_usd"] = gateway_cost
        else:
            sku = f"{event['provider']}:{event['model_raw']}"
            priced_as, _, how = pricing.resolve(sku, self.pricing_cfg)
            event["cost_micro_usd"] = pricing.cost_micro_usd(sku, self.pricing_cfg, event)
            if priced_as and how != "exact":
                # Audit trail: which price-book entry a non-exact SKU was billed as.
                meta = {**meta, "priced_as": priced_as, "priced_via": how}
                event["metadata"] = meta
        event["task"], event["agent"], event["team"] = attribution.apply_rules(
            event, self.rules_cfg
        )
        if ledger.insert_event(self.conn, event):
            self.inserted += 1
            if event["cost_micro_usd"] is None:
                self.unpriced += 1
        elif self._revise_partial(event):
            self.revised += 1
        else:
            self.duplicates += 1

    def _revise_partial(self, event: dict[str, Any]) -> bool:
        """A stored row flagged `partial` (e.g. a Claude Code line written before
        the response finished) may be superseded by a more complete record with
        the same key. The ledger is append-only, so the difference is written as
        a compensating correction event (`<key>#rev:<n>`, is_correction=1)."""
        key = event["idem_key"]
        orig = self.conn.execute(
            "SELECT task, agent, team, metadata_json FROM usage_events WHERE idem_key = ?",
            (key,),
        ).fetchone()
        if orig is None or not _meta(orig["metadata_json"]).get("partial"):
            return False  # stored record is final: a true duplicate
        prefix = f"{key}#rev:"
        rows = self.conn.execute(
            """SELECT input_tokens, cache_read_tokens, cache_write_tokens,
                      output_tokens, cost_micro_usd, metadata_json, idem_key
               FROM usage_events
               WHERE idem_key = ? OR (idem_key >= ? AND idem_key < ?)
               ORDER BY id""",
            (key, prefix, prefix + "\U0010ffff"),
        ).fetchall()
        if not _meta(rows[-1]["metadata_json"]).get("partial"):
            return False  # already revised to a final record
        fields = ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")
        have = {f: sum(r[f] for r in rows) for f in fields}
        new = {f: max(int(event.get(f, 0) or 0), 0) for f in fields}
        new_partial = bool(event["metadata"].get("partial"))
        if new == have or (new_partial and new["output_tokens"] <= have["output_tokens"]):
            return False
        costs = [r["cost_micro_usd"] for r in rows]
        new_cost = event.get("cost_micro_usd")
        delta_cost = (
            None if new_cost is None or any(c is None for c in costs)
            else new_cost - sum(costs)
        )
        meta: dict[str, Any] = {"revises": key, "partial": new_partial}
        for k in ("stop_reason", "priced_as", "priced_via", "source"):
            if k in event["metadata"]:
                meta[k] = event["metadata"][k]
        correction = {
            "idem_key": f"{prefix}{len(rows)}",
            "ts": event["ts"],
            "provider": event["provider"],
            "model_raw": event["model_raw"],
            **{f: new[f] - have[f] for f in fields},
            "cost_micro_usd": delta_cost,
            "is_correction": True,
            # Keep the original attribution so reports don't split one response.
            "task": orig["task"], "agent": orig["agent"], "team": orig["team"],
            "metadata": meta,
        }
        if delta_cost is None:
            self.unpriced += 1
        return ledger.insert_event(self.conn, correction)

    def commit(self) -> None:
        self.conn.commit()


def _meta(text: str | None) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def load_rules_and_pricing(
    rules_path, pricing_path
) -> tuple[dict[str, Any], dict[str, dict[str, float]]]:
    return attribution.load_rules(rules_path), pricing.load_pricing(pricing_path)


def anthropic_cache_write_1h(usage: dict[str, Any]) -> int:
    """1-hour cache-write tokens from an Anthropic usage block (0 if absent)."""
    cc = usage.get("cache_creation")
    if not isinstance(cc, dict):
        return 0
    try:
        return max(int(cc.get("ephemeral_1h_input_tokens") or 0), 0)
    except (TypeError, ValueError):
        return 0


def iter_json_lines(lines: Iterable[str]) -> Iterable[dict[str, Any]]:
    """Yield one JSON object per non-blank, non-comment line (strict)."""
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        d = json.loads(line)
        if not isinstance(d, dict):
            raise ValueError(f"expected a JSON object, got {type(d).__name__}")
        yield d


class Adapter:
    name = "base"
    #: When True, the CLI collects every event from every input file first and
    #: passes the whole batch through `reduce()` before inserting (used to pick
    #: the best of several duplicate records that share an idempotency key).
    buffered = False

    def parse_record(self, d: dict[str, Any]) -> dict[str, Any] | None:
        """Turn one decoded JSON object into a canonical event (or None to skip)."""
        raise NotImplementedError

    def parse(self, lines: Iterable[str]) -> Iterable[dict[str, Any]]:
        """Yield canonical event dicts from raw input lines (strict: raises on bad lines)."""
        events = (self.parse_record(d) for d in iter_json_lines(lines))
        events = [e for e in events if e is not None] if self.buffered else (
            e for e in events if e is not None
        )
        return self.reduce(events) if self.buffered else events

    def prefer(self, new: dict[str, Any], current: dict[str, Any]) -> bool:
        """For buffered adapters: should `new` replace `current` (same idem key)?"""
        return False

    def reduce(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep one event per idempotency key (per `prefer`), first-seen order."""
        best: dict[str, dict[str, Any]] = {}
        loose: list[dict[str, Any]] = []
        for e in events:
            key = e.get("idem_key")
            if not key:
                loose.append(e)
            elif key not in best or self.prefer(e, best[key]):
                best[key] = e
        return [*best.values(), *loose]
