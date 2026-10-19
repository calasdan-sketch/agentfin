from datetime import datetime, timedelta, timezone

import pytest

from agentfin import anomalies, budgets, ledger


@pytest.fixture
def conn(tmp_path):
    c = ledger.connect(tmp_path / "t.db")
    yield c
    c.close()


def _seed_daily(conn, agent, daily_micro, days, end=None):
    """Seed `days` of daily spend ending yesterday (or `end`)."""
    end = end or (datetime.now(timezone.utc) - timedelta(days=1))
    for i in range(days):
        day = end - timedelta(days=days - 1 - i)
        for j in range(2):  # two events per day -> also tests aggregation
            ledger.insert_event(conn, {
                "idem_key": f"{agent}-{i}-{j}",
                "ts": day.strftime("%Y-%m-%dT12:00:00Z"),
                "provider": "openai", "model_raw": "gpt-4o-mini",
                "input_tokens": 0, "output_tokens": 0,
                "cost_micro_usd": daily_micro // 2,
                "agent": agent, "team": "platform", "task": "batch-job",
            })
    conn.commit()


def test_robust_z_spike_detected():
    baseline = [100.0] * 20
    assert anomalies.robust_z(1000.0, baseline) > 3.5
    assert abs(anomalies.robust_z(100.0, baseline)) < 0.01


def test_robust_z_flat_baseline_zero_mad():
    assert anomalies.robust_z(100.0, [100.0] * 20) == 0.0
    assert anomalies.robust_z(500.0, [100.0] * 20) == float("inf")


def test_detect_flags_spike_with_probable_cause(conn):
    _seed_daily(conn, "runner", 1_000_000, 20)
    # Spike day: yesterday was seeded at baseline; add a spike today.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT12:00:00Z")
    ledger.insert_event(conn, {
        "idem_key": "spike-1", "ts": today,
        "provider": "openai", "model_raw": "gpt-4o",
        "input_tokens": 0, "output_tokens": 0, "cost_micro_usd": 10_000_000,
        "agent": "runner", "team": "platform", "task": "runaway-loop",
    })
    conn.commit()
    alerts = anomalies.detect(conn, dimension="agent", record=False)
    assert len(alerts) == 1
    a = alerts[0]
    assert a["scope"] == "agent=runner"
    assert a["multiple"] >= 2.0
    assert any(c["task"] == "runaway-loop" for c in a["probable_cause"])


def test_detect_ignores_small_bump(conn):
    _seed_daily(conn, "steady", 1_000_000, 20)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT12:00:00Z")
    ledger.insert_event(conn, {
        "idem_key": "bump-1", "ts": today,
        "provider": "openai", "model_raw": "gpt-4o-mini",
        "input_tokens": 0, "output_tokens": 0, "cost_micro_usd": 1_200_000,
        "agent": "steady", "team": "platform", "task": "batch-job",
    })
    conn.commit()
    assert anomalies.detect(conn, dimension="agent", record=False) == []


def test_detect_requires_minimum_baseline(conn):
    _seed_daily(conn, "new", 1_000_000, 5)
    assert anomalies.detect(conn, dimension="agent", record=False) == []


def test_cooldown_and_dismiss(conn):
    assert anomalies._cooldown_ok(conn, "anomaly:agent:x") is True
    anomalies._record_alert(conn, "anomaly:agent:x", {"scope": "x"})
    assert anomalies._cooldown_ok(conn, "anomaly:agent:x") is False
    assert anomalies.dismiss(conn, "anomaly:agent:x") is True
    assert anomalies._cooldown_ok(conn, "anomaly:agent:x") is False  # stays dismissed


def _budget_conn(tmp_path):
    c = ledger.connect(tmp_path / "b.db")
    return c


def test_budget_forecast_alerts_before_overrun(tmp_path):
    conn = _budget_conn(tmp_path)
    now = datetime.now(timezone.utc)
    # Day 5 of the month: already spent $400 of a $500 budget -> forecast $2400.
    day5 = now.replace(day=5, hour=12, minute=0, second=0, microsecond=0)
    ledger.insert_event(conn, {
        "idem_key": "spend-1", "ts": day5.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provider": "openai", "model_raw": "gpt-4o",
        "input_tokens": 0, "output_tokens": 0,
        "cost_micro_usd": 400_000_000,
        "agent": "a1", "team": "platform", "task": "t",
    })
    conn.commit()
    blist = [{"scope": "team:platform", "monthly_usd": 500, "alert_at": 0.8}]
    results = budgets.check(conn, blist, now=now.replace(day=5))
    conn.close()
    assert len(results) == 1
    r = results[0]
    assert r["alert"] is True
    assert r["forecast_pct_of_budget"] > 100


def test_budget_ok_when_under(tmp_path):
    conn = _budget_conn(tmp_path)
    now = datetime.now(timezone.utc).replace(day=5)
    day5 = now.replace(hour=12, minute=0, second=0, microsecond=0)
    ledger.insert_event(conn, {
        "idem_key": "spend-2", "ts": day5.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provider": "openai", "model_raw": "gpt-4o-mini",
        "input_tokens": 0, "output_tokens": 0, "cost_micro_usd": 10_000_000,
        "agent": "a1", "team": "platform", "task": "t",
    })
    conn.commit()
    blist = [{"scope": "team:platform", "monthly_usd": 500, "alert_at": 0.8}]
    results = budgets.check(conn, blist, now=now)
    conn.close()
    assert results[0]["alert"] is False
