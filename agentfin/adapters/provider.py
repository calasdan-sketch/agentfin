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

The provider is inferred from the id prefix (msg_ -> anthropic,
chatcmpl- -> openai) or given explicitly. Attribution tags can ride in an
optional `agentfin` sidecar object: {"agentfin": {"agent": "...", "team": "...", "task": "..."}}.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from .base import Adapter


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

    def parse(self, lines: Iterable[str]) -> Iterable[dict[str, Any]]:
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            d = json.loads(line)
            usage = d.get("usage") or {}
            provider = d.get("provider") or _infer_provider(d)
            prompt_details = usage.get("prompt_tokens_details") or {}
            sidecar = d.get("agentfin") or {}
            event: dict[str, Any] = {
                "ts": d.get("created") or d.get("timestamp") or d.get("ts"),
                "provider": provider,
                "model_raw": d.get("model", "unknown"),
                "input_tokens": int(
                    usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0
                ),
                "output_tokens": int(
                    usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0
                ),
                "cache_read_tokens": int(
                    usage.get("cache_read_input_tokens", 0)
                    or prompt_details.get("cached_tokens", 0)
                    or 0
                ),
                "cache_write_tokens": int(
                    usage.get("cache_creation_input_tokens", 0) or 0
                ),
                "task": sidecar.get("task", ""),
                "agent": sidecar.get("agent", ""),
                "team": sidecar.get("team", ""),
                "metadata": {"response_id": d.get("id")},
            }
            if d.get("id"):
                event["idem_key"] = f"{provider}:{d['id']}"
            yield event
