"""agentfin CLI."""

from __future__ import annotations

import json
from pathlib import Path

import click
import yaml

from . import (
    anomalies,
    attribution,
    budgets,
    dashboard,
    ledger,
    outcomes,
    pricing,
    reconcile,
    reports,
)
from .adapters.base import IngestContext
from .adapters.gateway import GatewayAdapter
from .adapters.jsonl import JsonlAdapter
from .adapters.provider import ProviderAdapter

ADAPTERS = {
    "jsonl": JsonlAdapter(),
    "provider": ProviderAdapter(),
    "gateway": GatewayAdapter(),
}

DEFAULT_DB = "agentfin.db"
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
        return yaml.safe_load(p.read_text()) or {}
    return {}


def _fx_convert(micro, currency, fx_rates) -> tuple[str, bool]:
    """Convert micro-USD to display currency. Returns (formatted, is_estimate)."""
    if micro is None:
        return "UNPRICED", False
    usd = micro / 1_000_000
    if not currency or currency.upper() == "USD":
        return f"${usd:,.2f}", False
    rate = (fx_rates or {}).get(currency.upper())
    if not rate:
        return f"${usd:,.2f} USD (no FX rate for {currency})", False
    return f"{usd * rate:,.2f} {currency.upper()}*", True


@click.group()
@click.option("--db", default=None, help="SQLite db path")
@click.option("--config-dir", default=None, help="Config dir (pricing/rules/budgets)")
@click.pass_context
def main(ctx, db, config_dir):
    ctx.ensure_object(dict)
    paths = _paths(db, config_dir)
    ctx.obj["paths"] = paths
    ctx.obj["config"] = _load_config(paths)


@main.command()
@click.option("--config-dir", default=str(DEFAULT_DIR))
@click.option("--force", is_flag=True,
              help="overwrite existing config files (default: refuse)")
def init(config_dir, force):
    """Scaffold config files and an empty ledger."""
    cfg = Path(config_dir)
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
        (cfg / name).write_text(content)
    conn = ledger.connect(cfg / DEFAULT_DB)
    conn.close()
    click.echo(f"initialized agentfin in {cfg}")


@main.command()
@click.argument("files", nargs=-1, required=True, type=click.Path(exists=True))
@click.option("--adapter", type=click.Choice(sorted(ADAPTERS)), default="jsonl")
@click.option("--no-redact", is_flag=True, help="disable PII redaction (not recommended)")
@click.pass_context
def ingest(ctx, files, adapter, no_redact):
    """Ingest usage records (JSONL files) into the ledger."""
    paths = ctx.obj["paths"]
    config = ctx.obj["config"]
    conn = ledger.connect(paths["db"])
    ictx = IngestContext(
        conn,
        pricing.load_pricing(paths["pricing"]),
        attribution.load_rules(paths["rules"]),
        redact_keys=set(config.get("redact_keys") or []),
        no_redact=no_redact,
    )
    for f in files:
        with open(f, encoding="utf-8") as fh:
            for event in ADAPTERS[adapter].parse(fh):
                ictx.emit(event)
    ictx.commit()
    conn.close()
    click.echo(
        f"ingested {ictx.inserted} events "
        f"({ictx.duplicates} duplicates skipped, {ictx.unpriced} unpriced)"
    )


def _money(ctx, micro):
    config = ctx.obj["config"]
    text, _ = _fx_convert(
        micro, config.get("display_currency", "USD"), config.get("fx")
    )
    return text


@main.command()
@click.option("--by", "by", type=click.Choice(sorted(reports.DIMENSIONS)), default="team")
@click.option("--from", "date_from", default=None)
@click.option("--to", "date_to", default=None)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def report(ctx, by, date_from, date_to, as_json):
    """Cost report grouped by agent, task, team, model, provider, or sku."""
    conn = ledger.connect(ctx.obj["paths"]["db"])
    rows = reports.summary(conn, by, date_from, date_to)
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
            f"{str(r['dim']):<28}{_money(ctx, r['cost_micro_usd']):>14}"
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
    conn = ledger.connect(ctx.obj["paths"]["db"])
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
@click.pass_context
def detect_anomalies(ctx, dimension, days):
    """Detect spend anomalies with probable cause."""
    conn = ledger.connect(ctx.obj["paths"]["db"])
    alerts = anomalies.detect(conn, dimension=dimension, days=days)
    conn.close()
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


@main.command("alerts")
@click.option("--dismiss", default=None, help="dismiss an alert by key")
@click.pass_context
def alerts_cmd(ctx, dismiss):
    """List (or dismiss) fired alerts."""
    conn = ledger.connect(ctx.obj["paths"]["db"])
    if dismiss:
        ok = anomalies.dismiss(conn, dismiss)
        click.echo("dismissed" if ok else "no such alert")
    else:
        for a in anomalies.list_alerts(conn):
            flag = "dismissed" if a["dismissed"] else "ACTIVE"
            click.echo(f"[{flag}] {a['alert_key']} fired {a['fired_at']}")
    conn.close()


@main.group("budgets", invoke_without_command=True)
@click.pass_context
def budgets_grp(ctx):
    """Budget management. Bare `budgets` shows status (same as `budgets check`)."""
    if ctx.invoked_subcommand is None:
        _budgets_check(ctx)


def _budgets_check(ctx):
    """Show budget status with month-end forecast (alerts fire on forecast)."""
    conn = ledger.connect(ctx.obj["paths"]["db"])
    blist = budgets.load_budgets(ctx.obj["paths"]["budgets"])
    # check() is idempotent and persists alert state.
    results = budgets.check(conn, blist)
    conn.close()
    if not results:
        click.echo("no budgets configured (see budgets.yaml, or `budgets set`)")
        return
    for r in results:
        status = "🔴 ALERT: forecast over budget" if r["alert"] else "🟢 ok"
        lowconf = " (low confidence: <3 days of data)" if r.get(
            "forecast_low_confidence"
        ) else ""
        click.echo(
            f"{r['scope']}: spent {_money(ctx, r['spent_micro_usd'])} / "
            f"${r['monthly_usd']:,.0f} budget · forecast "
            f"{_money(ctx, r['forecast_micro_usd'])} "
            f"({r['forecast_pct_of_budget']}%){lowconf} {status}"
        )


@budgets_grp.command("check")
@click.pass_context
def budgets_check_cmd(ctx):
    """Show budget status with month-end forecast."""
    _budgets_check(ctx)


@budgets_grp.command("set")
@click.argument("scope")
@click.argument("monthly_usd", type=float)
@click.option("--alert-at", "alert_at", type=float, default=0.8, show_default=True,
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
    conn = ledger.connect(ctx.obj["paths"]["db"])
    outcomes.tag_outcome(conn, task, kind, value, note)
    conn.close()
    click.echo(f"tagged outcome on task '{task}': {kind}={value}")


def _roi_report(ctx):
    conn = ledger.connect(ctx.obj["paths"]["db"])
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
@click.option("--value", type=float, required=True,
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
@click.option("--value", type=float, required=True,
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
@click.argument("invoice_csv", type=click.Path(exists=True))
@click.pass_context
def reconcile_cmd(ctx, invoice_csv):
    """Reconcile ledger totals against a provider invoice CSV (sku, cost_usd)."""
    conn = ledger.connect(ctx.obj["paths"]["db"])
    try:
        result = reconcile.reconcile(conn, invoice_csv)
    except ValueError as exc:
        conn.close()
        raise click.ClickException(str(exc)) from None
    conn.close()
    for line in result["lines"]:
        flag = "✓" if line["match"] else "✗ VARIANCE"
        click.echo(
            f"{flag} {line['sku']}: ledger ${line['ledger_usd']:,.4f} vs "
            f"invoiced ${line['invoiced_usd']:,.4f} "
            f"(Δ ${line['variance_usd']:,.4f})"
        )
    click.echo(f"total variance: ${result['total_variance_usd']:,.4f}")


@main.command()
@click.option("--out", default="agentfin-dashboard.html")
@click.option("--days", default=30, type=int)
@click.pass_context
def dashboard_cmd(ctx, out, days):
    """Export a static HTML dashboard."""
    conn = ledger.connect(ctx.obj["paths"]["db"])
    blist = budgets.load_budgets(ctx.obj["paths"]["budgets"])
    html_doc = dashboard.generate(conn, blist, days=days)
    conn.close()
    Path(out).write_text(html_doc)
    click.echo(f"wrote {out}")


@main.command()
@click.pass_context
def reprice(ctx):
    """Fill NULL (unpriced) costs using current pricing.yaml rates.

    Only touches events that were ingested before their SKU had a price —
    priced events are never rewritten, preserving ledger immutability."""
    paths = ctx.obj["paths"]
    conn = ledger.connect(paths["db"])
    rates = pricing.load_pricing(paths["pricing"])
    rows = conn.execute(
        """SELECT id, sku, input_tokens, cache_read_tokens, cache_write_tokens,
                  output_tokens FROM usage_events WHERE cost_micro_usd IS NULL"""
    ).fetchall()
    fixed = 0
    for r in rows:
        cost = pricing.cost_micro_usd(r["sku"], rates, dict(r))
        if cost is not None:
            conn.execute(
                "UPDATE usage_events SET cost_micro_usd = ? WHERE id = ?",
                (cost, r["id"]),
            )
            fixed += 1
    conn.commit()
    conn.close()
    click.echo(f"repriced {fixed} of {len(rows)} unpriced events")


@main.command()
@click.pass_context
def doctor(ctx):
    """Sanity checks: unpriced SKUs, unattributed events, data coverage."""
    conn = ledger.connect(ctx.obj["paths"]["db"])
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
    click.echo(f"events: {tot['events']:,}  span: {span['mn']} → {span['mx']}")
    click.echo(f"unpriced events: {tot['unpriced_events'] or 0:,}")
    for r in unpriced:
        click.echo(f"  · {r['sku']}: {r['c']:,} events (add to pricing.yaml)")
    click.echo(f"unattributed (team=unassigned): {unattributed:,}")
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
    if dupes:
        click.echo(
            f"⚠ {len(dupes)} groups of near-identical events under different "
            "idempotency keys — possible cross-source double-ingest. "
            "Ingest each source exactly once."
        )
    conn.close()
    if tot["unpriced_events"]:
        click.echo("tip: add the SKUs to pricing.yaml, then run `agentfin reprice` "
                   "to fill NULL costs from the new rates.")
