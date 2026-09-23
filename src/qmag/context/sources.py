"""Concrete context sources. Each ``fetch_*`` function fills part of a
``ContextReport`` and never raises: failures are recorded in
``report.errors`` and the source is marked unavailable.

Sources (all optional at runtime):

* finviz (via ``finvizfinance``, https://github.com/lit26/finvizfinance):
  headlines, earnings date, sector / industry, float, short interest,
  insider and institutional transactions, analyst consensus. Free, no key.
* Yahoo Finance (``yfinance``): headline fallback, next earnings date.
* StockTwits public symbol stream: message count, Bullish/Bearish labels.
* Reddit (OAuth app: ``REDDIT_CLIENT_ID`` / ``REDDIT_CLIENT_SECRET``):
  recent posts mentioning the ticker across the trading subs.
* Unusual Whales (``UNUSUAL_WHALES_API_KEY``, paid, ``options_flow.enabled``):
  options volume/premium summary, unusual flow alerts (sweeps, repeated
  hits, opening OTM trades) and the ticker's option-volume percentile.
"""

from __future__ import annotations

import logging
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from .base import ContextReport, Headline
from .scoring import score_headline, score_social

log = logging.getLogger(__name__)
UA = {"User-Agent": "qmag/0.1 (+https://github.com/; research tool)"}
TIMEOUT = 10


def _num(text: str | None) -> float | None:
    """'18.05M' -> 18_050_000, '17.63%' -> 17.63, '-' -> None."""
    if text is None:
        return None
    s = str(text).strip().replace(",", "")
    if s in ("-", "", "N/A"):
        return None
    mult = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}
    try:
        if s[-1] in mult:
            return float(s[:-1]) * mult[s[-1]]
        return float(s.rstrip("%"))
    except ValueError:
        return None


def _recency_weight(when: datetime, now: datetime, half_life_days: float = 2.0) -> float:
    age = max((now - when).total_seconds() / 86400, 0.0)
    return 0.5 ** (age / half_life_days)


def _parse_finviz_earnings(text: str | None, now: datetime) -> str | None:
    """finviz gives 'Aug 04 AMC' / 'Nov 03 BMO' with no year; pick the year that makes sense."""
    if not text or text.strip() in ("-", ""):
        return None
    m = re.match(r"([A-Z][a-z]{2}) (\d{1,2})", text.strip())
    if not m:
        return None
    for year in (now.year, now.year + 1, now.year - 1):
        try:
            d = datetime.strptime(f"{m.group(1)} {m.group(2)} {year}", "%b %d %Y").date()
        except ValueError:
            continue
        # finviz shows the next print when it is within ~6 months, else the last one.
        if -200 <= (d - now.date()).days <= 200:
            return d.isoformat()
    return None


# --------------------------------------------------------------------------- #
# finviz: news + fundamentals + events
# --------------------------------------------------------------------------- #
def fetch_finviz(report: ContextReport, lookback_days: int, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    try:
        from finvizfinance.quote import finvizfinance as Quote
    except ImportError:
        for name in ("fundamentals", "news_finviz"):
            report.errors[name] = "finvizfinance not installed"
            report.available[name] = False
        return
    try:
        q = Quote(report.symbol)
        fund = q.ticker_fundament() or {}
        report.sector = fund.get("Sector") or None
        report.industry = fund.get("Industry") or None
        report.market_cap = _num(fund.get("Market Cap"))
        report.float_shares = _num(fund.get("Shs Float"))
        report.short_float_pct = _num(fund.get("Short Float"))
        report.insider_trans_pct = _num(fund.get("Insider Trans"))
        report.inst_own_pct = _num(fund.get("Inst Own"))
        report.analyst_recom = _num(fund.get("Recom"))
        report.target_price = _num(fund.get("Target Price"))
        earn = _parse_finviz_earnings(fund.get("Earnings"), now)
        if earn:
            d = datetime.fromisoformat(earn).date()
            delta = (d - now.date()).days
            if delta >= 0:
                report.earnings_date, report.days_to_earnings = earn, delta
            else:
                report.earnings_recent_days = -delta
        report.available["fundamentals"] = True
    except Exception as exc:
        report.errors["fundamentals"] = f"{type(exc).__name__}: {exc}"
        report.available["fundamentals"] = False

    try:
        news = q.ticker_news()
        cutoff = now - timedelta(days=lookback_days)
        for _, row in news.iterrows():
            when = pd.Timestamp(row["Date"])
            when = when.tz_localize("America/New_York").tz_convert("UTC") if when.tzinfo is None else when.tz_convert("UTC")
            if when.to_pydatetime() < cutoff:
                continue
            title = " ".join(str(row["Title"]).split())
            score, tags = score_headline(title)
            link = str(row.get("Link", "") or "")
            if link.startswith("/"):  # finviz-hosted stories come back as site-relative paths
                link = "https://finviz.com" + link
            report.headlines.append(Headline(when=when.isoformat(), title=title, source=str(row.get("Source", "finviz")), url=link, score=score, tags=tags))
        report.available["news_finviz"] = True
    except Exception as exc:
        report.errors["news_finviz"] = f"{type(exc).__name__}: {exc}"
        report.available["news_finviz"] = False


# --------------------------------------------------------------------------- #
# Yahoo Finance: news fallback + earnings date
# --------------------------------------------------------------------------- #
def fetch_yahoo(report: ContextReport, lookback_days: int, now: datetime | None = None, need_news: bool = True) -> None:
    now = now or datetime.now(timezone.utc)
    try:
        import yfinance as yf

        t = yf.Ticker(report.symbol)
        if need_news:
            cutoff = now - timedelta(days=lookback_days)
            seen = {h.title.lower() for h in report.headlines}
            for item in t.news or []:
                content = item.get("content", item)
                title = " ".join(str(content.get("title", "")).split())
                pub = content.get("pubDate") or content.get("providerPublishTime")
                if isinstance(pub, (int, float)):
                    when = datetime.fromtimestamp(pub, tz=timezone.utc)
                else:
                    when = pd.Timestamp(pub).tz_convert("UTC").to_pydatetime() if pub else now
                if not title or title.lower() in seen or when < cutoff:
                    continue
                score, tags = score_headline(title)
                provider = (content.get("provider") or {}).get("displayName", "yahoo") if isinstance(content.get("provider"), dict) else "yahoo"
                url = ((content.get("canonicalUrl") or {}).get("url") if isinstance(content.get("canonicalUrl"), dict) else content.get("link")) or ""
                report.headlines.append(Headline(when=when.isoformat(), title=title, source=provider, url=url, score=score, tags=tags))
            report.available["news_yahoo"] = True
        if report.earnings_date is None:
            cal = t.calendar or {}
            dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
            if dates:
                d = pd.Timestamp(dates[0]).date()
                delta = (d - now.date()).days
                if delta >= 0:
                    report.earnings_date, report.days_to_earnings = d.isoformat(), delta
        report.available["yahoo"] = True
    except Exception as exc:
        report.errors["yahoo"] = f"{type(exc).__name__}: {exc}"
        report.available["yahoo"] = False
        if need_news and "news_yahoo" not in report.available:
            report.errors["news_yahoo"] = report.errors["yahoo"]
            report.available["news_yahoo"] = False


def finalize_news(report: ContextReport, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    report.headlines.sort(key=lambda h: h.when, reverse=True)
    report.news_count = len(report.headlines)
    tags: list[str] = []
    for h in report.headlines:
        for t in h.tags:
            if t not in tags:
                tags.append(t)
    report.catalysts = tags
    if not report.headlines:
        report.news_score = None
        return
    num = den = 0.0
    for h in report.headlines:
        try:
            when = datetime.fromisoformat(h.when)
            when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
        except ValueError:
            when = now
        w = _recency_weight(when, now)
        num += w * h.score
        den += w
    report.news_score = round(num / den, 3) if den else None


# --------------------------------------------------------------------------- #
# StockTwits
# --------------------------------------------------------------------------- #
def fetch_stocktwits(report: ContextReport) -> tuple[list[float], list[str]]:
    url = f"https://api.stocktwits.com/api/2/streams/symbol/{report.symbol}.json"
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT)
        if r.status_code != 200:
            report.errors["stocktwits"] = f"HTTP {r.status_code}"
            report.available["stocktwits"] = False
            return [], []
        scores, samples = [], []
        for m in r.json().get("messages", []):
            body = m.get("body", "")
            label = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
            s = score_social(body, label)
            scores.append(s)
            if label:
                if label.lower().startswith("bull"):
                    report.social_bullish += 1
                elif label.lower().startswith("bear"):
                    report.social_bearish += 1
            if len(samples) < 3 and len(body) > 20:
                samples.append(f"[stocktwits{' ' + label if label else ''}] {body[:140]}")
        report.available["stocktwits"] = True
        report.social_sources.append("stocktwits")
        return scores, samples
    except Exception as exc:
        report.errors["stocktwits"] = f"{type(exc).__name__}: {exc}"
        report.available["stocktwits"] = False
        return [], []


# --------------------------------------------------------------------------- #
# Reddit (OAuth "script" app)
# --------------------------------------------------------------------------- #
_reddit_token: dict[str, object] = {}
REDDIT_SUBS = "wallstreetbets+stocks+StockMarket+pennystocks+Shortsqueeze+options+swingtrading+Daytrading"


def _reddit_auth() -> str | None:
    cid, secret = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
    if not cid or not secret:
        return None
    if _reddit_token.get("exp", 0) > time.time() + 60:  # type: ignore[operator]
        return str(_reddit_token["token"])
    r = requests.post(
        "https://www.reddit.com/api/v1/access_token",
        auth=(cid, secret),
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": os.environ.get("REDDIT_USER_AGENT", "qmag/0.1 by qmag")},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    tok = r.json()
    _reddit_token.update(token=tok["access_token"], exp=time.time() + int(tok.get("expires_in", 3600)))
    return str(tok["access_token"])


def fetch_reddit(report: ContextReport, lookback_days: int) -> tuple[list[float], list[str]]:
    try:
        token = _reddit_auth()
    except Exception as exc:
        report.errors["reddit"] = f"auth: {type(exc).__name__}: {exc}"
        report.available["reddit"] = False
        return [], []
    if token is None:
        report.errors["reddit"] = "no REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET"
        report.available["reddit"] = False
        return [], []
    try:
        r = requests.get(
            f"https://oauth.reddit.com/r/{REDDIT_SUBS}/search",
            params={"q": f'"{report.symbol}" OR "${report.symbol}"', "sort": "new", "limit": 50, "t": "week", "restrict_sr": "on"},
            headers={"Authorization": f"bearer {token}", "User-Agent": os.environ.get("REDDIT_USER_AGENT", "qmag/0.1 by qmag")},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        cutoff = time.time() - lookback_days * 86400
        pat = re.compile(rf"(?<![A-Z]){re.escape(report.symbol)}(?![A-Z])")
        scores, samples = [], []
        for child in r.json().get("data", {}).get("children", []):
            d = child.get("data", {})
            if d.get("created_utc", 0) < cutoff:
                continue
            text = f"{d.get('title', '')} {d.get('selftext', '')[:400]}"
            if not pat.search(text):
                continue
            weight = 1 + math.log1p(max(d.get("score", 0), 0))
            s = score_social(text)
            scores.extend([s] * int(min(weight, 4)))
            if len(samples) < 2:
                samples.append(f"[r/{d.get('subreddit', '?')} +{d.get('score', 0)}] {d.get('title', '')[:140]}")
        report.available["reddit"] = True
        report.social_sources.append("reddit")
        return scores, samples
    except Exception as exc:
        report.errors["reddit"] = f"{type(exc).__name__}: {exc}"
        report.available["reddit"] = False
        return [], []


def finalize_social(report: ContextReport, scores: list[float], samples: list[str], min_messages: int) -> None:
    report.social_messages = len(scores)
    report.social_samples = samples[:4]
    if len(scores) < min_messages:
        report.social_score = None  # too quiet to mean anything
        return
    report.social_score = round(sum(scores) / len(scores), 3)


# --------------------------------------------------------------------------- #
# Unusual Whales options flow (paid; only called when options_flow.enabled)
# --------------------------------------------------------------------------- #
UW_BASE = "https://api.unusualwhales.com/api"


def _uw_get(path: str, headers: dict, params: dict | None, errors: dict, name: str) -> list[dict] | dict | None:
    """One GET against the Unusual Whales API; failures are recorded per endpoint, never raised."""
    from ..uw import UWClient

    # Options flow must use the same concurrency lease, retries and daily
    # accounting as the edge/price clients, including explicit caller keys.
    key = headers.get("Authorization", "").removeprefix("Bearer ").strip()
    response = UWClient(key=key, cache_path=None, timeout=TIMEOUT).get(path, params)
    if not response.ok:
        errors[name] = response.error
        return None
    return response.data


def fetch_unusual_whales(report: ContextReport, settings=None, api_key: str | None = None, now: datetime | None = None) -> None:
    """Scan a ticker's options tape for unusual activity.

    Three reads: the day's volume/premium summary (``/stock/{t}/options-volume``),
    flow alerts for the ticker (``/option-trades/flow-alerts`` with the
    ``unusual`` preset and our premium / age / DTE filters) and how unusual
    today's option volume is against the ticker's own history
    (``/stock/{t}/unusualness``). The source counts as available when the
    summary or the alerts answered.
    """
    from ..config import OptionsFlowSettings

    f = settings or OptionsFlowSettings()
    key = api_key or os.environ.get("UNUSUAL_WHALES_API_KEY")
    if not key:
        report.errors["unusual_whales"] = "no UNUSUAL_WHALES_API_KEY"
        report.available["unusual_whales"] = False
        return
    now = now or datetime.now(timezone.utc)
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json", **UA}
    errors: dict[str, str] = {}
    sym = report.symbol

    vol = _uw_get(f"/stock/{sym}/options-volume", headers, {"limit": 1}, errors, "options_volume")
    vol_row = vol[0] if isinstance(vol, list) and vol else vol if isinstance(vol, dict) and vol else None

    params: dict = {
        "ticker_symbol": sym,
        "limit": 200,
        "min_premium": int(f.min_premium),
        "max_dte": int(f.max_dte),
        "newer_than": (now - timedelta(days=f.lookback_days)).strftime("%Y-%m-%d"),
    }
    if f.unusual_preset:
        params["unusual"] = "true"
    alerts = _uw_get("/option-trades/flow-alerts", headers, params, errors, "flow_alerts")
    if alerts is None:  # older plans: the per-ticker endpoint
        alerts = _uw_get(f"/stock/{sym}/flow-alerts", headers, {"limit": 200}, errors, "flow_alerts_legacy")
    alert_rows = [a for a in (alerts or []) if isinstance(a, dict)]

    unusual = _uw_get(f"/stock/{sym}/unusualness", headers, None, errors, "unusualness")
    pct_row = unusual[0] if isinstance(unusual, list) and unusual else unusual if isinstance(unusual, dict) else None
    pctile = None
    if pct_row and pct_row.get("opt_vol_pctile") is not None:
        try:
            pctile = float(pct_row["opt_vol_pctile"])
        except (TypeError, ValueError):
            pctile = None

    ok = vol_row is not None or bool(alert_rows) or (alerts is not None)
    report.available["unusual_whales"] = ok
    if errors:
        report.errors["unusual_whales"] = "; ".join(f"{k}: {v}" for k, v in errors.items())
    if not ok:
        return
    report.flow_score = flow_score_from(vol_row, alert_rows, report, f, now=now, opt_vol_pctile=pctile)


def _alert_direction(a: dict) -> tuple[str, float, str]:
    """(bull|bear|neutral, weighted premium, side) for one flow alert.

    Calls bought at the ask and puts sold at the bid are bullish; puts bought
    at the ask and calls sold at the bid are bearish. When the side split is
    not given, a call is treated as bullish and a put as bearish.
    """
    typ = str(a.get("type", "")).lower()
    ask = float(a.get("total_ask_side_prem") or 0)
    bid = float(a.get("total_bid_side_prem") or 0)
    total = float(a.get("total_premium") or (ask + bid) or 0)
    if ask == 0 and bid == 0:
        return ("bull" if typ == "call" else "bear" if typ == "put" else "neutral"), total, "unknown"
    side = "ask" if ask >= bid else "bid"
    aggressive = max(ask, bid)
    if typ == "call":
        return ("bull" if side == "ask" else "bear"), aggressive, side
    if typ == "put":
        return ("bear" if side == "ask" else "bull"), aggressive, side
    return "neutral", aggressive, side


def _alert_age_days(a: dict, now: datetime) -> float | None:
    raw = a.get("created_at") or a.get("executed_at")
    if not raw:
        return None
    try:
        ts = pd.Timestamp(raw)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return max((now - ts.to_pydatetime()).total_seconds() / 86400, 0.0)
    except Exception:
        return None


def _dte(a: dict, now: datetime) -> int | None:
    exp = a.get("expiry")
    if not exp:
        return None
    try:
        return max((pd.Timestamp(exp).date() - now.date()).days, 0)
    except Exception:
        return None


def summarise_alert(a: dict, now: datetime) -> dict:
    """Compact, display-ready record of one unusual trade."""
    direction, prem, side = _alert_direction(a)
    strike = a.get("strike")
    under = a.get("underlying_price")
    otm = None
    try:
        if strike is not None and under:
            diff = (float(strike) - float(under)) / float(under)
            otm = round((diff if str(a.get("type", "")).lower() == "call" else -diff) * 100, 1)
    except (TypeError, ValueError, ZeroDivisionError):
        otm = None
    return {
        "when": str(a.get("created_at") or "")[:16].replace("T", " "),
        "type": str(a.get("type", "")).lower(),
        "strike": float(strike) if strike not in (None, "") else None,
        "expiry": str(a.get("expiry") or "")[:10],
        "dte": _dte(a, now),
        "premium": round(float(a.get("total_premium") or prem or 0)),
        "side": side,
        "direction": direction,
        "sweep": bool(a.get("has_sweep")),
        "floor": bool(a.get("has_floor")),
        "size": a.get("total_size"),
        "volume": a.get("volume"),
        "open_interest": a.get("open_interest"),
        "vol_oi": round(float(a["volume_oi_ratio"]), 2) if a.get("volume_oi_ratio") not in (None, "") else None,
        "otm_pct": otm,
        "rule": a.get("alert_rule"),
    }


def flow_score_from(
    volume_row: dict | None,
    alert_rows: list[dict],
    report: ContextReport | None = None,
    settings=None,
    now: datetime | None = None,
    opt_vol_pctile: float | None = None,
) -> float | None:
    """Combine the options-volume summary, the unusual flow alerts and the
    volume percentile into one -1..+1 tilt and fill the report's flow fields.

    * premium tilt: (bullish - bearish premium) / total, from the summary (w 1.0)
    * alert tilt: aggressive (ask-side) call premium vs put premium across the
      unusual trades, sweeps weighted ``sweep_weight`` (w 1.0)
    * activity: today's call volume vs its 30-day average against the same
      for puts nudges the score toward the busier side (w 0.5)
    * unusualness: a high option-volume percentile amplifies whichever way
      the tilt already points (w 0.5)
    """
    from ..config import OptionsFlowSettings

    f = settings or OptionsFlowSettings()
    now = now or datetime.now(timezone.utc)
    parts: list[tuple[float, float]] = []
    call_prem = put_prem = bull_prem = bear_prem = call_ratio = put_ratio = None
    if volume_row:
        bull_prem = float(volume_row.get("bullish_premium") or 0)
        bear_prem = float(volume_row.get("bearish_premium") or 0)
        call_prem = float(volume_row.get("call_premium") or 0)
        put_prem = float(volume_row.get("put_premium") or 0)
        if bull_prem + bear_prem > 0:
            parts.append(((bull_prem - bear_prem) / (bull_prem + bear_prem), 1.0))
        cv, avg = float(volume_row.get("call_volume") or 0), float(volume_row.get("avg_30_day_call_volume") or 0)
        pv, pavg = float(volume_row.get("put_volume") or 0), float(volume_row.get("avg_30_day_put_volume") or 0)
        if avg > 0:
            call_ratio = cv / avg
        if pavg > 0:
            put_ratio = pv / pavg
        if call_ratio is not None and put_ratio is not None:
            parts.append((math.tanh((call_ratio - put_ratio) / 2), 0.5))

    kept: list[dict] = []
    bull = bear = 0.0
    n_bull = n_bear = sweeps = 0
    for a in alert_rows:
        prem_total = float(a.get("total_premium") or a.get("total_ask_side_prem") or 0)
        if prem_total < f.min_premium:
            continue
        age = _alert_age_days(a, now)
        if age is not None and age > f.lookback_days:
            continue
        dte = _dte(a, now)
        if dte is not None and dte > f.max_dte:
            continue
        direction, prem, _side = _alert_direction(a)
        mult = f.sweep_weight if a.get("has_sweep") else 1.0
        sweeps += 1 if a.get("has_sweep") else 0
        if direction == "bull":
            bull += prem * mult
            n_bull += 1
        elif direction == "bear":
            bear += prem * mult
            n_bear += 1
        kept.append(a)
    if bull + bear > 0:
        parts.append(((bull - bear) / (bull + bear), 1.0))

    tilt = sum(s * w for s, w in parts) / sum(w for _, w in parts) if parts else None
    if opt_vol_pctile is not None and tilt is not None and tilt != 0:
        # volume in the 50th percentile says nothing; the 99th says "somebody knows something"
        parts.append((math.copysign((opt_vol_pctile - 50) / 50, tilt), 0.5))

    if report is not None:
        report.flow_call_premium, report.flow_put_premium = call_prem, put_prem
        report.flow_bull_premium, report.flow_bear_premium = bull_prem, bear_prem
        report.flow_call_vol_ratio = round(call_ratio, 2) if call_ratio is not None else None
        report.flow_put_vol_ratio = round(put_ratio, 2) if put_ratio is not None else None
        report.flow_opt_vol_pctile = opt_vol_pctile
        report.flow_alerts, report.flow_bull_alerts, report.flow_bear_alerts, report.flow_sweeps = len(kept), n_bull, n_bear, sweeps
        kept.sort(key=lambda a: float(a.get("total_premium") or 0), reverse=True)
        report.flow_trades = [summarise_alert(a, now) for a in kept[: f.top_trades]]
        report.flow_unusual = bool(
            len(kept) >= max(f.min_alerts, 1)
            or (call_ratio is not None and call_ratio >= f.volume_ratio_unusual)
            or (opt_vol_pctile is not None and opt_vol_pctile >= f.percentile_unusual)
        )
        bits = []
        if call_prem is not None and put_prem is not None:
            bits.append(f"call ${call_prem / 1e6:.1f}M vs put ${put_prem / 1e6:.1f}M premium")
        if call_ratio is not None:
            bits.append(f"call volume {call_ratio:.1f}x its 30-day average")
        if opt_vol_pctile is not None:
            bits.append(f"option volume in the {opt_vol_pctile:.0f}th percentile of its own history")
        bits.append(f"{len(kept)} unusual trades >= ${f.min_premium / 1e3:.0f}k ({n_bull} bullish / {n_bear} bearish, {sweeps} sweeps)")
        report.flow_note = ", ".join(bits)
    if not parts:
        return None
    return round(max(-1.0, min(1.0, sum(s * w for s, w in parts) / sum(w for _, w in parts))), 3)
