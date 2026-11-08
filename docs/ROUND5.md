# Round 5 - the money maths, the upgrade path, the books

Branch `expert-2`, version 0.4.0 (still unreleased). Registry check on 2026-11-08:
`pip index versions agentfin` shows only 0.2.0.

## How the panel was run (honest)

No sub-agent tool was available in this session, so **one reviewer played every role in
sequence** (the same limit as round 4). These are separate checklists run one after
another, not independent people. Every finding below names the command that showed it.
Roles that earlier rounds did not use: FinOps practitioner, LLM cost accountant, finance
controller, ccusage-style tool maintainer, plus a QA / test-methodology pass, a technical
writer and a performance pass.

## Baseline (before any change)

- `python -m pytest -q`: 237 passed. `ruff check .`: clean.
- After this round: 307 passed, `ruff check .` clean.
- `python benchmarks/bench.py 100000`: ingest 54.1 s wall / 6.64 s CPU; re-ingest 37.6 s /
  6.44 s CPU; `report --by task` 0.36 s; close run / verify 2.1 / 1.4 s. (Wall time on this
  shared VM swings 3x; CPU is the steady number.)

## Upgrade path (0.2.0 from the registry -> this tree)

`pip download agentfin==0.2.0 --no-deps`, installed in a clean venv, then `init`, ingest 12
events (2 unpriced), `budgets set`, `outcomes tag`, `report --json`. That ledger and config
are now test fixtures (`tests/fixtures/*_v0_2_0.*`, produced by the real 0.2.0).

| Check | Result | Proof |
|---|---|---|
| New code reads the 0.2.0 ledger; `report --by team --json` identical | same, byte for byte | `python -m agentfin --db l.db --config-dir cfg report --by team --json \| diff - old.json` |
| Re-ingesting the same source after upgrade | `0 events (12 duplicates skipped)`: idempotency keys are stable across versions, no double count | `ingest ledger_v0_2_0_source.jsonl` |
| `close run`, `close verify`, `outcomes roi`, `budgets check`, `status` on the old ledger | all work (the `closes` table is added on first open) | `tests/test_round5.py::test_old_ledger_close_outcomes_budgets_and_doctor_work` |
| 0.2.0's own CLI reads a ledger the new tree wrote to | yes (read-only commands) | `vold/bin/agentfin report --by agent` after `close run` |
| **Gap found:** a 0.2.0 `pricing.yaml` has no newer models, so events for them are UNPRICED after upgrading | `doctor` now says "this version's example price book lists 16 SKU(s) your pricing.yaml lacks" and `pricing-template` prints the new book. Their file is never edited | `agentfin doctor` on the fixture |

No migration is needed. The only upgrade action a user may need is the price book.

## Specialist findings

| Role | Finding | Proof | Fix |
|---|---|---|---|
| FinOps practitioner | `agentfin --config-dir ~/.agentfin init` **ignored the flag** and scaffolded `./.agentfin`; a hook set up the "obvious" way then priced everything as UNPRICED | `ls -a` after the command | `init` honours the global `--config-dir` and `--db`; `ingest` warns on stderr when there is no price book |
| FinOps practitioner | Budgets: `team:x` cap of $0.20 printed as "$0 budget" | `budgets set team:x 0.2; budgets check` | cents shown for fractional caps |
| LLM cost accountant | Per-event cost used float maths; on exact half-micro-dollar ties it rounded the wrong way (212 of 300,000 small random events were 1 micro-USD off a Decimal reference) | differential script, seed 5 | exact `Decimal` maths; 20,000-case differential test; ingest CPU unchanged (6.56 s vs 6.64 s per 100k) |
| Finance controller | `convert_parts` (journal in CAD etc.) shared the total by *absolute* weights: a team netting -$0.77 was booked as a **$0.57 debit** | built a 4-team ledger with a correction, `close journal` | each team keeps its own converted amount to within a cent, parts still sum exactly |
| Finance controller | Journal wrote negative debits; zero-amount lines; a lone 0.00 line for an empty month | same ledger | credits for negative teams, zero lines dropped, empty month = header only |
| Finance controller | `reconcile` deleted every comma, so a European `1,50` became **150** (100x); `;`-separated and UTF-16 files failed | `printf 'sku;amount\nx;1,50\n'` | `parse_amount`: `;`/tab files with decimal comma, `(12.50)` negatives, comma-file ambiguity refused; fuzzed 200,000 junk cells, 0 unexpected exceptions |
| ccusage-style maintainer | ccusage 20.0.48 reads 18 agent CLIs (its README, read-only); agentfin reads Claude Code plus generic formats. Not fixable without real transcripts of each CLI | `npm view ccusage version readme` | honest README comparison kept; idea 17 stays NOT-DONE |
| ccusage-style maintainer | No cheap "since last run" ingest: re-reading a 100k-event history costs 6.8 s CPU (18-37 s wall) every time a hook fires | bench "re-ingest" row | `ingest --modified-within 1d`; `status --oneline` (0.06 s wall at 100k) for a status bar |
| Technical writer | Dashboard footer said "agentfin v1" | grep | shows the real version and the author |
| Technical writer | README said the code has no `os.replace`; limits said a non-comma delimiter is an error; test counts stale | grep, ran each README command from a built wheel | all corrected |
| QA / test methodology | Mutation-style run (37 deliberate breaks of money, date and gate code): first pass 30 of 33 killed; survivors were the negative-token clamp, the reconcile tolerance boundary and the budget gate in `--json` mode | `python benchmarks/mutation_check.py` | three tests added; final result below |
| QA / test methodology | No golden files for output formats; no property tests | `ls tests` | 10 golden files (report text and json, FOCUS, raw csv/jsonl, journal, dashboard, allocate) and seeded property tests |
| Performance | Decimal pricing might slow ingest | `bench.py` before / after | no measurable change; `doctor --json` 0.5 s and `status` 0.06 s over 100k events |

## Pass B - attacking the result

| Finding | Proof | Fix |
|---|---|---|
| `status` waited the full 30 s ingest timeout when a writer held the lock (a status bar would freeze) | test with `BEGIN EXCLUSIVE`, took ~30 s | 3 s timeout for `status`; plain "ledger is busy" message |
| The "no price book" warning also fired for `--adapter gateway`, whose costs come with the data | code re-read | not shown for the gateway adapter |
| `budgets set` with a read-only/locked file after the atomic-save change | forced `os.replace` to raise PermissionError | one plain line, no temp file left (tested) |
| README/CHANGELOG claims re-run from a freshly built wheel in a clean venv (non-editable): `--version`, `demo`, `init`, `ingest`, `report`, `status`, `pricing-template`, `doctor`, hook-style invocation with `--modified-within` | see "Finish" commands in the final report | all as documented |

## Mutation result

First run: 30 killed, 3 survived (named above) of 33. After adding the three tests and 4
more mutations (for this round's new code): **37 killed, 0 survived, 0 broken of 37**
(`python benchmarks/mutation_check.py`, about 20 minutes on this VM). This is a hand-picked
list of money, date and gate conditions, not a full mutation-testing tool.

## Competitors (read-only re-check, 2026-11-08)

- ccusage: still 20.0.48 on npm (`npm view ccusage version`); its README lists 18 sources
  (Claude Code, Codex, OpenCode, Amp, Droid, Gemini CLI, Copilot CLI, ...) and daily /
  weekly / monthly / session reports. Not installed or run this round.
- LiteLLM: 1.108.3 on PyPI (`pip index versions litellm`; was 1.108.2). Feature claims not
  re-tested; the README comparison table is unchanged.
- Still unverified: the earlier "ccusage returned zero rows on the synthetic transcripts"
  result was not reproduced, so there is no head-to-head number.

## Idea list (scored 1-5; value / differentiation higher is better, effort / risk lower is better)

| # | Idea | Value | Diff. | Effort | Risk | Status |
|---|---|---|---|---|---|---|
| 1 | Exact Decimal pricing (no float tie errors) | 4 | 3 | 2 | 1 | DONE |
| 2 | Signed-safe currency split for journals | 4 | 3 | 2 | 1 | DONE |
| 3 | Journal: credits for negative teams, no zero lines | 4 | 3 | 1 | 1 | DONE |
| 4 | Reconcile: decimal comma, `;`/tab, UTF-16, accounting negatives, ambiguity refused | 4 | 3 | 2 | 2 | DONE |
| 5 | Global `--config-dir` / `--db` honoured by `init` | 5 | 1 | 1 | 1 | DONE |
| 6 | Warn when ingesting with no price book | 4 | 2 | 1 | 1 | DONE |
| 7 | `status` (+ `--json`, `--oneline`) | 4 | 2 | 2 | 1 | DONE (was round 4 #13) |
| 8 | `--json` for doctor / anomalies / close verify | 4 | 3 | 2 | 1 | DONE (was #12) |
| 9 | `pricing-template` + doctor hint for old price books | 4 | 2 | 2 | 1 | DONE |
| 10 | `ingest --modified-within` for hooks / cron | 4 | 3 | 2 | 1 | DONE |
| 11 | Atomic write of `budgets.yaml` | 2 | 1 | 1 | 1 | DONE (was #18) |
| 12 | Small budget caps shown with cents | 2 | 1 | 1 | 1 | DONE |
| 13 | Dashboard footer: real version + author | 1 | 1 | 1 | 1 | DONE |
| 14 | CI workflow: Linux / macOS / Windows, Python 3.10-3.13 | 5 | 1 | 2 | 1 | DONE as a file, **never run** (was #22) |
| 15 | Claude Code hook + status-line recipe in README | 4 | 3 | 1 | 2 | DONE as docs, **not tried inside Claude Code** |
| 16 | Real 0.2.0 ledger as an upgrade fixture | 4 | 2 | 2 | 1 | DONE |
| 17 | Golden files for every output format | 3 | 1 | 2 | 1 | DONE |
| 18 | Property + Decimal-differential tests | 3 | 2 | 2 | 1 | DONE |
| 19 | Mutation sanity script | 3 | 3 | 3 | 1 | DONE |
| 20 | Author line pinned by a test, in LICENSE, `--version`, metadata | 3 | 1 | 1 | 1 | DONE |
| 21 | More agent CLIs (Codex, Gemini CLI, ...) like ccusage | 5 | 2 | 4 | 2 | NOT-DONE (needs real transcripts to test against) |
| 22 | Price book updater from a provider page | 5 | 4 | 5 | 4 | NOT-DONE (needs verified sources) |
| 23 | Live collection (proxy / SDK callback) | 5 | 3 | 5 | 3 | NOT-DONE |
| 24 | WAL mode so readers do not wait on an ingest | 3 | 1 | 2 | 3 | NOT-DONE (file locking; needs Windows / network drive tests) |
| 25 | `py.typed` + type checker in CI | 2 | 1 | 3 | 1 | NOT-DONE (mypy reports 34 errors in 8 files, mostly Optional narrowing; marker would be a false claim) |
| 26 | Project URLs in package metadata | 3 | 1 | 1 | 1 | NOT-DONE (owner decision: only if the repository is public) |
| 27 | Month-over-month variance in `report` (budget vs actual, prior period) | 4 | 3 | 3 | 1 | NOT-DONE |
| 28 | ccusage-style 5-hour billing-block view | 3 | 2 | 3 | 1 | NOT-DONE (rate-limit window rules not verified) |
| 29 | Per-user cost for API-key showback | 4 | 3 | 4 | 2 | NOT-DONE |
| 30 | Slack / webhook on budget alert | 3 | 2 | 3 | 3 | NOT-DONE (sends messages; out of scope) |
| 31 | Parquet export for BI tools | 2 | 2 | 3 | 2 | NOT-DONE (adds a heavy dependency) |
| 32 | `reprice --all` correction path for already-priced events | 4 | 3 | 4 | 4 | NOT-DONE (interacts with closed months and the append-only guards; needs a design) |
| 33 | Effective-rate drift check vs invoice (cost per 1M tokens by SKU over time) | 3 | 3 | 3 | 1 | NOT-DONE |

## What could not be tested

- **Windows and macOS**: nothing was run on them. The CI file exists but has never run.
  `os.replace` on Windows when an editor holds `budgets.yaml` open is code-reviewed only.
- **Python 3.10** (declared minimum): not installed here; tests ran on 3.13 only this
  round (earlier rounds also ran 3.11 and 3.12).
- **The Claude Code hook and status-line settings**: written from memory of Claude Code's
  settings format and not run inside Claude Code. The `agentfin` commands they call were
  run from a built wheel with piped JSON on stdin.
- **Anthropic prices**: not re-checked this round (no change was made to any rate).
- **Accounting packages**: the journal CSV has not been imported into any.
- **ccusage head-to-head** and competitor feature claims (see above).
- Hypothesis (the property-testing library) is not used: it would be a new dev
  dependency; seeded generators are used instead.
- No independent reviewers (see top).
