"""Round 5: author line, upgrade path, property / differential / golden tests, new commands."""

from __future__ import annotations

import csv
import io
import json
import os
import random
import re
import shutil
import sqlite3
import time
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

import pytest
from click.testing import CliRunner

import agentfin
from agentfin import budgets, close, ledger, money, pricing, reconcile
from agentfin.cli import main

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "examples" / "claude-code-demo"
FIX = Path(__file__).resolve().parent / "fixtures"
GOLD = Path(__file__).resolve().parent / "golden"
AUTHOR = "Calas Automations Inc."


@pytest.fixture
def demo_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner()
    assert r.invoke(main, ["init"]).exit_code == 0
    assert r.invoke(main, ["ingest", "--adapter", "claude-code", str(DEMO)]).exit_code == 0
    return tmp_path


# --- the author line is pinned everywhere it ships -------------------------------------

def test_author_is_pinned():
    pp = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'authors = [{{ name = "{AUTHOR}" }}]' in pp
    assert f'maintainers = [{{ name = "{AUTHOR}" }}]' in pp
    assert agentfin.__author__ == AUTHOR
    assert f"Copyright (c) 2026 {AUTHOR}" in (ROOT / "LICENSE").read_text(encoding="utf-8")
    out = CliRunner().invoke(main, ["--version"]).output
    assert out.strip() == f"agentfin, version {agentfin.__version__} ({AUTHOR})"


def test_dashboard_footer_has_real_version_and_author(demo_dir):
    r = CliRunner().invoke(main, ["dashboard", "--out", "d.html"])
    assert r.exit_code == 0, r.output
    html = Path("d.html").read_text(encoding="utf-8")
    assert f"agentfin {agentfin.__version__} · {AUTHOR}" in html
    assert "agentfin v1" not in html


def test_no_personal_name_left_in_tracked_text():
    bad = re.compile(r"\bDan\b|Dan Calas")
    skip = {".git", ".venv", "agentfin.egg-info", "__pycache__", "build", "dist",
            ".pytest_cache", ".ruff_cache"}
    hits = []
    for p in ROOT.rglob("*"):
        if any(part in skip for part in p.relative_to(ROOT).parts) or not p.is_file():
            continue
        if p.suffix in {".py", ".md", ".toml", ".yaml", ".yml", ".in", ".txt", ".jsonl", ""}:
            if p.name == "test_round5.py":
                continue
            if bad.search(p.read_text(encoding="utf-8", errors="replace")):
                hits.append(str(p.relative_to(ROOT)))
    assert hits == []


# --- upgrade path: files written by the published 0.2.0 still work ---------------------

@pytest.fixture
def old_install(tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    shutil.copy(FIX / "budgets_v0_2_0.yaml", cfg / "budgets.yaml")
    shutil.copy(FIX / "pricing_v0_2_0.yaml", cfg / "pricing.yaml")
    shutil.copy(FIX / "rules.yaml", cfg / "rules.yaml")
    shutil.copy(FIX / "config.yaml", cfg / "config.yaml")
    shutil.copy(FIX / "ledger_v0_2_0.sqlite3", tmp_path / "old.db")
    return ["--db", str(tmp_path / "old.db"), "--config-dir", str(cfg)], tmp_path


def test_old_ledger_reports_identically(old_install):
    args, _ = old_install
    r = CliRunner().invoke(main, [*args, "report", "--by", "team", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output) == json.loads(
        (GOLD / "report_team_v0_2_0.json").read_text(encoding="utf-8"))


def test_old_ledger_reingest_is_all_duplicates_and_keys_are_stable(old_install):
    args, _ = old_install
    r = CliRunner().invoke(main, [*args, "ingest", str(FIX / "ledger_v0_2_0_source.jsonl")])
    assert r.exit_code == 0, r.output
    assert "ingested 0 events (12 duplicates skipped" in r.output


def test_old_ledger_close_outcomes_budgets_and_doctor_work(old_install):
    args, _ = old_install
    run = CliRunner().invoke
    assert run(main, [*args, "close", "run", "2026-09", "--allow-unpriced"]).exit_code == 0
    assert run(main, [*args, "close", "verify", "2026-09"]).exit_code == 0
    roi = run(main, [*args, "outcomes", "roi"])
    assert roi.exit_code == 0 and "1027.79x" in roi.output
    assert run(main, [*args, "budgets", "check", "--json"]).exit_code == 0
    d = run(main, [*args, "doctor", "--json"])
    data = json.loads(d.output)
    assert data["unpriced_events"] == 2
    # the 0.2.0 price book predates newer models: tell the user, never edit their file
    assert data["example_skus_missing_from_pricing_file"]


def test_old_schema_has_no_surprise_columns():
    """The 0.2.0 ledger and a fresh one hold the same usage_events columns."""
    old = sqlite3.connect(FIX / "ledger_v0_2_0.sqlite3")
    new = ledger.connect(":memory:")
    cols = lambda c: [r[1] for r in c.execute("PRAGMA table_info(usage_events)")]  # noqa: E731
    assert cols(old) == cols(new)


# --- differential: money maths against an independent Decimal reference ----------------

RATE_POOL = [0.1, 0.3, 0.25, 0.125, 0.625, 1.25, 2.5, 3.0, 0.07, 0.15, 0.35, 1.1, 0.01,
             0.05, 6.25, 18.75, 0.30]


def _ref_cost(rates, ev, mult=None):
    pairs = (("input_tokens", "input"), ("cache_read_tokens", "cache_read"),
             ("cache_write_tokens", "cache_write"), ("output_tokens", "output"))
    tot = sum(Decimal(ev[t]) * Decimal(str(rates[k])) for t, k in pairs)
    if mult is not None:
        tot *= Decimal(str(mult))
    return int(tot.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def test_cost_matches_decimal_reference_on_exact_ties():
    rng = random.Random(5)
    for _ in range(20000):
        rates = {k: rng.choice(RATE_POOL) for k in ("input", "cache_read", "cache_write",
                                                      "output")}
        ev = {t: rng.randint(0, 60) for t in ("input_tokens", "cache_read_tokens",
                                              "cache_write_tokens", "output_tokens")}
        assert pricing.cost_micro_usd("x:y", {"x:y": rates}, ev) == _ref_cost(rates, ev), (
            rates, ev)


def test_known_float_tie_regression():
    # 15 tokens x $0.10/M = 1.5 micro-USD: a tie, rounds half-even to 2. 36/14/28/22
    # tokens at these rates is 24 exactly; the old float maths said 23.
    r = {"x:y": {"input": 0.3, "cache_read": 0.15, "cache_write": 0.3, "output": 0.1}}
    ev = {"input_tokens": 36, "cache_read_tokens": 14, "cache_write_tokens": 28,
          "output_tokens": 22}
    assert pricing.cost_micro_usd("x:y", r, ev) == 24


def test_service_tier_modifier_uses_exact_maths():
    book = pricing.PriceBook({"x:y": {"input": 3.0, "output": 15.0}}, modifiers={"batch": 0.5})
    ev = {"input_tokens": 3, "output_tokens": 0, "service_tier": "batch"}
    assert pricing.cost_micro_usd("x:y", book, ev) == 4  # 4.5 -> half-even 4
    assert _ref_cost({"input": 3.0, "cache_read": 0, "cache_write": 0, "output": 15.0},
                     {"input_tokens": 3, "cache_read_tokens": 0, "cache_write_tokens": 0,
                      "output_tokens": 0}, 0.5) == 4


# --- property tests (seeded generators; hypothesis is not required) --------------------

def test_apportion_always_sums_exactly():
    rng = random.Random(11)
    for _ in range(3000):
        n = rng.randint(1, 8)
        weights = {f"k{i}": rng.choice([0, 1, 2, 3, 7, 1000, 10**9]) for i in range(n)}
        if not any(weights.values()):
            continue
        total = rng.randint(-10**7, 10**7)
        parts = money.apportion(total, weights)
        assert sum(parts.values()) == total
        assert all(parts[k] == 0 for k, w in weights.items() if w == 0)


def test_convert_parts_sum_to_converted_total():
    rng = random.Random(12)
    for _ in range(2000):
        parts = {f"t{i}": rng.randint(0, 10**8) for i in range(rng.randint(1, 6))}
        rate = Decimal(str(round(rng.uniform(0.5, 150), 4)))
        out = money.convert_parts(parts, rate)
        want = (Decimal(sum(parts.values())) * rate / 1_000_000 * 100).quantize(
            Decimal(1), rounding=ROUND_HALF_EVEN) / 100
        assert sum(out.values()) == want


def test_convert_parts_keeps_each_teams_own_amount_even_when_negative():
    rng = random.Random(13)
    for _ in range(3000):
        parts = {f"t{i}": rng.randint(-5 * 10**6, 10**8) for i in range(rng.randint(1, 6))}
        rate = Decimal(str(round(rng.uniform(0.5, 150), 4)))
        out = money.convert_parts(parts, rate)
        for k, micro in parts.items():
            own = Decimal(micro) * rate / 1_000_000
            assert abs(out[k] - own) <= Decimal("0.01"), (k, out[k], own)
        want = (Decimal(sum(parts.values())) * rate / 1_000_000 * 100).quantize(
            Decimal(1), rounding=ROUND_HALF_EVEN) / 100
        assert sum(out.values()) == want


def _events(rng, n):
    out = []
    for i in range(n):
        out.append({
            "ts": f"2026-10-{rng.randint(1, 28):02d}T{rng.randint(0, 23):02d}:00:00Z",
            "provider": "anthropic", "model": rng.choice(
                ["claude-sonnet-4-5", "claude-opus-4-5", "never-priced"]),
            "input_tokens": rng.randint(0, 50000), "output_tokens": rng.randint(0, 5000),
            "cache_read_tokens": rng.randint(0, 90000),
            "team": rng.choice(["a", "b", "c"]), "agent": rng.choice(["x", "y"]),
            "task": f"t{rng.randint(0, 4)}", "id": f"ev{i}",
        })
    return out


def _ingest_lines(tmp_path, name, events, cli_args=()):
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    r = CliRunner().invoke(main, [*cli_args, "ingest", str(p)])
    assert r.exit_code == 0, r.output
    return r


def _fingerprint(db):
    c = sqlite3.connect(db)
    return c.execute(
        "SELECT sku, team, agent, task, input_tokens, output_tokens, cache_read_tokens, "
        "cost_micro_usd FROM usage_events ORDER BY idem_key").fetchall()


def test_ingest_is_order_independent_and_idempotent(tmp_path, monkeypatch):
    rng = random.Random(21)
    evs = _events(rng, 150)
    prints = []
    for i, order in enumerate((evs, evs[::-1], rng.sample(evs, len(evs)))):
        d = tmp_path / f"w{i}"
        d.mkdir()
        monkeypatch.chdir(d)
        CliRunner().invoke(main, ["init"])
        _ingest_lines(d, "u.jsonl", order)
        again = _ingest_lines(d, "u2.jsonl", order)  # idempotent re-run
        assert "ingested 0 events" in again.output
        prints.append(_fingerprint(d / ".agentfin" / "agentfin.db"))
    assert prints[0] == prints[1] == prints[2]


def test_report_dimensions_always_sum_to_the_total(tmp_path, monkeypatch):
    rng = random.Random(22)
    monkeypatch.chdir(tmp_path)
    CliRunner().invoke(main, ["init"])
    _ingest_lines(tmp_path, "u.jsonl", _events(rng, 200))
    totals = {}
    for by in ("team", "agent", "task", "sku", "provider", "day", "week", "month"):
        r = CliRunner().invoke(main, ["report", "--by", by, "--json"])
        assert r.exit_code == 0, r.output
        rows = json.loads(r.output)
        totals[by] = (sum(x["cost_micro_usd"] or 0 for x in rows),
                      sum(x["events"] for x in rows))
    assert len(set(totals.values())) == 1, totals


def test_journal_balances_even_when_a_team_nets_negative(tmp_path, monkeypatch):
    """Corrections can drive a team's month below zero: that must be a credit,
    never a negative debit, and the journal must still balance to the cent."""
    monkeypatch.chdir(tmp_path)
    CliRunner().invoke(main, ["init"])
    conn = ledger.connect(tmp_path / ".agentfin" / "agentfin.db")
    base = {"provider": "anthropic", "model_raw": "m", "ts": "2026-10-05T10:00:00Z",
            "agent": "a", "task": "t"}
    rows = [
        ("k1", "alpha", 5_000_000, False), ("k2", "beta", 1_234_567, False),
        ("k3", "beta", -2_000_000, True), ("k4", "gamma", 333_333, False),
        ("k5", "delta", 0, False),
    ]
    for key, team, micro, corr in rows:
        ledger.insert_event(conn, {**base, "idem_key": key, "team": team,
                                   "cost_micro_usd": micro, "is_correction": corr})
    conn.commit()
    close.run_close(conn, "2026-10", "UTC", {"CAD": "1.37"}, None, False, "", False)
    for cur in ("USD", "CAD"):
        jr = close.journal_rows(conn, "2026-10", {}, cur)
        buf = io.StringIO()
        close.write_journal(jr, buf)
        parsed = list(csv.DictReader(io.StringIO(buf.getvalue())))
        debits = sum(Decimal(r["Debits"] or 0) for r in parsed)
        credits = sum(Decimal(r["Credits"] or 0) for r in parsed)
        assert debits == credits
        assert all("-" not in (r["Debits"] + r["Credits"]) for r in parsed)
        assert not any(r["Name"] == "delta" for r in parsed)  # zero lines are dropped
        beta = next(r for r in parsed if r["Name"] == "beta")
        assert beta["Debits"] == "" and beta["Credits"] != ""
    conn.close()


# --- money cells in invoices: exact, and ambiguity fails closed ------------------------

@pytest.mark.parametrize("raw,delim,want", [
    ("1.50", ",", "1.50"), ("$1,234.50", ",", "1234.50"), ("(12.50)", ",", "-12.50"),
    ("-3", ",", "-3"), ("-$3", ",", "-3"), ("", ",", "0"), ("1 234,50", ";", "1234.50"),
    ("1.234,50", ";", "1234.50"), ("12,5", ";", "12.5"), ("1.5", ";", "1.5"),
])
def test_parse_amount_accepts(raw, delim, want):
    assert reconcile.parse_amount(raw, delim) == Decimal(want)


@pytest.mark.parametrize("raw,delim", [
    ("1,5", ","), ("1,50", ","), ("1,500", ";"), ("1.2.3", ";"), ("abc", ","),
    ("NaN", ","), ("inf", ","), ("1,2,3", ";"),
])
def test_parse_amount_refuses_ambiguity(raw, delim):
    with pytest.raises(ValueError):
        reconcile.parse_amount(raw, delim)


def _recon(tmp_path, monkeypatch, body, encoding="utf-8"):
    monkeypatch.chdir(tmp_path)
    CliRunner().invoke(main, ["init"])
    ev = tmp_path / "e.jsonl"
    ev.write_text(json.dumps({
        "ts": "2026-10-05T10:00:00Z", "provider": "anthropic", "model": "claude-sonnet-4-5",
        "input_tokens": 1_000_000, "output_tokens": 0, "id": "z1", "team": "a"}) + "\n")
    CliRunner().invoke(main, ["ingest", str(ev)])
    inv = tmp_path / "inv.csv"
    inv.write_bytes(body.encode(encoding))
    return CliRunner().invoke(main, ["reconcile", str(inv), "--json"])


def test_reconcile_semicolon_decimal_comma(tmp_path, monkeypatch):
    r = _recon(tmp_path, monkeypatch, "sku;amount\nanthropic:claude-sonnet-4-5;3,00\n")
    assert r.exit_code == 0, r.output
    line = json.loads(r.output)["lines"][0]
    assert line["invoiced_usd"] == 3.0 and line["match"] is True


def test_reconcile_utf16_powershell_csv(tmp_path, monkeypatch):
    r = _recon(tmp_path, monkeypatch, "sku,amount\r\nanthropic:claude-sonnet-4-5,3.00\r\n",
               "utf-16")
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["all_match"] is True


def test_reconcile_decimal_comma_in_comma_file_is_refused_not_x100(tmp_path, monkeypatch):
    r = _recon(tmp_path, monkeypatch, 'sku,amount\nanthropic:claude-sonnet-4-5,"3,00"\n')
    assert r.exit_code == 1 and "ambiguous" in r.output
    assert "Traceback" not in r.output


# --- budgets -----------------------------------------------------------------------------

def test_small_budget_is_not_shown_as_zero(demo_dir):
    r = CliRunner()
    r.invoke(main, ["budgets", "set", "team:small", "0.2"])
    out = r.invoke(main, ["budgets", "check"]).output
    assert "$0.20 budget" in out and "$0 budget" not in out
    r.invoke(main, ["budgets", "set", "team:big", "500"])
    assert "$500 budget" in r.invoke(main, ["budgets", "check"]).output


def test_budgets_save_is_atomic(tmp_path, monkeypatch):
    p = tmp_path / "b.yaml"
    budgets.save_budgets(p, [{"scope": "team:a", "monthly_usd": 10, "alert_at": 0.8}])
    before = p.read_text(encoding="utf-8")

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        budgets.save_budgets(p, [{"scope": "team:b", "monthly_usd": 99, "alert_at": 0.5}])
    assert p.read_text(encoding="utf-8") == before  # old gate untouched
    assert [x.name for x in tmp_path.iterdir()] == ["b.yaml"]  # no temp file left behind


# --- status / --json / template ----------------------------------------------------------

def test_status_variants(demo_dir):
    r = CliRunner()
    j = json.loads(r.invoke(main, ["status", "--json"]).output)
    assert j["events"] == 24 and j["unpriced_events"] == 0
    assert j["total_cost_micro_usd"] > 0 and j["latest_closed_month"] is None
    one = r.invoke(main, ["status", "--oneline"]).output
    assert one.count("\n") == 1 and one.startswith("agentfin $")
    full = r.invoke(main, ["status"]).output
    assert "events:        24" in full and "last closed:   none" in full


def test_status_refuses_missing_ledger(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(main, ["status"])
    assert r.exit_code == 1 and "ledger not found" in r.output
    assert not (tmp_path / ".agentfin").exists()


def test_doctor_json_matches_text(demo_dir):
    r = CliRunner()
    j = json.loads(r.invoke(main, ["doctor", "--json"]).output)
    text = r.invoke(main, ["doctor"]).output
    assert f"events: {j['events']:,}" in text
    assert j["unpriced_events"] == 0 and j["possible_duplicate_groups"] == 0


def test_anomalies_json_and_gate(demo_dir):
    r = CliRunner().invoke(main, ["anomalies", "--json"])
    assert r.exit_code == 0 and isinstance(json.loads(r.output), list)


def test_close_verify_json_ok_and_drift(demo_dir):
    r = CliRunner()
    months = [x["dim"] for x in json.loads(
        r.invoke(main, ["report", "--by", "month", "--json"]).output)]
    month = months[0]
    assert r.invoke(main, ["close", "run", month]).exit_code == 0
    ok = r.invoke(main, ["close", "verify", month, "--json"])
    assert ok.exit_code == 0 and json.loads(ok.output)["ok"] is True
    extra = Path("late.jsonl")
    extra.write_text(json.dumps({
        "ts": f"{month}-15T12:00:00Z", "provider": "anthropic", "model": "claude-sonnet-4-5",
        "input_tokens": 10, "output_tokens": 10, "id": "late-1"}) + "\n")
    r.invoke(main, ["ingest", str(extra)])
    bad = r.invoke(main, ["close", "verify", month, "--json"])
    assert bad.exit_code == 3 and json.loads(bad.output)["ok"] is False


def test_pricing_template_prints_example_book():
    r = CliRunner().invoke(main, ["pricing-template"])
    assert r.exit_code == 0 and r.output == pricing.DEFAULT_PRICING


# --- ingest --modified-within ------------------------------------------------------------

def test_modified_within_reads_only_recent_files_in_folders(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    CliRunner().invoke(main, ["init"])
    folder = tmp_path / "logs"
    folder.mkdir()
    old, new = folder / "old.jsonl", folder / "new.jsonl"
    for f, ident in ((old, "o"), (new, "n")):
        f.write_text(json.dumps({
            "ts": "2026-10-05T10:00:00Z", "provider": "anthropic",
            "model": "claude-sonnet-4-5", "input_tokens": 5, "output_tokens": 5,
            "id": ident}) + "\n")
    past = time.time() - 10 * 86400
    os.utime(old, (past, past))
    r = CliRunner().invoke(main, ["ingest", "--modified-within", "2d", str(folder)])
    assert r.exit_code == 0 and "ingested 1 events" in r.output
    named = CliRunner().invoke(main, ["ingest", "--modified-within", "2d", str(old)])
    assert "ingested 1 events" in named.output  # a file you name is always read


@pytest.mark.parametrize("bad", ["", "0d", "5", "d", "-1h", "1w", "99999999d"])
def test_modified_within_rejects_bad_values(tmp_path, monkeypatch, bad):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(main, ["ingest", "--modified-within", bad, "x"])
    assert r.exit_code == 2


# --- golden files: every machine-readable output format ------------------------------------

def _golden(name, text):
    path = GOLD / name
    if os.environ.get("AGENTFIN_UPDATE_GOLDEN") == "1":
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    assert text == path.read_text(encoding="utf-8"), f"{name} changed; review, then " \
        "re-run with AGENTFIN_UPDATE_GOLDEN=1 if intended"


def test_golden_report_json(demo_dir):
    for by in ("task", "team"):
        out = CliRunner().invoke(main, ["report", "--by", by, "--json"]).output
        _golden(f"report_{by}.json", out)


def test_golden_report_text(demo_dir):
    _golden("report_task.txt", CliRunner().invoke(main, ["report", "--by", "task"]).output)


def test_golden_focus_csv(demo_dir):
    r = CliRunner().invoke(main, ["export", "--format", "focus", "--out", "-"])
    assert r.exit_code == 0, r.output
    _golden("export_focus.csv", r.output)


def test_golden_raw_csv_and_jsonl(demo_dir):
    for fmt in ("csv", "jsonl"):
        r = CliRunner().invoke(main, ["export", "--format", fmt, "--out", "-"])
        assert r.exit_code == 0, r.output
        _golden(f"export_raw.{fmt}", r.output)


def test_golden_journal_csv(demo_dir):
    r = CliRunner()
    month = json.loads(r.invoke(main, ["report", "--by", "month", "--json"]).output)[0]["dim"]
    assert r.invoke(main, ["close", "run", month, "--fx", "CAD=1.37"]).exit_code == 0
    out = r.invoke(main, ["close", "journal", month, "--currency", "CAD"])
    assert out.exit_code == 0, out.output
    _golden("journal_cad.csv", out.output)


def test_golden_dashboard_html(demo_dir):
    CliRunner().invoke(main, ["dashboard", "--out", "d.html"])
    html = Path("d.html").read_text(encoding="utf-8")
    html = re.sub(r"Generated [^·]+·", "Generated <T> ·", html)
    html = html.replace(agentfin.__version__, "<V>")
    _golden("dashboard.html", html)


def test_golden_allocate_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    CliRunner().invoke(main, ["init"])
    rng = random.Random(3)
    evs = _events(rng, 60)
    for e in evs[::4]:
        del e["team"]  # these land in the shared "unassigned" pool
    _ingest_lines(tmp_path, "u.jsonl", evs)
    out = CliRunner().invoke(main, ["allocate", "--json"])
    assert out.exit_code == 0, out.output
    _golden("allocate.json", out.output)
    data = json.loads(out.output)
    assert data["total_micro_usd"] == sum(r["loaded_micro_usd"] for r in data["rows"])


def test_empty_month_journal_has_no_lines(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    CliRunner().invoke(main, ["init"])
    _ingest_lines(tmp_path, "u.jsonl", [{
        "ts": "2026-10-05T10:00:00Z", "provider": "anthropic", "model": "claude-sonnet-4-5",
        "input_tokens": 0, "output_tokens": 1, "id": "z", "team": "a"}])
    r = CliRunner()
    assert r.invoke(main, ["close", "run", "2026-10"]).exit_code == 0
    out = r.invoke(main, ["close", "journal", "2026-10"])
    assert out.exit_code == 0
    assert out.output.strip().splitlines() == [
        "JournalNo,JournalDate,AccountName,Debits,Credits,Description,Name,Currency"]


def test_budgets_set_permission_problem_is_one_plain_line(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    CliRunner().invoke(main, ["init"])

    def deny(*a, **k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(os, "replace", deny)
    r = CliRunner().invoke(main, ["budgets", "set", "team:z", "5"])
    assert r.exit_code == 1 and "permission denied" in r.output.lower()
    assert "Traceback" not in r.output
    leftovers = [p.name for p in (tmp_path / ".agentfin").iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_pricing_clamps_negative_tokens_but_not_corrections():
    book = {"x:y": {"input": 1.0, "output": 1.0}}
    assert pricing.cost_micro_usd("x:y", book, {"input_tokens": -100, "output_tokens": 50}) == 50
    corr = {"input_tokens": -100, "output_tokens": 50, "is_correction": True}
    assert pricing.cost_micro_usd("x:y", book, corr) == -50


def test_global_config_dir_and_db_are_honoured_by_init(tmp_path, monkeypatch):
    """`agentfin --config-dir X init` used to scaffold ./.agentfin and ignore X."""
    monkeypatch.chdir(tmp_path)
    r = CliRunner()
    assert r.invoke(main, ["--config-dir", "cfgA", "init"]).exit_code == 0
    assert (tmp_path / "cfgA" / "pricing.yaml").is_file()
    assert (tmp_path / "cfgA" / "agentfin.db").is_file()
    assert not (tmp_path / ".agentfin").exists()
    assert r.invoke(main, ["--db", "x/my.db", "--config-dir", "cfgB", "init"]).exit_code == 0
    assert (tmp_path / "x" / "my.db").is_file() and not (tmp_path / "cfgB" / "agentfin.db").exists()
    assert r.invoke(main, ["init", "--config-dir", "cfgC"]).exit_code == 0  # old spelling
    assert (tmp_path / "cfgC" / "agentfin.db").is_file()


def test_ingest_warns_when_there_is_no_price_book(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "u.jsonl"
    f.write_text(json.dumps({"ts": "2026-10-05T10:00:00Z", "provider": "anthropic",
                             "model": "claude-sonnet-4-5", "input_tokens": 5,
                             "output_tokens": 5, "id": "q"}) + "\n")
    r = CliRunner().invoke(main, ["--db", "l.db", "--config-dir", "nocfg", "ingest", str(f)])
    assert r.exit_code == 0 and "1 unpriced" in r.output
    assert "no price book" in r.output  # stderr is merged into output by CliRunner


def test_reconcile_tolerance_boundary_is_inclusive(tmp_path, monkeypatch):
    r = _recon(tmp_path, monkeypatch, "sku,amount\nanthropic:claude-sonnet-4-5,3.01\n")
    line = json.loads(r.output)["lines"][0]
    assert line["variance_usd"] == -0.01 and line["match"] is True  # exactly at the 0.01 default
    r2 = CliRunner().invoke(main, ["reconcile", "inv.csv", "--json", "--tolerance", "0.005"])
    assert json.loads(r2.output)["lines"][0]["match"] is False


def test_status_gives_up_quickly_when_the_ledger_is_locked(demo_dir):
    lock = sqlite3.connect(demo_dir / ".agentfin" / "agentfin.db")
    lock.execute("BEGIN EXCLUSIVE")
    try:
        t0 = time.time()
        r = CliRunner().invoke(main, ["status", "--oneline"])
        took = time.time() - t0
    finally:
        lock.rollback()
        lock.close()
    assert r.exit_code == 1 and "busy" in r.output.lower()
    assert took < 15  # not the 30 s ingest wait


def test_status_works_on_a_0_2_0_ledger(old_install):
    args, _ = old_install
    r = CliRunner().invoke(main, [*args, "status", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["events"] == 12


@pytest.mark.parametrize("extra", [[], ["--json"]])
def test_budget_gate_trips_on_unpriced_spend_in_both_output_modes(tmp_path, monkeypatch, extra):
    monkeypatch.chdir(tmp_path)
    r = CliRunner()
    r.invoke(main, ["init"])
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _ingest_lines(tmp_path, "u.jsonl", [{
        "ts": now, "provider": "anthropic", "model": "a-model-nobody-priced",
        "input_tokens": 10, "output_tokens": 10, "id": "u1", "team": "x"}])
    r.invoke(main, ["budgets", "set", "team:x", "100000"])
    out = r.invoke(main, ["budgets", "check", "--fail-on-alert", *extra])
    assert out.exit_code == 3, out.output
