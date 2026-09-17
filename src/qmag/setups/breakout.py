"""Momentum breakout: a leader consolidates in a tight flag, then clears the
flag high on expanding volume.

Kullamägi's description of the setup, translated into daily-bar rules:

1. The stock is a momentum leader (large 1/3/6 month gain, high ADR, liquid).
2. After the impulse it forms an orderly flag / consolidation of 10-60 days
   that is shallow relative to the prior move and whose range contracts.
3. Price "surfs" the rising 10/20-day moving averages while it consolidates.
4. Trigger: the first day the high clears the flag high, ideally on volume.
   Entry is the opening-range-high break, approximated as pivot + buffer, or
   the open when the stock gaps through the pivot (skipped if the gap is big).
5. Initial stop: the low of the entry day, approximated as entry - k * ADR.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import StrategyConfig
from .base import Signal, context_ok, momentum_ok, momentum_score


def _theme_details(row: pd.Series) -> dict:
    theme = row.get("theme")
    if not isinstance(theme, str):
        return {}
    return {"theme": theme, "theme_pct": float(row["theme_pct"])}


class BreakoutDetector:
    name = "breakout"

    # ------------------------------------------------------------------ #
    def _flag(self, df: pd.DataFrame, t: int, cfg: StrategyConfig) -> dict | None:
        """Find the shortest valid flag ending at bar ``t - 1``. Returns its geometry."""
        b = cfg.breakout
        high = df["high"].to_numpy()
        low = df["low"].to_numpy()
        close = df["close"].to_numpy()
        adr = df["adr_pct"].to_numpy()

        prev = t - 1
        if prev < b.max_flag_days + 63:
            return None

        # Structural context: ride above the MAs going into the breakout.
        if b.require_above_ma:
            fast = df[f"sma_{b.fast_ma}"].to_numpy()
            slow = df[f"sma_{b.slow_ma}"].to_numpy()
            if np.isnan(slow[prev]) or np.isnan(fast[prev]):
                return None
            if close[prev] < slow[prev] or fast[prev] < fast[prev - 3]:
                return None

        recent_adr = float(np.nanmean((high[prev - 4 : prev + 1] / low[prev - 4 : prev + 1] - 1) * 100))
        for length in range(b.min_flag_days, b.max_flag_days + 1):
            start = prev - length + 1
            flag_high = float(high[start : prev + 1].max())
            flag_low = float(low[start : prev + 1].min())

            # The impulse that preceded the flag: from the swing low of the
            # previous quarter up to the flag high.
            impulse_low = float(low[max(0, start - 63) : start].min())
            impulse = flag_high - impulse_low
            if impulse <= 0:
                continue
            depth = (flag_high - flag_low) / impulse
            if depth > b.max_flag_depth:
                continue

            # The pivot should be a real high, not a lower high inside a downtrend.
            lookback_high = float(high[max(0, start - 126) : prev + 1].max())
            if flag_high < 0.97 * lookback_high:
                continue

            flag_adr = float(np.nanmean((high[start : prev + 1] / low[start : prev + 1] - 1) * 100))
            if flag_adr <= 0 or recent_adr / flag_adr > b.max_recent_adr_ratio:
                continue

            # Already broke out earlier in the flag? Then the pivot is not fresh.
            if close[prev] > flag_high:
                continue

            return {
                "flag_days": length,
                "flag_high": flag_high,
                "flag_low": flag_low,
                "depth": depth,
                "contraction": recent_adr / flag_adr,
                "adr_pct_prev": float(adr[prev]),
            }
        return None

    # ------------------------------------------------------------------ #
    def _signal_at(self, symbol: str, df: pd.DataFrame, t: int, cfg: StrategyConfig) -> Signal | None:
        b = cfg.breakout
        row = df.iloc[t]
        prev_row = df.iloc[t - 1]
        if not momentum_ok(prev_row, cfg) or not context_ok(prev_row, cfg, self.name):
            return None
        flag = self._flag(df, t, cfg)
        if flag is None:
            return None
        pivot = flag["flag_high"]
        trigger = pivot * (1 + b.entry_buffer_pct)
        if row["high"] < trigger:
            return None
        if row["open"] > pivot * (1 + b.max_gap_pct):
            return None  # gapped too far above the pivot: don't chase
        rvol = row["rvol_20"]
        if np.isnan(rvol) or rvol < b.min_breakout_volume_ratio:
            return None

        entry = max(float(row["open"]), trigger)
        adr_d = float(prev_row["adr_dollar"])
        if np.isnan(adr_d) or adr_d <= 0:
            return None
        return Signal(
            symbol=symbol,
            date=df.index[t],
            setup=self.name,
            pivot=float(pivot),
            entry=float(entry),
            stop=float(entry - cfg.management.stop_adr_mult * adr_d),
            adr_dollar=adr_d,
            adr_pct=float(prev_row["adr_pct"]),
            score=momentum_score(prev_row, cfg),
            details={**_theme_details(prev_row), **flag, "rvol": float(rvol)},
        )

    def detect(self, symbol: str, df: pd.DataFrame, cfg: StrategyConfig) -> list[Signal]:
        if not cfg.breakout.enabled or len(df) < cfg.warmup_bars:
            return []
        high = df["high"].to_numpy()
        # Cheap necessary condition: today's high exceeds the prior
        # ``min_flag_days`` highs. Everything else is evaluated only there.
        prior_max = pd.Series(high).shift(1).rolling(cfg.breakout.min_flag_days).max().to_numpy()
        candidates = np.flatnonzero(high > prior_max)
        signals: list[Signal] = []
        for t in candidates:
            if t < cfg.warmup_bars:
                continue
            sig = self._signal_at(symbol, df, int(t), cfg)
            if sig is not None:
                signals.append(sig)
        return signals

    def detect_last(self, symbol: str, df: pd.DataFrame, cfg: StrategyConfig) -> Signal | None:
        """Did the *latest* bar trigger? Cheap path for live scanning."""
        if not cfg.breakout.enabled or len(df) < cfg.warmup_bars:
            return None
        return self._signal_at(symbol, df, len(df) - 1, cfg)

    # ------------------------------------------------------------------ #
    def watchlist(self, symbol: str, df: pd.DataFrame, cfg: StrategyConfig) -> Signal | None:
        """Is a flag *ready* as of the last bar? If so, return the buy-stop plan."""
        if not cfg.breakout.enabled or len(df) < cfg.warmup_bars:
            return None
        t = len(df)  # hypothetical "tomorrow"
        last = df.iloc[-1]
        if not momentum_ok(last, cfg) or not context_ok(last, cfg, self.name):
            return None
        flag = self._flag(df, t, cfg)
        if flag is None:
            return None
        pivot = flag["flag_high"]
        # Only list names within striking distance of the pivot.
        if last["close"] < pivot * 0.90:
            return None
        trigger = pivot * (1 + cfg.breakout.entry_buffer_pct)
        adr_d = float(last["adr_dollar"])
        return Signal(
            symbol=symbol,
            date=df.index[-1],
            setup=self.name,
            pivot=float(pivot),
            entry=float(trigger),
            stop=float(trigger - cfg.management.stop_adr_mult * adr_d),
            adr_dollar=adr_d,
            adr_pct=float(last["adr_pct"]),
            score=momentum_score(last, cfg),
            details={**_theme_details(last), "distance_to_pivot_pct": float(pivot / last["close"] - 1) * 100, **flag},
        )
