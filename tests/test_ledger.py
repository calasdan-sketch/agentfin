import pytest

from agentfin import ledger


@pytest.fixture
def conn(tmp_path):
    c = ledger.connect(tmp_path / "test.db")
    yield c
    c.close()


def _event(**kw):
    base = {
        "idem_key": "k1",
        "ts": "2026-10-19T09:00:00Z",
        "provider": "anthropic",
        "model_raw": "claude-sonnet-4-5",
        "input_tokens": 1000,
        "output_tokens": 500,
        "cost_micro_usd": 10500,
        "task": "coding",
        "agent": "claude-code",
        "team": "platform",
    }
    base.update(kw)
    return base


def test_insert_and_dedupe(conn):
    assert ledger.insert_event(conn, _event()) is True
    assert ledger.insert_event(conn, _event()) is False  # duplicate idem_key
    n = conn.execute("SELECT COUNT(*) c FROM usage_events").fetchone()["c"]
    assert n == 1


def test_epoch_timestamp_normalized_to_utc(conn):
    ledger.insert_event(conn, _event(idem_key="k2", ts=1760878920))
    row = conn.execute("SELECT ts_utc FROM usage_events WHERE idem_key='k2'").fetchone()
    assert row["ts_utc"].endswith("Z")
    assert "T" in row["ts_utc"]


def test_offset_timestamp_converted_and_preserved(conn):
    ledger.insert_event(conn, _event(idem_key="k3", ts="2026-10-19T09:00:00-05:00"))
    row = conn.execute(
        "SELECT ts_utc, ts_offset FROM usage_events WHERE idem_key='k3'"
    ).fetchone()
    assert row["ts_utc"] == "2026-10-19T14:00:00Z"  # -05:00 -> UTC
    assert row["ts_offset"] == "-05:00"


def test_naive_timestamp_assumed_utc(conn):
    ledger.insert_event(conn, _event(idem_key="k4", ts="2026-10-19 09:00:00"))
    row = conn.execute("SELECT ts_utc FROM usage_events WHERE idem_key='k4'").fetchone()
    assert row["ts_utc"] == "2026-10-19T09:00:00Z"


def test_bad_timestamp_raises():
    with pytest.raises(ValueError):
        ledger.parse_ts("not a timestamp")


def test_unpriced_cost_is_null(conn):
    ledger.insert_event(conn, _event(idem_key="k5", cost_micro_usd=None))
    row = conn.execute(
        "SELECT cost_micro_usd FROM usage_events WHERE idem_key='k5'"
    ).fetchone()
    assert row["cost_micro_usd"] is None


def test_idem_key_stable():
    rec = {"a": 1, "b": [2, 3]}
    assert ledger.idem_key_for(rec) == ledger.idem_key_for({"b": [2, 3], "a": 1})
