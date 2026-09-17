"""Daily-bar, portfolio-level backtester for the Qullamaggie playbook.

Entry: a completed signal on day t fills at the next available session open,
       subject to the configured gap, cash and risk limits. legacy_intrabar
       is an explicit comparison-only option with same-bar timing bias.
Exits:  1. stop hit intraday (gap-through fills at the open)
        2. partial profit: ``partial_fraction`` comes off at the
           ``partial_target_r`` limit if price gets there, otherwise after
           ``partial_after_days`` bars if the trade is green; either way the
           stop is lifted to breakeven
        3. once the partial is done, sell the rest on a close below the
           ``trail_ma``-day SMA
        4. ``max_hold_days`` safety valve
Regime: no new entries while the benchmark closed below its SMA yesterday.

Conservative conventions: the entry-day low is checked against the stop
(as if the low printed after the fill), and slippage is applied to every
fill. Close-dependent exits execute at the next available open. Intraday
stop/target ordering and breakeven timing remain daily-bar assumptions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable

import numpy as np
import pandas as pd

from .config import StrategyConfig
from .indicators import enrich
from .plan import partial_quantity, size_shares
from .regime import regime_series
from .sentiment import attach_sentiment_columns
from .setups import BreakoutDetector, EpisodicPivotDetector, SetupDetector, Signal, detect_signals
from .themes import attach_theme_columns

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
@dataclass
class Position:
    symbol: str
    setup: str
    entry_date: pd.Timestamp
    entry_price: float
    shares: int
    stop: float
    initial_risk: float
    target: float | None = None
    remaining: int = field(init=False)
    partial_done: bool = False
    bars_held: int = 0
    mfe_r: float = 0.0  # best open profit seen, in R (for the time stop)
    realised: float = 0.0
    exits: list[tuple[pd.Timestamp, int, float, str]] = field(default_factory=list)
    pivot: float | None = None
    theme: str | None = None
    pending_exit: tuple[int, str] | None = None

    def __post_init__(self) -> None:
        self.remaining = self.shares


@dataclass
class Trade:
    symbol: str
    setup: str
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    avg_exit_price: float
    shares: int
    pnl: float
    pnl_pct: float
    r_multiple: float
    bars_held: int
    exit_reason: str
    partial_taken: bool = False

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "setup": self.setup,
            "entry_date": self.entry_date.date().isoformat(),
            "exit_date": self.exit_date.date().isoformat(),
            "entry_price": round(self.entry_price, 4),
            "avg_exit_price": round(self.avg_exit_price, 4),
            "shares": self.shares,
            "pnl": round(self.pnl, 2),
            "pnl_pct": round(self.pnl_pct * 100, 2),
            "r_multiple": round(self.r_multiple, 2),
            "bars_held": self.bars_held,
            "exit_reason": self.exit_reason,
            "partial_taken": self.partial_taken,
        }


@dataclass
class BacktestResult:
    equity: pd.Series
    trades: list[Trade]
    config: StrategyConfig
    signals_seen: int
    signals_taken: int
    regime_active: bool
    regime_gates: dict[str, bool]
    start: pd.Timestamp
    end: pd.Timestamp

    @property
    def trades_frame(self) -> pd.DataFrame:
        if not self.trades:
            return pd.DataFrame(columns=list(Trade.__dataclass_fields__))
        return pd.DataFrame([t.to_dict() for t in self.trades])

    execution_model: str = "next_open"

    def metrics(self) -> dict[str, float]:
        return compute_metrics(self.equity, self.trades, start_equity=self.config.risk.starting_equity)


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def compute_metrics(equity: pd.Series, trades: list[Trade], start_equity: float | None = None) -> dict[str, float]:
    if equity.empty:
        return {}
    start_eq = float(equity.iloc[0]) if start_equity is None else float(start_equity)
    end_eq = float(equity.iloc[-1])
    if start_equity is not None:
        equity = pd.concat([pd.Series([start_eq], index=[equity.index[0] - pd.Timedelta(days=1)]), equity])
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-9)
    total_return = end_eq / start_eq - 1
    cagr = (end_eq / start_eq) ** (1 / years) - 1 if years > 0.05 else total_return
    rets = equity.pct_change().dropna()
    sharpe = float(np.sqrt(252) * rets.mean() / rets.std()) if len(rets) > 2 and rets.std() > 0 else 0.0
    running_max = equity.cummax()
    drawdown = equity / running_max - 1
    max_dd = float(drawdown.min())

    pnls = np.array([t.pnl for t in trades])
    rs = np.array([t.r_multiple for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    win_rate = len(wins) / len(pnls) if len(pnls) else 0.0
    profit_factor = float(wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() != 0 else float("inf") if len(wins) else 0.0
    return {
        "start_equity": start_eq,
        "end_equity": end_eq,
        "total_return": total_return,
        "cagr": cagr,
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "calmar": (cagr / abs(max_dd)) if max_dd < 0 else 0.0,
        "trades": float(len(trades)),
        "win_rate": win_rate,
        "avg_r": float(rs.mean()) if len(rs) else 0.0,
        "expectancy_r": float(rs.mean()) if len(rs) else 0.0,
        "profit_factor": profit_factor,
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "avg_bars_held": float(np.mean([t.bars_held for t in trades])) if trades else 0.0,
    }


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
def default_detectors() -> list[SetupDetector]:
    return [BreakoutDetector(), EpisodicPivotDetector()]


def prepare_data(raw: dict[str, pd.DataFrame], cfg: StrategyConfig) -> dict[str, pd.DataFrame]:
    """Enrich every frame with indicators, then theme and sentiment context."""
    b, m = cfg.breakout, cfg.management
    data = {sym: enrich(df, b.fast_ma, b.slow_ma, m.trail_ma) for sym, df in raw.items() if len(df) > 0}
    if cfg.themes.enabled:
        data = attach_theme_columns(data, cfg)
    if cfg.sentiment.enabled:
        data = attach_sentiment_columns(data, cfg)
    return data


def collect_signals(
    data: dict[str, pd.DataFrame], cfg: StrategyConfig, detectors: Iterable[SetupDetector] | None = None
) -> dict[pd.Timestamp, list[Signal]]:
    detectors = list(detectors) if detectors is not None else default_detectors()
    by_date: dict[pd.Timestamp, list[Signal]] = {}
    for sym, df in data.items():
        if sym in cfg.auxiliary_symbols:
            continue
        for sig in detect_signals(sym, df, cfg, detectors):
            by_date.setdefault(sig.date, []).append(sig)
    return by_date


def run_backtest(
    raw: dict[str, pd.DataFrame],
    cfg: StrategyConfig,
    start: str | None = None,
    end: str | None = None,
    detectors: Iterable[SetupDetector] | None = None,
    execution_model: str = "next_open",
) -> BacktestResult:
    if execution_model not in ("next_open", "legacy_intrabar"):
        raise ValueError("execution_model must be next_open or legacy_intrabar")
    data = prepare_data(raw, cfg)
    if not data:
        raise ValueError("No price data supplied")

    # Regime is known after the close, so it gates the *next* day's entries.
    regime_ok, regime_gates = regime_series(data, cfg)
    regime_active = cfg.regime.enabled
    if cfg.regime.enabled and cfg.regime.benchmark not in data:
        log.warning("Regime filter enabled but benchmark %s not in data; new entries blocked", cfg.regime.benchmark)
    regime_ok = regime_ok.shift(1).fillna(False).astype(bool)

    signals_by_date = collect_signals(data, cfg, detectors)
    if execution_model == "next_open":
        # A completed daily bar's volume and closing price cannot authorize a
        # retrospective fill at that same bar's pivot. Execute next session.
        delayed = {}
        for found_on, signals in signals_by_date.items():
            for signal in signals:
                future = data[signal.symbol].index[data[signal.symbol].index > found_on]
                if len(future):
                    delayed.setdefault(future[0], []).append(signal)
        signals_by_date = delayed
    all_dates = sorted(set().union(*[set(df.index) for df in data.values()]))
    dates = pd.DatetimeIndex(all_dates)
    if start:
        dates = dates[dates >= pd.Timestamp(start)]
    if end:
        dates = dates[dates <= pd.Timestamp(end)]
    if len(dates) == 0:
        raise ValueError("No trading days in the requested window")

    risk, mgmt = cfg.risk, cfg.management
    slip = cfg.risk.slippage_bps / 10_000
    trail_col = f"sma_{mgmt.trail_ma}"

    cash = risk.starting_equity
    positions: dict[str, Position] = {}
    trades: list[Trade] = []
    equity_curve: list[float] = []
    signals_seen = signals_taken = 0

    def close_out(pos: Position, date: pd.Timestamp, qty: int, price: float, reason: str) -> None:
        nonlocal cash
        fill = price * (1 - slip)
        proceeds = fill * qty - risk.commission_per_share * qty
        cash += proceeds
        pos.realised += proceeds
        pos.remaining -= qty
        pos.exits.append((date, qty, fill, reason))
        if pos.remaining == 0:
            cost = pos.entry_price * pos.shares + risk.commission_per_share * pos.shares
            pnl = pos.realised - cost
            avg_exit = pos.realised / pos.shares
            trades.append(
                Trade(
                    symbol=pos.symbol,
                    setup=pos.setup,
                    entry_date=pos.entry_date,
                    exit_date=date,
                    entry_price=pos.entry_price,
                    avg_exit_price=avg_exit,
                    shares=pos.shares,
                    pnl=pnl,
                    pnl_pct=pnl / cost,
                    r_multiple=pnl / (pos.initial_risk * pos.shares),
                    bars_held=pos.bars_held,
                    exit_reason=reason,
                    partial_taken=len(pos.exits) > 1,
                )
            )
            del positions[pos.symbol]

    for date in dates:
        # Completed-close decisions execute at the next available open.
        for sym in list(positions):
            pos = positions[sym]
            if pos.pending_exit and date in data[sym].index:
                qty, reason = pos.pending_exit
                pos.pending_exit = None
                close_out(pos, date, min(qty, pos.remaining), float(data[sym].loc[date, "open"]), reason)
                if sym in positions and reason == "partial":
                    pos.partial_done = True
                    if mgmt.move_stop_to_breakeven:
                        pos.stop = max(pos.stop, pos.entry_price)

        # ---- 2. new entries ---------------------------------------------
        todays = signals_by_date.get(date, [])
        signals_seen += len(todays)
        allowed = True
        if regime_active:
            allowed = bool(regime_ok.get(date, False))
        if allowed and todays:
            # Size off yesterday's mark-to-market equity (no lookahead).
            mtm = cash + sum(p.remaining * (float(data[s].loc[date, "open"]) if date in data[s].index else float(data[s]["close"].asof(date))) for s, p in positions.items())
            for sig in sorted(todays, key=lambda s: s.score, reverse=True):
                if len(positions) >= risk.max_positions or sig.symbol in positions:
                    continue
                signal_row = data[sig.symbol].loc[sig.date]
                theme = signal_row.get("theme")
                theme = theme if isinstance(theme, str) else None
                if theme and risk.max_positions_per_theme > 0 and sum(p.theme == theme for p in positions.values()) >= risk.max_positions_per_theme:
                    continue
                entry_price = float(data[sig.symbol].loc[date, "open"]) if execution_model == "next_open" else sig.entry
                if execution_model == "next_open" and (entry_price <= sig.stop or entry_price > sig.pivot * (1 + cfg.breakout.max_gap_pct)):
                    continue
                fill = entry_price * (1 + slip)
                per_share_risk = fill - sig.stop
                if per_share_risk <= 0:
                    continue
                exposure = sum(p.remaining * (float(data[s].loc[date, "open"]) if date in data[s].index else float(data[s]["close"].asof(date))) for s, p in positions.items())
                shares = size_shares(mtm, fill, sig.stop, cfg, exposure, cash)
                if risk.max_portfolio_heat_pct > 0:
                    heat = sum(max(p.entry_price - p.stop, 0) * p.remaining for p in positions.values())
                    shares = min(shares, int(max(0, mtm * risk.max_portfolio_heat_pct - heat) // per_share_risk))
                if shares < 1:
                    continue
                cost = shares * fill + risk.commission_per_share * shares
                cash -= cost
                pos = Position(
                    symbol=sig.symbol,
                    setup=sig.setup,
                    entry_date=date,
                    entry_price=fill,
                    shares=shares,
                    stop=sig.stop,
                    initial_risk=per_share_risk,
                    pivot=sig.pivot,
                    theme=theme,
                    target=(fill + mgmt.partial_target_r * per_share_risk) if mgmt.partial_target_r else None,
                )
                positions[sig.symbol] = pos
                signals_taken += 1

        # ---- 1. manage open positions ------------------------------------
        for sym in list(positions):
            pos = positions[sym]
            df = data[sym]
            if date not in df.index:
                continue
            row = df.loc[date]
            pos.bars_held += int(date > pos.entry_date)
            if pos.initial_risk > 0:
                pos.mfe_r = max(pos.mfe_r, (float(row["high"]) - pos.entry_price) / pos.initial_risk)

            if row["open"] <= pos.stop:
                close_out(pos, date, pos.remaining, float(row["open"]), "stop_gap")
                continue
            if row["low"] <= pos.stop:
                close_out(pos, date, pos.remaining, pos.stop, "stop")
                continue

            # Profit-taking plan: a resting limit at +N R takes the partial
            # whenever price gets there; otherwise the 3-5 day rule applies.
            if not pos.partial_done and pos.target is not None and row["high"] >= pos.target:
                qty = partial_quantity(pos.remaining, mgmt.partial_fraction)
                if qty > 0:
                    close_out(pos, date, qty, max(float(row["open"]), pos.target), "partial_target")
                pos.partial_done = True
                if mgmt.move_stop_to_breakeven:
                    pos.stop = max(pos.stop, pos.entry_price)
                continue

            en = cfg.entry
            if en.failed_breakout_exit and pos.pivot and not pos.partial_done and pos.bars_held <= en.failed_breakout_days:
                floor = pos.pivot * (1 - en.failed_breakout_tolerance_pct)
                if row["close"] < floor and pos.stop < floor:
                    pos.pending_exit = (pos.remaining, "failed_breakout")
                    continue

            if (
                mgmt.time_stop_days > 0 and not pos.partial_done and pos.bars_held >= mgmt.time_stop_days
                and float(row["close"]) <= pos.entry_price and pos.mfe_r < mgmt.time_stop_min_mfe_r
            ):
                pos.pending_exit = (pos.remaining, "time_stop")
                continue

            if not pos.partial_done and pos.bars_held >= mgmt.partial_after_days:
                if row["close"] > pos.entry_price and mgmt.partial_fraction > 0:
                    qty = partial_quantity(pos.remaining, mgmt.partial_fraction)
                    if qty > 0:
                        pos.pending_exit = (qty, "partial")
                    continue

            trail = row.get(trail_col, np.nan)
            if pos.partial_done and not np.isnan(trail) and row["close"] < trail:
                pos.pending_exit = (pos.remaining, "trail_ma")
                continue
            if pos.bars_held >= mgmt.max_hold_days:
                pos.pending_exit = (pos.remaining, "max_hold")

        # ---- 3. mark to market -------------------------------------------
        value = cash
        for s, pos in positions.items():
            df = data[s]
            px = df["close"].asof(date)
            value += pos.remaining * float(px)
        equity_curve.append(value)

    equity = pd.Series(equity_curve, index=dates, name="equity")
    return BacktestResult(
        equity=equity,
        execution_model=execution_model,
        trades=trades,
        config=cfg,
        signals_seen=signals_seen,
        signals_taken=signals_taken,
        regime_active=regime_active,
        regime_gates=regime_gates,
        start=dates[0],
        end=dates[-1],
    )
