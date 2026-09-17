"""Market-sentiment regime: is the market paying momentum right now?

Combines up to three gates from ``RegimeFilter``:

* benchmark trend  – QQQ close above its N-day SMA
* breadth          – share of the scan universe above its N-day SMA
* volatility       – VIX below a ceiling (only if ``^VIX`` is loaded)

``regime_series`` returns one boolean per date using data *through that
date* (i.e. what you know after the close). The backtester shifts it by one
day before gating entries; the trader/scan use the last value directly since
they run after the close.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .config import StrategyConfig


@dataclass
class RegimeSnapshot:
    ok: bool
    benchmark_ok: bool | None
    breadth: float | None
    breadth_ok: bool | None
    vix: float | None
    vix_ok: bool | None
    missing: list[str] = field(default_factory=list)  # enabled gates whose data was absent or stale

    @property
    def known(self) -> bool:
        """False when an enabled gate could not be evaluated; ``ok`` is then False
        too - an unknown regime is never assumed to be risk-on."""
        return not self.missing

    def describe(self, cfg: StrategyConfig) -> str:
        parts = []
        r = cfg.regime
        if self.benchmark_ok is not None:
            parts.append(f"{r.benchmark} {'>' if self.benchmark_ok else '<'} {r.ma_length}d MA")
        if self.breadth is not None:
            parts.append(f"breadth {self.breadth:.0%} (min {r.min_breadth:.0%})")
        if self.vix is not None:
            parts.append(f"VIX {self.vix:.1f} (max {r.max_vix})")
        if self.missing:
            parts.append("REGIME UNKNOWN - missing data: " + "; ".join(self.missing) + " (treated as risk-off, nothing assumed)")
        return ", ".join(parts) if parts else "no regime inputs"


def market_breadth(data: dict[str, pd.DataFrame], cfg: StrategyConfig) -> pd.Series:
    """Fraction of tradeable symbols closing above their breadth SMA, per date."""
    aux = cfg.auxiliary_symbols
    series = {s: df["close"] for s, df in data.items() if s not in aux and not df.empty}
    if not series:
        return pd.Series(dtype=float)
    closes = pd.concat(series, axis=1).sort_index()
    if closes.empty:
        return pd.Series(dtype=float)
    ma = closes.rolling(cfg.regime.breadth_ma_length, min_periods=cfg.regime.breadth_ma_length).mean()
    above = (closes > ma).astype(float).where(closes.notna() & ma.notna())
    return above.mean(axis=1)


def regime_series(data: dict[str, pd.DataFrame], cfg: StrategyConfig) -> tuple[pd.Series, dict[str, bool]]:
    """Boolean regime per date plus which gates were actually active."""
    r = cfg.regime
    dates = pd.DatetimeIndex(sorted(set().union(*[set(df.index) for df in data.values()])))
    ok = pd.Series(True, index=dates)
    active = {"benchmark": False, "breadth": False, "vix": False}
    if not r.enabled:
        return ok, active

    bench = data.get(r.benchmark)
    if bench is not None:
        ma = bench["close"].rolling(r.ma_length, min_periods=r.ma_length).mean()
        gate = (bench["close"] > ma).reindex(dates).fillna(False)
        ok &= gate.astype(bool)
        active["benchmark"] = True
    else:
        ok[:] = False

    if r.breadth_enabled:
        breadth = market_breadth(data, cfg).reindex(dates)
        ok &= (breadth >= r.min_breadth).fillna(False).astype(bool)
        if breadth.notna().any():
            active["breadth"] = True

    if r.max_vix is not None:
        vix = data.get(r.vix_symbol)
        if vix is not None:
            gate = (vix["close"] < r.max_vix).reindex(dates).fillna(False)
            ok &= gate.astype(bool)
            active["vix"] = True
        else:
            ok[:] = False

    return ok, active


def regime_snapshot(data: dict[str, pd.DataFrame], cfg: StrategyConfig, max_stale_days: int = 4) -> RegimeSnapshot:
    """Current regime with the underlying readings, for reports.

    Every enabled gate must have current data. A missing benchmark, a
    benchmark / VIX series that stopped ``max_stale_days`` before the latest
    bar in ``data``, or too little history for the moving average is
    reported in ``missing`` and the regime is *unknown* (``ok`` False)
    rather than quietly passed.
    """
    r = cfg.regime
    if not r.enabled:
        return RegimeSnapshot(True, None, None, None, None, None)
    ok_series, active = regime_series(data, cfg)
    bench_ok = breadth = breadth_ok = vix = vix_ok = None
    missing: list[str] = []
    latest = max(df.index[-1] for df in data.values()) if data else None

    def _stale(sym: str) -> str | None:
        last = data[sym].index[-1]
        if latest is not None and last < latest - pd.Timedelta(days=max_stale_days):
            return f"{sym} last bar {last.date()} vs {latest.date()}"
        return None

    if active["benchmark"]:
        b = data[r.benchmark]["close"]
        ma = b.rolling(r.ma_length, min_periods=r.ma_length).mean().iloc[-1]
        if pd.isna(ma):
            missing.append(f"{r.benchmark} has fewer than {r.ma_length} bars")
        else:
            bench_ok = bool(b.iloc[-1] > ma)
        if (why := _stale(r.benchmark)) is not None:
            missing.append(why)
    else:
        missing.append(f"{r.benchmark} bars not loaded")
    if r.breadth_enabled:
        if active["breadth"]:
            breadth = float(market_breadth(data, cfg).dropna().iloc[-1])
            breadth_ok = breadth >= r.min_breadth
        else:
            missing.append("breadth (no universe symbols with enough history)")
    if r.max_vix is not None:
        if active["vix"]:
            vix = float(data[r.vix_symbol]["close"].iloc[-1])
            vix_ok = vix < r.max_vix
            if (why := _stale(r.vix_symbol)) is not None:
                missing.append(why)
        else:
            missing.append(f"{r.vix_symbol} bars not loaded")
    ok = bool(ok_series.iloc[-1]) and not missing if len(ok_series) else False
    return RegimeSnapshot(ok, bench_ok, breadth, breadth_ok, vix, vix_ok, missing)
