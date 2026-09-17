"""Episodic pivot (EP): a large gap up on huge volume, usually on earnings or
another fundamental catalyst, in a stock that has been *neglected* (flat or
down) rather than already extended.

Daily-bar translation:

* gap = open / previous close - 1  >= min_gap_pct
* volume >= min_volume_ratio * 50-day average volume
  (full-day volume is used as a proxy for the opening volume surge; when the
  detector is run intraday on a partial bar it becomes a genuine live filter)
* prior 3-month gain <= max_prior_gain_3m  (not already a runner)
* entry: opening-range-high break, approximated as open + buffer
* stop: entry - k * ADR (approximates the low of day)

EPs are allowed to skip the momentum-leader filter because by definition the
stock is *not* a leader yet — that is the point of the setup.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import StrategyConfig
from .base import Signal, context_ok, context_score
from .breakout import _theme_details


class EpisodicPivotDetector:
    name = "episodic_pivot"

    def _signal_at(self, symbol: str, df: pd.DataFrame, t: int, cfg: StrategyConfig) -> Signal | None:
        ep = cfg.episodic_pivot
        row = df.iloc[t]
        prev = df.iloc[t - 1]
        gap = row["gap_pct"]
        rvol = row["rvol_50"]
        if np.isnan(gap) or np.isnan(rvol) or np.isnan(prev["gain_3m"]):
            return None
        if gap < ep.min_gap_pct or rvol < ep.min_volume_ratio:
            return None
        if prev["gain_3m"] > ep.max_prior_gain_3m:
            return None
        if row["close"] < cfg.momentum.min_price:
            return None
        if np.isnan(prev["dollar_vol_20"]) or prev["dollar_vol_20"] * rvol < cfg.momentum.min_dollar_volume:
            return None
        if not context_ok(prev, cfg, self.name):
            return None

        entry = row["open"] * (1 + ep.entry_buffer_pct)
        if row["high"] < entry:
            return None
        # The gap itself widens the range; use the larger of ADR and a slice
        # of the gap so the stop is not absurdly tight on a 30 % gapper.
        adr_d = float(prev["adr_dollar"]) if not np.isnan(prev["adr_dollar"]) else 0.0
        risk = max(cfg.management.stop_adr_mult * adr_d, 0.25 * (row["open"] - prev["close"]))
        if risk <= 0:
            return None
        return Signal(
            symbol=symbol,
            date=df.index[t],
            setup=self.name,
            pivot=float(row["open"]),
            entry=float(entry),
            stop=float(entry - risk),
            adr_dollar=adr_d,
            adr_pct=float(prev["adr_pct"]) if not np.isnan(prev["adr_pct"]) else 0.0,
            # EPs rank by the size of the surprise (gap x relative volume),
            # nudged by theme strength / sentiment when those are enabled.
            score=float(gap * rvol + context_score(prev, cfg)),
            details={**_theme_details(prev), "gap_pct": float(gap) * 100, "rvol": float(rvol), "prior_gain_3m": float(prev["gain_3m"]) * 100},
        )

    def detect(self, symbol: str, df: pd.DataFrame, cfg: StrategyConfig) -> list[Signal]:
        if not cfg.episodic_pivot.enabled or len(df) < cfg.warmup_bars:
            return []
        gap = df["gap_pct"].to_numpy()
        candidates = np.flatnonzero(gap >= cfg.episodic_pivot.min_gap_pct)
        out: list[Signal] = []
        for t in candidates:
            if t < cfg.warmup_bars:
                continue
            sig = self._signal_at(symbol, df, int(t), cfg)
            if sig is not None:
                out.append(sig)
        return out

    def detect_last(self, symbol: str, df: pd.DataFrame, cfg: StrategyConfig) -> Signal | None:
        """Evaluate the *current* (possibly partial) bar – what the paper
        trader does when run after the open."""
        if not cfg.episodic_pivot.enabled or len(df) < cfg.warmup_bars:
            return None
        return self._signal_at(symbol, df, len(df) - 1, cfg)

    def watchlist(self, symbol: str, df: pd.DataFrame, cfg: StrategyConfig) -> Signal | None:
        """EPs cannot be pre-planned from daily bars: nothing to watch."""
        return None
