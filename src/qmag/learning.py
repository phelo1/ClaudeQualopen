"""Learning from the trade journal: what worked, what did not, and why.

Four research layers using journal records and explicitly simulated shadows.
Outcome provenance separates observed broker fills, paper fills and estimates:

1. **Post-mortems.** Every closed trade gets a rule-based explanation
   (``explain_trade``) from the conditions it was entered under (the
   ``features`` the trader stored at order time: volume pace, time of day,
   theme rank, edge score, flag shape, reviewer verdict, ...) and how it
   travelled (MFE / MAE, exit reason). ``learning.llm_enabled`` adds a
   strict-JSON post-mortem from the configured model when a key is present.

2. **Shadow trades.** Setups the trader saw but did not take (rejected by a
   check, held back by an entry gate, portfolio full, or armed without a
   resting order) are resolved against later bars (``update_shadows``) with
   a deliberately simple fill / stop / target / time-out simulation. They
   are counterfactuals, labelled as such, used only to judge whether a
   filter or gate is earning its keep.

3. **The weekly review** (``review``). Buckets the journal by feature and
   reports each bucket's lift over the overall expectancy (shrunk towards
   zero for small samples), writes plain-English lessons, scores every
   check / gate by the shadows it blocked. Changes are proposals by default.
   Experimental ``auto_apply`` can tighten one bounded knob by one step only
   with sufficient fresh broker-verified negative evidence. Applied changes use
   ``learning_overrides.yaml`` with their evidence, layered on top of the
   operator's settings by the session, listed on the learning page and can
   be reset. A knob is never moved outside its band, never more than one
   step per review, and never reversed within ``COOLDOWN_DAYS``.

   The objective is **total R**, not R per trade: a knob is only tightened
   when the trades it would remove *lose* money on average (removing
   profitable-but-mediocre trades would lift the average and lower the
   total), and only loosened when the setups it excluded would have made
   money. Every applied adjustment then carries a **scorecard**
   (``score_adjustments``): what the real trades and shadows did after the
   change. Reversions remain advisory proposals; shadow outcomes cannot
   automatically loosen settings or reverse an applied change.

4. **Near misses.** The full scan also runs the detectors one step looser
   on the detector-level knobs (volume ratio, flag depth, ADR floor) and
   records what appears only under the relaxed rules as ``near_miss``
   shadows tagged with the knob that excluded them - the evidence the
   loosening side of those knobs would otherwise never see.
"""

from __future__ import annotations

import json
from .persistence import atomic_text, atomic_json
import logging
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
import yaml

from .config import StrategyConfig
from .redact import describe_error

if TYPE_CHECKING:  # pragma: no cover
    from .trader import TraderState

log = logging.getLogger(__name__)

OVERRIDES_FILE = "learning_overrides.yaml"
REPORT_FILE = "learning_report.json"
COOLDOWN_DAYS = 14
REVERT_COOLDOWN_DAYS = 28  # a knob whose adjustment was reverted rests twice as long
SHRINK_K = 4.0  # pseudo-count that pulls small-sample lifts towards zero
NEAR_MISS_PER_SCAN = 12  # most near-miss shadows a single full scan may add


# --------------------------------------------------------------------------- #
# Knobs the review may move
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Knob:
    key: str  # dotted config key
    label: str
    lo: float
    hi: float
    step: float
    feature: str  # journal feature the knob is a floor / ceiling on
    kind: str  # "floor" (tighten = raise) | "ceiling" (tighten = lower)
    check: str | None = None  # plan check the knob is solely responsible for (loosen from shadows)
    gate: str | None = None  # entry gate the knob is solely responsible for (loosen from held shadows)
    fmt: str = "{:.2f}"
    detector: bool = False  # applied inside the detectors: near-miss shadows are the loosening evidence

    def clamp(self, v: float) -> float:
        v = min(max(v, self.lo), self.hi)
        digits = max(0, -int(math.floor(math.log10(self.step)))) if self.step < 1 else 0
        return round(v, digits)

    def tighter(self, v: float) -> float:
        return self.clamp(v + self.step if self.kind == "floor" else v - self.step)

    def looser(self, v: float) -> float:
        return self.clamp(v - self.step if self.kind == "floor" else v + self.step)

    def in_marginal_band(self, value: float, current: float) -> bool:
        """Trades that only just cleared the knob: the ones a one-step tightening would have removed."""
        if self.kind == "floor":
            return current <= value < current + self.step
        return current - self.step < value <= current

    def excludes(self, value: float, setting: float) -> bool:
        """Would this knob, set to ``setting``, exclude a trade with this feature value?"""
        return value < setting if self.kind == "floor" else value > setting

    def between(self, value: float, a: float, b: float) -> bool:
        """Feature values that one setting admits and the other excludes."""
        return self.excludes(value, a) != self.excludes(value, b)


KNOBS: tuple[Knob, ...] = (
    Knob("breakout.min_breakout_volume_ratio", "breakout volume ratio (full day)", 1.0, 2.5, 0.1, "rvol", "floor", detector=True),
    Knob("entry.confirm_volume_ratio", "intraday volume-pace confirmation", 0.8, 2.0, 0.1, "rvol", "floor", gate="volume"),
    Knob("entry.opening_range_minutes", "opening-range wait (minutes)", 0, 30, 5, "minutes_since_open", "floor", gate="opening_range", fmt="{:.0f}"),
    Knob("edge.threshold", "Unusual Whales edge threshold", -0.2, 0.6, 0.05, "edge_score", "floor", check="uw_edge"),
    Knob("themes.min_theme_percentile", "theme strength floor", 0.1, 0.6, 0.05, "theme_pct", "floor", check="theme_strength"),
    Knob("momentum.min_adr_pct", "ADR floor (%)", 3.0, 7.0, 0.5, "adr_pct", "floor", check="momentum_leader", fmt="{:.1f}", detector=True),
    Knob("breakout.max_flag_depth", "max flag depth", 0.3, 0.6, 0.05, "depth", "ceiling", detector=True),
    Knob("breakout.max_gap_pct", "max gap / extension over the pivot", 0.02, 0.08, 0.01, "gap_pct", "ceiling", gate="extension"),
)
KNOB_BY_KEY = {k.key: k for k in KNOBS}
DETECTOR_KNOBS = tuple(k for k in KNOBS if k.detector)


def relaxed_config(cfg: StrategyConfig) -> StrategyConfig:
    """The detectors one step looser on every detector-level knob (the near-miss pass)."""
    return cfg.with_overrides({k.key: k.looser(config_value(cfg, k.key)) for k in DETECTOR_KNOBS})


def near_miss_reasons(features: dict, cfg: StrategyConfig) -> list[str]:
    """Which detector-level knobs, at their *strict* settings, exclude a signal
    found by the relaxed pass. Empty when the relaxed flag differs for some
    other reason (then it is not attributable and is not recorded)."""
    out = []
    for k in DETECTOR_KNOBS:
        v = feature_value(features, k.feature)
        if v is not None and k.excludes(v, config_value(cfg, k.key)):
            out.append(k.key)
    return out


def solely_blocked_by(knob: Knob, shadow: dict) -> bool:
    """A shadow that this knob, and nothing else, kept out of the book."""
    kind = shadow.get("kind")
    if kind == "rejected":
        return bool(knob.check) and list(shadow.get("failed_checks") or []) == [knob.check]
    if kind == "held":
        return bool(knob.gate) and {gate_of(r) for r in shadow.get("reasons") or []} == {knob.gate}
    if kind == "near_miss":
        return list(shadow.get("reasons") or []) == [knob.key]
    return False


def config_value(cfg: StrategyConfig, key: str) -> float:
    obj: Any = cfg
    for part in key.split("."):
        obj = getattr(obj, part)
    return float(obj)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _minutes_since_open(entry_time: str | None) -> float | None:
    if not entry_time or entry_time == "close":
        return None
    try:
        hh, mm = entry_time.split(":")
        return (int(hh) - 9) * 60 + int(mm) - 30
    except ValueError:
        return None


def feature_value(features: dict | None, name: str) -> float | None:
    """Numeric feature by name (derives ``minutes_since_open`` from ``entry_time``)."""
    f = features or {}
    if name == "minutes_since_open":
        return _minutes_since_open(f.get("entry_time"))
    return _f(f.get(name))


def stats(rs: list[float]) -> dict[str, Any]:
    n = len(rs)
    if n == 0:
        return {"n": 0, "win_rate": None, "avg_r": None, "median_r": None, "profit_factor": None, "best": None, "worst": None, "total_r": 0.0}
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gross_w, gross_l = sum(wins), -sum(losses)
    return {
        "n": n,
        "win_rate": round(len(wins) / n, 3),
        "avg_r": round(sum(rs) / n, 3),
        "median_r": round(float(pd.Series(rs).median()), 3),
        "profit_factor": round(gross_w / gross_l, 2) if gross_l > 0 else None,  # None: no losses (or no wins) to divide by
        "best": round(max(rs), 2),
        "worst": round(min(rs), 2),
        "total_r": round(sum(rs), 2),
    }


def shrunk_lift(avg_bucket: float, avg_all: float, n: int) -> float:
    return round((avg_bucket - avg_all) * n / (n + SHRINK_K), 3)


# --------------------------------------------------------------------------- #
# 1. Post-mortems
# --------------------------------------------------------------------------- #
def explain_trade(rec: dict) -> dict:
    """Rule-based post-mortem for a closed trade record (features + outcome).

    Returns ``{"text", "tags", "what_worked", "what_failed", "lesson"}``.
    Only states what the stored numbers show; unknown inputs are skipped.
    """
    f = rec.get("features") or {}
    r = _f(rec.get("r_multiple")) or 0.0
    reason = rec.get("exit_reason") or "unknown"
    hold = rec.get("hold_days")
    mfe, mae = _f(rec.get("mfe_r")) or 0.0, _f(rec.get("mae_r")) or 0.0
    rvol, theme_pct, edge = _f(f.get("rvol")), _f(f.get("theme_pct")), _f(f.get("edge_score"))
    gap, depth, adr = _f(f.get("gap_pct")), _f(f.get("depth")), _f(f.get("adr_pct"))
    mins = _minutes_since_open(f.get("entry_time"))
    mode = f.get("entry_mode") or "unknown"
    tags: list[str] = [reason]
    worked: list[str] = []
    failed: list[str] = []

    if reason == "failed_breakout":
        tags.append("failed_breakout")
        if mode == "buy_stop":
            tags.append("wick_fill")
            failed.append("a resting buy-stop was filled on a move that did not hold above the pivot")
        else:
            failed.append("the breakout did not hold above the pivot")
    if reason == "time_stop":
        failed.append(f"no follow-through: best open profit was {mfe:+.2f}R and it closed at or under the entry after {hold if hold is not None else 'several'} sessions")
    if rvol is not None:
        if rvol < 1.2:
            tags.append("low_volume")
            failed.append(f"volume was only {rvol:.2f}x the 20-day average" + (" (projected)" if f.get("rvol_projected") else ""))
        elif rvol >= 2.0:
            tags.append("strong_volume")
            worked.append(f"volume ran {rvol:.1f}x the 20-day average")
    if mins is not None and mins < 15:
        tags.append("opening_range")
        failed.append(f"entered {mins:.0f} min after the open, inside the opening range") if r <= 0 else worked.append("an early entry caught the move")
    if theme_pct is not None:
        if theme_pct < 0.5:
            tags.append("weak_theme")
            failed.append(f"theme ranked in the {theme_pct:.0%} percentile")
        elif theme_pct >= 0.8:
            tags.append("top_theme")
            worked.append(f"theme in the top {1 - theme_pct:.0%} of the market")
    if edge is not None:
        if edge < 0:
            tags.append("negative_edge")
            failed.append(f"Unusual Whales edge score was {edge:+.2f}")
        elif edge >= 0.3:
            tags.append("strong_edge")
            worked.append(f"Unusual Whales edge score was {edge:+.2f}")
    if gap is not None and gap >= 0.03:
        tags.append("extended_entry")
        failed.append(f"entry was {gap:+.1%} above the pivot")
    if depth is not None:
        if depth >= 0.35:
            tags.append("deep_flag")
            failed.append(f"the flag was deep ({depth:.0%} pullback)")
        elif depth <= 0.15:
            tags.append("tight_flag")
            worked.append(f"tight flag ({depth:.0%} pullback)")
    if adr is not None and adr < 4:
        tags.append("low_adr")
        failed.append(f"ADR of {adr:.1f}% left little room to run")
    if f.get("reviewer_action") in ("HOLD", "SELL"):
        tags.append("reviewer_doubt")
        (failed if r <= 0 else worked).append(f"the LLM reviewer said {f['reviewer_action']}")
    if f.get("regime_ok") is False:
        tags.append("risk_off_regime")
        failed.append("the market regime was risk-off")
    if mfe >= 1.5 and r <= 0.2:
        tags.append("gave_back")
        failed.append(f"the trade was up {mfe:.1f}R at best and closed at {r:+.2f}R")
    if mfe < 0.3 and r < 0:
        tags.append("never_worked")
        failed.append("it never went more than 0.3R in our favour")
    if hold is not None and hold <= 1 and r < 0:
        tags.append("quick_loss")
    if r >= 3:
        tags.append("runner")
        worked.append(f"a {r:.1f}R runner")
    if r > 0 and reason in ("trail_ma", "target"):
        worked.append("the exit rule banked the move")

    outcome = "winner" if r > 0.1 else "loser" if r < -0.1 else "scratch"
    head = f"{rec.get('symbol', '?')} {rec.get('setup', '')}: {outcome} {r:+.2f}R, exit by {reason}" + (f" after {hold} days" if hold is not None else "")
    if failed and r <= 0:
        lesson = "Avoid: " + "; ".join(failed[:3]) + "."
    elif worked and r > 0:
        lesson = "Repeat: " + "; ".join(worked[:3]) + "."
    elif failed:
        lesson = "Won despite: " + "; ".join(failed[:2]) + "."
    else:
        lesson = "Nothing in the recorded conditions stands out."
    text = head + ". " + (("Worked: " + "; ".join(worked) + ". ") if worked else "") + (("Did not work: " + "; ".join(failed) + ". ") if failed else "") + lesson
    return {"text": text, "tags": sorted(set(tags)), "what_worked": worked, "what_failed": failed, "lesson": lesson}


POST_MORTEM_PROMPT = (
    "You are a trading coach reviewing ONE closed swing trade taken by a systematic Qullamaggie-style breakout strategy. "
    "You get the entry conditions that were measured at the time (features), the outcome (R multiple, exit reason, "
    "maximum favourable / adverse excursion in R) and a rule-based post-mortem. Explain in plain English what worked, "
    "what did not, and the single most useful lesson for the selection or trigger rules. Use only the numbers given; "
    "do not invent context. Answer ONLY with JSON: "
    '{"what_worked": str, "what_failed": str, "lesson": str, "tags": [str], "confidence": number 0-1}.'
)
POST_MORTEM_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "what_worked": {"type": "STRING"},
        "what_failed": {"type": "STRING"},
        "lesson": {"type": "STRING"},
        "tags": {"type": "ARRAY", "items": {"type": "STRING"}},
        "confidence": {"type": "NUMBER"},
    },
    "required": ["what_worked", "what_failed", "lesson", "tags", "confidence"],
}


def llm_available(cfg: StrategyConfig) -> tuple[bool, str]:
    """(usable, note) for the learning post-mortem model - no key, no call."""
    ln = cfg.learning
    if not (ln.enabled and ln.llm_enabled):
        return False, "AI post-mortems off"
    from .reviewer import resolve

    res = resolve(ln)
    return res.configured, res.describe() if res.configured else (res.error or "not configured")


def ai_post_mortems(closed: list[dict], cfg: StrategyConfig, registry: Any = None) -> dict:
    """Add ``post_mortem_ai`` to recent closed trades that lack one. Mutates the records."""
    ln = cfg.learning
    out: dict[str, Any] = {"enabled": bool(ln.enabled and ln.llm_enabled), "analysed": 0, "skipped": 0, "error": None, "provider": None}
    ok, note = llm_available(cfg)
    out["provider"] = note
    if not ok:
        out["error"] = None if not out["enabled"] else note
        if registry is not None and out["enabled"]:
            registry.record("llm_learning", False, detail="AI post-mortems", error=note)
        return out
    from .reviewer import ask_json, parse_json_object

    todo = [r for r in reversed(closed) if not r.get("post_mortem_ai")][: ln.max_post_mortems_per_run]
    for rec in todo:
        bundle = {
            "symbol": rec.get("symbol"), "setup": rec.get("setup"), "entry_date": rec.get("entry_date"), "closed_on": rec.get("closed_on"),
            "entry_price": rec.get("entry_price"), "initial_stop": rec.get("initial_stop"), "exit_price": rec.get("exit_price"), "exit_reason": rec.get("exit_reason"),
            "r_multiple": rec.get("r_multiple"), "pnl_pct": rec.get("pnl_pct"), "hold_days": rec.get("hold_days"), "mfe_r": rec.get("mfe_r"), "mae_r": rec.get("mae_r"),
            "features": {k: v for k, v in (rec.get("features") or {}).items() if k not in ("checks",)},
            "rule_based_post_mortem": rec.get("post_mortem"),
        }
        try:
            text, provider, model = ask_json(bundle, ln, POST_MORTEM_PROMPT, POST_MORTEM_SCHEMA)
            raw = parse_json_object(text)
            rec["post_mortem_ai"] = {
                "what_worked": str(raw.get("what_worked", ""))[:600],
                "what_failed": str(raw.get("what_failed", ""))[:600],
                "lesson": str(raw.get("lesson", ""))[:400],
                "tags": [str(t)[:40] for t in (raw.get("tags") or []) if isinstance(t, (str, int, float))][:8],
                "confidence": max(0.0, min(1.0, float(raw.get("confidence", 0.0) or 0.0))),
                "model": f"{provider}/{model}",
                "at": pd.Timestamp.now("UTC").isoformat(),
            }
            out["analysed"] += 1
        except Exception as exc:
            out["error"] = describe_error(exc)
            out["skipped"] += 1
            log.warning("AI post-mortem for %s failed: %s", rec.get("symbol"), out["error"])
            break  # one transport failure means the rest would fail the same way
    if registry is not None:
        registry.record("llm_learning", out["error"] is None, detail=f"AI post-mortems via {note}: {out['analysed']} analysed", items=out["analysed"], error=out["error"])
    return out


# --------------------------------------------------------------------------- #
# 2. Shadow trades
# --------------------------------------------------------------------------- #
def update_shadows(state: "TraderState", data: dict[str, pd.DataFrame], cfg: StrategyConfig, asof: str) -> int:
    """Resolve open shadow trades against the bars now available.

    A non-immediate shadow is a hypothetical buy-stop at ``entry``: it
    triggers on the first later bar whose high reaches it without opening
    more than ``breakout.max_gap_pct`` above the pivot, within
    ``learning.shadow_max_days`` bars, else it *expires* (never triggered).
    From the fill the simulation is deliberately simple: stopped at the
    original stop distance (gap-downs fill at the open), or full exit at the
    partial target, or exit at the close after ``shadow_hold_days`` bars.
    It is a counterfactual, not a P&L. Returns the number resolved now.
    """
    from .shadow_quality import quarantine_invalid
    quarantine_invalid(state.shadow, asof)
    ln = cfg.learning
    resolved = 0
    asof_ts = pd.Timestamp(asof)
    for s in state.shadow:
        if s.get("status") != "open":
            continue
        df = data.get(s["symbol"])
        if df is None or len(df) == 0:
            continue
        start = pd.Timestamp(s["date"])
        after = df.loc[(df.index > start) & (df.index <= asof_ts)]
        if after.empty:
            continue
        entry, stop = float(s["entry"]), float(s["stop"])
        risk = entry - stop  # invalid/non-positive risk was quarantined above
        pivot = float(s.get("pivot") or entry)
        target = float(s["target"]) if s.get("target") else None
        if s.get("immediate"):
            fill, fill_pos, triggered_on = entry, 0, s["date"]
            path = after
        else:
            fill_pos = None
            for i, (ts, bar) in enumerate(after.iterrows()):
                if i >= ln.shadow_max_days:
                    break
                if float(bar["high"]) >= entry and float(bar["open"]) <= pivot * (1 + cfg.breakout.max_gap_pct):
                    fill_pos, fill, triggered_on = i, max(float(bar["open"]), entry), str(ts.date())
                    break
            if fill_pos is None:
                if len(after) >= ln.shadow_max_days:
                    s.update(status="expired", note=f"never triggered within {ln.shadow_max_days} sessions", resolved_on=asof)
                    resolved += 1
                continue
            path = after.iloc[fill_pos:]
        stop_px = fill - risk
        target_px = fill + (target - entry) if target else None
        mfe = 0.0
        exit_px = exit_reason = exited_on = None
        held_bars = 0
        for i, (ts, bar) in enumerate(path.iterrows()):
            hi, lo, op, cl = (float(bar[c]) for c in ("high", "low", "open", "close"))
            mfe = max(mfe, (hi - fill) / risk)
            held_bars = i + 1
            if lo <= stop_px:
                exit_px, exit_reason, exited_on = min(op, stop_px), "stop", str(ts.date())
                break
            if target_px is not None and hi >= target_px:
                exit_px, exit_reason, exited_on = target_px, "target", str(ts.date())
                break
            if held_bars >= ln.shadow_hold_days:
                exit_px, exit_reason, exited_on = cl, "time", str(ts.date())
                break
        if exit_px is None:
            s["mfe_r"] = round(mfe, 3)
            continue
        s.update(
            status="closed", triggered_on=triggered_on, fill=round(fill, 4), exit_price=round(exit_px, 4), exit_reason=exit_reason, exited_on=exited_on,
            r_multiple=round((exit_px - fill) / risk, 3), hold_days=held_bars, mfe_r=round(mfe, 3), resolved_on=asof,
        )
        # The same rule-based post-mortem a real trade gets: *why* the trade
        # we did not take would have worked or failed, not just that it did.
        s["post_mortem"] = explain_trade({**s, "mae_r": None})
        resolved += 1
    return resolved


def gate_of(reason: str) -> str:
    r = reason.lower()
    if "wick" in r or "below the pivot" in r:
        return "hold_above_pivot"
    if "opening range" in r or "first" in r and "minutes" in r:
        return "opening_range"
    if "extended" in r or "chasing" in r:
        return "extension"
    if "volume" in r:
        return "volume"
    if "portfolio full" in r:
        return "portfolio_full"
    return "other"


# --------------------------------------------------------------------------- #
# 3. The review
# --------------------------------------------------------------------------- #
def _bucket(name: str, f: dict, rec: dict) -> str:
    v = f.get(name)
    num = _f(v)
    if name in ("setup", "entry_mode", "source", "reviewer_action", "committee_verdict", "theme"):
        return str(v) if v else "n/a"
    if name == "regime_ok":
        return "risk-on" if v else "risk-off" if v is False else "n/a"
    if name == "rvol_projected":
        return "projected pace" if v else "full-day print"
    if name == "entry_time":
        m = _minutes_since_open(v)
        if m is None:
            return "after the close (next open)"
        return "09:30-09:59" if m < 30 else "10:00-10:59" if m < 90 else "11:00-13:59" if m < 270 else "14:00-close"
    if name == "exit_reason":
        return str(rec.get("exit_reason") or "n/a")
    if num is None:
        return "n/a"
    edges = {
        "rvol": [(1.2, "< 1.2x"), (1.5, "1.2-1.5x"), (2.0, "1.5-2x"), (3.0, "2-3x"), (math.inf, ">= 3x")],
        "theme_pct": [(0.3, "< 30th pct"), (0.6, "30-60th"), (0.8, "60-80th"), (math.inf, ">= 80th pct")],
        "edge_score": [(0.0, "negative"), (0.15, "0 to 0.15"), (0.3, "0.15-0.3"), (math.inf, ">= 0.3")],
        "adr_pct": [(4.0, "< 4%"), (6.0, "4-6%"), (8.0, "6-8%"), (math.inf, ">= 8%")],
        "depth": [(0.15, "<= 15% (tight)"), (0.25, "15-25%"), (0.35, "25-35%"), (math.inf, ">= 35% (deep)")],
        "gap_pct": [(0.01, "0-1% over pivot"), (0.03, "1-3%"), (math.inf, ">= 3% (extended)")],
        "flag_days": [(10, "< 10 days"), (20, "10-20 days"), (35, "20-35 days"), (math.inf, "> 35 days")],
        "context_score": [(-0.2, "< -0.2"), (0.2, "-0.2 to 0.2"), (math.inf, "> 0.2")],
        "stop_pct": [(0.03, "< 3%"), (0.05, "3-5%"), (0.08, "5-8%"), (math.inf, ">= 8%")],
    }.get(name)
    if edges is None:
        return str(v)
    for hi, label in edges:
        if num < hi:
            return label
    return "n/a"


BUCKET_FEATURES = (
    ("setup", "Setup"),
    ("entry_mode", "Entry mode"),
    ("source", "Where the idea came from"),
    ("entry_time", "Time of entry"),
    ("rvol", "Relative volume at entry"),
    ("rvol_projected", "Volume measured or projected"),
    ("theme_pct", "Theme strength"),
    ("edge_score", "Unusual Whales edge"),
    ("adr_pct", "ADR"),
    ("depth", "Flag depth"),
    ("gap_pct", "Entry vs pivot"),
    ("flag_days", "Flag length"),
    ("stop_pct", "Stop distance"),
    ("reviewer_action", "LLM reviewer verdict"),
    ("regime_ok", "Market regime"),
    ("exit_reason", "How it ended"),
)


def _load_overrides(path: Path) -> dict:
    if not path.exists():
        return {"overrides": {}, "history": []}
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return {"overrides": {}, "history": []}
    if not isinstance(raw, dict):
        return {"overrides": {}, "history": []}
    if not isinstance(raw.get("overrides", {}), dict):
        raw["overrides"] = {}
    if not isinstance(raw.get("history", []), list):
        raw["history"] = []
    raw.setdefault("overrides", {})
    raw.setdefault("history", [])
    return raw


def load_overrides(state_dir: Path) -> dict[str, float]:
    """The learned knob values (dotted key -> value) to layer on the settings."""
    raw = _load_overrides(Path(state_dir) / OVERRIDES_FILE).get("overrides") or {}
    return {key: float(value) for key, value in raw.items()
            if key in KNOB_BY_KEY and _f(value) is not None
            and KNOB_BY_KEY[key].lo <= float(value) <= KNOB_BY_KEY[key].hi}


def reset_overrides(state_dir: Path) -> bool:
    path = Path(state_dir) / OVERRIDES_FILE
    if path.exists():
        path.unlink()
        return True
    return False


def load_report(state_dir: Path) -> dict | None:
    path = Path(state_dir) / REPORT_FILE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def review(
    state: "TraderState",
    cfg: StrategyConfig,
    state_dir: Path,
    now: datetime | None = None,
    apply: bool | None = None,
    ai: bool | None = None,
    registry: Any = None,
) -> dict:
    """Weekly learning review. Writes ``learning_report.json`` and, when
    adjustments are applied, ``learning_overrides.yaml``. Returns the report."""
    ln = cfg.learning
    now = _naive_ny(now or datetime.now().astimezone())
    apply = ln.auto_apply if apply is None else apply
    requested_apply = apply
    state_dir = Path(state_dir)
    from .shadow_quality import quarantine_invalid
    quarantine_invalid(state.shadow, str(pd.Timestamp(now).date()))
    closed = [r for r in state.closed if _f(r.get("r_multiple")) is not None]
    for rec in closed:
        if not rec.get("post_mortem"):
            rec["post_mortem"] = explain_trade(rec)  # journal entries written before the learning layer
    rs = [float(r["r_multiple"]) for r in closed]
    overall = stats(rs)
    report: dict[str, Any] = {
        "generated_at": pd.Timestamp(now).isoformat(),
        "enabled": ln.enabled,
        "trades": len(closed),
        "min_trades": ln.min_trades,
        "min_lift_r": ln.min_lift_r,
        "overall": overall,
        "status": "ok" if len(closed) >= ln.min_trades else "insufficient",
        "buckets": [],
        "lessons": [],
        "tags": {},
        "filters": {},
        "gates": {},
        "shadows": {},
        "adjustments": [],
        "scorecards": [],
        "reverted": [],
        "active_overrides": {},
        "post_mortems": [],
        "objective": _objective(closed, [], now),
        "ai": {"enabled": bool(ln.llm_enabled), "analysed": 0, "error": None},
        "data_gaps": [],
    }
    if not ln.enabled:
        report["data_gaps"].append("learning disabled in settings")
        _write_report(state_dir, report)
        return report

    avg_all = overall["avg_r"] if overall["avg_r"] is not None else 0.0

    # Buckets and lessons from real trades.
    for name, label in BUCKET_FEATURES:
        groups: dict[str, list[float]] = {}
        for rec in closed:
            groups.setdefault(_bucket(name, rec.get("features") or {}, rec), []).append(float(rec["r_multiple"]))
        rows = []
        for b, vals in groups.items():
            st = stats(vals)
            st.update(bucket=b, lift=shrunk_lift(st["avg_r"], avg_all, st["n"]))
            rows.append(st)
        rows.sort(key=lambda r: r["lift"], reverse=True)
        if rows and len(closed) > 0:
            report["buckets"].append({"feature": name, "label": label, "rows": rows})
        for row in rows:
            if row["n"] >= 3 and abs(row["lift"]) >= ln.min_lift_r and name != "exit_reason":
                good = row["lift"] > 0
                report["lessons"].append(
                    {
                        "kind": "worked" if good else "didnt",
                        "feature": name,
                        "bucket": row["bucket"],
                        "n": row["n"],
                        "avg_r": row["avg_r"],
                        "lift": row["lift"],
                        "text": (
                            f"{label} = {row['bucket']}: {row['n']} trades averaged {row['avg_r']:+.2f}R "
                            f"({row['lift']:+.2f}R {'better' if good else 'worse'} than the book's {avg_all:+.2f}R, small-sample adjusted)."
                        ),
                    }
                )
    report["lessons"].sort(key=lambda l: abs(l["lift"]), reverse=True)

    # Tags across post-mortems.
    tag_rs: dict[str, list[float]] = {}
    for rec in closed:
        for t in (rec.get("post_mortem") or {}).get("tags", []):
            tag_rs.setdefault(t, []).append(float(rec["r_multiple"]))
    report["tags"] = {t: stats(v) for t, v in sorted(tag_rs.items(), key=lambda kv: -len(kv[1]))}

    # Shadows: what the filters and gates blocked, and how those would have done.
    shadows = state.shadow
    by_status: dict[str, int] = {}
    by_kind: dict[str, dict[str, Any]] = {}
    for s in shadows:
        by_status[s.get("status", "open")] = by_status.get(s.get("status", "open"), 0) + 1
        k = by_kind.setdefault(s.get("kind", "?"), {"n": 0, "closed": [], "expired": 0})
        k["n"] += 1
        if s.get("status") == "closed" and _f(s.get("r_multiple")) is not None:
            k["closed"].append(float(s["r_multiple"]))
        elif s.get("status") == "expired":
            k["expired"] += 1
    report["shadows"] = {
        "total": len(shadows),
        "by_status": by_status,
        "by_kind": {k: {"n": v["n"], "expired": v["expired"], **stats(v["closed"])} for k, v in by_kind.items()},
        "note": "Counterfactuals on real bars (simple stop / target / time-out fill model), not P&L.",
        "missed": _notable_shadows(shadows, best=True),
        "avoided": _notable_shadows(shadows, best=False),
    }
    report["objective"] = _objective(closed, shadows, now)
    filters: dict[str, dict[str, Any]] = {}
    for s in shadows:
        if s.get("kind") == "near_miss":
            # Detector-level thresholds: attributed to the knob key itself.
            for key in s.get("reasons") or []:
                fl = filters.setdefault(key, {"blocked": 0, "sole_blocker": 0, "resolved": [], "expired": 0, "sole_resolved": []})
                fl["blocked"] += 1
                sole = len(s.get("reasons") or []) == 1
                if sole:
                    fl["sole_blocker"] += 1
                if s.get("status") == "closed" and _f(s.get("r_multiple")) is not None:
                    fl["resolved"].append(float(s["r_multiple"]))
                    if sole:
                        fl["sole_resolved"].append(float(s["r_multiple"]))
                elif s.get("status") == "expired":
                    fl["expired"] += 1
            continue
        if s.get("kind") != "rejected":
            continue
        for chk in s.get("failed_checks") or []:
            fl = filters.setdefault(chk, {"blocked": 0, "sole_blocker": 0, "resolved": [], "expired": 0, "sole_resolved": []})
            fl["blocked"] += 1
            sole = len(s.get("failed_checks") or []) == 1
            if sole:
                fl["sole_blocker"] += 1
            if s.get("status") == "closed" and _f(s.get("r_multiple")) is not None:
                fl["resolved"].append(float(s["r_multiple"]))
                if sole:
                    fl["sole_resolved"].append(float(s["r_multiple"]))
            elif s.get("status") == "expired":
                fl["expired"] += 1
    for chk, fl in filters.items():
        st = stats(fl["resolved"])
        sole = stats(fl["sole_resolved"])
        verdict = "not enough resolved shadows yet"
        if sole["n"] >= max(3, ln.min_trades // 2):
            if sole["avg_r"] >= max(avg_all, 0.0) + ln.min_lift_r:
                verdict = f"costly: the {sole['n']} setups it alone blocked would have averaged {sole['avg_r']:+.2f}R - candidate for loosening"
            elif sole["avg_r"] <= min(avg_all, 0.0) - ln.min_lift_r / 2:
                verdict = f"earning its keep: the {sole['n']} setups it alone blocked averaged {sole['avg_r']:+.2f}R"
            else:
                verdict = f"neutral: blocked setups averaged {sole['avg_r']:+.2f}R"
        report["filters"][chk] = {"blocked": fl["blocked"], "sole_blocker": fl["sole_blocker"], "expired": fl["expired"], "all": st, "sole": sole, "verdict": verdict}
    gates: dict[str, dict[str, Any]] = {}
    for s in shadows:
        if s.get("kind") != "held":
            continue
        names = sorted({gate_of(r) for r in s.get("reasons") or []})
        for g in names:
            gt = gates.setdefault(g, {"held": 0, "sole": 0, "resolved": [], "sole_resolved": [], "expired": 0})
            gt["held"] += 1
            if len(names) == 1:
                gt["sole"] += 1
            if s.get("status") == "closed" and _f(s.get("r_multiple")) is not None:
                gt["resolved"].append(float(s["r_multiple"]))
                if len(names) == 1:
                    gt["sole_resolved"].append(float(s["r_multiple"]))
            elif s.get("status") == "expired":
                gt["expired"] += 1
    for g, gt in gates.items():
        st, sole = stats(gt["resolved"]), stats(gt["sole_resolved"])
        verdict = "not enough resolved shadows yet"
        if sole["n"] >= max(3, ln.min_trades // 2):
            if sole["avg_r"] >= max(avg_all, 0.0) + ln.min_lift_r:
                verdict = f"costly: {sole['n']} confirmed-later setups it alone held back would have averaged {sole['avg_r']:+.2f}R"
            elif sole["avg_r"] <= min(avg_all, 0.0) - ln.min_lift_r / 2:
                verdict = f"protecting us: setups it held back averaged {sole['avg_r']:+.2f}R"
            else:
                verdict = f"neutral: held-back setups averaged {sole['avg_r']:+.2f}R"
        report["gates"][g] = {"held": gt["held"], "sole": gt["sole"], "expired": gt["expired"], "all": st, "sole_only": sole, "verdict": verdict}
    for chk, fl in report["filters"].items():
        if fl["verdict"].startswith("costly") or fl["verdict"].startswith("earning"):
            what = f"Detector threshold '{KNOB_BY_KEY[chk].label}' (near misses)" if chk in KNOB_BY_KEY else f"Check '{chk}'"
            report["lessons"].append({"kind": "filter", "feature": chk, "bucket": "shadow trades", "n": fl["sole"]["n"], "avg_r": fl["sole"]["avg_r"], "lift": 0.0, "text": f"{what} is {fl['verdict']}."})
    for g, gt in report["gates"].items():
        if gt["verdict"].startswith("costly") or gt["verdict"].startswith("protecting"):
            report["lessons"].append({"kind": "gate", "feature": g, "bucket": "held setups", "n": gt["sole_only"]["n"], "avg_r": gt["sole_only"]["avg_r"], "lift": 0.0, "text": f"Entry gate '{g}' is {gt['verdict']}."})

    # Scorecards: did the adjustments already in force earn their keep?
    existing = _load_overrides(state_dir / OVERRIDES_FILE)
    scorecards = score_adjustments(closed, shadows, cfg, existing, now)
    verified = [r for r in closed if r.get("evidence") == "broker_verified"]
    evidence_note = "Paper fills, estimated exits, legacy records and bar-based shadows are exploratory. They cannot authorize automatic changes."
    report["evidence"] = {"verified": len(verified), "other": len(closed) - len(verified), "note": evidence_note,
                          "interval": mean_interval([float(r["r_multiple"]) for r in verified])}
    # Reversions based on the simplified shadow model remain proposals.
    apply = False
    report["scorecards"] = scorecards
    reverted = [sc for sc in scorecards if sc["verdict"] == "hurting"]
    report["proposed_only"] = not apply
    report["proposed_reversions"] = reverted

    # Knob adjustments (bounded, one step, evidence attached).
    adjustments = propose_adjustments(closed, shadows, cfg, existing, now)
    report["adjustments"] = adjustments
    # Auto-application only considers one conservative tightening supported by
    # fresh, verified executions and a negative upper confidence bound.
    approved = propose_adjustments(verified, [], cfg, existing, now) if requested_apply else []
    approved = [a for a in approved if a["direction"] == "tighten" and a["n"] >= max(30, ln.min_trades)]
    eligible = []
    for adj in approved:
        knob = KNOB_BY_KEY[adj["key"]]
        cutoff = max((stamp for h in existing.get("history", []) if h.get("key") == knob.key and (stamp := _entry_ts({"date": h.get("date")})) is not None), default=None)
        vals = [float(r["r_multiple"]) for r in verified
                if (cutoff is None or (_entry_ts(r) is not None and _entry_ts(r) > cutoff))
                and (v := feature_value(r.get("features"), knob.feature)) is not None
                and knob.in_marginal_band(v, config_value(cfg, knob.key))]
        interval = mean_interval(vals)
        if interval and len(vals) >= max(30, ln.min_trades) and interval[1] < -ln.min_lift_r:
            eligible.append(adj)
    adjustments = eligible[:1]
    reverted = []
    apply = bool(adjustments)
    report["applied"] = adjustments
    report["proposed_only"] = not apply
    if apply and (adjustments or reverted):
        for adj in adjustments:
            existing["overrides"][adj["key"]] = adj["to"]
            existing["history"].append({k: adj[k] for k in ("key", "from", "to", "direction", "reason", "n", "avg_r", "date")})
        existing["generated_at"] = pd.Timestamp(now).isoformat()
        existing["note"] = "Written by the qmag learning review. Values are layered on top of settings.yaml; delete this file or use the learning page to reset."
        atomic_text(state_dir / OVERRIDES_FILE, yaml.safe_dump(existing, sort_keys=False))
    report["active_overrides"] = {
        k: {"value": v, "label": KNOB_BY_KEY[k].label if k in KNOB_BY_KEY else k, "history": [h for h in existing.get("history", []) if h.get("key") == k]}
        for k, v in (existing.get("overrides") or {}).items()
    }

    # AI post-mortems (optional; never required).
    want_ai = ln.llm_enabled if ai is None else ai
    if want_ai and closed:
        report["ai"] = ai_post_mortems(state.closed, cfg, registry)
    else:
        report["ai"] = {"enabled": bool(want_ai), "analysed": 0, "error": None, "provider": None if not want_ai else llm_available(cfg)[1]}
    if report["ai"].get("error"):
        report["data_gaps"].append(f"AI post-mortems unavailable: {report['ai']['error']}")

    report["post_mortems"] = [
        {
            "symbol": r.get("symbol"), "setup": r.get("setup"), "entry_date": r.get("entry_date"), "closed_on": r.get("closed_on"), "exit_reason": r.get("exit_reason"),
            "r_multiple": r.get("r_multiple"), "hold_days": r.get("hold_days"), "mfe_r": r.get("mfe_r"), "mae_r": r.get("mae_r"),
            "entry_mode": (r.get("features") or {}).get("entry_mode"), "post_mortem": r.get("post_mortem"), "post_mortem_ai": r.get("post_mortem_ai"),
        }
        for r in reversed(closed[-25:])
    ]
    if report["status"] == "insufficient":
        report["data_gaps"].append(f"only {len(closed)} closed trades; at least {ln.min_trades} are needed before any knob is moved")
    if registry is not None:
        reverts = f", {len(reverted)} reverted" if reverted else ""
        registry.record(
            "learning", True, detail=f"review over {len(closed)} trades, {len(shadows)} shadows; {len(adjustments)} adjustment(s) {'applied' if apply else 'proposed'}{reverts}",
            items=len(closed), degraded=report["status"] == "insufficient",
        )
    _write_report(state_dir, report)
    return report


def _naive_ny(value) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_convert("America/New_York").tz_localize(None) if stamp.tzinfo else stamp


def mean_interval(values: list[float]) -> list[float] | None:
    """Descriptive 95% normal interval; correlated trades weaken its coverage.

    This is not an out-of-sample performance guarantee or a causal estimate.
    """
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    se = (sum((v - mean) ** 2 for v in values) / (len(values) - 1) / len(values)) ** .5
    return [round(mean - 1.96 * se, 4), round(mean + 1.96 * se, 4)]


def _entry_ts(rec: dict) -> pd.Timestamp | None:
    try:
        value = rec.get("entry_date") or rec.get("date")
        return _naive_ny(value) if value else None
    except (ValueError, TypeError):
        return None


def _objective(closed: list[dict], shadows: list[dict], now: datetime) -> dict:
    """What the loop is actually trying to maximise: total R per week, net of
    what the filters left on the table. Averages alone reward over-filtering."""
    rs = [float(r["r_multiple"]) for r in closed if _f(r.get("r_multiple")) is not None]
    dates = [d for d in (_entry_ts(r) for r in closed) if d is not None]
    weeks = None
    if dates:
        weeks = max((_naive_ny(now).normalize() - min(dates).normalize()).days / 7.0, 1.0)
    closed_sh = [s for s in shadows if s.get("status") == "closed" and _f(s.get("r_multiple")) is not None]
    missed = [float(s["r_multiple"]) for s in closed_sh if s.get("kind") in ("rejected", "held", "near_miss")]
    return {
        "total_r": round(sum(rs), 2),
        "trades": len(rs),
        "weeks": round(weeks, 1) if weeks else None,
        "r_per_week": round(sum(rs) / weeks, 3) if weeks else None,
        "trades_per_week": round(len(rs) / weeks, 2) if weeks else None,
        "filtered_total_r": round(sum(missed), 2) if missed else 0.0,  # what the blocked setups would have made (or lost), in total
        "filtered_n": len(missed),
        "note": "Total R per week is the target. Tightening only pays when the trades it removes lose money; loosening only when the setups it admits make money.",
    }


def _notable_shadows(shadows: list[dict], best: bool, limit: int = 8) -> list[dict]:
    """Best trades we did not take (``best``) or worst ones we dodged, with the reason and the post-mortem."""
    closed = [s for s in shadows if s.get("status") == "closed" and _f(s.get("r_multiple")) is not None and s.get("kind") in ("rejected", "held", "near_miss", "no_slot")]
    closed.sort(key=lambda s: float(s["r_multiple"]), reverse=best)
    out = []
    for s in closed[:limit]:
        r = float(s["r_multiple"])
        if (best and r <= 0) or (not best and r >= 0):
            break
        blocker = s.get("failed_checks") or s.get("reasons") or []
        out.append(
            {
                "symbol": s.get("symbol"), "kind": s.get("kind"), "date": s.get("date"), "r_multiple": round(r, 2), "exit_reason": s.get("exit_reason"),
                "blocked_by": [str(b)[:80] for b in blocker][:3], "lesson": ((s.get("post_mortem") or {}).get("lesson")) or "",
            }
        )
    return out


def score_adjustments(closed: list[dict], shadows: list[dict], cfg: StrategyConfig, existing: dict, now: datetime) -> list[dict]:
    """One scorecard per learned override in force: what happened *after* it.

    * A **tightening** is judged by the setups it has excluded since - the
      resolved shadows this knob alone blocked whose feature value sits
      between the old and the new setting. If they would have made money
      (``min_lift_r`` above the book, ``floor_n`` of them) the tightening is
      *hurting* and is reverted; if they lost, it is *helping*.
    * A **loosening** is judged by the real trades it admitted - those
      entered since with a feature value between the old and new setting -
      against the rest of the book since the change.
    Anything with less evidence than ``floor_n`` is *pending*.
    """
    ln = cfg.learning
    floor_n = max(3, ln.min_trades // 2)
    history = existing.get("history") or []
    out: list[dict] = []
    for key, value in (existing.get("overrides") or {}).items():
        knob = KNOB_BY_KEY.get(key)
        entries = [h for h in history if h.get("key") == key]
        if knob is None or not entries:
            continue
        adj = entries[-1]
        try:
            since, old, new = pd.Timestamp(adj["date"]), float(adj["from"]), float(adj["to"])
        except (KeyError, ValueError, TypeError):
            continue
        direction = adj.get("direction")
        after = [r for r in closed if (_entry_ts(r) or pd.Timestamp.min) > since]
        rs_after = [float(r["r_multiple"]) for r in after]
        card: dict[str, Any] = {
            "key": key, "label": knob.label, "direction": direction, "from": old, "to": new, "date": adj.get("date"),
            "days_active": int((_naive_ny(now).normalize() - since.normalize()).days), "real_after": stats(rs_after), "verdict": "pending", "note": "",
        }
        if direction == "revert":
            card["verdict"] = "reverted"
            card["note"] = "reverted; the knob rests before it can move again"
            card["evidence"] = stats([])
            out.append(card)
            continue
        if direction == "tighten":
            evidence = []
            for s in shadows:
                if s.get("status") != "closed" or _f(s.get("r_multiple")) is None or not solely_blocked_by(knob, s):
                    continue
                if (_entry_ts(s) or pd.Timestamp.min) <= since:
                    continue
                v = feature_value(s.get("features"), knob.feature)
                if v is not None and knob.between(v, old, new):
                    evidence.append(float(s["r_multiple"]))
            ev = stats(evidence)
            card["evidence"] = ev
            card["evidence_label"] = "setups the tightening has kept out since"
            base = sum(rs_after) / len(rs_after) if rs_after else 0.0
            if ev["n"] >= floor_n:
                if ev["avg_r"] >= max(base, 0.0) + ln.min_lift_r:
                    card["verdict"] = "hurting"
                    card["note"] = f"the {ev['n']} setups it kept out would have averaged {ev['avg_r']:+.2f}R ({ev['total_r']:+.1f}R in total) against {base:+.2f}R for the book since"
                elif ev["avg_r"] <= min(base, 0.0) - ln.min_lift_r / 2:
                    card["verdict"] = "helping"
                    card["note"] = f"the {ev['n']} setups it kept out averaged {ev['avg_r']:+.2f}R ({ev['total_r']:+.1f}R avoided)"
                else:
                    card["verdict"] = "neutral"
                    card["note"] = f"the {ev['n']} setups it kept out averaged {ev['avg_r']:+.2f}R"
            else:
                card["note"] = f"{ev['n']} of {floor_n} resolved shadows needed to judge it"
        else:  # loosen
            admitted, rest = [], []
            for r in after:
                v = feature_value(r.get("features"), knob.feature)
                if v is None:
                    continue
                (admitted if knob.between(v, old, new) else rest).append(float(r["r_multiple"]))
            ev = stats(admitted)
            card["evidence"] = ev
            card["evidence_label"] = "real trades the loosening admitted"
            base = sum(rest) / len(rest) if rest else 0.0
            if ev["n"] >= floor_n:
                if ev["avg_r"] <= min(base, 0.0) - ln.min_lift_r:
                    card["verdict"] = "hurting"
                    card["note"] = f"the {ev['n']} trades it admitted averaged {ev['avg_r']:+.2f}R ({ev['total_r']:+.1f}R in total) against {base:+.2f}R for the rest of the book since"
                elif ev["avg_r"] >= max(base, 0.0) + ln.min_lift_r:
                    card["verdict"] = "helping"
                    card["note"] = f"the {ev['n']} trades it admitted averaged {ev['avg_r']:+.2f}R ({ev['total_r']:+.1f}R earned)"
                else:
                    card["verdict"] = "neutral"
                    card["note"] = f"the {ev['n']} trades it admitted averaged {ev['avg_r']:+.2f}R"
            else:
                card["note"] = f"{ev['n']} of {floor_n} admitted trades needed to judge it"
        out.append(card)
    return out


def _write_report(state_dir: Path, report: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    atomic_json(state_dir / REPORT_FILE, report)


def propose_adjustments(closed: list[dict], shadows: list[dict], cfg: StrategyConfig, existing: dict, now: datetime) -> list[dict]:
    """One bounded step per knob, only where the evidence clears the bar.

    * **Tighten** when the trades that only just cleared the knob (its
      marginal band, one step wide) *lost money* on average and
      underperformed the rest of the book by ``min_lift_r`` R, with at least
      ``max(3, min_trades // 2)`` trades in the band and ``min_trades``
      trades overall. Removing trades that merely earn less than the rest
      would raise the average and lower the total - so it is not done.
    * **Loosen** when the resolved shadow trades that the knob's check, gate
      or detector threshold *alone* blocked would have beaten the book by
      ``min_lift_r`` R, with the same sample floor.
    A knob changed within ``COOLDOWN_DAYS`` is left alone; one whose last
    change was a revert rests for ``REVERT_COOLDOWN_DAYS``.
    """
    ln = cfg.learning
    floor_n = max(3, ln.min_trades // 2)
    rs = [float(r["r_multiple"]) for r in closed]
    avg_all = sum(rs) / len(rs) if rs else 0.0
    today = _naive_ny(now).normalize()
    last_change: dict[str, pd.Timestamp] = {}
    last_direction: dict[str, str] = {}
    for h in existing.get("history", []):
        try:
            ts = pd.Timestamp(h["date"])
        except (KeyError, ValueError, TypeError):
            continue
        if ts >= last_change.get(h["key"], pd.Timestamp.min):
            last_change[h["key"]], last_direction[h["key"]] = ts, str(h.get("direction") or "")
    out: list[dict] = []
    for knob in KNOBS:
        current = config_value(cfg, knob.key)
        if knob.key in last_change:
            rest_days = REVERT_COOLDOWN_DAYS if last_direction.get(knob.key) == "revert" else COOLDOWN_DAYS
            if (today - last_change[knob.key].normalize()).days < rest_days:
                continue
        # Tighten from real trades.
        if len(rs) >= ln.min_trades:
            band, rest = [], []
            for rec in closed:
                if knob.key in last_change and (_entry_ts(rec) is None or _entry_ts(rec) <= last_change[knob.key]):
                    continue
                v = feature_value(rec.get("features"), knob.feature)
                if v is None:
                    continue
                (band if knob.in_marginal_band(v, current) else rest).append(float(rec["r_multiple"]))
            if len(band) >= floor_n and rest:
                avg_band, avg_rest = sum(band) / len(band), sum(rest) / len(rest)
                new = knob.tighter(current)
                # Total-R rule: only remove a band that loses money.
                if avg_band <= min(avg_rest - ln.min_lift_r, 0.0) and new != current:
                    out.append(
                        {
                            "key": knob.key, "label": knob.label, "from": current, "to": new, "direction": "tighten", "n": len(band), "avg_r": round(avg_band, 3),
                            "date": str(today.date()),
                            "reason": (
                                f"{len(band)} trades that only just cleared {knob.label} ({knob.fmt.format(current)} to {knob.fmt.format(current + knob.step if knob.kind == 'floor' else current - knob.step)}) "
                                f"lost {avg_band:+.2f}R on average ({sum(band):+.1f}R in total) vs {avg_rest:+.2f}R for the other {len(rest)}; moving it to {knob.fmt.format(new)}"
                            ),
                        }
                    )
                    continue
        # Loosen from shadows the knob alone blocked (check, gate or detector threshold).
        blocked: list[float] = []
        for s in shadows:
            if s.get("status") != "closed" or _f(s.get("r_multiple")) is None:
                continue
            if knob.key in last_change and (_entry_ts(s) is None or _entry_ts(s) <= last_change[knob.key]):
                continue
            v = feature_value(s.get("features"), knob.feature)
            if solely_blocked_by(knob, s) and v is not None and knob.between(v, current, knob.looser(current)):
                blocked.append(float(s["r_multiple"]))
        if len(blocked) >= floor_n:
            avg_b = sum(blocked) / len(blocked)
            new = knob.looser(current)
            if avg_b >= max(avg_all, 0.0) + ln.min_lift_r and new != current:
                out.append(
                    {
                        "key": knob.key, "label": knob.label, "from": current, "to": new, "direction": "loosen", "n": len(blocked), "avg_r": round(avg_b, 3),
                        "date": str(today.date()),
                        "reason": (
                            f"{len(blocked)} shadow trades blocked only by {knob.label} would have averaged {avg_b:+.2f}R ({sum(blocked):+.1f}R in total; "
                            f"book: {avg_all:+.2f}R); moving it from {knob.fmt.format(current)} to {knob.fmt.format(new)}"
                        ),
                    }
                )
    return out
