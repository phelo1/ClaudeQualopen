"""Pluggable per-symbol sentiment (social media, news, options flow, ...).

The engine does not scrape anything itself: social APIs change, need keys,
and the useful signal is vendor-specific. Instead any source that can emit
``date,symbol,score[,buzz]`` rows can feed the strategy through
``CsvSentimentProvider``, and new providers only need to implement
``SentimentProvider.scores``.

Conventions
* ``score``  in [-1, +1]: bearish .. bullish tone.
* ``buzz``   optional, >= 0: attention / message volume relative to normal.
* Values are forward-filled for up to ``max_staleness_days`` so a weekend
  reading still applies on Monday, then go NaN (which passes the filter -
  missing data is not a signal).

How the strategy uses it
* Hard filter: ``min_score`` (avoid negative tone) and ``max_score`` (avoid
  crowded, euphoric names - Kullamägi's EPs in particular tend to work best
  when the stock is still *neglected*).
* Soft ranking: ``score_weight * score`` is added to the candidate score.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Protocol

import numpy as np
import pandas as pd

from .config import StrategyConfig

SENTIMENT_COLUMNS = ["sentiment", "buzz"]


class SentimentProvider(Protocol):
    def scores(self, symbols: Iterable[str]) -> pd.DataFrame:
        """Long frame with columns date, symbol, score[, buzz]."""
        ...


class CsvSentimentProvider:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def scores(self, symbols: Iterable[str]) -> pd.DataFrame:
        if not self.path.exists():
            raise FileNotFoundError(f"Sentiment CSV not found: {self.path}")
        df = pd.read_csv(self.path)
        df.columns = [c.strip().lower() for c in df.columns]
        required = {"date", "symbol", "score"}
        if not required <= set(df.columns):
            raise ValueError(f"Sentiment CSV needs columns {sorted(required)}, got {list(df.columns)}")
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()
        df["symbol"] = df["symbol"].str.upper()
        wanted = {s.upper() for s in symbols}
        df = df[df["symbol"].isin(wanted)]
        if "buzz" not in df.columns:
            df["buzz"] = np.nan
        return df[["date", "symbol", "score", "buzz"]]


def make_sentiment_provider(cfg: StrategyConfig) -> SentimentProvider | None:
    s = cfg.sentiment
    if not s.enabled:
        return None
    if s.source == "csv":
        if not s.path:
            raise ValueError("sentiment.path must point at a CSV when sentiment.source is 'csv'")
        return CsvSentimentProvider(s.path)
    raise ValueError(f"Unknown sentiment source '{s.source}'")


def attach_sentiment_columns(data: dict[str, pd.DataFrame], cfg: StrategyConfig, provider: SentimentProvider | None = None) -> dict[str, pd.DataFrame]:
    provider = provider or make_sentiment_provider(cfg)
    if provider is None:
        return data
    long = provider.scores(data.keys())
    out: dict[str, pd.DataFrame] = {}
    limit = cfg.sentiment.max_staleness_days
    for sym, df in data.items():
        df = df.copy()
        rows = long[long["symbol"] == sym].sort_values("date").drop_duplicates("date", keep="last").set_index("date")
        if rows.empty:
            df["sentiment"] = np.nan
            df["buzz"] = np.nan
        else:
            # Align on the union so a reading on a non-trading day still
            # propagates to the next bar, then keep only bar dates.
            union = df.index.union(rows.index)
            aligned = rows.reindex(union).ffill(limit=limit).reindex(df.index)
            df["sentiment"] = aligned["score"].to_numpy()
            df["buzz"] = aligned["buzz"].to_numpy()
        out[sym] = df
    return out


def sentiment_ok(row: pd.Series, cfg: StrategyConfig) -> bool:
    s = cfg.sentiment
    if not s.enabled:
        return True
    val = row.get("sentiment", np.nan)
    if val is None or np.isnan(val):
        return True
    if s.min_score is not None and val < s.min_score:
        return False
    if s.max_score is not None and val > s.max_score:
        return False
    return True
