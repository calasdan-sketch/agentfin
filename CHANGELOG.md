# Changelog

## 0.4.0 - unreleased (expert round 4: the stranger's first hour)

Round 4 is additive on the same unreleased line; no version bump (0.4.0 is not
on PyPI yet, which has 0.2.0). Focus: what someone who types `pip install` gets.
See `docs/ROUND4.md` for the panel, the scored idea list and what was not tested.

### Fixed
- **The published files were incomplete**: no `LICENSE` in the wheel or sdist
  (MIT was declared but the text was missing), no classifiers, keywords or SPDX
  license expression, and `agentfin/adapters/` had no `__init__.py`. All added;
  `twine check` passes on both artifacts.
- **Tracebacks for ordinary problems**: a damaged/non-SQLite ledger, a broken
  `config.yaml`, a permissions problem and a closed pipe (`... | head`) each print one
  plain line (exit 1; a closed pipe exits 0 quietly).
- **`init --force` could not repair a broken `config.yaml`** because the file was read
  before `init` ran. It no longer reads it.
- **Typing `report` (or `doctor`, `export`, `dashboard`, ...) before `init` silently
  created an empty ledger and showed "no spend"**. Read commands now stop with
  "ledger not found ... run `agentfin init`" (exit 1) and create nothing.
- UTF-16 input (what Windows PowerShell 5's `>` writes) was reported as every line
  malformed; it is now read. UTF-8 BOM and CRLF were already fine (tests added).
- Two writers at once now wait up to 30 s (was 5 s) and then say "the ledger is busy".
- `export --from bad` is a usage error (2) even when no ledger exists yet.

### Added
- `agentfin demo`: runs the README example on built-in synthetic data in a throwaway
  folder, so a pip user can see the output without cloning the repo (the demo data
  is now inside the wheel).
- `budgets check --json`.
- `benchmarks/bench.py` prints CPU time next to wall time.
- `tests/test_round4.py`: 23 new tests (214 -> 237), including a version-consistency check.

### Changed
- **Behaviour change:** read commands exit 1 instead of creating an empty ledger when
  none exists. Scripts that relied on `agentfin report` creating one should run
  `agentfin init` first.
- Build needs setuptools >= 77 (SPDX `license = "MIT"`).

## 0.4.0 - unreleased (expert round 3, same unreleased line)

Panel: attacker (hostile input), performance engineer, finance auditor,
first-time buyer (README only), docs/DX lead, competitor engineer (ccusage,
LiteLLM). Round 3 is additive on top of round 2 below; no version bump.

### Fixed (found by running the tool, not by reading it)
- **Negative token counts were silently set to 0**, and NaN, booleans, 10.5 and
  10^30 got through some adapters. All are now refused per line with a plain
  message naming the field (correction events may still be negative).
- **Timestamps like year 1 or 9999 were stored** and later crashed `export
  --format focus` with a traceback. Events before 2015 or more than 2 days in
  the future are refused; old bad rows give a clean error, and `doctor` counts them.
- **Gateway `cost_usd` was trusted blindly**: a negative cost cancelled real
  spend and an epoch in the wrong column became $1.7 billion. Now 0 to $1,000,000
  per request, else the line is refused. The ledger also refuses a negative cost
  on a non-correction event.
- **`reconcile` treated a wrong header as an invoice of $0** (a file with an
  `amount` column "matched"). Now: column check, aliases, BOM, `$` and thousands
  separators, exact decimal sums, NaN/inf rejected, one-sided SKUs labelled,
  SKUs with unpriced events never "match", output sanitized.
- `budgets set` accepted `nan`/`inf`; `outcomes tag --value inf` crashed with a
  traceback. Both are usage errors now.
- A file with no newline, or `/dev/zero`, was read into memory whole. Lines over
  16 MiB are skipped; device and pipe paths are refused (use `-`).
- Windows: YAML/HTML files are read and written as UTF-8 (they used the system
  code page), and stdout no longer crashes on the arrows and ticks in a legacy
  code page. Not run on Windows; covered by an emulated cp1252 test only.
- Error lines read "input_tokens is negative (-5)" instead of
  "OverflowError: Python int too large to convert to SQLite INTEGER".

### Added
- `reconcile --from/--to` (invoice period), `--tolerance`, `--fail-on-variance`
  (exit 3), `--json`.
- `report --by day|week|month` cut in `--tz` (bare `--from/--to` dates are
  whole days in that zone) and `--by session` (Claude Code session id).
- `ingest --dry-run`.
- `doctor` warns when `pricing.yaml` has not been modified for 60 days (file age
  only) and counts events with impossible dates.
- `python -m agentfin`; `benchmarks/bench.py` runs from a checkout and gained a
  corrupt-line section, `report --by day` and `reconcile` timings.
- `tests/test_round3.py`: 52 new tests (162 -> 214).

### Changed
- **Behaviour change:** negative token counts and implausible values are
  rejected instead of clamped (see Fixed). A pipeline that relied on clamping
  now sees those lines in the "malformed" list.
- `reconcile` output flags are `ok` / `XX VARIANCE` (ASCII) and it prints a note
  when no period was given. The default tolerance stays $0.01 per SKU.
- `redact()` builds its key set once per call tree, and the clock is read once
  a second during ingest; together these roughly cancel the new validation cost.

## 0.4.0 - unreleased (expert round 2)

Expert panel: FinOps practitioner, LLM billing specialist, data engineer,
engineering manager, finance controller, skeptical CFO. Findings that drove this
release: terminal escape sequences in tag/model names reached the screen; Gemini
usage records were unreadable; OpenTelemetry spans could not be ingested; a
closed month could silently change after reprice or late ingest; shared spend
could not be allocated; FOCUS export lacked columns FOCUS 1.4 marks Mandatory;
nothing linked spend to merged work.

### Added
- `agentfin close run|verify|journal`: month-end close in a chosen time zone
  with user-supplied FX, SHA-256 digest of the closed events, drift report
  (late events vs altered rows, exit 3), balanced journal CSV with exact
  rounding. Closes are append-only; `--supersede` records a new version.
- `agentfin allocate`: showback of `unassigned` (or any) spend across teams in
  exact integer micro-dollars.
- `agentfin outcomes git`: cost per merged branch, spend on branches that never
  merged, spend that cannot be linked. Runs local git only, without a shell,
  with external diff/textconv/fsmonitor disabled; branch names from transcripts
  are validated before use.
- `agentfin savings` (prompt-cache savings vs no cache) and `agentfin whatif
  --swap A=B` (same tokens on another SKU), both from your own price book.
- `otel` adapter for OpenTelemetry GenAI spans (OTLP JSON lines or Python SDK
  JSON), with cache split, aggregate-span skipping and cross-source dedupe.
- Gemini/Vertex `usageMetadata` support in the `provider` adapter (cached
  tokens split, thoughts tokens billed as output).
- `modifiers:` in pricing.yaml (user-supplied multipliers by `service_tier`,
  e.g. a batch discount); `service_tier` read from Claude Code, Anthropic and
  OpenAI records; `doctor` warns about tiers without a modifier.
- `benchmarks/bench.py`: seeded, re-runnable accuracy and throughput benchmark.
- `tests/test_expert2.py`: 46 new tests (116 -> 162).

### Changed
- FOCUS export now follows FOCUS 1.4: adds BillingAccountId, BillingAccountName,
  ChargeClass, ChargeFrequency, ContractedCost, HostProviderName,
  InvoiceIssuerName, PricingQuantity, PricingUnit, ServiceProviderName and
  drops the old `ProviderName` / `PublisherName`. **Breaking for scripts that
  read those two columns.**
- Ledger refuses `DELETE` and any `UPDATE` except filling an unpriced cost
  (`reprice` still works). Existing ledgers gain the guards on next open.
- Ingest is about 10-20% slower because every tag and model name is sanitized.

### Security
- Tags, provider and model names are stripped of ANSI/OSC escape sequences,
  control characters, bidi overrides and zero-width characters before storage
  and again before printing; credential-looking strings in tags are redacted.
- Error text echoed from input files is sanitized the same way.

## 0.3.0 — unreleased

### Fixed after the synapse-1 panel review
- **Mid-session Claude Code lines are no longer stored for good.** Lines without
  `stop_reason` are flagged `partial`; a later, more complete record with the
  same id (from the transcript or a `provider` export) appends a compensating
  correction event `<key>#rev:<n>` with the token/cost difference. Final records
  are never revised, and re-ingesting is still a no-op. Reports count events
  excluding corrections.
- **`anomalies --fail-on-anomaly` now fails on every CI retry**: the gate ignores
  the 24h display cooldown. A dismissal now mutes only the day it acknowledged,
  not the scope forever.
- **Gates fail closed**: with `--fail-on-alert` / `--fail-on-anomaly` a missing
  ledger file is an error (no empty db is created), zero budgets is an error, and
  invalid budget entries (unknown dimension, non-positive amount) are an error —
  and are always reported instead of silently skipped. Unpriced events inside a
  budget scope are shown and trip the budget gate (exit 3).
- README no longer shows real usage from the development machine; it uses the
  labelled synthetic transcripts in `examples/claude-code-demo/` (generator:
  `examples/make_claude_code_demo.py`), and a test keeps the README output exact.
- CSV/FOCUS export: formula prefixes after leading (including Unicode)
  whitespace are neutralized.
- FOCUS export: `BilledCost`/`EffectiveCost`/`ListCost` are never empty; groups
  with unpriced events get 0 plus `x_Unpriced=true`, and the CLI warns.

### Added after review
- Price book: Claude Fable 5.1, Mythos 5.1, Fable 5 and Mythos 5, at rates
  verified on platform.claude.com/docs/en/about-claude/pricing (2026-11-06).
- Tests: 95 -> 116.


### Added
- `claude-code` adapter: zero-config ingest of Claude Code transcripts
  (`~/.claude/projects`, `~/.config/claude/projects`, or `CLAUDE_CONFIG_DIR`).
  De-duplicates split responses and forked sessions on `message.id` (keeping the
  most complete line), uses the same key as the `provider` adapter so the two
  sources never double-count, tags sub-agents, never stores message text.
- SKU normalization in pricing: exact → `aliases:` → Anthropic dated snapshots,
  Bedrock and Vertex model IDs. Non-exact matches are recorded on the event
  (`priced_as`, `priced_via`) and listed by `doctor`. OpenAI dated snapshots are
  deliberately not guessed.
- Price book: Anthropic rates refreshed from the official pricing page
  (2026-11-06), new `cache_write_1h` rate for 1-hour cache writes, and per-request
  `tiers` (used for Haiku 5.5's >100K-token prompt pricing).
- `agentfin export --format focus|csv|jsonl` — FOCUS-aligned daily cost rows or
  raw events; spreadsheet formula injection is neutralized.
- `budgets check --fail-on-alert` and `anomalies --fail-on-anomaly` exit with
  status 3 for CI/cron gates.
- `ingest` accepts directories, `-` for stdin, and UTF-8 BOMs; malformed lines
  are skipped and reported as `file:line: reason` (`--strict` writes nothing
  instead). `--adapter claude-code` needs no file arguments.
- `agentfin --version`.

### Fixed
- **OpenAI cached tokens were billed twice**: `cached_tokens` is a subset of
  `prompt_tokens` / `input_tokens`, but was priced both as full input and as
  cache read. Now split. Responses-API `input_tokens_details` and `created_at`
  are read too. Events already in a ledger are not rewritten (append-only).
- `--to YYYY-MM-DD` silently dropped every event on that day after midnight; it
  is now inclusive of the whole day. Invalid dates are a usage error instead of a
  silent string comparison.
- `ingested … unpriced` count included lines that then failed to insert.
- A malformed `pricing.yaml` (non-numeric or negative rate) now fails fast with
  the SKU named, instead of failing every line.
- `__version__` reported 0.1.0 while the package was 0.2.0.
- The repository no longer tracks `.venv/`, `__pycache__/` and `*.egg-info/`
  (added `.gitignore`).

### Changed
- Adapters implement `parse_record(dict)`; `parse(lines)` still works as before.
- `load_pricing()` returns a `PriceBook` (a `dict` subclass with `.aliases`), so
  existing callers are unaffected.
