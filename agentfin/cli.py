"""agentfin CLI."""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml

from . import (
    __author__,
    __version__,
    anomalies,
    attribution,
    budgets,
    dashboard,
    export,
    gitlink,
    ledger,
    outcomes,
    pricing,
    reconcile,
    reports,
    textsafe,
    whatif,
)
from . import (
    allocate as allocate_mod,
)
from . import (
    close as close_mod,
)
from .adapters.base import IngestContext
from .adapters.claude_code import ClaudeCodeAdapter, default_roots
from .adapters.gateway import GatewayAdapter
from .adapters.jsonl import JsonlAdapter
from .adapters.otel import OtelAdapter
from .adapters.provider import ProviderAdapter

ADAPTERS = {
    "claude-code": ClaudeCodeAdapter(),
    "jsonl": JsonlAdapter(),
    "provider": ProviderAdapter(),
    "gateway": GatewayAdapter(),
    "otel": OtelAdapter(),
}

DEFAULT_DB = "agentfin.db"

#: Exit status for "the check ran fine and found something" (CI / cron gates).
#: Distinct from 1 (error) and 2 (usage error).
ALERT_EXIT_CODE = 3
DEFAULT_DIR = Path(".agentfin")


def _paths(db, config_dir):
    cfg = Path(config_dir) if config_dir else DEFAULT_DIR
    return {
        "db": db or str(cfg / DEFAULT_DB),
        "pricing": cfg / "pricing.yaml",
        "rules": cfg / "rules.yaml",
        "budgets": cfg / "budgets.yaml",
        "config": cfg / "config.yaml",
    }


def _load_config(paths) -> dict:
    p = paths["config"]
    if p.exists():
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {}


def _fx_convert(micro, currency, fx_rates) -> tuple[str, bool]:
    """Convert micro-USD to display currency. Returns (formatted, is_estimate)."""
    if micro is None:
        return "UNPRICED", False
    usd = micro / 1_000_000
    if not currency or currency.upper() == "USD":
        return (f"-${-usd:,.2f}" if usd < 0 else f"${usd:,.2f}"), False
    rate = (fx_rates or {}).get(currency.upper())
    if not rate:
        return f"${usd:,.2f} USD (no FX rate for {currency})", False
    return f"{usd * rate:,.2f} {currency.upper()}*", True


class _FriendlyGroup(click.Group):
    """Turn environment problems (damaged ledger, broken YAML, no permission,
    full disk, closed pipe) into one plain line and exit 1, never a traceback.
    Genuine bugs still raise, so they can be reported with their traceback."""

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except click.ClickException:
            raise
        except click.exceptions.Exit:
            raise
        except BrokenPipeError:
            try:  # reader (e.g. `| head`) went away: not an error; silence the exit flush
                sys.stdout = open(os.devnull, "w")  # noqa: SIM115
            except OSError:
                pass
            sys.exit(0)
        except sqlite3.DatabaseError as exc:
            msg = str(exc).lower()
            if "locked" in msg or "busy" in msg:
                why = "the ledger is busy (another agentfin is writing); try again in a moment"
            elif "not a database" in msg or "malformed" in msg or "encrypted" in msg:
                why = ("the ledger file is not a readable agentfin database or is damaged; "
                       "restore it from a backup or point --db at a different file")
            elif "readonly" in msg or "read-only" in msg or "unable to open" in msg:
                why = "the ledger cannot be opened for writing (permissions or read-only disk)"
            elif "full" in msg:
                why = "the disk is full"
            else:
                why = textsafe.clean(str(exc), 200)
            raise click.ClickException(why) from None
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            where = f" (line {mark.line + 1})" if mark is not None else ""
            raise click.ClickException(
                f"a config file is not valid YAML{where}: "
                f"{textsafe.clean(getattr(exc, 'problem', None) or 'syntax error', 120)}. "
                "Fix it, or run `agentfin init --force` to rewrite the defaults"
            ) from None
        except PermissionError as exc:
            raise click.ClickException(
                f"permission denied: {textsafe.clean(exc.filename or exc, 200)}") from None
        except OSError as exc:
            raise click.ClickException(
                f"file problem: {textsafe.clean(exc.strerror or exc, 120)}"
                + (f" ({textsafe.clean(exc.filename, 200)})" if exc.filename else "")
            ) from None
        except UnicodeDecodeError:
            raise click.ClickException(
                "a config file is not valid UTF-8 text; re-save it as UTF-8") from None


@click.group(cls=_FriendlyGroup)
@click.version_option(__version__, prog_name="agentfin",
                      message=f"%(prog)s, version %(version)s ({__author__})")
@click.option("--db", default=None, help="SQLite db path")
@click.option("--config-dir", default=None, help="Config dir (pricing/rules/budgets)")
@click.pass_context
def main(ctx, db, config_dir):
    for stream in (sys.stdout, sys.stderr):
        try:  # a legacy Windows code page must not crash on the arrows and ticks we print
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    ctx.ensure_object(dict)
    paths = _paths(db, config_dir)
    ctx.obj["paths"] = paths
    # `init --force` is the repair path for a broken config, so it must not read it;
    # `demo` uses its own throwaway config.
    ctx.obj["config"] = {} if ctx.invoked_subcommand in ("init", "demo") \
        else _load_config(paths)


@main.command()
@click.option("--config-dir", default=None,
              help="where to scaffold (also accepted before the command: "
                   "`agentfin --config-dir X init`)")
@click.option("--force", is_flag=True,
              help="overwrite existing config files (default: refuse)")
@click.pass_context
def init(ctx, config_dir, force):
    """Scaffold config files and an empty ledger."""
    # `agentfin --config-dir X init` used to be ignored (it scaffolded ./.agentfin).
    cfg = Path(config_dir) if config_dir else ctx.obj["paths"]["pricing"].parent
    cfg.mkdir(parents=True, exist_ok=True)
    files = {
        "pricing.yaml": pricing.DEFAULT_PRICING,
        "rules.yaml": attribution.DEFAULT_RULES,
        "budgets.yaml": budgets.DEFAULT_BUDGETS,
        "config.yaml": (
            "# display_currency: USD  (FX shown as estimate, marked *)\n"
            "# fx:\n#   CAD: 1.36\n#   EUR: 0.92\n"
            "display_currency: USD\nfx: {}\nredact_keys: []\n"
        ),
    }
    existing = [name for name in files if (cfg / name).exists()]
    if existing and not force:
        raise click.ClickException(
            f"refusing to overwrite existing config: {', '.join(existing)} "
            "(use --force to overwrite)"
        )
    for name, content in files.items():
        (cfg / name).write_text(content, encoding="utf-8")
    global_db = ctx.parent.params.get("db") if ctx.parent else None
    conn = ledger.connect(global_db or cfg / DEFAULT_DB)
    conn.close()
    click.echo(f"initialized agentfin in {cfg}")


MAX_REPORTED_ERRORS = 10


PRICE_BOOK_STALE_DAYS = 60
def _open_text(src: str):
    """Open an input file (or stdin for `-`) as text. UTF-8 with or without a BOM,
    and UTF-16 with a BOM (what Windows PowerShell 5's `>` writes) both work."""
    if src == "-":
        buf = sys.stdin.buffer
        if not hasattr(buf, "peek"):
            buf = io.BufferedReader(buf)
        try:
            head = buf.peek(2)[:2]
        except (ValueError, OSError):
            head = b""
        enc = "utf-16" if head in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
        return io.TextIOWrapper(buf, encoding=enc, errors="replace")
    with open(src, "rb") as raw:
        head = raw.read(2)
    enc = "utf-16" if head in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
    return open(src, encoding=enc, errors="replace")


MAX_LINE_CHARS = 16 * 1024 * 1024  # a usage record is a few hundred bytes


def _bounded_lines(fh):
    """Yield (lineno, text); a line longer than MAX_LINE_CHARS yields (lineno,
    None) once and the rest of it is dropped, so a file with no newlines (or
    /dev/zero) cannot exhaust memory."""
    lineno = 0
    while True:
        chunk = fh.readline(MAX_LINE_CHARS + 1)
        if not chunk:
            return
        lineno += 1
        if len(chunk) > MAX_LINE_CHARS and not chunk.endswith("\n"):
            while True:  # discard the remainder of this oversize line
                rest = fh.readline(MAX_LINE_CHARS)
                if not rest or rest.endswith("\n"):
                    break
            yield lineno, None
            continue
        yield lineno, chunk


@main.command()
def demo():
    """Try agentfin on built-in SYNTHETIC data, in a throwaway ledger.

    Nothing is written to your own ledger or config; the temporary files are
    deleted when it finishes. Use it to see what the reports look like.
    """
    import contextlib
    import tempfile
    from importlib import resources

    src = resources.files("agentfin") / "demo_data" / "projects"
    with tempfile.TemporaryDirectory(prefix="agentfin-demo-") as td:
        common = ["--config-dir", str(Path(td) / "cfg")]
        with contextlib.redirect_stdout(io.StringIO()):
            main.main(args=[*common, "init", "--config-dir", str(Path(td) / "cfg")],
                      standalone_mode=False)
        with resources.as_file(src) as real:
            click.echo("Synthetic data (made up; not your usage). Your own ledger is untouched.\n")
            click.echo("$ agentfin ingest --adapter claude-code <demo folder>")
            main.main(args=[*common, "ingest", "--adapter", "claude-code", str(real)],
                      standalone_mode=False)
            click.echo("\n$ agentfin report --by task")
            main.main(args=[*common, "report", "--by", "task"], standalone_mode=False)
    click.echo("\nNext: `agentfin init`, then `agentfin ingest --adapter claude-code` "
               "to read your own Claude Code usage.")


def _parse_age(ctx, param, value):
    """click callback: '90m', '6h', '2d' -> seconds (None when not given)."""
    if value is None:
        return None
    m = re.fullmatch(r"\s*(\d{1,6})\s*([mhd])\s*", value.lower())
    if not m or int(m.group(1)) == 0:
        raise click.BadParameter("use a number and m, h or d, e.g. 90m, 6h or 2d")
    return int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)]


def _fresh_enough(path: str, max_age_s) -> bool:
    if max_age_s is None:
        return True
    try:
        return time.time() - os.path.getmtime(path) <= max_age_s
    except OSError:
        return False


def _expand_inputs(files, adapter, max_age_s=None) -> list[str]:
    """Files as given; directories expand to their *.jsonl files (sorted, no
    symlinked-directory traversal); `-` is stdin. With no inputs, the
    claude-code adapter discovers Claude Code's transcript folders itself."""
    if not files:
        if adapter != "claude-code":
            raise click.UsageError("give at least one FILE (or `-` for stdin)")
        roots = default_roots()
        if not roots:
            raise click.ClickException(
                "no Claude Code transcripts found in ~/.claude/projects or "
                "~/.config/claude/projects (set CLAUDE_CONFIG_DIR or pass a path)"
            )
        files = [str(r) for r in roots]
    out: list[str] = []
    for f in files:
        if f != "-" and os.path.isdir(f):
            found = []
            for root, dirs, names in os.walk(f, followlinks=False):
                dirs.sort()
                found.extend(
                    os.path.join(root, n) for n in sorted(names) if n.endswith(".jsonl")
                    and _fresh_enough(os.path.join(root, n), max_age_s)
                )
            out.extend(found)
        elif f != "-" and not os.path.isfile(f):
            raise click.ClickException(
                f"{textsafe.clean(f, 120)}: not a regular file or directory "
                "(device and pipe paths are refused; pipe data in with `-`)")
        else:
            out.append(f)
    return out


@main.command()
@click.argument("files", nargs=-1, type=click.Path(exists=True, allow_dash=True))
@click.option("--adapter", type=click.Choice(sorted(ADAPTERS)), default="jsonl",
              show_default=True)
@click.option("--no-redact", is_flag=True, help="disable PII redaction (not recommended)")
@click.option("--dry-run", is_flag=True,
              help="parse, price and count everything but write nothing to the ledger")
@click.option("--strict", is_flag=True,
              help="abort and write nothing if any line is malformed (default: skip "
                   "bad lines, report them with file:line, keep the good ones)")
@click.option("--modified-within", "max_age_s", default=None, callback=_parse_age,
              metavar="AGE",
              help="in folders, read only files changed in the last AGE (90m, 6h, 2d): "
                   "a fast re-run for a scheduled job or hook. Files you name are always read.")
@click.pass_context
def ingest(ctx, files, adapter, no_redact, dry_run, strict, max_age_s):
    """Ingest usage records into the ledger.

    FILES may be JSONL files, directories (all *.jsonl inside), or `-` for
    stdin. `--adapter claude-code` with no FILES reads Claude Code's own
    transcript folders.
    """
    paths = ctx.obj["paths"]
    config = ctx.obj["config"]
    sources = _expand_inputs(files, adapter, max_age_s)
    if adapter != "gateway" and not Path(paths["pricing"]).is_file():
        click.echo(f"⚠ no price book at {paths['pricing']}: every event will be UNPRICED. "
                   "Run `agentfin init` (with the same --config-dir) first.", err=True)
    try:
        price_book = pricing.load_pricing(paths["pricing"])
    except (ValueError, yaml.YAMLError) as exc:
        raise click.ClickException(f"pricing.yaml: {exc}") from None
    conn = ledger.connect(paths["db"])
    ictx = IngestContext(
        conn,
        price_book,
        attribution.load_rules(paths["rules"]),
        redact_keys=set(config.get("redact_keys") or []),
        no_redact=no_redact,
    )
    parser = ADAPTERS[adapter]
    errors: list[str] = []
    pending: dict[str, dict] = {}  # buffered adapters: best record per key, first-seen order
    unkeyed: list[dict] = []
    skipped = 0

    def fail(where: str, exc: Exception) -> None:
        errors.append(textsafe.clean(f"{where}: {_plain_error(exc)}", 300))
        if strict:
            conn.rollback()
            conn.close()
            raise click.ClickException(f"{errors[-1]} (--strict: nothing written)")

    def emit(where: str, event: dict) -> None:
        try:
            ictx.emit(event)
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
            fail(where, exc)

    for src in sources:
        fh = _open_text(src)
        label = "<stdin>" if src == "-" else src
        try:
            for lineno, line in _bounded_lines(fh):
                where = f"{label}:{lineno}"
                if line is None:
                    fail(where, ValueError(
                        f"line is longer than {MAX_LINE_CHARS // 1048576} MiB, skipped"))
                    continue
                line = line.strip().lstrip("\ufeff")  # BOMs from concatenated files
                if not line or line.startswith("#"):
                    continue
                try:
                    d = json.loads(line)
                    if not isinstance(d, dict):
                        raise ValueError(f"expected a JSON object, got {type(d).__name__}")
                    found = parser.parse_records(d)
                except (ValueError, TypeError, KeyError, AttributeError,
                        OverflowError, RecursionError) as exc:
                    fail(where, exc)
                    continue
                if not found:
                    skipped += 1
                for event in found:
                    if parser.buffered:
                        event["_where"] = where
                        key = event.get("idem_key")
                        if not key:
                            unkeyed.append(event)
                        elif key not in pending or parser.prefer(event, pending[key]):
                            pending[key] = event
                    else:
                        emit(where, event)
        finally:
            if src == "-":
                fh.detach()  # leave the process's stdin open
            else:
                fh.close()
    batch = parser.post_reduce([*pending.values(), *unkeyed]) if parser.buffered else []
    for event in batch:
        emit(event.pop("_where", "?"), event)
    aggregates = getattr(parser, "skipped_aggregates", 0)
    if aggregates:
        skipped += aggregates
        parser.skipped_aggregates = 0
    if dry_run:
        conn.rollback()  # nothing was committed: counts below are what WOULD happen
    else:
        ictx.commit()
    conn.close()
    click.echo(
        f"{'dry run, nothing written: would ingest' if dry_run else 'ingested'} "
        f"{ictx.inserted} events "
        f"({ictx.duplicates} duplicates skipped, {ictx.unpriced} unpriced"
        + (f", {ictx.revised} partial records revised" if ictx.revised else "")
        + (f", {skipped} non-usage lines ignored" if skipped else "") + ")"
    )
    if errors:
        click.echo(f"⚠ {len(errors)} malformed line(s) skipped:", err=True)
        for e in errors[:MAX_REPORTED_ERRORS]:
            click.echo(f"    {e}", err=True)
        if len(errors) > MAX_REPORTED_ERRORS:
            click.echo(f"    … and {len(errors) - MAX_REPORTED_ERRORS} more", err=True)


def _finite(ctx, param, value):
    """click callback: reject NaN / inf, which float() happily accepts."""
    if value is not None and (value != value or value in (float("inf"), float("-inf"))
                              or abs(value) > 1e12):
        raise click.BadParameter("must be a finite number below 1e12")
    return value


def _plain_error(exc: Exception) -> str:
    """Say what is wrong with a line in words, not as a Python exception name."""
    if isinstance(exc, RecursionError):
        return "JSON is nested too deeply"
    if isinstance(exc, OverflowError):
        return f"number out of range ({exc})"
    if isinstance(exc, KeyError):
        return f"missing field {exc}"
    if isinstance(exc, (ValueError, TypeError)):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def _require_db(ctx) -> None:
    """Gates must fail closed: a typo'd --db path would otherwise create an
    empty ledger and report 'all clear'."""
    db = ctx.obj["paths"]["db"]
    if db != ":memory:" and not Path(db).is_file():
        raise click.ClickException(
            f"ledger not found at {db}. Run `agentfin init` and then `agentfin ingest ...` "
            "first (or point --db / --config-dir at your existing ledger)"
        )


def _open(ctx):
    """Open an EXISTING ledger. Read/report commands never create one: a typo'd
    path or a forgotten `init` must not look like 'no spend'."""
    _require_db(ctx)
    return ledger.connect(ctx.obj["paths"]["db"])


def _money(ctx, micro):
    config = ctx.obj["config"]
    text, _ = _fx_convert(
        micro, config.get("display_currency", "USD"), config.get("fx")
    )
    return text


@main.command()
@click.option("--by", "by", default="team", show_default=True,
              type=click.Choice(sorted(reports.DIMENSIONS) + list(reports.BUCKETS)),
              help="a dimension, or day / week / month (cut in --tz) / session")
@click.option("--from", "date_from", default=None)
@click.option("--to", "date_to", default=None)
@click.option("--tz", default="UTC", show_default=True,
              help="time zone for --by day|week|month, e.g. America/Chicago")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def report(ctx, by, date_from, date_to, tz, as_json):
    """Cost report grouped by agent, task, team, model, provider, sku, day, week,
    month or session."""
    conn = _open(ctx)
    try:
        if by in reports.BUCKETS:
            rows = reports.summary_bucketed(conn, by, date_from, date_to, tz)
        else:
            rows = reports.summary(conn, by, date_from, date_to)
    except ValueError as exc:
        hint = "--tz" if "time zone" in str(exc) else "--from/--to"
        raise click.BadParameter(str(exc), param_hint=hint) from None
    finally:
        conn.close()
    if as_json:
        click.echo(json.dumps(rows, indent=2, default=str))
        return
    config = ctx.obj["config"]
    currency = (config.get("display_currency") or "USD").upper()
    click.echo(f"{'dim':<28}{'cost':>14}{'events':>10}  notes")
    if currency != "USD":
        click.echo(f"(costs converted to {currency}; * = FX estimate, not accounting truth)")
    click.echo("-" * 60)
    for r in rows:
        note = "UNPRICED events!" if r["unpriced_events"] else ""
        click.echo(
            f"{textsafe.clean(r['dim'], 27):<28}{_money(ctx, r['cost_micro_usd']):>14}"
            f"{r['events']:>10}  {note}"
        )


@main.command()
@click.option("--by", "dimension", type=click.Choice(sorted(reports.DIMENSIONS)),
              default=None, help="scope to one dimension value")
@click.option("--value", default=None, help="scope value, e.g. --by team --value platform")
@click.option("--days", default=30, type=int)
@click.pass_context
def trends(ctx, dimension, value, days):
    """Daily spend trend (ASCII bars)."""
    conn = _open(ctx)
    series = reports.daily_series(conn, dimension, value, days=days)
    conn.close()
    if not series:
        click.echo("no data")
        return
    peak = max(r["cost_micro_usd"] or 0 for r in series) or 1
    for r in series:
        bar = "█" * int(40 * (r["cost_micro_usd"] or 0) / peak)
        click.echo(f"{r['day']}  {bar:<40} {_money(ctx, r['cost_micro_usd'])}")


@main.command("anomalies")
@click.option("--by", "dimension", type=click.Choice(sorted(reports.DIMENSIONS)),
              default="agent", show_default=True)
@click.option("--days", default=45, type=int)
@click.option("--fail-on-anomaly", is_flag=True,
              help=f"exit with status {ALERT_EXIT_CODE} if any anomaly is found (CI/cron gate)")
@click.option("--json", "as_json", is_flag=True, help="machine-readable output")
@click.pass_context
def detect_anomalies(ctx, dimension, days, fail_on_anomaly, as_json):
    """Detect spend anomalies with probable cause."""
    if fail_on_anomaly:
        _require_db(ctx)
    conn = _open(ctx)
    # As a gate, ignore the 24h display cooldown so a CI retry fails again.
    alerts = anomalies.detect(conn, dimension=dimension, days=days,
                              respect_cooldown=not fail_on_anomaly)
    conn.close()
    if as_json:
        click.echo(json.dumps(alerts, indent=2, default=str))
        if alerts and fail_on_anomaly:
            ctx.exit(ALERT_EXIT_CODE)
        return
    if not alerts:
        click.echo("no anomalies detected")
        return
    for a in alerts:
        click.echo(
            f"⚠ {a['scope']} on {a['day']}: {_money(ctx, a['spend_micro_usd'])} "
            f"= {a['multiple']}x median baseline (z={a['z']})"
        )
        for c in a["probable_cause"][:3]:
            click.echo(f"    · {c['task']}: {_money(ctx, c['cost_micro_usd'])}")
    if fail_on_anomaly:
        ctx.exit(ALERT_EXIT_CODE)


@main.command("alerts")
@click.option("--dismiss", default=None, help="dismiss an alert by key")
@click.pass_context
def alerts_cmd(ctx, dismiss):
    """List (or dismiss) fired alerts."""
    conn = _open(ctx)
    if dismiss:
        ok = anomalies.dismiss(conn, dismiss)
        click.echo("dismissed" if ok else "no such alert")
    else:
        for a in anomalies.list_alerts(conn):
            flag = "dismissed" if a["dismissed"] else "ACTIVE"
            click.echo(f"[{flag}] {textsafe.clean(a['alert_key'])} fired {a['fired_at']}")
    conn.close()


@main.group("budgets", invoke_without_command=True)
@click.pass_context
def budgets_grp(ctx):
    """Budget management. Bare `budgets` shows status (same as `budgets check`)."""
    if ctx.invoked_subcommand is None:
        _budgets_check(ctx)


def _budget_cap(usd) -> str:
    """$500 for whole dollars, $0.20 for small caps (never a misleading "$0")."""
    usd = float(usd)
    return f"${usd:,.0f}" if usd == int(usd) else f"${usd:,.2f}"


def _budgets_check(ctx, fail_on_alert=False, as_json=False):
    """Show budget status with month-end forecast (alerts fire on forecast)."""
    if fail_on_alert:
        _require_db(ctx)
    blist = budgets.load_budgets(ctx.obj["paths"]["budgets"])
    bad = budgets.invalid_scopes(blist)
    for scope in bad:
        click.echo(f"⚠ invalid budget entry skipped: {scope} (scope must be "
                   "team:/agent:/task:<name> with a positive monthly_usd)", err=True)
    if fail_on_alert and bad:
        raise click.ClickException("budgets.yaml has invalid entries (see above)")
    conn = _open(ctx)
    # check() is idempotent and persists alert state.
    results = budgets.check(conn, blist)
    conn.close()
    if not results:
        if fail_on_alert:
            raise click.ClickException(
                "no budgets configured — a budget gate with nothing to check cannot pass"
            )
        if as_json:
            click.echo("[]")
            return
        click.echo("no budgets configured (see budgets.yaml, or `budgets set`)")
        return
    if as_json:
        click.echo(json.dumps(results, default=str, indent=2))
        if fail_on_alert and any(r["alert"] or r.get("unpriced_events") for r in results):
            ctx.exit(ALERT_EXIT_CODE)
        return
    for r in results:
        status = "🔴 ALERT: forecast over budget" if r["alert"] else "🟢 ok"
        lowconf = " (low confidence: <3 days of data)" if r.get(
            "forecast_low_confidence"
        ) else ""
        unpriced = (f" ⚠ +{r['unpriced_events']} UNPRICED events (true spend unknown)"
                    if r.get("unpriced_events") else "")
        click.echo(
            f"{r['scope']}: spent {_money(ctx, r['spent_micro_usd'])} / "
            f"{_budget_cap(r['monthly_usd'])} budget · forecast "
            f"{_money(ctx, r['forecast_micro_usd'])} "
            f"({r['forecast_pct_of_budget']}%){lowconf} {status}{unpriced}"
        )
    if fail_on_alert and any(r["alert"] or r.get("unpriced_events") for r in results):
        ctx.exit(ALERT_EXIT_CODE)


@budgets_grp.command("check")
@click.option("--fail-on-alert", is_flag=True,
              help=f"exit with status {ALERT_EXIT_CODE} if any budget is forecast to "
                   "overrun (CI/cron gate)")
@click.option("--json", "as_json", is_flag=True, help="machine-readable output")
@click.pass_context
def budgets_check_cmd(ctx, fail_on_alert, as_json):
    """Show budget status with month-end forecast."""
    _budgets_check(ctx, fail_on_alert, as_json)


@budgets_grp.command("set")
@click.argument("scope")
@click.argument("monthly_usd", type=float, callback=_finite)
@click.option("--alert-at", "alert_at", type=float, default=0.8, show_default=True,
              callback=_finite,
              help="fraction of forecasted month-end spend that triggers an alert")
@click.pass_context
def budgets_set_cmd(ctx, scope, monthly_usd, alert_at):
    """Set a monthly budget cap, e.g. `budgets set team:platform 500`.

    SCOPE looks like team:platform, agent:worker-1, or task:nightly-eval.
    """
    dim, sep, value = scope.partition(":")
    if not sep or not value or dim not in ("team", "agent", "task"):
        raise click.BadParameter(
            "scope must look like team:platform, agent:worker-1, or task:nightly-eval"
        )
    if monthly_usd <= 0:
        raise click.BadParameter("monthly_usd must be positive")
    if not 0 < alert_at <= 1:
        raise click.BadParameter("--alert-at must be in (0, 1]")
    paths = ctx.obj["paths"]
    blist = budgets.load_budgets(paths["budgets"])
    entry = {"scope": scope, "monthly_usd": monthly_usd, "alert_at": alert_at}
    for i, b in enumerate(blist):
        if b.get("scope") == scope:
            blist[i] = entry
            break
    else:
        blist.append(entry)
    budgets.save_budgets(paths["budgets"], blist)
    click.echo(
        f"set budget {scope}: ${monthly_usd:,.2f}/mo, "
        f"alert at {alert_at:.0%} of forecasted spend"
    )


@main.group("outcomes", invoke_without_command=True)
@click.pass_context
def outcomes_grp(ctx):
    """Outcome tagging and cost-per-outcome ROI. Bare `outcomes` shows ROI."""
    if ctx.invoked_subcommand is None:
        _roi_report(ctx)


def _tag_outcome(ctx, task, kind, value, note):
    conn = _open(ctx)
    outcomes.tag_outcome(conn, task, kind, value, note)
    conn.close()
    click.echo(f"tagged outcome on task '{task}': {kind}={value}")


def _roi_report(ctx):
    conn = _open(ctx)
    rows = outcomes.roi_report(conn)
    conn.close()
    if not rows:
        click.echo("no cost or outcome data yet")
        return
    click.echo(f"{'task':<28}{'cost':>12}{'value':>12}{'roi':>10}")
    click.echo("-" * 64)
    for r in rows:
        roi_s = "∞" if r["roi"] == float("inf") else f"{r['roi']:.2f}x"
        click.echo(
            f"{r['task']:<28}{_money(ctx, r['cost_micro_usd']):>12}"
            f"{_money(ctx, r['value_micro_usd']):>12}{roi_s:>10}"
        )


@outcomes_grp.command("tag")
@click.argument("task")
@click.option("--kind", type=click.Choice(["revenue", "hours_saved", "tickets", "custom"]),
              default="revenue")
@click.option("--value", type=float, required=True, callback=_finite,
              help="USD for revenue/custom; units for hours_saved/tickets")
@click.option("--note", default="")
@click.pass_context
def outcomes_tag_cmd(ctx, task, kind, value, note):
    """Tag a task with a monetized business outcome."""
    _tag_outcome(ctx, task, kind, value, note)


@outcomes_grp.command("roi")
@click.pass_context
def outcomes_roi_cmd(ctx):
    """Cost-per-outcome and ROI per task."""
    _roi_report(ctx)


# Backwards-compatible aliases for the pre-group command names.
@main.command("outcome", hidden=True)
@click.argument("task")
@click.option("--kind", type=click.Choice(["revenue", "hours_saved", "tickets", "custom"]),
              default="revenue")
@click.option("--value", type=float, required=True, callback=_finite,
              help="USD for revenue/custom; units for hours_saved/tickets")
@click.option("--note", default="")
@click.pass_context
def outcome_compat_cmd(ctx, task, kind, value, note):
    """Tag a task with a monetized business outcome (alias: outcomes tag)."""
    _tag_outcome(ctx, task, kind, value, note)


@main.command("roi", hidden=True)
@click.pass_context
def roi_compat_cmd(ctx):
    """Cost-per-outcome and ROI per task (alias: outcomes roi)."""
    _roi_report(ctx)


@main.command()
@click.argument("invoice_csv", type=click.Path(exists=True, dir_okay=False))
@click.option("--from", "date_from", default=None, help="invoice period start (YYYY-MM-DD)")
@click.option("--to", "date_to", default=None, help="invoice period end, inclusive")
@click.option("--tolerance", default=0.01, type=float, show_default=True,
              help="USD difference per SKU still called a match")
@click.option("--fail-on-variance", is_flag=True,
              help="exit 3 if any SKU differs by more than the tolerance (cron/CI)")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def reconcile_cmd(ctx, invoice_csv, date_from, date_to, tolerance, fail_on_variance,
                  as_json):
    """Reconcile ledger totals against a provider invoice CSV.

    The CSV needs a SKU column (`sku` or `model`, as provider:model) and a USD
    cost column (`cost_usd`, `amount_usd`, `cost` or `amount`). Pass --from/--to
    for the invoice period, otherwise the WHOLE ledger is compared.
    """
    if not (tolerance >= 0 and tolerance < 1e9):
        raise click.BadParameter("must be a number >= 0", param_hint="--tolerance")
    if fail_on_variance:
        _require_db(ctx)
    conn = _open(ctx)
    try:
        result = reconcile.reconcile(conn, invoice_csv, date_from, date_to, tolerance)
    except ValueError as exc:
        raise click.ClickException(textsafe.clean(str(exc), 400)) from None
    finally:
        conn.close()
    if as_json:
        click.echo(json.dumps(result, indent=2))
    else:
        if not (date_from or date_to):
            click.echo("note: no --from/--to given, comparing the whole ledger")
        for line in result["lines"]:
            flag = "ok " if line["match"] else "XX VARIANCE"
            extra = f" [{line['note']}]" if line["note"] else ""
            if line["unpriced_events"]:
                extra += f" [{line['unpriced_events']} UNPRICED events]"
            click.echo(
                f"{flag} {line['sku']}: ledger ${line['ledger_usd']:,.4f} vs "
                f"invoiced ${line['invoiced_usd']:,.4f} "
                f"(diff ${line['variance_usd']:,.4f}){extra}"
            )
        click.echo(f"total variance: ${result['total_variance_usd']:,.4f}")
    if fail_on_variance and not result["all_match"]:
        ctx.exit(3)


@main.command()
@click.option("--out", default="agentfin-dashboard.html")
@click.option("--days", default=30, type=int)
@click.pass_context
def dashboard_cmd(ctx, out, days):
    """Export a static HTML dashboard."""
    conn = _open(ctx)
    blist = budgets.load_budgets(ctx.obj["paths"]["budgets"])
    html_doc = dashboard.generate(conn, blist, days=days)
    conn.close()
    Path(out).write_text(html_doc, encoding="utf-8")
    click.echo(f"wrote {out}")


@main.command("export")
@click.option("--format", "fmt", type=click.Choice(["focus", "csv", "jsonl"]),
              default="focus", show_default=True,
              help="focus = FOCUS-aligned daily cost rows; csv/jsonl = raw ledger events")
@click.option("--out", default="-", show_default=True, help="output file, or - for stdout")
@click.option("--from", "date_from", default=None, help="YYYY-MM-DD or ISO timestamp")
@click.option("--to", "date_to", default=None, help="YYYY-MM-DD (inclusive) or ISO timestamp")
@click.pass_context
def export_cmd(ctx, fmt, out, date_from, date_to):
    """Export the ledger for spreadsheets, BI or FinOps tools (FOCUS)."""
    try:
        reports.time_where(date_from, date_to)  # validate before opening ledger/--out
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--from/--to") from None
    conn = _open(ctx)
    fh = sys.stdout if out == "-" else open(out, "w", encoding="utf-8", newline="")
    try:
        unpriced_rows = 0
        if fmt == "focus":
            def counted(rows):
                nonlocal unpriced_rows
                for row in rows:
                    unpriced_rows += row["x_Unpriced"] == "true"
                    yield row
            cfg = ctx.obj["config"]
            n = export.write_csv(
                counted(export.focus_rows(
                    conn, date_from, date_to,
                    billing_account_id=textsafe.clean(cfg.get("billing_account_id") or ""),
                    billing_account_name=textsafe.clean(cfg.get("billing_account_name") or ""),
                )),
                export.FOCUS_COLUMNS, fh)
        elif fmt == "csv":
            n = export.write_csv(export.event_rows(conn, date_from, date_to),
                                 export.EVENT_COLUMNS, fh)
        else:
            n = export.write_jsonl(export.event_rows(conn, date_from, date_to), fh)
    except ValueError as exc:
        raise click.ClickException(textsafe.clean(str(exc), 400)) from None
    finally:
        if out != "-":
            fh.close()
        conn.close()
    if out != "-":
        click.echo(f"wrote {n} {fmt} rows to {out}")
    if unpriced_rows:
        click.echo(f"⚠ {unpriced_rows} FOCUS row(s) contain UNPRICED events: cost written "
                   "as 0 with x_Unpriced=true. Add the SKUs to pricing.yaml and run "
                   "`agentfin reprice`.", err=True)


@main.command()
@click.pass_context
def reprice(ctx):
    """Fill NULL (unpriced) costs using current pricing.yaml rates.

    Only touches events that were ingested before their SKU had a price —
    priced events are never rewritten, preserving ledger immutability."""
    paths = ctx.obj["paths"]
    try:
        rates = pricing.load_pricing(paths["pricing"])
    except (ValueError, yaml.YAMLError) as exc:
        raise click.ClickException(f"pricing.yaml: {exc}") from None
    conn = _open(ctx)
    rows = conn.execute(
        """SELECT id, sku, input_tokens, cache_read_tokens, cache_write_tokens,
                  output_tokens, is_correction, metadata_json FROM usage_events
           WHERE cost_micro_usd IS NULL"""
    ).fetchall()
    fixed = 0
    for r in rows:
        ev = dict(r)
        try:
            meta = json.loads(r["metadata_json"] or "{}")
            ev["cache_write_1h_tokens"] = int(meta.get("cache_write_1h_tokens") or 0)
            ev["service_tier"] = meta.get("service_tier")
        except (ValueError, TypeError, AttributeError):
            pass
        cost = pricing.cost_micro_usd(r["sku"], rates, ev)
        if cost is not None:
            conn.execute(
                "UPDATE usage_events SET cost_micro_usd = ? WHERE id = ?",
                (cost, r["id"]),
            )
            fixed += 1
    conn.commit()
    conn.close()
    click.echo(f"repriced {fixed} of {len(rows)} unpriced events")


def _doctor_data(ctx, conn) -> dict:
    """Everything `doctor` checks, as plain data (text and --json both render this)."""
    tot = reports.totals(conn)
    unpriced = conn.execute(
        "SELECT sku, COUNT(*) c FROM usage_events WHERE cost_micro_usd IS NULL "
        "GROUP BY sku ORDER BY c DESC"
    ).fetchall()
    unattributed = conn.execute(
        "SELECT COUNT(*) c FROM usage_events WHERE team = 'unassigned'"
    ).fetchone()["c"]
    span = conn.execute(
        "SELECT MIN(ts_utc) mn, MAX(ts_utc) mx FROM usage_events"
    ).fetchone()
    try:
        fuzzy = conn.execute(
            """SELECT sku, json_extract(metadata_json, '$.priced_as') AS priced_as,
                      COUNT(*) c FROM usage_events
               WHERE json_extract(metadata_json, '$.priced_as') IS NOT NULL
               GROUP BY sku, priced_as ORDER BY c DESC"""
        ).fetchall()
    except sqlite3.OperationalError:  # SQLite built without JSON1
        fuzzy = []
    try:
        tiers = conn.execute(
            """SELECT json_extract(metadata_json, '$.service_tier') AS tier, COUNT(*) c
               FROM usage_events WHERE json_extract(metadata_json, '$.service_tier')
               IS NOT NULL GROUP BY tier"""
        ).fetchall()
    except sqlite3.OperationalError:
        tiers = []
    try:
        mods = pricing.load_pricing(ctx.obj["paths"]["pricing"]).modifiers
    except (ValueError, yaml.YAMLError):
        mods = {}
    unmodified = [
        {"tier": textsafe.clean(r["tier"], 32), "events": r["c"]} for r in tiers
        if r["tier"] not in ("standard", "default", "auto") and r["tier"] not in mods
    ]
    # Cross-source double-ingest heuristic: identical token/timestamp/SKU
    # rows under different idempotency keys (e.g. gateway file + provider
    # export covering the same calls) would double-count spend.
    dupes = conn.execute(
        """SELECT ts_utc, sku, input_tokens, output_tokens, COUNT(*) c
           FROM usage_events
           GROUP BY ts_utc, sku, input_tokens, output_tokens,
                    cache_read_tokens, cache_write_tokens
           HAVING c > 1"""
    ).fetchall()
    odd = conn.execute(
        "SELECT COUNT(*) c FROM usage_events WHERE ts_utc < ? OR ts_utc > ?",
        (ledger.MIN_TS, ledger._clock()[1]),
    ).fetchone()["c"]
    try:
        age_days = (time.time() - os.path.getmtime(ctx.obj["paths"]["pricing"])) / 86400
    except OSError:
        age_days = 0
    try:
        mine = pricing.load_pricing(ctx.obj["paths"]["pricing"])
        shipped = yaml.safe_load(pricing.DEFAULT_PRICING)
        newer = sorted(k for k in shipped.get("skus", {}) if k not in mine
                       and k not in mine.aliases)
    except (ValueError, yaml.YAMLError, OSError):
        newer = []
    return {
        "events": tot["events"],
        "first_event": span["mn"],
        "last_event": span["mx"],
        "unpriced_events": tot["unpriced_events"] or 0,
        "unpriced_skus": [{"sku": textsafe.clean(r["sku"]), "events": r["c"]} for r in unpriced],
        "unattributed_events": unattributed,
        "priced_via_normalization": [
            {"sku": textsafe.clean(r["sku"]), "priced_as": textsafe.clean(r["priced_as"]),
             "events": r["c"]} for r in fuzzy],
        "service_tiers_without_modifier": unmodified,
        "possible_duplicate_groups": len(dupes),
        "events_with_implausible_dates": odd,
        "pricing_file_age_days": int(age_days),
        "pricing_file_stale": age_days > PRICE_BOOK_STALE_DAYS,
        "example_skus_missing_from_pricing_file": newer,
    }


@main.command()
@click.option("--json", "as_json", is_flag=True, help="machine-readable output")
@click.pass_context
def doctor(ctx, as_json):
    """Sanity checks: unpriced SKUs, unattributed events, data coverage."""
    conn = _open(ctx)
    d = _doctor_data(ctx, conn)
    conn.close()
    if as_json:
        click.echo(json.dumps(d, indent=2))
        return
    click.echo(f"events: {d['events']:,}  span: {d['first_event']} → {d['last_event']}")
    click.echo(f"unpriced events: {d['unpriced_events']:,}")
    for r in d["unpriced_skus"]:
        click.echo(f"  · {r['sku']}: {r['events']:,} events (add to pricing.yaml)")
    click.echo(f"unattributed (team=unassigned): {d['unattributed_events']:,}")
    if d["priced_via_normalization"]:
        click.echo("priced via normalization/alias (check these are right):")
        for r in d["priced_via_normalization"]:
            click.echo(f"  · {r['sku']} → {r['priced_as']}: {r['events']:,} events")
    for r in d["service_tiers_without_modifier"]:
        click.echo(
            f"⚠ {r['events']:,} events used service tier {r['tier']!r} with no "
            "`modifiers:` entry in pricing.yaml: priced at the base rate "
            "(batch/flex/priority prices differ; add the multiplier from the provider's "
            "pricing page, then re-ingest into a fresh ledger)")
    if d["possible_duplicate_groups"]:
        click.echo(
            f"⚠ {d['possible_duplicate_groups']} groups of near-identical events under "
            "different idempotency keys — possible cross-source double-ingest. "
            "Ingest each source exactly once."
        )
    if d["events_with_implausible_dates"]:
        click.echo(f"⚠ {d['events_with_implausible_dates']:,} events are dated before 2015 "
                   "or in the future (written by an older version); they distort daily and "
                   "monthly reports. Find them with `agentfin export --format csv`.")
    if d["pricing_file_stale"]:
        click.echo(
            f"⚠ pricing.yaml was last modified {d['pricing_file_age_days']} days ago. "
            "Provider prices change; compare it with the provider's pricing page and "
            "reconcile to your latest invoice (`agentfin reconcile`). (File age only: "
            "agentfin cannot tell whether the rates are still current.)")
    if d["example_skus_missing_from_pricing_file"]:
        miss = d["example_skus_missing_from_pricing_file"]
        click.echo(
            f"note: this version's example price book lists {len(miss)} SKU(s) your "
            f"pricing.yaml lacks (e.g. {', '.join(miss[:3])}). Your file is never changed "
            "automatically; see them with `agentfin pricing-template` and copy what you "
            "need after checking the provider's pricing page.")
    if d["unpriced_events"]:
        click.echo("tip: add the SKUs to pricing.yaml, then run `agentfin reprice` "
                   "to fill NULL costs from the new rates.")


@main.command("pricing-template")
def pricing_template_cmd():
    """Print this version's example pricing.yaml (to compare with your own file)."""
    click.echo(pricing.DEFAULT_PRICING, nl=False)


@main.command()
@click.option("--json", "as_json", is_flag=True, help="machine-readable output")
@click.option("--oneline", is_flag=True,
              help="one short line (today / month to date), e.g. for a status bar")
@click.pass_context
def status(ctx, as_json, oneline):
    """One-screen summary: ledger, event count, latest event, month to date."""
    _require_db(ctx)
    # Often run from a status bar: never hang for the usual 30 s if a writer is busy.
    conn = ledger.connect(ctx.obj["paths"]["db"], timeout=3)
    now = datetime.now(timezone.utc)
    today, month_start = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-01")
    total = reports.totals(conn)
    t_today = reports.totals(conn, today, today)
    t_month = reports.totals(conn, month_start, today)
    last = conn.execute("SELECT MAX(ts_utc) m FROM usage_events").fetchone()["m"]
    closed = conn.execute(
        "SELECT month FROM closes ORDER BY month DESC, version DESC LIMIT 1").fetchone()
    conn.close()
    d = {
        "ledger": str(ctx.obj["paths"]["db"]),
        "events": total["events"],
        "last_event": last,
        "total_cost_micro_usd": total["cost_micro_usd"] or 0,
        "today_cost_micro_usd": t_today["cost_micro_usd"] or 0,
        "month_to_date_cost_micro_usd": t_month["cost_micro_usd"] or 0,
        "unpriced_events": total["unpriced_events"] or 0,
        "latest_closed_month": closed["month"] if closed else None,
        "as_of_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if as_json:
        click.echo(json.dumps(d, indent=2))
        return
    flag = f" ⚠ {d['unpriced_events']} unpriced" if d["unpriced_events"] else ""
    if oneline:
        click.echo(f"agentfin {_money(ctx, d['today_cost_micro_usd'])} today · "
                   f"{_money(ctx, d['month_to_date_cost_micro_usd'])} this month{flag}")
        return
    click.echo(f"ledger:        {textsafe.clean(d['ledger'], 200)}")
    click.echo(f"events:        {d['events']:,}  (latest: {d['last_event'] or 'none'})")
    click.echo(f"today (UTC):   {_money(ctx, d['today_cost_micro_usd'])}")
    click.echo(f"month to date: {_money(ctx, d['month_to_date_cost_micro_usd'])}")
    click.echo(f"all time:      {_money(ctx, d['total_cost_micro_usd'])}{flag}")
    click.echo(f"last closed:   {d['latest_closed_month'] or 'none'}")


# ---------------------------------------------------------------------------
# Month-end close, showback, savings, git outcomes
# ---------------------------------------------------------------------------


def _fail(exc: Exception):
    raise click.ClickException(textsafe.clean(exc, 400)) from None


@main.group("close")
def close_grp():
    """Month-end close: freeze a month, detect later drift, write a journal."""


@close_grp.command("run")
@click.argument("month")
@click.option("--tz", default="UTC", show_default=True,
              help="IANA time zone that defines the month, e.g. America/Chicago")
@click.option("--fx", "fx_pairs", multiple=True,
              help="FX rate to record, units per 1 USD, e.g. --fx CAD=1.37 (repeatable)")
@click.option("--allow-unpriced", is_flag=True,
              help="close even though some events have no price (they are excluded)")
@click.option("--supersede", is_flag=True, help="record a new version of an existing close")
@click.option("--note", default="")
@click.pass_context
def close_run_cmd(ctx, month, tz, fx_pairs, allow_unpriced, supersede, note):
    """Close MONTH (YYYY-MM): store totals, a digest and the FX rates you give."""
    _require_db(ctx)
    conn = _open(ctx)
    try:
        rec = close_mod.run_close(
            conn, month, tz, close_mod.parse_fx(fx_pairs), ctx.obj["paths"]["pricing"],
            allow_unpriced, note, supersede)
    except close_mod.CloseError as exc:
        conn.close()
        _fail(exc)
    conn.close()
    click.echo(
        f"closed {rec['month']} v{rec['version']} ({rec['tz']}): {rec['events']:,} rows, "
        f"{pricing.format_usd(rec['cost_micro_usd'])} USD, digest {rec['digest'][:16]}"
        + (f", {rec['unpriced_events']} unpriced excluded" if rec["unpriced_events"] else ""))


@close_grp.command("verify")
@click.argument("month")
@click.option("--json", "as_json", is_flag=True, help="machine-readable output")
@click.pass_context
def close_verify_cmd(ctx, month, as_json):
    """Check a closed month still matches the ledger (exit 3 if it drifted)."""
    _require_db(ctx)
    conn = _open(ctx)
    try:
        v = close_mod.verify(conn, month)
    except close_mod.CloseError as exc:
        conn.close()
        _fail(exc)
    conn.close()
    if as_json:
        click.echo(json.dumps(v, indent=2, default=str))
        if not v["ok"]:
            ctx.exit(ALERT_EXIT_CODE)
        return
    if v["ok"]:
        click.echo(f"{month} v{v['version']}: OK - matches the close of {v['closed_at']}")
        return
    click.echo(f"{month} v{v['version']}: DRIFT since the close of {v['closed_at']}")
    if v["late_events"]:
        click.echo(f"  {v['late_events']} late event(s) in the month, "
                   f"{pricing.format_usd(v['late_cost_micro_usd'])} USD"
                   + (f" ({v['late_unpriced']} unpriced)" if v["late_unpriced"] else ""))
    if v["altered"]:
        click.echo(f"  closed rows changed: {v['rows_at_close']} at close, "
                   f"{v['rows_now']} now; cost {pricing.format_usd(v['closed_cost_micro_usd'])} -> "
                   f"{pricing.format_usd(v['cost_now_micro_usd'])} (repriced, edited or deleted)")
    click.echo(f"  fix: agentfin close run {month} --supersede")
    ctx.exit(ALERT_EXIT_CODE)


@close_grp.command("journal")
@click.argument("month")
@click.option("--currency", default="USD", show_default=True,
              help="amounts in this currency, using the FX rate recorded at close")
@click.option("--out", default="-", show_default=True)
@click.pass_context
def close_journal_cmd(ctx, month, currency, out):
    """Write a balanced journal CSV (debit per team, credit to accrual).

    Account names come from config.yaml: gl_accounts (team -> account),
    gl_expense_default, gl_accrual. Column names resemble common journal-import
    templates but have not been tested against any accounting package."""
    _require_db(ctx)
    conn = _open(ctx)
    try:
        rows = close_mod.journal_rows(conn, month, ctx.obj["config"], currency)
    except (close_mod.CloseError, ValueError) as exc:
        conn.close()
        _fail(exc)
    conn.close()
    fh = sys.stdout if out == "-" else open(out, "w", encoding="utf-8", newline="")
    try:
        close_mod.write_journal(rows, fh)
    finally:
        if out != "-":
            fh.close()
    if out != "-":
        click.echo(f"wrote {len(rows)} journal lines to {out}")


@main.command("allocate")
@click.option("--by", "by", type=click.Choice(sorted(reports.DIMENSIONS)), default="team",
              show_default=True)
@click.option("--pool", default="unassigned", show_default=True,
              help="the value whose spend is shared out (e.g. unassigned)")
@click.option("--from", "date_from", default=None)
@click.option("--to", "date_to", default=None)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def allocate_cmd(ctx, by, pool, date_from, date_to, as_json):
    """Showback: share unattributed spend across teams in proportion to their own."""
    _require_db(ctx)
    conn = _open(ctx)
    try:
        res = allocate_mod.allocate(conn, date_from, date_to, pool, by)
    except ValueError as exc:
        conn.close()
        _fail(exc)
    conn.close()
    if as_json:
        click.echo(json.dumps(res, indent=2))
        return
    click.echo(f"{by:<24}{'direct':>12}{'shared':>12}{'loaded':>12}")
    click.echo("-" * 60)
    for r in res["rows"]:
        click.echo(f"{textsafe.clean(r[by], 23):<24}{_money(ctx, r['direct_micro_usd']):>12}"
                   f"{_money(ctx, r['allocated_micro_usd']):>12}"
                   f"{_money(ctx, r['loaded_micro_usd']):>12}")
    click.echo(f"{'total':<24}{'':>12}{_money(ctx, res['pool_micro_usd']):>12}"
               f"{_money(ctx, res['total_micro_usd']):>12}   (shared pool: {pool})")
    if res["unpriced_events"]:
        click.echo(f"⚠ {res['unpriced_events']} UNPRICED events are not in these totals", err=True)


@main.command("savings")
@click.option("--by", "by", type=click.Choice(sorted(reports.DIMENSIONS)), default="sku",
              show_default=True)
@click.option("--from", "date_from", default=None)
@click.option("--to", "date_to", default=None)
@click.pass_context
def savings_cmd(ctx, by, date_from, date_to):
    """What prompt caching saved: actual cost vs the same tokens without a cache."""
    _require_db(ctx)
    book = pricing.load_pricing(ctx.obj["paths"]["pricing"])
    conn = _open(ctx)
    try:
        res = whatif.cache_savings(conn, book, date_from, date_to, by)
    except ValueError as exc:
        conn.close()
        _fail(exc)
    conn.close()
    click.echo(f"{by:<28}{'actual':>12}{'no cache':>12}{'saved':>12}")
    click.echo("-" * 64)
    for r in res["rows"]:
        click.echo(f"{textsafe.clean(r[by], 27):<28}{_money(ctx, r['actual_micro_usd']):>12}"
                   f"{_money(ctx, r['without_cache_micro_usd']):>12}"
                   f"{_money(ctx, r['saved_micro_usd']):>12}")
    if res["skipped_events"]:
        click.echo(f"({res['skipped_events']} unpriced events left out)", err=True)


@main.command("whatif")
@click.option("--swap", "swaps", multiple=True, required=True,
              help="SKU=SKU, e.g. anthropic:claude-opus-5=anthropic:claude-sonnet-5")
@click.option("--by", "by", type=click.Choice(sorted(reports.DIMENSIONS)), default="task",
              show_default=True)
@click.option("--from", "date_from", default=None)
@click.option("--to", "date_to", default=None)
@click.pass_context
def whatif_cmd(ctx, swaps, by, date_from, date_to):
    """Price the same tokens on another SKU (a comparison, not a forecast)."""
    _require_db(ctx)
    mapping = {}
    for sw in swaps:
        a, sep, b = sw.partition("=")
        if not sep or not a or not b:
            raise click.BadParameter(f"{sw!r}: use FROM_SKU=TO_SKU", param_hint="--swap")
        mapping[a] = b
    book = pricing.load_pricing(ctx.obj["paths"]["pricing"])
    unknown = [b for b in mapping.values() if pricing.resolve(b, book)[1] is None]
    if unknown:
        raise click.ClickException(f"no price for {', '.join(unknown)} in pricing.yaml")
    conn = _open(ctx)
    try:
        res = whatif.model_swap(conn, book, mapping, date_from, date_to, by)
    except ValueError as exc:
        conn.close()
        _fail(exc)
    conn.close()
    click.echo("HYPOTHETICAL: same tokens, other SKU's list prices. Quality, retries and token "
               "counts would differ.")
    click.echo(f"{by:<28}{'actual':>12}{'swapped':>12}{'delta':>12}")
    click.echo("-" * 64)
    for r in res["rows"]:
        click.echo(f"{textsafe.clean(r[by], 27):<28}{_money(ctx, r['actual_micro_usd']):>12}"
                   f"{_money(ctx, r['swapped_micro_usd']):>12}"
                   f"{_money(ctx, r['delta_micro_usd']):>12}")
    if res["skipped_events"]:
        click.echo(f"({res['skipped_events']} unpriced events left out)", err=True)


@outcomes_grp.command("git")
@click.option("--repo", default=".", show_default=True,
              type=click.Path(exists=True, file_okay=False))
@click.option("--base", default=None, help="base branch (default: main/master/trunk/develop)")
@click.option("--merged-list", type=click.Path(exists=True, dir_okay=False), default=None,
              help="file of merged branch names, one per line (e.g. from `gh pr list`)")
@click.option("--project", default=None, help="only events from this project (cwd folder name)")
@click.option("--from", "date_from", default=None)
@click.option("--to", "date_to", default=None)
@click.pass_context
def outcomes_git_cmd(ctx, repo, base, merged_list, project, date_from, date_to):
    """Cost per merged branch, and spend on branches that never merged."""
    _require_db(ctx)
    extra = None
    if merged_list:
        extra = {textsafe.clean(line) for line in
                 Path(merged_list).read_text(encoding="utf-8", errors="replace").splitlines()
                 if line.strip() and not line.startswith("#")}
    conn = _open(ctx)
    try:
        res = gitlink.analyze(conn, repo, base, extra, date_from, date_to, project)
    except (gitlink.GitError, ValueError) as exc:
        conn.close()
        _fail(exc)
    conn.close()
    click.echo(f"{'branch':<40}{'status':<10}{'cost':>12}{'commits':>9}{'lines':>8}")
    click.echo("-" * 79)
    for r in res["rows"]:
        click.echo(f"{r['branch'][:39]:<40}{r['status']:<10}"
                   f"{_money(ctx, r['cost_micro_usd']):>12}"
                   f"{'' if r['commits'] is None else r['commits']:>9}"
                   f"{'' if r['lines_changed'] is None else r['lines_changed']:>8}")
    click.echo("-" * 79)
    per = res["cost_per_merged_branch_micro_usd"]
    click.echo(f"merged: {res['merged_branches']} branches, "
               f"{_money(ctx, res['merged_cost_micro_usd'])}"
               + (f" ({_money(ctx, per)} per merged branch)" if per is not None else ""))
    click.echo(f"never merged: {res['unmerged_branches']} branches, "
               f"{_money(ctx, res['unmerged_cost_micro_usd'])}")
    click.echo(f"on {res['base']} or no branch recorded (cannot be linked): "
               f"{_money(ctx, res['base_cost_micro_usd'] + res['unlinked_cost_micro_usd'])}")
    if res["unpriced_events"]:
        click.echo(f"⚠ {res['unpriced_events']} UNPRICED events are not in these costs", err=True)
