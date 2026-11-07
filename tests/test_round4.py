"""Round 4: what a stranger who just ran `pip install agentfin` actually meets."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

import agentfin
from agentfin.cli import main

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "examples" / "claude-code-demo"


@pytest.fixture
def ledger_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner()
    assert r.invoke(main, ["init"]).exit_code == 0
    assert r.invoke(main, ["ingest", "--adapter", "claude-code", str(DEMO)]).exit_code == 0
    return tmp_path


# --- demo ships in the wheel and touches nothing ---------------------------------------

def test_demo_matches_readme_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(main, ["demo"])
    assert r.exit_code == 0, r.output
    assert "ingested 24 events" in r.output
    assert re.search(r"acme-api\s+\$0\.61\s+12", r.output)
    assert list(tmp_path.iterdir()) == []  # no .agentfin/, no db


def test_packaged_demo_data_equals_examples():
    pkg = ROOT / "agentfin" / "demo_data" / "projects"
    src = DEMO / "projects"
    a = {p.relative_to(pkg): p.read_bytes() for p in pkg.rglob("*.jsonl")}
    b = {p.relative_to(src): p.read_bytes() for p in src.rglob("*.jsonl")}
    assert a == b and len(a) == 3


# --- reads never invent an empty ledger -------------------------------------------------

@pytest.mark.parametrize("cmd", [["report"], ["doctor"], ["trends"], ["alerts"], ["savings"],
                                 ["export", "--out", "x.csv"], ["dashboard", "--out", "d.html"],
                                 ["outcomes", "roi"], ["reprice"], ["anomalies"]])
def test_read_commands_refuse_missing_ledger(tmp_path, monkeypatch, cmd):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(main, cmd)
    assert r.exit_code == 1
    assert "ledger not found" in r.output and "agentfin init" in r.output
    assert not (tmp_path / ".agentfin").exists()  # and none was created as a side effect


# --- environment problems become one plain line -----------------------------------------

def test_damaged_ledger_is_plain_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bad.db").write_bytes(b"this is not sqlite" * 50)
    r = CliRunner().invoke(main, ["--db", "bad.db", "report"])
    assert r.exit_code == 1 and "Traceback" not in r.output
    assert "not a readable agentfin database" in r.output
    assert r.exception is None or isinstance(r.exception, SystemExit)


def test_broken_yaml_is_plain_error_and_init_force_repairs(ledger_dir):
    cfg = ledger_dir / ".agentfin" / "config.yaml"
    cfg.write_text("x: [\n", encoding="utf-8")
    r = CliRunner().invoke(main, ["report"])
    assert r.exit_code == 1 and "not valid YAML" in r.output and "line" in r.output
    assert isinstance(r.exception, SystemExit)
    assert CliRunner().invoke(main, ["init", "--force"]).exit_code == 0
    assert CliRunner().invoke(main, ["report", "--by", "task"]).exit_code == 0


def test_bad_date_is_usage_error_even_without_ledger(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(main, ["export", "--from", "nonsense", "--out", "x.csv"])
    assert r.exit_code == 2


def test_closed_pipe_exits_quietly(ledger_dir):
    p = subprocess.Popen(
        [sys.executable, "-m", "agentfin", "export", "--format", "csv", "--out", "-"],
        cwd=ledger_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin", "PYTHONIOENCODING": "utf-8"})
    p.stdout.read(10)
    p.stdout.close()
    err = p.stderr.read().decode()
    p.wait(timeout=30)
    assert "Traceback" not in err and p.returncode == 0


# --- Windows-flavoured input ------------------------------------------------------------

_LINE = {"ts": "2026-10-02T10:00:00Z", "provider": "anthropic", "model": "claude-sonnet-5-5",
         "input_tokens": 100, "output_tokens": 50}


def _ingest(path, extra=()):
    return CliRunner().invoke(main, ["ingest", "--adapter", "jsonl", *extra, str(path)])


def test_utf16_bom_file_ingests(ledger_dir):  # PowerShell 5's `>` writes this
    f = ledger_dir / "u16.jsonl"
    f.write_bytes(("\n".join(json.dumps({**_LINE, "id": f"u{i}"}) for i in range(2)) + "\n")
                  .encode("utf-16"))
    r = _ingest(f)
    assert r.exit_code == 0 and "ingested 2 events" in r.output and "malformed" not in r.output


def test_utf16_on_stdin(ledger_dir):
    data = (json.dumps({**_LINE, "id": "s1"}) + "\n").encode("utf-16")
    r = CliRunner().invoke(main, ["ingest", "--adapter", "jsonl", "-"], input=data)
    assert "ingested 1 events" in r.output


def test_crlf_and_bom_files_ingest(ledger_dir):
    a = ledger_dir / "crlf.jsonl"
    a.write_bytes(b"\r\n".join(json.dumps({**_LINE, "id": f"c{i}"}).encode() for i in range(2))
                  + b"\r\n")
    b = ledger_dir / "bom.jsonl"
    b.write_bytes(b"\xef\xbb\xbf" + json.dumps({**_LINE, "id": "b1"}).encode() + b"\n")
    assert "ingested 2 events" in _ingest(a).output
    assert "ingested 1 events" in _ingest(b).output


def test_works_from_a_folder_with_spaces(tmp_path, monkeypatch):
    d = tmp_path / "my folder (test)"
    d.mkdir()
    monkeypatch.chdir(d)
    r = CliRunner()
    assert r.invoke(main, ["init"]).exit_code == 0
    assert r.invoke(main, ["ingest", "--adapter", "claude-code", str(DEMO)]).exit_code == 0
    assert r.invoke(main, ["report", "--by", "task"]).exit_code == 0


# --- CI gates ---------------------------------------------------------------------------

def test_budgets_check_json(ledger_dir):
    r = CliRunner()
    r.invoke(main, ["budgets", "set", "team:unassigned", "0.01"])
    out = r.invoke(main, ["budgets", "check", "--json", "--fail-on-alert"])
    data = json.loads(out.output)
    assert any(x["scope"] == "team:unassigned" for x in data)


# --- packaging truth --------------------------------------------------------------------

def test_version_is_consistent_everywhere():
    pp = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    ver = re.search(r'^version = "([^"]+)"', pp, re.M).group(1)
    assert ver == agentfin.__version__
    assert f"## {ver} " in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_package_has_license_and_all_subpackages():
    assert "MIT License" in (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert (ROOT / "agentfin" / "adapters" / "__init__.py").exists()
    pp = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'license = "MIT"' in pp and "demo_data" in pp
