"""Link spend to outcomes in git: cost per merged branch, and spend that never merged.

Claude Code transcripts record the git branch each response was produced on
(`git_branch` in event metadata). Joined with a local clone this answers the
engineering manager's question: how much did the work that *shipped* cost, and
how much went into branches that were abandoned?

Limits (also in the README):
* "Merged" is `git branch --merged <base>` plus an optional file of branch names
  you supply (e.g. from `gh pr list --state merged`), because a squash- or
  rebase-merged branch is not an ancestor of the base. Without that file such
  branches are reported as `unmerged`.
* Events on the base branch itself, or with no recorded branch, are reported
  separately (`<base branch>` / `<no branch>`): they cannot be tied to a PR.
* Only local git commands run (no network), without a shell, with external diff
  drivers, textconv and fsmonitor disabled, so a hostile repository's config
  cannot execute code through this command.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from . import reports, textsafe

_SAFE_REF = re.compile(r"^[A-Za-z0-9._/@+-]{1,200}$")
_GIT = ["git", "-c", "core.fsmonitor=false", "-c", "diff.external=", "-c", "core.pager=cat",
        "--no-pager"]


class GitError(RuntimeError):
    pass


def _git(repo: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            [*_GIT, "-C", str(repo), *args], capture_output=True, text=True, timeout=60,
            env={"PATH": os.environ.get("PATH", ""), "GIT_TERMINAL_PROMPT": "0",
                 "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                 "HOME": os.devnull, "LC_ALL": "C",
                 **({"SYSTEMROOT": os.environ["SYSTEMROOT"]} if "SYSTEMROOT" in os.environ
                    else {})},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git failed: {exc}") from None
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip()[:200] or f"git exit {proc.returncode}")
    return proc.stdout


def safe_ref(name: str) -> bool:
    """Branch names come from transcripts (untrusted). Only plain ref names are
    ever passed to git, and never one that looks like an option."""
    return bool(_SAFE_REF.match(name)) and not name.startswith("-") and ".." not in name


def branch_costs(
    conn: sqlite3.Connection, date_from: str | None = None, date_to: str | None = None,
    project: str | None = None,
) -> list[dict[str, Any]]:
    where, params = reports.time_where(date_from, date_to)
    extra = ""
    if project:
        extra = " AND json_extract(metadata_json, '$.project') = ?"
        params = [*params, project]
    rows = conn.execute(
        f"""SELECT COALESCE(json_extract(metadata_json, '$.git_branch'), '') AS branch,
                   SUM(cost_micro_usd) AS cost,
                   COALESCE(SUM(CASE WHEN is_correction = 0 THEN 1 ELSE 0 END), 0) AS events,
                   SUM(CASE WHEN cost_micro_usd IS NULL THEN 1 ELSE 0 END) AS unpriced
            FROM usage_events WHERE {where}{extra}
            GROUP BY branch ORDER BY cost DESC""",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def analyze(
    conn: sqlite3.Connection, repo: str | Path, base: str | None = None,
    merged_extra: set[str] | None = None, date_from: str | None = None,
    date_to: str | None = None, project: str | None = None,
) -> dict[str, Any]:
    repo = Path(repo)
    if not (repo / ".git").exists() and not repo.is_dir():
        raise GitError(f"not a directory: {repo}")
    if base is None:
        base = _default_base(repo)
    if not safe_ref(base):
        raise GitError(f"unsafe base branch name: {textsafe.clean(base, 60)!r}")
    _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{base}^{{commit}}")
    merged = {b.strip().lstrip("*+ ").strip()
              for b in _git(repo, "branch", "--merged", f"refs/heads/{base}",
                            "--format=%(refname:short)").splitlines()}
    merged.discard(base)
    merged |= {m for m in (merged_extra or set()) if m != base}
    existing = {b.strip() for b in _git(repo, "for-each-ref", "refs/heads",
                                        "--format=%(refname:short)").splitlines()}
    out, buckets = [], {"merged": 0, "unmerged": 0, "base": 0, "none": 0}
    totals = {"merged": 0, "unmerged": 0, "base": 0, "none": 0}
    unpriced = 0
    for r in branch_costs(conn, date_from, date_to, project):
        name, cost = r["branch"], r["cost"] or 0
        unpriced += r["unpriced"] or 0
        if not name or name == "HEAD":
            status = "none"
        elif name == base:
            status = "base"
        elif name in merged:
            status = "merged"
        else:
            status = "unmerged"
        row: dict[str, Any] = {
            "branch": textsafe.clean(name) or "<no branch>", "status": status,
            "cost_micro_usd": cost, "events": r["events"], "commits": None,
            "lines_changed": None,
        }
        if status in ("merged", "unmerged") and name in existing and safe_ref(name):
            try:
                row["commits"] = int(_git(repo, "rev-list", "--count",
                                          f"refs/heads/{base}..refs/heads/{name}").strip())
                added = deleted = 0
                for line in _git(repo, "diff", "--numstat", "--no-ext-diff", "--no-textconv",
                                 f"refs/heads/{base}...refs/heads/{name}").splitlines():
                    a, _, rest = line.partition("\t")
                    d = rest.partition("\t")[0]
                    added += int(a) if a.isdigit() else 0
                    deleted += int(d) if d.isdigit() else 0
                row["lines_changed"] = added + deleted
            except (GitError, ValueError):
                pass
        buckets[status] += 1
        totals[status] += cost
        out.append(row)
    n_merged = buckets["merged"]
    return {
        "base": base, "rows": out, "unpriced_events": unpriced,
        "merged_branches": n_merged, "merged_cost_micro_usd": totals["merged"],
        "unmerged_branches": buckets["unmerged"],
        "unmerged_cost_micro_usd": totals["unmerged"],
        "base_cost_micro_usd": totals["base"], "unlinked_cost_micro_usd": totals["none"],
        "cost_per_merged_branch_micro_usd": (totals["merged"] // n_merged) if n_merged else None,
    }


def _default_base(repo: Path) -> str:
    for cand in ("main", "master", "trunk", "develop"):
        try:
            _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{cand}")
            return cand
        except GitError:
            continue
    head = _git(repo, "symbolic-ref", "--short", "HEAD").strip()
    return head
