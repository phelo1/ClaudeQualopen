"""Market-wide screens for the tiered schedule.

Two screens feed the intraday passes so fast movers outside the nightly
arming list are not missed:

* ``premarket`` - stocks gapping up by ``schedule.premarket_min_gap_pct`` or
  more versus the prior close, before the open;
* ``movers``    - stocks up ``schedule.movers_min_change_pct`` on at least
  ``schedule.movers_min_rvol`` x their normal volume during the session.

Sources, in order of preference:

1. Unusual Whales ``GET /screener/stocks`` (needs ``UNUSUAL_WHALES_API_KEY``);
2. the free finviz screener (``finvizfinance``) - movers only; finviz's free
   screener has no pre-market change filter.

Screen hits are *candidates*: the trader still needs a valid setup on real
bars before anything is armed or traded. When no source is available the
result says so (``unavailable``) and the cycle records a data gap - nothing
is substituted.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import StrategyConfig
from .uw import UWClient, fnum, has_key

log = logging.getLogger(__name__)

ISSUE_TYPES = ["Common Stock", "ADR"]


@dataclass
class ScreenResult:
    kind: str  # premarket | movers
    hits: list[dict] = field(default_factory=list)  # {symbol, change_pct, rvol, price, market_cap, sector, source, ...}
    source: str | None = None  # unusual_whales | finviz | None
    error: str | None = None
    calls: int = 0
    considered: int = 0  # rows returned by the source before local filtering

    @property
    def unavailable(self) -> bool:
        return self.source is None

    @property
    def symbols(self) -> list[str]:
        return [h["symbol"] for h in self.hits]

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "source": self.source, "error": self.error, "calls": self.calls,
            "considered": self.considered, "hits": self.hits, "unavailable": self.unavailable,
        }


def _thresholds(kind: str, cfg: StrategyConfig) -> tuple[float, float | None, int]:
    sch = cfg.schedule
    if kind == "premarket":
        return sch.premarket_min_gap_pct, None, sch.premarket_max_names
    return sch.movers_min_change_pct, sch.movers_min_rvol, sch.movers_max_names


# --------------------------------------------------------------------------- #
# Unusual Whales
# --------------------------------------------------------------------------- #
def _uw_screen(kind: str, cfg: StrategyConfig, client: UWClient) -> ScreenResult:
    min_change, min_rvol, max_names = _thresholds(kind, cfg)
    params: dict[str, Any] = {
        "min_change": round(min_change, 4),  # fraction; rows are re-checked locally from close / prev_close
        "min_underlying_price": cfg.momentum.min_price,
        "issue_types[]": ISSUE_TYPES,
        "order": "perc_change",
        "order_direction": "desc",
    }
    if min_rvol:
        params["min_stock_volume_vs_avg30_volume"] = round(min_rvol, 2)
    before = client.calls
    resp = client.get("/screener/stocks", params, ttl=0.0)
    out = ScreenResult(kind=kind, source="unusual_whales", calls=client.calls - before)
    if not resp.ok:
        out.error = resp.error
        return out
    rows = resp.data if isinstance(resp.data, list) else []
    out.considered = len(rows)
    excluded = set(cfg.auxiliary_symbols)
    for row in rows:
        sym = str(row.get("ticker") or "").upper().strip()
        close, prev = fnum(row.get("close")), fnum(row.get("prev_close"))
        if not sym or sym in excluded or not close or not prev or prev <= 0:
            continue
        change = close / prev - 1.0
        rvol = fnum(row.get("relative_volume"))
        if change < min_change:
            continue
        if min_rvol and (rvol is None or rvol < min_rvol):
            continue
        out.hits.append(
            {
                "symbol": sym,
                "change_pct": round(change, 4),
                "rvol": round(rvol, 2) if rvol is not None else None,
                "price": close,
                "prev_close": prev,
                "market_cap": fnum(row.get("marketcap")),
                "sector": row.get("sector"),
                "next_earnings_date": row.get("next_earnings_date"),
                "er_time": row.get("er_time"),
                "source": kind,
                "provider": "unusual_whales",
            }
        )
    out.hits.sort(key=lambda h: h["change_pct"], reverse=True)
    out.hits = out.hits[:max_names]
    return out


# --------------------------------------------------------------------------- #
# finviz (free) - movers only
# --------------------------------------------------------------------------- #
_FV_CHANGE = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 20]
_FV_RVOL = [0.25, 0.5, 0.75, 1, 1.5, 2, 3, 5, 10]
_FV_PRICE = [1, 2, 3, 4, 5, 7, 10, 15, 20, 30, 40, 50]


def _snap_down(value: float, options: list[float]) -> float | None:
    fitting = [o for o in options if o <= value + 1e-9]
    return max(fitting) if fitting else None


def _pct(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        # finvizfinance versions differ: 5.2 (percent) vs 0.052 (fraction). Rows come
        # back pre-filtered to >= 1%, so a value below 1 can only be a fraction.
        return x / 100.0 if abs(x) >= 1.0 else x
    m = re.search(r"-?\d+(\.\d+)?", str(v))
    if not m:
        return None
    return float(m.group(0)) / 100.0


def _finviz_screen(kind: str, cfg: StrategyConfig) -> ScreenResult:
    out = ScreenResult(kind=kind, source="finviz")
    if kind == "premarket":
        out.source = None
        out.error = "finviz's free screener has no pre-market change filter; set UNUSUAL_WHALES_API_KEY for pre-market gaps"
        return out
    try:
        from finvizfinance.screener.overview import Overview
    except ImportError:
        out.source = None
        out.error = "finvizfinance not installed"
        return out
    min_change, min_rvol, max_names = _thresholds(kind, cfg)
    filters: dict[str, str] = {}
    chg = _snap_down(min_change * 100, _FV_CHANGE)
    if chg is not None:
        filters["Change"] = f"Up {int(chg)}%"
    rv = _snap_down(min_rvol or 0, _FV_RVOL)
    if rv:
        filters["Relative Volume"] = f"Over {rv:g}"
    px = _snap_down(cfg.momentum.min_price, _FV_PRICE)
    if px:
        filters["Price"] = f"Over ${int(px)}"
    try:
        scr = Overview()
        scr.set_filter(filters_dict=filters)
        df = scr.screener_view(order="Change", ascend=False, verbose=0)
        out.calls = 1
    except Exception as exc:
        out.error = f"{type(exc).__name__}: {exc}"
        return out
    if df is None or len(df) == 0:
        return out
    out.considered = len(df)
    excluded = set(cfg.auxiliary_symbols)
    for _, row in df.iterrows():
        sym = str(row.get("Ticker") or "").upper().strip()
        change = _pct(row.get("Change"))
        if not sym or sym in excluded or change is None or change < min_change:
            continue
        out.hits.append(
            {
                "symbol": sym,
                "change_pct": round(change, 4),
                "rvol": None,  # finviz's overview table has no relative-volume column; the filter was applied server-side
                "price": fnum(row.get("Price")),
                "market_cap": row.get("Market Cap"),
                "sector": row.get("Sector"),
                "source": kind,
                "provider": "finviz",
            }
        )
    out.hits.sort(key=lambda h: h["change_pct"], reverse=True)
    out.hits = out.hits[:max_names]
    return out


# --------------------------------------------------------------------------- #
def run_screen(kind: str, cfg: StrategyConfig, uw_cache_path: Path | None = None, client: UWClient | None = None) -> ScreenResult:
    """Run the ``premarket`` or ``movers`` screen with the best available source."""
    if kind not in ("premarket", "movers"):
        raise ValueError(f"unknown screen '{kind}'")
    if client is not None or has_key():
        client = client or UWClient(cache_path=uw_cache_path)
        res = _uw_screen(kind, cfg, client)
        client.save_cache()
        if res.error is None or kind == "premarket":
            return res
        log.warning("Unusual Whales screener failed (%s); trying finviz", res.error)
        fv = _finviz_screen(kind, cfg)
        fv.calls += res.calls
        if fv.source is None:
            fv.error = f"unusual_whales: {res.error}; finviz: {fv.error}"
        return fv
    res = _finviz_screen(kind, cfg)
    if res.source is None and kind == "premarket":
        res.error = "no screener source: " + (res.error or "set UNUSUAL_WHALES_API_KEY")
    return res
