"""Exact money helpers: no float drift, parts always sum to the whole."""

from __future__ import annotations

from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal


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
    parts sum exactly to the converted total (rounded once, half-even).

    Each key keeps (to within one cent) its OWN converted amount, including when a
    key is negative (net corrections): parts are floored to the cent and the few
    cents of residue go to the largest fractional remainders, ties by key."""
    keys = sorted(micro_by_key)
    total_cents = int((micro_to_cents(sum(micro_by_key.values()), rate) * 100)
                      .quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
    exact = {k: micro_to_cents(micro_by_key[k], rate) * 100 for k in keys}
    floors = {k: int(exact[k].to_integral_value(rounding=ROUND_FLOOR)) for k in keys}
    residue = total_cents - sum(floors.values())
    order = sorted(keys, key=lambda k: (-(exact[k] - floors[k]), k))
    if residue < 0 or residue > len(keys):  # cannot happen: |total error| < len(keys) cents
        raise ValueError("internal rounding error in currency conversion")
    for k in order[:residue]:
        floors[k] += 1
    return {k: Decimal(floors[k]) / 100 for k in micro_by_key}
