"""Regression tests for the synapse-1 panel review findings."""

import csv
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from agentfin import export, ledger, pricing, reports
from agentfin.cli import ALERT_EXIT_CODE, main

ROOT = Path(__file__).resolve().parent.parent


def _cc(mid, out, stop=None, ts="2026-11-01T10:00:00Z"):
    msg = {"id": mid, "model": "claude-sonnet-4-5", "usage": {
        "input_tokens": 1000, "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0, "output_tokens": out}}
    if stop:
        msg["stop_reason"] = stop
    return json.dumps({"type": "assistant", "timestamp": ts, "cwd": "/w/proj",
                       "uuid": f"u{out}", "message": msg})


def _total(conn):
    return reports.totals(conn)


# --- 1. partial mid-session lines --------------------------------------------

def test_partial_line_is_revised_by_later_complete_line(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(main, ["init"])
    t = tmp_path / "s.jsonl"
    t.write_text(_cc("msg_1", 5) + "\n")
    r = runner.invoke(main, ["ingest", "--adapter", "claude-code", str(t)])
    assert "ingested 1 events" in r.output
    t.write_text(_cc("msg_1", 5) + "\n" + _cc("msg_1", 50_000, "end_turn") + "\n")
    r = runner.invoke(main, ["ingest", "--adapter", "claude-code", str(t)])
    assert "1 partial records revised" in r.output, r.output
    conn = ledger.connect(".agentfin/agentfin.db")
    tot = _total(conn)
    # 1000 in @ $3 + 50,000 out @ $15 = $0.753 — not the partial $0.003075
    assert tot["cost_micro_usd"] == 1000 * 3 + 50_000 * 15
    assert tot["events"] == 1  # the correction is not a second response
    out = reports.summary(conn, "task")[0]
    assert out["output_tokens"] == 50_000 and out["events"] == 1
    # Re-ingesting the finished transcript is now a pure no-op.
    r = runner.invoke(main, ["ingest", "--adapter", "claude-code", str(t)])
    assert "ingested 0 events (1 duplicates skipped" in r.output
    assert "revised" not in r.output
    assert _total(conn)["cost_micro_usd"] == 1000 * 3 + 50_000 * 15


def test_partial_line_revised_by_provider_record(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(main, ["init"])
    (tmp_path / "s.jsonl").write_text(_cc("msg_9", 5) + "\n")
    runner.invoke(main, ["ingest", "--adapter", "claude-code", "s.jsonl"])
    (tmp_path / "p.jsonl").write_text(json.dumps({
        "id": "msg_9", "model": "claude-sonnet-4-5", "created": 1761991200,
        "usage": {"input_tokens": 1000, "output_tokens": 800}}) + "\n")
    r = runner.invoke(main, ["ingest", "--adapter", "provider", "p.jsonl"])
    assert "1 partial records revised" in r.output
    conn = ledger.connect(".agentfin/agentfin.db")
    assert _total(conn)["cost_micro_usd"] == 1000 * 3 + 800 * 15


def test_complete_records_are_never_revised(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(main, ["init"])
    (tmp_path / "a.jsonl").write_text(_cc("msg_c", 100, "end_turn") + "\n")
    runner.invoke(main, ["ingest", "--adapter", "claude-code", "a.jsonl"])
    (tmp_path / "b.jsonl").write_text(_cc("msg_c", 999_999, "end_turn") + "\n")
    r = runner.invoke(main, ["ingest", "--adapter", "claude-code", "b.jsonl"])
    assert "1 duplicates skipped" in r.output and "revised" not in r.output


def test_partial_revised_by_still_partial_but_larger_then_final(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(main, ["init"])
    for out, stop in [(5, None), (3, None), (40, None), (40, None), (70, "end_turn")]:
        (tmp_path / "s.jsonl").write_text(_cc("msg_p", out, stop) + "\n")
        runner.invoke(main, ["ingest", "--adapter", "claude-code", "s.jsonl"])
    conn = ledger.connect(".agentfin/agentfin.db")
    rows = conn.execute("SELECT idem_key, output_tokens FROM usage_events ORDER BY id").fetchall()
    assert [r["idem_key"] for r in rows] == [
        "anthropic:msg_p", "anthropic:msg_p#rev:1", "anthropic:msg_p#rev:2"]
    assert sum(r["output_tokens"] for r in rows) == 70


# --- 2. anomaly gate on retry ------------------------------------------------------

def _spike_ledger(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(main, ["init"])
    today = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    lines = []
    for d in range(20, 0, -1):
        lines.append({"ts": (today - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "provider": "openai", "model": "gpt-4o", "input_tokens": 1_000_000,
                      "agent": "bot", "message_id": f"d{d}"})
    lines.append({"ts": today.strftime("%Y-%m-%dT%H:%M:%SZ"), "provider": "openai",
                  "model": "gpt-4o", "input_tokens": 100_000_000, "agent": "bot",
                  "message_id": "spike"})
    (tmp_path / "e.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    runner.invoke(main, ["ingest", "e.jsonl"])
    return runner


def test_anomaly_gate_fails_again_on_retry(tmp_path, monkeypatch):
    runner = _spike_ledger(tmp_path, monkeypatch)
    for _ in range(2):
        r = runner.invoke(main, ["anomalies", "--fail-on-anomaly"])
        assert r.exit_code == ALERT_EXIT_CODE, r.output
    # Display mode still honours the cooldown.
    assert "no anomalies" in runner.invoke(main, ["anomalies"]).output


def test_dismissal_mutes_only_that_day(tmp_path, monkeypatch):
    runner = _spike_ledger(tmp_path, monkeypatch)
    runner.invoke(main, ["anomalies"])
    runner.invoke(main, ["alerts", "--dismiss", "anomaly:agent:bot"])
    r = runner.invoke(main, ["anomalies", "--fail-on-anomaly"])
    assert r.exit_code == 0, r.output  # acknowledged today's spike
    conn = ledger.connect(".agentfin/agentfin.db")
    conn.execute("UPDATE alert_state SET detail_json = ? WHERE alert_key = ?",
                 (json.dumps({"day": "2000-01-01"}), "anomaly:agent:bot"))
    conn.commit()
    r = runner.invoke(main, ["anomalies", "--fail-on-anomaly"])
    assert r.exit_code == ALERT_EXIT_CODE  # an old dismissal no longer mutes new spikes


# --- 3. gates fail closed -------------------------------------------------------------

@pytest.mark.parametrize("cmd", [["anomalies", "--fail-on-anomaly"],
                                 ["budgets", "check", "--fail-on-alert"]])
def test_gates_refuse_missing_db(tmp_path, monkeypatch, cmd):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(main, ["--db", "typo.db", *cmd])
    assert r.exit_code == 1 and "ledger not found" in r.output
    assert not (tmp_path / "typo.db").exists()


def test_budget_gate_refuses_zero_budgets(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(main, ["init"])
    Path(".agentfin/budgets.yaml").write_text("budgets: []\n")
    r = runner.invoke(main, ["budgets", "check", "--fail-on-alert"])
    assert r.exit_code == 1 and "no budgets configured" in r.output
    assert runner.invoke(main, ["budgets", "check"]).exit_code == 0  # display mode is fine


def test_budget_gate_refuses_unknown_dimension(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(main, ["init"])
    Path(".agentfin/budgets.yaml").write_text(yaml.safe_dump({"budgets": [
        {"scope": "team:platform", "monthly_usd": 500},
        {"scope": "teams:typo", "monthly_usd": 10},
        {"scope": "agent:x", "monthly_usd": "lots"},
    ]}))
    r = runner.invoke(main, ["budgets", "check"])
    assert r.exit_code == 0 and "invalid budget entry skipped: teams:typo" in r.output
    assert "agent:x" in r.output
    r = runner.invoke(main, ["budgets", "check", "--fail-on-alert"])
    assert r.exit_code == 1 and "invalid entries" in r.output


def test_budget_gate_trips_on_unpriced_spend(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(main, ["init"])
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (tmp_path / "e.jsonl").write_text(json.dumps({
        "ts": now, "provider": "mystery", "model": "big-9", "input_tokens": 10**9,
        "team": "platform", "message_id": "u1"}) + "\n")
    runner.invoke(main, ["ingest", "e.jsonl"])
    r = runner.invoke(main, ["budgets", "check"])
    assert "UNPRICED events (true spend unknown)" in r.output and r.exit_code == 0
    r = runner.invoke(main, ["budgets", "check", "--fail-on-alert"])
    assert r.exit_code == ALERT_EXIT_CODE


# --- 4. README demo is synthetic and reproducible ----------------------------------------

def test_readme_has_no_real_spend_and_demo_output_matches(tmp_path, monkeypatch):
    readme = (ROOT / "README.md").read_text()
    assert "development machine" not in readme
    assert "Real output" not in readme
    block = readme.split("$ agentfin ingest --adapter claude-code examples/claude-code-demo\n")[1]
    block = block.split("```")[0]
    expected_ingest, rest = block.split("\n$ agentfin report --by task\n")
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(main, ["init"])
    r = runner.invoke(main, ["ingest", "--adapter", "claude-code",
                             str(ROOT / "examples" / "claude-code-demo")])
    assert r.output.strip() == expected_ingest.strip()
    r = runner.invoke(main, ["report", "--by", "task"])
    got = [ln.rstrip() for ln in r.output.strip().splitlines()]
    assert got == [ln.rstrip() for ln in rest.strip().splitlines()]
    meta = ROOT / "examples" / "make_claude_code_demo.py"
    assert "SYNTHETIC" in meta.read_text()


# --- 5/6. export ------------------------------------------------------------------------------

@pytest.mark.parametrize("value", [" =1+1", "\t=1+1", "  @SUM(A1)", " +1"])
def test_csv_injection_with_leading_whitespace(value):
    assert export.safe_cell(value).startswith("'")


def test_focus_billed_cost_never_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(main, ["init"])
    (tmp_path / "e.jsonl").write_text(json.dumps({
        "ts": "2026-10-31T10:00:00Z", "provider": "mystery", "model": "m",
        "input_tokens": 5, "team": " =1+1", "message_id": "x"}) + "\n")
    runner.invoke(main, ["ingest", "e.jsonl"])
    r = runner.invoke(main, ["export"])
    rows = list(csv.DictReader(io.StringIO(r.stdout)))
    for col in ("BilledCost", "EffectiveCost", "ListCost"):
        assert rows[0][col] != ""
    r = runner.invoke(main, ["export", "--format", "csv"])
    assert "' =1+1" in r.stdout


# --- verified Anthropic additions ------------------------------------------------------------

@pytest.mark.parametrize("model,cache_hit", [
    ("claude-fable-5-1", 0.25), ("claude-mythos-5-1", 0.25),
    ("claude-fable-5", 1.00), ("claude-mythos-5", 1.00),
])
def test_fable_mythos_prices(model, cache_hit):
    data = yaml.safe_load(pricing.DEFAULT_PRICING)
    rates = data["skus"][f"anthropic:{model}"]
    assert (rates["input"], rates["cache_write"], rates["cache_write_1h"], rates["output"]) \
        == (10.0, 12.5, 20.0, 50.0)
    assert rates["cache_read"] == cache_hit
    book = pricing.PriceBook(data["skus"], data["aliases"])
    assert pricing.resolve(f"bedrock:anthropic.{model}", book)[0] == f"anthropic:{model}"

