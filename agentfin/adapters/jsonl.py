"""Generic JSONL adapter.

Each line is one usage record. Accepts flat or nested shapes; missing fields
default to zero. Example:

    {"ts": "2026-10-19T09:12:00-05:00", "provider": "anthropic",
     "model": "claude-sonnet-4-5", "input_tokens": 1200, "output_tokens": 300,
     "agent": "claude-code", "team": "platform", "task": "pr-review",
     "message_id": "msg_abc123", "metadata": {"run_id": "r1"}}

Accepted timestamp keys: ts, timestamp, created_at, time.
Accepted token keys: input_tokens/input, output_tokens/output,
cache_read_tokens/cache_read, cache_write_tokens/cache_write.
Idempotency: message_id / id / event_id if present, else content hash.
"""

from __future__ import annotations

from typing import Any

from .base import Adapter


def _first(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


class JsonlAdapter(Adapter):
    name = "jsonl"

    def parse_record(self, d: dict[str, Any]) -> dict[str, Any] | None:
        usage = d.get("usage") if isinstance(d.get("usage"), dict) else d
        event: dict[str, Any] = {
            "ts": _first(d, "ts", "timestamp", "created_at", "time"),
            "provider": _first(d, "provider", default="unknown"),
            "model_raw": _first(d, "model", "model_raw", "model_name", default="unknown"),
            "input_tokens": int(_first(usage, "input_tokens", "input", default=0) or 0),
            "output_tokens": int(_first(usage, "output_tokens", "output", default=0) or 0),
            "cache_read_tokens": int(
                _first(usage, "cache_read_tokens", "cache_read", default=0) or 0
            ),
            "cache_write_tokens": int(
                _first(usage, "cache_write_tokens", "cache_write", default=0) or 0
            ),
            "task": _first(d, "task", "task_name", default=""),
            "agent": _first(d, "agent", "agent_name", default=""),
            "team": _first(d, "team", default=""),
            "metadata": {
                **(d.get("metadata") if isinstance(d.get("metadata"), dict) else {}),
                **{k: v for k, v in d.items()
                   if k not in _RESERVED and k != "metadata"},
            },
        }
        idem = _first(d, "message_id", "id", "event_id")
        if idem:
            event["idem_key"] = f"{event['provider']}:{idem}"
        return event


_RESERVED = {
    "ts", "timestamp", "created_at", "time", "provider", "model", "model_raw",
    "model_name", "input_tokens", "input", "output_tokens", "output",
    "cache_read_tokens", "cache_read", "cache_write_tokens", "cache_write",
    "usage", "task", "task_name", "agent", "agent_name", "team",
    "message_id", "id", "event_id",
}
