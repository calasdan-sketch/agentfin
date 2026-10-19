"""End-to-end: init -> ingest (all 3 adapters) -> report -> dedupe -> reconcile."""

import json

from click.testing import CliRunner

from agentfin import ledger
from agentfin.cli import main


def _write(path, lines):
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def test_full_pipeline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    r = runner.invoke(main, ["init"])
    assert r.exit_code == 0, r.output

    # Pricing file has our SKUs; ingest one file per adapter.
    _write(tmp_path / "a.jsonl", [{
        "ts": "2026-10-19T09:00:00Z", "provider": "anthropic",
        "model": "claude-sonnet-4-5", "input_tokens": 1_000_000,
        "output_tokens": 0, "agent": "cc", "team": "platform",
        "task": "refactor", "message_id": "m1",
        "metadata": {"prompt": "do not store this"},
    }])
    _write(tmp_path / "b.jsonl", [{
        "id": "chatcmpl-1", "model": "gpt-4o-mini", "created": 1760878920,
        "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0},
        "agentfin": {"agent": "batch", "team": "data", "task": "etl"},
    }])
    _write(tmp_path / "c.jsonl", [{
        "ts": "2026-10-19T10:00:00Z", "route": "openai/gpt-4o",
        "tokens_in": 1_000_000, "tokens_out": 0, "agent": "jarvis",
        "project": "onfile", "run_id": "run_1", "cost_usd": 2.50,
    }])

    r = runner.invoke(main, ["ingest", "--adapter", "jsonl", "a.jsonl"])
    assert r.exit_code == 0, r.output
    assert "ingested 1 events" in r.output

    r = runner.invoke(main, ["ingest", "--adapter", "provider", "b.jsonl"])
    assert r.exit_code == 0, r.output

    r = runner.invoke(main, ["ingest", "--adapter", "gateway", "c.jsonl"])
    assert r.exit_code == 0, r.output

    # Re-ingest must be a no-op (idempotency keys).
    r = runner.invoke(main, ["ingest", "--adapter", "jsonl", "a.jsonl"])
    assert "1 duplicates skipped" in r.output
    assert "ingested 0 events" in r.output

    # PII redaction happened before storage.
    conn = ledger.connect(tmp_path / ".agentfin" / "agentfin.db")
    meta = conn.execute(
        "SELECT metadata_json FROM usage_events WHERE idem_key='anthropic:m1'"
    ).fetchone()["metadata_json"]
    assert "do not store this" not in meta
    assert "[REDACTED]" in meta

    # Expected costs: sonnet 1M in @ $3.00 = $3.00; mini 1M in @ $0.15 = $0.15;
    # gateway pre-computed $2.50.
    rows = conn.execute(
        "SELECT idem_key, cost_micro_usd FROM usage_events"
    ).fetchall()
    costs = {row["idem_key"]: row["cost_micro_usd"] for row in rows}
    assert costs["anthropic:m1"] == 3_000_000
    assert costs["openai:chatcmpl-1"] == 150_000
    assert costs["gateway:run_1"] == 2_500_000
    conn.close()

    # Report works and shows all three teams/agents.
    r = runner.invoke(main, ["report", "--by", "team"])
    assert r.exit_code == 0, r.output
    assert "platform" in r.output and "data" in r.output

    # Reconcile against an invoice CSV.
    (tmp_path / "invoice.csv").write_text(
        "sku,cost_usd\n"
        "anthropic:claude-sonnet-4-5,3.00\n"
        "openai:gpt-4o-mini,0.15\n"
        "openai:gpt-4o,2.50\n"
    )
    r = runner.invoke(main, ["reconcile", "invoice.csv"])
    assert r.exit_code == 0, r.output
    assert "total variance: $0.0000" in r.output

    # Outcome + ROI.
    r = runner.invoke(main, ["outcome", "refactor", "--kind", "hours_saved",
                             "--value", "4"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(main, ["roi"])
    assert r.exit_code == 0, r.output
    assert "refactor" in r.output

    # Dashboard export.
    r = runner.invoke(main, ["dashboard", "--out", "dash.html"])
    assert r.exit_code == 0, r.output
    html = (tmp_path / "dash.html").read_text()
    assert "agentfin dashboard" in html and "$5.65" in html


def test_reprice_fills_unpriced(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(main, ["init"])

    (tmp_path / "u.jsonl").write_text(json.dumps({
        "ts": "2026-10-19T09:00:00Z", "provider": "mystery",
        "model": "future-model-9", "input_tokens": 1_000_000,
        "output_tokens": 0,
    }) + "\n")
    r = runner.invoke(main, ["ingest", "--adapter", "jsonl", "u.jsonl"])
    assert "1 unpriced" in r.output

    # Add the SKU to pricing, then reprice fills the NULL cost.
    ppath = tmp_path / ".agentfin" / "pricing.yaml"
    text = ppath.read_text()
    ppath.write_text(text + "  mystery:future-model-9:\n    input: 5.00\n    output: 20.00\n")
    r = runner.invoke(main, ["reprice"])
    assert r.exit_code == 0, r.output
    assert "repriced 1 of 1" in r.output

    conn = ledger.connect(tmp_path / ".agentfin" / "agentfin.db")
    cost = conn.execute("SELECT cost_micro_usd FROM usage_events").fetchone()[0]
    conn.close()
    assert cost == 5_000_000


def test_doctor_warns_on_cross_source_duplicates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(main, ["init"])
    rec = {"ts": "2026-10-19T09:00:00Z", "provider": "anthropic",
           "model": "claude-sonnet-4-5", "input_tokens": 100,
           "output_tokens": 10, "message_id": "dup-a"}
    (tmp_path / "a.jsonl").write_text(json.dumps(rec) + "\n")
    rec2 = dict(rec, message_id="dup-b")  # same call, different idem key
    (tmp_path / "b.jsonl").write_text(json.dumps(rec2) + "\n")
    runner.invoke(main, ["ingest", "--adapter", "jsonl", "a.jsonl"])
    runner.invoke(main, ["ingest", "--adapter", "jsonl", "b.jsonl"])
    r = runner.invoke(main, ["doctor"])
    assert r.exit_code == 0, r.output
    assert "double-ingest" in r.output


def test_report_fx_footnote(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(main, ["init"])
    cfg = tmp_path / ".agentfin" / "config.yaml"
    cfg.write_text("display_currency: CAD\nfx:\n  CAD: 1.36\nredact_keys: []\n")
    r = runner.invoke(main, ["report", "--by", "team"])
    assert r.exit_code == 0, r.output
    assert "FX estimate" in r.output


def test_budget_low_confidence_flag(tmp_path):
    from datetime import datetime, timezone

    from agentfin import budgets, ledger
    conn = ledger.connect(tmp_path / "b.db")
    now = datetime.now(timezone.utc).replace(day=1, hour=12)
    blist = [{"scope": "team:t", "monthly_usd": 100, "alert_at": 0.8}]
    results = budgets.check(conn, blist, now=now)
    conn.close()
    assert results[0]["forecast_low_confidence"] is True
