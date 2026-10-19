"""Adapter base: raw records -> canonical events -> priced, tagged, inserted."""

from __future__ import annotations

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

    def emit(self, raw: dict[str, Any]) -> None:
        event = dict(raw)
        event.setdefault("idem_key", ledger.idem_key_for(event))
        meta = event.get("metadata") or {}
        if not self.no_redact:
            meta = redact.redact(meta, self.redact_keys)
        event["metadata"] = meta
        # Some sources (e.g. calas-gateway) are the system of record for their
        # own cost and ship it pre-computed; that always wins over re-pricing.
        gateway_cost = event.pop("_gateway_cost_micro", None)
        if gateway_cost is not None:
            event["cost_micro_usd"] = gateway_cost
        else:
            event["cost_micro_usd"] = pricing.cost_micro_usd(
                f"{event['provider']}:{event['model_raw']}", self.pricing_cfg, event
            )
        if event["cost_micro_usd"] is None:
            self.unpriced += 1
        event["task"], event["agent"], event["team"] = attribution.apply_rules(
            event, self.rules_cfg
        )
        if ledger.insert_event(self.conn, event):
            self.inserted += 1
        else:
            self.duplicates += 1

    def commit(self) -> None:
        self.conn.commit()


def load_rules_and_pricing(
    rules_path, pricing_path
) -> tuple[dict[str, Any], dict[str, dict[str, float]]]:
    return attribution.load_rules(rules_path), pricing.load_pricing(pricing_path)


class Adapter:
    name = "base"

    def parse(self, lines: Iterable[str]) -> Iterable[dict[str, Any]]:
        """Yield canonical event dicts from raw input lines/records."""
        raise NotImplementedError
