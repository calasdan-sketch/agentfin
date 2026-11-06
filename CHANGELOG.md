# Changelog

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
