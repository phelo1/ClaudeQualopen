"""Weekly unusual-options review: flag trades that look like somebody knows something.

Runs on Saturday (``insider_scan.weekday`` / ``run_time``) over the past
``lookback_days``. It is *research*, not trading: the output is a ranked list
of tickers whose options tape over the week was unusual enough to be worth a
human's time, each with the facts behind the flag and - when a model is
configured - a strict-JSON analysis of what catalyst the trades could be
tied to and what the buyer may be speculating on.

What "unusual" means here (all from Unusual Whales, nothing inferred from
price alone):

* market-wide **flow alerts** (``/option-trades/flow-alerts``) for the week,
  filtered server-side by the ``unusual`` preset plus our own premium,
  volume/OI, DTE, OTM-distance and ask-side thresholds, then paged by time;
* the daily **unusual-contract screen** (``/option-activity/unusual``) for
  each session in the window, when ``contract_screen`` is on.

Per ticker the week is aggregated and scored 0..10 (see ``score_ticker``):
premium size, how far out of the money, how short-dated, how aggressive
(sweeps, ask-side fills, repeated days), how one-directional, whether the
contracts expire *before* the next scheduled earnings (a scheduled event
explains a lot of aggressive positioning; positioning that expires before
it does not have that excuse). Index / ETF products are excluded by
default - they are hedging vehicles, not insider vehicles.

Every flagged ticker is a **lead to investigate, not an accusation**. The AI
step is told the same: reason only from the facts in the bundle, prefer the
boring explanation when public information supplies one, and say what to
check next. When the API key or the model is missing the report says so
(``unavailable`` / ``data_gaps``); nothing is filled in.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from .config import InsiderScanSettings, StrategyConfig
from .market_calendar import is_trading_day
from .redact import describe_error
from .uw import UWClient, fnum, has_key

log = logging.getLogger(__name__)

ISSUE_TYPES = ["Common Stock", "ADR"]
PAGE = 200
HISTORY_WEEKS = 12

SYSTEM_PROMPT = """You are a forensic options-flow analyst supporting a research desk.

You receive one ticker's unusual options activity for the past week (aggregated
from Unusual Whales flow alerts and unusual-contract screens) together with the
public context that could be gathered: recent headlines, the next scheduled
earnings date, insider (Form 4) transactions, sector and market cap. Your job is
to explain what the buyers could be positioning for.

Rules:
- Reason ONLY from the facts in the bundle. Never invent news, dates, filings,
  people or numbers. If the bundle does not contain something, say it is unknown.
- A scheduled, public event that the contracts straddle (earnings, FDA date,
  investor day, index rebalance, product launch) lowers suspicion: the trades
  are then ordinary event speculation. Contracts that expire BEFORE the next
  scheduled event, with no public catalyst in the headlines, raise it.
- Use "consistent with" language. A flag is a lead for a human to investigate,
  never an accusation of wrongdoing against anyone.
- Be concrete: which strike/expiry cluster implies which price move by which
  date, and therefore which kind of catalyst would have to happen. The flag's
  "cluster" is the dominant bet; "volume_surge" is that week's peak option
  volume vs the 30-day average; "ticker_volume" is how busy the chain is.
- The profile you are matching against is the one seen the day before
  takeovers, FDA decisions and guidance shocks: out-of-the-money calls (or
  puts) a few weeks out, bought aggressively, concentrated in one strike, in
  a normally quiet chain, with no scheduled event inside their life. Say how
  well the bundle fits it and what would fit better.
- Reply with ONE JSON object only, no prose around it, with exactly these keys:
  verdict            one of "investigate" | "likely_explained" | "noise"
  suspicion          number 0..1 (how much the activity looks informed rather than routine)
  direction          one of "bullish" | "bearish" | "mixed"
  speculating_on     <= 60 words: the move (size, direction, deadline) the positioning pays off on
  possible_catalysts array of {"catalyst": str, "likelihood": "high"|"medium"|"low", "basis": str}
  explained_by_public_info  boolean - true when the headlines / calendar in the bundle already explain the trades
  what_to_check      array of <= 6 short strings a human should verify next
  risks              array of <= 5 short strings (ways this read could be wrong)
  summary            <= 80 words for a dashboard card
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["investigate", "likely_explained", "noise"]},
        "suspicion": {"type": "number"},
        "direction": {"type": "string", "enum": ["bullish", "bearish", "mixed"]},
        "speculating_on": {"type": "string"},
        "possible_catalysts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"catalyst": {"type": "string"}, "likelihood": {"type": "string"}, "basis": {"type": "string"}},
                "required": ["catalyst", "likelihood", "basis"],
            },
        },
        "explained_by_public_info": {"type": "boolean"},
        "what_to_check": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["verdict", "suspicion", "direction", "speculating_on", "possible_catalysts", "explained_by_public_info", "what_to_check", "risks", "summary"],
}

VERDICTS = ("investigate", "likely_explained", "noise")


# --------------------------------------------------------------------------- #
# Window & fetch
# --------------------------------------------------------------------------- #
def scan_window(now: datetime, lookback_days: int) -> tuple[date, date]:
    """(first, last) calendar dates covered: the ``lookback_days`` ending yesterday
    (a Saturday run therefore covers Monday .. Friday of the week just ended)."""
    end = now.date() - timedelta(days=1)
    return end - timedelta(days=lookback_days - 1), end


def sessions_in(start: date, end: date) -> list[date]:
    d, out = start, []
    while d <= end:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


@dataclass
class Pull:
    alerts: list[dict] = field(default_factory=list)
    contracts: list[dict] = field(default_factory=list)  # daily unusual-contract rows, each tagged with ``date``
    calls: int = 0
    pages: int = 0
    errors: list[str] = field(default_factory=list)
    truncated: bool = False  # hit max_pages before the window was exhausted


def _alert_params(ins: InsiderScanSettings) -> dict[str, Any]:
    p: dict[str, Any] = {
        "unusual": "true",
        "min_premium": int(ins.min_premium),
        "min_volume_oi_ratio": ins.min_volume_oi_ratio,
        "max_dte": ins.max_dte,
        "min_diff": round(ins.min_otm_pct / 100.0, 4),
        "min_ask_perc": ins.min_ask_side_pct,
        "issue_types[]": ISSUE_TYPES,
        "limit": PAGE,
    }
    if ins.min_market_cap is not None:
        p["min_marketcap"] = int(ins.min_market_cap)
    if ins.max_market_cap is not None:
        p["max_marketcap"] = int(ins.max_market_cap)
    if not ins.include_puts:
        p["is_call"] = "true"
    return p


def pull_week(client: UWClient, ins: InsiderScanSettings, start: date, end: date) -> Pull:
    """Page the week's flow alerts by time, then the daily unusual-contract screens."""
    out = Pull()
    excluded = {t.upper() for t in ins.exclude_tickers}
    params = _alert_params(ins)
    params["newer_than"] = start.isoformat()
    older: str | None = (end + timedelta(days=1)).isoformat()
    seen_ids: set[str] = set()
    for _ in range(max(1, ins.max_pages)):
        p = dict(params)
        if older:
            p["older_than"] = older
        before = client.calls
        resp = client.get("/option-trades/flow-alerts", p, ttl=0.0)
        out.calls += client.calls - before
        out.pages += 1
        if not resp.ok:
            out.errors.append(f"flow-alerts page {out.pages}: {resp.error}")
            break
        rows = [r for r in (resp.data if isinstance(resp.data, list) else []) if isinstance(r, dict)]
        fresh = 0
        oldest: str | None = None
        for r in rows:
            key = f"{r.get('option_chain')}|{r.get('created_at')}|{r.get('total_premium')}"
            created = str(r.get("created_at") or "")
            if oldest is None or created < oldest:
                oldest = created
            if key in seen_ids:
                continue
            seen_ids.add(key)
            sym = str(r.get("ticker") or "").upper()
            if not sym or sym in excluded:
                continue
            if created[:10] and (created[:10] < start.isoformat() or created[:10] > end.isoformat()):
                continue
            out.alerts.append(r)
            fresh += 1
        if len(rows) < PAGE or not oldest or oldest[:10] < start.isoformat() or fresh == 0:
            break
        older = oldest
    else:
        out.truncated = True

    if ins.contract_screen:
        for d in sessions_in(start, end):
            p = {
                "unusual": "true",
                "min_premium": int(ins.min_premium),
                "max_dte": ins.max_dte,
                "issue_types[]": ISSUE_TYPES,
                "order": "premium",
                "order_direction": "desc",
                "limit": PAGE,
                "date": d.isoformat(),
            }
            before = client.calls
            resp = client.get("/option-activity/unusual", p, ttl=6 * 86400)  # a past session never changes
            out.calls += client.calls - before
            if not resp.ok:
                out.errors.append(f"unusual contracts {d}: {resp.error}")
                continue
            for r in resp.data if isinstance(resp.data, list) else []:
                if not isinstance(r, dict):
                    continue
                sym = str(r.get("ticker") or _ticker_from_symbol(r.get("option_symbol")) or "").upper()
                if not sym or sym in excluded:
                    continue
                out.contracts.append({**r, "ticker": sym, "date": d.isoformat()})
    return out


def _ticker_from_symbol(option_symbol: Any) -> str | None:
    """'MSFT231222C00375000' -> 'MSFT' (OCC symbols: ticker then 6-digit date)."""
    s = str(option_symbol or "")
    for i, ch in enumerate(s):
        if ch.isdigit():
            return s[:i] or None
    return None


# --------------------------------------------------------------------------- #
# Aggregate & score
# --------------------------------------------------------------------------- #
def _otm_pct(strike: Any, under: Any, typ: str) -> float | None:
    s, u = fnum(strike), fnum(under)
    if not s or not u:
        return None
    diff = (s - u) / u
    return round((diff if typ == "call" else -diff) * 100, 1)


def _dte(expiry: Any, when: date) -> int | None:
    try:
        return max((pd.Timestamp(expiry).date() - when).days, 0)
    except Exception:
        return None


def _days_to(target: Any, when: date) -> int | None:
    try:
        return (pd.Timestamp(target).date() - when).days
    except Exception:
        return None


def summarise_trade(a: dict) -> dict:
    typ = str(a.get("type") or "").lower()
    created = str(a.get("created_at") or "")
    when = pd.Timestamp(created).date() if created else None
    ask = fnum(a.get("total_ask_side_prem")) or 0.0
    bid = fnum(a.get("total_bid_side_prem")) or 0.0
    prem = fnum(a.get("total_premium")) or (ask + bid)
    ask_share = ask / prem if prem else None
    return {
        "when": created[:16].replace("T", " "),
        "date": created[:10],
        "type": typ,
        "strike": fnum(a.get("strike")),
        "expiry": str(a.get("expiry") or "")[:10],
        "dte": _dte(a.get("expiry"), when) if when else None,
        "premium": round(prem),
        "ask_share": round(ask_share, 2) if ask_share is not None else None,
        "size": a.get("total_size"),
        "volume": a.get("volume"),
        "open_interest": a.get("open_interest"),
        "vol_oi": round(fnum(a.get("volume_oi_ratio")) or 0.0, 2),
        "otm_pct": _otm_pct(a.get("strike"), a.get("underlying_price"), typ),
        "underlying": fnum(a.get("underlying_price")),
        "sweep": bool(a.get("has_sweep")),
        "floor": bool(a.get("has_floor")),
        "all_opening": bool(a.get("all_opening_trades")),
        "rule": a.get("alert_rule"),
        "chain": a.get("option_chain"),
    }


def summarise_contract(c: dict) -> dict:
    typ = str(c.get("option_type") or "").lower()
    when = pd.Timestamp(c["date"]).date() if c.get("date") else None
    vol, oi = fnum(c.get("volume")) or 0.0, fnum(c.get("open_interest")) or 0.0
    ask, bid = fnum(c.get("ask_side_volume")) or 0.0, fnum(c.get("bid_side_volume")) or 0.0
    return {
        "date": c.get("date"),
        "type": typ,
        "strike": fnum(c.get("strike")),
        "expiry": str(c.get("expiry") or "")[:10],
        "dte": _dte(c.get("expiry"), when) if when else None,
        "premium": round(fnum(c.get("premium")) or 0.0),
        "volume": vol,
        "open_interest": oi,
        "vol_oi": round(vol / max(oi, 1.0), 2),
        "ask_share": round(ask / (ask + bid), 2) if (ask + bid) else None,
        "sweep_volume": fnum(c.get("sweep_volume")),
        "otm_pct": _otm_pct(c.get("strike"), c.get("stock_price"), typ),
        "underlying": fnum(c.get("stock_price")),
        "next_earnings_date": c.get("next_earnings_date"),
        "sector": c.get("sector"),
        "symbol": c.get("option_symbol"),
        "prev_oi": fnum(c.get("prev_oi")),
        "is_new": bool(c.get("is_new")) if c.get("is_new") is not None else None,
        "ticker_volume": fnum(c.get("ticker_vol")),  # the ticker's whole option volume that day (how crowded the chain is)
        "vol_pctile_15d": fnum(c.get("vol_pctile_15d")),
    }


def _direction_of(t: dict) -> str:
    """bull / bear for one trade: ask-side calls and bid-side puts are bullish."""
    ask = t.get("ask_share")
    if t["type"] == "call":
        return "bull" if ask is None or ask >= 0.5 else "bear"
    if t["type"] == "put":
        return "bear" if ask is None or ask >= 0.5 else "bull"
    return "neutral"


def _band(value: float | None, bands: list[tuple[float, float]], default: float = 0.0) -> float:
    """First (threshold, points) whose threshold ``value`` reaches, scanning from the top."""
    if value is None:
        return default
    for threshold, points in bands:
        if value >= threshold:
            return points
    return default


def dominant_cluster(rows: list[dict]) -> dict | None:
    """The (type, strike, expiry) that took the most premium - the bet itself.

    Informed positioning tends to be one idea: one strike or two, one expiry,
    hit repeatedly. Scattered premium across a whole chain is what market
    makers and funds hedging look like.
    """
    if not rows:
        return None
    prem: dict[tuple, float] = defaultdict(float)
    for r in rows:
        prem[(r.get("type"), r.get("strike"), r.get("expiry"))] += r.get("premium") or 0.0
    total = sum(prem.values())
    (typ, strike, expiry), p = max(prem.items(), key=lambda kv: kv[1])
    members = [r for r in rows if (r.get("type"), r.get("strike"), r.get("expiry")) == (typ, strike, expiry)]
    otm = [r["otm_pct"] for r in members if r.get("otm_pct") is not None]
    dte = [r["dte"] for r in members if r.get("dte") is not None]
    return {
        "type": typ, "strike": strike, "expiry": expiry, "premium": round(p),
        "share": round(p / total, 2) if total else 0.0,
        "otm_pct": round(sum(otm) / len(otm), 1) if otm else None,
        "dte": min(dte) if dte else None,
        "hits": len(members),
        "vol_oi": max((r.get("vol_oi") or 0.0) for r in members),
    }


def score_ticker(
    trades: list[dict], contracts: list[dict], asof: date, *,
    next_earnings: str | None = None, volume_surge: float | None = None, market_cap: float | None = None,
    stock: dict | None = None,
) -> dict:
    """0..10 with a breakdown, tuned to the profile that precedes M&A / FDA /
    guidance surprises: out-of-the-money calls (or puts), a few weeks out,
    bought aggressively (sweeps, at the ask, opening), concentrated in one
    strike/expiry, in a chain that is normally quiet, with no scheduled event
    inside the contracts' life.

    ``volume_surge`` (that week's peak daily option volume vs the 30-day
    average) and ``next_earnings`` come from the enrichment step when the
    scan could afford the calls; ``market_cap`` is reported, not scored (the
    cap filter is applied by the caller). ``stock`` is the underlying's
    backdrop from :func:`stock_backdrop` (was the share price quiet when the
    bet went on? is the strike beyond the 52-week range?).
    """
    prem_total = sum(t["premium"] for t in trades) + sum(c["premium"] for c in contracts if not trades)
    bull = sum(t["premium"] for t in trades if _direction_of(t) == "bull") + sum(c["premium"] for c in contracts if not trades and _direction_of(c) == "bull")
    bear = sum(t["premium"] for t in trades if _direction_of(t) == "bear") + sum(c["premium"] for c in contracts if not trades and _direction_of(c) == "bear")
    rows = trades or contracts
    all_rows = trades + contracts
    vol_oi = max((r.get("vol_oi") or 0.0) for r in rows) if rows else 0.0
    otm = max((r.get("otm_pct") or 0.0) for r in rows) if rows else 0.0
    dtes = [r["dte"] for r in rows if r.get("dte") is not None]
    min_dte = min(dtes) if dtes else None
    days = {r.get("date") for r in rows if r.get("date")}
    sweeps = sum(1 for t in trades if t.get("sweep")) + sum(1 for c in contracts if (c.get("sweep_volume") or 0) > 0)
    aggressive = sum(1 for r in rows if (r.get("ask_share") or 0) >= 0.8)
    opening = sum(1 for t in trades if t.get("all_opening"))
    cluster = dominant_cluster(rows)
    ticker_volume = max((c.get("ticker_volume") or 0.0) for c in contracts) if contracts else None
    fresh = opening > 0 or any(c.get("is_new") or ((c.get("prev_oi") is not None) and c["prev_oi"] * 5 < (c.get("volume") or 0)) for c in contracts)

    breakdown: dict[str, float] = {}
    # Size matters less than shape: $300k -> 0.5, $1m -> 1.0, $3m+ -> 1.5. A
    # $50m block in a mega cap is routine institutional business, not a tell.
    breakdown["premium"] = round(min(1.5, max(0.0, math.log10(max(prem_total, 1.0)) - 5.0)), 2)
    # Volume vs open interest: new positions, not existing holders trading around.
    breakdown["vol_oi"] = _band(vol_oi, [(10, 1.5), (5, 1.0), (2, 0.5)])
    # How far out of the money the *bet* (dominant cluster) is: 10-35 % is the
    # sweet spot (ZEN $70s at 24 % OTM, Heinz $65s at 8 %), beyond 60 % is lottery.
    c_otm = cluster["otm_pct"] if cluster and cluster.get("otm_pct") is not None else otm
    breakdown["otm"] = 0.0 if c_otm is None or c_otm < 5 else 0.75 if c_otm < 10 else 1.5 if c_otm <= 35 else 1.0 if c_otm <= 60 else 0.5
    # Days to expiry of the bet: a few weeks out is the informed window; 0-2
    # DTE is day-trading, beyond 45 is ordinary positioning.
    c_dte = cluster["dte"] if cluster and cluster.get("dte") is not None else min_dte
    breakdown["expiry_window"] = 0.0 if c_dte is None else 0.25 if c_dte <= 2 else 1.0 if c_dte <= 7 else 1.5 if c_dte <= 45 else 0.75 if c_dte <= 60 else 0.25
    urgency = 0.0
    if sweeps:
        urgency += 0.5
    if aggressive:
        urgency += 0.5
    if len(days) >= 2:
        urgency += 0.5
    if opening:
        urgency += 0.5
    breakdown["urgency"] = round(min(1.5, urgency), 2)
    conviction = max(bull, bear) / prem_total if prem_total else 0.0
    breakdown["one_directional"] = 0.5 if len(all_rows) < 2 else round(1.0 if conviction >= 0.8 else 0.5 if conviction >= 0.65 else 0.0, 2)
    # One idea, hit repeatedly: the dominant strike/expiry's share of the premium.
    share = cluster["share"] if cluster else 0.0
    breakdown["concentration"] = 0.5 if len(all_rows) < 2 else (1.0 if share >= 0.6 else 0.5 if share >= 0.4 else 0.0)
    breakdown["fresh_position"] = 0.5 if fresh else 0.0
    # The GoPro tell: the whole chain traded many times its normal volume.
    breakdown["volume_surge"] = _band(volume_surge, [(20, 2.0), (10, 1.5), (5, 1.0), (3, 0.5)])
    # A chain that trades hundreds of thousands of contracts a day (AAPL,
    # META, MSTR...) produces 'unusual' prints every session; nothing there
    # is a quiet tell.
    breakdown["crowded_chain"] = -_band(ticker_volume, [(500_000, 1.5), (200_000, 1.0), (75_000, 0.5)])

    # Earnings: contracts expiring before the next scheduled report have no
    # scheduled excuse; expiring after it is ordinary event speculation.
    next_er = next_earnings or next((c.get("next_earnings_date") for c in contracts if c.get("next_earnings_date")), None)
    er_days = _days_to(next_er, asof) if next_er else None
    expiries = [r.get("expiry") for r in rows if r.get("expiry")]
    earliest_exp = min(expiries) if expiries else None
    er_adj = 0.0
    er_note = "next earnings date unknown"
    if er_days is not None and earliest_exp:
        exp_days = _days_to(earliest_exp, asof)
        if exp_days is not None and 0 <= er_days and exp_days < er_days:
            er_adj, er_note = 1.5, f"earliest expiry {earliest_exp} is BEFORE the next earnings ({next_er}): no scheduled excuse"
        elif er_days >= 0:
            er_adj, er_note = -1.0, f"contracts straddle the next earnings ({next_er}): ordinary event speculation is plausible"
        else:
            er_note = f"next earnings date {next_er} already passed"
    breakdown["pre_earnings"] = er_adj

    # The same contract bought on several sessions: someone building a
    # position on purpose (Cerevel's calls went on over a week), as opposed
    # to one print that could be anyone's hedge.
    cluster_days = len({r.get("date") for r in rows if r.get("date") and cluster and (r.get("type"), r.get("strike"), r.get("expiry")) == (cluster["type"], cluster["strike"], cluster["expiry"])})
    breakdown["repeat_buyer"] = 1.0 if cluster_days >= 3 else 0.75 if cluster_days == 2 else 0.0

    # The underlying's own tape when the bet went on. Informed buyers move
    # before the stock does: a flat share price on normal volume is the
    # profile (a small credit - most stocks are quiet most weeks); a stock
    # that has already run 10 % in the bet's direction is being chased, not
    # foreseen, and that costs more than the quiet case earns.
    quiet = 0.0
    beyond = 0.0
    if stock:
        ret, vr = stock.get("ret_5d_pct"), stock.get("volume_ratio")
        side = direction_of_premium(bull, bear)
        toward = (ret or 0.0) * (1 if side == "bullish" else -1 if side == "bearish" else 0)
        if ret is not None:
            if toward >= 10.0:
                quiet = -1.0
            elif toward >= 6.0:
                quiet = -0.5
            elif abs(ret) < 3.0 and (vr is None or vr < 1.5):
                quiet = 0.5
        # A call strike clear above the 52-week high (put clear below the
        # low) is a bet on a price the stock has not seen in a year - the
        # takeover-price profile - not a swing trade.
        if stock.get("strike_beyond_52w"):
            beyond = 0.75
    breakdown["stock_quiet"] = quiet
    breakdown["strike_beyond_52w"] = beyond

    raw = sum(breakdown.values())
    # The displayed score is capped at 10 ("everything about this fits the
    # profile"); ``raw`` keeps ranking meaningful among the textbook cases.
    score = max(0.0, min(10.0, raw))
    direction = direction_of_premium(bull, bear)
    return {
        "score": round(score, 2),
        "raw": round(raw, 2),
        "breakdown": breakdown,
        "direction": direction,
        "premium_total": round(prem_total),
        "bull_premium": round(bull),
        "bear_premium": round(bear),
        "max_vol_oi": round(vol_oi, 2),
        "max_otm_pct": round(otm, 1),
        "min_dte": min_dte,
        "days_active": sorted(d for d in days if d),
        "sweeps": sweeps,
        "aggressive_fills": aggressive,
        "cluster": cluster,
        "ticker_volume": ticker_volume,
        "volume_surge": round(volume_surge, 1) if volume_surge is not None else None,
        "market_cap": market_cap,
        "stock": stock,
        "next_earnings_date": next_er,
        "earliest_expiry": earliest_exp,
        "earnings_note": er_note,
    }


def direction_of_premium(bull: float, bear: float) -> str:
    return "bullish" if bull > bear * 1.5 else "bearish" if bear > bull * 1.5 else "mixed"


def stock_backdrop(df: pd.DataFrame | None, active_days: list[str], cluster: dict | None) -> dict | None:
    """What the share price was doing when the options were bought.

    * ``ret_5d_pct`` - close on the first active day vs five sessions before.
    * ``volume_ratio`` - average share volume over the active days vs the
      30 sessions before them.
    * ``high_52w`` / ``low_52w`` and ``strike_beyond_52w`` - a call strike
      above the 52-week high (or a put strike below the low) is a bet on a
      price the stock has not seen in a year: a takeover-price bet, not a
      swing trade.
    Needs daily bars up to the first active day; returns None without them.
    """
    if df is None or df.empty or not active_days:
        return None
    try:
        first = pd.Timestamp(min(active_days))
    except Exception:
        return None
    hist = df[df.index <= first]
    if len(hist) < 6:
        return None
    close = float(hist["close"].iloc[-1])
    ret_5d = close / float(hist["close"].iloc[-6]) - 1.0
    window = df[(df.index >= first) & (df.index <= pd.Timestamp(max(active_days)))]
    before = hist.iloc[-31:-1]
    vol_ratio = None
    if "volume" in df and len(before) >= 10 and float(before["volume"].mean()) > 0 and not window.empty:
        vol_ratio = float(window["volume"].mean()) / float(before["volume"].mean())
    year = hist.iloc[-252:]
    high_52w, low_52w = float(year["high"].max()), float(year["low"].min())
    beyond = None
    if cluster and cluster.get("strike") is not None:
        strike = float(cluster["strike"])
        # "Beyond" means clear of the range, not a strike a few percent past
        # a stock sitting at its own high or low.
        beyond = strike > high_52w * 1.05 if cluster.get("type") == "call" else strike < low_52w * 0.95 if cluster.get("type") == "put" else None
    return {
        "asof": hist.index[-1].date().isoformat(),
        "close": round(close, 2),
        "ret_5d_pct": round(ret_5d * 100, 1),
        "volume_ratio": round(vol_ratio, 2) if vol_ratio is not None else None,
        "high_52w": round(high_52w, 2),
        "low_52w": round(low_52w, 2),
        "strike_beyond_52w": beyond,
        "bars": len(hist),
    }


OUTCOME_SESSIONS = 10


def track_outcomes(history: list[dict], bars: dict[str, pd.DataFrame], asof: date) -> dict[str, Any]:
    """Did the flagged stocks move the way the options said they would?

    For every flag in ``history`` the entry is the first close after the
    scan week; the best move in the bet's direction over the next
    ``OUTCOME_SESSIONS`` sessions is recorded (bullish: highest high vs
    entry, bearish: lowest low). An outcome is ``done`` once those sessions
    have passed. Flags whose bars are missing stay unmeasured - nothing is
    assumed. Returns the scorecard; ``history`` is updated in place.
    """
    measured, done, hit_10, hit_20, moves = 0, 0, 0, 0, []
    flags = 0
    for week in history:
        for f in week.get("flagged") or []:
            flags += 1
            if (f.get("outcome") or {}).get("done"):
                o = f["outcome"]
            else:
                o = _outcome_for(f, week.get("week_end"), bars.get(f["ticker"]), asof)
                if o is not None:
                    f["outcome"] = o
            if not o:
                continue
            measured += 1
            moves.append(o["best_move_pct"])
            if o["done"]:
                done += 1
            if o["best_move_pct"] >= 10:
                hit_10 += 1
            if o["best_move_pct"] >= 20:
                hit_20 += 1
    return {
        "flags": flags, "measured": measured, "done": done, "hit_10": hit_10, "hit_20": hit_20,
        "avg_best_move_pct": round(sum(moves) / len(moves), 1) if moves else None,
        "sessions": OUTCOME_SESSIONS, "asof": asof.isoformat(),
    }


def _outcome_for(flag: dict, week_end: str | None, df: pd.DataFrame | None, asof: date) -> dict | None:
    if df is None or df.empty or not week_end:
        return None
    after = df[df.index > pd.Timestamp(week_end)]
    if after.empty:
        return None
    entry = float(after["close"].iloc[0])
    path = after.iloc[1 : OUTCOME_SESSIONS + 1]
    if path.empty or entry <= 0:
        return None
    direction = flag.get("direction")
    if direction == "bearish":
        best = (1.0 - float(path["low"].min()) / entry) * 100
    else:
        best = (float(path["high"].max()) / entry - 1.0) * 100
    return {
        "entry_date": after.index[0].date().isoformat(),
        "entry": round(entry, 2),
        "best_move_pct": round(best, 1),
        "sessions": int(len(path)),
        "done": len(path) >= OUTCOME_SESSIONS,
        "last": after.index[min(len(path), len(after) - 1)].date().isoformat(),
    }


def _within_dte(dte: int | None, ins: InsiderScanSettings) -> bool:
    if dte is None:
        return True
    return ins.min_dte <= dte <= ins.max_dte


def aggregate(pull: Pull, ins: InsiderScanSettings, asof: date) -> list[dict]:
    """Per-ticker summaries scored on the options data alone (before enrichment).

    Rows outside the DTE / OTM / premium / side windows are dropped here as
    well, because the daily contract screen has fewer server-side filters
    than the flow-alert feed.
    """
    by_sym_alerts: dict[str, list[dict]] = defaultdict(list)
    by_sym_contracts: dict[str, list[dict]] = defaultdict(list)
    caps: dict[str, float] = {}
    for a in pull.alerts:
        st = summarise_trade(a)
        if not _within_dte(st["dte"], ins):
            continue
        sym = str(a.get("ticker")).upper()
        by_sym_alerts[sym].append(st)
        cap = fnum(a.get("marketcap"))
        if cap:
            caps[sym] = cap
    for c in pull.contracts:
        sc = summarise_contract(c)
        if sc["premium"] < ins.min_premium or (sc["otm_pct"] is not None and sc["otm_pct"] < ins.min_otm_pct):
            continue
        if not _within_dte(sc["dte"], ins):
            continue
        if not ins.include_puts and sc["type"] == "put":
            continue
        if sc["ask_share"] is not None and sc["ask_share"] < ins.min_ask_side_pct:
            continue
        by_sym_contracts[str(c.get("ticker")).upper()].append(sc)
    out = []
    for sym in set(by_sym_alerts) | set(by_sym_contracts):
        trades = sorted(by_sym_alerts.get(sym, []), key=lambda t: t["premium"], reverse=True)
        contracts = sorted(by_sym_contracts.get(sym, []), key=lambda c: c["premium"], reverse=True)
        s = score_ticker(trades, contracts, asof, market_cap=caps.get(sym))
        out.append(
            {
                "ticker": sym,
                **s,
                "alerts": len(trades),
                "contracts_flagged": len(contracts),
                "top_trades": trades[:8],
                "contracts": contracts[:6],
                "sector": next((c.get("sector") for c in contracts if c.get("sector")), None),
                "enriched": False,
                "_trades": trades,  # full lists for the re-score after enrichment; stripped from the report
                "_contracts": contracts,
            }
        )
    out.sort(key=lambda r: (r.get("raw", r["score"]), r["premium_total"]), reverse=True)
    return out


def _outside_cap(cap: float | None, ins: InsiderScanSettings) -> bool:
    if cap is None:
        return False
    if ins.min_market_cap is not None and cap < ins.min_market_cap:
        return True
    return ins.max_market_cap is not None and cap > ins.max_market_cap


def enrich_candidates(ranked: list[dict], client: UWClient, ins: InsiderScanSettings, start: date, end: date, asof: date) -> dict[str, Any]:
    """Two paid reads for each of the top candidates, then a re-score.

    ``/stock/{t}/info`` gives the market cap (the daily contract screen has no
    cap filter, so mega caps otherwise slip through) and the next earnings
    date; ``/stock/{t}/options-volume`` gives the week's daily call / put
    volume against the 30-day average - the "50x normal volume" tell. Tickers
    outside the cap window are removed; the rest are re-ranked. A read that
    fails leaves the ticker as it was and is listed as a gap, never guessed.
    """
    stats: dict[str, Any] = {"enriched": 0, "excluded_by_cap": [], "excluded_issue_type": [], "gaps": [], "calls": 0}
    todo = [r for r in ranked if r["score"] >= ins.min_flag_score - 2.0][: max(1, ins.enrich_top)]
    keep: list[dict] = []
    for r in ranked:
        if r not in todo:
            keep.append(r)
            continue
        sym = r["ticker"]
        before = client.calls
        info = client.get(f"/stock/{sym}/info", None, ttl=6 * 86400)
        vol = client.get(f"/stock/{sym}/options-volume", {"limit": 12}, ttl=86400)
        stats["calls"] += client.calls - before
        cap, next_er, issue = r.get("market_cap"), None, None
        if info.ok and isinstance(info.data, dict):
            cap = fnum(info.data.get("marketcap")) or cap
            next_er = info.data.get("next_earnings_date")
            issue = info.data.get("issue_type")
        elif not info.ok:
            stats["gaps"].append(f"{sym} info: {info.error}")
        surge: float | None = None
        surge_note = None
        if vol.ok and isinstance(vol.data, list):
            surge = _volume_surge(vol.data, start, end, r.get("direction", "mixed"))
            if surge is None:
                surge_note = "no 30-day average in the volume history"
        elif not vol.ok:
            stats["gaps"].append(f"{sym} options-volume: {vol.error}")
        if issue and str(issue).upper() in ("ETF", "ETN", "INDEX", "FUND"):
            stats["excluded_issue_type"].append({"ticker": sym, "issue_type": issue})
            continue
        if _outside_cap(cap, ins):
            stats["excluded_by_cap"].append({"ticker": sym, "market_cap": cap, "score": r["score"]})
            continue
        s = score_ticker(r.get("_trades", r["top_trades"]), r.get("_contracts", r["contracts"]), asof, next_earnings=next_er, volume_surge=surge, market_cap=cap)
        r.update(s)
        r["enriched"] = True
        if surge_note:
            r["volume_note"] = surge_note
        stats["enriched"] += 1
        keep.append(r)
    keep.sort(key=lambda r: (r.get("raw", r["score"]), r["premium_total"]), reverse=True)
    stats["ranked"] = keep
    return stats


def _volume_surge(rows: list[Any], start: date, end: date, direction: str) -> float | None:
    """Peak (daily side volume / 30-day average of that side) over the window.
    Calls for a bullish flag, puts for a bearish one, the larger of the two when mixed."""
    best: float | None = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            d = pd.Timestamp(row.get("date")).date()
        except Exception:
            continue
        if d < start or d > end:
            continue
        ratios = []
        for side in (("call",) if direction == "bullish" else ("put",) if direction == "bearish" else ("call", "put")):
            v, avg = fnum(row.get(f"{side}_volume")), fnum(row.get(f"avg_30_day_{side}_volume"))
            if v is not None and avg:
                ratios.append(v / avg)
        if ratios:
            best = max(best or 0.0, max(ratios))
    return round(best, 1) if best is not None else None


# --------------------------------------------------------------------------- #
# Context & AI
# --------------------------------------------------------------------------- #
def gather_context(sym: str, client: UWClient | None, ins: InsiderScanSettings, now: datetime) -> dict:
    """Public facts for the analyst: headlines, earnings, insider filings, profile. Gaps are listed, not filled."""
    from .context.base import ContextReport
    from .context.sources import fetch_finviz

    rep = ContextReport(symbol=sym, asof=now.date().isoformat())
    gaps: list[str] = []
    try:
        fetch_finviz(rep, ins.context_news_days, now)
    except Exception as exc:  # pragma: no cover - finviz is best effort
        rep.errors["news_finviz"] = f"{type(exc).__name__}: {exc}"
    for name in ("news_finviz", "fundamentals"):
        if not rep.available.get(name):
            gaps.append(f"{name}: {rep.errors.get(name, 'not fetched')}")
    headlines = [{"when": h.when[:16], "title": h.title, "source": h.source, "tags": h.tags} for h in rep.headlines]
    insider_rows: list[dict] = []
    profile: dict = {}
    if client is not None:
        cutoff = (now - timedelta(days=ins.context_news_days)).replace(tzinfo=None)
        r = client.get("/news/headlines", {"ticker": sym, "limit": 50}, ttl=86400)
        if r.ok:
            seen = {h["title"].lower() for h in headlines}
            for row in r.data if isinstance(r.data, list) else []:
                title = " ".join(str(row.get("headline") or "").split())
                if not title or title.lower() in seen:
                    continue
                try:
                    when = pd.Timestamp(row.get("created_at")).tz_localize(None) if pd.Timestamp(row.get("created_at")).tzinfo is None else pd.Timestamp(row.get("created_at")).tz_convert("UTC").tz_localize(None)
                except Exception:
                    continue
                if when.to_pydatetime() < cutoff:
                    continue
                headlines.append({"when": str(when)[:16], "title": title, "source": str(row.get("source") or "unusual_whales"), "sentiment": row.get("sentiment"), "major": bool(row.get("is_major"))})
                seen.add(title.lower())
        else:
            gaps.append(f"uw news: {r.error}")
        r = client.get(f"/insider/{sym}/ticker-flow", {"limit": 50}, ttl=86400)
        if r.ok:
            for row in r.data if isinstance(r.data, list) else []:
                if not isinstance(row, dict):
                    continue
                try:
                    if pd.Timestamp(row.get("date")).date() < (now - timedelta(days=90)).date():
                        continue
                except Exception:
                    pass
                insider_rows.append({k: row.get(k) for k in ("date", "transaction_code", "buy_sell", "shares", "premium", "avg_price", "owner_name", "is_director", "is_officer", "is_ten_percent_owner", "officer_title") if k in row})
        else:
            gaps.append(f"uw insider flow: {r.error}")
        r = client.get(f"/stock/{sym}/info", None, ttl=6 * 86400)
        if r.ok and isinstance(r.data, dict):
            profile = {k: r.data.get(k) for k in ("full_name", "sector", "marketcap", "next_earnings_date", "earnings_announce_time", "issue_type", "short_description") if k in r.data}
        elif not r.ok:
            gaps.append(f"uw stock info: {r.error}")
    headlines.sort(key=lambda h: h["when"], reverse=True)
    return {
        "headlines": headlines[:25],
        "earnings_date": rep.earnings_date or profile.get("next_earnings_date"),
        "days_to_earnings": rep.days_to_earnings,
        "earnings_recent_days": rep.earnings_recent_days,
        "sector": rep.sector or profile.get("sector"),
        "industry": rep.industry,
        "market_cap": rep.market_cap or fnum(profile.get("marketcap")),
        "float_shares": rep.float_shares,
        "short_float_pct": rep.short_float_pct,
        "insider_trans_pct": rep.insider_trans_pct,
        "inst_own_pct": rep.inst_own_pct,
        "insider_filings_90d": insider_rows[:20],
        "profile": profile,
        "data_gaps": gaps,
    }


def normalise_analysis(raw: dict[str, Any], provider: str, model: str) -> dict[str, Any]:
    verdict = str(raw.get("verdict", "")).strip().lower()
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}, got {verdict!r}")
    try:
        susp = float(raw.get("suspicion", 0.0))
    except (TypeError, ValueError):
        susp = 0.0
    if susp > 1.0:
        susp /= 100.0
    direction = str(raw.get("direction", "mixed")).strip().lower()
    if direction not in ("bullish", "bearish", "mixed"):
        direction = "mixed"

    def _strs(v: Any, n: int) -> list[str]:
        if isinstance(v, str):
            v = [s.strip() for s in v.split(";")]
        return [str(x).strip() for x in (v or []) if str(x).strip()][:n]

    cats = []
    for c in raw.get("possible_catalysts") or []:
        if isinstance(c, dict):
            cats.append({"catalyst": str(c.get("catalyst", "")).strip(), "likelihood": str(c.get("likelihood", "low")).strip().lower(), "basis": str(c.get("basis", "")).strip()})
        elif isinstance(c, str) and c.strip():
            cats.append({"catalyst": c.strip(), "likelihood": "low", "basis": ""})
    return {
        "verdict": verdict,
        "suspicion": round(max(0.0, min(1.0, susp)), 3),
        "direction": direction,
        "speculating_on": str(raw.get("speculating_on", "")).strip(),
        "possible_catalysts": cats[:6],
        "explained_by_public_info": bool(raw.get("explained_by_public_info", False)),
        "what_to_check": _strs(raw.get("what_to_check"), 6),
        "risks": _strs(raw.get("risks"), 5),
        "summary": str(raw.get("summary", "")).strip(),
        "provider": provider,
        "model": model,
    }


def analyse_with_ai(flag: dict, context: dict, ins: InsiderScanSettings, window: tuple[date, date]) -> dict[str, Any]:
    """Ask the configured model for a strict-JSON read. Never raises: errors come back as {"error": ...}."""
    from .reviewer import ask_json, parse_json_object, resolve_provider

    provider, model, _ = resolve_provider(ins)
    bundle = {
        "ticker": flag["ticker"],
        "window": {"start": window[0].isoformat(), "end": window[1].isoformat()},
        "flag": {k: flag[k] for k in ("score", "breakdown", "direction", "premium_total", "bull_premium", "bear_premium", "max_vol_oi", "max_otm_pct", "min_dte", "days_active", "sweeps", "aggressive_fills", "cluster", "ticker_volume", "volume_surge", "market_cap", "stock", "earliest_expiry", "next_earnings_date", "earnings_note") if k in flag},
        "top_trades": flag.get("top_trades", []),
        "unusual_contracts": flag.get("contracts", []),
        "public_context": context,
        "instructions": "Explain what these buyers could be positioning for; prefer the boring explanation when the public context supplies one.",
    }
    try:
        text, provider, model = ask_json(bundle, ins, SYSTEM_PROMPT, RESPONSE_SCHEMA)
        return normalise_analysis(parse_json_object(text), provider, model)
    except Exception as exc:
        log.warning("insider-scan AI failed for %s: %s", flag["ticker"], describe_error(exc))
        return {"error": describe_error(exc), "provider": provider, "model": model}


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def run_weekly_scan(
    cfg: StrategyConfig,
    now: datetime | None = None,
    uw_cache_path: Path | None = None,
    client: UWClient | None = None,
    ai: bool | None = None,
    previous: dict | None = None,
    registry=None,
    bars: Callable[[list[str], str], dict[str, pd.DataFrame]] | None = None,
) -> dict:
    """The whole Saturday job. Returns the report dict that is written to ``insider_scan.json``.

    ``bars(symbols, start)`` is the desk's own price provider: with it the
    candidates get their share-price backdrop (was the stock quiet when the
    bet went on?) and earlier weeks' flags get their outcomes measured.
    """
    ins = cfg.insider_scan
    now = now or datetime.now(timezone.utc)
    start, end = scan_window(now, ins.lookback_days)
    t0 = time.perf_counter()
    report: dict[str, Any] = {
        "generated_at": now.isoformat(),
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "sessions": [d.isoformat() for d in sessions_in(start, end)],
        "thresholds": {
            "min_premium": ins.min_premium, "min_volume_oi_ratio": ins.min_volume_oi_ratio, "min_dte": ins.min_dte, "max_dte": ins.max_dte,
            "min_otm_pct": ins.min_otm_pct, "min_ask_side_pct": ins.min_ask_side_pct, "include_puts": ins.include_puts,
            "min_market_cap": ins.min_market_cap, "max_market_cap": ins.max_market_cap, "min_flag_score": ins.min_flag_score,
            "max_flagged": ins.max_flagged, "enrich_top": ins.enrich_top, "excluded": list(ins.exclude_tickers),
        },
        "source": "unusual_whales",
        "unavailable": False,
        "data_gaps": [],
        "calls": 0,
        "alerts_considered": 0,
        "contracts_considered": 0,
        "tickers_considered": 0,
        "flagged": [],
        "below_threshold": [],
        "ai": {"enabled": bool(ins.ai_enabled if ai is None else ai), "provider": None, "model": None, "analysed": 0, "errors": 0},
        "history": list((previous or {}).get("history") or [])[-HISTORY_WEEKS:],
    }
    if client is None and not has_key():
        report["unavailable"] = True
        report["data_gaps"].append("Unusual Whales API key not configured (UNUSUAL_WHALES_API_KEY): no options data, nothing scanned")
        if registry is not None:
            registry.record("insider_scan", False, detail=f"week {start} .. {end}", error="set UNUSUAL_WHALES_API_KEY")
        return report
    client = client or UWClient(cache_path=uw_cache_path)

    pull = pull_week(client, ins, start, end)
    report["calls"] = pull.calls
    report["pages"] = pull.pages
    report["alerts_considered"] = len(pull.alerts)
    report["contracts_considered"] = len(pull.contracts)
    report["data_gaps"].extend(pull.errors)
    if pull.truncated:
        report["data_gaps"].append(f"flow alerts truncated at {ins.max_pages} pages x {PAGE}; raise insider_scan.max_pages or tighten the filters")
    if not pull.alerts and not pull.contracts and pull.errors:
        report["unavailable"] = True

    ranked = aggregate(pull, ins, end)
    report["tickers_considered"] = len(ranked)
    if ins.enrich_top > 0 and ranked:
        enr = enrich_candidates(ranked, client, ins, start, end, end)
        ranked = enr["ranked"]
        report["calls"] += enr["calls"]
        report["enrichment"] = {k: enr[k] for k in ("enriched", "excluded_by_cap", "excluded_issue_type", "calls")}
        report["data_gaps"].extend(enr["gaps"][:10])
        if len(enr["gaps"]) > 10:
            report["data_gaps"].append(f"... {len(enr['gaps']) - 10} more enrichment reads failed")
    else:
        report["enrichment"] = {"enriched": 0, "excluded_by_cap": [], "excluded_issue_type": [], "calls": 0}
    price_frames: dict[str, pd.DataFrame] = {}
    if bars is not None:
        candidates = [r["ticker"] for r in ranked if r["score"] >= ins.min_flag_score - 1.5][: max(1, ins.enrich_top)]
        past = sorted({f["ticker"] for w in report["history"] for f in (w.get("flagged") or []) if not (f.get("outcome") or {}).get("done")})
        wanted = list(dict.fromkeys(candidates + past))
        if wanted:
            try:
                price_frames = bars(wanted, (start - timedelta(days=400)).isoformat()) or {}
            except Exception as exc:
                report["data_gaps"].append(f"share-price backdrop unavailable ({describe_error(exc)}): stock_quiet / 52-week checks not scored")
        for r in ranked:
            if r["ticker"] in candidates:
                backdrop = stock_backdrop(price_frames.get(r["ticker"]), r.get("days_active") or [], r.get("cluster"))
                if backdrop is None:
                    r["stock_note"] = "no daily bars for the underlying: share-price backdrop not scored"
                    continue
                s = score_ticker(
                    r.get("_trades", r["top_trades"]), r.get("_contracts", r["contracts"]), end,
                    next_earnings=r.get("next_earnings_date"), volume_surge=r.get("volume_surge"), market_cap=r.get("market_cap"), stock=backdrop,
                )
                r.update(s)
        ranked.sort(key=lambda r: (r.get("raw", r["score"]), r["premium_total"]), reverse=True)
    for r in ranked:
        r.pop("_trades", None)
        r.pop("_contracts", None)
    flagged = [r for r in ranked if r["score"] >= ins.min_flag_score][: ins.max_flagged]
    report["below_threshold"] = [
        {"ticker": r["ticker"], "score": r["score"], "direction": r["direction"], "premium_total": r["premium_total"], "alerts": r["alerts"], "market_cap": r.get("market_cap")}
        for r in ranked if r not in flagged
    ][:25]

    use_ai = report["ai"]["enabled"]
    if use_ai:
        from .reviewer import resolve

        res = resolve(ins)
        report["ai"]["provider"], report["ai"]["model"] = res.id, res.model
        if not res.configured:
            use_ai = False
            report["ai"]["enabled"] = False
            report["data_gaps"].append(f"AI analysis skipped: {res.error}")
    for flag in flagged:
        flag["context"] = gather_context(flag["ticker"], client, ins, now)
        if use_ai:
            flag["ai"] = analyse_with_ai(flag, flag["context"], ins, (start, end))
            report["ai"]["analysed"] += 1
            if "error" in flag["ai"]:
                report["ai"]["errors"] += 1
        else:
            flag["ai"] = None
    report["flagged"] = flagged
    client.save_cache()

    report["history"] = [h for h in report["history"] if h.get("week_end") != end.isoformat()]
    report["history"].append(
        {
            "week_end": end.isoformat(),
            "generated_at": now.isoformat(),
            "flagged": [{"ticker": f["ticker"], "score": f["score"], "direction": f["direction"], "verdict": (f.get("ai") or {}).get("verdict"), "cluster": f.get("cluster")} for f in flagged],
            "tickers_considered": len(ranked),
            "alerts": len(pull.alerts),
        }
    )
    report["history"] = report["history"][-HISTORY_WEEKS:]
    report["outcomes"] = track_outcomes(report["history"], price_frames, end) if bars is not None else None
    report["elapsed_seconds"] = round(time.perf_counter() - t0, 1)

    if registry is not None:
        detail = f"week {start} .. {end}: {len(pull.alerts)} alerts, {len(ranked)} tickers, {len(flagged)} flagged"
        registry.record("insider_scan", not report["unavailable"], detail=detail, items=len(flagged), degraded=bool(pull.errors), error=("; ".join(pull.errors[:2]) if report["unavailable"] else None), latency_ms=(time.perf_counter() - t0) * 1000, save=False)
        if report["ai"]["enabled"]:
            errs = report["ai"]["errors"]
            registry.record("llm_insider", errs < max(1, report["ai"]["analysed"]), detail=f"{report['ai']['provider']} / {report['ai']['model']}: {report['ai']['analysed'] - errs}/{report['ai']['analysed']} analyses", degraded=errs > 0, error=(next((f["ai"]["error"] for f in flagged if f.get("ai") and "error" in f["ai"]), None) if errs else None), save=False)
        registry.save()
    return report


def load_report(path: Path) -> dict | None:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return None
    return None
