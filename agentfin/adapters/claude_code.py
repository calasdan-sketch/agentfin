"""Claude Code session-transcript adapter (zero config).

Claude Code writes one JSONL file per session under
``~/.claude/projects/<project>/<session-id>.jsonl``. Assistant lines carry the
API response's ``message.usage``. Only token counts, model, timestamps and
non-content identifiers are read — message text, thinking and tool calls are
never copied into the ledger.

Correctness details handled here:

* **Split responses.** One API response with several content blocks is
  written as several ``assistant`` lines that share ``message.id`` and repeat
  the same ``usage``. Summing every line over-counts. Events are keyed on
  ``anthropic:<message.id>`` and, within one ingest run, the most complete
  line (one with ``stop_reason``, else the largest ``output_tokens``) wins.
* **Forked sessions.** ``/branch`` / ``--fork-session`` copy history into new
  files; the shared key makes the copies no-ops.
* **Cross-source dedupe.** The key matches the ``provider`` adapter's key for
  the same Anthropic response, so ingesting both does not double-count.

Attribution: ``agent`` = ``claude-code`` (``claude-code-subagent`` for
sidechain lines), ``task`` = the project folder name taken from ``cwd``.
Rules in ``rules.yaml`` can override either via ``meta.project`` /
``meta.git_branch``.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .base import Adapter, anthropic_cache_write_1h


def default_roots() -> list[Path]:
    """Where Claude Code keeps transcripts. ``CLAUDE_CONFIG_DIR`` (comma-separated)
    overrides the defaults ``~/.claude`` and ``~/.config/claude``."""
    env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    bases = (
        [Path(p.strip()).expanduser() for p in env.split(",") if p.strip()]
        if env
        else [Path.home() / ".claude", Path.home() / ".config" / "claude"]
    )
    return [b / "projects" for b in bases if (b / "projects").is_dir()]


def _project_name(cwd: Any) -> str:
    if not isinstance(cwd, str) or not cwd.strip():
        return ""
    cwd = cwd.strip()
    # Transcripts written on Windows carry backslash paths even when read on POSIX.
    pure = PureWindowsPath(cwd) if ("\\" in cwd or (len(cwd) > 1 and cwd[1] == ":")) \
        else PurePosixPath(cwd)
    return pure.name[:128]


def _int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


class ClaudeCodeAdapter(Adapter):
    name = "claude-code"
    buffered = True

    def parse_record(self, d: dict[str, Any]) -> dict[str, Any] | None:
        if d.get("type") != "assistant":
            return None
        msg = d.get("message")
        if not isinstance(msg, dict):
            return None
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            return None
        tokens = {
            "input_tokens": _int(usage.get("input_tokens")),
            "cache_read_tokens": _int(usage.get("cache_read_input_tokens")),
            "cache_write_tokens": _int(usage.get("cache_creation_input_tokens")),
            "output_tokens": _int(usage.get("output_tokens")),
        }
        if not any(tokens.values()):
            return None  # synthetic/error lines carry no billable usage
        ts = d.get("timestamp")
        if not ts:
            return None
        project = _project_name(d.get("cwd"))
        raw_tier = usage.get("service_tier")
        tier = raw_tier.lower()[:32] if isinstance(raw_tier, str) and raw_tier else None
        meta: dict[str, Any] = {
            "source": "claude-code",
            "session_id": d.get("sessionId"),
            "request_id": d.get("requestId"),
            "project": project or None,
            "git_branch": d.get("gitBranch"),
            "stop_reason": msg.get("stop_reason"),
            "service_tier": tier,
            # No stop_reason yet = the response may still be streaming; a later,
            # more complete record revises it via a correction event.
            "partial": not msg.get("stop_reason"),
        }
        event: dict[str, Any] = {
            "ts": ts,
            "provider": "anthropic",
            "model_raw": str(msg.get("model") or "unknown"),
            **tokens,
            "cache_write_1h_tokens": anthropic_cache_write_1h(usage),
            "service_tier": tier,
            "task": project,
            "agent": "claude-code-subagent" if d.get("isSidechain") else "claude-code",
            "team": "",
            "metadata": {k: v for k, v in meta.items() if v is not None and v != ""},
        }
        if msg.get("id"):
            event["idem_key"] = f"anthropic:{msg['id']}"
        elif d.get("uuid"):
            event["idem_key"] = f"claude-code:{d['uuid']}"
        return event

    def prefer(self, new: dict[str, Any], current: dict[str, Any]) -> bool:
        """The most complete line wins: has a stop_reason, then most output tokens."""
        return _rank(new) > _rank(current)


def _rank(e: dict[str, Any]) -> tuple[int, int]:
    return (1 if e["metadata"].get("stop_reason") else 0, e["output_tokens"])
