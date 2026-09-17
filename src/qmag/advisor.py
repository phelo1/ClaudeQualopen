"""The desk advisor: plain English in, reviewed setting changes out.

You write what you want - "risk half as much until I have twenty trades",
"be stricter about entries in this market", "why did we sit out Tuesday?" -
and the configured model answers with advice and a list of concrete setting
changes (``key``, ``value``, ``why``). It is given:

* the **settings map**: every strategy parameter with its meaning, current
  value, default, allowed values / range and whether the CLI or the learning
  layer currently pins it;
* the **desk**: broker, equity, cash, open positions, recent R multiples,
  regime, kill switch, learned adjustments and the latest lessons;
* the **recent conversation**.

The model never changes anything. Every proposal is validated exactly as a
settings-page submission would be (unknown key, wrong type, out of range ->
rejected with the reason) and is applied only when the operator accepts it
on the advisor page (or with ``qmag advise --apply``). Credentials, the
broker, the account and the live switch live outside the strategy config
and are therefore out of the advisor's reach by construction.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import StrategyConfig
from .redact import describe_error
from .settings import CHOICES, FIELD_HELP, SECTION_LABELS, _coerce, _field_kind, _optional, describe_config, validate_config

if TYPE_CHECKING:  # pragma: no cover
    from .session import TradingSession

log = logging.getLogger(__name__)

ADVISOR_FILE = "advisor.json"
MAX_EXCHANGES_KEPT = 60
FORBIDDEN_PREFIXES = ("advisor.",)  # the advisor does not reconfigure itself

# Limits validate_config enforces, in a form the model can read.
RANGES: dict[str, str] = {
    "risk.risk_per_trade_pct": "0 < x <= 0.1 (fraction of equity; 0.005 = 0.5 %)",
    "risk.max_position_pct": "0 < x <= 1 (fraction of equity)",
    "risk.max_positions": "integer >= 1",
    "risk.max_portfolio_heat_pct": "0 (off) .. 0.5",
    "risk.daily_loss_limit_pct": "0 (off) .. 0.5",
    "risk.max_positions_per_theme": "0 (off) or more",
    "management.stop_adr_mult": "> 0",
    "management.partial_fraction": "0 < x < 1",
    "management.time_stop_days": "0 (off) or more",
    "edge.threshold": "-1 .. +1",
    "edge.min_coverage": "0 .. 1",
    "reviewer.min_confidence": "0 .. 1",
    "insider_scan.min_flag_score": "0 .. 10",
    "entry.confirm_volume_ratio": "0 < x <= 5",
    "entry.opening_range_minutes": "0 .. 120",
    "schedule.focused_interval_minutes": "integer >= 1",
}

SYSTEM_PROMPT = """You are the desk advisor of an automated momentum swing-trading desk run in the style of Kristjan Kullamägi (Qullamaggie): breakouts from tight flags and episodic pivots in momentum leaders, 1R stops, partial profits, moving-average trails, market-regime and theme filters, an optional Unusual Whales edge score, an LLM trade reviewer, a 24/7 tiered scanner and a learning layer.

You receive the operator's message, the recent conversation, the desk's current state and the complete settings map (every parameter with its meaning, current value, default and allowed range). Your job is to give sound, specific advice in plain English and, when the operator wants something changed, to propose the exact setting changes that do it.

Rules:
- Reason ONLY from the settings map and the desk state you are given. Never invent parameters, prices, trades or numbers; a key you propose MUST appear in the settings map exactly as written.
- Propose changes only when the message asks for a change or clearly implies one. For questions, answer them and leave "changes" empty.
- Prefer the smallest set of changes that achieves what was asked. One knob per idea; never change a parameter you cannot explain in one sentence.
- Respect the allowed ranges and choices. Give values in the parameter's own units (fractions such as 0.005 for 0.5 %, integers for counts, true/false for switches, comma-separated for lists, exact choice names for choices).
- Parameters marked "pinned" cannot be changed from here (say so if the operator asks).
- Risk changes must be justified against the desk state (equity, recent R multiples, open positions, regime). If a request is dangerous (for example risking several percent per trade, disabling every filter, switching the reviewer to fail-open while gating), say so plainly, propose the safer version, and put the operator's literal request in "questions" so they can confirm.
- The operator applies changes; you never do. Never claim anything has been changed.
- Reply with ONE JSON object only and exactly these keys:
  reply       plain text for the operator (<= 220 words, no markdown headings)
  changes     array of {"key": str, "value": str, "why": str} - the setting changes you recommend (may be empty). "value" is always a string.
  questions   array of short strings: what you need the operator to confirm or clarify (may be empty)
  risk_note   one sentence on what the proposed changes do to risk (empty string when there are none)
"""

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "reply": {"type": "STRING"},
        "changes": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {"key": {"type": "STRING"}, "value": {"type": "STRING"}, "why": {"type": "STRING"}},
                "required": ["key", "value", "why"],
            },
        },
        "questions": {"type": "ARRAY", "items": {"type": "STRING"}},
        "risk_note": {"type": "STRING"},
    },
    "required": ["reply", "changes", "questions", "risk_note"],
}

EXAMPLES = (
    "I want to risk half as much per trade until I have twenty closed trades, then we can talk again.",
    "Be stricter about entries: only the cleanest breakouts in a strong market.",
    "The insider scan flags too many big names - what would you tighten?",
    "Explain my current risk settings in plain English and tell me what you would change.",
    "We sat out the whole week. Which filter kept us out, and is it right?",
)


# --------------------------------------------------------------------------- #
# What the model sees
# --------------------------------------------------------------------------- #
def settings_catalogue(cfg: StrategyConfig, pinned: dict[str, Any] | None = None, learned: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Every changeable parameter with meaning, value, default and constraints.

    ``pinned`` are CLI overrides (cannot be changed from the dashboard);
    ``learned`` are the learning layer's current adjustments (changing them by
    hand is allowed but pointless while auto_apply is on, so they are marked).
    """
    pinned = pinned or {}
    learned = learned or {}
    out: list[dict[str, Any]] = []
    for section in describe_config(cfg):
        if section["name"] == "advisor":
            continue
        for f in section["fields"]:
            key = f["key"]
            if f["kind"] == "weights":
                for name, w in (f["value"] or {}).items():
                    out.append({
                        "key": f"{key}.{name}", "section": section["label"], "kind": "float", "value": w,
                        "default": (f["default"] or {}).get(name), "help": f"weight of the '{name}' feature in the edge score (0 = ignore)", "range": ">= 0",
                    })
                continue
            row: dict[str, Any] = {
                "key": key, "section": section["label"], "kind": f["kind"], "value": f["value"] if f["value"] != "" else None,
                "default": f["default"], "help": f["help"] or FIELD_HELP.get(key, ""),
            }
            if f["choices"]:
                row["choices"] = f["choices"]
            if key in RANGES:
                row["range"] = RANGES[key]
            if f["optional"]:
                row["optional"] = "blank / null switches it off"
            if key in pinned:
                row["pinned"] = f"set on the command line to {pinned[key]!r}; cannot be changed from here"
            if key in learned:
                row["learned"] = f"currently adjusted by the learning layer to {learned[key]!r}"
            out.append(row)
    return out


def _r_stats(closed: list[dict], n: int = 20) -> dict[str, Any]:
    rs = [float(t["r_multiple"]) for t in closed[-n:] if t.get("r_multiple") is not None]
    if not rs:
        return {"n": 0}
    wins = [r for r in rs if r > 0]
    return {
        "n": len(rs), "avg_r": round(sum(rs) / len(rs), 2), "win_rate": round(len(wins) / len(rs), 2),
        "best": round(max(rs), 2), "worst": round(min(rs), 2), "last": [round(r, 2) for r in rs[-8:]],
    }


def desk_summary(session: "TradingSession") -> dict[str, Any]:
    """The state the advisor reasons against. Read from the files the desk
    already writes; anything missing is reported as missing, not guessed."""
    from .accounts import read_snapshot
    from .learning import load_overrides, load_report
    from .trader import TraderState

    s = session.s
    state = TraderState.load(session.state_path)
    acct = read_snapshot(session.state_dir) or {}
    last = session.last_report() or {}
    learn = load_report(session.state_dir) or {}
    halt = session.halt_state()
    summary: dict[str, Any] = {
        "desk": {"broker": s.broker, "live": bool(s.live), "data_source": s.data, "config_source": session.config_source, "state_dir": str(session.state_dir)},
        "account": {
            "asof": acct.get("asof"), "currency": acct.get("currency"), "equity": acct.get("equity"), "cash": acct.get("cash"),
            "open_positions": len(acct.get("positions") or state.managed), "unrealized": acct.get("unrealized"), "day_pnl": acct.get("day_pnl"),
            "realized_total": acct.get("realized_total"), "error": acct.get("error"),
        } if acct else {"note": "no account snapshot yet (written after the first cycle)"},
        "positions": [
            {"symbol": p.get("symbol"), "setup": p.get("setup"), "qty": p.get("qty"), "unrealized_pct": p.get("unrealized_pct"), "entry_date": p.get("entry_date")}
            for p in (acct.get("positions") or [])[:12]
        ],
        "pending_entries": sorted(state.pending)[:12],
        "arming_list": len(state.arming),
        "closed_trades_total": len(state.closed),
        "recent_r_multiples": _r_stats(state.closed),
        "last_cycle": {"asof": last.get("asof"), "label": last.get("label"), "regime_ok": last.get("regime_ok"), "regime_note": last.get("regime_note"), "data_gaps": (last.get("data_gaps") or [])[:5]} if last else {"note": "no cycle report yet"},
        "kill_switch": halt or {"on": False},
        "learned_adjustments": load_overrides(session.state_dir),
        "lessons": [l.get("text") for l in (learn.get("lessons") or [])[:6]],
        "cli_overrides": dict(s.overrides or {}),
    }
    return summary


# --------------------------------------------------------------------------- #
# Transcript
# --------------------------------------------------------------------------- #
def _path(session: "TradingSession") -> Path:
    return session.state_dir / ADVISOR_FILE


def load_transcript(session: "TradingSession") -> dict[str, Any]:
    p = _path(session)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            pass
    return {"exchanges": []}


def _save(session: "TradingSession", transcript: dict[str, Any]) -> None:
    transcript["exchanges"] = transcript["exchanges"][-MAX_EXCHANGES_KEPT:]
    p = _path(session)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(transcript, indent=2, default=str))
    tmp.replace(p)


def clear_transcript(session: "TradingSession") -> bool:
    p = _path(session)
    if p.exists():
        p.unlink()
        return True
    return False


def pending_changes(transcript: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for ex in transcript.get("exchanges", []) for c in ex.get("changes", []) if c.get("status") == "proposed" and c.get("valid")]


# --------------------------------------------------------------------------- #
# Ask
# --------------------------------------------------------------------------- #
def _field_meta(cfg: StrategyConfig, key: str) -> tuple[str, bool] | None:
    """(kind, optional) for a dotted key, or None when it does not exist."""
    section, _, rest = key.partition(".")
    if not rest or not hasattr(cfg, section) or section == "advisor":
        return None
    sub = getattr(cfg, section)
    name, _, leaf = rest.partition(".")
    for f in fields(sub):
        if f.name == name:
            kind = _field_kind(str(f.type))
            if kind == "weights":
                return ("float", False) if leaf else None
            return (kind, _optional(str(f.type))) if not leaf else None
    return None


def validate_change(cfg: StrategyConfig, key: str, raw_value: Any, pinned: dict[str, Any]) -> dict[str, Any]:
    """Coerce and test one proposed change against the current config. Never raises."""
    change: dict[str, Any] = {"key": key, "proposed": raw_value, "valid": False}
    if any(key.startswith(p) for p in FORBIDDEN_PREFIXES):
        change["problem"] = "the advisor may not change its own settings"
        return change
    if key in pinned:
        change["problem"] = f"pinned by the command line ({pinned[key]!r}); change it there"
        return change
    meta = _field_meta(cfg, key)
    if meta is None:
        change["problem"] = "unknown parameter (not in the settings map)"
        return change
    kind, optional = meta
    text = raw_value if isinstance(raw_value, str) else ("" if raw_value is None else json.dumps(raw_value) if isinstance(raw_value, (list, dict)) else str(raw_value))
    if isinstance(raw_value, list):
        text = ", ".join(str(x) for x in raw_value)
    if isinstance(raw_value, bool):
        text = "1" if raw_value else "0"
    try:
        value = _coerce(kind, text, optional, key)
        if key in CHOICES and value not in CHOICES[key]:
            raise ValueError(f"{key}: must be one of {', '.join(CHOICES[key])}")
        candidate = cfg.with_overrides({key: value})
        validate_config(candidate)
    except (ValueError, KeyError, TypeError) as exc:
        change["problem"] = str(exc).strip("'")
        return change
    current = _current(cfg, key)
    change.update(value=value, current=current, valid=True, no_op=(value == current))
    return change


def _current(cfg: StrategyConfig, key: str) -> Any:
    section, _, rest = key.partition(".")
    name, _, leaf = rest.partition(".")
    v = getattr(getattr(cfg, section), name)
    return (v or {}).get(leaf) if leaf else v


def ask_advisor(session: "TradingSession", message: str, now: datetime | None = None) -> dict[str, Any]:
    """One exchange: build the bundle, call the model, validate its proposals,
    append to the transcript. Never raises: transport / parsing problems are
    recorded on the exchange (``error``) and in the health registry."""
    from .reviewer import ask_json, parse_json_object, resolve_provider

    now = now or datetime.now(timezone.utc)
    message = " ".join(str(message or "").split())
    session.reload_settings()
    cfg = session.cfg
    adv = cfg.advisor
    transcript = load_transcript(session)
    pinned = dict(session.s.overrides or {})
    learned = dict(getattr(session, "learned_overrides", {}) or {})
    history = [
        {"you": ex["message"], "advisor": ex.get("reply"), "changes": [{"key": c["key"], "value": c.get("proposed"), "status": c.get("status")} for c in ex.get("changes", [])]}
        for ex in transcript["exchanges"][-max(0, adv.max_history):] if not ex.get("error")
    ]
    exchange: dict[str, Any] = {
        "id": uuid.uuid4().hex[:10], "at": now.isoformat(timespec="seconds"), "message": message,
        "reply": None, "changes": [], "questions": [], "risk_note": "", "provider": None, "model": None, "error": None,
    }
    if not message:
        exchange["error"] = "empty message"
        transcript["exchanges"].append(exchange)
        _save(session, transcript)
        return exchange
    provider, model, _ = resolve_provider(adv)
    exchange["provider"], exchange["model"] = provider, model
    bundle = {
        "operator_message": message,
        "conversation": history,
        "desk": desk_summary(session),
        "settings_map": settings_catalogue(cfg, pinned, learned),
    }
    try:
        text, provider, model = ask_json(bundle, adv, SYSTEM_PROMPT, RESPONSE_SCHEMA)
        raw = parse_json_object(text)
        exchange["provider"], exchange["model"] = provider, model
        exchange["reply"] = str(raw.get("reply", "")).strip()
        exchange["risk_note"] = str(raw.get("risk_note", "") or "").strip()
        exchange["questions"] = [str(q).strip() for q in (raw.get("questions") or []) if str(q).strip()][:6]
        proposals = raw.get("changes") or []
        if not isinstance(proposals, list):
            proposals = []
        dropped = max(0, len(proposals) - adv.max_changes)
        for p in proposals[: adv.max_changes]:
            if not isinstance(p, dict) or not p.get("key"):
                continue
            c = validate_change(cfg, str(p["key"]).strip(), p.get("value"), pinned)
            c.update(id=uuid.uuid4().hex[:8], why=str(p.get("why", "")).strip(), status="proposed" if c["valid"] and not c.get("no_op") else "rejected" if not c["valid"] else "no_change")
            exchange["changes"].append(c)
        if dropped:
            exchange["questions"].append(f"{dropped} further proposal(s) were dropped (advisor.max_changes = {adv.max_changes}).")
        ok = True
    except Exception as exc:
        exchange["error"] = describe_error(exc)
        ok = False
        log.warning("advisor failed: %s", exchange["error"])
    transcript["exchanges"].append(exchange)
    _save(session, transcript)
    try:
        n = sum(1 for c in exchange["changes"] if c["status"] == "proposed")
        session.health.record("llm_advisor", ok, detail=f"{exchange['provider']} / {exchange['model']}: {n} change(s) proposed", error=exchange["error"], items=n)
    except Exception:  # pragma: no cover
        pass
    return exchange


# --------------------------------------------------------------------------- #
# Apply / dismiss
# --------------------------------------------------------------------------- #
def apply_changes(session: "TradingSession", change_ids: list[str], by: str = "advisor page") -> dict[str, Any]:
    """Apply the accepted proposals to settings.yaml, in one validated write.

    Every accepted change is re-validated against the *current* config (the
    settings may have moved since it was proposed). Returns what was applied
    and what was skipped, with reasons. Nothing is written when any accepted
    change fails validation against the others.
    """
    session.reload_settings()
    transcript = load_transcript(session)
    wanted = set(change_ids or [])
    cfg = session.cfg
    pinned = dict(session.s.overrides or {})
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    overrides: dict[str, Any] = {}
    targets = [c for ex in transcript["exchanges"] for c in ex.get("changes", []) if c.get("id") in wanted]
    for c in targets:
        if c.get("status") != "proposed":
            skipped.append({"id": c["id"], "key": c["key"], "reason": f"already {c.get('status')}"})
            continue
        check = validate_change(cfg, c["key"], c.get("proposed"), pinned)
        if not check["valid"]:
            c.update(status="rejected", problem=check.get("problem"))
            skipped.append({"id": c["id"], "key": c["key"], "reason": check.get("problem")})
            continue
        overrides[c["key"]] = check["value"]
        applied.append(c)
    if overrides:
        try:
            new_cfg = cfg.with_overrides(overrides)
            validate_config(new_cfg)
        except (ValueError, KeyError) as exc:
            return {"applied": [], "skipped": skipped + [{"id": c["id"], "key": c["key"], "reason": f"combined changes invalid: {exc}"} for c in applied], "written": False}
        session.store.save_config(new_cfg)
        session.reload_settings(force=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for c in applied:
            c.update(status="applied", applied_at=stamp, applied_by=by)
        try:
            session.health.record("advisor_apply", True, detail=f"{len(applied)} setting(s) changed by {by}: " + ", ".join(f"{c['key']}={c.get('value')}" for c in applied), items=len(applied))
        except Exception:  # pragma: no cover
            pass
        log.info("advisor: %s applied %s", by, overrides)
    _save(session, transcript)
    return {"applied": [{"id": c["id"], "key": c["key"], "value": c.get("value"), "was": c.get("current")} for c in applied], "skipped": skipped, "written": bool(overrides)}


def dismiss_changes(session: "TradingSession", change_ids: list[str]) -> int:
    transcript = load_transcript(session)
    wanted = set(change_ids or [])
    n = 0
    for ex in transcript["exchanges"]:
        for c in ex.get("changes", []):
            if c.get("id") in wanted and c.get("status") == "proposed":
                c["status"] = "dismissed"
                n += 1
    if n:
        _save(session, transcript)
    return n


def advisor_available(cfg: StrategyConfig) -> tuple[bool, str]:
    """(usable, note) - whether a key exists for the configured provider."""
    from .reviewer import resolve

    res = resolve(cfg.advisor)
    return res.configured, res.describe() if res.configured else (res.error or "not configured")
