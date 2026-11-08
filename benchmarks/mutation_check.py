"""Mutation-style sanity check: break the code on purpose and confirm the tests notice.

    python benchmarks/mutation_check.py            # all mutations
    python benchmarks/mutation_check.py -k pricing # only mutations whose name matches

Each mutation flips one condition, operator or constant in a COPY of the source tree
(your checkout is never touched), runs the test suite there, and expects it to FAIL.
A mutation that survives (tests still pass) means a hole in the tests: it is printed
and the script exits 1. It runs the real suite a few dozen times, so it takes minutes,
not seconds. The list is deliberately about money, dates and gates, the places where a
silent wrong answer hurts most; it is not a full mutation-testing tool.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (name, file, old text, new text). `old` must occur exactly once in the file.
MUTATIONS = [
    ("pricing: tier threshold >= instead of >", "agentfin/pricing.py",
     "if prompt > threshold > best:", "if prompt >= threshold > best:"),
    ("pricing: round half-up instead of half-even", "agentfin/pricing.py",
     "return int(total.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))",
     "return int(total.quantize(Decimal(1), rounding=ROUND_HALF_UP))"),
    ("pricing: cache reads billed at the input rate", "agentfin/pricing.py",
     '+ cr * d("cache_read")', '+ cr * d("input")'),
    ("pricing: 1h cache writes billed at the 5m rate", "agentfin/pricing.py",
     '+ cw_1h * d("cache_write_1h" if "cache_write_1h" in active else "cache_write")',
     '+ cw_1h * d("cache_write")'),
    ("pricing: batch/tier modifier ignored", "agentfin/pricing.py",
     "    if mult is not None:\n        total *= Decimal(repr(float(mult)))",
     "    if False:\n        total *= Decimal(repr(float(mult)))"),
    ("pricing: negative tokens no longer clamped", "agentfin/pricing.py",
     "return value if event.get(\"is_correction\") else max(value, 0)", "return value"),
    ("ledger: accept timestamps before 2015", "agentfin/ledger.py",
     "if ts_utc < MIN_TS or ts_utc > latest:", "if ts_utc > latest:"),
    ("ledger: accept far-future timestamps", "agentfin/ledger.py",
     "if ts_utc < MIN_TS or ts_utc > latest:", "if ts_utc < MIN_TS:"),
    ("ledger: negative cost allowed on normal events", "agentfin/ledger.py",
     "or (cost < 0 and not event.get(\"is_correction\"))", "or False"),
    ("money: apportion loses its sign", "agentfin/money.py",
     "return {k: sign * v for k, v in parts.items()}", "return {k: v for k, v in parts.items()}"),
    ("money: convert_parts drops the residue", "agentfin/money.py",
     "    for k in order[:residue]:\n        floors[k] += 1", "    for k in order[:0]:\n        floors[k] += 1"),
    ("reports: --to date exclusive", "agentfin/reports.py",
     "AND substr({column}, 1, 10) <= ?", "AND substr({column}, 1, 10) < ?"),
    ("reports: --from ignored", "agentfin/reports.py",
     'where += f" AND {column} >= ?"', 'where += " AND 1=1"'),
    ("reconcile: tolerance strict", "agentfin/reconcile.py",
     "abs(variance) <= tol", "abs(variance) < tol"),
    ("reconcile: unpriced SKU may match", "agentfin/reconcile.py",
     "and sku not in unpriced,", ","),
    ("reconcile: accounting negatives lose sign", "agentfin/reconcile.py",
     "return -val if neg else val", "return val"),
    ("reconcile: comma file accepts decimal comma", "agentfin/reconcile.py",
     'if _THOUSANDS.match(t) and delimiter == ",":', 'if _THOUSANDS.match(t):'),
    ("close: late events not counted", "agentfin/close.py",
     '"ok": not altered and late["n"] == 0,', '"ok": not altered,'),
    ("close: altered rows not noticed", "agentfin/close.py",
     "altered = digest != c[\"digest\"]", "altered = False"),
    ("close: journal credit sign", "agentfin/close.py",
     '"Credits": f"{total:.2f}" if total >= 0 else "",',
     '"Credits": f"{-total:.2f}" if total >= 0 else "",'),
    ("close: negative team amounts shown as debits", "agentfin/close.py",
     '"Debits": f"{amount:.2f}" if amount > 0 else "",', '"Debits": f"{amount:.2f}",'),
    ("budgets: alert only when already over", "agentfin/budgets.py",
     "breached = forecast >= monthly_micro * threshold", "breached = spent >= monthly_micro"),
    ("budgets: save not atomic-safe (temp file left)", "agentfin/budgets.py",
     "        if tmp.exists():\n            tmp.unlink()", "        pass"),
    ("claude-code: sidechain not separated", "agentfin/adapters/claude_code.py",
     '"claude-code-subagent" if d.get("isSidechain") else "claude-code"', '"claude-code"'),
    ("claude-code: less complete line wins", "agentfin/adapters/claude_code.py",
     "return _rank(new) > _rank(current)", "return _rank(new) < _rank(current)"),
    ("claude-code: split responses double counted", "agentfin/adapters/claude_code.py",
     'event["idem_key"] = f"anthropic:{msg[\'id\']}"', 'event["idem_key"] = f"anthropic:{d.get(\'uuid\')}"'),
    ("cli: read commands create a missing ledger", "agentfin/cli.py",
     'if db != ":memory:" and not Path(db).is_file():', "if False:"),
    ("cli: alert gate exit code", "agentfin/cli.py",
     "ALERT_EXIT_CODE = 3", "ALERT_EXIT_CODE = 0"),
    ("cli: budget gate passes with unpriced events", "agentfin/cli.py",
     'if fail_on_alert and any(r["alert"] or r.get("unpriced_events") for r in results):\n            ctx.exit(ALERT_EXIT_CODE)\n        return',
     'if fail_on_alert and any(r["alert"] for r in results):\n            ctx.exit(ALERT_EXIT_CODE)\n        return'),
    ("cli: small budgets shown as $0", "agentfin/cli.py",
     'return f"${usd:,.0f}" if usd == int(usd) else f"${usd:,.2f}"', 'return f"${usd:,.0f}"'),
    ("cli: --modified-within ignored", "agentfin/cli.py",
     "    if max_age_s is None:\n        return True\n    try:", "    if True:\n        return True\n    try:"),
    ("export: spreadsheet formula injection", "agentfin/export.py",
     "def safe_cell(value: Any) -> Any:", "def safe_cell(value: Any) -> Any:\n    return value"),
    ("textsafe: control characters kept", "agentfin/textsafe.py",
     "def clean(value: object, limit: int = 256) -> str:",
     "def clean(value: object, limit: int = 256) -> str:\n    return str(value)[:limit]"),
    ("cli: init ignores a global --config-dir", "agentfin/cli.py",
     'cfg = Path(config_dir) if config_dir else ctx.obj["paths"]["pricing"].parent',
     "cfg = Path(config_dir) if config_dir else DEFAULT_DIR"),
    ("cli: status waits the full 30 s on a locked ledger", "agentfin/cli.py",
     'ctx.obj["paths"]["db"], timeout=3)', 'ctx.obj["paths"]["db"], timeout=30)'),
    ("close: an empty month writes a lone 0.00 line", "agentfin/close.py",
     "if not rows and total == 0:", "if False:"),
    ("reconcile: UTF-16 invoices not detected", "agentfin/reconcile.py",
     'enc = "utf-16" if head in (b"\\xff\\xfe", b"\\xfe\\xff") else "utf-8-sig"',
     'enc = "utf-8-sig"'),
]


def run_suite(tree: Path) -> bool:
    """True if the suite passes in `tree`."""
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"],
        cwd=tree, capture_output=True, text=True, env={"PYTHONPATH": str(tree), "PATH": "/usr/bin:/bin",
                                                       "HOME": str(tree)},
    )
    return r.returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-k", default="", help="only mutations whose name contains this text")
    args = ap.parse_args()
    todo = [m for m in MUTATIONS if args.k.lower() in m[0].lower()]
    survived, killed, bad = [], 0, []
    with tempfile.TemporaryDirectory(prefix="agentfin-mut-") as tmp:
        work = Path(tmp) / "tree"
        shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", "*.egg-info", ".pytest_cache", ".ruff_cache",
            "build", "dist"))
        for name, rel, old, new in todo:
            path = work / rel
            original = path.read_text(encoding="utf-8")
            if original.count(old) != 1:
                bad.append(name)
                print(f"  BROKEN MUTATION (text not found exactly once): {name}")
                continue
            path.write_text(original.replace(old, new), encoding="utf-8")
            t0 = time.time()
            try:
                passed = run_suite(work)
            finally:
                path.write_text(original, encoding="utf-8")
            if passed:
                survived.append(name)
                print(f"  SURVIVED  {name}  ({time.time() - t0:.0f}s)")
            else:
                killed += 1
                print(f"  killed    {name}  ({time.time() - t0:.0f}s)")
    print(f"\n{killed} killed, {len(survived)} survived, {len(bad)} broken of {len(todo)}")
    return 1 if survived or bad else 0


if __name__ == "__main__":
    sys.exit(main())
