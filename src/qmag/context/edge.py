"""Unusual Whales edge score: many independent reads of one ticker's options
tape, stock tape, ownership filings, calendar and market backdrop, each turned
into a -1..+1 sub-score (bullish positive), weight-averaged into one number
that must clear ``edge.threshold`` before the trader enters.

Design rules

* every feature reads real endpoints and states exactly how its sub-score is
  derived (``Feature.rule``); the same text is shown in the dashboard and
  the README, so nothing is a black box;
* a feature that could not be sourced (HTTP error, empty answer, unknown
  response shape) scores ``None`` - it is *not* zero - and is listed with its
  error; ``coverage`` (answered weight / applicable weight) tells how much of
  the intended evidence was actually seen and is gated separately;
* features that do not apply (options data for a ticker with no listed
  options) are excluded from both the score and the coverage;
* weights are plain config (``edge.weights``); 0 switches a feature off and
  its endpoint is never called.

Cost: roughly 20 requests per candidate on the first read of a day, about
half that afterwards (daily facts are cached for ``edge.daily_cache_hours``),
under the shared throttle in ``qmag.uw``.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pandas as pd

from ..uw import UWClient, UWResponse, fnum
from .base import ContextReport, Headline

log = logging.getLogger(__name__)

UW_SECTORS = {
    "Basic Materials", "Communication Services", "Consumer Cyclical", "Consumer Defensive", "Energy",
    "Financial Services", "Healthcare", "Industrials", "Real Estate", "Technology", "Utilities",
}
MARKET_TTL = 300  # seconds; market / sector tide are shared across symbols


@dataclass(frozen=True)
class Feature:
    name: str
    label: str
    group: str  # options | tape | ownership | calendar | market
    reads: str  # endpoint(s)
    rule: str  # how the -1..+1 sub-score is derived
    needs_options: bool = False
    daily: bool = False  # answer changes once a day -> cached for edge.daily_cache_hours


FEATURES: tuple[Feature, ...] = (
    Feature("flow", "Unusual options flow", "options", "/stock/{t}/options-volume, /option-trades/flow-alerts, /stock/{t}/unusualness",
            "the options-flow tilt: bullish vs bearish premium, aggressive unusual trades (sweeps weighted), amplified by the option-volume percentile", needs_options=True),
    Feature("net_premium", "Net premium today", "options", "/stock/{t}/net-prem-ticks",
            "(net call premium - net put premium) / (|net call| + |net put|) summed over today's minute ticks; ask-side buying counts positive, bid-side selling negative", needs_options=True),
    Feature("oi_change", "Open interest built", "options", "/stock/{t}/oi-change",
            "(call OI change - put OI change) / (|call| + |put|) across the contracts with the largest overnight OI change", needs_options=True, daily=True),
    Feature("dealer_delta", "Dealer delta exposure", "options", "/stock/{t}/greek-exposure",
            "(call delta + put delta) / (|call delta| + |put delta|) of the latest market-maker exposure snapshot", needs_options=True, daily=True),
    Feature("gamma", "Gamma regime & walls", "options", "/stock/{t}/greek-exposure, /stock/{t}/gex-levels",
            "+0.5 when net dealer gamma is negative (hedging amplifies moves), -0.25 when positive (pinning); plus room to the call wall: +0.5 at >= 8% above spot, -0.5 within 2%", needs_options=True),
    Feature("option_sentiment", "Options positioning sentiment", "options", "/stock/{t}/volatility/option-sentiment",
            "Unusual Whales' blended VWKS + AVAR positioning score, normalised to -1..+1 (positive = volume centred above spot and calls bid over puts)", needs_options=True),
    Feature("options_pulse", "Nasdaq Options Pulse", "options", "/stock/{t}/options-pulse",
            "the running daily sentiment score (sntm_score) of opening-buy transactions, normalised to -1..+1", needs_options=True),
    Feature("skew", "25-delta skew", "options", "/stock/{t}/volatility/term-structure, /stock/{t}/historical-risk-reversal-skew",
            "today's 25-delta risk reversal (put IV - call IV) for the ~30-day expiry versus its own one-month history: calls bid richer than usual scores positive (z-score / 2, clipped)", needs_options=True, daily=True),
    Feature("max_pain", "Max pain", "options", "/stock/{t}/max-pain",
            "spot above the nearest expiry's max pain scores negative (pin / pullback risk into expiry), below scores positive; scaled by 1 / 0.5 / 0.25 for <= 3 / 7 / more days to expiry", needs_options=True, daily=True),
    Feature("volatility", "Implied vs realised vol", "options", "/stock/{t}/volatility/stats, /stock/{t}/volatility/term-structure",
            "implied 30-day vol close to realised scores mildly positive (moves are not yet paid for), implied 60%+ richer than realised scores negative (an event is priced); an inverted term structure subtracts 0.3", needs_options=True, daily=True),
    Feature("dark_pool", "Dark pool prints", "tape", "/darkpool/{t}",
            "notional-weighted position of large off-exchange prints inside the NBBO: at/above the offer +1, at/below the bid -1 (prints without a quote are ignored)"),
    Feature("relative_volume", "Stock volume percentile", "tape", "/stock/{t}/unusualness",
            "(today's stock-volume percentile vs the ticker's own ~90 sessions - 50) / 50"),
    Feature("short_interest", "Short interest", "tape", "/shorts/{t}/interest-float/v2",
            "squeeze fuel, 0..+1: 0.6 x min(short % of float / high_short_float, 1) + 0.4 x min((days to cover - 1) / 4, 1); low short interest is neutral, never negative", daily=True),
    Feature("insiders", "Insider transactions", "ownership", "/insider/{t}/ticker-flow",
            "(2 x insider buy $ - insider sell $) / (2 x buys + sells) over insider_days; open-market buys are rarer and count double", daily=True),
    Feature("institutions", "13F holders", "ownership", "/institution/{t}/ownership",
            "(shares added - shares trimmed) / (added + trimmed) across holders in the latest reported quarter", daily=True),
    Feature("congress", "Congressional trades", "ownership", "/congress/recent-trades",
            "(buy notional - sell notional) / (buys + sells) over congress_days using the midpoint of each disclosed amount range", daily=True),
    Feature("analysts", "Analyst actions", "ownership", "/screener/analysts",
            "mean of: upgrade +1, downgrade -1, initiation at buy +0.75 / sell -0.75, maintained or reiterated buy +0.25 / sell -0.25, hold 0, over analyst_days", daily=True),
    Feature("seasonality", "Seasonality", "calendar", "/seasonality/{t}/monthly",
            "0.5 x (share of positive closes for this calendar month - 0.5) x 2 + 0.5 x median monthly change / 5%, clipped; needs at least 3 years", daily=True),
    Feature("earnings", "Last earnings reaction", "calendar", "/earnings/{t}, /stock/{t}/info",
            "0.5 x (beat +1 / miss -1 vs the street estimate) + 0.5 x next-day move / 5%, clipped, for the most recent report; also supplies the next report date", daily=True),
    Feature("news", "Unusual Whales headlines", "calendar", "/news/headlines",
            "mean of the feed's own sentiment labels (positive +1, negative -1, neutral 0) over the news window, major headlines counted twice; the headlines are merged into the news list"),
    Feature("market_tide", "Market tide", "market", "/market/market-tide",
            "(net call premium - net put premium) / (|net call| + |net put|) for the whole market at the latest tick"),
    Feature("sector_tide", "Sector tide", "market", "/market/{sector}/sector-tide",
            "the same for the ticker's sector"),
)
FEATURE_BY_NAME = {f.name: f for f in FEATURES}
GROUP_LABELS = {"options": "Options positioning & flow", "tape": "Stock tape", "ownership": "Ownership & filings", "calendar": "Calendar & catalysts", "market": "Market & sector backdrop"}


def clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def tilt(pos: float, neg: float) -> float | None:
    """(pos - neg) / (|pos| + |neg|), None when both are zero."""
    den = abs(pos) + abs(neg)
    return None if den <= 0 else clip((pos - neg) / den)


def _norm_score(v: float) -> float:
    """Normalise a vendor sentiment number of unknown scale into -1..+1."""
    if abs(v) <= 1.0:
        return v
    if abs(v) <= 100.0:
        return v / 100.0
    return math.tanh(v / 100.0)


def _rows(resp: UWResponse) -> list[dict]:
    d = resp.data
    if isinstance(d, list):
        return [r for r in d if isinstance(r, dict)]
    if isinstance(d, dict):
        for k in ("data", "rows", "items", "history", "series"):
            if isinstance(d.get(k), list):
                return [r for r in d[k] if isinstance(r, dict)]
        return [d]
    return []


def _money(x: float | None) -> str:
    if x is None:
        return "n/a"
    a = abs(x)
    s = "-" if x < 0 else ""
    if a >= 1e9:
        return f"{s}${a / 1e9:.2f}B"
    if a >= 1e6:
        return f"{s}${a / 1e6:.1f}M"
    if a >= 1e3:
        return f"{s}${a / 1e3:.0f}k"
    return f"{s}${a:.0f}"


def _find_number(obj: Any, keys: tuple[str, ...]) -> float | None:
    """Depth-first search for the first numeric value under any of ``keys``."""
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and fnum(obj[k]) is not None:
                return fnum(obj[k])
        for v in obj.values():
            found = _find_number(v, keys)
            if found is not None:
                return found
    elif isinstance(obj, list) and obj:
        return _find_number(obj[-1], keys)  # newest last in most series
    return None


# --------------------------------------------------------------------------- #
# Per-symbol evaluation context
# --------------------------------------------------------------------------- #
@dataclass
class Sub:
    score: float | None = None
    value: str = ""  # human summary of what was read
    error: str | None = None
    applicable: bool = True
    cached: bool = False

    @property
    def available(self) -> bool:
        return self.applicable and self.score is not None


class _Ctx:
    def __init__(self, report: ContextReport, cfg, client: UWClient, now: datetime):
        self.report, self.cfg, self.client, self.now = report, cfg, client, now
        self.e = cfg.edge
        self.sym = report.symbol
        self.intraday_ttl = float(cfg.context.cache_minutes) * 60
        self.daily_ttl = float(self.e.daily_cache_hours) * 3600
        self.calls_before = client.calls
        self.info: dict = {}
        self.spot: float | None = None
        self.has_options: bool | None = None
        self.greeks: dict | None = None
        self.term: list[dict] | None = None
        self._memo: dict[str, UWResponse] = {}

    def get(self, path: str, params: dict | None = None, ttl: str = "intraday") -> UWResponse:
        seconds = {"intraday": self.intraday_ttl, "daily": self.daily_ttl, "market": MARKET_TTL, "none": 0.0}[ttl]
        key = UWClient.cache_key(path, params)
        if key not in self._memo:
            self._memo[key] = self.client.get(path, params, ttl=seconds)
        return self._memo[key]

    # shared reads ---------------------------------------------------------
    def load_basics(self) -> None:
        info = self.get(f"/stock/{self.sym}/info", None, "daily")
        if info.ok and isinstance(info.data, dict):
            self.info = info.data
            self.has_options = bool(info.data.get("has_options")) if info.data.get("has_options") is not None else None
            r = self.report
            r.sector = r.sector or info.data.get("sector") or None
            r.market_cap = r.market_cap if r.market_cap is not None else fnum(info.data.get("marketcap"))
            nxt = info.data.get("next_earnings_date")
            if nxt and r.earnings_date is None:
                try:
                    d = pd.Timestamp(nxt).date()
                    delta = (d - self.now.date()).days
                    if delta >= 0:
                        r.earnings_date, r.days_to_earnings = d.isoformat(), delta
                except (TypeError, ValueError):
                    pass
        state = self.get(f"/stock/{self.sym}/stock-state", None, "none")
        if state.ok and isinstance(state.data, dict):
            self.spot = fnum(state.data.get("close")) or fnum(state.data.get("prev_close"))
        elif self.report.flow_trades:
            # no quote from the stock-state endpoint: leave spot unknown; features that need it say so
            self.spot = None

    def greek_latest(self) -> dict | None:
        if self.greeks is None:
            resp = self.get(f"/stock/{self.sym}/greek-exposure", {"timeframe": "5D"}, "daily")
            rows = _rows(resp) if resp.ok else []
            rows = [r for r in rows if r.get("date")]
            self.greeks = max(rows, key=lambda r: str(r["date"])) if rows else {"_error": resp.error or "no exposure rows"}
        return None if "_error" in self.greeks else self.greeks

    def term_structure(self) -> list[dict]:
        if self.term is None:
            resp = self.get(f"/stock/{self.sym}/volatility/term-structure", None, "daily")
            self.term = [r for r in (_rows(resp) if resp.ok else []) if r.get("expiry")]
            if not resp.ok:
                self.term_error = resp.error
        return self.term


# --------------------------------------------------------------------------- #
# Feature functions: each returns a Sub and never raises
# --------------------------------------------------------------------------- #
def f_flow(c: _Ctx) -> Sub:
    r = c.report
    if not c.cfg.options_flow.enabled:
        return Sub(applicable=False, value="options_flow.enabled is false")
    ok = r.available.get("unusual_whales")
    if ok is None:
        return Sub(error="flow scan not run for this symbol")
    if not ok:
        return Sub(error=r.errors.get("unusual_whales", "no answer"))
    return Sub(score=r.flow_score if r.flow_score is not None else 0.0, value=r.flow_note or "quiet options tape: nothing above the premium floor")


def f_net_premium(c: _Ctx) -> Sub:
    resp = c.get(f"/stock/{c.sym}/net-prem-ticks")
    if not resp.ok:
        return Sub(error=resp.error)
    rows = _rows(resp)
    if not rows:
        return Sub(error="no premium ticks for the last session")
    nc = sum(fnum(r.get("net_call_premium")) or 0.0 for r in rows)
    np_ = sum(fnum(r.get("net_put_premium")) or 0.0 for r in rows)
    s = tilt(nc, np_)
    last = max(rows, key=lambda r: str(r.get("tape_time") or ""))
    when = str(last.get("tape_time") or "")[11:16]
    return Sub(score=0.0 if s is None else s, value=f"net call {_money(nc)} vs net put {_money(np_)} through {when or 'the last tick'} ({len(rows)} ticks)")


_OSI = re.compile(r"\d{6}([CP])\d{8}$")


def f_oi_change(c: _Ctx) -> Sub:
    resp = c.get(f"/stock/{c.sym}/oi-change", {"limit": 100}, "daily")
    if not resp.ok:
        return Sub(error=resp.error)
    calls = puts = 0.0
    n = 0
    for r in _rows(resp):
        m = _OSI.search(str(r.get("option_symbol") or ""))
        diff = fnum(r.get("oi_diff_plain"))
        if not m or diff is None:
            continue
        n += 1
        if m.group(1) == "C":
            calls += diff
        else:
            puts += diff
    if n == 0:
        return Sub(error="no contracts with an open-interest change")
    s = tilt(calls, puts)
    return Sub(score=0.0 if s is None else s, value=f"calls {calls:+,.0f} / puts {puts:+,.0f} contracts across the {n} biggest OI changes")


def f_dealer_delta(c: _Ctx) -> Sub:
    g = c.greek_latest()
    if g is None:
        return Sub(error=(c.greeks or {}).get("_error", "no exposure data"))
    cd, pdl = fnum(g.get("call_delta")), fnum(g.get("put_delta"))
    if cd is None or pdl is None:
        return Sub(error="exposure row without delta fields")
    den = abs(cd) + abs(pdl)
    s = clip((cd + pdl) / den) if den else 0.0
    return Sub(score=s, value=f"call delta {cd / 1e6:+.1f}M vs put delta {pdl / 1e6:+.1f}M ({g.get('date')})")


def f_gamma(c: _Ctx) -> Sub:
    g = c.greek_latest()
    if g is None:
        return Sub(error=(c.greeks or {}).get("_error", "no exposure data"))
    cg, pg = fnum(g.get("call_gamma")), fnum(g.get("put_gamma"))
    if cg is None or pg is None:
        return Sub(error="exposure row without gamma fields")
    net = cg + pg
    score = 0.5 if net < 0 else -0.25
    bits = [f"net gamma {_money(net)} ({'negative: hedging amplifies moves' if net < 0 else 'positive: hedging dampens moves'})"]
    lv = c.get(f"/stock/{c.sym}/gex-levels", {"source": "vol"})
    if lv.ok and isinstance(lv.data, dict):
        cw, pw = fnum(lv.data.get("call_wall")), fnum(lv.data.get("put_wall"))
        if cw and c.spot:
            room = (cw - c.spot) / c.spot
            score += clip((room - 0.02) / 0.06, -0.5, 0.5) * 1.0
            bits.append(f"call wall {cw:g} ({room * 100:+.1f}% from spot)")
        elif cw:
            bits.append(f"call wall {cw:g} (spot unknown, room not scored)")
        if pw:
            bits.append(f"put wall {pw:g}")
    else:
        bits.append(f"GEX levels unavailable ({lv.error})")
    return Sub(score=clip(score), value=", ".join(bits))


def f_option_sentiment(c: _Ctx) -> Sub:
    resp = c.get(f"/stock/{c.sym}/volatility/option-sentiment")
    if not resp.ok:
        return Sub(error=resp.error)
    if isinstance(resp.data, dict) and 'latest' in resp.data and resp.data.get('latest') is None and not resp.data.get('history'):
        return Sub(error='no options sentiment observations reported for this symbol')
    latest = resp.data.get("latest") if isinstance(resp.data, dict) and isinstance(resp.data.get("latest"), dict) else resp.data
    v = _find_number(latest, ("score", "sentiment_score", "sentiment", "blended_score", "blended", "positioning_score"))
    if v is None:
        keys = ", ".join(sorted(latest.keys()))[:120] if isinstance(latest, dict) else type(latest).__name__
        return Sub(error=f"unrecognised response shape (keys: {keys})")
    vwks, avar = _find_number(latest, ("vwks",)), _find_number(latest, ("avar",))
    extra = "".join(f", {k} {x:+.3f}" for k, x in (("VWKS", vwks), ("AVAR", avar)) if x is not None)
    return Sub(score=clip(_norm_score(v)), value=f"positioning score {v:+.3f}{extra}")


def f_options_pulse(c: _Ctx) -> Sub:
    resp = c.get(f"/stock/{c.sym}/options-pulse")
    if not resp.ok:
        return Sub(error=resp.error)
    if isinstance(resp.data, dict) and 'latest' in resp.data and resp.data.get('latest') is None and not resp.data.get('intraday'):
        return Sub(error='no options pulse observations reported for this symbol')
    d = resp.data
    latest = d.get("latest") if isinstance(d, dict) and isinstance(d.get("latest"), dict) else d
    v = _find_number(latest, ("sntm_score",))
    if v is None:
        v = _find_number(d, ("sntm_score",))
    if v is None:
        keys = ", ".join(sorted(d.keys()))[:120] if isinstance(d, dict) else type(d).__name__
        return Sub(error=f"unrecognised response shape (keys: {keys})")
    calls, puts = _find_number(latest, ("call_txn",)), _find_number(latest, ("put_txn",))
    extra = f", {calls:.0f} call / {puts:.0f} put opening buys" if calls is not None and puts is not None else ""
    return Sub(score=clip(_norm_score(v)), value=f"pulse sentiment {v:+.3f}{extra}")


def _pick_expiry(term: list[dict], now: datetime, lo: int, hi: int, target: int) -> dict | None:
    cands = []
    for r in term:
        dte = r.get("dte")
        if dte is None:
            try:
                dte = (pd.Timestamp(r["expiry"]).date() - now.date()).days
            except (TypeError, ValueError):
                continue
        if lo <= int(dte) <= hi:
            cands.append((abs(int(dte) - target), int(dte), r))
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    row = dict(cands[0][2])
    row["dte"] = cands[0][1]
    return row


def f_skew(c: _Ctx) -> Sub:
    term = c.term_structure()
    if not term:
        return Sub(error=getattr(c, "term_error", None) or "no term structure (no expiries)")
    exp = _pick_expiry(term, c.now, 20, 60, 30)
    if exp is None:
        return Sub(error="no expiry between 20 and 60 days out")
    resp = c.get(f"/stock/{c.sym}/historical-risk-reversal-skew", {"expiry": exp["expiry"], "delta": "25", "timeframe": "1M"}, "daily")
    if not resp.ok:
        return Sub(error=resp.error)
    hist = [(str(r.get("date")), fnum(r.get("risk_reversal"))) for r in _rows(resp)]
    hist = [(d, v) for d, v in hist if v is not None]
    if not hist:
        return Sub(error="no risk-reversal history")
    hist.sort()
    latest = hist[-1][1]
    vals = [v for _, v in hist]
    mean = sum(vals) / len(vals)
    sd = (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5
    z = (latest - mean) / sd if sd > 1e-9 else 0.0
    score = clip(-z / 2)  # risk reversal = put IV - call IV: falling means calls are being bid
    lean = "calls bid" if score > 0.15 else "puts bid" if score < -0.15 else "normal"
    return Sub(score=score, value=f"25Δ RR {latest:+.3f} vs 1M mean {mean:+.3f} for {exp['expiry']} ({exp['dte']}d): {lean}")


def f_max_pain(c: _Ctx) -> Sub:
    resp = c.get(f"/stock/{c.sym}/max-pain", None, "daily")
    if not resp.ok:
        return Sub(error=resp.error)
    if c.spot is None:
        return Sub(error="spot price unavailable (stock-state)")
    rows = []
    for r in _rows(resp):
        mp = fnum(r.get("max_pain"))
        try:
            dte = (pd.Timestamp(r.get("expiry")).date() - c.now.date()).days
        except (TypeError, ValueError):
            continue
        if mp and dte >= 0:
            rows.append((dte, r.get("expiry"), mp))
    if not rows:
        return Sub(error="no upcoming expiry with a max-pain level")
    dte, expiry, mp = min(rows)
    dist = (c.spot - mp) / c.spot
    urgency = 1.0 if dte <= 3 else 0.5 if dte <= 7 else 0.25
    return Sub(score=clip(-dist / 0.05) * urgency, value=f"max pain {mp:g} for {expiry} ({dte}d), spot {dist * 100:+.1f}% {'above' if dist >= 0 else 'below'}")


def f_volatility(c: _Ctx) -> Sub:
    resp = c.get(f"/stock/{c.sym}/volatility/stats", None, "daily")
    if not resp.ok:
        return Sub(error=resp.error)
    d = resp.data if isinstance(resp.data, dict) else (_rows(resp) or [{}])[0]
    iv, rv, rank = fnum(d.get("iv")), fnum(d.get("rv")), fnum(d.get("iv_rank"))
    if iv is None or rv is None or rv <= 0:
        return Sub(error="volatility stats without iv / rv")
    vrp = (iv - rv) / rv
    score = clip(-(vrp - 0.1) / 0.5)
    bits = [f"IV {iv * 100:.0f}% vs RV {rv * 100:.0f}%" + (f" (IV rank {rank:.2f})" if rank is not None else "")]
    term = c.term_structure()
    if len(term) >= 3:
        front = _pick_expiry(term, c.now, 1, 21, 7)
        back = _pick_expiry(term, c.now, 45, 120, 60)
        fv, bv = (fnum(front.get("volatility")) if front else None), (fnum(back.get("volatility")) if back else None)
        if fv and bv:
            if fv > bv * 1.1:
                score -= 0.3
                bits.append(f"term structure inverted (front {fv * 100:.0f}% > back {bv * 100:.0f}%): an event is priced")
            else:
                bits.append("term structure normal")
    return Sub(score=clip(score), value=", ".join(bits))


def f_dark_pool(c: _Ctx) -> Sub:
    resp = c.get(f"/darkpool/{c.sym}", {"limit": 200, "min_premium": int(c.e.dark_pool_min_premium)})
    if not resp.ok:
        return Sub(error=resp.error)
    rows = _rows(resp)
    num = den = 0.0
    n_quoted = above = 0
    total = 0.0
    for r in rows:
        if r.get("canceled"):
            continue
        prem = fnum(r.get("premium")) or 0.0
        total += prem
        px, bid, ask = fnum(r.get("price")), fnum(r.get("nbbo_bid")), fnum(r.get("nbbo_ask"))
        if px is None or bid is None or ask is None or ask <= bid:
            continue
        pos = clip(((px - (bid + ask) / 2) / ((ask - bid) / 2)))
        n_quoted += 1
        above += 1 if pos > 0.5 else 0
        num += pos * prem
        den += prem
    if not rows:
        return Sub(error=f"no off-exchange prints >= {_money(c.e.dark_pool_min_premium)} in the last session")
    if den <= 0:
        return Sub(error=f"{len(rows)} prints but none carried an NBBO quote")
    return Sub(score=clip(num / den), value=f"{_money(total)} in {len(rows)} prints >= {_money(c.e.dark_pool_min_premium)}; of {n_quoted} quoted prints {above / n_quoted * 100:.0f}% went off at or above the offer")


def f_relative_volume(c: _Ctx) -> Sub:
    resp = c.get(f"/stock/{c.sym}/unusualness")
    if not resp.ok:
        return Sub(error=resp.error)
    d = resp.data if isinstance(resp.data, dict) else (_rows(resp) or [{}])[0]
    p = fnum(d.get("stock_vol_pctile"))
    if p is None:
        return Sub(error="no stock-volume percentile")
    return Sub(score=clip((p - 50) / 50), value=f"stock volume in the {p:.0f}th percentile of its own {d.get('stock_samples') or '~90'} sessions")


def f_short_interest(c: _Ctx) -> Sub:
    resp = c.get(f"/shorts/{c.sym}/interest-float/v2", None, "daily")
    if not resp.ok:
        return Sub(error=resp.error)
    d = resp.data if isinstance(resp.data, dict) else (_rows(resp) or [{}])[0]
    si, dtc = fnum(d.get("si_float")), fnum(d.get("days_to_cover"))
    if si is None:
        return Sub(error="no short-interest figure")
    if si > 1.5:  # some tickers come back in percent
        si = si / 100
    fuel = 0.6 * clip(si / max(c.e.high_short_float, 1e-6), 0, 1) + (0.4 * clip((dtc - 1) / 4, 0, 1) if dtc is not None else 0.0)
    c.report.short_float_pct = c.report.short_float_pct if c.report.short_float_pct is not None else round(si * 100, 2)
    c.report.float_shares = c.report.float_shares if c.report.float_shares is not None else fnum(d.get("total_float"))
    return Sub(score=clip(fuel, 0, 1), value=f"short {si * 100:.1f}% of float" + (f", {dtc:.1f} days to cover" if dtc is not None else "") + (f" (as of {d.get('market_date')})" if d.get("market_date") else ""))


def f_insiders(c: _Ctx) -> Sub:
    resp = c.get(f"/insider/{c.sym}/ticker-flow", {"limit": 200}, "daily")
    if not resp.ok:
        return Sub(error=resp.error)
    cutoff = (c.now - timedelta(days=c.e.insider_days)).date()
    buys = sells = 0.0
    nb = ns = 0
    for r in _rows(resp):
        try:
            d = pd.Timestamp(r.get("date")).date()
        except (TypeError, ValueError):
            continue
        if d < cutoff:
            continue
        prem = abs(fnum(r.get("premium")) or 0.0)
        if str(r.get("buy_sell", "")).lower().startswith("buy"):
            buys += prem
            nb += int(r.get("transactions") or 1)
        else:
            sells += prem
            ns += int(r.get("transactions") or 1)
    if buys + sells == 0:
        return Sub(score=0.0, value=f"no insider transactions in the last {c.e.insider_days} days")
    s = tilt(2 * buys, sells)
    return Sub(score=0.0 if s is None else s, value=f"insiders bought {_money(buys)} ({nb} tx) and sold {_money(sells)} ({ns} tx) in {c.e.insider_days} days")


def f_institutions(c: _Ctx) -> Sub:
    resp = c.get(f"/institution/{c.sym}/ownership", {"limit": 200}, "daily")
    if not resp.ok:
        return Sub(error=resp.error)
    rows = [r for r in _rows(resp) if r.get("units_change") is not None]
    if not rows:
        return Sub(error="no holders with a reported change")
    latest = max(str(r.get("report_date") or "") for r in rows)
    rows = [r for r in rows if str(r.get("report_date") or "") == latest] or rows
    added = trimmed = 0.0
    na = nt = 0
    for r in rows:
        ch = fnum(r.get("units_change")) or 0.0
        if ch > 0:
            added += ch
            na += 1
        elif ch < 0:
            trimmed += -ch
            nt += 1
    s = tilt(added, trimmed)
    return Sub(score=0.0 if s is None else s, value=f"13F holders: {na} added {added / 1e6:.1f}M sh, {nt} trimmed {trimmed / 1e6:.1f}M sh (quarter {latest or 'n/a'})")


_AMOUNT = re.compile(r"\$?([\d,]+)")


def _amount_mid(text: str | None) -> float:
    nums = [float(x.replace(",", "")) for x in _AMOUNT.findall(str(text or ""))]
    if not nums:
        return 0.0
    return sum(nums[:2]) / len(nums[:2])


def f_congress(c: _Ctx) -> Sub:
    resp = c.get("/congress/recent-trades", {"ticker": c.sym, "limit": 100}, "daily")
    if not resp.ok:
        return Sub(error=resp.error)
    cutoff = (c.now - timedelta(days=c.e.congress_days)).date()
    buys = sells = 0.0
    nb = ns = 0
    for r in _rows(resp):
        if str(r.get("ticker") or c.sym).upper() != c.sym:
            continue
        try:
            d = pd.Timestamp(r.get("transaction_date")).date()
        except (TypeError, ValueError):
            continue
        if d < cutoff:
            continue
        amt = _amount_mid(r.get("amounts"))
        t = str(r.get("txn_type", "")).lower()
        if t.startswith(("buy", "purchase")):
            buys += amt
            nb += 1
        elif t.startswith(("sell", "sale")):
            sells += amt
            ns += 1
    if nb + ns == 0:
        return Sub(score=0.0, value=f"no congressional trades in the last {c.e.congress_days} days")
    s = tilt(buys, sells)
    return Sub(score=0.0 if s is None else s, value=f"congress: {nb} buys (~{_money(buys)}) vs {ns} sells (~{_money(sells)}) in {c.e.congress_days} days")


_ACTION_POINTS = {"upgraded": 1.0, "downgraded": -1.0}


def f_analysts(c: _Ctx) -> Sub:
    newer = (c.now - timedelta(days=c.e.analyst_days)).strftime("%Y-%m-%d")
    resp = c.get("/screener/analysts", {"ticker": c.sym, "limit": 50, "newer_than": newer}, "daily")
    if not resp.ok:
        return Sub(error=resp.error)
    pts: list[float] = []
    targets: list[float] = []
    ups = downs = 0
    for r in _rows(resp):
        if str(r.get("ticker") or c.sym).upper() != c.sym:
            continue
        action, rec = str(r.get("action", "")).lower(), str(r.get("recommendation", "")).lower()
        if action in _ACTION_POINTS:
            p = _ACTION_POINTS[action]
        elif action == "initiated":
            p = 0.75 if rec == "buy" else -0.75 if rec == "sell" else 0.0
        else:
            p = 0.25 if rec == "buy" else -0.25 if rec == "sell" else 0.0
        pts.append(p)
        ups += 1 if p > 0.5 else 0
        downs += 1 if p < -0.5 else 0
        t = fnum(r.get("target"))
        if t:
            targets.append(t)
    if not pts:
        return Sub(score=0.0, value=f"no analyst actions in the last {c.e.analyst_days} days")
    score = clip(sum(pts) / len(pts))
    tgt = ""
    if targets and c.spot:
        avg = sum(targets) / len(targets)
        tgt = f", mean target {avg:.2f} ({(avg / c.spot - 1) * 100:+.0f}% vs spot)"
        c.report.target_price = c.report.target_price if c.report.target_price is not None else round(avg, 2)
    return Sub(score=score, value=f"{len(pts)} analyst actions in {c.e.analyst_days} days: {ups} upgrades / buy initiations, {downs} downgrades{tgt}")


def f_seasonality(c: _Ctx) -> Sub:
    resp = c.get(f"/seasonality/{c.sym}/monthly", None, "daily")
    if not resp.ok:
        return Sub(error=resp.error)
    month = c.now.month
    row = next((r for r in _rows(resp) if int(r.get("month") or 0) == month), None)
    if row is None:
        return Sub(error="no seasonality row for this month")
    years = fnum(row.get("years")) or 0
    if years < 3:
        return Sub(error=f"only {years:.0f} years of history")
    pmp, med = fnum(row.get("positive_months_perc")), fnum(row.get("median_change"))
    if pmp is None:
        return Sub(error="seasonality row without positive_months_perc")
    score = 0.5 * clip((pmp - 0.5) * 2) + (0.5 * clip(med / 0.05) if med is not None else 0.0)
    name = datetime(2000, month, 1).strftime("%B")
    return Sub(score=clip(score), value=f"{name}: up in {pmp * 100:.0f}% of {years:.0f} years" + (f", median {med * 100:+.1f}%" if med is not None else ""))


def f_earnings(c: _Ctx) -> Sub:
    resp = c.get(f"/earnings/{c.sym}", None, "daily")
    if not resp.ok:
        return Sub(error=resp.error)
    past = []
    for r in _rows(resp):
        try:
            stamp = pd.Timestamp(r.get("report_date"))
            if pd.isna(stamp):
                continue
            d = stamp.date()
        except (TypeError, ValueError):
            continue
        if d <= c.now.date() and r.get("actual_eps") is not None:
            past.append((d, r))
    if not past:
        return Sub(error="no past earnings reports")
    d, r = max(past, key=lambda x: x[0])
    actual, est = fnum(r.get("actual_eps")), fnum(r.get("street_mean_est"))
    move = fnum(r.get("post_earnings_move_1d"))
    beat = None if actual is None or est is None else (1.0 if actual > est else -1.0 if actual < est else 0.0)
    score = 0.5 * (beat or 0.0) + (0.5 * clip(move / 0.05) if move is not None else 0.0)
    rep = c.report
    since = (c.now.date() - d).days
    if rep.earnings_recent_days is None and since <= 60:
        rep.earnings_recent_days = since
    bits = [f"last report {d}: EPS {actual:g} vs {est:g} est ({'beat' if beat == 1 else 'miss' if beat == -1 else 'in line'})" if beat is not None else f"last report {d}"]
    if move is not None:
        bits.append(f"{move * 100:+.1f}% next day")
    if rep.earnings_date:
        bits.append(f"next {rep.earnings_date}")
    return Sub(score=clip(score), value=", ".join(bits))


def f_news(c: _Ctx) -> Sub:
    from .scoring import score_headline
    from .sources import finalize_news

    resp = c.get("/news/headlines", {"ticker": c.sym, "limit": 50})
    if not resp.ok:
        return Sub(error=resp.error)
    cutoff = c.now - timedelta(days=c.cfg.context.news_lookback_days)
    num = den = 0.0
    pos = neg = major = 0
    seen = {h.title.lower() for h in c.report.headlines}
    added = 0
    for r in _rows(resp):
        title = " ".join(str(r.get("headline") or "").split())
        if not title:
            continue
        try:
            when = pd.Timestamp(r.get("created_at"))
            when = when.tz_localize("UTC") if when.tzinfo is None else when.tz_convert("UTC")
        except (TypeError, ValueError):
            continue
        if when.to_pydatetime() < cutoff:
            continue
        label = str(r.get("sentiment") or "").lower()
        s = 1.0 if label.startswith("pos") else -1.0 if label.startswith("neg") else 0.0
        w = 2.0 if r.get("is_major") else 1.0
        num += s * w
        den += w
        pos += 1 if s > 0 else 0
        neg += 1 if s < 0 else 0
        major += 1 if r.get("is_major") else 0
        if title.lower() not in seen:
            hs, tags = score_headline(title)
            c.report.headlines.append(Headline(when=when.isoformat(), title=title, source=str(r.get("source") or "unusual_whales"), url="", score=hs if s == 0 else s, tags=tags))
            seen.add(title.lower())
            added += 1
    if den == 0:
        return Sub(score=0.0, value=f"no Unusual Whales headlines in the last {c.cfg.context.news_lookback_days} days")
    if added and c.cfg.context.news_enabled:
        finalize_news(c.report, c.now)
    return Sub(score=clip(num / den), value=f"{int(pos + neg + (den - pos - neg))} headlines: {pos} positive / {neg} negative ({major} major)")


def _tide(resp: UWResponse) -> tuple[float | None, str]:
    if not resp.ok:
        return None, resp.error or "no answer"
    rows = [r for r in _rows(resp) if r.get("net_call_premium") is not None]
    if not rows:
        return None, "no tide ticks"
    last = max(rows, key=lambda r: str(r.get("timestamp") or ""))
    nc, np_ = fnum(last.get("net_call_premium")) or 0.0, fnum(last.get("net_put_premium")) or 0.0
    s = tilt(nc, np_)
    return (0.0 if s is None else s), f"net call {_money(nc)} vs net put {_money(np_)} at {str(last.get('timestamp') or '')[11:16]}"


def f_market_tide(c: _Ctx) -> Sub:
    s, note = _tide(c.get("/market/market-tide", {"interval_5m": "true"}, "market"))
    return Sub(score=s, value=note) if s is not None else Sub(error=note)


def f_sector_tide(c: _Ctx) -> Sub:
    sector = c.report.sector or c.info.get("sector")
    if not sector:
        return Sub(error="sector unknown (info endpoint gave none)")
    if sector not in UW_SECTORS:
        return Sub(applicable=False, value=f"no sector tide for '{sector}'")
    s, note = _tide(c.get(f"/market/{sector}/sector-tide", None, "market"))
    return Sub(score=s, value=f"{sector}: {note}") if s is not None else Sub(error=note)


FUNCS: dict[str, Callable[[_Ctx], Sub]] = {
    "flow": f_flow, "net_premium": f_net_premium, "oi_change": f_oi_change, "dealer_delta": f_dealer_delta, "gamma": f_gamma,
    "option_sentiment": f_option_sentiment, "options_pulse": f_options_pulse, "skew": f_skew, "max_pain": f_max_pain, "volatility": f_volatility,
    "dark_pool": f_dark_pool, "relative_volume": f_relative_volume, "short_interest": f_short_interest,
    "insiders": f_insiders, "institutions": f_institutions, "congress": f_congress, "analysts": f_analysts,
    "seasonality": f_seasonality, "earnings": f_earnings, "news": f_news, "market_tide": f_market_tide, "sector_tide": f_sector_tide,
}


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def enabled_features(cfg) -> list[tuple[Feature, float]]:
    out = []
    for name, w in (cfg.edge.weights or {}).items():
        f = FEATURE_BY_NAME.get(name)
        try:
            w = float(w)
        except (TypeError, ValueError):
            continue
        if f is not None and w > 0:
            out.append((f, w))
    return out


def summarise(features: dict[str, dict], cfg) -> dict[str, Any]:
    """Weighted score + coverage + pass flag from per-feature records."""
    e = cfg.edge
    applicable = {n: r for n, r in features.items() if r.get("applicable", True)}
    answered = {n: r for n, r in applicable.items() if r.get("score") is not None}
    w_app = sum(float(r["weight"]) for r in applicable.values())
    w_ans = sum(float(r["weight"]) for r in answered.values())
    score = round(sum(float(r["weight"]) * float(r["score"]) for r in answered.values()) / w_ans, 3) if w_ans > 0 else None
    coverage = round(w_ans / w_app, 3) if w_app > 0 else 0.0
    contrib = sorted(((n, float(r["weight"]) * float(r["score"])) for n, r in answered.items()), key=lambda x: x[1])
    positives = [n for n, v in reversed(contrib) if v > 0.02][:4]
    negatives = [n for n, v in contrib if v < -0.02][:4]
    missing = [n for n, r in applicable.items() if r.get("score") is None]
    passed = None
    if e.gate:
        passed = bool(score is not None and score >= e.threshold and coverage >= e.min_coverage)
    return {
        "score": score,
        "coverage": coverage,
        "threshold": e.threshold,
        "min_coverage": e.min_coverage,
        "gate": e.gate,
        "passed": passed,
        "answered": len(answered),
        "applicable": len(applicable),
        "positives": positives,
        "negatives": negatives,
        "missing": missing,
    }


def compute_edge(report: ContextReport, cfg, client: UWClient, now: datetime | None = None) -> dict[str, Any]:
    """Evaluate every enabled feature for ``report.symbol`` and fill
    ``report.edge`` / ``report.edge_score``. Never raises."""
    now = now or datetime.now(timezone.utc)
    e = cfg.edge
    feats = enabled_features(cfg)
    out: dict[str, Any] = {"features": {}, "calls": 0, "cached": 0, "asof": now.isoformat(timespec="minutes")}
    if not client.key:
        out.update(summarise({}, cfg))
        out["error"] = "no UNUSUAL_WHALES_API_KEY"
        report.edge, report.edge_score = out, None
        report.available["uw_edge"] = False
        report.errors["uw_edge"] = out["error"]
        return out
    c = _Ctx(report, cfg, client, now)
    hits_before = client.hits
    c.load_basics()
    for f, w in feats:
        if f.needs_options and c.has_options is False:
            sub = Sub(applicable=False, value="no listed options")
        else:
            try:
                sub = FUNCS[f.name](c)
            except Exception as exc:  # a parsing surprise must never kill the scan, and must be visible
                log.debug("edge feature %s failed for %s", f.name, c.sym, exc_info=True)
                sub = Sub(error=f"{type(exc).__name__}: {exc}")
        out["features"][f.name] = {
            "label": f.label,
            "group": f.group,
            "weight": w,
            "score": None if sub.score is None else round(float(sub.score), 3),
            "value": sub.value,
            "error": sub.error,
            "applicable": sub.applicable,
        }
    out["calls"] = client.calls - c.calls_before
    out["cached"] = client.hits - hits_before
    out["spot"] = c.spot
    out["has_options"] = c.has_options
    out.update(summarise(out["features"], cfg))
    report.edge, report.edge_score = out, out["score"]
    report.available["uw_edge"] = out["score"] is not None
    if out["score"] is None:
        report.errors["uw_edge"] = "no feature answered" if feats else "no features enabled"
    elif out["missing"]:
        report.errors["uw_edge"] = "unavailable: " + ", ".join(out["missing"])
    client.save_cache()
    return out


def edge_gaps(edge: dict | None, cfg) -> list[str]:
    """Data-gap lines for a plan: what the edge score could not see."""
    if not cfg.edge.enabled:
        return []
    if not edge:
        return ["Unusual Whales edge score not computed for this symbol (beyond the per-cycle cap or context not gathered)"]
    if edge.get("error"):
        return [f"Unusual Whales edge score not computed: {edge['error']}"]
    feats = edge.get("features") or {}
    missing = [f"{feats[n].get('label', n)} ({feats[n].get('error') or 'no answer'})" for n in edge.get("missing", []) if n in feats]
    gaps = []
    if missing:
        gaps.append("Unusual Whales features unavailable (scored as missing, not as neutral): " + "; ".join(missing))
    if edge.get("score") is None:
        gaps.append("Unusual Whales edge score unavailable: no feature answered")
    elif cfg.edge.gate and edge.get("coverage", 0) < cfg.edge.min_coverage:
        gaps.append(f"Unusual Whales edge coverage {edge.get('coverage', 0) * 100:.0f}% is below the {cfg.edge.min_coverage * 100:.0f}% required to trust the score")
    return gaps


def describe_edge(edge: dict | None, cfg) -> str:
    """One sentence for the rationale."""
    e = cfg.edge
    if not e.enabled:
        return "Unusual Whales edge score is off (edge.enabled: false)."
    if not edge or edge.get("score") is None:
        why = (edge or {}).get("error") or "no feature answered"
        return f"Unusual Whales edge score UNAVAILABLE ({why})" + (" - the entry threshold cannot be verified, so the gate fails." if e.gate else ".")
    feats = edge.get("features") or {}

    def name(n: str) -> str:
        r = feats.get(n, {})
        return f"{r.get('label', n).lower()} {r.get('score', 0):+.2f}"

    pos = ", ".join(name(n) for n in edge.get("positives", [])) or "nothing material"
    neg = ", ".join(name(n) for n in edge.get("negatives", [])) or "nothing material"
    verdict = ""
    if e.gate:
        verdict = " PASSES the entry threshold." if edge.get("passed") else (
            f" FAILS: coverage below {e.min_coverage * 100:.0f}%." if edge.get("coverage", 0) < e.min_coverage else f" FAILS the {e.threshold:+.2f} entry threshold."
        )
    return (
        f"Unusual Whales edge score {edge['score']:+.2f} (threshold {e.threshold:+.2f}; {edge.get('answered', 0)}/{edge.get('applicable', 0)} features answered, "
        f"coverage {edge.get('coverage', 0) * 100:.0f}%). For: {pos}. Against: {neg}.{verdict}"
    )
