"""Exact money helpers: no float drift, parts always sum to the whole."""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal


def apportion(total: int, weights: dict[str, int]) -> dict[str, int]:
    """Split integer `total` across keys in proportion to non-negative integer
    `weights` (largest-remainder method). The parts always sum to `total`
    exactly; ties break on key order so the result is deterministic."""
    w = {k: max(int(v), 0) for k, v in weights.items()}
    denom = sum(w.values())
    if denom <= 0:
        raise ValueError("cannot apportion: all weights are zero")
    sign = -1 if total < 0 else 1
    mag = abs(total)
    parts = {k: mag * v // denom for k, v in w.items()}
    left = mag - sum(parts.values())
    order = sorted(w, key=lambda k: (-(mag * w[k] % denom), k))
    for k in order[:left]:
        parts[k] += 1
    return {k: sign * v for k, v in parts.items()}


def micro_to_cents(micro: int, rate: Decimal) -> Decimal:
    """micro-USD -> currency units at `rate` (units per 1 USD), as an exact Decimal."""
    return Decimal(micro) * rate / Decimal(1_000_000)


def convert_parts(micro_by_key: dict[str, int], rate: Decimal) -> dict[str, Decimal]:
    """Convert per-key micro-USD to a currency in whole cents so the converted
    parts sum exactly to the converted total (rounded once, half-even)."""
    total_cents = int((micro_to_cents(sum(micro_by_key.values()), rate) * 100)
                      .quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
    weights = {k: abs(v) for k, v in micro_by_key.items()}
    if not any(weights.values()):
        return {k: Decimal(0) for k in micro_by_key}
    cents = apportion(total_cents, weights)
    return {k: Decimal(v) / 100 for k, v in cents.items()}
