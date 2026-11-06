"""Tests for the 0.3.0 round: Claude Code adapter, SKU normalization, tolerant
ingest, OpenAI cached-token split, CI exit codes, FOCUS export, date windows."""

import csv
import io
import json
import re
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

import agentfin
from agentfin import export, ledger, pricing, reports
from agentfin.adapters.claude_code import ClaudeCodeAdapter, _project_name
from agentfin.adapters.provider import ProviderAdapter
from agentfin.cli import ALERT_EXIT_CODE, main

ROOT = Path(__file__).resolve().parent.parent


def _cc_line(mid, out=10, stop=None, ts="2026-11-01T10:00:00Z", **extra):
    msg = {
        "id": mid, "model": "claude-sonnet-4-5-20250929", "role": "assistant",
        "content": [{"type": "text", "text": "SECRET PROMPT TEXT"}],
        "usage": {"input_tokens": 1000, "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 0, "output_tokens": out},
    }
    if stop:
        msg["stop_reason"] = stop
    d = {"type": "assistant", "timestamp": ts, "sessionId": "s1",
         "requestId": "req_1", "cwd": "/home/dev/acme-api", "uuid": f"u-{mid}-{out}",
         "message": msg}
    d.update(extra)
    return json.dumps(d)


def _book():
    data = yaml.safe_load(pricing.DEFAULT_PRICING)
    return pricing.PriceBook(data["skus"], data["aliases"])


# --- version ---------------------------------------------------------------

def test_version_matches_pyproject():
    text = (ROOT / "pyproject.toml").read_text()
    assert agentfin.__version__ == re.search(r'^version = "([^"]+)"', text, re.M).group(1)
    r = CliRunner().invoke(main, ["--version"])
    assert agentfin.__version__ in r.output


# --- SKU normalization ------------------------------------------------------

@pytest.mark.parametrize("sku,expected", [
    ("anthropic:claude-sonnet-4-5-20250929", "anthropic:claude-sonnet-4-5"),
    ("bedrock:us.anthropic.claude-sonnet-4-5-20250929-v1:0", "anthropic:claude-sonnet-4-5"),
    ("bedrock:anthropic.claude-opus-4-7", "anthropic:claude-opus-4-7"),
    ("vertex_ai:claude-haiku-4-5@20251001", "anthropic:claude-haiku-4-5"),
    ("anthropic:claude-opus-4-20250514", "anthropic:claude-opus-4"),
    ("anthropic:claude-opus-4-0", "anthropic:claude-opus-4"),
    ("ANTHROPIC:Claude-Opus-4-8", "anthropic:claude-opus-4-8"),
    ("anthropic:claude-opus-5-5", "anthropic:claude-opus-5-5"),
])
def test_resolve_normalizes_safely(sku, expected):
    priced_as, rates, _ = pricing.resolve(sku, _book())
    assert priced_as == expected and rates


@pytest.mark.parametrize("sku", [
    "openai:gpt-4o-2024-05-13",       # dated OpenAI snapshots differ in price
    "anthropic:claude-sonnet-9",       # unknown model never borrows a price
    "mystery:anthropic.",              # degenerate bedrock-ish id
    "anthropic:",                      # empty model
    "anthropic:claude-sonnet-4-5-2025",  # not an 8-digit snapshot date
])
def test_resolve_refuses_unsafe_guesses(sku):
    assert pricing.resolve(sku, _book())[0] is None
    assert pricing.cost_micro_usd(sku, _book(), {"input_tokens": 10}) is None


def test_long_context_tier():
    book = _book()
    small = {"input_tokens": 100_000}
    large = {"input_tokens": 200_000}
    assert pricing.cost_micro_usd("anthropic:claude-haiku-5-5", book, small) == 10_000
    assert pricing.cost_micro_usd("anthropic:claude-haiku-5-5", book, large) == 100_000  # $0.50/M


def test_bad_price_values_rejected():
    with pytest.raises(ValueError):
        pricing.cost_micro_usd("a:b", {"a:b": {"input": "free"}}, {"input_tokens": 1})
    with pytest.raises(ValueError):
        pricing.cost_micro_usd("a:b", {"a:b": {"input": -1}}, {"input_tokens": 1})


def test_plain_dict_pricing_still_works():
    # Library users passing a bare dict (pre-0.3 API) keep working.
    assert pricing.cost_micro_usd(
        "anthropic:claude-sonnet-4-5-20250929",
        {"anthropic:claude-sonnet-4-5": {"input": 3.0}},
        {"input_tokens": 1_000_000},
    ) == 3_000_000


# --- OpenAI cached-token split ----------------------------------------------

def test_openai_responses_api_cached_subset():
    line = json.dumps({
        "id": "resp_1", "model": "gpt-4o", "created_at": 1760878920,
        "usage": {"input_tokens": 15000, "output_tokens": 10,
                  "input_tokens_details": {"cached_tokens": 12000,
                                           "cache_write_tokens": 3000}},
    })
    e = list(ProviderAdapter().parse([line]))[0]
    assert (e["input_tokens"], e["cache_read_tokens"], e["cache_write_tokens"]) == (0, 12000, 3000)


def test_openai_cached_larger_than_prompt_clamps():
    line = json.dumps({"id": "chatcmpl-2", "model": "gpt-4o", "created": 1,
                       "usage": {"prompt_tokens": 5, "prompt_tokens_details": {"cached_tokens": 9}}})
    assert list(ProviderAdapter().parse([line]))[0]["input_tokens"] == 0


# --- Claude Code adapter ----------------------------------------------------

def test_claude_code_dedupes_split_responses_and_keeps_best():
    lines = [
        _cc_line("msg_A", out=1),
        _cc_line("msg_A", out=1),
        _cc_line("msg_A", out=250, stop="end_turn"),
        json.dumps({"type": "user", "message": {"content": "hi"}}),
        _cc_line("msg_B", out=5),
    ]
    events = list(ClaudeCodeAdapter().parse(lines))
    assert [e["idem_key"] for e in events] == ["anthropic:msg_A", "anthropic:msg_B"]
    assert events[0]["output_tokens"] == 250
    assert events[0]["task"] == "acme-api"
    assert events[0]["agent"] == "claude-code"
    assert "SECRET" not in json.dumps(events)


def test_claude_code_sidechain_and_zero_usage():
    a = ClaudeCodeAdapter()
    e = a.parse_record(json.loads(_cc_line("msg_S", isSidechain=True)))
    assert e["agent"] == "claude-code-subagent"
    zero = json.loads(_cc_line("msg_Z", out=0))
    zero["message"]["usage"]["input_tokens"] = 0
    assert a.parse_record(zero) is None
    assert a.parse_record({"type": "assistant", "message": "not a dict"}) is None


@pytest.mark.parametrize("cwd,name", [
    ("/home/dev/acme-api", "acme-api"),
    ("C:\\Users\\dan\\code\\on-file", "on-file"),
    ("D:/work/lead-me", "lead-me"),
    ("/", ""),
    ("", ""),
    (None, ""),
    (42, ""),
])
def test_project_name_windows_and_posix(cwd, name):
    assert _project_name(cwd) == name


def test_claude_code_cli_discovery_and_cross_source_dedupe(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "claudecfg"
    proj = cfg / "projects" / "-home-dev-acme-api"
    proj.mkdir(parents=True)
    (proj / "s1.jsonl").write_text(_cc_line("msg_A", out=1) + "\n"
                                   + _cc_line("msg_A", out=300, stop="end_turn") + "\n")
    # A forked session copies history into a second file.
    (proj / "s2.jsonl").write_text(_cc_line("msg_A", out=300, stop="end_turn") + "\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    runner = CliRunner()
    assert runner.invoke(main, ["init"]).exit_code == 0
    r = runner.invoke(main, ["ingest", "--adapter", "claude-code"])
    assert r.exit_code == 0, r.output
    assert "ingested 1 events" in r.output
    # Same response exported via the provider adapter is a no-op.
    (tmp_path / "p.jsonl").write_text(json.dumps({
        "id": "msg_A", "model": "claude-sonnet-4-5", "created": 1761991200,
        "usage": {"input_tokens": 1000, "output_tokens": 300}}) + "\n")
    r = runner.invoke(main, ["ingest", "--adapter", "provider", "p.jsonl"])
    assert "ingested 0 events (1 duplicates" in r.output
    conn = ledger.connect(".agentfin/agentfin.db")
    row = conn.execute("SELECT * FROM usage_events").fetchone()
    assert row["output_tokens"] == 300
    # Dated model id priced through normalization, with an audit trail.
    assert row["cost_micro_usd"] == 1000 * 3 + 300 * 15
    assert json.loads(row["metadata_json"])["priced_as"] == "anthropic:claude-sonnet-4-5"
    r = runner.invoke(main, ["doctor"])
    assert "claude-sonnet-4-5-20250929 → anthropic:claude-sonnet-4-5" in r.output


def test_claude_code_no_transcripts_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nope"))
    r = CliRunner().invoke(main, ["ingest", "--adapter", "claude-code"])
    assert r.exit_code == 1 and "no Claude Code transcripts" in r.output


def test_other_adapters_still_require_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(main, ["ingest"])
    assert r.exit_code == 2


# --- tolerant / strict ingest ----------------------------------------------

GOOD = json.dumps({"ts": "2026-11-01T00:00:00Z", "provider": "openai", "model": "gpt-4o",
                   "input_tokens": 10, "message_id": "ok1"})


def _bad_file(tmp_path):
    f = tmp_path / "mixed.jsonl"
    f.write_text("\n".join([
        GOOD,
        "{not json",
        "[1, 2]",
        json.dumps({"ts": "yesterday-ish", "provider": "openai", "model": "x"}),
        json.dumps({"ts": "2026-11-01T00:00:00Z", "provider": "openai", "model": "gpt-4o",
                    "input_tokens": "lots"}),
        "\ufeff" + GOOD.replace("ok1", "ok2"),
    ]) + "\n", encoding="utf-8")
    return f


def test_tolerant_ingest_reports_file_and_line(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = _bad_file(tmp_path)
    r = CliRunner().invoke(main, ["ingest", str(f)])
    assert r.exit_code == 0, r.output
    assert "ingested 2 events" in r.output
    assert "4 malformed line(s)" in r.output
    for n in (2, 3, 4, 5):
        assert f"mixed.jsonl:{n}:" in r.output


def test_strict_ingest_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = _bad_file(tmp_path)
    r = CliRunner().invoke(main, ["ingest", "--strict", str(f)])
    assert r.exit_code == 1 and "nothing written" in r.output
    conn = ledger.connect(".agentfin/agentfin.db")
    assert conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 0


def test_bom_on_first_line_and_stdin(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "bom.jsonl"
    f.write_bytes(b"\xef\xbb\xbf" + GOOD.encode() + b"\n")
    r = CliRunner().invoke(main, ["ingest", str(f)])
    assert "ingested 1 events" in r.output, r.output
    r = CliRunner().invoke(main, ["ingest", "-"], input=GOOD.replace("ok1", "ok3") + "\n")
    assert "ingested 1 events" in r.output, r.output


def test_directory_input(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "logs" / "nested"
    d.mkdir(parents=True)
    (d / "a.jsonl").write_text(GOOD + "\n")
    (d / "ignore.txt").write_text("not jsonl")
    r = CliRunner().invoke(main, ["ingest", str(tmp_path / "logs")])
    assert "ingested 1 events" in r.output, r.output


# --- CI exit codes ------------------------------------------------------------

def test_budget_gate_exit_code(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(main, ["init"])
    runner.invoke(main, ["budgets", "set", "team:platform", "1"])
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (tmp_path / "e.jsonl").write_text(json.dumps({
        "ts": now, "provider": "openai", "model": "gpt-4o", "input_tokens": 10_000_000,
        "team": "platform", "message_id": "big"}) + "\n")
    runner.invoke(main, ["ingest", "e.jsonl"])
    assert runner.invoke(main, ["budgets", "check"]).exit_code == 0
    r = runner.invoke(main, ["budgets", "check", "--fail-on-alert"])
    assert r.exit_code == ALERT_EXIT_CODE, r.output


def test_anomaly_gate_quiet_when_clean(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    CliRunner().invoke(main, ["init"])
    r = CliRunner().invoke(main, ["anomalies", "--fail-on-anomaly"])
    assert r.exit_code == 0 and "no anomalies" in r.output


# --- date windows ---------------------------------------------------------------

def test_date_to_is_inclusive_of_whole_day():
    conn = ledger.connect(":memory:")
    ledger.insert_event(conn, {"idem_key": "k", "ts": "2026-10-31T23:30:00Z",
                               "provider": "p", "model_raw": "m", "cost_micro_usd": 5})
    assert reports.totals(conn, date_to="2026-10-31")["events"] == 1
    assert reports.totals(conn, date_from="2026-11-01")["events"] == 0
    assert reports.totals(conn, date_to="2026-10-31T23:00:00Z")["events"] == 0
    with pytest.raises(ValueError):
        reports.totals(conn, date_to="2026-13-45")
    with pytest.raises(ValueError):
        reports.totals(conn, date_from="'; DROP TABLE usage_events; --")


# --- FOCUS export -----------------------------------------------------------------

def test_focus_export_rows_and_injection_safety(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(main, ["init"])
    (tmp_path / "e.jsonl").write_text("\n".join(json.dumps(x) for x in [
        {"ts": "2026-10-31T10:00:00Z", "provider": "openai", "model": "gpt-4o",
         "input_tokens": 1_000_000, "task": "=HYPERLINK(\"http://x\")", "message_id": "1"},
        {"ts": "2026-10-31T11:00:00Z", "provider": "openai", "model": "gpt-4o",
         "output_tokens": 1_000_000, "task": "=HYPERLINK(\"http://x\")", "message_id": "2"},
        {"ts": "2026-10-31T12:00:00Z", "provider": "mystery", "model": "m9",
         "input_tokens": 5, "message_id": "3"},
    ]) + "\n")
    runner.invoke(main, ["ingest", "e.jsonl"])
    r = runner.invoke(main, ["export", "--format", "focus", "--to", "2026-10-31"])
    assert r.exit_code == 0, r.output
    rows = list(csv.DictReader(io.StringIO(r.stdout)))
    assert len(rows) == 2
    assert "1 FOCUS row(s) contain UNPRICED" in r.stderr
    priced = next(x for x in rows if x["SkuId"] == "openai:gpt-4o")
    assert priced["BilledCost"] == "12.500000"
    assert priced["ConsumedQuantity"] == "2000000"
    assert priced["ServiceCategory"] == "AI and Machine Learning"
    assert priced["ChargePeriodStart"] == "2026-10-31T00:00:00Z"
    assert priced["BillingPeriodEnd"] == "2026-11-01T00:00:00Z"
    assert json.loads(priced["Tags"])["task"].startswith("=")  # JSON cell starts with {
    unpriced = next(x for x in rows if x["SkuId"] == "mystery:m9")
    # FOCUS requires BilledCost: 0 plus an explicit flag, never an empty cell.
    assert unpriced["BilledCost"] == "0.000000" and unpriced["x_Unpriced"] == "true"
    r = runner.invoke(main, ["export", "--format", "csv", "--out", "ev.csv"])
    assert "wrote 3 csv rows" in r.output
    text = (tmp_path / "ev.csv").read_text()
    assert "'=HYPERLINK" in text and ",=HYPERLINK" not in text
    r = runner.invoke(main, ["export", "--format", "jsonl"])
    assert len(r.output.strip().splitlines()) == 3


def test_safe_cell():
    assert export.safe_cell("=1+1") == "'=1+1"
    assert export.safe_cell("-2+3") == "'-2+3"
    assert export.safe_cell("@SUM") == "'@SUM"
    assert export.safe_cell("normal") == "normal"
    assert export.safe_cell(" =1+1") == "' =1+1"      # leading whitespace (LibreOffice trim)
    assert export.safe_cell("\u00a0\t=cmd") == "'\u00a0\t=cmd"  # unicode spaces too
    assert export.safe_cell(-5) == -5  # numbers untouched
    assert export.safe_cell("-1.500000") == "-1.500000"


def test_export_bad_date_is_usage_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(main, ["export", "--from", "nonsense", "--out", "x.csv"])
    assert r.exit_code == 2
    assert not (tmp_path / "x.csv").exists()


def test_one_hour_cache_writes_priced_at_their_own_rate():
    line = _cc_line("msg_1h")
    d = json.loads(line)
    d["message"]["usage"].update({
        "input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 3_000_000,
        "cache_creation": {"ephemeral_5m_input_tokens": 1_000_000,
                           "ephemeral_1h_input_tokens": 2_000_000},
    })
    e = ClaudeCodeAdapter().parse_record(d)
    assert e["cache_write_1h_tokens"] == 2_000_000
    # sonnet-4-5: 1M x $3.75 (5m) + 2M x $6.00 (1h) = $15.75
    assert pricing.cost_micro_usd(
        "anthropic:claude-sonnet-4-5-20250929", _book(), e) == 15_750_000
    # A SKU without a 1h rate falls back to cache_write, never to zero.
    assert pricing.cost_micro_usd(
        "x:y", {"x:y": {"cache_write": 1.0}}, e) == 3_000_000
    # 1h count can never exceed the total cache-write count.
    assert pricing.cost_micro_usd("x:y", {"x:y": {"cache_write": 1.0, "cache_write_1h": 9.0}},
                                  {"cache_write_tokens": 1, "cache_write_1h_tokens": 10**9}) == 9


def test_broken_price_book_fails_fast(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(main, ["init"])
    Path(".agentfin/pricing.yaml").write_text(
        "skus:\n  a:b:\n    input: 1\n    tiers:\n      - above_prompt_tokens: lots\n")
    (tmp_path / "e.jsonl").write_text(GOOD + "\n")
    r = runner.invoke(main, ["ingest", "e.jsonl"])
    assert r.exit_code == 1 and "pricing.yaml" in r.output and "a:b" in r.output


def test_deeply_nested_line_is_rejected_not_crashing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    deep = '{"ts": "2026-11-01T00:00:00Z", "metadata": ' + '{"a": ' * 5000 + "1" + "}" * 5001
    (tmp_path / "d.jsonl").write_text(deep + "\n" + GOOD + "\n")
    r = CliRunner().invoke(main, ["ingest", "d.jsonl"])
    assert r.exit_code == 0, r.output
    assert "ingested 1 events" in r.output and "d.jsonl:1" in r.output
