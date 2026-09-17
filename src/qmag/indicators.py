"""Vectorised indicator helpers on OHLCV DataFrames.

All functions expect a DataFrame with columns: open, high, low, close, volume
indexed by a DatetimeIndex sorted ascending.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

OHLCV = ["open", "high", "low", "close", "volume"]


def validate_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in OHLCV if c not in df.columns]
    if missing:
        raise ValueError(f"OHLCV frame missing columns: {missing}")
    out = df[OHLCV].copy()
    out.index = pd.DatetimeIndex(out.index).tz_localize(None)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out.dropna()


def sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(length, min_periods=length).mean()


def adr_pct(df: pd.DataFrame, length: int = 20) -> pd.Series:
    """Average daily range in percent: mean(high/low - 1) * 100."""
    daily_range = (df["high"] / df["low"] - 1.0) * 100.0
    return daily_range.rolling(length, min_periods=length).mean()


def adr_dollar(df: pd.DataFrame, length: int = 20) -> pd.Series:
    return (df["high"] - df["low"]).rolling(length, min_periods=length).mean()


def pct_change_over(series: pd.Series, bars: int) -> pd.Series:
    return series / series.shift(bars) - 1.0


def relative_volume(df: pd.DataFrame, length: int = 20) -> pd.Series:
    avg = df["volume"].rolling(length, min_periods=length).mean().shift(1)
    return df["volume"] / avg


def dollar_volume(df: pd.DataFrame, length: int = 20) -> pd.Series:
    return (df["close"] * df["volume"]).rolling(length, min_periods=length).mean()


def close_position(df: pd.DataFrame) -> pd.Series:
    """Where the close sits inside the day's range (0 = low, 1 = high)."""
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    return ((df["close"] - df["low"]) / rng).fillna(0.5)


def enrich(df: pd.DataFrame, fast_ma: int = 10, slow_ma: int = 20, trail_ma: int = 10) -> pd.DataFrame:
    """Attach every indicator the setups and trade management need."""
    out = validate_ohlcv(df)
    out["adr_pct"] = adr_pct(out)
    out["adr_dollar"] = adr_dollar(out)
    out["gain_1m"] = pct_change_over(out["close"], 21)
    out["gain_3m"] = pct_change_over(out["close"], 63)
    out["gain_6m"] = pct_change_over(out["close"], 126)
    out["rvol_20"] = relative_volume(out, 20)
    out["rvol_50"] = relative_volume(out, 50)
    out["dollar_vol_20"] = dollar_volume(out, 20)
    out["close_pos"] = close_position(out)
    out["gap_pct"] = out["open"] / out["close"].shift(1) - 1.0
    for length in sorted({fast_ma, slow_ma, trail_ma, 50}):
        out[f"sma_{length}"] = sma(out["close"], length)
    return out
