"""Accounts: what every account the desk touches holds, in one place.

One desk (a state directory with its own daemon) drives exactly one broker
account. That is deliberate: fills, cash, base currency and permissions
differ between accounts, so an order is never mirrored from one account to
another - each desk sizes, sends, protects and journals its own trades. To
trade several accounts you run several desks (``deploy/add-desk.sh``).

What this module adds on top:

* every desk writes ``account.json`` after each cycle (and on demand): the
  broker's own read of equity and cash, every holding the broker reports
  (managed by qmag or not) marked at the latest real bar, unrealised and
  realised P&L, resting orders, the daemon heartbeat;
* the local desk lists the other desks it should show (``QMAG_DESKS``) and
  reads their snapshots read-only, so one dashboard shows every account;
* totals are added per currency - there is no made-up FX conversion.

Nothing in here invents a number. A position without a fresh bar shows no
market value; an unreachable broker leaves ``equity`` empty and says why; a
snapshot older than ``STALE_AFTER_SECONDS`` is flagged stale.
"""

from __future__ import annotations

import json
from .persistence import atomic_json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from .halt import halt_status, set_halt
from .market_calendar import NY
from .persistence import serialized

if TYPE_CHECKING:  # pragma: no cover
    from .session import TradingSession

log = logging.getLogger(__name__)

ACCOUNT_FILE = "account.json"
DESKS_ENV = "QMAG_DESKS"
DESK_NAME_ENV = "QMAG_DESK_NAME"
STALE_AFTER_SECONDS = 8 * 3600  # a snapshot older than this is shown as stale (the desk did not cycle)
HEARTBEAT_STALE_SECONDS = 5 * 60
MARK_MAX_AGE_HOURS = 1.0


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat(timespec="seconds") if ts else None


def _age_seconds(iso: str | None, now: datetime | None = None) -> float | None:
    if not iso:
        return None
    try:
        ts = datetime.fromisoformat(str(iso))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ((now or _now_utc()) - ts).total_seconds()


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def desk_name(state_dir: Path) -> str:
    """The local desk's display name: ``QMAG_DESK_NAME`` or the state directory's name."""
    return (os.environ.get(DESK_NAME_ENV) or "").strip() or Path(state_dir).name


# --------------------------------------------------------------------------- #
# Snapshot of the local desk's account
# --------------------------------------------------------------------------- #
def _marks_from_frames(frames: dict[str, pd.DataFrame] | None, symbols: list[str]) -> dict[str, tuple[float, str]]:
    out: dict[str, tuple[float, str]] = {}
    for sym in symbols:
        df = (frames or {}).get(sym)
        if df is None or not len(df) or "close" not in df.columns:
            continue
        last = df.iloc[-1]
        if pd.isna(last["close"]):
            continue
        out[sym] = (float(last["close"]), str(pd.Timestamp(df.index[-1]).date()))
    return out


def _fetch_marks(session: "TradingSession", symbols: list[str], max_age_hours: float) -> tuple[dict[str, tuple[float, str]], str | None]:
    """Latest real closes for ``symbols`` from the desk's price source (cached bars are fine)."""
    if not symbols:
        return {}, None
    from .data import make_provider

    try:
        provider = make_provider(session.s.data, directory=session.s.csv_dir, cache_dir=session.s.cache_dir, max_age_hours=max_age_hours)
        start = str((pd.Timestamp.today().normalize() - pd.Timedelta(days=30)).date())
        frames = provider.load(list(symbols), start=start, end=None)
    except Exception as exc:
        return {}, f"marks unavailable from {session.s.data}: {type(exc).__name__}: {exc}"
    marks = _marks_from_frames(frames, symbols)
    missing = [s for s in symbols if s not in marks]
    return marks, (f"no bars for {', '.join(missing)} from {session.s.data}" if missing else None)


@serialized
def build_snapshot(
    session: "TradingSession",
    frames: dict[str, pd.DataFrame] | None = None,
    fetch_missing: bool = True,
    now: datetime | None = None,
    max_age_hours: float = MARK_MAX_AGE_HOURS,
) -> dict:
    """Read the broker account and holdings, mark them at the latest real bar,
    and merge in what the trader knows about each position.

    ``frames`` (the bars a cycle just loaded) supply the marks; symbols the
    broker holds that are not in them are fetched from the desk's price
    source when ``fetch_missing`` is on. Never raises: an unreachable broker
    yields a snapshot with ``error`` set and empty figures.
    """
    now = now or _now_utc()
    today_ny = now.astimezone(NY).date()
    state = session.state()
    snap: dict = {
        "asof": _iso(now),
        "desk": desk_name(session.state_dir),
        "state_dir": str(session.state_dir),
        "broker": session.s.broker,
        "broker_name": session.s.broker,
        "live": bool(session.s.live),
        "currency": None,
        "equity": None,
        "cash": None,
        "invested_cost": None,
        "market_value": None,
        "unrealized": None,
        "unrealized_known": False,
        "day_open_equity": None,
        "day_pnl": None,
        "positions": [],
        "open_orders": None,
        "realized_total": round(sum(float(r.get("pnl") or 0.0) for r in state.closed), 2),
        "realized_today": round(sum(float(r.get("pnl") or 0.0) for r in state.closed if str(r.get("closed_on") or "") == str(today_ny)), 2),
        "trades": len(state.closed),
        "pending_entries": len(state.pending),
        "arming": len(state.arming),
        "marks_source": session.s.data,
        "marks_asof": None,
        "data_gaps": [],
        "error": None,
        "halt": halt_status(session.state_dir),
    }
    try:
        broker = session.broker
        acct = session.account()
        held = broker.positions()
        orders = broker.open_orders()
    except Exception as exc:
        snap["error"] = f"broker unreachable: {type(exc).__name__}: {exc}"
        snap["data_gaps"].append(snap["error"])
        return snap
    snap["broker_name"] = getattr(broker, "name", session.s.broker)
    snap["currency"] = acct.currency
    snap["equity"] = round(float(acct.equity), 2)
    snap["cash"] = round(float(acct.cash), 2)
    snap["open_orders"] = len(orders)
    day = state.day_equity or {}
    if day.get("date") and float(day.get("equity") or 0) > 0 and str(day["date"]) == str(today_ny):
        snap["day_open_equity"] = round(float(day["equity"]), 2)
        snap["day_pnl"] = round(snap["equity"] - snap["day_open_equity"], 2)

    managed = {s: dict(r) for s, r in state.managed.items()}
    symbols = sorted(set(held) | set(managed))
    marks = _marks_from_frames(frames, symbols)
    missing = [s for s in symbols if s not in marks]
    if missing and fetch_missing:
        fetched, gap = _fetch_marks(session, missing, max_age_hours)
        marks.update(fetched)
        if gap:
            snap["data_gaps"].append(gap)
    elif missing:
        snap["data_gaps"].append(f"no fresh bars for {', '.join(missing)}: market value not shown")

    rows: list[dict] = []
    orders_by_symbol: dict[str, int] = {}
    for o in orders:
        orders_by_symbol[o.symbol] = orders_by_symbol.get(o.symbol, 0) + 1
    for sym in symbols:
        bp = held.get(sym)
        m = managed.get(sym)
        qty = int(bp.qty) if bp is not None else int(m.get("remaining", m.get("shares", 0)) if m else 0)
        avg = float(bp.avg_price) if bp is not None else float(m.get("entry_price") or 0.0) if m else 0.0
        mark = marks.get(sym)
        last, mark_asof = (mark if mark else (None, None))
        cost = round(qty * avg, 2)
        row = {
            "symbol": sym,
            "qty": qty,
            "avg_cost": round(avg, 4),
            "cost_basis": cost,
            "last": last,
            "mark_asof": mark_asof,
            "market_value": round(qty * last, 2) if last is not None else None,
            "unrealized": round(qty * (last - avg), 2) if last is not None else None,
            "unrealized_pct": round((last - avg) / avg, 4) if last is not None and avg else None,
            "managed": m is not None,
            "at_broker": bp is not None,
            "orders": orders_by_symbol.get(sym, 0),
            "setup": m.get("setup") if m else None,
            "entry_date": m.get("entry_date") if m else None,
            "stop": m.get("stop") if m else None,
            "target": (m.get("target") if not m.get("partial_done") else None) if m else None,
            "partial_done": bool(m.get("partial_done")) if m else None,
            "note": None,
        }
        if m is not None and bp is None:
            row["note"] = "managed by qmag but not reported by the broker"
        elif m is None and bp is not None:
            row["note"] = "held at the broker outside qmag (not managed: no stop, not in the journal)"
        rows.append(row)
    snap["positions"] = rows
    snap["invested_cost"] = round(sum(r["cost_basis"] for r in rows if r["at_broker"]), 2)
    marked = [r for r in rows if r["at_broker"] and r["market_value"] is not None]
    if marked:
        snap["market_value"] = round(sum(r["market_value"] for r in marked), 2)
        snap["unrealized"] = round(sum(r["unrealized"] for r in marked), 2)
        snap["marks_asof"] = max(r["mark_asof"] for r in marked)
    elif not rows:
        snap["market_value"] = 0.0
        snap["unrealized"] = 0.0
    snap["unrealized_known"] = len(marked) == len([r for r in rows if r["at_broker"]])
    return snap


def snapshot_path(state_dir: Path) -> Path:
    return Path(state_dir) / ACCOUNT_FILE


def write_snapshot(session: "TradingSession", frames: dict[str, pd.DataFrame] | None = None, fetch_missing: bool = True, now: datetime | None = None) -> dict:
    """Build the local desk's snapshot and write ``account.json`` atomically."""
    snap = build_snapshot(session, frames=frames, fetch_missing=fetch_missing, now=now)
    path = snapshot_path(session.state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, snap)
    return snap


def read_snapshot(state_dir: Path) -> dict | None:
    return _read_json(snapshot_path(state_dir))


# --------------------------------------------------------------------------- #
# Several desks
# --------------------------------------------------------------------------- #
def parse_desks(value: str | None) -> list[dict]:
    """``QMAG_DESKS`` -> [{name, state_dir, url}].

    Format: ``name=/path/to/state[|http://dashboard]`` entries separated by
    commas, semicolons or newlines. A bare path gets its directory name.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for raw in (value or "").replace(";", ",").replace("\n", ",").split(","):
        item = raw.strip()
        if not item:
            continue
        url = None
        if "|" in item:
            item, url = (p.strip() for p in item.split("|", 1))
        if "=" in item:
            name, path = (p.strip() for p in item.split("=", 1))
        else:
            name, path = "", item
        if not path:
            continue
        state_dir = Path(os.path.expanduser(path))
        key = str(state_dir.resolve()) if state_dir.exists() else str(state_dir)
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name or state_dir.name, "state_dir": str(state_dir), "url": url or None})
    return out


def desk_list(session: "TradingSession") -> list[dict]:
    """The local desk first, then every desk listed in ``QMAG_DESKS`` (the local one is never duplicated)."""
    local = {"name": desk_name(session.state_dir), "state_dir": str(session.state_dir), "url": None, "local": True}
    out = [local]
    local_key = str(Path(session.state_dir).resolve())
    for d in parse_desks(os.environ.get(DESKS_ENV)):
        p = Path(d["state_dir"])
        if (str(p.resolve()) if p.exists() else str(p)) == local_key:
            continue
        out.append({**d, "local": False})
    return out


def read_desk(desk: dict, now: datetime | None = None) -> dict:
    """One desk's account snapshot plus its daemon heartbeat and kill switch, with a plain status."""
    now = now or _now_utc()
    state_dir = Path(desk["state_dir"])
    snap = read_snapshot(state_dir)
    status = _read_json(state_dir / "daemon_status.json") or {}
    hb_age = _age_seconds(status.get("heartbeat"), now)
    snap_age = _age_seconds((snap or {}).get("asof"), now)
    out = {
        "name": desk["name"],
        "state_dir": str(state_dir),
        "url": desk.get("url"),
        "local": bool(desk.get("local")),
        "exists": state_dir.exists(),
        "snapshot": snap,
        "snapshot_age_s": snap_age,
        "stale": snap_age is not None and snap_age > STALE_AFTER_SECONDS,
        "daemon": {
            "heartbeat": status.get("heartbeat"),
            "age_s": hb_age,
            "running": hb_age is not None and hb_age <= HEARTBEAT_STALE_SECONDS,
            "task": status.get("running"),
            "next_task": status.get("next_task"),
            "broker": status.get("broker"),
            "live": status.get("live"),
        },
        "halt": halt_status(state_dir) if state_dir.exists() else None,
    }
    if not state_dir.exists():
        out["state"], out["problem"] = "missing", f"state directory {state_dir} does not exist"
    elif snap is None:
        out["state"], out["problem"] = "no_snapshot", "no account snapshot yet - the desk has not completed a cycle (or runs an older qmag)"
    elif snap.get("error"):
        out["state"], out["problem"] = "error", snap["error"]
    elif out["stale"]:
        out["state"], out["problem"] = "stale", f"snapshot is {snap_age / 3600:.1f} h old - the desk's daemon is not cycling"
    else:
        out["state"], out["problem"] = "ok", None
    return out


def _sum(rows: list[dict], key: str) -> float | None:
    vals = [r["snapshot"].get(key) for r in rows if r.get("snapshot")]
    known = [float(v) for v in vals if v is not None]
    return round(sum(known), 2) if known else None


def totals_by_currency(desks: list[dict]) -> list[dict]:
    """Sum the desks that have a fresh, usable snapshot, one row per currency.
    Stale or failed snapshots are left out (and listed under problems); no FX is applied."""
    groups: dict[str, list[dict]] = {}
    for d in desks:
        snap = d.get("snapshot")
        if not snap or snap.get("error") or snap.get("equity") is None or d.get("stale"):
            continue
        groups.setdefault(str(snap.get("currency") or "?"), []).append(d)
    out = []
    for cur, rows in sorted(groups.items(), key=lambda kv: (kv[0] != "USD", kv[0])):
        out.append({
            "currency": cur,
            "accounts": len(rows),
            "equity": _sum(rows, "equity"),
            "cash": _sum(rows, "cash"),
            "market_value": _sum(rows, "market_value"),
            "unrealized": _sum(rows, "unrealized"),
            "unrealized_known": all(r["snapshot"].get("unrealized_known") for r in rows),
            "day_pnl": _sum(rows, "day_pnl"),
            "day_pnl_known": all(r["snapshot"].get("day_pnl") is not None for r in rows),
            "realized_today": _sum(rows, "realized_today"),
            "realized_total": _sum(rows, "realized_total"),
            "positions": sum(len(r["snapshot"].get("positions") or []) for r in rows),
            "open_orders": sum(int(r["snapshot"].get("open_orders") or 0) for r in rows),
            "desks": [r["name"] for r in rows],
        })
    return out


def desk_overview(session: "TradingSession", refresh_local: bool = False, now: datetime | None = None) -> dict:
    """Everything the accounts page shows: each desk's snapshot and status, per-currency totals."""
    now = now or _now_utc()
    if refresh_local:
        try:
            write_snapshot(session, now=now)
        except Exception as exc:  # pragma: no cover - the page must still render
            log.warning("account snapshot failed: %s", exc)
    desks = [read_desk(d, now) for d in desk_list(session)]
    problems = [f"{d['name']}: {d['problem']}" for d in desks if d.get("problem")]
    return {
        "generated_at": _iso(now),
        "desks": desks,
        "totals": totals_by_currency(desks),
        "problems": problems,
        "configured": len(desks) > 1,
        "desks_env": os.environ.get(DESKS_ENV) or "",
    }


def find_desk(session: "TradingSession", name: str) -> dict | None:
    return next((d for d in desk_list(session) if d["name"] == name), None)


def halt_desk(session: "TradingSession", name: str, on: bool, reason: str = "", by: str = "accounts page", now: datetime | None = None) -> dict:
    """Throw or clear another desk's kill switch by writing its ``halt.json``.

    Its daemon reads the file at the start of every pass, so new entries stop
    there within one focused interval. Flattening (selling at market) needs
    that desk's broker connection and is only offered on the desk itself.
    """
    desk = find_desk(session, name)
    if desk is None:
        raise KeyError(f"no desk named '{name}' (local desk or {DESKS_ENV})")
    if desk.get("local"):
        return session.halt(on, reason=reason, by=by)
    state_dir = Path(desk["state_dir"])
    if not state_dir.exists():
        raise FileNotFoundError(f"desk '{name}': state directory {state_dir} does not exist")
    if on:
        st = set_halt(state_dir, True, reason, by=by, now=now)
        return {"on": True, "desk": name, "status": st, "message": f"{name}: trading halted ({st['reason']}) - its daemon stops new entries on its next pass; open positions keep their stops"}
    set_halt(state_dir, False)
    return {"on": False, "desk": name, "status": None, "message": f"{name}: trading resumed on its next pass"}
