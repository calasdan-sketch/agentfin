# agentfin — Agent FinOps: cost attribution & ROI

Which agent, which task, which team spent what — and was it worth it?

`agentfin` ingests token usage from your agent stack into an append-only
SQLite ledger, attributes every event to a task/agent/team via rules,
prices it from a YAML price book, and answers the Monday-morning question:
*did the $4K spike come from a runaway agent or real work?* Then it goes
one step further than every competitor: **cost-per-outcome and ROI**.

## Install

```bash
pip install agentfin        # (when published)
# or from source:
pip install -e ".[dev]"
```

## 60-second start

```bash
agentfin init                                   # scaffolds .agentfin/
agentfin ingest --adapter provider usage.jsonl  # Anthropic/OpenAI-style records
agentfin ingest --adapter gateway gateway.jsonl # calas-gateway ledger lines
agentfin ingest --adapter jsonl custom.jsonl    # your own shape
agentfin report --by team
agentfin anomalies                              # spikes + probable cause
agentfin budgets                                # month-end forecast vs caps
agentfin budgets set team:platform 500          # create/update a monthly cap
agentfin outcomes tag refactor --kind hours_saved --value 4
agentfin outcomes roi                           # cost-per-outcome per task
agentfin dashboard --out report.html            # static HTML, no server
```

## How it works

1. **Ingest** — adapters normalize provider usage records into one canonical
   event: `{ts_utc, provider, model, tokens in/cache/out, task, agent, team}`.
   Redaction runs *before* storage (prompts, keys, `sk-…` strings scrubbed).
2. **Attribute** — `rules.yaml` maps model/provider/metadata regexes to
   task/agent/team tags. Adapter-supplied tags win; defaults catch the rest.
3. **Price** — `pricing.yaml` holds exact-SKU rates (USD per 1M tokens).
   Cost is computed at ingest and stored as **integer micro-USD** (no float
   drift). SKUs with no price land in the **UNPRICED** bucket — loud in every
   report, never silently zero. `agentfin reprice` backfills once you add rates.
4. **Ledger** — SQLite, append-only. Every event carries an idempotency key,
   so re-ingesting the same file is a no-op (no double-counting). Corrections
   are new events, never edits.
5. **Analyze** — reports by agent/task/team/model, daily trends, robust
   anomaly detection (median/MAD, 14-day baseline minimum, 24h cooldown,
   probable-cause attached), budget burn-rate forecasts that alert *before*
   overrun, and ROI from tagged outcomes.
6. **Trust** — `agentfin reconcile invoice.csv` diffs ledger totals against
   your actual provider invoice, per SKU.

## Config files (in `.agentfin/`)

| file | purpose |
|---|---|
| `pricing.yaml` | exact-SKU USD/1M-token rates. Paste from the provider pricing page. |
| `rules.yaml` | attribution rules, first match wins. |
| `budgets.yaml` | monthly caps per team/agent with forecast-alert thresholds. |
| `config.yaml` | display currency + FX (marked `*` as estimates), extra redact keys. |

## Adapters

- `jsonl` — generic JSONL, flat or nested `usage` blocks, flexible key names.
- `provider` — raw Anthropic (`msg_…`) / OpenAI (`chatcmpl-…`) API responses;
  cache tokens mapped correctly. Optional `agentfin` sidecar for tags.
- `gateway` — calas-gateway ledger lines (`route: provider/model`);
  gateway-computed `cost_usd` is preserved verbatim, never re-priced.

## Commands

`init [--force] · ingest · report · trends · anomalies (detect) · alerts ·
budgets [check|set] · outcomes [tag|roi] · reconcile · reprice · dashboard ·
doctor`

`doctor` surfaces unpriced SKUs and unattributed events — the two things
that silently ruin FinOps data. `init` refuses to overwrite existing config
files unless `--force` is given. `outcome`/`roi` remain as hidden aliases
for `outcomes tag` / `outcomes roi`.

## Development

```bash
python -m pytest        # 37 tests
ruff check agentfin/ tests/
```

## Limitations (read before trusting the numbers)

- **Prices are yours to maintain.** Model prices change; `pricing.yaml` is a
  snapshot. Totals are only as right as your price book — reconcile monthly.
- **File-based ingest only.** v1 does not poll provider APIs; bring your own
  export or cron the API pull into JSONL.
- **Attribution is heuristic.** Rules are regexes over metadata. Garbage tags
  in → garbage attribution out. `doctor` shows the unattributed share.
- **Ledger is USD-only.** Multi-currency display is a report-time FX estimate
  (marked `*`), not accounting truth.
- **Anomalies need history.** 14 days of baseline minimum; new teams/agents
  get no anomaly coverage until then.
- **One source per call.** Idempotency keys stop same-file re-ingest, but the
  same API calls ingested via two adapters (e.g. gateway file + provider
  export) have different keys and will double-count. `doctor` warns on
  suspicious groups; ingest each source exactly once.
- **Anomalies evaluate the latest day in the data**, not necessarily today.
  Backfilled history gets a spike verdict on its last day.
- **Single-user SQLite.** No auth, no concurrency story beyond one writer.
  Fine for a team cron job; not a multi-tenant SaaS backend.
- **Outcome values are estimates.** `hours_saved × rate` is a model, not a
  bank deposit. ROI ranks tasks; don't put it in a 10-K.
