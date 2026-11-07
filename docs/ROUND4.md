# Round 4 - the stranger's first hour

Branch `expert-2`, version 0.4.0 (unreleased). Registry check on 2026-11-07:
`pip index versions agentfin` shows only 0.2.0, so a stranger who installs today does
not get most of what this README describes.

## How the panel was run (honest)

No sub-agent tool was available in this session, so **one reviewer played every role**.
These are separate checklists run one after another, not independent people. Treat the
findings as one careful pass, not a consensus. Every finding below names the command
that showed it.

## Specialist findings

| Role | Finding | Proof |
|---|---|---|
| First-time user (README only, fresh venv) | README says `examples/claude-code-demo` but a pip install has no `examples/`; the quick start could not be tried. Fixed: `agentfin demo` + data in the wheel. | `pip install dist/*.whl` in clean venv, `agentfin demo` |
| First-time user | `agentfin report` before `init` printed an empty table, exit 0, and created `.agentfin/agentfin.db`. Looked like "no spend". Fixed (exit 1, plain message, nothing created). | `agentfin report` in empty dir |
| Release engineer | Wheel/sdist had no LICENSE, classifiers, keywords or SPDX license; `adapters/` lacked `__init__.py`; stale `agentfin.egg-info/` sat in the working tree (ignored, not tracked). Fixed; `twine check` passes. | `unzip -l dist/*.whl`, `twine check dist/*` |
| Release engineer | Version consistent: pyproject = `__version__` = CHANGELOG = `--version` = wheel METADATA (0.4.0). Now enforced by a test. | `agentfin --version`, `tests/test_round4.py` |
| Supply-chain | Runtime deps are `click`, `pyyaml` (+ `tzdata` on Windows only); both used; clean-venv install pulls exactly those. No network calls, no `shell=True`, git is invoked with an argument list. Artifact scan found no secrets or real paths (the `/home/dev/...` strings are synthetic demo data). | `pip list` in clean venv; grep of unpacked artifacts |
| Docs/DX | Error text "Path 'x' does not exist" for a typo is click's own and fine. Damaged ledger and broken YAML printed 40-line tracebacks. Fixed. | `echo garbage > bad.db; agentfin --db bad.db report` |
| CI/CD integrator | `budgets check` had no machine-readable output. Added `--json`. Gates already fail closed (exit 1/3). | `agentfin budgets check --json --fail-on-alert` |
| Hostile-input fuzzer | 750 random junk files (binary, 100k nested brackets, bidi/ANSI names, wrong shapes) across all 5 adapters: 0 crashes. | in-process fuzz loop, seed 7 |
| Concurrency | 3 writers ingesting the same 20k-line file: one wins, the others report "busy" or "0 new, 20000 duplicates"; the ledger holds exactly 20,000 events. The busy case used to be a traceback. | 3 background `agentfin ingest` |
| Windows reviewer (static only) | No `os.replace`, `shell=True`, signals; all text files opened with explicit UTF-8; console uses `errors="replace"`. Real gap: PowerShell 5 `>` writes UTF-16, which every adapter rejected. Fixed. | `iconv -t UTF-16` test file |
| Performance | CPU cost is ~65 us/event (6.4 s per 100k); wall time on this VM is 4-6x higher and varies 2x run to run because of disk wait, so the bench now prints CPU time too. No code change: ingest is already one transaction. | `python benchmarks/bench.py 100000`, cProfile |
| Competitor maintainer | ccusage is still 20.0.48 on npm and LiteLLM 1.108.2 on PyPI (read-only version lookups). Their feature claims in the README comparison were not re-tested this round; the ccusage head-to-head is still unverified. | `npm view ccusage version`, `pip index versions litellm` |

## Idea list (scored 1-5; value / differentiation higher is better, effort / risk lower is better)

| # | Idea | Value | Diff. | Effort | Risk | Status |
|---|---|---|---|---|---|---|
| 1 | `agentfin demo` shipped in the wheel | 5 | 3 | 2 | 1 | DONE |
| 2 | Add LICENSE + SPDX + classifiers + keywords | 5 | 1 | 1 | 1 | DONE |
| 3 | Read commands refuse a missing ledger | 5 | 2 | 1 | 2 | DONE |
| 4 | Plain-English errors: damaged DB, bad YAML, permissions, disk | 4 | 2 | 2 | 1 | DONE |
| 5 | Quiet exit on closed pipe | 3 | 1 | 1 | 1 | DONE |
| 6 | `init --force` repairs a broken config | 3 | 1 | 1 | 1 | DONE |
| 7 | UTF-16 (PowerShell) input | 3 | 3 | 1 | 1 | DONE |
| 8 | `budgets check --json` | 4 | 3 | 1 | 1 | DONE |
| 9 | 30 s busy timeout | 2 | 1 | 1 | 1 | DONE |
| 10 | CPU time in the benchmark | 2 | 1 | 1 | 1 | DONE |
| 11 | Version-consistency test | 3 | 1 | 1 | 1 | DONE |
| 12 | `--json` for `doctor`, `anomalies`, `close verify` | 4 | 3 | 2 | 1 | NOT-DONE |
| 13 | `agentfin status` one-screen summary (ledger path, rows, last event, unpriced) | 4 | 2 | 2 | 1 | NOT-DONE |
| 14 | Shipped price book updater from a provider page | 5 | 4 | 5 | 4 | NOT-DONE (needs verified sources, prices change) |
| 15 | Live collection (proxy / SDK callback) | 5 | 3 | 5 | 3 | NOT-DONE |
| 16 | WAL mode for readers during an ingest | 3 | 1 | 2 | 3 | NOT-DONE (file-locking change, needs Windows/network-drive testing) |
| 17 | More agent CLIs (Codex, Gemini CLI, Cursor) like ccusage | 5 | 2 | 4 | 2 | NOT-DONE (needs real transcripts to test against) |
| 18 | Atomic write of `budgets.yaml` | 2 | 1 | 1 | 1 | NOT-DONE |
| 19 | Reconcile accepts UTF-16 and `;` CSVs | 2 | 1 | 2 | 1 | NOT-DONE |
| 20 | `py.typed` + a type checker in CI | 2 | 1 | 3 | 1 | NOT-DONE (no mypy configured; shipping the marker unchecked would be a false claim) |
| 21 | Project URLs in package metadata | 3 | 1 | 1 | 1 | NOT-DONE (repository URL not known to this session; owner to add) |
| 22 | GitHub Actions workflow that runs tests on Windows/macOS/3.10 | 5 | 1 | 2 | 1 | NOT-DONE (cannot run it here; the best fix for "Windows untested") |
| 23 | Slack/webhook on budget alert | 3 | 2 | 3 | 3 | NOT-DONE (sends messages; out of scope) |
| 24 | Per-user cost for team showback of API keys | 4 | 3 | 4 | 2 | NOT-DONE |

## What could not be tested

- **Windows and macOS**: nothing was run on them. Static review and emulated tests only.
- **Python 3.10** (the declared minimum): not installed. Tests pass on 3.11 and 3.13; the
  wheel installs and `agentfin demo` runs on 3.11, 3.12, 3.13.
- **Read-only filesystem and permission errors**: this sandbox runs as root, which ignores
  file permissions, so the "permission denied" message is code-reviewed, not exercised.
- Competitor feature claims and the ccusage zero-row result (not re-run).
- The actual `pip install agentfin` from the registry of the 0.4.0 release (not published).
- No independent reviewers (see top).
