"""Pricing: SKU -> per-million-token USD rates. Cost computed at ingest.

Unpriced SKUs are never silently zero — they produce NULL cost (the UNPRICED
bucket) that shows up loudly in every report.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PRICING = """\
# agentfin pricing — USD per 1M tokens, keyed by exact SKU (provider:model).
# Verify against your invoice with `agentfin reconcile`; prices change.
#
# Anthropic rates: copied from https://platform.claude.com/docs/en/about-claude/pricing
#   on 2026-11-06. cache_write = 5-minute cache writes, cache_write_1h = 1-hour
#   cache writes (falls back to cache_write when a SKU has no 1h rate).
#   Model IDs (e.g. claude-fable-5-1, claude-mythos-5) from the per-model pages
#   under https://platform.claude.com/docs/en/models/ on 2026-11-06.
# OpenAI / Google rates: carried over from agentfin 0.2 — re-check before trusting.
#
# Lookup order: exact SKU -> `aliases` below -> safe normalization
# (Anthropic date suffixes, Bedrock/Vertex IDs). Anything else is UNPRICED.

# Explicit SKU aliases: "<sku as it appears in your data>": "<sku below>".
aliases:
  anthropic:claude-opus-4-0: anthropic:claude-opus-4
  anthropic:claude-sonnet-4-0: anthropic:claude-sonnet-4

skus:
  anthropic:claude-fable-5-1:
    input: 10.00
    cache_read: 0.25
    cache_write: 12.50
    cache_write_1h: 20.00
    output: 50.00
  anthropic:claude-mythos-5-1:
    input: 10.00
    cache_read: 0.25
    cache_write: 12.50
    cache_write_1h: 20.00
    output: 50.00
  anthropic:claude-fable-5:
    input: 10.00
    cache_read: 1.00
    cache_write: 12.50
    cache_write_1h: 20.00
    output: 50.00
  anthropic:claude-mythos-5:
    input: 10.00
    cache_read: 1.00
    cache_write: 12.50
    cache_write_1h: 20.00
    output: 50.00
  anthropic:claude-opus-5-5:
    input: 4.00
    cache_read: 0.20
    cache_write: 5.00
    cache_write_1h: 8.00
    output: 20.00
  anthropic:claude-sonnet-5-5:
    input: 2.00
    cache_read: 0.10
    cache_write: 2.50
    cache_write_1h: 4.00
    output: 10.00
  anthropic:claude-haiku-5-5:
    # <=100K-token prompts; the `tiers` entry switches rates per request above that.
    input: 0.10
    cache_read: 0.01
    cache_write: 0.125
    cache_write_1h: 0.20
    output: 0.50
    tiers:
      - above_prompt_tokens: 100000
        input: 0.50
        cache_read: 0.05
        cache_write: 0.625
        cache_write_1h: 1.00
        output: 2.50
  anthropic:claude-opus-5:
    input: 5.00
    cache_read: 0.50
    cache_write: 6.25
    cache_write_1h: 10.00
    output: 25.00
  anthropic:claude-sonnet-5:
    input: 2.00
    cache_read: 0.20
    cache_write: 2.50
    cache_write_1h: 4.00
    output: 10.00
  anthropic:claude-opus-4-8:
    input: 5.00
    cache_read: 0.50
    cache_write: 6.25
    cache_write_1h: 10.00
    output: 25.00
  anthropic:claude-opus-4-7:
    input: 5.00
    cache_read: 0.50
    cache_write: 6.25
    cache_write_1h: 10.00
    output: 25.00
  anthropic:claude-opus-4-6:
    input: 5.00
    cache_read: 0.50
    cache_write: 6.25
    cache_write_1h: 10.00
    output: 25.00
  anthropic:claude-opus-4-5:
    input: 5.00
    cache_read: 0.50
    cache_write: 6.25
    cache_write_1h: 10.00
    output: 25.00
  anthropic:claude-opus-4-1:
    input: 15.00
    cache_read: 1.50
    cache_write: 18.75
    cache_write_1h: 30.00
    output: 75.00
  anthropic:claude-opus-4:
    input: 15.00
    cache_read: 1.50
    cache_write: 18.75
    cache_write_1h: 30.00
    output: 75.00
  anthropic:claude-sonnet-4-6:
    input: 3.00
    cache_read: 0.30
    cache_write: 3.75
    cache_write_1h: 6.00
    output: 15.00
  anthropic:claude-sonnet-4-5:
    input: 3.00
    cache_read: 0.30
    cache_write: 3.75
    cache_write_1h: 6.00
    output: 15.00
  anthropic:claude-sonnet-4:
    input: 3.00
    cache_read: 0.30
    cache_write: 3.75
    cache_write_1h: 6.00
    output: 15.00
  anthropic:claude-haiku-4-5:
    input: 1.00
    cache_read: 0.10
    cache_write: 1.25
    cache_write_1h: 2.00
    output: 5.00
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


class PriceBook(dict):
    """`dict` of SKU -> rates (backward compatible) plus explicit `aliases`."""

    def __init__(self, skus=None, aliases=None):
        super().__init__(skus or {})
        self.aliases: dict[str, str] = dict(aliases or {})


def load_pricing(path: str | Path | None) -> PriceBook:
    if path is None:
        return PriceBook()
    p = Path(path)
    if not p.exists():
        return PriceBook()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{p}: expected a mapping with a `skus:` key")
    skus = data.get("skus", {}) or {}
    aliases = data.get("aliases", {}) or {}
    if not isinstance(skus, dict) or not isinstance(aliases, dict):
        raise ValueError(f"{p}: `skus` and `aliases` must be mappings")
    book = PriceBook(skus, {str(k): str(v) for k, v in aliases.items()})
    # Fail fast on a broken price book instead of rejecting every ingested line.
    probe = {"input_tokens": 1, "cache_read_tokens": 1, "cache_write_tokens": 1,
             "output_tokens": 1}
    for sku, rates in book.items():
        if not isinstance(rates, dict):
            raise ValueError(f"{p}: rates for {sku!r} must be a mapping")
        try:
            cost_micro_usd(sku, {sku: rates}, probe)
            for tier in rates.get("tiers") or []:
                cost_micro_usd(sku, {sku: {**rates, **tier, "tiers": []}}, probe)
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError(f"{p}: {sku}: {exc}") from None
    return book


# Anthropic snapshot suffixes: claude-sonnet-4-5-20250929, claude-3-5-haiku-latest.
_ANTHROPIC_DATE = re.compile(r"-(\d{8}|latest)$")
# Vertex AI: claude-haiku-4-5@20251001
_VERTEX_DATE = re.compile(r"@\d{8}$")
# Bedrock: [us.|eu.|apac.|global.]anthropic.claude-sonnet-4-5-20250929-v1:0
_BEDROCK = re.compile(r"^(?:[a-z]{2,6}\.)?anthropic\.(?P<model>.+?)(?:-v\d+(?::\d+)?)?$")


def _candidates(sku: str) -> list[tuple[str, str]]:
    """Normalized SKU guesses, safest first, as (sku, reason).

    Only rewrites that are price-preserving by construction are attempted:
    Anthropic's dated snapshot IDs share the price of their dateless alias,
    and Bedrock/Vertex IDs name the same Anthropic model. OpenAI dated
    snapshots are deliberately NOT stripped (e.g. gpt-4o-2024-05-13 was priced
    differently from later gpt-4o snapshots) — alias those explicitly.
    """
    provider, _, model = sku.partition(":")
    provider_l, model_l = provider.strip().lower(), model.strip().lower()
    out: list[tuple[str, str]] = []
    m = _BEDROCK.match(model_l)
    if m:
        model_l, provider_l = m.group("model"), "anthropic"
        out.append((f"anthropic:{model_l}", "bedrock-id"))
    if _VERTEX_DATE.search(model_l) and model_l.startswith("claude"):
        model_l, provider_l = _VERTEX_DATE.sub("", model_l), "anthropic"
        out.append((f"anthropic:{model_l}", "vertex-id"))
    if model_l.startswith("claude") and provider_l in {
        "anthropic", "bedrock", "aws", "vertex", "vertex_ai", "google", "unknown"
    }:
        out.append((f"anthropic:{model_l}", "provider-normalized"))
        if _ANTHROPIC_DATE.search(model_l):
            out.append((f"anthropic:{_ANTHROPIC_DATE.sub('', model_l)}", "snapshot-date"))
    out.append((f"{provider_l}:{model_l}", "case-normalized"))
    seen, uniq = {sku}, []
    for cand in out:
        if cand[0] not in seen:
            seen.add(cand[0])
            uniq.append(cand)
    return uniq


def resolve(
    sku: str, pricing: dict[str, dict[str, Any]]
) -> tuple[str | None, dict[str, Any] | None, str]:
    """Find the price entry for `sku`. Returns (priced_as, rates, how).

    `how` is "exact", "alias", a normalization reason, or "unpriced".
    """
    if pricing.get(sku):
        return sku, pricing[sku], "exact"
    aliases = getattr(pricing, "aliases", {}) or {}
    target = aliases.get(sku)
    if target and pricing.get(target):
        return target, pricing[target], "alias"
    for cand, how in _candidates(sku):
        target = aliases.get(cand)
        if target and pricing.get(target):
            return target, pricing[target], f"alias+{how}"
        if pricing.get(cand):
            return cand, pricing[cand], how
    return None, None, "unpriced"


def _num(rates: dict[str, Any], key: str) -> float:
    try:
        value = float(rates.get(key, 0.0) or 0.0)
    except (TypeError, ValueError):
        raise ValueError(f"price {key!r} is not a number: {rates.get(key)!r}") from None
    if value < 0 or value != value or value == float("inf"):
        raise ValueError(f"price {key!r} must be a finite, non-negative number")
    return value


def _tok(event: dict[str, Any], key: str) -> int:
    value = int(event.get(key, 0) or 0)
    # Correction events carry signed deltas; everything else is clamped.
    return value if event.get("is_correction") else max(value, 0)


def cost_micro_usd(
    sku: str, pricing: dict[str, dict[str, float]], event: dict[str, Any]
) -> int | None:
    """Compute cost in integer micro-USD. Returns None for unpriced SKUs.

    Rates may carry `tiers: [{above_prompt_tokens: N, input: .., ...}]`; the
    highest tier whose threshold the request's prompt (input + cache read +
    cache write tokens) exceeds replaces the base rates for that request.
    """
    _, rates, _ = resolve(sku, pricing)
    if not rates:
        return None
    inp, cr = _tok(event, "input_tokens"), _tok(event, "cache_read_tokens")
    cw, out = _tok(event, "cache_write_tokens"), _tok(event, "output_tokens")
    # 1-hour cache writes are a subset of cache_write_tokens billed at their own rate.
    cw_1h = min(max(_tok(event, "cache_write_1h_tokens"), 0), max(cw, 0))
    prompt = inp + cr + cw
    active = rates
    best = -1
    for tier in rates.get("tiers") or []:
        if not isinstance(tier, dict):
            continue
        threshold = int(tier.get("above_prompt_tokens", 0) or 0)
        if prompt > threshold > best:
            best, active = threshold, {**rates, **tier}
    # micro_usd = tokens * price_per_mtok_usd  (since 1e6 micro-USD = $1)
    total = (
        inp * _num(active, "input")
        + cr * _num(active, "cache_read")
        + (cw - cw_1h) * _num(active, "cache_write")
        + cw_1h * _num(active, "cache_write_1h" if "cache_write_1h" in active
                       else "cache_write")
        + out * _num(active, "output")
    )
    return int(round(total))


def format_usd(micro: int | None) -> str:
    if micro is None:
        return "UNPRICED"
    return f"${micro / 1_000_000:,.2f}"
