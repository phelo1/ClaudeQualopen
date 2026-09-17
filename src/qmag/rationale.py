"""Plain-English justification for a trade plan.

Every plan carries a ``rationale`` dict with one paragraph per decision -
setup, entry, position size, stop, profit taking, context - plus a bull
case, a bear case and the verdict. It is generated deterministically from the
numbers the engine actually used, so it is an audit trail rather than a
story. The optional LLM committee (``llm.py``) may add its own bull / bear /
risk review on top but never replaces these sections.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from .config import StrategyConfig

if TYPE_CHECKING:  # pragma: no cover
    from .context import ContextReport
    from .plan import TradePlan
    from .setups import Signal


def _pct(x: float | None, digits: int = 0) -> str:
    return "n/a" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x * 100:+.{digits}f}%"


def _money(x: float | None) -> str:
    if x is None:
        return "n/a"
    if x >= 1e9:
        return f"${x / 1e9:.1f}B"
    if x >= 1e6:
        return f"${x / 1e6:.0f}M"
    return f"${x:,.0f}"


def _shares(x: float | None) -> str:
    return "n/a" if x is None else (f"{x / 1e6:.1f}M" if x >= 1e6 else f"{x / 1e3:.0f}K")


def _describe_trade(t: dict) -> str:
    """'$1.2M of 17-Jan 150C bought at the ask, sweep, vol/OI 4.1 (12% OTM)'."""
    prem = t.get("premium") or 0
    money = f"${prem / 1e6:.1f}M" if prem >= 1e6 else f"${prem / 1e3:.0f}k"
    strike = t.get("strike")
    contract = f"{strike:g}{'C' if t.get('type') == 'call' else 'P' if t.get('type') == 'put' else ''}" if strike is not None else t.get("type", "")
    expiry = t.get("expiry") or ""
    side = {"ask": "bought at the ask", "bid": "sold at the bid", "unknown": ""}.get(t.get("side", "unknown"), "")
    extras = []
    if t.get("sweep"):
        extras.append("sweep")
    if t.get("vol_oi") is not None:
        extras.append(f"vol/OI {t['vol_oi']:.1f}")
    if t.get("otm_pct") is not None:
        extras.append(f"{t['otm_pct']:.0f}% OTM")
    tail = f" ({', '.join(extras)})" if extras else ""
    return f"{money} of {expiry} {contract} {side}".strip() + tail


def build_rationale(plan: "TradePlan", sig: "Signal", row, cfg: StrategyConfig, ctx: "ContextReport | None", regime_note: str = "", risk_mult: float = 1.0) -> dict[str, str]:
    d = sig.details
    m, r = cfg.management, cfg.risk
    risk = plan.entry - plan.stop
    bull: list[str] = []
    bear: list[str] = []

    # ---- setup ----------------------------------------------------------
    g1, g3, g6 = row.get("gain_1m"), row.get("gain_3m"), row.get("gain_6m")
    adr = float(sig.adr_pct)
    if sig.setup == "breakout":
        setup = (
            f"Momentum breakout. {sig.symbol} is a leader: {_pct(g1)} over 1 month, {_pct(g3)} over 3 months, {_pct(g6)} over 6 months, "
            f"with an average daily range of {adr:.1f}% (minimum {cfg.momentum.min_adr_pct}%). "
            f"It has consolidated for {int(d.get('flag_days', 0))} days in a flag {d.get('depth', 0) * 100:.0f}% as deep as the prior move "
            f"(limit {cfg.breakout.max_flag_depth * 100:.0f}%) while the range contracted to {d.get('contraction', 1) * 100:.0f}% of normal, "
            f"holding above the rising {cfg.breakout.fast_ma}/{cfg.breakout.slow_ma}-day averages. The pivot is the flag high at {plan.pivot:.2f}."
        )
        if "rvol" in d:
            setup += f" It cleared the pivot today on {d['rvol']:.1f}x average volume."
            bull.append(f"volume confirmation {d['rvol']:.1f}x on the breakout day")
        else:
            setup += f" Price closes {d.get('distance_to_pivot_pct', 0):.1f}% below the pivot; the order only fires if it trades through."
        bull.append("tight, orderly flag after a strong impulse - the highest-probability version of the pattern")
        if d.get("contraction", 1) < 0.6:
            bull.append("range contraction is pronounced (volatility squeeze)")
        if d.get("depth", 0) > 0.4:
            bear.append("the flag is on the deep side; shallower flags fail less")
    else:
        setup = (
            f"Episodic pivot. {sig.symbol} gapped {d.get('gap_pct', 0):+.0f}% on {d.get('rvol', 0):.1f}x its 50-day volume from a neglected base "
            f"(prior 3-month move {d.get('prior_gain_3m', 0):+.0f}%, cap {cfg.episodic_pivot.max_prior_gain_3m * 100:.0f}%), and closed in the top of its range. "
            "Institutions cannot finish buying in one day, which is what makes EPs trend."
        )
        bull.append("neglected before the gap - little overhead supply from recent buyers")
        if d.get("rvol", 0) >= 5:
            bull.append(f"extreme volume ({d['rvol']:.1f}x) signals a genuine change in ownership")
        if ctx is not None:
            if (ctx.earnings_recent_days is not None and ctx.earnings_recent_days <= 2) or "earnings" in ctx.catalysts or "guidance" in ctx.catalysts:
                bull.append("gap is earnings/guidance-driven - the catalyst type EPs work best on")
            elif ctx.catalysts:
                bull.append(f"identified catalyst: {', '.join(ctx.catalysts[:3])}")
            else:
                bear.append("no identified catalyst in the news flow - gaps on rumours fade more often")

    # ---- entry ----------------------------------------------------------
    if sig.setup == "breakout" and "rvol" not in d:
        entry = (
            f"Buy-stop-limit {plan.shares} shares at {plan.entry:.2f} ({cfg.breakout.entry_buffer_pct * 100:.1f}% above the {plan.pivot:.2f} pivot), "
            f"limit {plan.pivot * (1 + cfg.breakout.max_gap_pct):.2f}: if it gaps more than {cfg.breakout.max_gap_pct * 100:.0f}% over the pivot we do not chase - "
            "a big gap through the pivot changes the risk/reward and often gets sold into."
        )
    else:
        entry = f"Buy {plan.shares} shares at market (~{plan.entry:.2f}); the trigger has already printed, so waiting only adds risk to the stop."

    # ---- size -----------------------------------------------------------
    eff_risk = r.risk_per_trade_pct * risk_mult
    size = (
        f"Risk {_pct(eff_risk, 2)} of equity = ${plan.risk_dollars:,.0f} at the stop. Stop distance is {risk:.2f} ({risk / plan.entry * 100:.1f}%), "
        f"so ${plan.risk_dollars:,.0f} / {risk:.2f} = {plan.shares} shares (${plan.position_value:,.0f}, {plan.position_pct * 100:.1f}% of equity; cap {r.max_position_pct * 100:.0f}%)."
    )
    if risk_mult != 1.0:
        size += f" Risk is scaled x{risk_mult:.2f} by recent realised results (see adaptive risk)."
    if plan.position_pct >= r.max_position_pct - 0.005:
        size += " The position cap, not the risk budget, is the binding limit - the stop is tight relative to the price."

    # ---- stop -----------------------------------------------------------
    stop = (
        f"Initial stop {plan.stop:.2f} = entry - {m.stop_adr_mult:g} x ADR (${sig.adr_dollar:.2f}), i.e. {(1 - plan.stop / plan.entry) * 100:.1f}% below entry. "
        "This approximates the low of the breakout day; a close back inside the flag means the breakout failed and we want out at 1R, not 3R. "
        f"The stop is {'within' if plan.checks.get('stop_within_2_adr', True) else 'wider than'} 2 ADR - "
        + ("a normal day's noise should not take us out." if plan.checks.get("stop_within_2_adr", True) else "too wide for a clean 1R loss.")
    )
    if not plan.checks.get("stop_distance_ok", True):
        stop += (
            f" REJECTED: a {(1 - plan.stop / plan.entry) * 100:.1f}% stop exceeds the {m.max_stop_pct * 100:.0f}% ceiling - "
            "the name is too erratic for a clean 1R loss; the position would be tiny and the R poorly defined."
        )

    # ---- profit plan ----------------------------------------------------
    tgt = plan.partial_target
    profit = (
        f"Sell {plan.partial_qty} shares ({m.partial_fraction * 100:.0f}%) at {tgt:.2f} (+{m.partial_target_r:g}R, {(tgt / plan.entry - 1) * 100:.1f}%) via a resting OCO order, "
        if tgt
        else f"Sell {plan.partial_qty} shares ({m.partial_fraction * 100:.0f}%) "
    )
    profit += (
        f"or at the close of day {m.partial_after_days} if the trade is green - whichever comes first. Then lift the stop to breakeven ({plan.entry:.2f}) "
        f"and trail the remaining {plan.rest_qty} shares on a close below the {m.trail_ma}-day SMA; time stop after {m.max_hold_days} days. "
        "Selling a third into strength pays for the trade; the runner is where the outsized winners come from."
    )

    # ---- context --------------------------------------------------------
    bits: list[str] = []
    if regime_note:
        bits.append(f"Market: {regime_note} -> {'risk-on' if plan.checks.get('market_regime') else 'RISK-OFF'}.")
    if plan.theme:
        bits.append(f"Theme '{plan.theme}' ranks in the {plan.theme_pct * 100:.0f}th percentile of theme momentum." if plan.theme_pct is not None else f"Theme: {plan.theme}.")
        if plan.theme_pct is not None and plan.theme_pct >= 0.7:
            bull.append(f"strong group: '{plan.theme}' is a leading theme")
        elif plan.theme_pct is not None and plan.theme_pct < cfg.themes.min_theme_percentile:
            bear.append(f"weak group: '{plan.theme}' theme is lagging")
    if ctx is not None:
        if ctx.industry:
            bits.append(f"{ctx.sector} / {ctx.industry}, market cap {_money(ctx.market_cap)}.")
        if ctx.news_score is not None:
            tone = "positive" if ctx.news_score > 0.15 else "negative" if ctx.news_score < -0.15 else "neutral"
            bits.append(f"News tone over {cfg.context.news_lookback_days} days is {tone} ({ctx.news_score:+.2f} across {ctx.news_count} headlines)" + (f"; catalysts: {', '.join(ctx.catalysts[:4])}." if ctx.catalysts else "."))
            if ctx.news_score < -0.3:
                bear.append("news flow is negative")
            if "offering" in ctx.catalysts:
                bear.append("recent offering / dilution headline")
            if "fda" in ctx.catalysts or "contract" in ctx.catalysts or "guidance" in ctx.catalysts:
                bull.append("fundamental catalyst in the headlines")
        elif ctx.available.get("news_finviz") or ctx.available.get("news_yahoo"):
            bits.append("No headlines in the window - a quiet tape, which suits breakouts.")
        if ctx.social_score is not None:
            crowd = "euphoric" if ctx.social_score > 0.7 else "bullish" if ctx.social_score > 0.2 else "bearish" if ctx.social_score < -0.2 else "mixed"
            bits.append(f"Social chatter is {crowd} ({ctx.social_score:+.2f}, {ctx.social_messages} posts, {ctx.social_bullish} tagged bullish / {ctx.social_bearish} bearish).")
            if ctx.social_score > 0.7:
                bear.append("crowd is already unanimous - crowded trades reverse hard")
            elif ctx.social_score < -0.3:
                bear.append("crowd is bearish on the name")
        elif ctx.available.get("stocktwits"):
            bits.append(f"Social chatter is quiet ({ctx.social_messages} posts) - the name is still under the radar.")
            bull.append("under-owned by the crowd")
        if ctx.flow_score is not None:
            bits.append(f"Options flow {'leans bullish' if ctx.flow_score > 0.2 else 'leans bearish' if ctx.flow_score < -0.2 else 'is balanced'} ({ctx.flow_score:+.2f}; {ctx.flow_note}).")
            if ctx.flow_trades:
                top = ctx.flow_trades[:3]
                bits.append("Largest unusual trades: " + "; ".join(_describe_trade(t) for t in top) + ".")
            if ctx.flow_unusual and ctx.flow_score > 0.2:
                bull.append(f"unusual bullish options activity ({ctx.flow_bull_alerts} trades, {ctx.flow_sweeps} sweeps)")
            elif ctx.flow_unusual and ctx.flow_score < -0.2:
                bear.append(f"unusual bearish options activity ({ctx.flow_bear_alerts} trades) - somebody is buying protection")
            else:
                (bull if ctx.flow_score > 0.3 else bear if ctx.flow_score < -0.3 else []).append(f"options flow {ctx.flow_score:+.2f}")
            if not plan.checks.get("unusual_flow_bullish", True):
                bits.append("REJECTED: the strategy requires bullish unusual flow and there is none.")
        elif cfg.options_flow.enabled and ctx.available.get("unusual_whales"):
            bits.append("Options tape is quiet - no unusual trades above the premium floor.")
            if not plan.checks.get("unusual_flow_bullish", True):
                bits.append("REJECTED: the strategy requires bullish unusual flow and there is none.")
        elif cfg.options_flow.enabled and "unusual_whales" in ctx.available:
            bits.append("Options flow unavailable (Unusual Whales did not answer).")
            if not plan.checks.get("unusual_flow_bullish", True):
                bits.append("REJECTED: bullish unusual flow is required and could not be verified.")
        elif not cfg.options_flow.enabled:
            bits.append("Options flow scan is off (options_flow.enabled: false).")
        if cfg.options_flow.enabled and cfg.edge.enabled:
            from .context.edge import describe_edge

            bits.append(describe_edge(ctx.edge, cfg))
            feats = (ctx.edge or {}).get("features") or {}
            for n in (ctx.edge or {}).get("positives", [])[:3]:
                r = feats.get(n, {})
                bull.append(f"{r.get('label', n).lower()} {r.get('score', 0):+.2f} (Unusual Whales)")
            for n in (ctx.edge or {}).get("negatives", [])[:3]:
                r = feats.get(n, {})
                bear.append(f"{r.get('label', n).lower()} {r.get('score', 0):+.2f} (Unusual Whales)")
            if plan.checks.get("uw_edge_coverage") is False:
                bits.append("REJECTED: not enough Unusual Whales evidence was sourced to trust the edge score.")
            elif plan.checks.get("uw_edge") is False:
                bits.append("REJECTED: the Unusual Whales edge score is below the entry threshold.")
        if ctx.days_to_earnings is not None:
            bits.append(f"Next earnings {ctx.earnings_date} ({ctx.days_to_earnings} days away).")
            if ctx.days_to_earnings <= cfg.context.avoid_earnings_within_days:
                bear.append("earnings imminent - binary event inside the trade")
            elif ctx.days_to_earnings <= 10:
                bear.append("earnings within two weeks; plan to be at breakeven or out")
        if ctx.float_shares is not None:
            lf = ctx.float_shares < cfg.context.small_float_shares
            bits.append(f"Float {_shares(ctx.float_shares)}{' (low float)' if lf else ''}, short interest {ctx.short_float_pct:.1f}% of float." if ctx.short_float_pct is not None else f"Float {_shares(ctx.float_shares)}.")
            if lf:
                bull.append("low float amplifies the move")
            if ctx.short_float_pct is not None and ctx.short_float_pct >= cfg.context.high_short_float_pct:
                bull.append(f"{ctx.short_float_pct:.0f}% short interest is squeeze fuel")
        if ctx.insider_trans_pct is not None and ctx.insider_trans_pct < -10:
            bear.append(f"insiders net sellers ({ctx.insider_trans_pct:+.0f}% over 6 months)")
        if ctx.insider_trans_pct is not None and ctx.insider_trans_pct > 5:
            bull.append(f"insiders net buyers ({ctx.insider_trans_pct:+.0f}%)")
        missing = [k for k, ok in ctx.available.items() if not ok]
        if missing:
            bits.append(f"Unavailable sources: {', '.join(missing)}.")
    context = " ".join(bits) if bits else "No external context configured."

    # ---- verdict --------------------------------------------------------
    if plan.ok:
        verdict = f"TAKE. All {len(plan.checks)} checks pass; expected payoff is asymmetric (1R at risk, {m.partial_target_r or 2:g}R partial then an open-ended runner)."
    else:
        verdict = f"PASS. Failed: {', '.join(plan.failed_checks)}. The setup is valid but the checklist exists to skip exactly these conditions."

    return {
        "setup": setup,
        "entry": entry,
        "size": size,
        "stop": stop,
        "profit_plan": profit,
        "context": context,
        "bull_case": "; ".join(dict.fromkeys(bull)) or "momentum leader in a valid setup",
        "bear_case": "; ".join(dict.fromkeys(bear)) or "the usual: roughly half of breakouts fail, which the 1R stop is for",
        "verdict": verdict,
    }
