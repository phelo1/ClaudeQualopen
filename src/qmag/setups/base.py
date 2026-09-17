from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np
import pandas as pd

from ..config import StrategyConfig


@dataclass(frozen=True)
class Signal:
    """A tradeable trigger on a given bar.

    ``entry`` is the estimated fill (pivot plus buffer, or the open if the
    stock gapped through the pivot). ``stop`` is the initial protective stop.
    ``pivot`` is the level a live buy-stop order should sit at.
    """

    symbol: str
    date: pd.Timestamp
    setup: str
    pivot: float
    entry: float
    stop: float
    adr_dollar: float
    adr_pct: float
    score: float
    details: dict = field(default_factory=dict, compare=False)

    @property
    def risk_per_share(self) -> float:
        return max(self.entry - self.stop, 1e-9)


class SetupDetector(Protocol):
    name: str

    def detect(self, symbol: str, df: pd.DataFrame, cfg: StrategyConfig) -> list[Signal]: ...

    def detect_last(self, symbol: str, df: pd.DataFrame, cfg: StrategyConfig) -> Signal | None: ...

    def watchlist(self, symbol: str, df: pd.DataFrame, cfg: StrategyConfig) -> Signal | None: ...


def momentum_ok(row: pd.Series, cfg: StrategyConfig) -> bool:
    """Is this stock one of the market's momentum leaders as of ``row``?"""
    m = cfg.momentum
    gains = (row.get("gain_1m"), row.get("gain_3m"), row.get("gain_6m"))
    if any(g is None or np.isnan(g) for g in gains):
        return False
    g1, g3, g6 = gains
    if not (g1 >= m.min_gain_1m or g3 >= m.min_gain_3m or g6 >= m.min_gain_6m):
        return False
    if np.isnan(row.get("adr_pct", np.nan)) or row["adr_pct"] < m.min_adr_pct:
        return False
    if row["close"] < m.min_price:
        return False
    if np.isnan(row.get("dollar_vol_20", np.nan)) or row["dollar_vol_20"] < m.min_dollar_volume:
        return False
    return True


def _num(row: pd.Series, key: str, default: float = 0.0) -> float:
    val = row.get(key, np.nan)
    try:
        return default if val is None or np.isnan(val) else float(val)
    except TypeError:
        return default


def context_ok(row: pd.Series, cfg: StrategyConfig, setup: str | None = None) -> bool:
    """Theme and sentiment gates shared by every setup (theme gate honours ``themes.apply_to``)."""
    from ..sentiment import sentiment_ok
    from ..themes import theme_ok

    return theme_ok(row, cfg, setup) and sentiment_ok(row, cfg)


def context_score(row: pd.Series, cfg: StrategyConfig) -> float:
    """Ranking bonus from theme strength and sentiment; 0 when neither is present."""
    bonus = 0.0
    if cfg.themes.enabled:
        bonus += cfg.themes.score_weight * _num(row, "theme_pct")
    if cfg.sentiment.enabled:
        bonus += cfg.sentiment.score_weight * _num(row, "sentiment")
    return bonus


def momentum_score(row: pd.Series, cfg: StrategyConfig | None = None) -> float:
    """Composite used to rank candidates when there are more signals than slots.

    Stock momentum plus, when enabled, the strength of its theme and its
    sentiment reading - so with two equally good flags the one in the hotter
    group gets the slot.
    """
    base = _num(row, "gain_1m") + 0.5 * _num(row, "gain_3m") + 0.25 * _num(row, "gain_6m") + _num(row, "adr_pct") / 100.0
    return float(base + (context_score(row, cfg) if cfg is not None else 0.0))


def detect_signals(symbol: str, df: pd.DataFrame, cfg: StrategyConfig, detectors: list[SetupDetector]) -> list[Signal]:
    signals: list[Signal] = []
    for det in detectors:
        signals.extend(det.detect(symbol, df, cfg))
    signals.sort(key=lambda s: s.date)
    return signals
