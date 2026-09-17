"""Fetch context for a batch of candidate symbols, concurrently and cached."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from ..config import StrategyConfig
from ..uw import UWClient
from .base import ContextCache, ContextReport
from .edge import compute_edge
from .sources import fetch_finviz, fetch_reddit, fetch_stocktwits, fetch_unusual_whales, fetch_yahoo, finalize_news, finalize_social

log = logging.getLogger(__name__)


class ContextGatherer:
    def __init__(self, cfg: StrategyConfig, cache_path: Path | None = None, workers: int = 6, registry=None, uw_cache_path: Path | None = None):
        self.cfg = cfg
        self.c = cfg.context
        self.cache = ContextCache(cache_path or Path("paper_state/context_cache.json"), ttl_minutes=self.c.cache_minutes)
        self.workers = workers
        self.registry = registry  # optional health.ConnectionRegistry: every fetch outcome is recorded there
        # One Unusual Whales client per gatherer: shared throttle, per-endpoint TTL cache on disk.
        self.uw = UWClient(cache_path=uw_cache_path or self.cache.path.with_name("uw_cache.json"))

    def paid_caps(self) -> tuple[int, int]:
        """(flow cap, edge cap): how many top-ranked candidates get each paid
        Unusual Whales read per cycle; the edge score never runs without the flow scan."""
        f, e = self.cfg.options_flow, self.cfg.edge
        flow_cap = f.max_symbols_per_cycle if f.enabled else 0
        return flow_cap, min(flow_cap, e.max_symbols_per_cycle) if e.enabled else 0

    def one(self, symbol: str, now: datetime | None = None, flow: bool = True, edge: bool | None = None) -> ContextReport:
        """Fetch every enabled source for one symbol. ``flow=False`` skips the paid
        Unusual Whales reads (used to cap the per-cycle spend); ``edge`` narrows
        that further to the flow scan only."""
        now = now or datetime.now(timezone.utc)
        report = ContextReport(symbol=symbol, asof=now.date().isoformat(), fetched_at=time.time())
        c = self.c
        report.weights = {"news": 1.0, "social": 0.6, "flow": self.cfg.options_flow.weight, "edge": self.cfg.edge.rank_weight}
        if c.news_enabled or c.events_enabled:
            fetch_finviz(report, c.news_lookback_days, now)
            fetch_yahoo(report, c.news_lookback_days, now, need_news=c.news_enabled and len(report.headlines) < 3)
            if c.news_enabled:
                finalize_news(report, now)
            else:
                report.headlines, report.news_count = [], 0
        if c.social_enabled:
            scores, samples = [], []
            if "stocktwits" in c.social_sources:
                s, smp = fetch_stocktwits(report)
                scores += s
                samples += smp
            if "reddit" in c.social_sources:
                s, smp = fetch_reddit(report, c.news_lookback_days)
                scores += s
                samples += smp
            finalize_social(report, scores, samples, c.social_min_messages)
        f = self.cfg.options_flow
        if f.enabled and flow:
            fetch_unusual_whales(report, settings=f, now=now)
            if self.cfg.edge.enabled and (edge is None or edge):
                compute_edge(report, self.cfg, self.uw, now)
        return report

    def gather(self, symbols: list[str], now: datetime | None = None) -> dict[str, ContextReport]:
        """Context for ``symbols`` (cache first, then network, top ``max_symbols_per_cycle``)."""
        if not self.c.enabled:
            return {}
        out: dict[str, ContextReport] = {}
        todo: list[str] = []
        for s in symbols[: self.c.max_symbols_per_cycle]:
            hit = self.cache.get(s)
            if hit is not None:
                out[s] = hit
            else:
                todo.append(s)
        if todo:
            log.info("fetching context for %d symbols (%d cached)", len(todo), len(out))
            # Paid Unusual Whales reads go to the best-ranked candidates first, up to the per-cycle cap.
            flow_cap, edge_cap = self.paid_caps()
            flow_syms, edge_syms = set(symbols[:flow_cap]), set(symbols[:edge_cap])
            fetched: list[ContextReport] = []
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                for rep in pool.map(lambda s: self._safe(s, now, s in flow_syms, s in edge_syms), todo):
                    out[rep.symbol] = rep
                    fetched.append(rep)
                    self.cache.put(rep)
            self.cache.save()
            if self.registry is not None:
                self.registry.record_sources(fetched)
        return out

    def _safe(self, symbol: str, now: datetime | None, flow: bool = True, edge: bool = True) -> ContextReport:
        try:
            return self.one(symbol, now, flow=flow, edge=edge)
        except Exception as exc:  # pragma: no cover - belt and braces
            rep = ContextReport(symbol=symbol, asof=(now or datetime.now(timezone.utc)).date().isoformat(), fetched_at=time.time())
            rep.errors["gather"] = f"{type(exc).__name__}: {exc}"
            return rep
