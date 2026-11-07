"""PII/credential redaction applied in adapters BEFORE anything is stored."""

from __future__ import annotations

import re
from typing import Any

DEFAULT_REDACT_KEYS = {
    "prompt",
    "messages",
    "input",
    "content",
    "api_key",
    "apikey",
    "authorization",
    "secret",
    "token",
    "password",
    "bearer",
}

# Looks like leaked credentials inside free-form strings.
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9\-_]{8,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-_]{8,}"),
    re.compile(r"ghp_[A-Za-z0-9]{8,}"),
    re.compile(r"AIza[A-Za-z0-9\-_]{8,}"),
    re.compile(r"(?i)(api[_-]?key|bearer)\s*[:=]\s*['\"]?([^\s'\"]{8,})"),
]

REDACTED = "[REDACTED]"


def _scrub_string(s: str) -> str:
    for pat in _SECRET_PATTERNS:
        s = pat.sub(REDACTED, s)
    return s


def redact(obj: Any, extra_keys: set[str] | None = None) -> Any:
    """Recursively redact sensitive keys and credential-looking strings."""
    keys = {k.lower() for k in DEFAULT_REDACT_KEYS} | {
        k.lower() for k in (extra_keys or set())
    }
    return _redact(obj, keys)


def _redact(obj: Any, keys: set[str]) -> Any:
    if isinstance(obj, dict):
        return {
            k: REDACTED if str(k).lower() in keys else _redact(v, keys)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(v, keys) for v in obj]
    if isinstance(obj, str):
        return _scrub_string(obj)
    return obj
