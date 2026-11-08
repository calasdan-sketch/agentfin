"""calas-gateway ledger adapter (Calas Automations' own gateway usage ledger).

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

from typing import Any

from ..ledger import token_value
from .base import Adapter

MAX_EVENT_COST_USD = 1_000_000  # one request costing more than this is a data error


def _gateway_cost(value: Any) -> int:
    """The gateway's own cost is trusted verbatim, but only if it is a sane
    amount: a negative cost would silently cancel real spend, and a huge one
    (an epoch timestamp in the wrong column, say) would swamp every report."""
    if isinstance(value, bool):
        raise ValueError("cost_usd must be a number, got a boolean")
    try:
        usd = float(value)
    except (TypeError, ValueError):
        raise ValueError("cost_usd is not a number") from None
    if usd != usd or usd < 0 or usd > MAX_EVENT_COST_USD:
        raise ValueError(
            f"cost_usd {usd!r} is outside 0 to {MAX_EVENT_COST_USD:,} USD per request")
    return int(round(usd * 1_000_000))


class GatewayAdapter(Adapter):
    name = "gateway"

    def parse_record(self, d: dict[str, Any]) -> dict[str, Any] | None:
        route = str(d.get("route", "/"))
        provider, _, model = route.partition("/")
        event: dict[str, Any] = {
            "ts": d.get("ts") or d.get("timestamp"),
            "provider": provider or "gateway",
            "model_raw": model or "unknown",
            "input_tokens": token_value(d.get("tokens_in"), "tokens_in"),
            "output_tokens": token_value(d.get("tokens_out"), "tokens_out"),
            "cache_read_tokens": token_value(d.get("tokens_cached"), "tokens_cached"),
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
            event["_gateway_cost_micro"] = _gateway_cost(d["cost_usd"])
        rid = d.get("run_id") or d.get("trace_id")
        if rid:
            event["idem_key"] = f"gateway:{rid}"
        return event
