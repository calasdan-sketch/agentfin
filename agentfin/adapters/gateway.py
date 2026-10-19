"""calas-gateway ledger adapter (Dan's own gateway usage ledger).

The gateway writes one JSON object per line with this shape:

    {"ts": "2026-10-19T09:12:00Z", "route": "anthropic/claude-sonnet-4-5",
     "tokens_in": 1200, "tokens_out": 300, "tokens_cached": 800,
     "agent": "jarvis", "project": "onfile", "run_id": "run_9f2",
     "cost_usd": 0.0042, "trace_id": "tr_77"}

`route` is "<provider>/<model>". If the gateway already computed a cost,
it is preserved verbatim (converted to micro-USD) instead of re-pricing —
the gateway is the system of record for its own spend.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from .base import Adapter


class GatewayAdapter(Adapter):
    name = "gateway"

    def parse(self, lines: Iterable[str]) -> Iterable[dict[str, Any]]:
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            d = json.loads(line)
            route = str(d.get("route", "/"))
            provider, _, model = route.partition("/")
            event: dict[str, Any] = {
                "ts": d.get("ts") or d.get("timestamp"),
                "provider": provider or "gateway",
                "model_raw": model or "unknown",
                "input_tokens": int(d.get("tokens_in", 0) or 0),
                "output_tokens": int(d.get("tokens_out", 0) or 0),
                "cache_read_tokens": int(d.get("tokens_cached", 0) or 0),
                "cache_write_tokens": 0,
                "task": d.get("project", "") or d.get("task", ""),
                "agent": d.get("agent", ""),
                "team": d.get("team", ""),
                "metadata": {
                    "run_id": d.get("run_id"),
                    "trace_id": d.get("trace_id"),
                    "route": route,
                },
            }
            # Gateway-provided cost wins over re-pricing.
            if d.get("cost_usd") is not None:
                event["_gateway_cost_micro"] = int(round(float(d["cost_usd"]) * 1_000_000))
            rid = d.get("run_id") or d.get("trace_id")
            if rid:
                event["idem_key"] = f"gateway:{rid}"
            yield event
