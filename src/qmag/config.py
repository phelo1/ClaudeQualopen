"""Strategy, risk and engine parameters.

Everything the scanner, backtester, optimiser and paper trader depend on lives
here so a single YAML file (or a single optimiser grid) can drive all of them.
Defaults follow Kristjan Kullamägi's publicly described rules; they are a
starting point for research, not a claim about what "works".
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

# (old_section, old_key, new_section, new_key) - renames that older YAML files may still use.
_MOVED_KEYS = (
    ("context", "flow_enabled", "options_flow", "enabled"),
    ("context", "flow_min_score", "options_flow", "min_score"),
    ("context", "llm_enabled", "committee", "enabled"),
    ("context", "llm_can_veto", "committee", "can_veto"),
)
# The old single-model committee was OpenAI-compatible only: its model / endpoint become every seat's, pinned to openai.
_LEGACY_COMMITTEE_KEYS = {"llm_model": "model", "llm_base_url": "base_url"}


@dataclass(frozen=True)
class MomentumFilter:
    """How we approximate "the strongest stocks in the market"."""

    min_gain_1m: float = 0.30  # +30 % over ~21 trading days, or ...
    min_gain_3m: float = 0.50  # +50 % over ~63 days, or ...
    min_gain_6m: float = 1.00  # +100 % over ~126 days
    min_adr_pct: float = 3.5  # average daily range in percent; he prefers > 5
    min_price: float = 3.0
    min_dollar_volume: float = 5_000_000.0  # 20-day average close * volume


@dataclass(frozen=True)
class BreakoutSetup:
    """Momentum breakout out of a tight flag / consolidation."""

    enabled: bool = True
    min_flag_days: int = 10
    max_flag_days: int = 60
    # Flag depth as a fraction of the prior impulse move. He wants
    # "orderly" pullbacks; deep, disorderly bases are skipped.
    max_flag_depth: float = 0.5
    # Range contraction: ADR% over the last 5 days versus over the flag.
    max_recent_adr_ratio: float = 0.85
    # Price must ride above the rising short MAs (surfing the 10/20).
    require_above_ma: bool = True
    fast_ma: int = 10
    slow_ma: int = 20
    min_breakout_volume_ratio: float = 1.2  # vs 20-day average volume
    max_gap_pct: float = 0.05  # skip if it gaps > 5 % over the pivot
    # Entry buffer above the pivot (approximates an opening-range-high buy).
    entry_buffer_pct: float = 0.002


@dataclass(frozen=True)
class EpisodicPivotSetup:
    """Gap up on huge volume from a neglected base (earnings-style EP)."""

    enabled: bool = True
    min_gap_pct: float = 0.10
    min_volume_ratio: float = 3.0  # vs 50-day average volume
    # The stock should NOT already be extended: cap the prior 3-month gain.
    max_prior_gain_3m: float = 0.30
    # Must hold the gap: close in the upper part of the day's range.
    min_close_position: float = 0.6
    entry_buffer_pct: float = 0.002


@dataclass(frozen=True)
class TradeManagement:
    """Kullamägi's exit playbook."""

    # Initial stop = entry - stop_adr_mult * ADR (approximates low-of-day stop)
    stop_adr_mult: float = 1.0
    # Reject setups whose 1-ADR stop is absurdly wide (thin, erratic names):
    # a 20 % stop means a tiny position and a coin-flip R.
    max_stop_pct: float = 0.15
    # Sell part into strength: at ``partial_target_r`` R-multiples (a resting
    # limit order, so a spike gets sold), or at the close of day
    # ``partial_after_days`` if the trade is green - whichever comes first ...
    partial_after_days: int = 3
    partial_fraction: float = 0.33
    partial_target_r: float | None = 2.0  # None = time rule only
    # ... then move the stop to breakeven and trail the rest on a moving average.
    move_stop_to_breakeven: bool = True
    trail_ma: int = 10  # 10-day for fast movers, 20-day for slower ones
    max_hold_days: int = 60
    # Time stop: a breakout that has done nothing after this many completed
    # sessions (closing at or under the entry, never having reached
    # ``time_stop_min_mfe_r`` R of open profit, no partial taken) is cut at
    # the close instead of sitting until the stop is hit - dead money is
    # risk and slot cost. 0 = off.
    time_stop_days: int = 5
    time_stop_min_mfe_r: float = 1.0


@dataclass(frozen=True)
class RiskSettings:
    starting_equity: float = 100_000.0
    risk_per_trade_pct: float = 0.005  # 0.5 % of equity at the stop
    max_position_pct: float = 0.25  # never more than 25 % of equity in a name
    max_positions: int = 8
    max_gross_exposure: float = 1.0  # 1.0 = no margin
    # Portfolio-level gates, judged before every new entry:
    # * heat: the sum of what every open position and resting entry would
    #   lose at its stop (from the latest price), plus this plan's risk, may
    #   not exceed this fraction of equity. 0 = off.
    max_portfolio_heat_pct: float = 0.04
    # * daily loss circuit-breaker: once equity is down this much from the
    #   first pass of the session, no new entries until the next session
    #   (exits keep being managed). 0 = off.
    daily_loss_limit_pct: float = 0.03
    # * concentration: at most this many open / pending positions in one
    #   theme (a hot group tends to reverse together). 0 = off.
    max_positions_per_theme: int = 3
    commission_per_share: float = 0.0
    slippage_bps: float = 5.0  # applied to entries and exits


@dataclass(frozen=True)
class RegimeFilter:
    """Market sentiment: only buy breakouts when the market itself is paying.

    Three independent gates, each optional; all active gates must pass.
    * benchmark trend: QQQ above its 20-day MA
    * breadth: share of the scan universe above its 20-day MA (a direct read
      of whether momentum names are working, computed from the data you
      already have)
    * volatility: VIX below a ceiling (needs ``^VIX`` in the data)
    """

    enabled: bool = True
    benchmark: str = "QQQ"
    ma_length: int = 20
    breadth_enabled: bool = True
    breadth_ma_length: int = 20
    min_breadth: float = 0.40  # >= 40 % of the universe above its 20-day MA
    vix_symbol: str = "^VIX"
    max_vix: float | None = None  # e.g. 30.0 to stand aside in panics; None = off


@dataclass(frozen=True)
class ThemeFilter:
    """Segment / niche momentum: favour leaders inside the strongest themes.

    Themes are ticker groups (see ``universe/themes.yaml``). For each theme an
    index is built from the **median** member's daily return - so one stock
    that triples cannot make its whole industry look hot - and ranked against
    the other themes on a 1m/3m momentum composite. A stock inherits the best
    percentile of the themes it belongs to. Themes with fewer than
    ``min_theme_members`` scanned members do not rank at all.
    """

    enabled: bool = True
    min_theme_members: int = 3  # smaller groups are not a "theme": their stocks count as themeless
    themes_file: str | None = None  # default: universe/themes.yaml
    groups: dict[str, list[str]] | None = None  # inline theme -> tickers; overrides themes_file
    # Add one theme per finviz industry from the cached fundamentals
    # (``qmag universe build --fundamentals``), so the whole market is
    # covered without a hand-kept YAML. Hand-kept themes still apply on top.
    use_industries: bool = True
    min_industry_members: int = 4
    # Hard gate. Applied to breakouts only by default: episodic pivots come
    # from *neglected* names and groups, so gating them on theme strength
    # throws away exactly the trades the setup exists for.
    apply_to: list[str] = field(default_factory=lambda: ["breakout"])
    min_theme_percentile: float = 0.3  # skip breakouts whose best theme ranks in the bottom 30 %
    min_theme_breadth: float = 0.4  # >= 40 % of the theme's members above their 20-day MA
    require_theme: bool = False  # True: ignore stocks that belong to no theme
    # Soft ranking: theme percentile added to every setup's score, so with
    # two equally good signals the one in the hotter group gets the slot.
    score_weight: float = 1.0


@dataclass(frozen=True)
class SentimentFilter:
    """Optional external (social / news) sentiment per symbol per day.

    Sources are pluggable; the bundled one reads ``date,symbol,score`` CSVs
    so any vendor or scraper can feed it. Score convention: -1 .. +1, with
    ``buzz`` (attention) optional in a second column. Off by default because
    it needs data you have to bring.
    """

    enabled: bool = False
    source: str = "csv"
    path: str | None = None
    min_score: float | None = None  # skip if sentiment below this
    max_score: float | None = None  # skip if sentiment above this (crowded-trade guard)
    score_weight: float = 0.5  # contribution of sentiment to the ranking score
    max_staleness_days: int = 3


@dataclass(frozen=True)
class ContextSettings:
    """Live, per-candidate context fetched only for the handful of names that
    already pass the price-based setup - news, social chatter, options flow,
    earnings dates and float / short-interest facts.

    All of it is *soft* by default: it is scored, recorded in the plan's
    justification and used to rank, and only the explicit gates below can
    veto a trade. Everything degrades gracefully: a source that is down or
    has no key is skipped and marked "unavailable" in the plan.
    """

    enabled: bool = True
    # -- news headlines (finviz first, Yahoo Finance fallback; both free)
    news_enabled: bool = True
    news_lookback_days: int = 5
    news_min_score: float | None = -0.5  # veto breakouts on clearly negative news flow (None = never)
    # -- social chatter (StockTwits public stream; Reddit needs REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET)
    social_enabled: bool = True
    social_sources: list[str] = field(default_factory=lambda: ["stocktwits", "reddit"])
    social_min_score: float | None = None
    social_max_score: float | None = 0.85  # crowded-trade guard: near-unanimous bullish chatter is a warning for EPs
    social_min_messages: int = 5  # below this the read is "quiet", not a signal
    # -- options flow lives in its own section (``options_flow``): it is a paid feed.
    # -- events / fundamentals (finviz + Yahoo)
    events_enabled: bool = True
    avoid_earnings_within_days: int = 3  # breakouts only: don't open a fresh breakout right into a print
    small_float_shares: float = 30e6  # float below this is flagged as "low float" (bigger moves both ways)
    high_short_float_pct: float = 15.0  # short interest above this is flagged as squeeze fuel
    # -- scoring
    score_weight: float = 0.5  # composite context score (-1..1) x weight added to the ranking score
    cache_minutes: int = 30
    max_symbols_per_cycle: int = 40  # cap outbound requests: top ideas by price score get context first


@dataclass(frozen=True)
class OptionsFlowSettings:
    """Unusual options activity per candidate from the Unusual Whales API
    (``UNUSUAL_WHALES_API_KEY``). Paid data; on by default now that the desk
    runs on an Unusual Whales key, and switchable at any time
    (``options_flow.enabled`` or ``--options-flow/--no-options-flow``); when
    off nothing is called and the plan says so. Without a key the source is
    reported as NOT CONFIGURED and the reading stays empty.

    Three endpoints are read for each candidate that reached the context
    stage: the day's options volume/premium summary (bullish vs bearish
    premium, call/put volume vs 30-day average), the flow *alerts* filtered
    to Unusual Whales' own "unusual" preset (volume > OI, opening, OTM,
    single-leg, ask-side, ≥$10k) and the ticker's option-volume percentile
    against its own history. They become a -1..+1 tilt, a list of the
    largest unusual trades and an ``unusual`` flag that feed the score, the
    checklist, the rationale, the dashboard and the LLM reviewer bundle.
    """

    enabled: bool = True  # needs UNUSUAL_WHALES_API_KEY; without it the source shows NOT CONFIGURED and nothing is invented
    provider: str = "unusual_whales"
    # -- what counts as an unusual trade
    lookback_days: int = 3  # alerts newer than this many days
    min_premium: float = 50_000  # ignore alerts below this total premium
    unusual_preset: bool = True  # ask for UW's "unusual" preset (vol>OI, opening, OTM, DTE<=60, ask-side>=50%)
    max_dte: int = 90  # ignore contracts expiring further out than this
    sweep_weight: float = 1.5  # intermarket sweeps are urgent; weigh them more
    top_trades: int = 10  # how many of the largest trades to keep for display / the reviewer
    # -- how it feeds the edge
    weight: float = 1.0  # weight of the flow tilt inside the composite context score
    volume_ratio_unusual: float = 2.0  # call volume >= this x 30-day average counts as unusual
    percentile_unusual: float = 90.0  # option-volume percentile vs own history that counts as unusual
    # -- gates (None / False = record only)
    min_score: float | None = None  # e.g. -0.3 vetoes names with heavy put buying; missing data passes
    require_bullish: bool = False  # only trade when bullish unusual flow is present; missing data FAILS
    bullish_threshold: float = 0.2
    min_alerts: int = 1  # unusual trades needed for ``require_bullish``
    # -- cost control: paid calls per cycle, top-ranked candidates first
    max_symbols_per_cycle: int = 25


# Default weight of every Unusual Whales feature inside the edge score. 0 = not
# fetched, not scored. See ``qmag.context.edge.FEATURES`` for what each one reads
# and how it is turned into a -1..+1 sub-score.
DEFAULT_EDGE_WEIGHTS: dict[str, float] = {
    # options positioning & flow
    "flow": 1.5,  # unusual flow scan: bullish vs bearish premium, sweeps, option-volume percentile
    "net_premium": 1.0,  # today's net call vs net put premium (ask-side buying minus bid-side selling)
    "oi_change": 0.75,  # call vs put open interest being built overnight
    "dealer_delta": 0.5,  # net dealer delta exposure (call_delta + put_delta)
    "gamma": 0.5,  # gamma regime + room to the call wall
    "option_sentiment": 0.75,  # UW options positioning sentiment (VWKS + AVAR)
    "options_pulse": 0.5,  # Nasdaq Options Pulse sentiment
    "skew": 0.5,  # 25-delta risk reversal: calls bid over puts
    "max_pain": 0.25,  # spot vs nearest-expiry max pain (pin risk)
    "volatility": 0.25,  # implied vs realised volatility and term structure
    # stock tape
    "dark_pool": 0.75,  # off-exchange prints: at-ask vs at-bid notional
    "relative_volume": 0.5,  # stock volume percentile vs its own 90 days
    "short_interest": 0.5,  # short % of float, days to cover (squeeze fuel)
    # ownership & filings
    "insiders": 0.5,  # insider buys vs sells (Form 4)
    "institutions": 0.5,  # 13F holders adding vs trimming
    "congress": 0.25,  # congressional buys vs sells
    "analysts": 0.5,  # upgrades / initiations vs downgrades
    # calendar & catalysts
    "seasonality": 0.25,  # this month's historical hit rate for the ticker
    "earnings": 0.5,  # last report: beat / miss and the post-earnings drift
    "news": 0.5,  # UW headline sentiment
    # market & sector backdrop
    "market_tide": 0.75,  # market-wide net call vs put premium today
    "sector_tide": 0.5,  # the same for the ticker's sector
}


@dataclass(frozen=True)
class EdgeScoreSettings:
    """Weighted Unusual Whales edge score with an entry threshold.

    Every feature in ``weights`` is read from the Unusual Whales API for each
    candidate that reached the context stage and turned into a -1..+1
    sub-score (bullish positive). The edge score is the weight-averaged
    sub-score over the features that *answered*; ``coverage`` is the share of
    enabled weight that answered. Features that do not apply (option data
    for a ticker without listed options) are excluded from both.

    Gate: with ``gate`` on, a plan fails ``uw_edge`` unless
    ``score >= threshold`` **and** ``coverage >= min_coverage``. Missing data
    never passes the gate - a threshold cannot be met with numbers that were
    not sourced - and every unavailable feature is listed in the plan's data
    gaps. Weights are tunable per feature; 0 switches a feature off.
    """

    enabled: bool = True
    gate: bool = True
    threshold: float = 0.15  # weighted edge score needed to enter (-1..+1)
    min_coverage: float = 0.5  # share of enabled weight that must have answered
    rank_weight: float = 1.0  # weight of the edge score inside the composite context score (ranking)
    max_symbols_per_cycle: int = 20  # paid calls: ~20 requests per candidate
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_EDGE_WEIGHTS))
    # -- lookback windows / thresholds used by individual features
    insider_days: int = 90
    congress_days: int = 90
    analyst_days: int = 45
    dark_pool_min_premium: float = 100_000  # only prints at least this large are read for direction
    high_short_float: float = 0.15  # short % of float that counts as full squeeze fuel
    daily_cache_hours: float = 12.0  # facts that change once a day are fetched once a day


@dataclass(frozen=True)
class ReviewerSettings:
    """Additive LLM trade reviewer (off by default), modelled on
    danilobatson/ai-trading-agent-gemini: the *whole* edge bundle - setup,
    sizing, targets, checklist, news/events, social, options flow,
    fundamentals, the committee debate and the portfolio state - goes to one
    model which must answer in strict JSON: action BUY/SELL/HOLD, confidence,
    thesis, catalysts, risks, invalidation, sizeNote (+ optional
    sizeMultiplier). The automated trader reads that verdict before placing
    an order according to ``mode``.
    """

    enabled: bool = False
    # gemini | openai | auto (Gemini when GEMINI_API_KEY / GOOGLE_API_KEY is set, else OpenAI-compatible)
    provider: str = "auto"
    model: str | None = None  # default: gemini-3.5-flash for Gemini, gpt-4o-mini for OpenAI-compatible; the settings page lists live models
    base_url: str | None = None  # OpenAI-compatible endpoint override (Ollama, vLLM, OpenRouter, Azure...)
    timeout_seconds: int = 60
    # advisory      -> record the verdict, trade on the checklist alone
    # gate          -> only trade when action == BUY and confidence >= min_confidence
    # gate_and_size -> as gate, and scale shares by the model's sizeMultiplier when < 1
    mode: str = "advisory"
    min_confidence: float = 0.6
    # An unreachable model must never silently pass a gate: fail-closed skips the trade.
    fail_closed: bool = False
    review_watchlist: bool = True  # also review buy-stop plans parked for tomorrow (costs one call each)


@dataclass(frozen=True)
class CommitteeSettings:
    """Three-seat analyst committee (off by default): a real debate between
    separate model calls, not one model playing three roles.

    1. The **bull researcher** argues for the trade from the plan, its
       checklist, the deterministic rationale and the gathered context.
    2. The **bear researcher** gets the same facts *and the bull case* and
       argues against it (``debate`` off: both write blind).
    3. The **risk chair** reads the facts and both cases and rules: take,
       reduce (with a size multiplier) or reject, with a confidence.

    Each seat has its own provider / model / endpoint, so the bull can be one
    vendor's model and the bear another's, or a local model can sit in one
    chair. Every seat sees only what the engine saw; nothing is invented.
    The verdict is advisory unless ``can_veto`` is on, in which case a
    reject blocks the trade and a reduce scales the shares. Three calls per
    plan: ``max_plans_per_cycle`` caps the cost.
    """

    enabled: bool = False
    can_veto: bool = False
    debate: bool = True  # the bear reads the bull case before writing; the chair reads both
    timeout_seconds: int = 60
    max_plans_per_cycle: int = 8
    # gemini | openai | auto (Gemini when its key is saved, else OpenAI-compatible); model blank = provider default
    bull_provider: str = "auto"
    bull_model: str | None = None
    bull_base_url: str | None = None
    bear_provider: str = "auto"
    bear_model: str | None = None
    bear_base_url: str | None = None
    risk_provider: str = "auto"
    risk_model: str | None = None
    risk_base_url: str | None = None


@dataclass(frozen=True)
class AdaptiveRisk:
    """Kullamägi sizes up when the market is paying and down when it is not.

    Realised R-multiples of recently closed trades (the trade journal) scale
    the per-trade risk: below ``cold_avg_r`` risk is cut to ``cold_risk_mult``
    of normal, above ``hot_avg_r`` (and with enough trades) it can be raised.
    """

    enabled: bool = True
    lookback_trades: int = 10
    min_trades: int = 6
    cold_avg_r: float = -0.2
    cold_risk_mult: float = 0.5
    hot_avg_r: float = 0.6
    hot_risk_mult: float = 1.25
    max_risk_per_trade_pct: float = 0.01


@dataclass(frozen=True)
class ScheduleSettings:
    """Tiered scan schedule: one heavy scan a night, light focused passes all day.

    * **Nightly arming scan** (after the close): the whole universe on
      completed bars. Flags within ``arming_distance_pct`` of their pivot,
      plus anything that triggered, become the *arming list* (top
      ``arming_max_names`` by score); buy-stop-limit brackets are parked
      for the sized plans.
    * **Pre-market gap scan** (``premarket_times``): one market-wide
      screener request for gappers >= ``premarket_min_gap_pct`` with heavy
      early volume; hits join the arming list as episodic-pivot candidates.
    * **Post-open full scan** (``post_open_time``, optional): one sweep of
      the universe on the day's first bars so an EP that no screener
      surfaced is still caught.
    * **Focused cycle** every ``focused_interval_minutes``: only the arming
      list, open positions, pending entries and the day's screener hits are
      refreshed and checked. Partial-bar volume is judged on *pace*
      (projected full-day volume vs average, see ``qmag.pace``) instead of
      the raw partial count. Live context / paid edge reads go only to names
      within ``trigger_distance_pct`` of their pivot.
    * **Movers sweep** every ``movers_interval_minutes``: one screener call
      for the day's top gainers on relative volume; new names get the full
      check and join the arming list.

    With ``tiered`` off the legacy schedule runs (09:45 post-open, 30-minute
    whole-universe intraday cycles, after-close cycle).
    """

    tiered: bool = True
    # -- nightly arming scan
    arming_distance_pct: float = 0.05  # flags this close to the pivot are armed for the next session
    arming_max_names: int = 60
    # -- pre-market gap scan (needs a screener source: Unusual Whales key, else finviz)
    premarket_enabled: bool = True
    premarket_times: list[str] = field(default_factory=lambda: ["08:30", "09:20"])
    premarket_min_gap_pct: float = 0.08
    premarket_max_names: int = 25
    # -- post-open whole-universe pass (catches EPs without a screener)
    post_open_full_scan: bool = True
    post_open_time: str = "09:40"
    # -- focused intraday cycle
    focused_interval_minutes: int = 5
    focused_start: str = "09:35"
    focused_stop_before_close_minutes: int = 5
    trigger_distance_pct: float = 0.02  # "armed": names this close to the pivot get context / edge refreshed and plans re-sized
    volume_pace: bool = True  # judge partial-bar volume on projected full-day pace
    pace_min_session_fraction: float = 0.08  # before ~9:44 the projection is too noisy: raw volume is used
    # -- movers sweep
    movers_enabled: bool = True
    movers_interval_minutes: int = 30
    movers_start: str = "10:00"
    movers_stop_before_close_minutes: int = 30
    movers_min_change_pct: float = 0.08
    movers_min_rvol: float = 3.0
    movers_max_names: int = 25


@dataclass(frozen=True)
class EntrySettings:
    """How a breakout entry is actually triggered.

    ``mode``:

    * ``confirmed`` (default) - no orders rest at the broker. The intraday
      focused pass buys at market only when the live bar shows the pivot
      taken out **and held** (last price still above it, not a wick), on
      volume pacing to at least ``confirm_volume_ratio`` x the 20-day
      average, outside the first ``opening_range_minutes`` of the session
      and not more than ``breakout.max_gap_pct`` above the pivot. It cannot
      be filled by a one-tick stop-run; it pays a few minutes of latency.
    * ``resting``   - the nightly scan parks stop-limit buy brackets at the
      broker (price-only fills, never misses a fast move, takes more false
      breakouts). Only plans scoring at least ``resting_min_score`` rest.
    * ``hybrid``    - nothing rests overnight; buy-stops are parked by the
      first focused pass at or after ``resting_from`` (after the opening
      auction and the usual stop-run), and expire at the close.

    ``failed_breakout_exit`` sells a fresh position (held ``failed_breakout_days``
    bars or fewer) at market when price is back below the pivot by more than
    ``failed_breakout_tolerance_pct`` - on a completed bar, or intraday when the
    paced volume is also below ``confirm_volume_ratio``. That is the small,
    fast loss the method accepts instead of hoping. Every trigger decision is
    written to the cycle log and to the trade journal (see ``learning``).
    """

    mode: str = "confirmed"
    confirm_volume_ratio: float = 1.0
    require_hold_above_pivot: bool = True
    opening_range_minutes: int = 10
    resting_from: str = "09:40"
    resting_min_score: float = 0.0
    failed_breakout_exit: bool = True
    failed_breakout_days: int = 1
    failed_breakout_tolerance_pct: float = 0.005


@dataclass(frozen=True)
class LearningSettings:
    """Learn from the journal: what worked, what did not, why - and tune.

    Every entry records the features it was taken on (setup, entry mode,
    scan source, paced volume, time of day, ADR, flag depth, gap, theme rank,
    edge score, reviewer verdict, regime ...); every exit records the reason,
    hold time, best / worst excursion in R and a rule-based post-mortem.
    Setups the checklist rejected (or watch plans never taken) are followed
    as **shadow trades** on real bars for ``shadow_max_days`` so filters can
    be judged on what they blocked, not only on what they let through.

    The weekly review (``review_weekday`` / ``review_time``) buckets the
    journal by feature, writes plain-English lessons and - when
    ``auto_apply`` is on - moves a bounded set of selection / trigger knobs
    by one step at a time, only where at least ``min_trades`` trades (or
    shadow trades) show a lift of ``min_lift_r`` R or more against the
    overall expectancy. Adjustments live in ``learning_overrides.yaml`` in
    the state directory, are listed with their evidence on the learning page
    and can be reset at any time. ``llm_enabled`` adds a strict-JSON
    post-mortem per closed trade from the configured model (never required).
    """

    enabled: bool = True
    auto_apply: bool = False
    min_trades: int = 8
    min_lift_r: float = 0.25
    shadow_enabled: bool = True
    shadow_max_days: int = 5
    shadow_hold_days: int = 10
    review_weekday: str = "saturday"
    review_time: str = "11:00"
    llm_enabled: bool = True
    provider: str = "auto"
    model: str | None = None
    base_url: str | None = None
    timeout_seconds: int = 60
    max_post_mortems_per_run: int = 20


@dataclass(frozen=True)
class InsiderScanSettings:
    """Weekly (Saturday) hunt for options trades that fit the profile seen the
    day before takeovers, FDA decisions and guidance shocks - the kind of
    positioning that sometimes precedes news only insiders could know.

    The profile (Zendesk's $70 calls at 24 % out of the money the day before
    its buyout, GoPro's short-dated calls at 50x normal volume, Heinz's June
    $65 calls with almost no prior open interest): out-of-the-money calls or
    puts, a few weeks to expiry, bought aggressively (sweeps, at the ask,
    opening trades), concentrated in one strike / expiry, in a chain that is
    normally quiet, with no scheduled event inside the contracts' life.

    The whole week's Unusual Whales flow alerts (market-wide, ``unusual``
    preset plus the thresholds below) and the daily unusual-contract screen
    are pulled and aggregated per ticker. Each ticker is scored 0-10 on:
    premium (log scale, small weight), volume vs open interest, how far out
    of the money the dominant bet is (10-35 % is the sweet spot), its days to
    expiry (3-45 is the informed window), aggressiveness, one-directional
    conviction, concentration in one strike / expiry, fresh open interest,
    that week's option volume vs the 30-day average (``enrich_top``
    candidates get two extra reads for this and for the market cap), a
    penalty for chains that trade hundreds of thousands of contracts a day
    (mega caps: nothing there is a quiet tell), and whether the contracts
    expire *before* the next scheduled earnings. Tickers outside the market-
    cap window are removed even when the API's own filter let them through.
    Tickers at or above ``min_flag_score`` are flagged for investigation.

    With ``ai_enabled`` each flagged ticker - its trades plus everything the
    desk could source (news, next earnings, insider filings, fundamentals) -
    goes to an LLM that must answer in strict JSON: what catalyst the flow
    could be associated with, what the buyer may be speculating on, whether
    public information already explains it, and what to check. Nothing is
    invented: sources that did not answer are listed as gaps, and a model
    that is unreachable is recorded as such. A flag is a research lead, not
    an accusation.
    """

    enabled: bool = True
    weekday: str = "saturday"
    run_time: str = "10:00"
    lookback_days: int = 7
    # -- what counts as highly unusual (server-side filters where the API supports them)
    min_premium: float = 150_000  # total premium of one alert ($150k buys a lot of far-OTM calls in a small cap)
    min_volume_oi_ratio: float = 3.0  # contract volume vs open interest: new positions, not existing holders
    min_dte: int = 3  # 0-2 DTE is day-trading / lottery flow, not a bet on an event
    max_dte: int = 45  # the informed window is a few weeks; beyond that it is ordinary positioning
    min_otm_pct: float = 5.0  # strike this far out of the money (Heinz's $65s were ~8 %, Zendesk's $70s 24 %)
    min_ask_side_pct: float = 0.7  # share of premium paid at the ask (aggressive buyer)
    include_puts: bool = True  # bearish bets ahead of bad news are just as telling
    min_market_cap: float | None = 100e6
    max_market_cap: float | None = 30e9  # enforced client-side too: mega-cap institutional flow is routine; the edge is in small / mid caps
    exclude_tickers: list[str] = field(default_factory=lambda: ["SPY", "QQQ", "IWM", "DIA", "SPX", "SPXW", "NDX", "VIX", "XSP", "TLT", "GLD", "SLV", "HYG", "EEM", "EFA", "XLF", "XLE", "XLK", "SMH", "ARKK", "TQQQ", "SQQQ", "SOXL"])
    # -- cost control (paid calls)
    max_pages: int = 5  # flow-alert pages of 200 per week
    contract_screen: bool = True  # also read the daily unusual-contract screen for each session (one call per day)
    enrich_top: int = 40  # candidates given two extra reads (market cap + option-volume history) before the final ranking; 0 = off
    # -- flagging
    min_flag_score: float = 5.5
    max_flagged: int = 15
    # -- AI catalyst analysis
    ai_enabled: bool = True
    provider: str = "auto"  # gemini | openai | auto
    model: str | None = None
    base_url: str | None = None
    timeout_seconds: int = 90
    context_news_days: int = 10


@dataclass(frozen=True)
class AdvisorSettings:
    """The desk advisor: plain English in, reviewed setting changes out.

    On the advisor page you write what you want in ordinary language - how
    much risk to take, what to tighten or loosen, or simply a question - and
    the configured model answers with advice plus a list of concrete
    setting changes (key, new value, why). It sees the full settings map
    (every parameter with its meaning, current value, default and allowed
    range), the desk's state (equity, open positions, recent R multiples,
    regime, kill switch, learned adjustments, lessons) and the recent
    conversation. **Nothing is changed by the model itself**: each proposal
    is validated against the same rules as the settings page and applied
    only when you accept it. Credentials, the broker, the account and the
    live switch are outside its reach.
    """

    enabled: bool = True
    provider: str = "auto"  # gemini | openai | auto
    model: str | None = None
    base_url: str | None = None
    timeout_seconds: int = 120
    max_history: int = 12  # earlier exchanges kept in the prompt
    max_changes: int = 12  # most proposals accepted from one reply


@dataclass(frozen=True)
class StrategyConfig:
    momentum: MomentumFilter = field(default_factory=MomentumFilter)
    breakout: BreakoutSetup = field(default_factory=BreakoutSetup)
    episodic_pivot: EpisodicPivotSetup = field(default_factory=EpisodicPivotSetup)
    management: TradeManagement = field(default_factory=TradeManagement)
    risk: RiskSettings = field(default_factory=RiskSettings)
    regime: RegimeFilter = field(default_factory=RegimeFilter)
    themes: ThemeFilter = field(default_factory=ThemeFilter)
    sentiment: SentimentFilter = field(default_factory=SentimentFilter)
    context: ContextSettings = field(default_factory=ContextSettings)
    options_flow: OptionsFlowSettings = field(default_factory=OptionsFlowSettings)
    edge: EdgeScoreSettings = field(default_factory=EdgeScoreSettings)
    adaptive: AdaptiveRisk = field(default_factory=AdaptiveRisk)
    reviewer: ReviewerSettings = field(default_factory=ReviewerSettings)
    committee: CommitteeSettings = field(default_factory=CommitteeSettings)
    schedule: ScheduleSettings = field(default_factory=ScheduleSettings)
    insider_scan: InsiderScanSettings = field(default_factory=InsiderScanSettings)
    entry: EntrySettings = field(default_factory=EntrySettings)
    learning: LearningSettings = field(default_factory=LearningSettings)
    advisor: AdvisorSettings = field(default_factory=AdvisorSettings)

    @property
    def auxiliary_symbols(self) -> set[str]:
        """Symbols loaded for context, never traded or scanned."""
        aux = {self.regime.benchmark}
        if self.regime.max_vix is not None:
            aux.add(self.regime.vix_symbol)
        return aux

    # ---- serialisation -------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "StrategyConfig":
        raw = {k: dict(v) if isinstance(v, dict) else v for k, v in (raw or {}).items()}
        # Keys that moved between sections keep working from older YAML files.
        for old_section, old_key, new_section, new_key in _MOVED_KEYS:
            if old_section in raw and isinstance(raw[old_section], dict) and old_key in raw[old_section]:
                raw.setdefault(new_section, {}).setdefault(new_key, raw[old_section].pop(old_key))
        ctx = raw.get("context")
        if isinstance(ctx, dict) and any(k in ctx for k in _LEGACY_COMMITTEE_KEYS):
            committee = raw.setdefault("committee", {})
            for old_key, leaf in _LEGACY_COMMITTEE_KEYS.items():
                value = ctx.pop(old_key, None)
                for seat in ("bull", "bear", "risk"):
                    if value is not None:
                        committee.setdefault(f"{seat}_{leaf}", value)
                    committee.setdefault(f"{seat}_provider", "openai")
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            section = raw.get(f.name, {}) or {}
            section_type = f.default_factory  # type: ignore[union-attr]
            known = {sf.name for sf in fields(section_type)}
            unknown = set(section) - known
            if unknown:
                log.warning("config: ignoring unknown %s parameters: %s", f.name, ", ".join(sorted(unknown)))
            values = {k: v for k, v in section.items() if k in known}
            if f.name == "edge" and isinstance(values.get("weights"), dict):
                # A YAML file may list only the weights it changes; the rest keep their defaults.
                merged = dict(DEFAULT_EDGE_WEIGHTS)
                merged.update({str(k): float(v) for k, v in values["weights"].items()})
                values["weights"] = merged
            kwargs[f.name] = section_type(**values)
        return cls(**kwargs)

    @classmethod
    def load(cls, path: str | Path | None) -> "StrategyConfig":
        if path is None:
            return cls()
        with open(path) as fh:
            raw = yaml.safe_load(fh) or {}
        return cls.from_dict(raw)

    def save(self, path: str | Path) -> None:
        with open(path, "w") as fh:
            yaml.safe_dump(self.to_dict(), fh, sort_keys=False)

    def with_overrides(self, overrides: dict[str, Any]) -> "StrategyConfig":
        """Apply dotted overrides such as {"management.trail_ma": 20}."""
        cfg = self
        for dotted, value in overrides.items():
            section, _, key = dotted.partition(".")
            if not key or not hasattr(cfg, section):
                raise KeyError(f"Unknown parameter '{dotted}'")
            sub = getattr(cfg, section)
            key, _, leaf = key.partition(".")
            if not hasattr(sub, key):
                raise KeyError(f"Unknown parameter '{dotted}'")
            if leaf:  # e.g. edge.weights.flow -> one entry of a dict-valued field
                current = getattr(sub, key)
                if not isinstance(current, dict):
                    raise KeyError(f"Unknown parameter '{dotted}'")
                value = {**current, leaf: value}
            cfg = replace(cfg, **{section: replace(sub, **{key: value})})
        return cfg

    @property
    def warmup_bars(self) -> int:
        """Bars of history needed before the first signal can be evaluated."""
        return max(130, self.breakout.max_flag_days + 30, self.regime.ma_length + 5)
