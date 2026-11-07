"""Round 2 (expert-2): terminal-safe text, append-only guards, Gemini/OTel adapters,
service-tier modifiers, month-end close, showback, what-if, git outcomes, FOCUS 1.4."""

from __future__ import annotations

import csv
import io
import json
import os
import random
import sqlite3
import subprocess
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import yaml
from click.testing import CliRunner

from agentfin import allocate, close, gitlink, ledger, money, pricing, textsafe, whatif
from agentfin.adapters.base import IngestContext
from agentfin.adapters.otel import OtelAdapter
from agentfin.adapters.provider import ProviderAdapter
from agentfin.cli import main

BOOK = pricing.PriceBook(
    {
        "anthropic:m-big": {"input": 10.0, "cache_read": 1.0, "cache_write": 12.5,
                            "output": 50.0},
        "anthropic:m-small": {"input": 2.0, "cache_read": 0.2, "cache_write": 2.5,
                              "output": 10.0},
    },
    modifiers={"batch": 0.5},
)


def conn_mem():
    return ledger.connect(":memory:")


def put(conn, key, ts, model="m-big", inp=1000, out=0, cr=0, cw=0, team="t1",
        task="x", cost="auto", **meta):
    ev = {"idem_key": key, "ts": ts, "provider": "anthropic", "model_raw": model,
          "input_tokens": inp, "output_tokens": out, "cache_read_tokens": cr,
          "cache_write_tokens": cw, "team": team, "task": task, "agent": "a",
          "metadata": meta}
    ev["cost_micro_usd"] = pricing.cost_micro_usd(f"anthropic:{model}", BOOK, ev) \
        if cost == "auto" else cost
    assert ledger.insert_event(conn, ev)
    conn.commit()


# ---------------------------------------------------------------- text safety

@pytest.mark.parametrize("raw,expected", [
    ("\x1b[2Jhello", "hello"),
    ("a\x1b]0;pwned\x07b", "ab"),
    ("a\x1b]0;pwned\x1b\\b", "ab"),
    ("exe‮gpj.txt", "exegpj.txt"),
    ("a​b⁠c﻿", "abc"),
    ("a\x00b\x7fc\x9bd", "abcd"),
    ("line1\nline2\tx", "line1 line2 x"),
])
def test_textsafe_clean(raw, expected):
    assert textsafe.clean(raw) == expected


def test_textsafe_caps_length_and_keeps_formula_guard_edge():
    assert len(textsafe.clean("x" * 1000)) == 256
    assert textsafe.clean(" =1+1") == " =1+1"


def test_tags_are_sanitized_and_secret_scrubbed_in_ledger():
    c = conn_mem()
    key = "sk-" + "a1b2c3d4e5f6g7"  # built at runtime: not a real key
    put(c, "k", "2026-10-02T00:00:00Z", team="\x1b[31mred\x1b[0m", task=f"deploy {key}")
    row = c.execute("SELECT team, task FROM usage_events").fetchone()
    assert row["team"] == "red"
    assert "sk-" not in row["task"] and "[REDACTED]" in row["task"]


def test_report_output_has_no_escape_bytes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner()
    r.invoke(main, ["init"])
    (tmp_path / "e.jsonl").write_text(json.dumps({
        "id": "1", "ts": "2026-10-02T00:00:00Z", "provider": "anthropic",
        "model": "\x1b]0;x\x07m-big", "input_tokens": 5, "task": "\x1b[2J‮bad"}) + "\n")
    r.invoke(main, ["ingest", "e.jsonl"])
    out = r.invoke(main, ["report", "--by", "task"]).output
    assert "\x1b" not in out and "‮" not in out and "bad" in out
    out = r.invoke(main, ["report", "--by", "sku"]).output
    assert "\x1b" not in out and "\x07" not in out


# ------------------------------------------------------------- append-only guards

def test_events_cannot_be_deleted_or_edited_but_unpriced_cost_can_be_filled():
    c = conn_mem()
    put(c, "k1", "2026-10-02T00:00:00Z")
    put(c, "k2", "2026-10-02T00:00:00Z", cost=None)
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("DELETE FROM usage_events")
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("UPDATE usage_events SET cost_micro_usd = 1 WHERE idem_key = 'k1'")
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("UPDATE usage_events SET input_tokens = 1 WHERE idem_key = 'k2'")
    c.execute("UPDATE usage_events SET cost_micro_usd = 5 WHERE idem_key = 'k2'")
    c.commit()
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("UPDATE usage_events SET cost_micro_usd = 6 WHERE idem_key = 'k2'")


# ------------------------------------------------------------------ Gemini

def test_gemini_usage_metadata_splits_cache_and_adds_thoughts():
    ev = ProviderAdapter().parse_record({
        "responseId": "r1", "modelVersion": "gemini-2.5-pro",
        "createTime": "2026-10-19T09:12:00Z",
        "usageMetadata": {"promptTokenCount": 1000, "cachedContentTokenCount": 300,
                          "candidatesTokenCount": 200, "thoughtsTokenCount": 800,
                          "toolUsePromptTokenCount": 50}})
    assert ev["provider"] == "google" and ev["idem_key"] == "google:r1"
    assert (ev["input_tokens"], ev["cache_read_tokens"]) == (750, 300)
    assert ev["output_tokens"] == 1000  # candidates + thoughts
    assert ev["metadata"]["reasoning_tokens"] == 800


def test_gemini_cached_larger_than_prompt_is_clamped():
    ev = ProviderAdapter().parse_record({
        "responseId": "r", "modelVersion": "gemini-2.5-flash", "ts": 1790000000,
        "usageMetadata": {"promptTokenCount": 10, "cachedContentTokenCount": 99}})
    assert ev["input_tokens"] == 0 and ev["cache_read_tokens"] == 10


def test_gemini_without_timestamp_is_reported_not_guessed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner()
    r.invoke(main, ["init"])
    (tmp_path / "g.jsonl").write_text(json.dumps({
        "responseId": "r", "modelVersion": "gemini-2.5-pro",
        "usageMetadata": {"promptTokenCount": 5}}) + "\n")
    res = r.invoke(main, ["ingest", "--adapter", "provider", "g.jsonl"])
    assert "ingested 0 events" in res.output and "unparseable timestamp" in res.output


def test_openai_reasoning_tokens_recorded_but_not_double_billed():
    ev = ProviderAdapter().parse_record({
        "id": "chatcmpl-1", "model": "gpt-5", "created": 1790000000,
        "usage": {"prompt_tokens": 100, "completion_tokens": 900,
                  "completion_tokens_details": {"reasoning_tokens": 600}}})
    assert ev["output_tokens"] == 900 and ev["metadata"]["reasoning_tokens"] == 600


# ------------------------------------------------------- service tier modifiers

def test_modifier_scales_cost_and_unknown_tier_does_not():
    ev = {"input_tokens": 1_000_000, "output_tokens": 0}
    base = pricing.cost_micro_usd("anthropic:m-big", BOOK, ev)
    assert base == 10_000_000
    assert pricing.cost_micro_usd("anthropic:m-big", BOOK, {**ev, "service_tier": "batch"}) \
        == 5_000_000
    assert pricing.cost_micro_usd("anthropic:m-big", BOOK, {**ev, "service_tier": "priority"}) \
        == base


def test_modifiers_validated_and_loaded(tmp_path):
    p = tmp_path / "p.yaml"
    p.write_text("skus: {}\nmodifiers: {Batch: 0.5}\n")
    assert pricing.load_pricing(p).modifiers == {"batch": 0.5}
    for bad in ("{batch: -1}", "{batch: abc}", "{batch: 99}", "[1]"):
        p.write_text(f"skus: {{}}\nmodifiers: {bad}\n")
        with pytest.raises(ValueError):
            pricing.load_pricing(p)


def test_doctor_warns_for_tier_without_modifier(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner()
    r.invoke(main, ["init"])
    (tmp_path / "e.jsonl").write_text(json.dumps({
        "id": "msg_1", "model": "claude-sonnet-5", "created": 1790000000,
        "usage": {"input_tokens": 10, "output_tokens": 5, "service_tier": "batch"}}) + "\n")
    r.invoke(main, ["ingest", "--adapter", "provider", "e.jsonl"])
    assert "no `modifiers:` entry" in r.invoke(main, ["doctor"]).output
    cfg = tmp_path / ".agentfin" / "pricing.yaml"
    cfg.write_text(cfg.read_text() + "\nmodifiers:\n  batch: 0.5\n")
    assert "no `modifiers:` entry" not in r.invoke(main, ["doctor"]).output


# ---------------------------------------------------------------------- OTel

def _otlp(spans, service="svc"):
    return {"resourceSpans": [{
        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]},
        "scopeSpans": [{"spans": spans}]}]}


def _span(sid, parent=None, op="chat", **attrs):
    a = {"gen_ai.operation.name": op, "gen_ai.provider.name": "anthropic",
         "gen_ai.response.model": "m-big", **attrs}
    out = []
    for k, v in a.items():
        out.append({"key": k, "value": {"intValue": str(v)} if isinstance(v, int)
                    else {"stringValue": v}})
    s = {"traceId": "t" * 32, "spanId": sid, "name": op,
         "startTimeUnixNano": "1790000000000000000", "attributes": out}
    if parent:
        s["parentSpanId"] = parent
    return s


def test_otel_splits_cache_out_of_input_total():
    ev = OtelAdapter().parse_records(_otlp([_span(
        "s1", **{"gen_ai.usage.input_tokens": 1000,
                 "gen_ai.usage.cache_read.input_tokens": 600,
                 "gen_ai.usage.cache_creation.input_tokens": 100,
                 "gen_ai.usage.output_tokens": 50,
                 "gen_ai.usage.reasoning.output_tokens": 20})]))[0]
    assert (ev["input_tokens"], ev["cache_read_tokens"], ev["cache_write_tokens"]) \
        == (300, 600, 100)
    assert ev["output_tokens"] == 50 and ev["metadata"]["reasoning_tokens"] == 20
    assert ev["ts"] == 1790000000 and ev["agent"] == "svc"


def test_otel_exclusive_semantics_assumed_when_total_smaller_than_cache():
    ev = OtelAdapter().parse_records(_otlp([_span(
        "s1", **{"gen_ai.usage.input_tokens": 10,
                 "gen_ai.usage.cache_read.input_tokens": 600,
                 "gen_ai.usage.output_tokens": 5})]))[0]
    assert ev["input_tokens"] == 10 and ev["cache_read_tokens"] == 600
    assert ev["metadata"]["otel_input_semantics"] == "exclusive_assumed"


def test_otel_parent_and_agent_spans_do_not_double_count(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    usage = {"gen_ai.usage.input_tokens": 100, "gen_ai.usage.output_tokens": 10}
    spans = [
        _span("agent", op="invoke_agent", **{"gen_ai.usage.input_tokens": 300,
                                              "gen_ai.usage.output_tokens": 30}),
        _span("parent", parent="agent", **{"gen_ai.usage.input_tokens": 200,
                                            "gen_ai.usage.output_tokens": 20}),
        _span("c1", parent="parent", **usage),
        _span("c2", parent="parent", **usage),
        {"traceId": "t" * 32, "spanId": "tool", "name": "tool", "attributes": []},
    ]
    (tmp_path / "o.jsonl").write_text(json.dumps(_otlp(spans)) + "\n")
    r = CliRunner()
    r.invoke(main, ["init"])
    res = r.invoke(main, ["ingest", "--adapter", "otel", "o.jsonl"])
    assert "ingested 2 events" in res.output, res.output
    tot = json.loads(r.invoke(main, ["report", "--by", "agent", "--json"]).output)
    assert sum(x["input_tokens"] for x in tot) == 200


def test_otel_response_id_key_matches_provider_adapter():
    sp = _span("s", **{"gen_ai.usage.input_tokens": 10, "gen_ai.usage.output_tokens": 1,
                       "gen_ai.response.id": "msg_abc"})
    ev = OtelAdapter().parse_records(_otlp([sp]))[0]
    assert ev["idem_key"] == "anthropic:msg_abc"
    c = conn_mem()
    ctx = IngestContext(c, BOOK, {"defaults": {}, "rules": []})
    ctx.emit(ev)
    ctx.emit(ProviderAdapter().parse_record({
        "id": "msg_abc", "model": "m-big", "created": 1790000000,
        "usage": {"input_tokens": 10, "output_tokens": 1}}))
    assert (ctx.inserted, ctx.duplicates) == (1, 1)


def test_otel_console_exporter_shape_and_garbage():
    ad = OtelAdapter()
    ev = ad.parse_records({
        "name": "chat", "context": {"trace_id": "0xaa", "span_id": "0xbb"},
        "start_time": "2026-10-02T10:00:00.000000Z",
        "attributes": {"gen_ai.system": "openai", "gen_ai.request.model": "gpt-x",
                       "gen_ai.usage.input_tokens": 7, "gen_ai.usage.output_tokens": 3}})[0]
    assert ev["provider"] == "openai" and ev["idem_key"] == "otel:0xaa:0xbb"
    for junk in ({}, {"resourceSpans": "x"}, {"resourceSpans": [1, {"scopeSpans": [None]}]},
                 {"attributes": {"gen_ai.usage.input_tokens": "NaN"}}):
        assert ad.parse_records(junk) == []


# ------------------------------------------------------------------- money

def test_apportion_always_sums_exactly():
    rng = random.Random(7)
    for _ in range(300):
        total = rng.randint(-10**9, 10**9)
        w = {f"k{i}": rng.randint(0, 10**6) for i in range(rng.randint(1, 9))}
        if not any(w.values()):
            continue
        parts = money.apportion(total, w)
        assert sum(parts.values()) == total
        assert all(parts[k] == 0 for k, v in w.items() if v == 0)


def test_apportion_rejects_all_zero_weights():
    with pytest.raises(ValueError):
        money.apportion(10, {"a": 0})


def test_convert_parts_sum_equals_converted_total():
    rng = random.Random(3)
    for _ in range(200):
        parts = {f"t{i}": rng.randint(1, 5_000_000) for i in range(rng.randint(1, 8))}
        rate = Decimal(str(round(rng.uniform(0.5, 2.0), 4)))
        conv = money.convert_parts(parts, rate)
        expect = (Decimal(sum(parts.values())) * rate / 1_000_000 * 100).quantize(
            Decimal(1), rounding="ROUND_HALF_EVEN") / 100
        assert sum(conv.values()) == expect


# ------------------------------------------------------------------- close

NOW = datetime(2026, 12, 5, tzinfo=timezone.utc)


def seeded():
    c = conn_mem()
    put(c, "a", "2026-10-02T10:00:00Z", team="alpha", inp=1_000_000, out=0)
    put(c, "b", "2026-10-15T10:00:00Z", team="beta", inp=500_000, out=100_000)
    put(c, "c", "2026-10-20T10:00:00Z", team="unassigned", inp=100_000, out=0)
    put(c, "n", "2026-11-03T10:00:00Z", team="alpha", inp=1)
    return c


def test_month_window_respects_time_zone_and_dst():
    s, e, last = close.month_window("2026-10", "America/Chicago")
    assert (s, e, last) == ("2026-10-01T05:00:00Z", "2026-11-01T05:00:00Z", "2026-10-31")
    assert close.month_window("2026-12", "UTC")[:2] == ("2026-12-01T00:00:00Z",
                                                          "2027-01-01T00:00:00Z")
    for bad in ("2026-13", "oct", "2026-10-01"):
        with pytest.raises(close.CloseError):
            close.month_window(bad)
    with pytest.raises(close.CloseError):
        close.month_window("2026-10", "Mars/Olympus")


def test_close_uses_local_month_boundaries():
    c = conn_mem()
    put(c, "edge", "2026-11-01T04:30:00Z")  # Oct 31 23:30 in Chicago, Nov 1 in UTC
    rec = close.run_close(c, "2026-10", "America/Chicago", now=NOW)
    assert rec["events"] == 1
    with pytest.raises(close.CloseError):
        close.run_close(c, "2026-10", "UTC", now=NOW)  # no events in UTC October


def test_close_refuses_open_month_unpriced_and_repeat():
    c = seeded()
    with pytest.raises(close.CloseError, match="not ended"):
        close.run_close(c, "2026-11", now=NOW.replace(month=11, day=10))
    put(c, "u", "2026-10-21T00:00:00Z", cost=None)
    with pytest.raises(close.CloseError, match="UNPRICED"):
        close.run_close(c, "2026-10", now=NOW)
    rec = close.run_close(c, "2026-10", allow_unpriced=True, now=NOW)
    assert rec["unpriced_events"] == 1 and rec["version"] == 1
    with pytest.raises(close.CloseError, match="already closed"):
        close.run_close(c, "2026-10", allow_unpriced=True, now=NOW)
    assert close.run_close(c, "2026-10", allow_unpriced=True, supersede=True,
                           now=NOW)["version"] == 2


def test_verify_detects_late_events_and_altered_rows():
    c = seeded()
    close.run_close(c, "2026-10", now=NOW)
    assert close.verify(c, "2026-10")["ok"]
    put(c, "late", "2026-10-30T00:00:00Z", inp=2_000_000)
    v = close.verify(c, "2026-10")
    assert not v["ok"] and v["late_events"] == 1 and v["late_cost_micro_usd"] == 20_000_000
    assert not v["altered"]
    # tampering (needs the guard triggers dropped by someone with file access)
    c.execute("DROP TRIGGER usage_events_only_fill_cost")
    c.execute("UPDATE usage_events SET cost_micro_usd = 1 WHERE idem_key = 'a'")
    assert close.verify(c, "2026-10")["altered"]
    # other months are unaffected by the close
    with pytest.raises(close.CloseError, match="not been closed"):
        close.verify(c, "2026-09")


def test_closes_table_is_append_only():
    c = seeded()
    close.run_close(c, "2026-10", now=NOW)
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("UPDATE closes SET cost_micro_usd = 0")
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("DELETE FROM closes")


def test_journal_balances_in_usd_and_cad_and_uses_gl_map():
    c = seeded()
    close.run_close(c, "2026-10", fx={"CAD": "1.3712"}, now=NOW)
    cfg = {"gl_accounts": {"alpha": "6101 AI - Alpha"}, "gl_accrual": "2100 Accrued"}
    for cur, rate in (("USD", Decimal(1)), ("CAD", Decimal("1.3712"))):
        rows = close.journal_rows(c, "2026-10", cfg, cur)
        debits = sum(Decimal(r["Debits"]) for r in rows if r["Debits"])
        credits = sum(Decimal(r["Credits"]) for r in rows if r["Credits"])
        assert debits == credits
        total_micro = c.execute(
            "SELECT SUM(cost_micro_usd) s FROM usage_events WHERE ts_utc < '2026-11-01'"
        ).fetchone()["s"]
        assert credits == (Decimal(total_micro) * rate / 10**6).quantize(Decimal("0.01"))
    assert {r["AccountName"] for r in rows} >= {"6101 AI - Alpha", "2100 Accrued"}
    assert rows[0]["JournalDate"] == "2026-10-31"
    with pytest.raises(close.CloseError, match="FX rate"):
        close.journal_rows(c, "2026-10", cfg, "EUR")


def test_journal_refuses_after_drift_and_neutralizes_formulas():
    c = seeded()
    put(c, "evil", "2026-10-05T00:00:00Z", team="=cmd|' /C calc'!A0")
    close.run_close(c, "2026-10", now=NOW)
    buf = io.StringIO()
    close.write_journal(close.journal_rows(c, "2026-10", {}), buf)
    assert "'=cmd" in buf.getvalue()
    put(c, "late", "2026-10-30T00:00:00Z")
    with pytest.raises(close.CloseError, match="no longer matches"):
        close.journal_rows(c, "2026-10", {})


def test_parse_fx_rejects_nonsense():
    assert close.parse_fx(["cad=1.37"]) == {"CAD": "1.37"}
    for bad in ("CAD", "CAD=0", "CAD=-1", "CAD=abc", "CAD=nan", "CAD=inf", "=1", "DOLLARS=1"):
        with pytest.raises(close.CloseError):
            close.parse_fx([bad])


def test_close_cli_flow_and_exit_codes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner()
    r.invoke(main, ["init"])
    rows = [{"id": f"m{i}", "ts": f"2026-09-{i + 1:02d}T10:00:00Z", "provider": "anthropic",
             "model": "claude-sonnet-5", "input_tokens": 1_000_000, "team": "ops"}
            for i in range(3)]
    (tmp_path / "e.jsonl").write_text("\n".join(json.dumps(x) for x in rows) + "\n")
    r.invoke(main, ["ingest", "e.jsonl"])
    assert r.invoke(main, ["close", "verify", "2026-09"]).exit_code == 1  # not closed
    res = r.invoke(main, ["close", "run", "2026-09", "--fx", "CAD=1.40"])
    assert res.exit_code == 0 and "closed 2026-09 v1" in res.output
    assert r.invoke(main, ["close", "verify", "2026-09"]).exit_code == 0
    out = r.invoke(main, ["close", "journal", "2026-09", "--currency", "CAD"]).output
    lines = list(csv.DictReader(io.StringIO(out)))
    assert lines[0]["Debits"] == "8.40"  # 3 * $2.00 * 1.40
    (tmp_path / "late.jsonl").write_text(json.dumps({
        "id": "late", "ts": "2026-09-29T10:00:00Z", "provider": "anthropic",
        "model": "claude-sonnet-5", "input_tokens": 1_000_000, "team": "ops"}) + "\n")
    r.invoke(main, ["ingest", "late.jsonl"])
    res = r.invoke(main, ["close", "verify", "2026-09"])
    assert res.exit_code == 3 and "1 late event" in res.output
    assert r.invoke(main, ["close", "journal", "2026-09"]).exit_code == 1
    assert r.invoke(main, ["close", "run", "2026-09", "--supersede"]).exit_code == 0
    assert r.invoke(main, ["close", "verify", "2026-09"]).exit_code == 0


# --------------------------------------------------------------- showback

def test_allocate_spreads_unassigned_proportionally_and_exactly():
    c = seeded()
    res = allocate.allocate(c)
    assert res["pool_micro_usd"] == 1_000_000  # 100k tokens * $10/M
    by = {r["team"]: r for r in res["rows"]}
    direct_a, direct_b = by["alpha"]["direct_micro_usd"], by["beta"]["direct_micro_usd"]
    assert direct_a == 10_000_000 + 10
    assert by["alpha"]["allocated_micro_usd"] + by["beta"]["allocated_micro_usd"] == 1_000_000
    assert abs(by["alpha"]["allocated_micro_usd"] / 1_000_000
               - direct_a / (direct_a + direct_b)) < 1e-5
    ledger_total = c.execute("SELECT SUM(cost_micro_usd) s FROM usage_events").fetchone()["s"]
    assert res["total_micro_usd"] == ledger_total


def test_allocate_errors_when_nobody_to_charge_and_rejects_bad_dimension():
    c = conn_mem()
    put(c, "u", "2026-10-02T00:00:00Z", team="unassigned")
    with pytest.raises(ValueError, match="nothing to allocate"):
        allocate.allocate(c)
    with pytest.raises(ValueError):
        allocate.allocate(c, by="metadata_json; DROP TABLE x")


# ----------------------------------------------------------------- what-if

def test_cache_savings_matches_hand_calculation():
    c = conn_mem()
    put(c, "a", "2026-10-02T00:00:00Z", inp=0, out=0, cr=1_000_000, cw=0)
    put(c, "b", "2026-10-02T00:00:00Z", inp=0, out=0, cr=0, cw=1_000_000)
    res = whatif.cache_savings(c, BOOK, by="sku")["rows"][0]
    # a: read at $1/M instead of $10/M saves $9; b: write at $12.5/M vs $10/M costs $2.5 more
    assert res["actual_micro_usd"] == 1_000_000 + 12_500_000
    assert res["without_cache_micro_usd"] == 20_000_000
    assert res["saved_micro_usd"] == 6_500_000


def test_model_swap_prices_same_tokens_on_other_sku_and_skips_unpriced():
    c = conn_mem()
    put(c, "a", "2026-10-02T00:00:00Z", inp=1_000_000, out=0)
    put(c, "b", "2026-10-02T00:00:00Z", inp=1_000_000, out=0, cost=None)
    res = whatif.model_swap(c, BOOK, {"anthropic:m-big": "anthropic:m-small"}, by="sku")
    row = res["rows"][0]
    assert (row["actual_micro_usd"], row["swapped_micro_usd"], row["delta_micro_usd"]) \
        == (10_000_000, 2_000_000, -8_000_000)
    assert res["skipped_events"] == 1


# ------------------------------------------------------------------ git link

def _git(repo, *args, env=None):
    e = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
         "GIT_CONFIG_GLOBAL": os.devnull}
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=e)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "a.txt").write_text("one\n")
    _git(r, "add", "."), _git(r, "commit", "-qm", "init")
    for name, ln in (("feat-merged", "m"), ("feat-open", "o")):
        _git(r, "checkout", "-q", "-b", name)
        (r / f"{name}.txt").write_text(ln * 3 + "\n")
        _git(r, "add", "."), _git(r, "commit", "-qm", name)
        _git(r, "checkout", "-q", "main")
    _git(r, "merge", "-q", "--no-ff", "-m", "merge", "feat-merged")
    return r


def _branch_events():
    c = conn_mem()
    put(c, "1", "2026-10-02T00:00:00Z", inp=1_000_000, out=0, git_branch="feat-merged")
    put(c, "2", "2026-10-02T00:00:00Z", inp=500_000, out=0, git_branch="feat-open")
    put(c, "3", "2026-10-02T00:00:00Z", inp=200_000, out=0, git_branch="main")
    put(c, "4", "2026-10-02T00:00:00Z", inp=100_000, out=0)
    put(c, "5", "2026-10-02T00:00:00Z", inp=1_000, out=0, git_branch="--upload-pack=touch /tmp/x")
    put(c, "6", "2026-10-02T00:00:00Z", inp=1_000, out=0, git_branch="gone")
    return c


def test_git_outcomes_split_merged_unmerged_and_unlinked(repo):
    res = gitlink.analyze(_branch_events(), repo)
    by = {r["branch"]: r for r in res["rows"]}
    assert by["feat-merged"]["status"] == "merged" and by["feat-open"]["status"] == "unmerged"
    assert by["main"]["status"] == "base" and by["<no branch>"]["status"] == "none"
    assert by["feat-open"]["commits"] == 1 and by["feat-open"]["lines_changed"] == 1
    assert res["merged_cost_micro_usd"] == 10_000_000
    assert res["cost_per_merged_branch_micro_usd"] == 10_000_000
    assert res["unmerged_branches"] == 3 and res["base_cost_micro_usd"] == 2_000_000
    assert by["gone"]["commits"] is None  # branch deleted locally: no git facts, no crash


def test_git_hostile_branch_name_never_reaches_git(repo, monkeypatch):
    calls = []
    real = gitlink._git
    monkeypatch.setattr(gitlink, "_git", lambda r, *a: (calls.append(a), real(r, *a))[1])
    gitlink.analyze(_branch_events(), repo)
    assert not any("--upload-pack" in " ".join(a) for a in calls)
    assert not gitlink.safe_ref("-x") and not gitlink.safe_ref("a..b")
    assert not gitlink.safe_ref("a b") and gitlink.safe_ref("feat/x-1_2.3")


def test_git_merged_list_covers_squash_merges_and_bad_base_errors(repo):
    res = gitlink.analyze(_branch_events(), repo, merged_extra={"feat-open"})
    assert res["merged_branches"] == 2
    with pytest.raises(gitlink.GitError):
        gitlink.analyze(_branch_events(), repo, base="nope")
    with pytest.raises(gitlink.GitError):
        gitlink.analyze(_branch_events(), repo, base="--evil")


def test_git_cli(repo, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner()
    r.invoke(main, ["init"])
    rows = [{"id": "g1", "ts": "2026-10-02T00:00:00Z", "provider": "anthropic",
             "model": "claude-sonnet-5", "input_tokens": 1_000_000,
             "metadata": {"git_branch": "feat-merged"}}]
    (tmp_path / "e.jsonl").write_text(json.dumps(rows[0]) + "\n")
    r.invoke(main, ["ingest", "e.jsonl"])
    out = r.invoke(main, ["outcomes", "git", "--repo", str(repo)])
    assert out.exit_code == 0 and "per merged branch" in out.output, out.output
    assert r.invoke(main, ["outcomes", "git", "--repo", str(tmp_path)]).exit_code == 1


# ---------------------------------------------------------------------- FOCUS

MANDATORY_FOCUS_14 = {
    "BilledCost", "BillingAccountId", "BillingAccountName", "BillingCurrency",
    "BillingPeriodEnd", "BillingPeriodStart", "ChargeCategory", "ChargeClass",
    "ChargeDescription", "ChargePeriodEnd", "ChargePeriodStart", "ContractedCost",
    "EffectiveCost", "HostProviderName", "InvoiceIssuerName", "ListCost", "PricingQuantity",
    "PricingUnit", "ServiceCategory", "ServiceName", "ServiceProviderName",
}


def test_focus_export_has_every_v14_mandatory_cost_and_usage_column(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner()
    r.invoke(main, ["init"])
    cfg = tmp_path / ".agentfin" / "config.yaml"
    cfg.write_text(yaml.safe_dump({"billing_account_id": "acct-1",
                                   "billing_account_name": "=Evil"}))
    (tmp_path / "e.jsonl").write_text(json.dumps({
        "id": "1", "ts": "2026-10-02T00:00:00Z", "provider": "anthropic",
        "model": "claude-sonnet-5", "input_tokens": 1000}) + "\n")
    r.invoke(main, ["ingest", "e.jsonl"])
    rows = list(csv.DictReader(io.StringIO(r.invoke(main, ["export"]).output)))
    assert MANDATORY_FOCUS_14 <= set(rows[0])
    assert "ProviderName" not in rows[0]  # deprecated name replaced by ServiceProviderName
    assert rows[0]["BillingAccountId"] == "acct-1"
    assert rows[0]["BillingAccountName"] == "'=Evil"
    assert rows[0]["ContractedCost"] == rows[0]["ListCost"] == rows[0]["BilledCost"]
    assert rows[0]["PricingQuantity"] == "1000" and rows[0]["ChargeFrequency"] == "Usage-Based"


def test_hostile_close_inputs():
    for bad in ("CAD=1e400", "CAD=0.0000001", "CAD=1000001", "CAD=1e-400"):
        with pytest.raises(close.CloseError):
            close.parse_fx([bad])
    for tz in ("../../etc/passwd", "", "UTC\x00", "A" * 500):
        with pytest.raises(close.CloseError):
            close.month_window("2026-10", tz)
    for month in ("0000-01", "9999-12", "2026-00", "-2026-10", "2026-1"):
        try:
            close.month_window(month)
        except close.CloseError:
            pass
    c = seeded()
    close.run_close(c, "2026-10", now=NOW)
    with pytest.raises(close.CloseError, match="gl_accounts"):
        close.journal_rows(c, "2026-10", {"gl_accounts": ["x"]})


def test_failed_close_leaves_no_open_transaction():
    c = seeded()
    with pytest.raises(close.CloseError):
        close.run_close(c, "2026-11", now=NOW.replace(month=11, day=10))
    assert not c.in_transaction
    put(c, "after", "2026-10-03T00:00:00Z")  # ledger still writable
