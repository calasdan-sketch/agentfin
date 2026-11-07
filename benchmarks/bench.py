"""Re-runnable benchmark: `python benchmarks/bench.py [N]` (default N=100000).

All data is synthetic and generated from a fixed seed, so the *numbers* in the
accuracy section are exactly reproducible; timings depend on your machine.

1. Accuracy: a synthetic Claude Code transcript set where every API response is
   written as 1-4 repeated assistant lines, 30% of sessions are forked copies,
   and 5% of responses are still streaming when first ingested. A tool that sums
   every assistant line over-counts; agentfin must land on the true total.
2. Throughput: ingest N events, re-ingest them (idempotent no-op), run a report,
   then close the month, verify it and write the journal.
"""

from __future__ import annotations

import json
import random
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # run from a checkout

from agentfin import close, ledger
from agentfin.cli import main

SEED = 20261107


def gen_transcripts(root: Path, responses: int = 2000) -> dict[str, int]:
    rng = random.Random(SEED)
    base = datetime(2026, 10, 1, tzinfo=timezone.utc)
    lines_total = true_out = naive_out = 0
    sessions: list[list[str]] = []
    cur: list[str] = []
    for i in range(responses):
        ts = (base + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out = rng.randint(50, 3000)
        usage = {"input_tokens": rng.randint(1, 50), "output_tokens": out,
                 "cache_read_input_tokens": rng.randint(0, 40000),
                 "cache_creation_input_tokens": rng.randint(0, 3000)}
        n_lines = rng.randint(1, 4)
        streaming = rng.random() < 0.05
        for k in range(n_lines):
            cur.append(json.dumps({
                "type": "assistant", "timestamp": ts, "sessionId": "s", "uuid": f"u{i}-{k}",
                "cwd": "/work/demo", "message": {
                    "id": f"msg_{i}", "model": "claude-sonnet-5", "usage": usage,
                    "stop_reason": None if (streaming and k == 0) else "end_turn"}}))
            naive_out += out
            lines_total += 1
        true_out += out
        if rng.random() < 0.02:
            sessions.append(cur)
            cur = []
    sessions.append(cur)
    root.mkdir(parents=True, exist_ok=True)
    for n, lines in enumerate(sessions):
        (root / f"session-{n}.jsonl").write_text("\n".join(lines) + "\n")
        if rng.random() < 0.30:  # forked copy of the whole session
            (root / f"session-{n}-fork.jsonl").write_text("\n".join(lines) + "\n")
            lines_total += len(lines)
            naive_out += sum(json.loads(x)["message"]["usage"]["output_tokens"] for x in lines)
    return {"responses": responses, "lines": lines_total, "true_output": true_out,
            "naive_output": naive_out}


def accuracy(tmp: Path) -> None:
    truth = gen_transcripts(tmp / "t")
    runner = CliRunner()
    db = str(tmp / "acc.db")
    cfg = str(tmp / "cfg")
    runner.invoke(main, ["--config-dir", cfg, "init", "--config-dir", cfg])
    res = runner.invoke(main, ["--db", db, "--config-dir", cfg, "ingest", "--adapter",
                               "claude-code", str(tmp / "t")])
    conn = ledger.connect(db)
    row = conn.execute("SELECT COUNT(*) n, SUM(output_tokens) o FROM usage_events "
                       "WHERE is_correction = 0").fetchone()
    print(f"== accuracy (synthetic transcripts, seed {SEED})")
    print(f"  true responses        {truth['responses']:>10,}")
    print(f"  assistant lines       {truth['lines']:>10,}  (incl. forked copies)")
    print(f"  true output tokens    {truth['true_output']:>10,}")
    print(f"  naive line-sum tokens {truth['naive_output']:>10,}  "
          f"({truth['naive_output'] / truth['true_output']:.2f}x over-count)")
    print(f"  agentfin events       {row['n']:>10,}")
    print(f"  agentfin out tokens   {row['o']:>10,}")
    print(f"  ingest said: {res.output.strip()}")
    assert row["n"] == truth["responses"]
    conn.close()


def throughput(tmp: Path, n: int) -> None:
    rng = random.Random(SEED)
    base = datetime(2026, 10, 1, tzinfo=timezone.utc)
    src = tmp / "big.jsonl"
    with src.open("w") as f:
        for i in range(n):
            ts = (base + timedelta(seconds=i * 20)).strftime("%Y-%m-%dT%H:%M:%SZ")
            f.write(json.dumps({
                "id": f"r{i}", "ts": ts, "provider": "anthropic", "model": "claude-sonnet-5",
                "input_tokens": rng.randint(100, 5000), "output_tokens": rng.randint(10, 900),
                "cache_read_tokens": rng.randint(0, 20000), "team": f"t{i % 7}",
                "task": f"task{i % 50}"}) + "\n")
    cfg, db = str(tmp / "cfg2"), str(tmp / "thr.db")
    runner = CliRunner()
    runner.invoke(main, ["init", "--config-dir", cfg])
    args = ["--db", db, "--config-dir", cfg]

    def timed(label, argv):
        t, c = time.perf_counter(), time.process_time()
        res = runner.invoke(main, [*args, *argv])
        dt, cpu = time.perf_counter() - t, time.process_time() - c
        # wall time swings with machine load; CPU time is the steadier number
        print(f"  {label:<26}{dt:>8.2f}s wall {cpu:>6.2f}s cpu  ({n / dt:>9,.0f} events/s)")
        return res

    print(f"== throughput ({n:,} synthetic events)")
    timed("ingest", ["ingest", str(src)])
    timed("re-ingest (all dupes)", ["ingest", str(src)])
    timed("report --by task", ["report", "--by", "task"])
    timed("report --by day --tz", ["report", "--by", "day", "--tz", "America/Chicago"])
    inv = tmp / "invoice.csv"
    inv.write_text("sku,cost_usd\nanthropic:claude-sonnet-5,1\n", encoding="utf-8")
    timed("reconcile (1 invoice row)", ["reconcile", str(inv), "--json"])
    month = "2026-10"
    conn = ledger.connect(db)
    t = time.perf_counter()
    close.run_close(conn, month, fx={"CAD": "1.37"}, allow_unpriced=True,
                    now=datetime(2027, 6, 1, tzinfo=timezone.utc),
                    pricing_path=Path(cfg) / "pricing.yaml")
    dt = time.perf_counter() - t
    print(f"  {'close run ' + month:<26}{dt:>8.2f}s")
    t = time.perf_counter()
    v = close.verify(conn, month)
    print(f"  {'close verify':<26}{time.perf_counter() - t:>8.2f}s  ok={v['ok']}")
    t = time.perf_counter()
    close.journal_rows(conn, month, {}, "CAD")
    print(f"  {'close journal (CAD)':<26}{time.perf_counter() - t:>8.2f}s")


def hostile(tmp: Path) -> None:
    """Corrupt records mixed into good ones must be refused, and the good ones
    must still land exactly. Every bad line below is something a real export has
    contained at some point (negative counts, ms-epoch timestamps, NaN)."""
    rng = random.Random(SEED)
    good, bad = 5000, 0
    base = {"provider": "anthropic", "model": "claude-sonnet-5", "output_tokens": 5}
    lines = []
    for i in range(good):
        lines.append({**base, "id": f"g{i}", "ts": "2026-10-02T10:00:00Z",
                      "input_tokens": 10})
        if rng.random() < 0.2:
            bad += 1
            lines.append(rng.choice([
                {**base, "id": f"b{i}", "ts": "2026-10-02T10:00:00Z", "input_tokens": -1},
                {**base, "id": f"b{i}", "ts": 1760000000000, "input_tokens": 1},
                {**base, "id": f"b{i}", "ts": "2026-10-02T10:00:00Z",
                 "input_tokens": "NaN"},
                {**base, "id": f"b{i}", "ts": "2026-10-02T10:00:00Z",
                 "input_tokens": 10 ** 30},
            ]))
    src = tmp / "hostile.jsonl"
    src.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    cfg, db = str(tmp / "cfg3"), str(tmp / "h.db")
    runner = CliRunner()
    runner.invoke(main, ["init", "--config-dir", cfg])
    res = runner.invoke(main, ["--db", db, "--config-dir", cfg, "ingest", str(src)])
    first = res.output.splitlines()[0]
    print("== hostile input (good lines mixed with corrupt ones)")
    print(f"  good lines {good:,}, corrupt lines {bad:,}")
    print(f"  ingest said: {first}")
    assert f"ingested {good} events" in first
    assert f"{bad} malformed" in res.output


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100_000
    with tempfile.TemporaryDirectory() as d:
        accuracy(Path(d))
        hostile(Path(d))
        throughput(Path(d), n)
