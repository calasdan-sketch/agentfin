import pytest

from agentfin import ledger, outcomes, redact


@pytest.fixture
def conn(tmp_path):
    c = ledger.connect(tmp_path / "t.db")
    yield c
    c.close()


def _cost(conn, task, micro):
    ledger.insert_event(conn, {
        "idem_key": f"c-{task}", "ts": "2026-10-19T09:00:00Z",
        "provider": "openai", "model_raw": "gpt-4o-mini",
        "input_tokens": 0, "output_tokens": 0, "cost_micro_usd": micro,
        "task": task, "agent": "a", "team": "t",
    })
    conn.commit()


def test_roi_basic(conn):
    _cost(conn, "support-bot", 10_000_000)  # $10
    outcomes.tag_outcome(conn, "support-bot", "revenue", 50.0)
    rows = outcomes.roi_report(conn)
    assert len(rows) == 1
    r = rows[0]
    assert r["cost_micro_usd"] == 10_000_000
    assert r["value_micro_usd"] == 50_000_000
    assert r["roi"] == pytest.approx(5.0)
    assert r["cost_per_dollar"] == pytest.approx(0.2)


def test_hours_saved_converted_at_rate(conn):
    _cost(conn, "pr-review", 1_000_000)
    outcomes.tag_outcome(conn, "pr-review", "hours_saved", 10, hourly_rate_usd=100.0)
    rows = outcomes.roi_report(conn)
    assert rows[0]["value_micro_usd"] == 1_000_000_000  # 10h * $100


def test_outcome_without_cost_is_infinite_not_crash(conn):
    outcomes.tag_outcome(conn, "free-task", "revenue", 25.0)
    rows = outcomes.roi_report(conn)
    assert rows[0]["roi"] == float("inf")


def test_worst_roi_first(conn):
    _cost(conn, "good", 1_000_000)
    _cost(conn, "bad", 100_000_000)
    outcomes.tag_outcome(conn, "good", "revenue", 100.0)
    outcomes.tag_outcome(conn, "bad", "revenue", 1.0)
    rows = outcomes.roi_report(conn)
    assert [r["task"] for r in rows] == ["bad", "good"]


def test_redact_keys():
    meta = {"prompt": "secret instructions", "run_id": "r1",
            "nested": {"api_key": "abc123"}}
    out = redact.redact(meta)
    assert out["prompt"] == "[REDACTED]"
    assert out["run_id"] == "r1"
    assert out["nested"]["api_key"] == "[REDACTED]"


def test_redact_credential_strings():
    s = "using key sk-abcdef1234567890 for the call"
    assert "sk-abcdef" not in redact.redact(s)
    assert "[REDACTED]" in redact.redact(s)


def test_redact_non_string_passthrough():
    assert redact.redact(42) == 42
    assert redact.redact(None) is None
