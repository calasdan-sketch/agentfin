"""Pricing: SKU -> per-million-token USD rates. Cost computed at ingest.

Unpriced SKUs are never silently zero — they produce NULL cost (the UNPRICED
bucket) that shows up loudly in every report.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DEFAULT_PRICING = """\
# agentfin pricing — USD per 1M tokens, keyed by exact SKU (provider:model).
# Copy rates from the provider's pricing page. Verify with `agentfin reconcile`.
skus:
  openai:gpt-4o:
    input: 2.50
    cache_read: 1.25
    cache_write: 2.50
    output: 10.00
  openai:gpt-4o-mini:
    input: 0.15
    cache_read: 0.075
    cache_write: 0.15
    output: 0.60
  openai:o1:
    input: 15.00
    cache_read: 7.50
    cache_write: 15.00
    output: 60.00
  anthropic:claude-opus-4-1:
    input: 15.00
    cache_read: 1.50
    cache_write: 18.75
    output: 75.00
  anthropic:claude-sonnet-4-5:
    input: 3.00
    cache_read: 0.30
    cache_write: 3.75
    output: 15.00
  anthropic:claude-haiku-4-5:
    input: 1.00
    cache_read: 0.10
    cache_write: 1.25
    output: 5.00
  google:gemini-2.5-pro:
    input: 1.25
    cache_read: 0.31
    cache_write: 1.25
    output: 10.00
  google:gemini-2.5-flash:
    input: 0.30
    cache_read: 0.075
    cache_write: 0.30
    output: 2.50
"""

UNPRICED = object()


def load_pricing(path: str | Path | None) -> dict[str, dict[str, float]]:
    if path is None:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text()) or {}
    return data.get("skus", {}) or {}


def cost_micro_usd(
    sku: str, pricing: dict[str, dict[str, float]], event: dict[str, Any]
) -> int | None:
    """Compute cost in integer micro-USD. Returns None for unpriced SKUs."""
    rates = pricing.get(sku)
    if not rates:
        return None
    # micro_usd = tokens * price_per_mtok_usd  (since 1e6 micro-USD = $1)
    total = 0.0
    total += event.get("input_tokens", 0) * float(rates.get("input", 0.0))
    total += event.get("cache_read_tokens", 0) * float(rates.get("cache_read", 0.0))
    total += event.get("cache_write_tokens", 0) * float(rates.get("cache_write", 0.0))
    total += event.get("output_tokens", 0) * float(rates.get("output", 0.0))
    return int(round(total))


def format_usd(micro: int | None) -> str:
    if micro is None:
        return "UNPRICED"
    return f"${micro / 1_000_000:,.2f}"
