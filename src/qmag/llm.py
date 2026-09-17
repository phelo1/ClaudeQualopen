"""Optional three-seat LLM "analyst committee" (off by default).

Borrowed from the TradingAgents pattern (bull researcher vs bear researcher,
then a risk reviewer) and made a real debate: three *separate* model calls,
each seat with its own provider / model / endpoint (``committee.*``).

1. The bull researcher argues for the trade.
2. The bear researcher argues against it - and, with ``debate`` on, reads the
   bull case first and rebuts it.
3. The risk chair reads the facts and both cases and rules: take, reduce
   (with a size multiplier) or reject.

Every seat gets the *same* facts the engine saw (plan, checklist,
deterministic rationale, gathered context) and never anything else; a seat
that cannot answer is reported as an error, never filled in. By default the
verdict is advisory; ``committee.can_veto`` lets a reject block the trade and
a reduce scale it. Works with Gemini and any OpenAI-compatible endpoint
through the reviewer transport (``reviewer.ask_json``).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

from .config import CommitteeSettings
from .redact import describe_error

log = logging.getLogger(__name__)

SEATS = ("bull", "bear", "risk")
SEAT_LABELS = {"bull": "Bull researcher", "bear": "Bear researcher", "risk": "Risk chair"}

_STYLE = (
    "The desk trades swing setups in the style of Kristjan Kullamägi (momentum breakouts from tight flags and "
    "episodic pivots, stops under the low of the day, partial profits into strength, moving-average trails). "
    "You receive the sized plan, its pass/fail checklist, the deterministic rationale the engine wrote and the "
    "external context it gathered (news, social, options flow, events, fundamentals). Use ONLY the facts given; "
    "do not invent prices, news, dates or numbers. If a fact is missing, say it is missing. Reply with strict JSON."
)

BULL_SYSTEM = (
    "You are the BULL researcher on a three-person trading committee. " + _STYLE +
    ' Make the strongest honest case FOR taking this trade: {"case": str (under 110 words), "key_points": [str, up to 4], '
    '"confidence": number 0-1 that the trade reaches its target before its stop}. Do not manufacture enthusiasm: '
    "if the facts are thin, say so and keep confidence low."
)
BEAR_SYSTEM = (
    "You are the BEAR researcher on a three-person trading committee. " + _STYLE +
    " Make the strongest honest case AGAINST taking this trade. When a bull case is included, rebut it point by point "
    'where the facts allow and concede where they do not: {"case": str (under 110 words), "key_points": [str, up to 4], '
    '"confidence": number 0-1 that the trade FAILS (stops out or goes nowhere)}.'
)
RISK_SYSTEM = (
    "You are the RISK CHAIR of a three-person trading committee. " + _STYLE +
    " You have the bull case and the bear case (each may be marked unavailable). Weigh them against the plan's own "
    'numbers - risk per share, reward:risk, position value, checks that failed - and rule: {"verdict": "take"|"reduce"|"reject", '
    '"size_multiplier": number 0-1 (1 for take, the fraction of planned size for reduce, 0 for reject), '
    '"confidence": number 0-1 in your ruling, "risk_review": str (under 90 words: the decisive reasons), '
    '"decisive_factor": str (one line: what settled it)}. Reduce means take the trade at size_multiplier of the planned size.'
)

_CASE_SCHEMA = {
    "type": "object",
    "properties": {
        "case": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
    },
    "required": ["case", "key_points", "confidence"],
}
_RULING_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["take", "reduce", "reject"]},
        "size_multiplier": {"type": "number"},
        "confidence": {"type": "number"},
        "risk_review": {"type": "string"},
        "decisive_factor": {"type": "string"},
    },
    "required": ["verdict", "size_multiplier", "confidence", "risk_review", "decisive_factor"],
}


def seat_config(cfg: CommitteeSettings, seat: str) -> SimpleNamespace:
    """The provider / model / base_url / timeout of one seat, in the shape ``reviewer.ask_json`` reads."""
    return SimpleNamespace(
        provider=getattr(cfg, f"{seat}_provider"),
        model=getattr(cfg, f"{seat}_model"),
        base_url=getattr(cfg, f"{seat}_base_url"),
        timeout_seconds=cfg.timeout_seconds,
    )


def _facts(plan_dict: dict[str, Any], rationale: dict[str, str], context: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "plan": {k: v for k, v in plan_dict.items() if k not in ("rationale", "context", "notes", "committee", "reviewer", "chart")},
        "rationale": rationale,
        "context": _trim_context(context) if context else None,
    }


def _clamp(x: Any, lo: float = 0.0, hi: float = 1.0, default: float = 0.5) -> float:
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return default


def _ask_seat(seat: str, cfg: CommitteeSettings, bundle: dict[str, Any], system: str, schema: dict[str, Any]) -> dict[str, Any]:
    from .reviewer import ask_json, parse_json_object

    sc = seat_config(cfg, seat)
    out: dict[str, Any] = {"seat": seat, "label": SEAT_LABELS[seat]}
    try:
        text, provider, model = ask_json(bundle, sc, system, schema=schema)
        out.update(provider=provider, model=model)
        out.update(parse_json_object(text))
    except Exception as exc:
        from .reviewer import resolve_provider

        try:
            provider, model, _ = resolve_provider(sc)
            out.update(provider=provider, model=model)
        except Exception:
            pass
        out["error"] = describe_error(exc)
        log.warning("LLM committee %s failed: %s", seat, out["error"])
    return out


def committee_review(
    plan_dict: dict[str, Any], rationale: dict[str, str], context: dict[str, Any] | None, cfg: CommitteeSettings,
) -> dict[str, Any]:
    """Run the three seats and return the committee's record.

    Always returns a dict (never None): with a ``verdict`` when the chair
    ruled, or with ``error`` when a seat could not answer. Partial seats are
    kept so the page can show who said what and who was unavailable.
    """
    facts = _facts(plan_dict, rationale, context)
    seats: dict[str, dict[str, Any]] = {}

    seats["bull"] = _ask_seat("bull", cfg, {"facts": facts}, BULL_SYSTEM, _CASE_SCHEMA)
    bull_case = None if "error" in seats["bull"] else {"case": seats["bull"].get("case"), "key_points": seats["bull"].get("key_points"), "confidence": seats["bull"].get("confidence")}

    bear_bundle: dict[str, Any] = {"facts": facts}
    if cfg.debate:
        bear_bundle["bull_case"] = bull_case or "unavailable (the bull researcher could not answer)"
    seats["bear"] = _ask_seat("bear", cfg, bear_bundle, BEAR_SYSTEM, _CASE_SCHEMA)
    bear_case = None if "error" in seats["bear"] else {"case": seats["bear"].get("case"), "key_points": seats["bear"].get("key_points"), "confidence": seats["bear"].get("confidence")}

    seats["risk"] = _ask_seat(
        "risk", cfg,
        {"facts": facts, "bull_case": bull_case or "unavailable", "bear_case": bear_case or "unavailable"},
        RISK_SYSTEM, _RULING_SCHEMA,
    )

    record: dict[str, Any] = {
        "seats": seats,
        "calls": 3,
        "debate": cfg.debate,
        "model": " / ".join(f"{s}: {seats[s].get('model') or '?'}" for s in SEATS),
        "bull_case": (bull_case or {}).get("case"),
        "bear_case": (bear_case or {}).get("case"),
    }
    failed = [s for s in SEATS if "error" in seats[s]]
    if failed:
        record["error"] = "; ".join(f"{SEAT_LABELS[s]}: {seats[s]['error']}" for s in failed)
        return record
    chair = seats["risk"]
    verdict = str(chair.get("verdict", "")).lower()
    if verdict not in ("take", "reduce", "reject"):
        record["error"] = f"Risk chair: verdict '{chair.get('verdict')}' is not take / reduce / reject"
        return record
    record.update(
        verdict=verdict,
        confidence=_clamp(chair.get("confidence")),
        size_multiplier=1.0 if verdict == "take" else 0.0 if verdict == "reject" else _clamp(chair.get("size_multiplier"), default=1.0),
        risk_review=chair.get("risk_review"),
        decisive_factor=chair.get("decisive_factor"),
    )
    return record


def _trim_context(ctx: dict[str, Any]) -> dict[str, Any]:
    slim = {k: v for k, v in ctx.items() if k not in ("headlines", "social_samples", "errors", "available", "fetched_at")}
    slim["headlines"] = [{"when": str(h.get("when", ""))[:10], "title": h.get("title"), "score": h.get("score"), "tags": h.get("tags")} for h in ctx.get("headlines", [])[:8]]
    slim["social_samples"] = ctx.get("social_samples", [])[:3]
    return slim
