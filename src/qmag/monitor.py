"""Read-only chart records for the current monitored universe."""
from __future__ import annotations

from dataclasses import asdict
import json
import math
from pathlib import Path
import re
from datetime import datetime, timezone

from .persistence import atomic_json


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def research_symbols(directory: Path) -> set[str]:
    path = Path(directory) / "insider_scan.json"
    if not path.exists():
        return set()
    report = json.loads(path.read_text(encoding="utf-8"))
    at = report.get("generated_at")
    if not at or (datetime.now(timezone.utc)-datetime.fromisoformat(at).astimezone(timezone.utc)).days > 14:
        return set()
    return {str(row["ticker"]).upper() for row in report.get("flagged", []) if re.fullmatch(r"[A-Za-z0-9.^_-]{1,24}", str(row.get("ticker", "")))}


def capture(directory: Path, frames: dict, report, state) -> None:
    from .indicators import enrich
    path = Path(directory) / "monitor"
    signals = {s.symbol: s for s in report.watchlist + report.triggered}
    plans = {p.symbol: p for p in report.plans + report.rejected}
    symbols = set(state.arming) | set(state.managed) | set(state.pending) | set(plans) | research_symbols(directory)
    for symbol in symbols:
        if symbol not in frames or not re.fullmatch(r"[A-Za-z0-9.^_-]{1,24}", symbol):
            continue
        df = enrich(frames[symbol]).tail(180)
        if df.empty:
            continue
        bars = [{"date": str(date), **{k: number(row.get(k)) for k in ("open", "high", "low", "close", "volume", "sma_10", "sma_20", "sma_50")}} for date, row in df.iterrows()]
        pos, pending, arm, plan, signal = state.managed.get(symbol), state.pending.get(symbol), state.arming.get(symbol), plans.get(symbol), signals.get(symbol)
        levels = {}
        kind = "watching"
        if pos:
            levels = {"entry": pos["entry_price"], "stop": pos["stop"], "target": None if pos["partial_done"] else pos.get("target"), "pivot": pos.get("pivot")}
            kind = "position"
        elif pending:
            levels = {"entry": pending["trigger"], "stop": pending["stop"], "target": pending.get("target"), "pivot": pending["trigger"]}
            kind = "pending"
        elif plan:
            levels = {"entry": plan.entry, "stop": plan.stop, "target": plan.partial_target, "pivot": plan.pivot}
            kind = "qualified" if plan.ok else "blocked"
        elif arm:
            levels = {k: arm.get(k) for k in ("entry", "stop", "target", "pivot")}
        fills = [{"date": v.get("updated_at"), "side": v["side"], "quantity": v["filled"], "price": v["average"]}
                 for v in state.executions.values() if v["symbol"] == symbol and v["filled"]]
        latest = bars[-1]["close"]
        unrealized = (latest-pos["entry_price"]) * pos["remaining"] if pos and latest is not None else None
        realized = pos["realised"]-(pos["shares"]-pos["remaining"])*pos["entry_price"]-pos.get("fees", 0) if pos else None
        payload = {"symbol": symbol, "kind": kind, "asof": report.asof, "setup": (pos or pending or arm or {}).get("setup", signal.setup if signal else ""),
                   "volume_note": next((a for a in report.actions if a.startswith("PACE ")), "Recorded bar volume"),
                   "bars": bars, "levels": {k:number(v) for k,v in levels.items()}, "fills": fills,
                   "shares": pos["remaining"] if pos else pending["qty"] if pending else plan.shares if plan else None,
                   "unrealized": unrealized, "realized": realized, "fees_known": bool(pos and pos.get("fees_known")),
                   "evidence": pos.get("evidence") if pos else None, "entry_date": pos.get("entry_date") if pos else None,
                   "details": signal.details if signal else {}, "failed_checks": plan.failed_checks if plan else [],
                   "policy_version": (pos or {}).get("features", {}).get("policy_version", "baseline") if (pos or {}).get("features") else "baseline"}
        # Indicator frames can carry numpy scalar details; normalized prices are
        # finite and missing values stay null.
        payload["details"] = {k:number(v) if isinstance(v, (int,float)) else str(v) for k,v in payload["details"].items()}
        atomic_json(path / f"{symbol}.json", payload)


def records(directory: Path, state) -> list[dict]:
    symbols = set(state.arming) | set(state.managed) | set(state.pending) | research_symbols(directory)
    result = []
    for path in sorted((Path(directory) / "monitor").glob("*.json")):
        if path.stem not in symbols:
            continue
        row = json.loads(path.read_text(encoding="utf-8"))
        # Current state labels win over older chart observations.
        row["kind"] = "position" if path.stem in state.managed else "pending" if path.stem in state.pending else "watching"
        result.append(row)
    return sorted(result, key=lambda row: (row["kind"] != "position", row["symbol"]))
