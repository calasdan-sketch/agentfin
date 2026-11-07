"""Regression tests for final-audit fixes (v0.2.0).

- `budgets set` subcommand (blueprint: budgets set/check)
- `outcomes tag|roi` group + `outcome`/`roi` compat aliases
- dimension validation (SQL-interpolation hardening)
- `init` refuses to overwrite without --force
- reconcile: clean error on unparseable cost_usd
- negative token counts clamped (no negative costs in the ledger)
"""

import pytest
import yaml
from click.testing import CliRunner

from agentfin import anomalies, budgets, ledger, reports
from agentfin.cli import main
from agentfin.reconcile import reconcile


def _init(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    r = runner.invoke(main, ["init"])
    assert r.exit_code == 0, r.output
    return runner


# -- budgets set ------------------------------------------------------------


def test_budgets_set_creates_and_updates(tmp_path, monkeypatch):
    runner = _init(tmp_path, monkeypatch)
    r = runner.invoke(main, ["budgets", "set", "team:platform", "500"])
    assert r.exit_code == 0, r.output
    assert "team:platform" in r.output
    blist = budgets.load_budgets(tmp_path / ".agentfin" / "budgets.yaml")
    assert {"scope": "team:platform", "monthly_usd": 500.0, "alert_at": 0.8} in blist

    # Updating the same scope replaces, not duplicates.
    r = runner.invoke(
        main, ["budgets", "set", "team:platform", "1000", "--alert-at", "0.5"]
    )
    assert r.exit_code == 0, r.output
    blist = budgets.load_budgets(tmp_path / ".agentfin" / "budgets.yaml")
    assert len([b for b in blist if b["scope"] == "team:platform"]) == 1
    assert blist[0]["monthly_usd"] == 1000.0
    assert blist[0]["alert_at"] == 0.5

    # The budgets.yaml written by `set` still loads and checks.
    r = runner.invoke(main, ["budgets", "check"])
    assert r.exit_code == 0, r.output
    assert "team:platform" in r.output


def test_budgets_set_rejects_bad_input(tmp_path, monkeypatch):
    runner = _init(tmp_path, monkeypatch)
    for args in (
        ["budgets", "set", "platform", "500"],          # no dimension prefix
        ["budgets", "set", "org:platform", "500"],      # unknown dimension
        ["budgets", "set", "team:", "500"],             # empty value
        ["budgets", "set", "team:platform", "-5"],      # negative cap
        ["budgets", "set", "team:platform", "0"],       # zero cap
        ["budgets", "set", "team:platform", "500", "--alert-at", "2"],
    ):
        r = runner.invoke(main, args)
        assert r.exit_code != 0, args


def test_save_budgets_roundtrip(tmp_path):
    p = tmp_path / "budgets.yaml"
    budgets.save_budgets(p, [{"scope": "agent:worker", "monthly_usd": 42.5}])
    data = yaml.safe_load(p.read_text())
    assert data["budgets"][0]["monthly_usd"] == 42.5
    assert budgets.load_budgets(p)[0]["scope"] == "agent:worker"


# -- outcomes group ---------------------------------------------------------


def test_outcomes_group_and_compat_aliases(tmp_path, monkeypatch):
    runner = _init(tmp_path, monkeypatch)
    r = runner.invoke(
        main, ["outcomes", "tag", "refactor", "--kind", "hours_saved", "--value", "4"]
    )
    assert r.exit_code == 0, r.output
    r = runner.invoke(main, ["outcomes", "roi"])
    assert r.exit_code == 0, r.output
    # bare `outcomes` defaults to the roi report
    r = runner.invoke(main, ["outcomes"])
    assert r.exit_code == 0, r.output
    # pre-group names still work
    r = runner.invoke(main, ["outcome", "refactor2", "--value", "10"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(main, ["roi"])
    assert r.exit_code == 0, r.output
    assert "refactor2" in r.output


# -- dimension validation (SQL-interpolation hardening) ---------------------


def test_detect_rejects_unknown_dimension(tmp_path):
    conn = ledger.connect(tmp_path / "t.db")
    try:
        with pytest.raises(ValueError, match="unknown dimension"):
            anomalies.detect(conn, dimension="1=1; --")
    finally:
        conn.close()


def test_scope_filter_validates_without_value():
    with pytest.raises(ValueError, match="unknown dimension"):
        reports.scope_filter("bogus", None)


def test_cli_rejects_bad_dimension(tmp_path, monkeypatch):
    runner = _init(tmp_path, monkeypatch)
    r = runner.invoke(main, ["anomalies", "--by", "bogus"])
    assert r.exit_code != 0
    r = runner.invoke(main, ["trends", "--by", "bogus"])
    assert r.exit_code != 0
    r = runner.invoke(main, ["report", "--by", "bogus"])
    assert r.exit_code != 0


# -- init --force -----------------------------------------------------------


def test_init_refuses_overwrite_without_force(tmp_path, monkeypatch):
    runner = _init(tmp_path, monkeypatch)
    (tmp_path / ".agentfin" / "pricing.yaml").write_text("custom: true\n")
    r = runner.invoke(main, ["init"])
    assert r.exit_code != 0
    assert "refusing to overwrite" in r.output
    # custom content survived
    assert (tmp_path / ".agentfin" / "pricing.yaml").read_text() == "custom: true\n"
    r = runner.invoke(main, ["init", "--force"])
    assert r.exit_code == 0, r.output
    assert (tmp_path / ".agentfin" / "pricing.yaml").read_text() != "custom: true\n"


# -- reconcile clean errors -------------------------------------------------


def test_reconcile_bad_cost_usd_names_row(tmp_path):
    conn = ledger.connect(tmp_path / "t.db")
    try:
        inv = tmp_path / "inv.csv"
        inv.write_text("sku,cost_usd\nopenai:gpt-4o-mini,N/A\n")
        with pytest.raises(ValueError, match="row 2"):
            reconcile(conn, inv)
    finally:
        conn.close()


def test_reconcile_cli_clean_error(tmp_path, monkeypatch):
    runner = _init(tmp_path, monkeypatch)
    (tmp_path / "inv.csv").write_text("sku,cost_usd\nopenai:gpt-4o-mini,abc\n")
    r = runner.invoke(main, ["reconcile", "inv.csv"])
    assert r.exit_code != 0
    assert "Traceback" not in r.output
    assert "row 2" in r.output


# -- negative token clamp ---------------------------------------------------


def test_negative_tokens_rejected(tmp_path):
    """Round 3: a negative count is corrupt data, so the line is rejected with a
    message instead of being silently clamped to zero."""
    import pytest
    conn = ledger.connect(tmp_path / "t.db")
    try:
        with pytest.raises(ValueError, match="input_tokens is negative"):
            ledger.insert_event(conn, {
                "idem_key": "neg1", "ts": "2026-10-19T00:00:00Z",
                "provider": "openai", "model_raw": "gpt-4o-mini",
                "input_tokens": -500, "output_tokens": -10,
                "cost_micro_usd": 0,
            })
        assert conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 0
    finally:
        conn.close()
