"""Attribution rules: map raw usage events -> task / agent / team at ingest.

Rules are evaluated in order; the first rule whose conditions all match wins.
Unmatched events get the default team/agent/task from config.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

DEFAULT_RULES = """\
# agentfin attribution rules — first matching rule wins.
# Conditions: provider, model (regex), task (regex on task/hint), agent, team,
#             meta.<key> (regex on metadata value).
# Set: task, agent, team.
defaults:
  task: unattributed
  agent: unknown
  team: unassigned
rules:
  - match: {model: ".*sonnet.*"}
    set: {agent: claude-code}
  - match: {model: ".*gpt-4o-mini.*"}
    set: {agent: cheap-runner}
  - match: {meta.task: "support.*"}
    set: {task: support-triage, team: support}
"""

_COMPILED: dict[str, re.Pattern] = {}


def _rx(pattern: str) -> re.Pattern:
    if pattern not in _COMPILED:
        _COMPILED[pattern] = re.compile(pattern, re.IGNORECASE)
    return _COMPILED[pattern]


def load_rules(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return {"defaults": {}, "rules": []}
    p = Path(path)
    if not p.exists():
        return {"defaults": {}, "rules": []}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {"defaults": {}, "rules": []}


def _matches(conditions: dict[str, Any], event: dict[str, Any]) -> bool:
    metadata = event.get("metadata") or {}
    for key, pattern in conditions.items():
        if key == "provider":
            value = event.get("provider", "")
        elif key == "model":
            value = event.get("model_raw", "")
        elif key in ("task", "agent", "team"):
            value = event.get(key) or ""
        elif key.startswith("meta."):
            value = str(metadata.get(key[5:], ""))
        else:
            return False
        if not _rx(str(pattern)).search(str(value)):
            return False
    return True


def apply_rules(
    event: dict[str, Any], rules_cfg: dict[str, Any]
) -> tuple[str, str, str]:
    """Return (task, agent, team) for an event. Adapter tags win over rules."""
    defaults = rules_cfg.get("defaults", {}) or {}
    task = event.get("task") or ""
    agent = event.get("agent") or ""
    team = event.get("team") or ""
    for rule in rules_cfg.get("rules", []) or []:
        if _matches(rule.get("match", {}) or {}, event):
            for k, v in (rule.get("set", {}) or {}).items():
                if k == "task" and not task:
                    task = v
                elif k == "agent" and not agent:
                    agent = v
                elif k == "team" and not team:
                    team = v
    return (
        task or defaults.get("task", "unattributed"),
        agent or defaults.get("agent", "unknown"),
        team or defaults.get("team", "unassigned"),
    )
