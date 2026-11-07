"""Round 3 (expert-2 continued): hostile numbers and dates, bounded input, a stricter
invoice reconcile, time-bucket reports, Windows-safe file encodings."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner

from agentfin import ledger, reports
from agentfin.cli import main

BASE = {"ts": "2026-10-02T10:00:00Z", "provider": "anthropic",
        "model": "claude-sonnet-4-5", "input_tokens": 10, "output_tokens": 5}


def _init(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    assert runner.invoke(main, ["init"]).exit_code == 0
    return runner


def _write(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _ingest(runner, path):
    return runner.invoke(main, ["ingest", "--adapter", "jsonl", str(path)])


@pytest.mark.parametrize("patch,needle", [
    ({"input_tokens": -5}, "input_tokens is negative"),
    ({"input_tokens": 2 ** 70}, "implausibly large"),
    ({"input_tokens": 1e30}, "implausibly large"),
    ({"input_tokens": "NaN"}, "input_tokens"),
    ({"input_tokens": 12.5}, "whole number"),
    ({"input_tokens": True}, "must be a number"),
    ({"ts": "9999-12-31T23:59:59Z"}, "outside the accepted range"),
    ({"ts": "0001-01-01T00:00:00Z"}, "outside the accepted range"),
    ({"ts": "2014-12-31T23:59:59Z"}, "outside the accepted range"),
])
def test_hostile_numbers_and_dates_rejected_with_a_plain_message(
        tmp_path, monkeypatch, patch, needle):
    runner = _init(tmp_path, monkeypatch)
    _write(tmp_path / "h.jsonl", [{**BASE, **patch}, {**BASE, "request_id": "ok"}])
    r = _ingest(runner, tmp_path / "h.jsonl")
    assert r.exit_code == 0 and "ingested 1 events" in r.output, r.output
    assert needle in r.output
    assert "Error:" not in r.output.split("malformed")[0]
    assert "OverflowError" not in r.output and "ValueError" not in r.output


def test_whole_float_counts_and_near_future_are_accepted(tmp_path, monkeypatch):
    runner = _init(tmp_path, monkeypatch)
    soon = (datetime.now(timezone.utc) + timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write(tmp_path / "ok.jsonl", [{**BASE, "input_tokens": 100.0, "ts": soon}])
    r = _ingest(runner, tmp_path / "ok.jsonl")
    assert "ingested 1 events" in r.output, r.output
    far = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write(tmp_path / "far.jsonl", [{**BASE, "ts": far}])
    assert "ingested 0 events" in _ingest(runner, tmp_path / "far.jsonl").output


def test_strict_mode_writes_nothing_on_a_hostile_line(tmp_path, monkeypatch):
    runner = _init(tmp_path, monkeypatch)
    _write(tmp_path / "s.jsonl", [BASE, {**BASE, "input_tokens": -1, "request_id": "b"}])
    r = runner.invoke(main, ["ingest", "--adapter", "jsonl", "--strict",
                             str(tmp_path / "s.jsonl")])
    assert r.exit_code != 0 and "nothing written" in r.output
    r = runner.invoke(main, ["report", "--by", "team", "--json"])
    assert json.loads(r.output) == []


def test_oversize_line_is_skipped_not_loaded(tmp_path, monkeypatch):
    import agentfin.cli as cli
    monkeypatch.setattr(cli, "MAX_LINE_CHARS", 1000)
    runner = _init(tmp_path, monkeypatch)
    p = tmp_path / "big.jsonl"
    p.write_text(json.dumps({**BASE, "pad": "a" * 5000}) + "\n"
                 + json.dumps({**BASE, "request_id": "after"}) + "\n", encoding="utf-8")
    r = _ingest(runner, p)
    assert "ingested 1 events" in r.output and "line is longer than" in r.output


def test_oversize_final_line_without_newline(tmp_path, monkeypatch):
    import agentfin.cli as cli
    monkeypatch.setattr(cli, "MAX_LINE_CHARS", 1000)
    runner = _init(tmp_path, monkeypatch)
    p = tmp_path / "big.jsonl"
    p.write_text("x" * 5000, encoding="utf-8")
    r = _ingest(runner, p)
    assert "ingested 0 events" in r.output and "line is longer than" in r.output


@pytest.mark.skipif(sys.platform == "win32", reason="no /dev/zero")
def test_device_files_are_refused(tmp_path, monkeypatch):
    runner = _init(tmp_path, monkeypatch)
    r = runner.invoke(main, ["ingest", "--adapter", "jsonl", "/dev/zero"])
    assert r.exit_code == 1 and "not a regular file" in r.output


def test_deeply_nested_json_is_a_plain_error(tmp_path, monkeypatch):
    runner = _init(tmp_path, monkeypatch)
    p = tmp_path / "deep.jsonl"
    p.write_text("[" * 200000 + "\n", encoding="utf-8")
    r = _ingest(runner, p)
    assert r.exit_code == 0 and "RecursionError" not in r.output
    assert "nested too deeply" in r.output


def test_correction_events_may_be_negative(tmp_path):
    conn = ledger.connect(tmp_path / "t.db")
    assert ledger.insert_event(conn, {
        "idem_key": "c1", "ts": "2026-10-19T00:00:00Z", "provider": "openai",
        "model_raw": "m", "input_tokens": -5, "is_correction": True,
        "cost_micro_usd": -3})
    conn.close()


# -- budgets / outcomes numbers --------------------------------------------


@pytest.mark.parametrize("args", [
    ["budgets", "set", "team:x", "nan"],
    ["budgets", "set", "team:x", "inf"],
    ["budgets", "set", "team:x", "1e999"],
    ["budgets", "set", "team:x", "5", "--alert-at", "nan"],
    ["outcomes", "tag", "t", "--kind", "revenue", "--value", "inf"],
    ["outcomes", "tag", "t", "--kind", "hours_saved", "--value", "nan"],
])
def test_non_finite_numbers_are_usage_errors(tmp_path, monkeypatch, args):
    runner = _init(tmp_path, monkeypatch)
    r = runner.invoke(main, args)
    assert r.exit_code == 2 and "Traceback" not in r.output, r.output


# -- reconcile -------------------------------------------------------------


def _ledger_with(runner, tmp_path, rows):
    _write(tmp_path / "u.jsonl", rows)
    assert "ingested" in _ingest(runner, tmp_path / "u.jsonl").output


def test_reconcile_wrong_header_is_an_error_not_zero(tmp_path, monkeypatch):
    runner = _init(tmp_path, monkeypatch)
    _ledger_with(runner, tmp_path, [BASE])
    (tmp_path / "inv.csv").write_text("name,total\nanthropic:claude-sonnet-4-5,5\n")
    r = runner.invoke(main, ["reconcile", "inv.csv"])
    assert r.exit_code == 1 and "need a SKU column" in r.output


def test_reconcile_accepts_aliases_bom_dollar_signs_and_commas(tmp_path, monkeypatch):
    runner = _init(tmp_path, monkeypatch)
    _ledger_with(runner, tmp_path, [{**BASE, "input_tokens": 1_000_000,
                                     "output_tokens": 0}])
    sku = "anthropic:claude-sonnet-4-5"
    (tmp_path / "inv.csv").write_bytes(
        f'﻿Model,Amount\n{sku},"$3.00"\n'.encode())
    r = runner.invoke(main, ["reconcile", "inv.csv", "--from", "2026-10-01",
                             "--to", "2026-10-31"])
    assert r.exit_code == 0 and r.output.count("ok ") == 1, r.output


def test_reconcile_period_filter_and_exit_code(tmp_path, monkeypatch):
    runner = _init(tmp_path, monkeypatch)
    _ledger_with(runner, tmp_path, [
        {**BASE, "input_tokens": 1_000_000, "output_tokens": 0},
        {**BASE, "ts": "2026-09-02T10:00:00Z", "input_tokens": 1_000_000,
         "output_tokens": 0, "request_id": "sep"}])
    sku = "anthropic:claude-sonnet-4-5"
    (tmp_path / "inv.csv").write_text(f"sku,cost_usd\n{sku},3.00\n", encoding="utf-8")
    whole = runner.invoke(main, ["reconcile", "inv.csv", "--fail-on-variance"])
    assert whole.exit_code == 3 and "VARIANCE" in whole.output
    assert "whole ledger" in whole.output
    month = runner.invoke(main, ["reconcile", "inv.csv", "--from", "2026-10-01",
                                 "--to", "2026-10-31", "--fail-on-variance"])
    assert month.exit_code == 0, month.output


def test_reconcile_unpriced_sku_never_matches_and_labels_one_sided(tmp_path, monkeypatch):
    runner = _init(tmp_path, monkeypatch)
    _ledger_with(runner, tmp_path, [{**BASE, "model": "mystery-1"}])
    (tmp_path / "inv.csv").write_text("sku,cost_usd\nanthropic:other,2\n")
    r = runner.invoke(main, ["reconcile", "inv.csv", "--json"])
    data = json.loads(r.output)
    notes = {line["sku"]: line for line in data["lines"]}
    assert notes["anthropic:mystery-1"]["match"] is False
    assert notes["anthropic:mystery-1"]["unpriced_events"] == 1
    assert notes["anthropic:mystery-1"]["note"] == "ledger only"
    assert notes["anthropic:other"]["note"] == "invoice only"


@pytest.mark.parametrize("body", ["sku,cost_usd\nx,abc\n", "sku,cost_usd\nx,NaN\n",
                                  "sku,cost_usd\nx,Infinity\n"])
def test_reconcile_bad_numbers_name_the_row(tmp_path, monkeypatch, body):
    runner = _init(tmp_path, monkeypatch)
    (tmp_path / "inv.csv").write_text(body)
    r = runner.invoke(main, ["reconcile", "inv.csv"])
    assert r.exit_code == 1 and "row 2" in r.output and "Traceback" not in r.output


def test_reconcile_binary_file_is_a_clean_error(tmp_path, monkeypatch):
    runner = _init(tmp_path, monkeypatch)
    (tmp_path / "inv.csv").write_bytes(b"\xff\xfe\x00junk\x00")
    r = runner.invoke(main, ["reconcile", "inv.csv"])
    assert r.exit_code == 1 and "Traceback" not in r.output


def test_reconcile_sums_cents_without_float_drift(tmp_path, monkeypatch):
    runner = _init(tmp_path, monkeypatch)
    (tmp_path / "inv.csv").write_text(
        "sku,cost_usd\n" + "x,0.1\n" * 10, encoding="utf-8")
    r = runner.invoke(main, ["reconcile", "inv.csv", "--json"])
    assert json.loads(r.output)["lines"][0]["invoiced_usd"] == 1.0


# -- report buckets --------------------------------------------------------


def test_report_by_day_uses_the_requested_time_zone(tmp_path, monkeypatch):
    runner = _init(tmp_path, monkeypatch)
    _ledger_with(runner, tmp_path, [
        {**BASE, "ts": "2026-10-02T03:30:00Z", "request_id": "a"},   # Oct 1 evening Chicago
        {**BASE, "ts": "2026-10-02T15:00:00Z", "request_id": "b"},
        {**BASE, "ts": "2026-10-03T00:15:00Z", "request_id": "c"},   # Oct 2 evening Chicago
    ])
    utc = json.loads(runner.invoke(main, ["report", "--by", "day", "--json"]).output)
    assert [(r["dim"], r["events"]) for r in utc] == [("2026-10-02", 2), ("2026-10-03", 1)]
    chi = json.loads(runner.invoke(
        main, ["report", "--by", "day", "--tz", "America/Chicago", "--json"]).output)
    assert [(r["dim"], r["events"]) for r in chi] == [("2026-10-01", 1), ("2026-10-02", 2)]
    kol = json.loads(runner.invoke(
        main, ["report", "--by", "day", "--tz", "Asia/Kolkata", "--json"]).output)
    # 03:30Z is 09:00 IST on the 2nd; 15:00Z -> 20:30 IST; 00:15Z -> 05:45 IST on the 3rd
    assert [(r["dim"], r["events"]) for r in kol] == [("2026-10-02", 2), ("2026-10-03", 1)]


def test_report_by_week_month_and_totals_agree(tmp_path, monkeypatch):
    runner = _init(tmp_path, monkeypatch)
    _ledger_with(runner, tmp_path, [
        {**BASE, "ts": "2026-10-04T10:00:00Z", "request_id": "sun"},   # Sunday
        {**BASE, "ts": "2026-10-05T10:00:00Z", "request_id": "mon"},   # Monday
        {**BASE, "ts": "2026-11-01T10:00:00Z", "request_id": "nov"},
    ])
    week = json.loads(runner.invoke(main, ["report", "--by", "week", "--json"]).output)
    assert [r["dim"] for r in week] == ["2026-09-28", "2026-10-05", "2026-10-26"]
    month = json.loads(runner.invoke(main, ["report", "--by", "month", "--json"]).output)
    assert [r["dim"] for r in month] == ["2026-10", "2026-11"]
    team = json.loads(runner.invoke(main, ["report", "--by", "team", "--json"]).output)
    assert sum(r["cost_micro_usd"] for r in month) == team[0]["cost_micro_usd"]


def test_report_bad_tz_is_a_usage_error(tmp_path, monkeypatch):
    runner = _init(tmp_path, monkeypatch)
    r = runner.invoke(main, ["report", "--by", "day", "--tz", "Mars/Base"])
    assert r.exit_code == 2 and "--tz" in r.output


def test_report_by_session(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    conn = ledger.connect(tmp_path / ".agentfin" / "agentfin.db")
    for i, sid in enumerate(["s1", "s1", "s2", None]):
        ledger.insert_event(conn, {
            "idem_key": f"k{i}", "ts": "2026-10-02T10:00:00Z", "provider": "p",
            "model_raw": "m", "input_tokens": 1, "cost_micro_usd": 10 * (i + 1),
            "metadata": {"session_id": sid} if sid else {}})
    conn.commit()
    rows = {r["dim"]: r for r in reports.summary_bucketed(conn, "session")}
    assert rows["s1"]["cost_micro_usd"] == 30 and rows["s2"]["cost_micro_usd"] == 30
    assert rows["(no session id)"]["events"] == 1


def test_python_dash_m_agentfin_works():
    r = subprocess.run([sys.executable, "-m", "agentfin", "--version"],
                       capture_output=True, text=True)
    assert r.returncode == 0 and "agentfin" in r.stdout


def test_export_with_a_legacy_bad_date_is_a_clean_error(tmp_path, monkeypatch):
    runner = _init(tmp_path, monkeypatch)
    conn = ledger.connect(tmp_path / ".agentfin" / "agentfin.db")
    conn.execute(
        """INSERT INTO usage_events (idem_key, ts_utc, provider, model_raw, sku, ingested_at)
           VALUES ('old', '1-01-01T00:00:00Z', 'p', 'm', 'p:m', 'x')""")
    conn.commit()
    conn.close()
    r = runner.invoke(main, ["export", "--format", "focus"])
    assert r.exit_code == 1 and "invalid date" in r.output and "Traceback" not in r.output


def test_text_files_are_written_as_utf8(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(main, ["init"])
    r = runner.invoke(main, ["budgets", "set", "team:café", "5"])
    assert r.exit_code == 0
    assert "café" in (tmp_path / ".agentfin" / "budgets.yaml").read_bytes().decode("utf-8")


def test_dry_run_counts_but_writes_nothing(tmp_path, monkeypatch):
    runner = _init(tmp_path, monkeypatch)
    _write(tmp_path / "d.jsonl", [BASE, {**BASE, "request_id": "2"}])
    r = runner.invoke(main, ["ingest", "--adapter", "jsonl", "--dry-run",
                             str(tmp_path / "d.jsonl")])
    assert "dry run, nothing written: would ingest 2 events" in r.output, r.output
    assert json.loads(runner.invoke(main, ["report", "--json"]).output) == []
    real = _ingest(runner, tmp_path / "d.jsonl")
    assert "ingested 2 events" in real.output and "dry run" not in real.output


def test_doctor_warns_about_old_price_book_and_odd_dates(tmp_path, monkeypatch):
    import os
    import time
    runner = _init(tmp_path, monkeypatch)
    old = time.time() - 90 * 86400
    os.utime(tmp_path / ".agentfin" / "pricing.yaml", (old, old))
    conn = ledger.connect(tmp_path / ".agentfin" / "agentfin.db")
    conn.execute(
        """INSERT INTO usage_events (idem_key, ts_utc, provider, model_raw, sku, ingested_at)
           VALUES ('old', '1-01-01T00:00:00Z', 'p', 'm', 'p:m', 'x')""")
    conn.commit()
    conn.close()
    out = runner.invoke(main, ["doctor"]).output
    assert "pricing.yaml was last modified 90 days ago" in out
    assert "1 events are dated before 2015 or in the future" in out


def test_doctor_quiet_for_fresh_price_book(tmp_path, monkeypatch):
    runner = _init(tmp_path, monkeypatch)
    assert "last modified" not in runner.invoke(main, ["doctor"]).output


@pytest.mark.parametrize("cost", [-1, 1e12, 1700000000, "abc", True, float("inf")])
def test_gateway_cost_must_be_sane(tmp_path, monkeypatch, cost):
    runner = _init(tmp_path, monkeypatch)
    p = tmp_path / "g.jsonl"
    p.write_text(json.dumps({"ts": "2026-10-02T10:00:00Z", "route": "anthropic/m",
                             "tokens_in": 1, "cost_usd": cost, "run_id": "r1"})
                 .replace("Infinity", "1e999") + "\n", encoding="utf-8")
    r = runner.invoke(main, ["ingest", "--adapter", "gateway", str(p)])
    assert "ingested 0 events" in r.output and "cost_usd" in r.output, r.output


def test_gateway_cost_kept_verbatim_when_sane(tmp_path, monkeypatch):
    runner = _init(tmp_path, monkeypatch)
    p = tmp_path / "g.jsonl"
    p.write_text(json.dumps({"ts": "2026-10-02T10:00:00Z", "route": "anthropic/m",
                             "tokens_in": 1, "cost_usd": 0.5, "run_id": "r1"}) + "\n",
                 encoding="utf-8")
    runner.invoke(main, ["ingest", "--adapter", "gateway", str(p)])
    rows = json.loads(runner.invoke(main, ["report", "--json"]).output)
    assert rows[0]["cost_micro_usd"] == 500_000


def test_insert_event_rejects_negative_cost_unless_correction(tmp_path):
    conn = ledger.connect(tmp_path / "t.db")
    base = {"ts": "2026-10-19T00:00:00Z", "provider": "p", "model_raw": "m"}
    with pytest.raises(ValueError, match="cost is out of range"):
        ledger.insert_event(conn, {**base, "idem_key": "a", "cost_micro_usd": -5})
    assert ledger.insert_event(conn, {**base, "idem_key": "b", "cost_micro_usd": -5,
                                      "is_correction": True})
    conn.close()


def test_bucket_report_dates_are_whole_days_in_the_requested_zone(tmp_path, monkeypatch):
    runner = _init(tmp_path, monkeypatch)
    _ledger_with(runner, tmp_path, [
        {**BASE, "ts": "2026-10-02T03:30:00Z", "request_id": "a"},  # Oct 1, 22:30 Chicago
        {**BASE, "ts": "2026-10-02T06:00:00Z", "request_id": "b"},  # Oct 2, 01:00 Chicago
        {**BASE, "ts": "2026-10-03T04:30:00Z", "request_id": "c"},  # Oct 2, 23:30 Chicago
        {**BASE, "ts": "2026-10-03T05:30:00Z", "request_id": "d"},  # Oct 3, 00:30 Chicago
    ])
    rows = json.loads(runner.invoke(main, [
        "report", "--by", "day", "--tz", "America/Chicago", "--from", "2026-10-02",
        "--to", "2026-10-02", "--json"]).output)
    assert [(r["dim"], r["events"]) for r in rows] == [("2026-10-02", 2)]


def test_stdout_survives_a_legacy_code_page(tmp_path, monkeypatch):
    """Windows consoles/pipes in cp1252 would crash on the arrows and ticks."""
    monkeypatch.chdir(tmp_path)
    import os
    import pathlib
    root = str(pathlib.Path(__file__).resolve().parents[1])
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONPATH": root}
    r = subprocess.run([sys.executable, "-m", "agentfin", "init"], capture_output=True,
                       env=env, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    r = subprocess.run([sys.executable, "-m", "agentfin", "doctor"], capture_output=True,
                       env=env, cwd=tmp_path)
    assert r.returncode == 0 and b"Traceback" not in r.stderr, r.stderr


def test_bucket_report_with_a_legacy_bad_date_is_a_clean_error(tmp_path, monkeypatch):
    runner = _init(tmp_path, monkeypatch)
    conn = ledger.connect(tmp_path / ".agentfin" / "agentfin.db")
    conn.execute(
        """INSERT INTO usage_events (idem_key, ts_utc, provider, model_raw, sku, ingested_at)
           VALUES ('old', '1-01-01T00:00:00Z', 'p', 'm', 'p:m', 'x')""")
    conn.commit()
    conn.close()
    r = runner.invoke(main, ["report", "--by", "day"])
    assert r.exit_code == 2 and "invalid date" in r.output and "Traceback" not in r.output
