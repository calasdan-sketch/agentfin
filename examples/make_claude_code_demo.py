"""Generate the SYNTHETIC Claude Code transcripts in examples/claude-code-demo/.

Nothing here is real usage. The data is deterministic (fixed seed) so the
README example output can be checked by the test suite. It mimics the shape
of real transcripts: one API response split over several `assistant` lines
(only the last carries `stop_reason` and the final output count), user lines
with no usage, a sub-agent sidechain, and a forked session that copies history.

    python examples/make_claude_code_demo.py
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parent / "claude-code-demo" / "projects"
PROJECTS = {"acme-api": "claude-opus-5-5", "docs-site": "claude-sonnet-5-5"}


def main() -> None:
    rng = random.Random(20261106)
    start = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    n = 0
    for project, model in PROJECTS.items():
        folder = OUT / f"-home-dev-{project}"
        folder.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        for i in range(12):
            n += 1
            ts = start + timedelta(minutes=7 * n)
            mid = f"msg_demo{n:04d}"
            usage = {
                "input_tokens": rng.randint(2, 40),
                "cache_creation_input_tokens": rng.choice([0, 2000, 8000]),
                "cache_read_input_tokens": rng.randint(20_000, 90_000),
            }
            final_out = rng.randint(200, 2500)
            parts = rng.randint(1, 3)
            for part in range(parts):
                last = part == parts - 1
                msg = {"id": mid, "model": model, "role": "assistant",
                       "content": [{"type": "text", "text": "(synthetic)"}],
                       "usage": {**usage, "output_tokens": final_out if last else 1}}
                if last:
                    msg["stop_reason"] = rng.choice(["end_turn", "tool_use"])
                lines.append(json.dumps({
                    "type": "assistant", "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "sessionId": f"demo-{project}", "requestId": f"req_demo{n:04d}",
                    "cwd": f"/home/dev/{project}", "uuid": f"u{n:04d}{part}",
                    "isSidechain": project == "acme-api" and i % 4 == 3,
                    "message": msg,
                }))
            lines.append(json.dumps({"type": "user", "timestamp": ts.isoformat(),
                                     "message": {"role": "user", "content": "(synthetic)"}}))
        (folder / f"demo-{project}.jsonl").write_text("\n".join(lines) + "\n")
        if project == "acme-api":  # a /branch fork copies the first half of history
            (folder / "demo-acme-api-fork.jsonl").write_text(
                "\n".join(lines[: len(lines) // 2]) + "\n")


if __name__ == "__main__":
    main()
