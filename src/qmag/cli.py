"""``qmag`` command line interface."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Optional

import pandas as pd
import typer
from rich.console import Console
from rich.table import Table

from .backtest import default_detectors, prepare_data, run_backtest
from .broker import BROKER_KINDS, LIVE_BROKERS
from .config import StrategyConfig
from .data import DATA_KINDS, make_provider
from .optimize import DEFAULT_GRID, walk_forward
from .regime import regime_snapshot
from .session import SessionSettings, TradingSession, build_config, load_frames
from .setups import BreakoutDetector, Signal, momentum_ok
from .themes import latest_theme_leaderboard
from .trader import CycleReport, TraderState
from .universe import DEFAULT_UNIVERSE_FILE, MARKET_UNIVERSE_FILE, build_universe, load_universe

app = typer.Typer(help="Research, backtest, optimise and paper-trade Qullamaggie-style momentum setups.", no_args_is_help=True)
paper_app = typer.Typer(help="Paper / live trading loop.", no_args_is_help=True)
app.add_typer(paper_app, name="paper")
universe_app = typer.Typer(help="Build and inspect the whole-market scan universe.", no_args_is_help=True)
app.add_typer(universe_app, name="universe")
# When output is piped (logs, cron) rich would otherwise squeeze tables to 80 columns.
console = Console() if sys.stdout.isatty() else Console(width=150, height=60)

DataOpt = typer.Option(
    "auto", "--data", help="Data provider: " + " | ".join(DATA_KINDS) + " (auto = unusual_whales when UNUSUAL_WHALES_API_KEY is set, else yfinance)"
)
CsvDirOpt = typer.Option("data/csv", "--csv-dir", help="Directory of SYMBOL.csv files for --data csv")
UniverseOpt = typer.Option(None, "--universe", help="Ticker list file (default: universe/default.txt)")
SymbolsOpt = typer.Option(None, "--symbols", help="Comma-separated tickers (overrides --universe)")
ConfigOpt = typer.Option(None, "--config", help="Strategy YAML (default: built-in defaults)")
SentimentOpt = typer.Option(None, "--sentiment-csv", help="date,symbol,score[,buzz] CSV; enables the sentiment filter")
ThemesOpt = typer.Option(True, "--themes/--no-themes", help="Use theme/segment momentum (universe/themes.yaml)")


def _cfg(config: Optional[Path], sentiment_csv: Optional[Path], themes: bool) -> StrategyConfig:
    return build_config(config, sentiment_csv, themes)


StartOpt = typer.Option(None, "--start", help="YYYY-MM-DD")
EndOpt = typer.Option(None, "--end", help="YYYY-MM-DD")
BrokerOpt = typer.Option("paper", help="Broker: " + " | ".join(BROKER_KINDS))
StateDirOpt = typer.Option(Path("paper_state"), help="Where trader state, charts and the paper ledger live")
ChartsOpt = typer.Option(True, "--charts/--no-charts", help="Render annotated PNG charts for every plan and open position")
YesLiveOpt = typer.Option(False, "--yes-live", help="Skip the live-trading confirmation (for unattended daemons)")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _load(
    data: str, csv_dir: str, universe: Optional[str], symbols: Optional[str], cfg: StrategyConfig, start, end
) -> tuple[dict[str, pd.DataFrame], StrategyConfig]:
    try:
        frames, cfg = load_frames(data, csv_dir, universe, symbols, cfg, start, end)
    except ValueError as exc:  # a refused (simulated) or unknown data source
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    if not frames:
        console.print("[red]No data loaded. Check the provider, symbols or network access.[/red]")
        raise typer.Exit(1)
    return frames, cfg


def _confirm_live(broker: str, yes_live: bool) -> None:
    if broker in LIVE_BROKERS and not yes_live:
        typer.confirm(f"You are about to send LIVE orders through {broker}. Continue?", abort=True)


FlowOpt = typer.Option(
    None, "--options-flow/--no-options-flow",
    help="Scan each candidate's options tape for unusual trades via the paid Unusual Whales API (overrides options_flow.enabled)",
)


def _flow_override(options_flow: Optional[bool]) -> dict:
    return {} if options_flow is None else {"options_flow.enabled": options_flow}


def _session(data, csv_dir, universe, symbols, config, sentiment_csv, themes, broker, state_dir, charts, options_flow: Optional[bool] = None) -> TradingSession:
    try:
        return TradingSession(
            SessionSettings(
                data=data, csv_dir=csv_dir, universe=universe, symbols=symbols, config=config, sentiment_csv=sentiment_csv,
                themes=themes, broker=broker, state_dir=state_dir, charts=charts, overrides=_flow_override(options_flow),
            )
        )
    except ValueError as exc:  # e.g. a refused (simulated) data source
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)


def _fmt_pct(x: float) -> str:
    return f"{x * 100:,.1f}%"


def _signal_table(title: str, signals: list[Signal], limit: int = 40) -> Table:
    t = Table(title=title, show_lines=False)
    for col in ("Symbol", "Setup", "Date", "Pivot", "Entry", "Stop", "Risk %", "ADR %", "Score", "Notes"):
        t.add_column(col, justify="right" if col not in ("Symbol", "Setup", "Date", "Notes") else "left")
    for s in signals[:limit]:
        notes = ", ".join(f"{k}={v:.2f}" if isinstance(v, float) else f"{k}={v}" for k, v in list(s.details.items())[:4])
        t.add_row(
            s.symbol,
            s.setup,
            str(pd.Timestamp(s.date).date()),
            f"{s.pivot:.2f}",
            f"{s.entry:.2f}",
            f"{s.stop:.2f}",
            f"{(s.entry / s.stop - 1) * 100:.1f}",
            f"{s.adr_pct:.1f}",
            f"{s.score:.2f}",
            notes,
        )
    return t


# --------------------------------------------------------------------------- #
@app.command()
def scan(
    data: str = DataOpt,
    csv_dir: str = CsvDirOpt,
    universe: Optional[str] = UniverseOpt,
    symbols: Optional[str] = SymbolsOpt,
    config: Optional[Path] = ConfigOpt,
    sentiment_csv: Optional[Path] = SentimentOpt,
    themes: bool = ThemesOpt,
    end: Optional[str] = EndOpt,
    verbose: bool = typer.Option(False, "-v"),
):
    """Tonight's watchlist: flags ready to break out, plus anything that triggered on the latest bar."""
    _setup_logging(verbose)
    cfg = _cfg(config, sentiment_csv, themes)
    raw, cfg = _load(data, csv_dir, universe, symbols, cfg, None, end)
    frames = prepare_data(raw, cfg)
    asof = max(df.index[-1] for df in frames.values())
    detectors = default_detectors()

    triggered: list[Signal] = []
    watch: list[Signal] = []
    leaders = 0
    for sym, df in frames.items():
        if sym in cfg.auxiliary_symbols:
            continue
        if momentum_ok(df.iloc[-1], cfg):
            leaders += 1
        if df.index[-1] != asof:
            continue
        for det in detectors:
            sig = det.detect_last(sym, df, cfg)
            if sig is not None:
                triggered.append(sig)
        plan = BreakoutDetector().watchlist(sym, df, cfg)
        if plan:
            watch.append(plan)
    triggered.sort(key=lambda s: s.score, reverse=True)
    watch.sort(key=lambda s: s.score, reverse=True)

    snap = regime_snapshot(frames, cfg)
    regime = "[green]risk-on[/green]" if snap.ok else "[red]risk-off - stand aside[/red]"
    console.print(
        f"As of [bold]{asof.date()}[/bold] | universe {len(frames) - len(cfg.auxiliary_symbols & set(frames))} | "
        f"momentum leaders {leaders} | regime: {regime} ({snap.describe(cfg)})"
    )
    if cfg.themes.enabled:
        console.print(_theme_table(latest_theme_leaderboard(frames, cfg), cfg))
    console.print(_signal_table(f"Triggered on {asof.date()}", triggered) if triggered else "[dim]Nothing triggered on the latest bar.[/dim]")
    console.print(_signal_table("Breakout watchlist (buy-stop at Entry)", watch) if watch else "[dim]No flags within 10% of their pivot.[/dim]")


def _theme_table(board: pd.DataFrame, cfg: StrategyConfig) -> Table:
    t = Table(title="Theme momentum (equal-weight groups, ranked on 1m + 0.5 x 3m)")
    for col in ("Theme", "1m", "3m", "Percentile", "Breadth >20d", "Passes filter"):
        t.add_column(col, justify="left" if col == "Theme" else "right")
    for _, r in board.iterrows():
        passes = r["pct"] >= cfg.themes.min_theme_percentile and (pd.isna(r["breadth"]) or r["breadth"] >= cfg.themes.min_theme_breadth)
        t.add_row(
            str(r["theme"]),
            _fmt_pct(r["gain_1m"]) if pd.notna(r["gain_1m"]) else "-",
            _fmt_pct(r["gain_3m"]) if pd.notna(r["gain_3m"]) else "-",
            f"{r['pct']:.2f}" if pd.notna(r["pct"]) else "-",
            _fmt_pct(r["breadth"]) if pd.notna(r["breadth"]) else "-",
            "[green]yes[/green]" if passes else "[red]no[/red]",
        )
    return t


# --------------------------------------------------------------------------- #
@app.command()
def backtest(
    data: str = DataOpt,
    csv_dir: str = CsvDirOpt,
    universe: Optional[str] = UniverseOpt,
    symbols: Optional[str] = SymbolsOpt,
    config: Optional[Path] = ConfigOpt,
    sentiment_csv: Optional[Path] = SentimentOpt,
    themes: bool = ThemesOpt,
    start: Optional[str] = StartOpt,
    end: Optional[str] = EndOpt,
    out: Path = typer.Option(Path("reports"), "--out", help="Where to write trades.csv and equity.csv"),
    verbose: bool = typer.Option(False, "-v"),
):
    """Portfolio backtest with Kullamägi-style entries, sizing and exits."""
    _setup_logging(verbose)
    cfg = _cfg(config, sentiment_csv, themes)
    frames, cfg = _load(data, csv_dir, universe, symbols, cfg, start, end)
    with console.status("Running backtest..."):
        res = run_backtest(frames, cfg, start=start, end=end)
    m = res.metrics()

    gates = ", ".join(k for k, v in res.regime_gates.items() if v) or "off"
    context = " + ".join(x for x, on in (("themes", cfg.themes.enabled), ("sentiment", cfg.sentiment.enabled)) if on) or "none"
    t = Table(title=f"Backtest {res.start.date()} → {res.end.date()}  ({len(frames)} symbols | regime gates: {gates} | context: {context})")
    t.add_column("Metric")
    t.add_column("Value", justify="right")
    rows = [
        ("Start equity", f"${m['start_equity']:,.0f}"),
        ("End equity", f"${m['end_equity']:,.0f}"),
        ("Total return", _fmt_pct(m["total_return"])),
        ("CAGR", _fmt_pct(m["cagr"])),
        ("Max drawdown", _fmt_pct(m["max_drawdown"])),
        ("Sharpe", f"{m['sharpe']:.2f}"),
        ("Calmar", f"{m['calmar']:.2f}"),
        ("Trades (signals seen / taken)", f"{int(m['trades'])} ({res.signals_seen} / {res.signals_taken})"),
        ("Win rate", _fmt_pct(m["win_rate"])),
        ("Expectancy (R)", f"{m['expectancy_r']:.2f}"),
        ("Profit factor", f"{m['profit_factor']:.2f}"),
        ("Avg win / avg loss", f"${m['avg_win']:,.0f} / ${m['avg_loss']:,.0f}"),
        ("Avg bars held", f"{m['avg_bars_held']:.1f}"),
    ]
    for k, v in rows:
        t.add_row(k, v)
    console.print(t)

    tf = res.trades_frame
    if len(tf):
        by_setup = tf.groupby("setup").agg(trades=("pnl", "size"), win_rate=("pnl", lambda s: (s > 0).mean()), avg_r=("r_multiple", "mean"), pnl=("pnl", "sum"))
        st = Table(title="By setup")
        for c in ("Setup", "Trades", "Win rate", "Avg R", "P&L"):
            st.add_column(c, justify="right" if c != "Setup" else "left")
        for setup, r in by_setup.iterrows():
            st.add_row(str(setup), str(int(r["trades"])), _fmt_pct(r["win_rate"]), f"{r['avg_r']:.2f}", f"${r['pnl']:,.0f}")
        console.print(st)
        ex = tf["exit_reason"].value_counts()
        console.print("Exit reasons: " + ", ".join(f"{k} {v}" for k, v in ex.items()))

    out.mkdir(parents=True, exist_ok=True)
    tf.to_csv(out / "trades.csv", index=False)
    res.equity.to_csv(out / "equity.csv", header=True)
    console.print(f"[dim]Wrote {out / 'trades.csv'} and {out / 'equity.csv'}[/dim]")


# --------------------------------------------------------------------------- #
@app.command()
def optimize(
    data: str = DataOpt,
    csv_dir: str = CsvDirOpt,
    universe: Optional[str] = UniverseOpt,
    symbols: Optional[str] = SymbolsOpt,
    config: Optional[Path] = ConfigOpt,
    sentiment_csv: Optional[Path] = SentimentOpt,
    themes: bool = ThemesOpt,
    start: Optional[str] = StartOpt,
    end: Optional[str] = EndOpt,
    folds: int = typer.Option(3, help="Walk-forward folds"),
    is_fraction: float = typer.Option(0.6, help="In-sample share of each fold"),
    objective: str = typer.Option("calmar", help="calmar | expectancy | sharpe"),
    grid: Optional[Path] = typer.Option(None, help="YAML mapping of dotted parameter -> list of values"),
    out: Path = typer.Option(Path("reports"), "--out"),
    verbose: bool = typer.Option(False, "-v"),
):
    """Walk-forward parameter search. Judge the OOS columns, not the IS ones."""
    _setup_logging(verbose)
    import yaml

    cfg = _cfg(config, sentiment_csv, themes)
    grid_dict = DEFAULT_GRID if grid is None else yaml.safe_load(Path(grid).read_text())
    frames, cfg = _load(data, csv_dir, universe, symbols, cfg, start, end)
    if start or end:
        frames = {s: df.loc[start:end] for s, df in frames.items()}

    with console.status("Walking forward...") as status:
        res = walk_forward(frames, cfg, grid=grid_dict, n_folds=folds, is_fraction=is_fraction, objective=objective, progress=lambda msg: status.update(msg))

    summary = res.summary()
    if summary.empty:
        console.print("[red]Not enough history for the requested folds.[/red]")
        raise typer.Exit(1)
    t = Table(title=f"Walk-forward ({objective}) - baseline = untouched defaults")
    for c in summary.columns:
        t.add_column(c, justify="right" if c not in ("is", "oos") else "left")
    for _, r in summary.iterrows():
        t.add_row(*[(_fmt_pct(v) if "return" in c else f"{v:.2f}" if isinstance(v, float) else str(v)) for c, v in r.items()])
    console.print(t)
    if not res.stitched_oos_equity.empty:
        console.print(f"Stitched out-of-sample return of the re-optimised parameters: [bold]{_fmt_pct(res.stitched_oos_equity.iloc[-1] - 1)}[/bold]")
    out.mkdir(parents=True, exist_ok=True)
    res.grid_table.to_csv(out / "walkforward_grid.csv", index=False)
    summary.to_csv(out / "walkforward_summary.csv", index=False)
    console.print(f"[dim]Wrote {out / 'walkforward_grid.csv'} and {out / 'walkforward_summary.csv'}[/dim]")


# --------------------------------------------------------------------------- #
@universe_app.command("build")
def universe_build(
    out: Path = typer.Option(MARKET_UNIVERSE_FILE, "--out", help="Where to write the ticker list"),
    min_price: float = typer.Option(3.0, help="Drop names closing below this"),
    min_dollar_volume: float = typer.Option(5_000_000, help="Drop names with 20d avg $ volume below this"),
    min_bars: int = typer.Option(60, help="Minimum bars of history (keeps recent IPOs for EPs)"),
    limit: Optional[int] = typer.Option(None, help="Only process the first N listings (for a quick test)"),
    fundamentals: bool = typer.Option(True, "--fundamentals/--no-fundamentals", help="Also refresh universe/fundamentals.csv (sector, industry, float, short interest, earnings) from finviz"),
    verbose: bool = typer.Option(False, "-v"),
):
    """Scan the WHOLE US market: pull every NASDAQ/NYSE/AMEX/Arca/BATS common stock, download a year of
    bars (batched, cached) and keep the liquid ones. Re-run weekly; the daemon does this on Sundays."""
    _setup_logging(verbose)
    provider = make_provider("yfinance")
    with console.status("Fetching listings...") as status:
        res = build_universe(
            provider, out_path=out, min_price=min_price, min_dollar_volume=min_dollar_volume, min_bars=min_bars, limit=limit,
            progress=lambda msg: status.update(msg),
        )
    console.print(
        f"Listed common stocks: {res.listed} | with data: {res.downloaded} | [bold green]kept: {res.kept}[/bold green] "
        f"(rejected: price {res.rejected_price}, volume {res.rejected_volume}, history {res.rejected_history})"
    )
    console.print(f"Wrote [bold]{res.path}[/bold] - it is now the default universe for scan/backtest/paper.")
    if fundamentals:
        _refresh_fundamentals(load_universe(res.path))


def _refresh_fundamentals(symbols: list[str], workers: int = 4) -> None:
    from .fundamentals import FUNDAMENTALS_FILE, fetch_fundamentals, industry_themes, save_fundamentals

    with console.status(f"Fetching finviz fundamentals for {len(symbols)} symbols...") as status:
        df = fetch_fundamentals(symbols, workers=workers, progress=lambda msg: status.update(msg))
    path = save_fundamentals(df, FUNDAMENTALS_FILE)
    groups = industry_themes(df, symbols)
    console.print(f"Fundamentals for [bold]{len(df)}[/bold] symbols -> {path}; [bold]{len(groups)}[/bold] industry auto-themes available.")


@universe_app.command("fundamentals")
def universe_fundamentals(
    universe: Optional[str] = UniverseOpt,
    symbols: Optional[str] = SymbolsOpt,
    workers: int = typer.Option(4, help="Parallel finviz requests (be polite)"),
    verbose: bool = typer.Option(False, "-v"),
):
    """Refresh universe/fundamentals.csv from finviz without rebuilding the ticker list."""
    _setup_logging(verbose)
    _refresh_fundamentals(load_universe(universe, symbols), workers=workers)


@universe_app.command("show")
def universe_show(path: Optional[Path] = typer.Argument(None, help="Universe file (default: market.txt if built, else default.txt)")):
    """Print which universe file is active and how many symbols it holds."""
    file = path or (MARKET_UNIVERSE_FILE if MARKET_UNIVERSE_FILE.exists() else DEFAULT_UNIVERSE_FILE)
    syms = load_universe(file)
    header = [line for line in file.read_text().splitlines()[:3] if line.startswith("#")]
    console.print(f"[bold]{file}[/bold]: {len(syms)} symbols")
    for line in header:
        console.print(f"[dim]{line}[/dim]")


# --------------------------------------------------------------------------- #
@app.command("init-config")
def init_config(path: Path = typer.Argument(Path("qmag.yaml"))):
    """Write the default strategy parameters to a YAML file you can edit."""
    StrategyConfig().save(path)
    console.print(f"Wrote defaults to [bold]{path}[/bold]")


# --------------------------------------------------------------------------- #
@paper_app.command("run")
def paper_run(
    data: str = DataOpt,
    csv_dir: str = CsvDirOpt,
    universe: Optional[str] = UniverseOpt,
    symbols: Optional[str] = SymbolsOpt,
    config: Optional[Path] = ConfigOpt,
    sentiment_csv: Optional[Path] = SentimentOpt,
    themes: bool = ThemesOpt,
    broker: str = BrokerOpt,
    state_dir: Path = StateDirOpt,
    charts: bool = ChartsOpt,
    asof: Optional[str] = typer.Option(None, help="Pretend today is this date (for --data csv replays)"),
    focused: bool = typer.Option(False, "--focused", help="Focused pass: only the arming list, open positions and pending entries (what the daemon runs every few minutes)"),
    options_flow: Optional[bool] = FlowOpt,
    yes_live: bool = YesLiveOpt,
    verbose: bool = typer.Option(False, "-v"),
):
    """Run one trading cycle: reconcile, manage exits, check + size + chart new setups, place entries."""
    _setup_logging(verbose)
    _confirm_live(broker, yes_live)
    sess = _session(data, csv_dir, universe, symbols, config, sentiment_csv, themes, broker, state_dir, charts, options_flow)
    try:
        if focused:
            report = sess.focused_cycle(asof=asof, label="manual_focused")
            if report is None:
                console.print("[yellow]Nothing armed, held or pending: run a full cycle first to build the arming list.[/yellow]")
                return
        else:
            report = sess.cycle(asof=asof, label="manual")
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    _print_report(sess, report)


@app.command("insider-scan")
def insider_scan_cmd(
    config: Optional[Path] = ConfigOpt,
    state_dir: Path = StateDirOpt,
    no_ai: bool = typer.Option(False, "--no-ai", help="Skip the AI catalyst analysis (flags from the options data only)"),
    lookback_days: Optional[int] = typer.Option(None, help="Override insider_scan.lookback_days"),
    as_json: bool = typer.Option(False, "--json", help="Print the full report as JSON"),
    verbose: bool = typer.Option(False, "-v"),
):
    """Saturday job on demand: review the week's unusual options trades, flag the ones that look informed,
    and ask the AI what catalyst they could be tied to. Writes state_dir/insider_scan.json. Needs UNUSUAL_WHALES_API_KEY."""
    _setup_logging(verbose)
    sess = TradingSession(SessionSettings(config=config, state_dir=state_dir, charts=False))
    if lookback_days:
        sess.cfg = sess.cfg.with_overrides({"insider_scan.lookback_days": int(lookback_days)})
        sess._settings_signature = sess.store.signature()  # keep the override for this run
    report = sess.insider_scan(ai=False if no_ai else None)
    if as_json:
        console.print_json(json.dumps(report, default=str))
        return
    _print_insider(sess, report)
    if report.get("unavailable"):
        raise typer.Exit(1)


def _print_insider(sess: TradingSession, report: dict) -> None:
    console.print(f"[bold]Insider / unusual-options scan[/bold] week {report['week_start']} → {report['week_end']}")
    if report.get("unavailable"):
        console.print("[red]NOT RUN — options data unavailable:[/red]")
        for g in report.get("data_gaps", []):
            console.print(f"  - {g}")
        return
    console.print(
        f"  {report['alerts_considered']} alerts, {report['contracts_considered']} unusual contracts, {report['tickers_considered']} tickers, "
        f"{len(report['flagged'])} flagged · {report['calls']} API calls · AI {'on' if report['ai']['enabled'] else 'off'}"
    )
    for g in report.get("data_gaps", []):
        console.print(f"  [red]gap:[/red] {g}")
    if report["flagged"]:
        t = Table(title="Flagged for investigation (a lead, not an accusation)")
        for c in ("Score", "Ticker", "Dir", "Premium", "Alerts", "Days", "Max vol/OI", "Max OTM", "Nearest exp", "AI verdict", "Speculating on"):
            t.add_column(c, justify="right" if c in ("Score", "Premium", "Alerts", "Days", "Max vol/OI", "Max OTM") else "left")
        for f in report["flagged"]:
            ai = f.get("ai") or {}
            verdict = f"{ai['verdict']} {ai['suspicion']:.0%}" if ai.get("verdict") else ("error" if ai.get("error") else "-")
            t.add_row(
                f"{f['score']:.1f}", f["ticker"], f["direction"], f"${f['premium_total']:,.0f}", str(f["alerts"]), str(len(f["days_active"])),
                f"{f['max_vol_oi']:.1f}x", f"{f['max_otm_pct']:.0f}%", str(f.get("earliest_expiry") or "-"), verdict, (ai.get("speculating_on") or "")[:90],
            )
        console.print(t)
        for f in report["flagged"]:
            ai = f.get("ai") or {}
            if ai.get("summary"):
                console.print(f"[bold]{f['ticker']}[/bold]: {ai['summary']}")
                for c in ai.get("possible_catalysts", [])[:3]:
                    console.print(f"    [{c['likelihood']}] {c['catalyst']}" + (f" — {c['basis']}" if c.get("basis") else ""))
            elif ai.get("error"):
                console.print(f"[bold]{f['ticker']}[/bold]: [yellow]AI unavailable ({ai['error']})[/yellow]")
    else:
        console.print("  Nothing scored above the flag threshold this week.")
    console.print(f"[dim]Report: {sess.insider_path}  Dashboard: /insider[/dim]")


@app.command("learn")
def learn_cmd(
    config: Optional[Path] = ConfigOpt,
    state_dir: Path = StateDirOpt,
    apply: Optional[bool] = typer.Option(None, "--apply/--no-apply", help="Write knob adjustments to learning_overrides.yaml (default: learning.auto_apply)"),
    no_ai: bool = typer.Option(False, "--no-ai", help="Skip the AI post-mortems"),
    reset: bool = typer.Option(False, "--reset", help="Drop every learned adjustment and exit"),
    as_json: bool = typer.Option(False, "--json", help="Print the full report as JSON"),
    verbose: bool = typer.Option(False, "-v"),
):
    """The weekly learning review on demand: post-mortems for every closed trade, what worked / what did not by
    entry condition, how the filters and entry gates score on the setups they blocked (shadow trades), and the
    bounded knob adjustments the evidence supports. Writes state_dir/learning_report.json."""
    _setup_logging(verbose)
    sess = TradingSession(SessionSettings(config=config, state_dir=state_dir, charts=False))
    if reset:
        removed = sess.reset_learning()
        console.print("Learned adjustments removed; operator settings apply again." if removed else "No learned adjustments were active.")
        return
    report = sess.learn(apply=apply, ai=False if no_ai else None)
    if as_json:
        console.print_json(json.dumps(report, default=str))
        return
    _print_learning(sess, report)


def _print_learning(sess: TradingSession, report: dict) -> None:
    o = report["overall"]
    console.print(f"[bold]Learning review[/bold] {report['generated_at'][:16]} · {report['trades']} closed trades · status {report['status']}")
    if o["n"]:
        pf = f"{o['profit_factor']:.2f}" if o["profit_factor"] is not None else ("no losses" if o["win_rate"] == 1.0 else "-")
        console.print(f"  win rate {o['win_rate']:.0%} · avg {o['avg_r']:+.2f}R · median {o['median_r']:+.2f}R · profit factor {pf} · total {o['total_r']:+.1f}R")
    for g in report.get("data_gaps", []):
        console.print(f"  [yellow]note:[/yellow] {g}")
    sh = report.get("shadows") or {}
    if sh.get("total"):
        console.print(f"  shadow trades: {sh['total']} ({', '.join(f'{k} {v}' for k, v in sh.get('by_status', {}).items())}) - {sh.get('note', '')}")
    if report["lessons"]:
        t = Table(title="Lessons (small-sample adjusted)")
        for c in ("Kind", "Trades", "Avg R", "Lesson"):
            t.add_column(c, justify="right" if c in ("Trades", "Avg R") else "left")
        for l in report["lessons"][:14]:
            t.add_row(l["kind"], str(l["n"]), f"{l['avg_r']:+.2f}" if l.get("avg_r") is not None else "-", l["text"])
        console.print(t)
    else:
        console.print("  No lesson clears the evidence bar yet.")
    if report["adjustments"]:
        t = Table(title="Adjustments proposed (application is recorded separately)")
        for c in ("Knob", "From", "To", "Why"):
            t.add_column(c)
        for a in report["adjustments"]:
            t.add_row(a["label"], f"{a['from']:g}", f"{a['to']:g}", a["reason"])
        console.print(t)
    if report.get("applied"):
        console.print("  Applied after evidence checks: " + ", ".join(a["label"] for a in report["applied"]))
    if report["active_overrides"]:
        console.print("  Active learned values: " + ", ".join(f"{v['label']} = {v['value']:g}" for v in report["active_overrides"].values()))
    for pm in report["post_mortems"][:6]:
        console.print(f"  [bold]{pm['symbol']}[/bold] {pm['r_multiple']:+.2f}R ({pm['exit_reason']}): {(pm.get('post_mortem') or {}).get('text', '')[:220]}")
        if pm.get("post_mortem_ai"):
            console.print(f"      [cyan]AI:[/cyan] {pm['post_mortem_ai'].get('lesson', '')[:200]}")
    ai = report.get("ai") or {}
    console.print(f"  AI post-mortems: {'analysed ' + str(ai.get('analysed', 0)) if ai.get('enabled') else 'off'}" + (f" · [yellow]{ai['error']}[/yellow]" if ai.get("error") else ""))
    console.print(f"[dim]Report: {sess.state_dir / 'learning_report.json'}  Dashboard: /learning[/dim]")


def _print_report(sess: TradingSession, report: CycleReport) -> None:
    regime = "UNKNOWN (treated as risk-off)" if not report.regime_known else "ok" if report.regime_ok else "RISK-OFF"
    console.print(
        f"[bold]{sess.broker.name}[/bold] {report.scan} cycle as of {report.asof} | entry mode {report.entry_mode} | equity ${report.equity:,.0f} | "
        f"regime {regime} ({report.regime_note})"
    )
    if report.data_gaps:
        console.print("[red]Data gaps (nothing substituted):[/red]")
        for g in report.data_gaps:
            console.print(f"  - {g}")
    for a in report.actions:
        console.print("  " + a)
    if report.plans:
        t = Table(title="Trade plans (orders placed)")
        for c in ("Symbol", "Setup", "Theme", "Entry", "Stop", "Target 1/3", "Shares", "Risk $", "Pos %", "Chart"):
            t.add_column(c, justify="left" if c in ("Symbol", "Setup", "Theme", "Chart") else "right")
        charts = (sess.last_report() or {}).get("charts", {})
        for p in report.plans:
            t.add_row(
                p.symbol, p.setup, p.theme or "-", f"{p.entry:.2f}", f"{p.stop:.2f}",
                f"{p.partial_target:.2f}" if p.partial_target else "-", str(p.shares), f"{p.risk_dollars:,.0f}",
                f"{p.position_pct * 100:.1f}", charts.get(p.symbol, ""),
            )
        console.print(t)
    if report.open_positions:
        t = Table(title="Open positions")
        for c in ("Symbol", "Setup", "Entry date", "Entry", "Left/size", "Stop", "Target", "Stage"):
            t.add_column(c)
        for p in report.open_positions:
            t.add_row(
                p.symbol, p.setup, p.entry_date, f"{p.entry_price:.2f}", f"{p.remaining}/{p.shares}", f"{p.stop:.2f}",
                f"{p.target:.2f}" if p.target and not p.partial_done else "-", "trailing MA" if p.partial_done else "initial risk",
            )
        console.print(t)
    if report.triggered:
        console.print(_signal_table("Triggered today", report.triggered, limit=15))
    if report.watchlist:
        console.print(_signal_table("Watchlist for tomorrow", report.watchlist, limit=15))
    console.print(f"[dim]Report: {sess.report_path}  Charts: {sess.chart_dir}[/dim]")


# --------------------------------------------------------------------------- #
@app.command()
def chart(
    symbol: str = typer.Argument(..., help="Ticker to chart"),
    data: str = DataOpt,
    csv_dir: str = CsvDirOpt,
    config: Optional[Path] = ConfigOpt,
    end: Optional[str] = EndOpt,
    out: Path = typer.Option(Path("reports/charts"), help="Output directory"),
    equity: float = typer.Option(100_000.0, help="Account size used to size the plan"),
    bars: int = typer.Option(130, help="Bars to show"),
):
    """Chart a symbol with its setup (flag, pivot) and a sized entry / stop / target plan."""
    from .charts import ChartLevels, chart_signal, render_chart
    from .plan import build_plan

    cfg = _cfg(config, None, False)
    symbol = symbol.upper()
    raw, cfg = _load(data, csv_dir, None, symbol, cfg, None, end)
    frames = prepare_data(raw, cfg)
    df = frames[symbol]
    sig = None
    for det in default_detectors():
        sig = det.detect_last(symbol, df, cfg) or sig
    watch = BreakoutDetector().watchlist(symbol, df, cfg)
    sig = sig or watch
    if sig is not None:
        plan = build_plan(sig, df.iloc[-1], cfg, equity, 0.0, equity, True)
        path = chart_signal(df, sig, out, target=plan.partial_target, shares=plan.shares, note=plan.summary())
        console.print(f"{sig.setup} {'triggered' if sig is not watch else 'ready (buy-stop at pivot)'}: {plan.summary()}")
        if plan.failed_checks:
            console.print(f"[yellow]Would NOT be taken: failed {', '.join(plan.failed_checks)}[/yellow]")
    else:
        last = df.iloc[-1]
        entry = float(last["close"])
        if pd.isna(last.get("adr_dollar", float("nan"))):
            levels = ChartLevels(entry=entry, stop=None, setup="no setup", note="No setup on the latest bar; ADR unknown (too little history), so no stop / target is drawn.")
            console.print(f"[yellow]{symbol}: ADR could not be computed from the available history; no illustrative levels drawn.[/yellow]")
        else:
            stop = entry - cfg.management.stop_adr_mult * float(last["adr_dollar"])
            levels = ChartLevels(entry=entry, stop=stop, target=entry + (cfg.management.partial_target_r or 2) * (entry - stop), setup="no setup", note="No setup on the latest bar; levels are illustrative (last close, 1 ADR stop), not a plan.")
        path = render_chart(df, symbol, levels, out / f"{symbol}_{df.index[-1].date()}.png", bars=bars)
        console.print(f"[yellow]{symbol}: no Qullamaggie setup on the latest bar[/yellow] (momentum leader: {'yes' if momentum_ok(last, cfg) else 'no'})")
    console.print(f"Chart written to [bold]{path}[/bold]")


@app.command()
def review(
    symbol: str = typer.Argument(..., help="Ticker to run through the LLM reviewer"),
    data: str = DataOpt,
    csv_dir: str = CsvDirOpt,
    config: Optional[Path] = ConfigOpt,
    broker: str = BrokerOpt,
    state_dir: Path = StateDirOpt,
    mode: Optional[str] = typer.Option(None, help="Override reviewer.mode: advisory | gate | gate_and_size"),
    provider: Optional[str] = typer.Option(None, help="Override reviewer.provider: gemini | openai | auto"),
    model: Optional[str] = typer.Option(None, help="Override reviewer.model"),
    bundle_only: bool = typer.Option(False, "--bundle-only", help="Print the edge bundle that would be sent and exit (no API call)"),
    as_json: bool = typer.Option(False, "--json", help="Print the raw verdict JSON"),
    options_flow: Optional[bool] = FlowOpt,
):
    """Desk check one ticker and ask the additive LLM reviewer for its strict-JSON verdict.

    Builds the same edge bundle the trader sends before a trade - setup, sized
    plan, checklist, rationale, news/events, social, options flow,
    fundamentals, committee debate, regime and portfolio - and prints the
    action / confidence / thesis / catalysts / risks / invalidation / sizeNote.
    """
    import json as _json

    from .reviewer import build_edge_bundle

    overrides: dict = {"reviewer.enabled": True, **_flow_override(options_flow)}
    if mode:
        overrides["reviewer.mode"] = mode
    if provider:
        overrides["reviewer.provider"] = provider
    if model:
        overrides["reviewer.model"] = model
    if bundle_only:
        overrides["reviewer.enabled"] = False
    sess = TradingSession(SessionSettings(data=data, csv_dir=csv_dir, config=config, broker=broker, state_dir=state_dir, charts=False, overrides=overrides))
    result = sess.analyze_symbol(symbol)
    plan = result.get("plan")
    console.print(f"[bold]{result['symbol']}[/bold] as of {result['asof']}: setup status [bold]{result['status']}[/bold], last close {result['last_close']:.2f}")
    if plan is None:
        console.print("[yellow]No breakout flag or episodic pivot on the latest bar - nothing to review.[/yellow]")
        raise typer.Exit(0)
    console.print(("[green]" if plan["ok"] else "[yellow]") + plan["summary"] + "[/]")
    if plan["failed_checks"]:
        console.print(f"[yellow]Rule checklist failed: {', '.join(plan['failed_checks'])}[/yellow]")
    if bundle_only:
        entry_mode = "market now" if result["status"] == "triggered" else "buy-stop at pivot for the next session"
        console.print_json(_json.dumps(build_edge_bundle(plan, result["regime_ok"], result["regime_note"], {"broker": broker}, entry_mode), default=str))
        raise typer.Exit(0)
    v = plan.get("reviewer") or {}
    if as_json:
        console.print_json(_json.dumps(v, default=str))
        raise typer.Exit(0)
    if "error" in v:
        console.print(f"[red]Reviewer unavailable ({v.get('provider')} / {v.get('model')}): {v['error']}[/red]")
        console.print("Set GEMINI_API_KEY (or GOOGLE_API_KEY) for Gemini, or OPENAI_API_KEY / QMAG_LLM_API_KEY + QMAG_LLM_BASE_URL for an OpenAI-compatible endpoint.")
        raise typer.Exit(1)
    colour = {"BUY": "green", "SELL": "red", "HOLD": "yellow"}[v["action"]]
    t = Table(title=f"LLM reviewer · {v.get('provider', '?')} / {v.get('model', '?')} · mode {sess.cfg.reviewer.mode}", show_header=False, expand=True)
    t.add_column("k", style="bold", width=14)
    t.add_column("v", overflow="fold")
    t.add_row("Action", f"[{colour}]{v['action']}[/{colour}]  confidence {v['confidence']:.0%}  size x{v['sizeMultiplier']:.2f}")
    t.add_row("Thesis", v["thesis"])
    t.add_row("Catalysts", "\n".join(f"• {c}" for c in v["catalysts"]) or "–")
    t.add_row("Risks", "\n".join(f"• {c}" for c in v["risks"]) or "–")
    t.add_row("Invalidation", v["invalidation"] or "–")
    t.add_row("Size note", v["sizeNote"] or "–")
    t.add_row("Trader", str(plan.get("notes", {}).get("reviewer", "advisory")))
    console.print(t)


@app.command()
def flow(
    symbol: str = typer.Argument(..., help="Ticker to scan for unusual options activity"),
    config: Optional[Path] = ConfigOpt,
    min_premium: Optional[float] = typer.Option(None, help="Override options_flow.min_premium"),
    lookback_days: Optional[int] = typer.Option(None, help="Override options_flow.lookback_days"),
    as_json: bool = typer.Option(False, "--json", help="Print the raw flow fields"),
):
    """Scan one ticker's options tape for unusual trades (Unusual Whales, paid; needs UNUSUAL_WHALES_API_KEY).

    Prints the flow tilt, bullish vs bearish premium, call volume vs its
    30-day average, the option-volume percentile and the largest unusual
    trades exactly as the trader would see them. Runs regardless of
    ``options_flow.enabled`` so you can check the feed before switching it on.
    """
    import json as _json
    from datetime import datetime, timezone

    from .context.base import ContextReport
    from .context.sources import fetch_unusual_whales
    from .rationale import _describe_trade

    cfg = StrategyConfig.load(config)
    ov: dict = {}
    if min_premium is not None:
        ov["options_flow.min_premium"] = min_premium
    if lookback_days is not None:
        ov["options_flow.lookback_days"] = lookback_days
    cfg = cfg.with_overrides(ov) if ov else cfg
    f = cfg.options_flow
    now = datetime.now(timezone.utc)
    rep = ContextReport(symbol=symbol.upper(), asof=now.date().isoformat())
    fetch_unusual_whales(rep, settings=f, now=now)
    if as_json:
        console.print_json(_json.dumps({k: v for k, v in rep.to_dict().items() if k.startswith("flow_") or k in ("symbol", "asof", "available", "errors")}, default=str))
        raise typer.Exit(0 if rep.available.get("unusual_whales") else 1)
    if not rep.available.get("unusual_whales"):
        console.print(f"[red]Unusual Whales unavailable for {rep.symbol}: {rep.errors.get('unusual_whales', 'unknown error')}[/red]")
        if "API_KEY" in rep.errors.get("unusual_whales", ""):
            console.print("Set UNUSUAL_WHALES_API_KEY (paid plan) and, to use it in trading, options_flow.enabled: true or --options-flow.")
        raise typer.Exit(1)
    state = "off" if not f.enabled else "on"
    console.print(f"[bold]{rep.symbol}[/bold] unusual options flow (scan is [bold]{state}[/bold] in the strategy config)")
    if rep.flow_score is None:
        console.print("[yellow]Nothing to score: no premium summary and no unusual trades above the floor.[/yellow]")
        raise typer.Exit(0)
    colour = "green" if rep.flow_score > 0.2 else "red" if rep.flow_score < -0.2 else "yellow"
    t = Table(show_header=False, expand=True)
    t.add_column("k", style="bold", width=22)
    t.add_column("v", overflow="fold")
    t.add_row("Flow tilt", f"[{colour}]{rep.flow_score:+.2f}[/{colour}]  {'UNUSUAL ACTIVITY' if rep.flow_unusual else 'nothing unusual'}")
    if rep.flow_bull_premium is not None:
        t.add_row("Bullish / bearish prem", f"${rep.flow_bull_premium / 1e6:.2f}M / ${rep.flow_bear_premium / 1e6:.2f}M")
    if rep.flow_call_vol_ratio is not None:
        t.add_row("Call vol vs 30d", f"{rep.flow_call_vol_ratio:.1f}x" + (f" (puts {rep.flow_put_vol_ratio:.1f}x)" if rep.flow_put_vol_ratio is not None else ""))
    if rep.flow_opt_vol_pctile is not None:
        t.add_row("Option vol percentile", f"{rep.flow_opt_vol_pctile:.0f}")
    t.add_row("Unusual trades", f"{rep.flow_alerts} ({rep.flow_bull_alerts} bullish / {rep.flow_bear_alerts} bearish, {rep.flow_sweeps} sweeps) >= ${f.min_premium / 1e3:.0f}k, last {f.lookback_days}d, DTE <= {f.max_dte}")
    console.print(t)
    if rep.flow_trades:
        tt = Table(title="Largest unusual trades")
        for c in ("When", "Contract", "DTE", "Premium", "Side", "Vol/OI", "OTM", "Flags"):
            tt.add_column(c, justify="right" if c in ("DTE", "Premium", "Vol/OI", "OTM") else "left")
        for tr in rep.flow_trades:
            strike = f"{tr['strike']:g}" if tr.get("strike") is not None else ""
            flags = " ".join(x for x in ("sweep" if tr.get("sweep") else "", "floor" if tr.get("floor") else "", str(tr.get("rule") or "")) if x)
            tt.add_row(
                tr.get("when") or "", f"{strike}{'C' if tr['type'] == 'call' else 'P'} {tr.get('expiry', '')}", str(tr.get("dte", "")),
                f"${tr['premium'] / 1e3:,.0f}k", f"[{'green' if tr['direction'] == 'bull' else 'red'}]{tr['side']}[/]",
                f"{tr['vol_oi']:.1f}" if tr.get("vol_oi") is not None else "", f"{tr['otm_pct']:.0f}%" if tr.get("otm_pct") is not None else "", flags,
            )
        console.print(tt)
        console.print("[dim]" + "; ".join(_describe_trade(x) for x in rep.flow_trades[:3]) + "[/dim]")


@app.command()
def edge(
    symbol: str = typer.Argument(..., help="Ticker to score"),
    config: Optional[Path] = ConfigOpt,
    state_dir: Path = StateDirOpt,
    as_json: bool = typer.Option(False, "--json", help="Print the full edge breakdown as JSON"),
    rules: bool = typer.Option(False, "--rules", help="Print how every feature's sub-score is derived, then exit"),
):
    """Compute the Unusual Whales edge score for one ticker (paid; needs UNUSUAL_WHALES_API_KEY).

    Every enabled feature is read live (daily facts from the on-disk cache
    when fresh), scored -1..+1, weight-averaged and compared with
    ``edge.threshold`` and ``edge.min_coverage`` exactly as the trader does.
    Features that could not be sourced are listed as missing, never as neutral.
    """
    import json as _json
    from datetime import datetime, timezone

    from .context.base import ContextReport
    from .context.edge import FEATURES, GROUP_LABELS, compute_edge, describe_edge, enabled_features
    from .uw import UWClient

    cfg = StrategyConfig.load(config)
    e = cfg.edge
    if rules:
        t = Table(title="Unusual Whales edge features", show_lines=True)
        for c in ("Feature", "Group", "Weight", "Reads", "Sub-score rule"):
            t.add_column(c, justify="right" if c == "Weight" else "left", overflow="fold")
        for f in FEATURES:
            w = float((e.weights or {}).get(f.name, 0) or 0)
            t.add_row(f"{f.label}\n[dim]{f.name}[/dim]", GROUP_LABELS[f.group], f"{w:g}" if w > 0 else "[dim]off[/dim]", f.reads, f.rule)
        console.print(t)
        console.print(f"Gate: score >= {e.threshold:+.2f} with coverage >= {e.min_coverage * 100:.0f}% ({'enforced' if e.gate else 'advisory'}); {len(enabled_features(cfg))} features enabled.")
        raise typer.Exit(0)
    now = datetime.now(timezone.utc)
    rep = ContextReport(symbol=symbol.upper(), asof=now.date().isoformat())
    client = UWClient(cache_path=Path(state_dir) / "uw_cache.json")
    out = compute_edge(rep, cfg, client, now)
    if as_json:
        console.print_json(_json.dumps(out, default=str))
        raise typer.Exit(0 if out.get("score") is not None else 1)
    if out.get("error"):
        console.print(f"[red]Edge score unavailable for {rep.symbol}: {out['error']}[/red]")
        if "API_KEY" in out["error"]:
            console.print("Set UNUSUAL_WHALES_API_KEY (paid plan). With the key present, --data auto also switches price data to Unusual Whales.")
        raise typer.Exit(1)
    state = "on" if e.enabled and cfg.options_flow.enabled else "off"
    console.print(f"[bold]{rep.symbol}[/bold] Unusual Whales edge score (scoring is [bold]{state}[/bold] in the strategy config; gate {'enforced' if e.gate else 'advisory'})")
    t = Table(title=None, show_lines=False)
    for c in ("Feature", "Group", "Reading", "Score", "Weight", "Contrib", "Status"):
        t.add_column(c, justify="right" if c in ("Score", "Weight", "Contrib") else "left", overflow="fold")
    for name, f in out["features"].items():
        s = f.get("score")
        colour = "green" if s is not None and s > 0.15 else "red" if s is not None and s < -0.15 else "yellow"
        status = "[dim]n/a[/dim]" if f.get("applicable") is False else f"[{colour}]ok[/]" if s is not None else f"[red]missing[/red] {f.get('error') or 'no answer'}"
        t.add_row(
            f["label"], GROUP_LABELS.get(f["group"], f["group"]), str(f.get("value") if f.get("value") is not None else "-"),
            f"[{colour}]{s:+.2f}[/]" if s is not None else "-", f"{f['weight']:g}", f"{s * f['weight']:+.2f}" if s is not None else "-", status,
        )
    console.print(t)
    score = out.get("score")
    colour = "green" if score is not None and score >= e.threshold else "red"
    console.print(
        f"Score [{colour}]{score:+.3f}[/{colour}] vs threshold {e.threshold:+.2f} · coverage {out['coverage'] * 100:.0f}% (min {e.min_coverage * 100:.0f}%) · "
        f"{out['answered']}/{out['applicable']} features · {out['calls']} requests, {out['cached']} from cache"
        if score is not None else "[red]No feature answered - score unavailable.[/red]"
    )
    console.print(describe_edge(out, cfg))
    if out.get("missing"):
        console.print("[yellow]Missing: " + ", ".join(out["missing"]) + "[/yellow]")
    raise typer.Exit(0 if (out.get("passed") if e.gate else score is not None) else 1)


# --------------------------------------------------------------------------- #
@app.command()
def daemon(
    data: str = DataOpt,
    csv_dir: str = CsvDirOpt,
    universe: Optional[str] = UniverseOpt,
    symbols: Optional[str] = SymbolsOpt,
    config: Optional[Path] = ConfigOpt,
    sentiment_csv: Optional[Path] = SentimentOpt,
    themes: bool = ThemesOpt,
    broker: str = BrokerOpt,
    state_dir: Path = StateDirOpt,
    charts: bool = ChartsOpt,
    once: Optional[str] = typer.Option(None, help="Run a single task now and exit: premarket | post_open | focused | movers | after_close | insider_scan | learn | universe (legacy schedule: intraday)"),
    show_schedule: bool = typer.Option(False, "--show-schedule", help="Print the next 7 days of scheduled tasks and exit"),
    no_universe_rebuild: bool = typer.Option(False, "--no-universe-rebuild", help="Skip the Sunday whole-market universe rebuild"),
    options_flow: Optional[bool] = FlowOpt,
    yes_live: bool = YesLiveOpt,
    verbose: bool = typer.Option(True, "-v/-q"),
):
    """Run 24/7 on the US market clock (tiered): nightly full scan arms a shortlist, pre-market gap screen,
    focused passes every few minutes, movers sweeps, Saturday unusual-options scan, weekly universe rebuild."""
    from .daemon import Daemon

    _setup_logging(verbose)
    _confirm_live(broker, yes_live)
    sess = _session(data, csv_dir, universe, symbols, config, sentiment_csv, themes, broker, state_dir, charts, options_flow)
    d = Daemon(sess, rebuild_universe=not no_universe_rebuild)
    if show_schedule:
        t = Table(title="Next 7 days (America/New_York)")
        t.add_column("When")
        t.add_column("Task")
        for when, name in d.schedule():
            t.add_row(when.strftime("%a %Y-%m-%d %H:%M"), name)
        console.print(t)
        return
    if once:
        result = d.run_task(once)
        if once == "research" and sess._research_thread:
            sess._research_thread.join()
        if isinstance(result, CycleReport):
            _print_report(sess, result)
        elif isinstance(result, dict) and once == "insider_scan":
            _print_insider(sess, result)
        elif isinstance(result, dict) and once == "learn":
            _print_learning(sess, result)
        elif result is not None:
            console.print(result)
        else:
            task = next(t for t in d.tasks if t.name == once)
            if task.last_error:
                console.print(f"[red]{task.last_error}[/red]")
                raise typer.Exit(1)
        return
    from .daemon import AlreadyRunning

    console.print(f"[bold]qmag daemon[/bold] broker={broker} state={state_dir} — Ctrl-C to stop. Status: {d.status_path}")
    try:
        d.run_forever()
    except AlreadyRunning as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


# --------------------------------------------------------------------------- #
@app.command()
def dashboard(
    host: str = typer.Option("127.0.0.1", help="Bind address"),
    port: int = typer.Option(8765, help="Port"),
    data: str = DataOpt,
    csv_dir: str = CsvDirOpt,
    universe: Optional[str] = UniverseOpt,
    symbols: Optional[str] = SymbolsOpt,
    config: Optional[Path] = ConfigOpt,
    sentiment_csv: Optional[Path] = SentimentOpt,
    themes: bool = ThemesOpt,
    broker: str = BrokerOpt,
    state_dir: Path = StateDirOpt,
    options_flow: Optional[bool] = FlowOpt,
):
    """Serve the web dashboard (regime, themes, plans with charts, positions, daemon status)."""
    import uvicorn

    from .dashboard import create_app

    sess = _session(data, csv_dir, universe, symbols, config, sentiment_csv, themes, broker, state_dir, True, options_flow)
    from .auth import PASSWORD_ENV, configured_password

    sess.reload_settings()
    if configured_password():
        console.print(f"Dashboard on http://{host}:{port}  (state: {state_dir}) — password protected")
    else:
        console.print(f"Dashboard on http://{host}:{port}  (state: {state_dir})")
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise typer.BadParameter(f"Set {PASSWORD_ENV} before binding the dashboard to {host}, or use 127.0.0.1.")
    uvicorn.run(create_app(sess), host=host, port=port, log_level="warning")


@app.command()
def status(
    data: str = DataOpt,
    csv_dir: str = CsvDirOpt,
    universe: Optional[str] = UniverseOpt,
    symbols: Optional[str] = SymbolsOpt,
    config: Optional[Path] = ConfigOpt,
    sentiment_csv: Optional[Path] = SentimentOpt,
    themes: bool = ThemesOpt,
    broker: str = BrokerOpt,
    state_dir: Path = StateDirOpt,
    options_flow: Optional[bool] = FlowOpt,
    probe: bool = typer.Option(False, "--probe", help="Actively test every enabled connection now (broker, data, context sources, LLMs)"),
    as_json: bool = typer.Option(False, "--json", help="Print the status as JSON"),
):
    """Show every connection the desk depends on: enabled, configured, working, last OK, last error.

    Missing data is never invented: a required connection that is down blocks
    new entries and shows up here (and on the dashboard's /status page).
    """
    import json as _json

    from .health import probe_connections

    sess = _session(data, csv_dir, universe, symbols, config, sentiment_csv, themes, broker, state_dir, False, options_flow)
    conn = probe_connections(sess) if probe else sess.connections()
    if as_json:
        console.print_json(_json.dumps(conn, default=str))
        return
    colour = {"ok": "green", "degraded": "yellow", "error": "red", "unknown": "yellow"}[conn["overall"]]
    headline = {"ok": "ALL SYSTEMS OK", "degraded": "DEGRADED", "error": "REQUIRED CONNECTION DOWN", "unknown": "NOT CHECKED YET"}[conn["overall"]]
    console.print(f"[bold {colour}]{headline}[/bold {colour}]  data={conn['data_source']} broker={conn['broker']}")
    if conn["data_gaps"]:
        console.print("[red]Data gaps in the last cycle:[/red]")
        for g in conn["data_gaps"]:
            console.print(f"  - {g}")
    state_style = {"ok": "green", "degraded": "yellow", "error": "red", "not_configured": "red", "unknown": "dim", "off": "dim"}
    for group in conn["groups"]:
        t = Table(title=group["label"], show_lines=False)
        for c in ("Status", "Connection", "Configured as", "Last OK", "Latency", "Items", "Last error"):
            t.add_column(c, justify="right" if c in ("Latency", "Items") else "left")
        for c in group["connections"]:
            from .dashboard import STATE_LABELS, _ago

            st = c["state"]
            t.add_row(
                f"[{state_style[st]}]{STATE_LABELS[st]}[/{state_style[st]}]",
                c["label"] + (" *" if c["required"] else ""),
                c["note"],
                _ago(c["last_ok"]) if c["enabled"] else "-",
                f"{c['latency_ms']:.0f} ms" if c["latency_ms"] is not None else "-",
                str(c["items"]) if c["items"] is not None else "-",
                (c["last_error"] or "-")[:90],
            )
        console.print(t)
    console.print("[dim]* required: no new positions are opened while it is down. Registry: " + str(sess.state_dir / "connections.json") + "[/dim]")


@app.command("broker-test")
def broker_test_cmd(
    data: str = DataOpt,
    csv_dir: str = CsvDirOpt,
    config: Optional[Path] = ConfigOpt,
    broker: str = BrokerOpt,
    state_dir: Path = StateDirOpt,
    kind: str = typer.Option("bracket", help="bracket: place + cancel a far-away 1-share buy-stop bracket (cannot fill) | fill: 1-share market buy then sell (paper only)"),
    symbol: str = typer.Option("SPY", help="Test symbol; refused if the trader holds or has armed it"),
    allow_live: bool = typer.Option(False, "--allow-live", help="Allow the fill test on a live account (buys and sells 1 real share)"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Send a test order through the broker and report every step - the same calls the trader makes."""
    from .broker_test import run_order_test

    sess = TradingSession(SessionSettings(data=data, csv_dir=csv_dir, config=config, broker=broker, state_dir=state_dir, charts=False))
    res = run_order_test(sess, kind=kind, symbol=symbol, allow_live=allow_live)
    if as_json:
        console.print_json(json.dumps(res, default=str))
        raise typer.Exit(0 if res["ok"] else 1)
    colour = "green" if res["ok"] else "red"
    console.print(f"[bold {colour}]{'PASSED' if res['ok'] else 'FAILED'}[/bold {colour}] {res['kind']} test on {res['symbol']} via {res['broker']}{' (LIVE)' if res['live'] else ''}")
    for st in res["steps"]:
        mark = "[green]✓[/green]" if st["ok"] else "[red]✗[/red]"
        console.print(f"  {mark} {st['name']}: {st['detail']}" + (f" [dim]({st['ms']} ms)[/dim]" if st.get("ms") else ""))
    if res.get("note") and res["ok"]:
        console.print(f"  {res['note']}")
    if res.get("error"):
        console.print(f"  [red]{res['error']}[/red]")
    raise typer.Exit(0 if res["ok"] else 1)


@app.command()
def halt(
    data: str = DataOpt,
    csv_dir: str = CsvDirOpt,
    config: Optional[Path] = ConfigOpt,
    broker: str = BrokerOpt,
    state_dir: Path = StateDirOpt,
    reason: str = typer.Option("", "--reason", "-r", help="Why (shown on every dashboard page and in the report)"),
    flatten: bool = typer.Option(False, "--flatten", help="Also sell every open position at market and cancel every resting order"),
    yes_live: bool = typer.Option(False, "--yes-live", help="Required to flatten a LIVE account"),
):
    """Kill switch: stop every new entry now - daemon, dashboard and CLI all honour it.

    Open positions keep their stops, targets and trails unless --flatten is given.
    Clear it with `qmag resume`.
    """
    sess = TradingSession(SessionSettings(data=data, csv_dir=csv_dir, config=config, broker=broker, state_dir=state_dir, charts=False))
    if flatten and sess.s.live and not yes_live:
        console.print("[red]--flatten on a LIVE account sells every open position at market; add --yes-live to confirm.[/red]")
        raise typer.Exit(1)
    res = sess.halt(True, reason=reason, by="cli", flatten=flatten)
    console.print(f"[bold red]TRADING HALTED[/bold red] {res['message']}")
    for f in res["flattened"]:
        r = f"{f['r_multiple']:+.2f}R" if f.get("r_multiple") is not None else "-"
        console.print(f"  sold {f['qty']} {f['symbol']} @ {f['price']:.2f} ({r})")
    console.print(f"[dim]state file {sess.state_dir / 'halt.json'} · resume with: qmag resume --state-dir {state_dir}[/dim]")


@app.command()
def resume(
    data: str = DataOpt,
    csv_dir: str = CsvDirOpt,
    config: Optional[Path] = ConfigOpt,
    broker: str = BrokerOpt,
    state_dir: Path = StateDirOpt,
):
    """Clear the kill switch set by `qmag halt` (or the dashboard); the next cycle may open positions again."""
    sess = TradingSession(SessionSettings(data=data, csv_dir=csv_dir, config=config, broker=broker, state_dir=state_dir, charts=False))
    was = sess.halted()
    if not was:
        console.print("[green]Trading was not halted; nothing to do.[/green]")
        return
    res = sess.halt(False, by="cli")
    console.print(f"[bold green]TRADING RESUMED[/bold green] (was: {was}) - {res['message']}")


@app.command()
def accounts(
    data: str = DataOpt,
    csv_dir: str = CsvDirOpt,
    config: Optional[Path] = ConfigOpt,
    broker: str = BrokerOpt,
    state_dir: Path = StateDirOpt,
    refresh: bool = typer.Option(False, "--refresh", help="Read this desk's broker now instead of showing its last snapshot"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Every account this desk and the desks in QMAG_DESKS drive: equity, cash, holdings at the latest real bar, P&L.

    Figures come from each desk's account.json (written after every cycle);
    nothing is computed for a desk that has not written one, and accounts in
    different currencies are summed separately.
    """
    sess = TradingSession(SessionSettings(data=data, csv_dir=csv_dir, config=config, broker=broker, state_dir=state_dir, charts=False))
    ov = sess.accounts(refresh=refresh)
    if as_json:
        console.print_json(json.dumps(ov, default=str))
        return
    from .dashboard import _ago

    def money(v, sign=False):
        return "-" if v is None else (f"{v:+,.0f}" if sign else f"{v:,.0f}")

    for t in ov["totals"]:
        console.print(
            f"[bold]{t['currency']}[/bold] across {t['accounts']} account{'s' if t['accounts'] != 1 else ''}: equity {money(t['equity'])}, cash {money(t['cash'])}, "
            f"stocks {money(t['market_value'])}, open P&L {money(t['unrealized'], True)}{'' if t['unrealized_known'] else ' (partial)'}, today {money(t['day_pnl'], True)}, "
            f"realised {money(t['realized_today'], True)} today / {money(t['realized_total'], True)} total"
        )
    tbl = Table(title="Desks")
    for c in ("Desk", "Broker", "State", "Daemon", "Equity", "Cash", "Open P&L", "Today", "Realised", "Positions", "Snapshot"):
        tbl.add_column(c, justify="right" if c in ("Equity", "Cash", "Open P&L", "Today", "Realised", "Positions") else "left")
    for d in ov["desks"]:
        s = d.get("snapshot") or {}
        colour = {"ok": "green", "stale": "yellow", "no_snapshot": "yellow", "error": "red", "missing": "red"}[d["state"]]
        tbl.add_row(
            d["name"] + (" *" if d["local"] else ""), (s.get("broker_name") or d["daemon"].get("broker") or "-") + (" LIVE" if s.get("live") else ""),
            f"[{colour}]{d['state'].replace('_', ' ')}[/{colour}]" + (" HALTED" if d.get("halt") else ""),
            ("running" if d["daemon"]["running"] else "not heard from") + (f" ({_ago(d['daemon']['heartbeat'])})" if d["daemon"]["heartbeat"] else ""),
            money(s.get("equity")) + (f" {s['currency']}" if s.get("currency") else ""), money(s.get("cash")), money(s.get("unrealized"), True), money(s.get("day_pnl"), True),
            money(s.get("realized_total"), True), str(len(s.get("positions") or [])), _ago(s.get("asof")) if s else "-",
        )
    console.print(tbl)
    for d in ov["desks"]:
        s = d.get("snapshot") or {}
        if not s.get("positions"):
            continue
        pt = Table(title=f"{d['name']}: holdings")
        for c in ("Symbol", "Qty", "Avg cost", "Last", "Value", "P&L", "Stop", "Target", "Note"):
            pt.add_column(c, justify="left" if c in ("Symbol", "Note") else "right")
        for p in s["positions"]:
            pt.add_row(
                p["symbol"], str(p["qty"]), f"{p['avg_cost']:.2f}", f"{p['last']:.2f}" if p.get("last") is not None else "-", money(p.get("market_value")),
                (f"{p['unrealized']:+,.0f} ({p['unrealized_pct'] * 100:+.1f}%)" if p.get("unrealized") is not None else "-"),
                f"{p['stop']:.2f}" if p.get("stop") is not None else "-", f"{p['target']:.2f}" if p.get("target") else "-", p.get("note") or ("managed" if p.get("managed") else ""),
            )
        console.print(pt)
    for prob in ov["problems"]:
        console.print(f"[yellow]{prob}[/yellow]")
    console.print("[dim]* this desk · other desks come from QMAG_DESKS (settings page → Accounts) · add one with deploy/add-desk.sh[/dim]")


@app.command()
def advise(
    message: Optional[str] = typer.Argument(None, help="What you want, in plain English (omit to only list pending proposals)"),
    data: str = DataOpt,
    csv_dir: str = CsvDirOpt,
    config: Optional[Path] = ConfigOpt,
    broker: str = BrokerOpt,
    state_dir: Path = StateDirOpt,
    apply: Optional[str] = typer.Option(None, "--apply", help="Comma-separated proposal ids to apply (or 'all' for every pending one)"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Ask the desk advisor and review its proposed setting changes; nothing is applied without --apply.

    The same advisor as the dashboard page: it sees the settings map and the
    desk's state, answers in plain English and proposes exact setting
    changes. `qmag advise --apply ID,ID` applies accepted proposals.
    """
    from . import advisor as adv

    sess = TradingSession(SessionSettings(data=data, csv_dir=csv_dir, config=config, broker=broker, state_dir=state_dir, charts=False))
    out: dict = {}
    if message:
        ex = adv.ask_advisor(sess, message)
        out["exchange"] = ex
        if not as_json:
            if ex.get("error"):
                console.print(f"[red]The model did not answer: {ex['error']}[/red]")
            else:
                console.print(f"[bold]Advisor[/bold] ({ex['provider']} / {ex['model']}):\n{ex['reply']}\n")
                if ex.get("risk_note"):
                    console.print(f"[dim]Risk: {ex['risk_note']}[/dim]")
                for q in ex.get("questions") or []:
                    console.print(f"[yellow]? {q}[/yellow]")
    if apply:
        pending = adv.pending_changes(adv.load_transcript(sess))
        ids = [c["id"] for c in pending] if apply.strip().lower() == "all" else [i.strip() for i in apply.split(",") if i.strip()]
        result = adv.apply_changes(sess, ids, by="qmag advise")
        out["apply"] = result
        if not as_json:
            for a in result["applied"]:
                console.print(f"[green]applied[/green] {a['key']}: {a['was']} -> {a['value']}")
            for s_ in result["skipped"]:
                console.print(f"[yellow]skipped[/yellow] {s_['key']}: {s_['reason']}")
    pending = adv.pending_changes(adv.load_transcript(sess))
    out["pending"] = pending
    if as_json:
        console.print_json(json.dumps(out, default=str))
        return
    if pending:
        tbl = Table(title="Pending proposals (apply with --apply ID,ID or --apply all)")
        for c in ("Id", "Setting", "Now", "Proposed", "Why"):
            tbl.add_column(c)
        for c in pending:
            tbl.add_row(c["id"], c["key"], str(c.get("current")), str(c.get("value")), c.get("why") or "")
        console.print(tbl)
    elif not message:
        console.print("[dim]No pending proposals. Ask something: qmag advise \"risk half as much per trade\"[/dim]")


@app.command("alert-test")
def alert_test(
    data: str = DataOpt,
    csv_dir: str = CsvDirOpt,
    config: Optional[Path] = ConfigOpt,
    broker: str = BrokerOpt,
    state_dir: Path = StateDirOpt,
):
    """Send a test alert over every configured channel (Telegram bot / webhook) and show what answered."""
    sess = TradingSession(SessionSettings(data=data, csv_dir=csv_dir, config=config, broker=broker, state_dir=state_dir, charts=False))
    res = sess.alerts.test()
    if res["ok"]:
        console.print(f"[bold green]DELIVERED[/bold green] to {' + '.join(res['channels'])}")
        return
    console.print(f"[bold red]NOT DELIVERED[/bold red] {res['error']}")
    if not res["channels"]:
        console.print("[dim]Configure TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID and/or QMAG_ALERT_WEBHOOK_URL on the dashboard's settings page (or in settings.env).[/dim]")
    raise typer.Exit(1)


@app.command("alert")
def alert_send(
    title: str = typer.Argument(..., help="Alert title (one line)."),
    body: str = typer.Argument("", help="Optional body text."),
    level: str = typer.Option("info", "--level", help="info | warn | error"),
    data: str = DataOpt,
    csv_dir: str = CsvDirOpt,
    config: Optional[Path] = ConfigOpt,
    broker: str = BrokerOpt,
    state_dir: Path = StateDirOpt,
):
    """Push one alert over the configured channels (Telegram / webhook) - for scripts and cron jobs, e.g. the tunnel watcher telling you the dashboard's address changed."""
    sess = TradingSession(SessionSettings(data=data, csv_dir=csv_dir, config=config, broker=broker, state_dir=state_dir, charts=False))
    res = sess.alerts.send(title, body, level=level, wait=True)
    if res is None:
        console.print("[bold yellow]NO CHANNEL[/bold yellow] configure TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID and/or QMAG_ALERT_WEBHOOK_URL on the settings page (or in settings.env).")
        raise typer.Exit(2)
    if not res:
        console.print("[bold red]NOT DELIVERED[/bold red] see the connections page (alerts) for the error")
        raise typer.Exit(1)
    console.print(f"[bold green]DELIVERED[/bold green] to {' + '.join(sess.alerts.channels())}")


@paper_app.command("status")
def paper_status(state_dir: Path = typer.Option(Path("paper_state"))):
    """Show the paper ledger and closed trades."""
    from .broker import PaperBroker

    ledger_path = state_dir / "ledger.json"
    if not ledger_path.exists():
        console.print("No paper state yet. Run `qmag paper run` first.")
        raise typer.Exit()
    brk = PaperBroker(ledger_path)
    acct = brk.account()
    console.print(f"Equity ${acct.equity:,.0f} | cash ${acct.cash:,.0f} | positions {len(brk.positions())} | open orders {len(brk.open_orders())}")
    state = TraderState.load(state_dir / "trader.json")
    if state.closed:
        t = Table(title="Closed trades (trader view)")
        for c in ("Symbol", "Setup", "Entry", "Entry px", "Closed", "Reason"):
            t.add_column(c)
        for r in state.closed[-20:]:
            t.add_row(r["symbol"], r["setup"], r["entry_date"], f"{r['entry_price']:.2f}", r.get("closed_on", ""), r.get("exit_reason", "stop/manual"))
        console.print(t)


@app.command("replay")
def replay_command(
    intraday_dir: Path = typer.Option(..., help="One SYMBOL.csv per stock; timezone-aware timestamp, OHLCV"),
    daily_dir: Path = typer.Option(..., help="Daily OHLCV CSV directory for indicator warmup"),
    output: Path = typer.Option(..., help="Fresh experiment directory"),
    config: Optional[Path] = ConfigOpt,
    bar_minutes: int = typer.Option(1),
    context_file: Optional[Path] = typer.Option(None, help="JSON array of timestamped historical context events"),
):
    """Replay real intraday bars through the same execution / strategy loop offline."""
    from .replay import load_intraday, replay
    frames = load_intraday(intraday_dir)
    provider = make_provider("csv", directory=str(daily_dir))
    history = provider.load(list(frames))
    cfg = build_config(config, None, True, {})
    events = json.loads(context_file.read_text(encoding="utf-8")) if context_file else None
    result = replay(frames, history, cfg, output, bar_minutes, events)
    console.print(f"Replayed {result['observations']} observations. Artifact: {output / 'replay.json'}")


@app.command("operations")
def operations_command(state_dir: Path = StateDirOpt):
    """Read structured status for humans and agents without contacting a broker."""
    from .operations import status
    print(json.dumps(status(state_dir), indent=2))


@app.command("housekeeping")
def housekeeping_command(state_dir: Path = StateDirOpt):
    """Back up execution / research state; exclude credentials and preserve journals."""
    from .operations import housekeeping
    print(json.dumps(housekeeping(state_dir), indent=2))


@app.command("autopilot")
def autopilot_command(
    broker: str = BrokerOpt, data: str = DataOpt, state_dir: Path = StateDirOpt,
    config: Optional[Path] = ConfigOpt, port: int = typer.Option(8765),
    yes_live: bool = YesLiveOpt,
):
    """Supervise the scheduler and local dashboard together; restart failed services."""
    from .supervisor import run
    _confirm_live(broker, yes_live)
    run(broker=broker, data=data, state_dir=state_dir, config=config, port=port, live_confirmed=broker in LIVE_BROKERS)


if __name__ == "__main__":
    app()
