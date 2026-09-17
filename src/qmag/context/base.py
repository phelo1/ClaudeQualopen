"""Shared types for the context layer plus a tiny on-disk cache."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any


@dataclass
class Headline:
    when: str  # ISO timestamp (or date) as reported by the source
    title: str
    source: str
    url: str = ""
    score: float = 0.0  # -1 .. +1
    tags: list[str] = field(default_factory=list)  # catalyst tags: earnings, guidance, fda, offering, ...


@dataclass
class ContextReport:
    symbol: str
    asof: str
    # news
    news_score: float | None = None  # mean headline score, recency weighted
    news_count: int = 0
    headlines: list[Headline] = field(default_factory=list)
    catalysts: list[str] = field(default_factory=list)  # distinct catalyst tags seen in the window
    # social
    social_score: float | None = None  # -1 .. +1
    social_messages: int = 0
    social_bullish: int = 0
    social_bearish: int = 0
    social_sources: list[str] = field(default_factory=list)
    social_samples: list[str] = field(default_factory=list)
    # options flow (Unusual Whales; only filled when options_flow.enabled)
    flow_score: float | None = None  # -1 .. +1 (net bullish premium share, sweeps)
    flow_call_premium: float | None = None
    flow_put_premium: float | None = None
    flow_bull_premium: float | None = None  # ask-side calls + bid-side puts (UW definition)
    flow_bear_premium: float | None = None  # bid-side calls + ask-side puts
    flow_call_vol_ratio: float | None = None  # today's call volume / 30-day average
    flow_put_vol_ratio: float | None = None
    flow_opt_vol_pctile: float | None = None  # option volume percentile vs the ticker's own ~90 days
    flow_alerts: int = 0  # unusual trades kept after the premium / age / DTE filters
    flow_bull_alerts: int = 0
    flow_bear_alerts: int = 0
    flow_sweeps: int = 0
    flow_unusual: bool = False  # any unusual-activity signal fired (alerts, volume ratio or percentile)
    flow_trades: list[dict] = field(default_factory=list)  # largest unusual trades, newest data first
    flow_note: str = ""
    # Unusual Whales edge score (context/edge.py): weighted blend of many
    # independent reads; ``edge`` carries the per-feature breakdown, coverage
    # and the pass/fail verdict against edge.threshold.
    edge: dict | None = None
    edge_score: float | None = None  # -1 .. +1, None when not computed / nothing answered
    # composite weights (set by the gatherer from the config; defaults match the old fixed blend)
    weights: dict[str, float] = field(default_factory=lambda: {"news": 1.0, "social": 0.6, "flow": 1.0, "edge": 1.0})
    # events / fundamentals
    earnings_date: str | None = None
    days_to_earnings: int | None = None
    earnings_recent_days: int | None = None  # days since the last print (EP catalyst check)
    sector: str | None = None
    industry: str | None = None
    market_cap: float | None = None
    float_shares: float | None = None
    short_float_pct: float | None = None
    insider_trans_pct: float | None = None
    inst_own_pct: float | None = None
    analyst_recom: float | None = None
    target_price: float | None = None
    # bookkeeping
    available: dict[str, bool] = field(default_factory=dict)  # source -> fetched ok
    errors: dict[str, str] = field(default_factory=dict)
    fetched_at: float = 0.0

    @property
    def composite(self) -> float | None:
        """Blend of the available scores in -1..1; None when nothing was available."""
        w = self.weights or {}
        parts = [
            (self.news_score, w.get("news", 1.0)),
            (self.social_score, w.get("social", 0.6)),
            (self.flow_score, w.get("flow", 1.0)),
            (self.edge_score, w.get("edge", 1.0)),
        ]
        parts = [(s, wt) for s, wt in parts if s is not None and wt > 0]
        if not parts:
            return None
        return sum(s * w for s, w in parts) / sum(w for _, w in parts)

    @property
    def low_float(self) -> bool | None:
        return None if self.float_shares is None else self.float_shares < 30e6

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["composite"] = self.composite
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ContextReport":
        known = {f.name for f in fields(cls)}
        d = {k: v for k, v in d.items() if k in known}
        d["headlines"] = [Headline(**h) for h in d.get("headlines", [])]
        return cls(**d)


class ContextCache:
    """JSON-file cache keyed by symbol; entries expire after ``ttl_minutes``."""

    def __init__(self, path: Path, ttl_minutes: float = 30.0):
        self.path = Path(path)
        self.ttl = ttl_minutes * 60
        self._data: dict[str, dict] = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
            except json.JSONDecodeError:
                self._data = {}

    def get(self, symbol: str) -> ContextReport | None:
        raw = self._data.get(symbol)
        if raw is None or time.time() - raw.get("fetched_at", 0) > self.ttl:
            return None
        return ContextReport.from_dict(raw)

    def put(self, report: ContextReport) -> None:
        self._data[report.symbol] = report.to_dict()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - 7 * 86400
        self._data = {k: v for k, v in self._data.items() if v.get("fetched_at", 0) > cutoff}
        self.path.write_text(json.dumps(self._data, indent=1, default=str))
