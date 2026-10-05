"""A ``TradePlan`` is what we actually intend to do about a ``Signal``.

It records the sizing, every price level (entry, stop, partial target), the
management rules that will apply, an explicit checklist of the gates the
idea passed, the external context that was consulted and a plain-English
rationale - so the chart, the dashboard, the log and the orders all tell
the same story.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from .config import StrategyConfig
from .regime import RegimeSnapshot
from .sentiment import sentiment_ok
from .setups import Signal, momentum_ok
from .themes import theme_ok

if TYPE_CHECKING:  # pragma: no cover
    from .context import ContextReport


@dataclass
class TradePlan:
    symbol: str
    setup: str
    date: str
    entry: float
    stop: float
    shares: int
    risk_per_share: float
    risk_dollars: float
    risk_pct: float
    position_value: float
    position_pct: float
    partial_qty: int
    partial_target: float | None
    partial_after_days: int
    trail_ma: int
    max_hold_days: int
    pivot: float
    theme: str | None
    theme_pct: float | None
    score: float
    checks: dict[str, bool] = field(default_factory=dict)
    notes: dict[str, float | str] = field(default_factory=dict)
    context: dict[str, Any] | None = None
    context_score: float | None = None
    risk_mult: float = 1.0
    rationale: dict[str, str] = field(default_factory=dict)
    committee: dict[str, Any] | None = None
    reviewer: dict[str, Any] | None = None  # strict-JSON verdict from the LLM reviewer (see reviewer.py)
    # Inputs that could not be sourced when this plan was built. Nothing is
    # substituted for them: the reading stays empty and the gap is listed here
    # (and, for required data, fails a check).
    data_gaps: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(self.checks.values()) and self.shares > 0

    @property
    def failed_checks(self) -> list[str]:
        return [k for k, v in self.checks.items() if not v]

    @property
    def rest_qty(self) -> int:
        return self.shares - self.partial_qty

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ok"] = self.ok
        d["failed_checks"] = self.failed_checks
        d["summary"] = self.summary()
        return d

    def summary(self) -> str:
        tgt = f", 1/3 off at {self.partial_target:.2f} (+{(self.partial_target / self.entry - 1) * 100:.1f}%)" if self.partial_target else ""
        return (
            f"{self.symbol} {self.setup}: buy {self.shares} @ {self.entry:.2f}, stop {self.stop:.2f} "
            f"(-{(1 - self.stop / self.entry) * 100:.1f}%, ${self.risk_dollars:,.0f} = {self.risk_pct * 100:.2f}% risk){tgt}, "
            f"then breakeven + {self.trail_ma}d MA trail"
        )


def size_shares(equity: float, entry: float, stop: float, cfg: StrategyConfig, exposure: float, cash: float, risk_mult: float = 1.0) -> int:
    risk_per_share = entry - stop
    if not all(np.isfinite(v) for v in (equity, entry, stop, exposure, cash, risk_mult)) or equity <= 0 or risk_mult < 0 or risk_per_share <= 0 or entry <= 0:
        return 0
    risk_pct = min(cfg.risk.risk_per_trade_pct * risk_mult, cfg.adaptive.max_risk_per_trade_pct if cfg.adaptive.enabled else 1.0)
    shares = int(equity * risk_pct // risk_per_share)
    shares = min(shares, int(equity * cfg.risk.max_position_pct // entry))
    room = equity * cfg.risk.max_gross_exposure - exposure
    shares = min(shares, int(max(room, 0) // entry), int(max(cash, 0) // (entry * (1 + cfg.risk.slippage_bps / 10_000) + cfg.risk.commission_per_share)))
    return max(shares, 0)


def partial_quantity(shares: int, fraction: float) -> int:
    if shares <= 1 or fraction <= 0:
        return 0
    qty = int(round(shares * fraction))
    return min(max(qty, 1), shares - 1)


def adaptive_risk_multiplier(recent_r: list[float], cfg: StrategyConfig) -> float:
    """Scale risk by how the last N trades actually did (see ``AdaptiveRisk``)."""
    a = cfg.adaptive
    if not a.enabled or len(recent_r) < a.min_trades:
        return 1.0
    window = recent_r[-a.lookback_trades :]
    avg = float(np.mean(window))
    if avg <= a.cold_avg_r:
        return a.cold_risk_mult
    if avg >= a.hot_avg_r:
        return a.hot_risk_mult
    return 1.0


def edge_gate_active(cfg: StrategyConfig) -> bool:
    """Is the Unusual Whales edge gate armed on this desk?

    The gate demands a score computed from paid data. Without the key that
    data cannot exist, so a gate would reject every plan for ever - that is a
    misconfiguration, not evidence against the trade. The gate is therefore
    applied only when the feed is configured; the plan then says so in its
    data gaps. With the key present, missing or thin data still fails closed.
    """
    from .uw import has_key

    return bool(cfg.edge.enabled and cfg.edge.gate and cfg.options_flow.enabled and has_key())


EDGE_GATE_INACTIVE = "Unusual Whales edge gate not applied: no UNUSUAL_WHALES_API_KEY saved, so the score cannot be computed - add the key (Connections) or set edge.gate off"


def context_checks(sig: Signal, ctx: "ContextReport | None", cfg: StrategyConfig) -> dict[str, bool]:
    """Gates that depend on live context. Missing data passes, except for
    ``options_flow.require_bullish`` which demands the evidence."""
    c = cfg.context
    checks: dict[str, bool] = {}
    if not c.enabled:
        return checks
    if ctx is None:
        if cfg.options_flow.enabled and cfg.options_flow.require_bullish:
            checks["unusual_flow_bullish"] = False
        if edge_gate_active(cfg):
            checks.update(uw_edge_coverage=False, uw_edge=False)
        return checks
    if c.events_enabled:
        if sig.setup == "breakout" and ctx.days_to_earnings is not None:
            checks["earnings_window"] = ctx.days_to_earnings > c.avoid_earnings_within_days
        else:
            checks["earnings_window"] = True
    if c.news_enabled:
        checks["news_flow"] = True if ctx.news_score is None or c.news_min_score is None else ctx.news_score >= c.news_min_score
    if c.social_enabled:
        ok = True
        if ctx.social_score is not None:
            if c.social_min_score is not None and ctx.social_score < c.social_min_score:
                ok = False
            if c.social_max_score is not None and ctx.social_score > c.social_max_score and sig.setup == "episodic_pivot":
                ok = False  # crowded EP: everybody already knows
        checks["social_crowding"] = ok
    f = cfg.options_flow
    if f.enabled:
        checks["options_flow"] = True if ctx.flow_score is None or f.min_score is None else ctx.flow_score >= f.min_score
        if f.require_bullish:
            # The one gate where missing data fails: "require" means the edge must be seen.
            checks["unusual_flow_bullish"] = bool(
                ctx.flow_score is not None and ctx.flow_score >= f.bullish_threshold and ctx.flow_bull_alerts >= f.min_alerts
            )
        e = cfg.edge
        if edge_gate_active(cfg):
            # Fail closed: a threshold can only be "considered" against a score
            # that was actually computed from enough of the intended evidence.
            edge = ctx.edge or {}
            score = edge.get("score")
            checks["uw_edge_coverage"] = bool(score is not None and float(edge.get("coverage") or 0.0) >= e.min_coverage)
            checks["uw_edge"] = bool(score is not None and float(score) >= e.threshold)
    return checks


def build_plan(
    sig: Signal,
    row: pd.Series,
    cfg: StrategyConfig,
    equity: float,
    exposure: float,
    cash: float,
    regime_ok: bool,
    entry_override: float | None = None,
    context_row: pd.Series | None = None,
    context: "ContextReport | None" = None,
    risk_mult: float = 1.0,
    regime_note: str = "",
    context_expected: bool = False,
    equity_known: bool = True,
    regime_state: RegimeSnapshot | None = None,
) -> TradePlan:
    """Turn a signal into a fully specified, checked and justified trade plan.

    ``row`` is the latest bar (price, liquidity). ``context_row`` is the bar
    the setup's momentum / theme / sentiment context was judged on - the bar
    *before* a triggered breakout, the latest bar for a watchlist idea.
    ``entry_override`` lets the trader plan a market entry at the last price
    instead of the signal's theoretical fill. ``context`` is the live
    news / social / flow / events report, if gathered; ``context_expected``
    says the context layer is on, so a missing report is a data gap rather
    than a disabled feature. ``equity_known`` False means the broker account
    could not be read: the plan is built with zero shares and fails its
    ``broker_account`` check instead of sizing against an assumed balance.
    """
    from .rationale import build_rationale

    regime_scale = 1.0
    if regime_state is not None:
        decision = regime_state.entry_scale(cfg, sig.setup)
        regime_ok = decision is not None
        regime_scale = decision if decision is not None else 1.0
        risk_mult *= regime_scale
        regime_note = regime_state.describe(cfg)

    m = cfg.management
    ctx_row = row if context_row is None else context_row
    entry = float(entry_override if entry_override is not None else sig.entry)
    stop = float(sig.stop)
    risk_per_share = entry - stop
    if not equity_known:
        equity = cash = 0.0
    shares = size_shares(equity, entry, stop, cfg, exposure, cash, risk_mult)
    partial_qty = partial_quantity(shares, m.partial_fraction)
    target = entry + m.partial_target_r * risk_per_share if m.partial_target_r else None

    stop_pct = 1 - stop / entry if entry > 0 else 1.0
    dv = row.get("dollar_vol_20", np.nan)
    checks = {
        "setup_triggered": True,
        "momentum_leader": bool(momentum_ok(ctx_row, cfg)) if sig.setup == "breakout" else True,
        "theme_strength": bool(theme_ok(ctx_row, cfg, sig.setup)),
        "sentiment": bool(sentiment_ok(ctx_row, cfg)),
        "market_regime": bool(regime_ok),
        "liquidity": bool(dv >= cfg.momentum.min_dollar_volume) if not (dv is None or np.isnan(dv)) else False,
        "price_floor": bool(row["close"] >= cfg.momentum.min_price),
        "stop_within_2_adr": bool(stop_pct * 100 <= 2.0 * max(sig.adr_pct, 1e-9)) if sig.adr_pct else True,
        "stop_distance_ok": bool(0 < stop_pct <= m.max_stop_pct),
        **context_checks(sig, context, cfg),
        "size_positive": shares > 0,
    }
    if not equity_known:
        checks["broker_account"] = False
    gaps: list[str] = []
    if not equity_known:
        gaps.append("broker account unavailable: equity and cash unknown, position not sized")
    if dv is None or (isinstance(dv, float) and np.isnan(dv)):
        gaps.append("20-day dollar volume unavailable (too little history): liquidity check failed")
    if context is None and context_expected:
        gaps.append("live context not gathered: news, social, options flow and earnings unknown")
    elif context is not None:
        for name, ok in (context.available or {}).items():
            if not ok and name != "uw_edge":  # the edge score explains itself below
                gaps.append(f"{name} unavailable: {context.errors.get(name, 'no answer')}")
        if "uw_edge" in checks:
            from .context.edge import edge_gaps

            gaps.extend(edge_gaps(context.edge, cfg))
        elif cfg.edge.enabled and cfg.edge.gate and cfg.options_flow.enabled:
            gaps.append(EDGE_GATE_INACTIVE)
    theme = ctx_row.get("theme")
    theme = theme if isinstance(theme, str) else None
    tp = ctx_row.get("theme_pct", np.nan)
    composite = context.composite if context is not None else None
    score = float(sig.score) + (cfg.context.score_weight * composite if composite is not None else 0.0)

    plan = TradePlan(
        symbol=sig.symbol,
        setup=sig.setup,
        date=str(pd.Timestamp(sig.date).date()),
        entry=entry,
        stop=stop,
        shares=shares,
        risk_per_share=risk_per_share,
        risk_dollars=shares * risk_per_share,
        risk_pct=(shares * risk_per_share / equity) if equity > 0 else 0.0,
        position_value=shares * entry,
        position_pct=(shares * entry / equity) if equity > 0 else 0.0,
        partial_qty=partial_qty,
        partial_target=target,
        partial_after_days=m.partial_after_days,
        trail_ma=m.trail_ma,
        max_hold_days=m.max_hold_days,
        pivot=float(sig.pivot),
        theme=theme,
        theme_pct=None if tp is None or (isinstance(tp, float) and np.isnan(tp)) else float(tp),
        score=score,
        checks=checks,
        notes={k: v for k, v in sig.details.items() if isinstance(v, (int, float, str))},
        context=context.to_dict() if context is not None else None,
        context_score=composite,
        risk_mult=risk_mult,
        data_gaps=gaps,
    )
    plan.rationale = build_rationale(plan, sig, ctx_row, cfg, context, regime_note=regime_note, risk_mult=risk_mult)
    plan.notes["regime_risk_multiplier"] = regime_scale
    if regime_state is not None:
        plan.notes["market_regime_policy"] = regime_note
    if not equity_known:
        plan.rationale["size"] = (
            "NOT SIZED: the broker account could not be read, so equity and cash are unknown. "
            "No balance was assumed; the plan is blocked until the broker connection is back."
        )
    return plan


def apply_committee(plan: TradePlan, cfg: StrategyConfig, budget: dict[str, int] | None = None) -> TradePlan:
    """Optionally run the three-seat LLM committee and fold its ruling into the plan.

    ``budget`` (``{"left": n}``) is shared across one cycle so the committee
    sits on at most ``committee.max_plans_per_cycle`` plans; plans past the
    cap are noted, never silently skipped. With ``can_veto`` a reject fails
    the ``committee`` check and a reduce rescales the shares; a seat that
    could not answer leaves the plan exactly as the rules made it.
    """
    c = cfg.committee
    if not c.enabled or not plan.ok:
        return plan
    if budget is not None:
        if budget.get("left", 0) <= 0:
            plan.notes["committee"] = f"not convened: {c.max_plans_per_cycle} plans per cycle already reviewed"
            return plan
        budget["left"] -= 1
    from .llm import committee_review

    review = committee_review(plan.to_dict(), plan.rationale, plan.context, c)
    plan.committee = review
    if "error" in review:
        plan.notes["committee"] = "no ruling: " + review["error"]
        return plan
    if review["verdict"] == "reject" and c.can_veto:
        plan.checks["committee"] = False
    elif review["verdict"] == "reduce" and c.can_veto and review["size_multiplier"] < 1.0:
        _rescale(plan, review["size_multiplier"], cfg)
    return plan


def _rescale(plan: TradePlan, multiplier: float, cfg: StrategyConfig) -> None:
    new_shares = max(int(plan.shares * multiplier), 0)
    ratio = new_shares / plan.shares if plan.shares else 0.0
    plan.risk_pct *= ratio
    plan.position_pct *= ratio
    plan.shares = new_shares
    plan.partial_qty = partial_quantity(new_shares, cfg.management.partial_fraction)
    plan.risk_dollars = new_shares * plan.risk_per_share
    plan.position_value = new_shares * plan.entry
    plan.checks["size_positive"] = new_shares > 0


def resize_for_budget(plan: TradePlan, cfg: StrategyConfig, equity: float, exposure: float, cash: float) -> None:
    """Recheck available capital at submission, preserving any reviewer reduction."""
    qty = min(plan.shares, size_shares(equity, plan.entry, plan.stop, cfg, exposure, cash, plan.risk_mult))
    if qty < plan.shares:
        _rescale(plan, qty / plan.shares, cfg)
        plan.rationale["size"] = f"Resized to {plan.shares} shares against remaining cash and gross exposure at submission."


def apply_reviewer(
    plan: TradePlan,
    cfg: StrategyConfig,
    regime_ok: bool,
    regime_note: str = "",
    portfolio: dict[str, Any] | None = None,
    entry_mode: str | None = None,
    review_rejected: bool = False,
) -> TradePlan:
    """Run the additive LLM reviewer on the full edge bundle and fold the verdict in.

    Runs after the deterministic checklist and the committee, so the model
    only ever sees plans the rules already like (unless ``review_rejected``).
    ``reviewer.mode`` decides whether the verdict is recorded, gates the
    trade, or also resizes it.
    """
    r = cfg.reviewer
    if not r.enabled or (not plan.ok and not review_rejected):
        return plan
    from .reviewer import build_edge_bundle, review_trade, verdict_allows_trade

    bundle = build_edge_bundle(plan.to_dict(), regime_ok, regime_note, portfolio, entry_mode)
    verdict = review_trade(bundle, r)
    plan.reviewer = verdict
    allowed, reason = verdict_allows_trade(verdict, r)
    plan.notes["reviewer"] = reason
    was_ok = plan.ok
    if r.mode in ("gate", "gate_and_size"):
        plan.checks["llm_reviewer"] = bool(allowed)
    if allowed and r.mode == "gate_and_size" and "error" not in verdict and verdict.get("sizeMultiplier", 1.0) < 1.0:
        _rescale(plan, verdict["sizeMultiplier"], cfg)
        if plan.rationale:
            plan.rationale["size"] = (
                plan.rationale.get("size", "")
                + f" The LLM reviewer asked for x{verdict['sizeMultiplier']:.2f} size ({verdict.get('sizeNote') or 'no note'}), so {plan.shares} shares are taken."
            )
    if was_ok and not plan.ok and plan.rationale:
        thesis = verdict.get("thesis") or verdict.get("error") or ""
        plan.rationale["verdict"] = f"BLOCKED by the LLM reviewer ({reason}). {thesis}".strip()
    return plan
