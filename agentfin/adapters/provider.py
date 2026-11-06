"""Provider API-style adapter (Anthropic / OpenAI usage objects).

Ingests JSONL where each line is a raw API response carrying a `usage` block:

Anthropic:
    {"id": "msg_01...", "model": "claude-sonnet-4-5", "created": 1760878920,
     "usage": {"input_tokens": 1200, "cache_creation_input_tokens": 400,
               "cache_read_input_tokens": 800, "output_tokens": 300}}

OpenAI (chat completions):
    {"id": "chatcmpl-...", "model": "gpt-4o-mini", "created": 1760878920,
     "usage": {"prompt_tokens": 1200, "completion_tokens": 300,
               "prompt_tokens_details": {"cached_tokens": 200}}}

OpenAI (Responses API):
    {"id": "resp_...", "model": "gpt-4o", "created_at": 1760878920,
     "usage": {"input_tokens": 1200, "output_tokens": 300,
               "input_tokens_details": {"cached_tokens": 200}}}

OpenAI's cached tokens are a subset of the prompt/input total, so they are
split out (1200 above -> 1000 uncached input + 200 cache read). Anthropic's
cache buckets are already separate, and its 1-hour cache writes
(usage.cache_creation.ephemeral_1h_input_tokens) are billed at their own rate.

The provider is inferred from the id prefix (msg_ -> anthropic,
chatcmpl- -> openai) or given explicitly. Attribution tags can ride in an
optional `agentfin` sidecar object: {"agentfin": {"agent": "...", "team": "...", "task": "..."}}.
"""

from __future__ import annotations

from typing import Any

from .base import Adapter, anthropic_cache_write_1h


def _infer_provider(d: dict[str, Any]) -> str:
    rid = str(d.get("id", ""))
    if rid.startswith("msg_"):
        return "anthropic"
    if rid.startswith("chatcmpl-") or rid.startswith("resp_"):
        return "openai"
    if "gemini" in str(d.get("model", "")).lower():
        return "google"
    return "unknown"


class ProviderAdapter(Adapter):
    name = "provider"

    def parse_record(self, d: dict[str, Any]) -> dict[str, Any] | None:
        usage = d.get("usage") if isinstance(d.get("usage"), dict) else {}
        provider = d.get("provider") or _infer_provider(d)
        sidecar = d.get("agentfin") if isinstance(d.get("agentfin"), dict) else {}
        # OpenAI reports cached (and cache-write) tokens as a SUBSET of the
        # prompt/input total (Chat: prompt_tokens_details, Responses:
        # input_tokens_details). Anthropic reports them as separate buckets.
        # Splitting OpenAI's total keeps cached tokens from being billed twice.
        details = (
            usage.get("prompt_tokens_details")
            or usage.get("input_tokens_details")
            or {}
        )
        details = details if isinstance(details, dict) else {}
        openai_style = "prompt_tokens" in usage or "input_tokens_details" in usage
        if openai_style:
            total_in = _int(usage.get("prompt_tokens", usage.get("input_tokens")))
            cache_read = _int(details.get("cached_tokens"))
            cache_write = _int(details.get("cache_write_tokens"))
            input_tokens = max(total_in - cache_read - cache_write, 0)
            output_tokens = _int(usage.get("completion_tokens", usage.get("output_tokens")))
        else:
            input_tokens = _int(usage.get("input_tokens"))
            cache_read = _int(usage.get("cache_read_input_tokens"))
            cache_write = _int(usage.get("cache_creation_input_tokens"))
            output_tokens = _int(usage.get("output_tokens"))
        event: dict[str, Any] = {
            "ts": d.get("created") or d.get("created_at") or d.get("timestamp") or d.get("ts"),
            "provider": provider,
            "model_raw": d.get("model", "unknown"),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "cache_write_1h_tokens": 0 if openai_style else anthropic_cache_write_1h(usage),
            "task": sidecar.get("task", ""),
            "agent": sidecar.get("agent", ""),
            "team": sidecar.get("team", ""),
            "metadata": {"response_id": d.get("id")},
        }
        if d.get("id"):
            event["idem_key"] = f"{provider}:{d['id']}"
        return event


def _int(value: Any) -> int:
    return int(value or 0)
