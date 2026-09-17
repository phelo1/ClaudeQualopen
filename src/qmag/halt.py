"""The kill switch.

A file in the state directory (``halt.json``) that every process honours -
daemon, dashboard and CLI. While it is on, no new position is opened by any
path (automated cycles, focused passes, resting buy-stops, the lookup page's
Buy button, broker test orders) and resting entry orders are cancelled; open
positions keep being *managed* - stops, partials and trails still run - so
the switch never leaves a position unprotected. ``flatten`` additionally
sells everything at market when the switch is thrown.

It is a file rather than a setting so it survives restarts, can be thrown
from a shell on the box (``qmag halt``) when the dashboard is unreachable,
and is visible to a process that started before it was set.
"""

from __future__ import annotations

import json
from .persistence import atomic_text, atomic_json
from datetime import datetime
from pathlib import Path

from .market_calendar import NY

HALT_FILE = "halt.json"


def halt_path(state_dir: Path | str) -> Path:
    return Path(state_dir) / HALT_FILE


def halt_status(state_dir: Path | str) -> dict | None:
    """The active halt record (``reason``, ``at``, ``by``, ``flattened``) or None when trading is allowed."""
    path = halt_path(state_dir)
    if not path.exists():
        return None
    try:
        rec = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"reason": "halt file present but unreadable", "at": None, "by": "unknown", "flattened": False}
    if not rec.get("on", True):
        return None
    return {"reason": str(rec.get("reason") or "no reason given"), "at": rec.get("at"), "by": str(rec.get("by") or "unknown"), "flattened": bool(rec.get("flattened"))}


def halt_reason(state_dir: Path | str) -> str | None:
    """One line for reports and gates: ``"HALTED by dashboard 2026-09-12 09:41: reason"``, or None."""
    st = halt_status(state_dir)
    if st is None:
        return None
    when = f" {str(st['at'])[:16].replace('T', ' ')}" if st.get("at") else ""
    return f"HALTED by {st['by']}{when}: {st['reason']}"


def set_halt(state_dir: Path | str, on: bool, reason: str = "", by: str = "cli", flattened: bool = False, now: datetime | None = None) -> dict | None:
    """Throw or clear the switch. Returns the new status (None when cleared)."""
    path = halt_path(state_dir)
    if not on:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"on": True, "reason": reason.strip() or "no reason given", "by": by, "at": (now or datetime.now(NY)).isoformat(timespec="seconds"), "flattened": flattened}
    atomic_json(path, rec)
    return halt_status(state_dir)
