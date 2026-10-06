"""The trading routine that turns the research rules into orders.

One ``run_cycle`` does, in order:

1. let the paper broker fill resting orders against the latest bars
2. reconcile our book with the broker's (adopt fills, notice partials/stops)
3. manage every open position: profit target (resting OCO), 3-5 day partial
   rule, breakeven, MA trail, time stop - and make sure the exit orders that
   implement the plan are actually resting at the broker
4. read the market regime (trend / breadth / VIX)
5. scan for fresh setups, run every check, size the trade, and act on it
   according to the **entry mode** (``entry.mode``):

   * ``confirmed`` (default) - no resting orders. A setup is bought at market
     only when the live bar has taken out the pivot *and* is holding above
     it, on volume pacing at or above ``entry.confirm_volume_ratio`` of the
     20-day average, outside the opening range. Names that broke out on a
     wick, on thin volume or too early are **held** (kept armed, re-checked
     next pass), never bought. Nothing can fill us overnight or on a spike.
   * ``resting`` - the classic buy-stop-limit bracket parked at the pivot
     for the next session (fills on price alone).
   * ``hybrid`` - confirmed rules for market entries, plus buy-stop brackets
     parked from ``entry.resting_from`` (after the opening range) that are
     cancelled again after the close.

   In every mode a fresh position whose bar closes back below the pivot
   within ``entry.failed_breakout_days`` is exited (failed breakout) rather
   than left to ride down to the stop.

The daemon calls this as a *full scan* after the close (and once after the
open), and as a *focused* pass every few minutes on the **arming list**: the
names the nightly scan found within striking distance of a pivot, plus open
positions, pending entries and the day's screener hits. A focused pass loads
and checks only those symbols, re-sizes only names near their trigger, and
leaves untouched buy-stop brackets resting for the rest of the list.

The trader keeps its own JSON state because brokers know your quantity and
average price but not *why* you hold something, where the initial stop was,
what the profit-taking plan is - or which names are armed for tomorrow. The
state is also the **trade journal** the learning layer (``qmag.learning``)
reads: every entry stores the features it was taken on, every close gets a
post-mortem, and setups that were rejected, held back or not taken are
tracked as *shadow trades* so the filters can be judged on what they cost.
"""

from __future__ import annotations

import json
from .persistence import atomic_text, atomic_json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .backtest import default_detectors, prepare_data
from .broker import Broker, Order, orders_equivalent
from .config import StrategyConfig
from .context import ContextGatherer, ContextReport
from .market_calendar import NY, parse_hhmm
from .plan import (
    TradePlan,
    adaptive_risk_multiplier,
    apply_committee,
    apply_reviewer,
    build_plan,
    partial_quantity,
    resize_for_budget,
)
from .regime import RegimeSnapshot, regime_snapshot
from .setups import BreakoutDetector, Signal

log = logging.getLogger(__name__)

SHADOW_CAP = 600  # most recent shadow trades kept in the state file


@dataclass
class LiveClock:
    """What the trader knows about *when* it is running.

    ``now`` is the wall clock of a live pass (None for replays and for scans
    over completed bars); ``pace`` is the volume-projection note from
    ``qmag.pace.project_volume``. Together they say whether the latest bar is
    still forming, how far into the session we are and whether today's
    volume is a measured print or a projection.
    """

    now: datetime | None = None
    pace: dict | None = None

    @property
    def ny(self) -> datetime | None:
        if self.now is None:
            return None
        return self.now.astimezone(NY) if self.now.tzinfo else self.now.replace(tzinfo=NY)

    @property
    def fraction(self) -> float | None:
        if self.pace is not None and "fraction" in self.pace:
            return self.pace.get("fraction")
        if self.now is None:
            return None
        from .pace import session_fraction

        return session_fraction(self.now)

    @property
    def bar_complete(self) -> bool:
        """True when the latest bar is a finished session (or we have no clock at all)."""
        f = self.fraction
        return f is None or f >= 1.0

    @property
    def in_session(self) -> bool:
        return not self.bar_complete

    @property
    def pace_applied(self) -> bool:
        return bool(self.pace and self.pace.get("applied"))

    @property
    def volume_confirmed(self) -> bool:
        """Today's volume ratio can be judged: full-day print, or a projection past the noise floor."""
        return self.bar_complete or self.pace_applied

    @property
    def minutes_since_open(self) -> float | None:
        ny = self.ny
        if ny is None or not self.in_session:
            return None
        return (ny.hour - 9) * 60 + ny.minute - 30 + ny.second / 60

    def at_or_after(self, hhmm: str) -> bool:
        ny = self.ny
        if ny is None:
            return False
        return ny.time() >= parse_hhmm(hhmm)

    def time_label(self) -> str:
        ny = self.ny
        if ny is None or self.bar_complete:
            return "close"
        return ny.strftime("%H:%M")


@dataclass
class ManagedPosition:
    symbol: str
    setup: str
    entry_date: str
    entry_price: float
    shares: int
    initial_stop: float
    stop: float
    partial_done: bool = False
    pivot: float | None = None
    remaining: int = -1
    target: float | None = None
    partial_qty: int = 0
    theme: str | None = None
    realised: float = 0.0  # proceeds of shares sold so far (estimated from the bar for broker-side fills)
    plan: dict | None = None  # the TradePlan (with rationale) this position came from
    # Learning layer: the conditions the entry was taken under, and how far
    # the trade went for / against us (in R) while it was open.
    features: dict | None = None
    mfe_r: float = 0.0
    mae_r: float = 0.0
    evidence: str = "estimated"  # legacy records never become verified by migration
    pending_exit: dict | None = None
    entry_order_id: str = ""
    entry_complete: bool = True
    exit_order_ids: list[str] = field(default_factory=list)
    fees: float = 0.0
    fees_known: bool = False
    protection_intents: list[dict] = field(default_factory=list)
    liquidation_reason: str | None = None

    def __post_init__(self) -> None:
        if self.remaining < 0:
            self.remaining = self.shares
        # A confirmed market entry's trigger is its purchase reference, which
        # may be well above the breakout. Recover the actual setup pivot from
        # the frozen plan, including existing journals written by older builds.
        try:
            pivot = float((self.plan or {}).get('pivot'))
        except (TypeError, ValueError):
            pivot = None
        if pivot is not None and np.isfinite(pivot) and pivot > 0:
            self.pivot = pivot

    @property
    def risk_per_share(self) -> float:
        return max(self.entry_price - self.initial_stop, 1e-9)

    def book_sale(self, qty: int, price: float) -> None:
        self.realised += qty * price

    def track_excursion(self, bar: pd.Series) -> None:
        """Update MFE / MAE from a bar's high and low."""
        rps = self.risk_per_share
        self.mfe_r = round(max(self.mfe_r, (float(bar["high"]) - self.entry_price) / rps), 3)
        self.mae_r = round(min(self.mae_r, (float(bar["low"]) - self.entry_price) / rps), 3)

    def close_record(self, asof: str, reason: str, last_qty: int, last_price: float) -> dict:
        """Final journal entry once the last shares are gone - with a post-mortem."""
        from .learning import explain_trade

        self.book_sale(last_qty, last_price)
        cost = self.shares * self.entry_price
        pnl = self.realised - cost - self.fees
        rec = asdict(self)
        try:
            hold_days = max(len(pd.bdate_range(pd.Timestamp(self.entry_date), pd.Timestamp(asof))) - 1, 0)
        except Exception:  # pragma: no cover - odd date strings
            hold_days = None
        rec.update(
            closed_on=asof,
            exit_reason=reason,
            exit_price=round(float(last_price), 4),
            hold_days=hold_days,
            pnl=round(pnl, 2),
            pnl_pct=round(pnl / cost, 4) if cost else 0.0,
            r_multiple=round(pnl / (self.shares * self.risk_per_share), 3) if self.shares else 0.0,
        )
        rec["post_mortem"] = explain_trade(rec)
        return rec


@dataclass
class PendingPlan:
    symbol: str
    setup: str
    planned_on: str
    trigger: float
    stop: float
    qty: int
    order_id: str
    client_tag: str = ""
    entry_kind: str = "buy_stop"  # buy_stop | market
    target: float | None = None
    partial_qty: int = 0
    theme: str | None = None
    plan: dict | None = None
    features: dict | None = None


@dataclass
class TraderState:
    managed: dict[str, dict] = field(default_factory=dict)
    pending: dict[str, dict] = field(default_factory=dict)
    closed: list[dict] = field(default_factory=list)
    policy: dict = field(default_factory=dict)
    executions: dict[str, dict] = field(default_factory=dict)
    reconciliation: dict = field(default_factory=dict)
    last_run: str | None = None
    # The arming list: symbol -> {setup, pivot, entry, stop, score, distance_pct, source, armed_on, ...}.
    # Rebuilt by every full scan, extended by pre-market / movers screens and focused passes.
    arming: dict[str, dict] = field(default_factory=dict)
    last_full_scan: str | None = None
    # Shadow trades: setups we saw but did not take (rejected by a check, held
    # back by an entry gate, no free slot, or armed in confirmed mode without
    # a resting order). Resolved against real bars by later full scans so the
    # learning layer can tell whether a filter is earning its keep.
    shadow: list[dict] = field(default_factory=list)
    # Equity at the first pass of the current session: the daily loss
    # circuit-breaker measures today's drawdown against it.
    day_equity: dict | None = None

    @classmethod
    def load(cls, path: Path) -> "TraderState":
        if path.exists():
            raw = json.loads(path.read_text())
            known = {f.name for f in fields(cls)}
            return cls(**{k: v for k, v in raw.items() if k in known})
        return cls()

    def checkpoint(self) -> None:
        path = getattr(self, "_persistence_path", None)
        if path is not None:
            self.save(path)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(path, asdict(self))

    def arming_sorted(self) -> list[dict]:
        return sorted(self.arming.values(), key=lambda a: float(a.get("score") or 0.0), reverse=True)


@dataclass
class CycleReport:
    asof: str
    regime_ok: bool
    equity: float
    regime_note: str = ""
    actions: list[str] = field(default_factory=list)
    watchlist: list[Signal] = field(default_factory=list)
    triggered: list[Signal] = field(default_factory=list)
    plans: list[TradePlan] = field(default_factory=list)
    rejected: list[TradePlan] = field(default_factory=list)
    open_positions: list[ManagedPosition] = field(default_factory=list)
    broker: str = ""
    risk_mult: float = 1.0
    recent_r: list[float] = field(default_factory=list)
    context: dict[str, dict] = field(default_factory=dict)
    regime_known: bool = True  # False when an enabled regime input was missing (regime_ok is then False)
    regime_details: dict = field(default_factory=dict)
    regime_size_mult: float = 1.0
    data_gaps: list[str] = field(default_factory=list)  # required data that could not be sourced this cycle
    scan: str = "full"  # full | focused
    scope: list[str] = field(default_factory=list)  # symbols a focused pass looked at
    arming: list[dict] = field(default_factory=list)  # the arming list after this cycle
    skipped_far: list[str] = field(default_factory=list)  # armed flags not re-planned this pass (too far from the pivot)
    held: list[dict] = field(default_factory=list)  # triggered setups held back by the entry gates this pass
    entry_mode: str = "confirmed"
    shadows_resolved: int = 0
    # Portfolio risk this pass: open heat (fraction of equity at risk to the
    # stops), today's equity change, and entries the portfolio gates refused.
    heat_pct: float = 0.0
    day_pnl_pct: float | None = None
    risk_blocked: list[dict] = field(default_factory=list)
    halted: str | None = None  # kill-switch reason when new entries were suppressed

    def to_dict(self) -> dict:
        return {
            "asof": self.asof,
            "heat_pct": self.heat_pct,
            "day_pnl_pct": self.day_pnl_pct,
            "risk_blocked": self.risk_blocked,
            "halted": self.halted,
            "broker": self.broker,
            "scan": self.scan,
            "scope": self.scope,
            "entry_mode": self.entry_mode,
            "held": self.held,
            "shadows_resolved": self.shadows_resolved,
            "regime_ok": self.regime_ok,
            "regime_known": self.regime_known,
            "regime_note": self.regime_note,
            "regime_details": self.regime_details,
            "regime_size_mult": self.regime_size_mult,
            "data_gaps": self.data_gaps,
            "equity": self.equity,
            "risk_mult": self.risk_mult,
            "recent_r": self.recent_r,
            "context": self.context,
            "actions": self.actions,
            "watchlist": [_signal_dict(s) for s in self.watchlist],
            "triggered": [_signal_dict(s) for s in self.triggered],
            "plans": [p.to_dict() for p in self.plans],
            "rejected": [p.to_dict() for p in self.rejected],
            "open_positions": [asdict(p) for p in self.open_positions],
            "arming": self.arming,
            "skipped_far": self.skipped_far,
        }


def _signal_dict(s: Signal) -> dict:
    return {
        "symbol": s.symbol,
        "date": str(pd.Timestamp(s.date).date()),
        "setup": s.setup,
        "pivot": s.pivot,
        "entry": s.entry,
        "stop": s.stop,
        "adr_pct": s.adr_pct,
        "score": s.score,
        "details": {k: v for k, v in s.details.items() if isinstance(v, (int, float, str, bool))},
    }


def recent_r_multiples(state: TraderState, n: int = 20) -> list[float]:
    """Realised R of the most recent closed trades, oldest first."""
    return [float(r["r_multiple"]) for r in state.closed[-n:] if r.get("r_multiple") is not None]


def portfolio_heat(state: TraderState, data: dict[str, pd.DataFrame], equity: float) -> float:
    """Fraction of equity the book would lose if every open position hit its
    stop from the latest price and every resting entry filled and stopped."""
    if equity <= 0:
        return 0.0
    at_risk = 0.0
    for sym, rec in state.managed.items():
        pos = ManagedPosition(**rec)
        px = float(data[sym]["close"].iloc[-1]) if sym in data and len(data[sym]) else float(pos.entry_price)
        at_risk += max(px - float(pos.stop), 0.0) * pos.remaining
    for rec in state.pending.values():
        at_risk += max(float(rec["trigger"]) - float(rec["stop"]), 0.0) * int(rec["qty"])
    return at_risk / equity


def portfolio_gate(
    plan: TradePlan, state: TraderState, cfg: StrategyConfig, heat_pct: float, day_pnl_pct: float | None, equity: float
) -> list[str]:
    """Portfolio-level reasons not to open this position: heat cap, the daily
    loss circuit-breaker and theme concentration. Empty list = allowed."""
    r = cfg.risk
    reasons: list[str] = []
    if state.reconciliation.get("issues"):
        reasons.append("broker reconciliation incomplete; no additional risk")
    if any(not rec.get("entry_complete", True) for rec in state.managed.values()):
        reasons.append("partial entry remains working; no additional risk")
    if any(rec.get("pending_exit") for rec in state.managed.values()):
        reasons.append("exit execution unresolved; reconcile before opening more risk")
    if any(rec.get("protection_intents") for rec in state.managed.values()):
        reasons.append("protective order acknowledgement unresolved")
    if r.max_portfolio_heat_pct > 0 and equity > 0:
        added = float(plan.risk_dollars) / equity
        if heat_pct + added > r.max_portfolio_heat_pct + 1e-9:
            reasons.append(f"portfolio heat {heat_pct:.1%} + {added:.1%} would exceed the {r.max_portfolio_heat_pct:.1%} cap")
    if (state.day_equity or {}).get("loss_latched") and state.day_equity.get("date") == plan.date:
        reasons.append("daily loss limit remains latched until the next session")
    elif r.daily_loss_limit_pct > 0 and day_pnl_pct is not None and day_pnl_pct <= -r.daily_loss_limit_pct:
        reasons.append(f"daily loss limit: equity {day_pnl_pct:+.1%} today (limit -{r.daily_loss_limit_pct:.1%}); no new entries this session")
    if r.max_positions_per_theme > 0 and plan.theme:
        same = [s for s, rec in list(state.managed.items()) + list(state.pending.items()) if rec.get("theme") == plan.theme]
        if len(same) >= r.max_positions_per_theme:
            reasons.append(f"theme cap: already {len(same)} in {plan.theme} ({', '.join(sorted(same))})")
    return reasons


def submit_exit(broker: Broker, pos: ManagedPosition, qty: int, reason: str, asof: str, state: TraderState, report: CycleReport) -> bool:
    if pos.pending_exit:
        report.actions.append(f"WAIT {pos.symbol}: exit {pos.pending_exit['order_id']} awaits broker reconciliation")
        return False
    from .executions import cancel_one, capable
    if capable(broker):
        for existing in broker.open_orders():
            if existing.symbol == pos.symbol and existing.side == "sell":
                known = set(pos.exit_order_ids)
                owned = bool(set(existing.id.split(",")) & known) or existing.tag.startswith(("protective:", "target:", "qmag-"))
                if owned and not cancel_one(broker, existing.id):
                    report.actions.append(f"WAIT {pos.symbol}: protective cancellation is unconfirmed")
                    return False
    else:
        broker.cancel_orders(pos.symbol)
    if capable(broker) and pos.entry_order_id:
        held = broker.positions().get(pos.symbol)
        if (held.qty if held else 0) != pos.remaining:
            state.reconciliation.setdefault("issues", []).append(f"{pos.symbol}: fills changed while cancelling exits; reconcile before submitting")
            state.checkpoint()
            return False
    tag = "qmag-" + uuid.uuid4().hex[:24]
    if qty >= pos.remaining:
        pos.liquidation_reason = reason
    pos.pending_exit = {"order_id": "", "client_tag": tag, "qty": qty, "reason": reason, "submitted_at": asof}
    state.managed[pos.symbol] = asdict(pos)
    state.checkpoint()
    order = broker.market_sell(pos.symbol, qty, tag=tag)
    pos.exit_order_ids.extend(order.id.split(","))
    pos.pending_exit = {"order_id": order.id, "client_tag": tag, "qty": qty, "reason": reason, "submitted_at": asof}
    state.managed[pos.symbol] = asdict(pos)
    state.checkpoint()
    if order.status != "filled" or order.fill_price is None or not np.isfinite(order.fill_price):
        state.managed[pos.symbol] = asdict(pos)
        report.actions.append(f"WAIT {pos.symbol}: {reason} submitted, execution not confirmed")
        return False
    qty = min(qty, int(order.filled_qty if order.filled_qty is not None else order.qty))
    pos.pending_exit = None
    from .executions import BrokerView, poll
    if capable(broker):
        try:
            snap = poll(BrokerView(broker), state, order.id)
            if snap and snap.get("fees") is not None:
                pos.fees += snap["fees"]
        except Exception:
            pos.fees_known = False
    # Exact paper executions remain paper evidence, never live verification.
    if pos.evidence != "estimated":
        from .executions import evidence_for
        pos.evidence = evidence_for(broker)
    if qty >= pos.remaining:
        state.closed.append(pos.close_record(asof, reason, qty, float(order.fill_price)))
        state.managed.pop(pos.symbol, None)
    else:
        pos.book_sale(qty, float(order.fill_price))
        pos.remaining -= qty
        pos.partial_done = True
        state.managed[pos.symbol] = asdict(pos)
    state.checkpoint()
    report.actions.append(f"EXIT {pos.symbol}: {qty} filled @ {order.fill_price:.2f} ({reason})")
    return True


def _target_for(entry: float, stop: float, cfg: StrategyConfig) -> float | None:
    r = cfg.management.partial_target_r
    return entry + r * (entry - stop) if r and entry > stop else None


def _desired_exits(pos: ManagedPosition) -> list[Order]:
    """The sell orders that implement the plan for this position right now."""
    if pos.remaining <= 0:
        return []
    if not pos.partial_done and pos.target is not None and 0 < pos.partial_qty < pos.remaining:
        return [
            Order(id="", symbol=pos.symbol, side="sell", qty=pos.partial_qty, kind="oco", trigger=pos.stop, limit=pos.target),
            Order(id="", symbol=pos.symbol, side="sell", qty=pos.remaining - pos.partial_qty, kind="stop", trigger=pos.stop),
        ]
    return [Order(id="", symbol=pos.symbol, side="sell", qty=pos.remaining, kind="stop", trigger=pos.stop)]


def ensure_exit_orders(broker: Broker, pos: ManagedPosition, report: CycleReport, state: TraderState | None = None) -> None:
    from .executions import cancel_one, capable
    desired = _desired_exits(pos) if getattr(broker, "native_oco", True) else [Order("", pos.symbol, "sell", pos.remaining, "stop", trigger=pos.stop)]
    existing = [o for o in broker.open_orders() if o.symbol == pos.symbol and o.side == "sell"]
    if pos.protection_intents:
        from .executions import BrokerView, poll
        for intent in list(pos.protection_intents):
            matches = [o for o in existing if o.tag == intent["tag"]]
            ids = [oid for o in matches for oid in o.id.split(",")]
            if not ids and state is not None and capable(broker):
                snap = poll(BrokerView(broker), state, "", intent["tag"])
                if snap:
                    ids = [snap["id"], *snap.get("child_ids", [])]
            if ids:
                pos.exit_order_ids.extend(ids)
                pos.protection_intents.remove(intent)
        if pos.protection_intents and not (getattr(broker, "native_oco", True) is False and orders_equivalent(existing, desired)):
            report.actions.append(f"WAIT {pos.symbol}: protection acknowledgement unknown; retaining intent")
            return
        pos.protection_intents = []
        if state is not None:
            state.managed[pos.symbol] = asdict(pos)
            state.checkpoint()
    owned = lambda o: bool(set(o.id.split(",")) & set(pos.exit_order_ids)) or o.tag.startswith(("protective:", "target:", "qmag-"))
    if capable(broker) and any(not owned(o) for o in existing):
        report.actions.append(f"WAIT {pos.symbol}: an unowned sell order needs reconciliation")
        if state is not None:
            state.reconciliation.setdefault("issues", []).append(f"{pos.symbol}: unowned sell order")
        return
    if orders_equivalent(existing, desired):
        pos.exit_order_ids = list(dict.fromkeys(pos.exit_order_ids + [oid for o in existing for oid in o.id.split(",")]))
        if state is not None:
            state.managed[pos.symbol] = asdict(pos)
            state.checkpoint()
        return
    if not pos.entry_complete:
        # Stop the remaining entry before replacing protection for a partial fill.
        if not cancel_one(broker, pos.entry_order_id):
            report.actions.append(f"WAIT {pos.symbol}: partial entry cancellation pending")
            return
        pos.entry_complete = True
    for old in existing:
        owned = bool(set(old.id.split(",")) & set(pos.exit_order_ids)) or old.tag.startswith(("protective:", "target:", "qmag-"))
        if capable(broker) and (not owned or not cancel_one(broker, old.id)):
            report.actions.append(f"WAIT {pos.symbol}: protection replacement awaits owned-order cancellation")
            return
    if not capable(broker):
        broker.cancel_orders(pos.symbol)
    if capable(broker) and pos.entry_order_id:
        held = broker.positions().get(pos.symbol)
        if (held.qty if held else 0) != pos.remaining:
            report.actions.append(f"WAIT {pos.symbol}: fills changed during protection replacement")
            if state is not None:
                state.reconciliation.setdefault("issues", []).append(f"{pos.symbol}: protection resize needs fresh fills")
                state.checkpoint()
            return
    for o in desired:
        tag = "qmag-" + uuid.uuid4().hex[:24]
        pos.protection_intents.append({"tag": tag, "kind": o.kind, "qty": o.qty, "stop": o.trigger, "limit": o.limit})
        if state is not None:
            state.managed[pos.symbol] = asdict(pos)
            state.checkpoint()
        if o.kind == "oco":
            result = broker.oco_sell(pos.symbol, o.qty, o.limit, o.trigger, tag=tag)
        else:
            result = broker.stop_sell(pos.symbol, o.qty, o.trigger, tag=tag)
        pos.exit_order_ids.extend(result.id.split(","))
        pos.protection_intents = [p for p in pos.protection_intents if p["tag"] != tag]
        if state is not None:
            state.managed[pos.symbol] = asdict(pos)
            state.checkpoint()
        report.actions.append(f"EXITS {pos.symbol}: {o.kind} {o.qty} sh, stop {o.trigger:.2f}")


def _await_fill(broker: Broker, order: Order, symbol: str, timeout: float = 10.0) -> tuple[int, float] | None:
    """Wait briefly for a market order to show up as a position. Paper fills instantly."""
    if order.status == "filled" and order.fill_price is not None:
        return int(order.filled_qty if order.filled_qty is not None else order.qty), float(order.fill_price)
    if hasattr(broker, "mark"):  # paper broker: no price known yet, nothing to wait for
        return None
    wait = getattr(broker, "wait", time.sleep)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        from .executions import capable, BrokerView, TERMINAL
        if capable(broker):
            snap = BrokerView(broker).order(order.id, order.tag)
            if snap and snap.filled:
                return snap.filled, snap.average
            if snap and snap.status in TERMINAL:
                return None
            wait(1.0)
            continue
        held = broker.positions().get(symbol)
        if held is not None and held.qty > 0:
            return held.qty, held.avg_price
        wait(1.0)
    return None


def _adopt(state: TraderState, plan: PendingPlan, qty: int, avg_price: float, asof: str, cfg: StrategyConfig, evidence: str = "estimated") -> ManagedPosition:
    pos = ManagedPosition(
        symbol=plan.symbol,
        setup=plan.setup,
        entry_date=asof,
        entry_price=avg_price,
        shares=qty,
        initial_stop=plan.stop,
        stop=plan.stop,
        pivot=plan.trigger,
        remaining=qty,
        target=_target_for(avg_price, plan.stop, cfg),
        partial_qty=partial_quantity(qty, cfg.management.partial_fraction),
        theme=plan.theme,
        plan=plan.plan,
        features=plan.features,
        evidence=evidence,
        entry_order_id=plan.order_id,
        entry_complete=qty >= plan.qty,
    )
    state.managed[plan.symbol] = asdict(pos)
    state.pending.pop(plan.symbol, None)
    state.checkpoint()
    return pos


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(v) else round(v, 4)


def entry_features(
    sig: Signal,
    plan: TradePlan,
    last: pd.Series,
    entry_mode: str,
    source: str,
    scan: str,
    live: LiveClock,
    regime_ok: bool,
    regime_note: str,
    risk_mult: float,
) -> dict:
    """The conditions a trade (or shadow trade) is taken under - what the
    learning layer later buckets outcomes by. Only measured inputs; anything
    that was not available is ``None``."""
    d = sig.details
    ctx = plan.context or {}
    edge = ctx.get("edge") or {}
    rv = plan.reviewer or {}
    cm = plan.committee or {}
    entry = float(plan.entry)
    return {
        "setup": sig.setup,
        "entry_mode": entry_mode,
        "source": source,
        "scan": scan,
        "entry_time": live.time_label(),
        "session_fraction": live.fraction,
        "rvol": _num(last.get("rvol_20")),
        "rvol_projected": live.pace_applied,
        "signal_score": _num(sig.score),
        "plan_score": _num(plan.score),
        "adr_pct": _num(sig.adr_pct),
        "flag_days": d.get("flag_days"),
        "depth": _num(d.get("depth")),
        "contraction": _num(d.get("contraction")),
        "gap_pct": _num(entry / float(sig.pivot) - 1) if sig.pivot else None,
        "stop_pct": _num(1 - float(plan.stop) / entry) if entry else None,
        "gain_1m": _num(last.get("gain_1m")),
        "gain_3m": _num(last.get("gain_3m")),
        "gain_6m": _num(last.get("gain_6m")),
        "theme": plan.theme,
        "theme_pct": _num(plan.theme_pct),
        "edge_score": _num(edge.get("score")),
        "edge_coverage": _num(edge.get("coverage")),
        "context_score": _num(plan.context_score),
        "news_score": _num(ctx.get("news_score")),
        "social_score": _num(ctx.get("social_score")),
        "flow_score": _num(ctx.get("flow_score")),
        "days_to_earnings": ctx.get("days_to_earnings"),
        "reviewer_action": rv.get("action"),
        "reviewer_confidence": _num(rv.get("confidence")),
        "committee_verdict": cm.get("verdict"),
        "regime_ok": bool(regime_ok),
        "regime_note": (regime_note or "")[:160],
        "risk_mult": _num(plan.risk_mult),
        "regime_risk_multiplier": plan.notes.get("regime_risk_multiplier", 1.0),
        "checks": dict(plan.checks),
        "failed_checks": list(plan.failed_checks),
        "data_gaps": len(plan.data_gaps),
    }


def confirmation_gates(sig: Signal, last: pd.Series, cfg: StrategyConfig, live: LiveClock) -> list[str]:
    """Reasons a *triggered* setup must wait rather than be bought at market
    in ``confirmed`` / ``hybrid`` mode. Empty list = confirmed."""
    en, b = cfg.entry, cfg.breakout
    reasons: list[str] = []
    trigger = float(sig.pivot) * (1 + b.entry_buffer_pct)
    close = float(last["close"])
    if en.require_hold_above_pivot and close < trigger:
        reasons.append(f"price {close:.2f} back below the pivot {sig.pivot:.2f} (wick-only breakout)")
    if close > float(sig.pivot) * (1 + b.max_gap_pct):
        reasons.append(f"extended {close / float(sig.pivot) - 1:+.1%} above the pivot (> {b.max_gap_pct:.0%}); not chasing")
    mins = live.minutes_since_open
    if mins is not None and mins < en.opening_range_minutes:
        reasons.append(f"inside the first {en.opening_range_minutes} minutes (opening range)")
    rvol = _num(last.get("rvol_20"))
    if not live.volume_confirmed:
        reasons.append("volume pace not measurable yet (too early in the session)")
    elif rvol is None:
        reasons.append("relative volume unavailable")
    elif rvol < en.confirm_volume_ratio:
        kind = "projected" if live.pace_applied else "actual"
        reasons.append(f"{kind} volume {rvol:.2f}x < {en.confirm_volume_ratio:g}x the 20-day average")
    return reasons


def record_shadow(
    state: TraderState, cfg: StrategyConfig, kind: str, sig: Signal, plan: TradePlan, asof: str, reasons: list[str], features: dict, immediate: bool
) -> bool:
    """Remember a setup we did *not* take so its outcome can be scored later.

    ``kind``: ``rejected`` (failed a check), ``held`` (entry gate), ``no_slot``
    (portfolio full) or ``armed`` (confirmed-mode watch plan: the resting
    order we chose not to place). ``immediate`` means the setup had already
    triggered, so the shadow "fills" at ``plan.entry`` on ``asof``; otherwise
    it is a hypothetical buy-stop at ``entry`` that may never trigger. One
    open shadow per symbol / kind / pivot.
    """
    if not cfg.learning.enabled or not cfg.learning.shadow_enabled:
        return False
    from .shadow_quality import geometry_error
    if geometry_error({"entry": round(float(plan.entry), 4), "stop": round(float(plan.stop), 4),
                       "target": round(float(plan.partial_target), 4) if plan.partial_target is not None else None}):
        return False
    pivot = float(sig.pivot)
    for s in state.shadow:
        if s.get("status") == "open" and s.get("symbol") == sig.symbol and s.get("kind") == kind and abs(float(s.get("pivot") or 0) - pivot) <= 0.005 * pivot:
            return False
    state.shadow.append(
        {
            "symbol": sig.symbol,
            "kind": kind,
            "date": asof,
            "setup": sig.setup,
            "pivot": round(pivot, 4),
            "entry": round(float(plan.entry), 4),
            "stop": round(float(plan.stop), 4),
            "target": round(float(plan.partial_target), 4) if plan.partial_target else None,
            "immediate": bool(immediate),  # entry is the last close on ``date``; otherwise a buy-stop at ``entry``
            "reasons": list(reasons),
            "failed_checks": list(plan.failed_checks),
            "features": features,
            "status": "open",
        }
    )
    if len(state.shadow) > SHADOW_CAP:
        del state.shadow[: len(state.shadow) - SHADOW_CAP]
    return True


def record_near_misses(
    state: TraderState, cfg: StrategyConfig, data: dict[str, pd.DataFrame], asof: pd.Timestamp, candidates: set[str], skip: set[str], live: LiveClock, scan_label: str
) -> int:
    """The detectors one step looser on the detector-level knobs: whatever
    triggers only under the relaxed rules is a *near miss*, recorded as a
    shadow tagged with the knob(s) that excluded it. Returns how many were added."""
    from .learning import NEAR_MISS_PER_SCAN, near_miss_reasons, relaxed_config

    ln = cfg.learning
    if not (ln.enabled and ln.shadow_enabled):
        return 0
    relaxed = relaxed_config(cfg)
    if relaxed == cfg:
        return 0
    detectors = default_detectors()
    found: list[tuple[Signal, list[str]]] = []
    for sym, df in data.items():
        if sym in candidates or sym in skip or sym in cfg.auxiliary_symbols or df.index[-1] != asof:
            continue
        fired = [sig for det in detectors if (sig := det.detect_last(sym, df, relaxed)) is not None]
        if not fired:
            continue
        sig = max(fired, key=lambda s: s.score)
        last = df.iloc[-1]
        feats = {"rvol": _num(last.get("rvol_20")), "depth": _num(sig.details.get("depth")), "adr_pct": _num(sig.adr_pct)}
        reasons = near_miss_reasons(feats, cfg)
        if reasons:
            found.append((sig, reasons))
    found.sort(key=lambda t: t[0].score, reverse=True)
    added = 0
    for sig, reasons in found[:NEAR_MISS_PER_SCAN]:
        df = data[sig.symbol]
        last = df.iloc[-1]
        entry = float(last["close"])
        stop = entry - cfg.management.stop_adr_mult * float(sig.adr_dollar)
        from .shadow_quality import geometry_error
        if geometry_error({"entry": round(entry, 4), "stop": round(stop, 4),
                           "target": round(_target_for(entry, stop, cfg), 4) if _target_for(entry, stop, cfg) is not None else None}):
            continue
        pivot = float(sig.pivot)
        if any(s.get("status") == "open" and s.get("symbol") == sig.symbol and s.get("kind") == "near_miss" and abs(float(s.get("pivot") or 0) - pivot) <= 0.005 * pivot for s in state.shadow):
            continue
        state.shadow.append(
            {
                "symbol": sig.symbol, "kind": "near_miss", "date": str(pd.Timestamp(asof).date()), "setup": sig.setup, "pivot": round(pivot, 4),
                "entry": round(entry, 4), "stop": round(stop, 4), "target": round(_target_for(entry, stop, cfg), 4) if _target_for(entry, stop, cfg) else None,
                "immediate": True, "reasons": reasons, "failed_checks": [],
                "features": {
                    "setup": sig.setup, "entry_mode": "near_miss", "source": scan_label, "scan": "full", "entry_time": live.time_label(),
                    "rvol": _num(last.get("rvol_20")), "rvol_projected": live.pace_applied, "signal_score": _num(sig.score), "adr_pct": _num(sig.adr_pct),
                    "flag_days": sig.details.get("flag_days"), "depth": _num(sig.details.get("depth")), "gap_pct": _num(entry / pivot - 1) if pivot else None,
                    "stop_pct": _num(1 - stop / entry) if entry else None, "theme": sig.details.get("theme"), "theme_pct": _num(sig.details.get("theme_pct")),
                },
                "status": "open",
            }
        )
        added += 1
    if len(state.shadow) > SHADOW_CAP:
        del state.shadow[: len(state.shadow) - SHADOW_CAP]
    return added


def run_cycle(
    raw: dict[str, pd.DataFrame],
    broker: Broker,
    cfg: StrategyConfig,
    state: TraderState,
    asof: pd.Timestamp | None = None,
    gatherer: ContextGatherer | None = None,
    block_new_entries: str | None = None,
    scan_symbols: set[str] | None = None,
    regime: RegimeSnapshot | tuple[bool, str, bool] | None = None,
    full_scan: bool = True,
    pre_actions: list[str] | None = None,
    screen_hits: dict[str, dict] | None = None,
    label: str | None = None,
    live: LiveClock | None = None,
    halted: str | None = None,
    entry_guard=None,
) -> CycleReport:
    """One pass of the routine. ``block_new_entries`` is a reason (e.g. stale
    price data) that keeps exits managed but fails every new plan's
    ``price_data_fresh`` check, so nothing is opened on data we do not trust.
    ``halted`` is the kill-switch reason: exits are managed and the arming
    list is kept current, but nothing is planned, bought or left resting.

    A *full scan* (the default) looks at every frame, rebuilds the arming
    list and cancels buy-stops for names that dropped off the watchlist. A
    *focused pass* (``full_scan=False``) only detects on ``scan_symbols``,
    plans only the names close enough to trigger this session, and leaves
    resting buy-stops alone for armed names it did not re-plan. ``regime``
    is an (ok, note, known) override for passes that cannot recompute
    breadth; ``screen_hits`` are screener rows (symbol -> row with a
    ``source``) whose signals should be added to the arming list. ``live``
    carries the wall clock and the volume-pace note of a live pass; without
    it every bar is treated as complete (replays, after-close scans)."""
    live = live or LiveClock()
    mode = cfg.entry.mode
    data = prepare_data(raw, cfg)
    last_dates = [df.index[-1] for df in data.values()]
    asof = asof or max(last_dates)
    report = CycleReport(
        asof=str(pd.Timestamp(asof).date()), regime_ok=True, equity=0.0, broker=getattr(broker, "name", ""),
        scan="full" if full_scan else "focused", entry_mode=mode,
    )
    if not full_scan:
        report.scope = sorted(scan_symbols or [])
    for a in pre_actions or []:
        report.actions.append(a)
    if block_new_entries:
        report.data_gaps.append(block_new_entries)
        report.actions.append(f"DATA {block_new_entries}")
    if halted:
        report.halted = halted
        report.actions.append(f"HALT {halted} - no new entries; open positions still managed")
    mgmt = cfg.management
    sched = cfg.schedule
    en = cfg.entry
    screen_hits = screen_hits or {}
    # Buy-stop brackets may rest only in ``resting`` mode, or in ``hybrid``
    # mode during the session once the opening range is over.
    resting_allowed = mode == "resting" or (mode == "hybrid" and live.in_session and live.at_or_after(en.resting_from))
    gated = mode in ("confirmed", "hybrid")

    from .executions import cancel_pending

    # 0. A halted desk takes no new entries - including buy-stops that were
    # resting before the halt: cancel them before any fill can be booked.
    if halted:
        for sym in list(state.pending):
            if not cancel_pending(broker, state, sym):
                report.actions.append(f"WAIT {sym}: entry cancellation/reconciliation pending")
                continue
            state.pending.pop(sym)
            report.actions.append(f"CANCEL resting buy-stop {sym} (halted)")
    if state.day_equity and state.day_equity.get("date") == report.asof and state.day_equity.get("loss_latched"):
        for sym in list(state.pending):
            if not cancel_pending(broker, state, sym):
                report.actions.append(f"WAIT {sym}: entry cancellation/reconciliation pending")
                continue
            state.pending.pop(sym)
            report.actions.append(f"CANCEL {sym}: daily loss stop is latched")
    # Let the paper broker fill resting orders against the latest bars.
    if hasattr(broker, "mark"):
        bars = {s: df.iloc[-1] for s, df in data.items() if df.index[-1] == asof}
        for o in broker.mark(bars, when=asof):  # type: ignore[attr-defined]
            report.actions.append(f"FILL {o.side} {o.qty} {o.symbol} @ {o.fill_price:.2f} ({o.kind})")

    from .executions import capable, reconcile
    exact = reconcile(broker, state, cfg, report.asof, report.actions) if capable(broker) else set()
    held = broker.positions()
    for sym in exact:
        managed = state.managed.get(sym)
        if managed and int(managed["remaining"]) != int(held[sym].qty if sym in held else 0):
            state.reconciliation.setdefault("issues", []).append(f"{sym}: broker holdings differ from execution ledger")

    # 1. Reconcile our book with the broker's.
    for sym in list(state.managed):
        if sym in exact:
            continue
        if sym not in held:
            pos = ManagedPosition(**state.managed.pop(sym))
            bar = data[sym].iloc[-1] if sym in data else None
            # The broker took us out: infer the fill from the bar (stop, or target if only that was touched).
            if bar is not None and bar["low"] <= pos.stop:
                price, reason = min(float(bar["open"]), pos.stop), "stop"
            elif bar is not None and pos.target is not None and not pos.partial_done and bar["high"] >= pos.target:
                price, reason = pos.target, "target"
            else:
                price, reason = float(bar["close"]) if bar is not None else pos.stop, "manual_or_unknown"
            pos.evidence = "estimated"
            reason = (pos.pending_exit or {}).get("reason", reason)
            pos.pending_exit = None
            rec = pos.close_record(report.asof, reason, pos.remaining, price)
            state.closed.append(rec)
            broker.cancel_orders(sym)
            report.actions.append(f"CLOSED {sym} ({reason}) ~{price:.2f}, {rec['r_multiple']:+.2f}R")
    for sym, bp in held.items():
        if sym in exact:
            continue
        if sym in state.managed:
            pos = ManagedPosition(**state.managed[sym])
            if bp.qty < pos.remaining:
                pos.evidence = "estimated"
                sold = pos.remaining - bp.qty
                pos.remaining = bp.qty
                bar = data[sym].iloc[-1] if sym in data else None
                if pos.target is not None and bar is not None and bar["high"] >= pos.target and not (bar["low"] <= pos.stop):
                    pos.book_sale(sold, pos.target)
                elif bar is not None:
                    pos.book_sale(sold, float(bar["close"]))
                if not pos.partial_done:
                    pos.partial_done = True
                    if mgmt.move_stop_to_breakeven:
                        pos.stop = max(pos.stop, pos.entry_price)
                    report.actions.append(f"PARTIAL filled {sold} {sym}; {bp.qty} left, stop -> {pos.stop:.2f}")
                else:
                    report.actions.append(f"REDUCED {sym} by {sold} at broker; {bp.qty} left")
            elif bp.qty > pos.remaining:
                pos.remaining = bp.qty
                report.actions.append(f"WARN {sym} quantity grew to {bp.qty} at broker (manual add?)")
            state.managed[sym] = asdict(pos)
            continue
        plan_rec = state.pending.pop(sym, None)
        if plan_rec is None:
            report.actions.append(f"WARN {sym} held at broker but unknown to trader; leaving it alone")
            continue
        plan = PendingPlan(**plan_rec)
        pos = _adopt(state, plan, bp.qty, bp.avg_price, report.asof, cfg)
        tgt = f", target {pos.target:.2f}" if pos.target else ""
        report.actions.append(f"ADOPTED {sym}: {bp.qty} @ {bp.avg_price:.2f}, stop {plan.stop:.2f}{tgt}")

    # Unconfirmed market buys never survive a cycle: if the broker does not hold
    # the stock by now the order is gone and the plan must be re-made.
    for sym, rec in list(state.pending.items()):
        if sym in exact:
            continue
        if rec.get("entry_kind") != "buy_stop":
            if not cancel_pending(broker, state, sym):
                report.actions.append(f"WAIT {sym}: entry cancellation/reconciliation pending")
                continue
            state.pending.pop(sym)
            report.actions.append(f"CANCEL unconfirmed entry {sym}")
    # Resting buy-stops are reconciled against this pass's plans further down:
    # kept when the plan is unchanged, replaced when it moved, cancelled when
    # the name is no longer a candidate.
    prior_pending: dict[str, PendingPlan] = {s: PendingPlan(**r) for s, r in state.pending.items()}
    if prior_pending and not resting_allowed:
        # confirmed mode never rests orders; hybrid mode rests them only intraday.
        why = "confirmed mode: entries are bought at market once the breakout is confirmed" if mode == "confirmed" else "hybrid mode: no resting orders outside the session window"
        for sym in list(prior_pending):
            if not cancel_pending(broker, state, sym):
                continue
            state.pending.pop(sym, None)
            prior_pending.pop(sym)
            report.actions.append(f"CANCEL resting buy-stop {sym} ({why})")

    # 2. Manage open positions.
    trail_col = f"sma_{mgmt.trail_ma}"
    for sym, rec in list(state.managed.items()):
        df = data.get(sym)
        if df is None or sym not in held:
            continue
        pos = ManagedPosition(**rec)
        if sym in exact and any(issue.startswith(sym + ":") for issue in state.reconciliation.get("issues", [])):
            report.actions.append(f"WAIT {sym}: execution ledger and holdings must agree before further orders")
            continue
        if pos.pending_exit:
            report.actions.append(f"WAIT {sym}: exit awaiting reconciliation; inspect broker orders")
            continue
        if pos.liquidation_reason:
            submit_exit(broker, pos, pos.remaining, pos.liquidation_reason, report.asof, state, report)
            continue
        bars_held = int((df.index > pd.Timestamp(pos.entry_date)).sum())
        last = df.iloc[-1]
        remaining = pos.remaining
        if not getattr(broker, "native_oco", True) and not pos.partial_done and pos.target and float(df.iloc[-1]["close"]) >= pos.target:
            qty = partial_quantity(remaining, mgmt.partial_fraction)
            if qty > 0:
                submit_exit(broker, pos, qty, "partial_target", report.asof, state, report)
                continue
        if df.index[-1] == asof:
            pos.track_excursion(last)
            state.managed[sym] = asdict(pos)

        # Failed breakout: a fresh entry whose bar is back below the pivot is
        # cut, instead of waiting for the full stop to be hit. Judged on a
        # completed bar, or intraday once the volume pace is measurable.
        if en.failed_breakout_exit and pos.pivot and not pos.partial_done and bars_held <= en.failed_breakout_days and live.volume_confirmed:
            floor = float(pos.pivot) * (1 - en.failed_breakout_tolerance_pct)
            if float(last["close"]) < floor and pos.stop < floor:
                submit_exit(broker, pos, remaining, "failed_breakout", report.asof, state, report)
                continue

        # Time stop: no follow-through after N completed sessions -> out at the close.
        if (
            mgmt.time_stop_days > 0 and not pos.partial_done and bars_held >= mgmt.time_stop_days and live.bar_complete
            and float(last["close"]) <= pos.entry_price and pos.mfe_r < mgmt.time_stop_min_mfe_r
        ):
            submit_exit(broker, pos, remaining, "time_stop", report.asof, state, report)
            continue

        if not pos.partial_done and bars_held >= mgmt.partial_after_days and last["close"] > pos.entry_price:
            qty = partial_quantity(remaining, mgmt.partial_fraction)
            if qty > 0:
                if not submit_exit(broker, pos, qty, "partial", report.asof, state, report):
                    continue
                remaining = pos.remaining
            pos.partial_done = True
            pos.remaining = remaining
            if mgmt.move_stop_to_breakeven:
                pos.stop = max(pos.stop, pos.entry_price)
                report.actions.append(f"STOP {sym} -> {pos.stop:.2f} (breakeven)")
            state.managed[sym] = asdict(pos)
            ensure_exit_orders(broker, pos, report, state)
            continue

        trail = last.get(trail_col, np.nan)
        if (pos.partial_done and not np.isnan(trail) and last["close"] < trail) or bars_held >= mgmt.max_hold_days:
            reason = "trail_ma" if pos.partial_done else "max_hold"
            submit_exit(broker, pos, remaining, reason, report.asof, state, report)
            continue

        ensure_exit_orders(broker, pos, report, state)

    # 3. Regime (market sentiment): benchmark trend, breadth, volatility.
    regime_state = None
    if isinstance(regime, RegimeSnapshot):
        regime_state = regime
        report.regime_ok, report.regime_note, report.regime_known = regime.ok, regime.describe(cfg), regime.known
        if not regime.known:
            report.data_gaps.append("regime unknown: " + report.regime_note)
    elif regime is not None:
        report.regime_ok, report.regime_note, report.regime_known = regime
        if cfg.regime.enabled and (cfg.regime.breadth_mode == "scale" or cfg.regime.risk_off_ep_scale > 0):
            regime_state = RegimeSnapshot(False, None, None, None, None, None, ["structured inputs required for reduced-risk policy"])
            report.regime_ok, report.regime_known, report.regime_note = False, False, regime_state.describe(cfg)
        if not report.regime_known:
            gap = "regime unknown: " + (report.regime_note or "no full scan to inherit breadth from")
            report.data_gaps.append(gap)
            report.actions.append(f"DATA {gap} -> treated as risk-off")
    elif cfg.regime.enabled:
        snap = regime_snapshot(data, cfg)
        regime_state = snap
        report.regime_ok = snap.ok
        report.regime_known = snap.known
        report.regime_note = snap.describe(cfg)
        if not snap.known:
            gap = "regime unknown: " + "; ".join(snap.missing)
            report.data_gaps.append(gap)
            report.actions.append(f"DATA {gap} -> treated as risk-off")

    acct = broker.account()
    if acct.currency != "USD":
        block_new_entries = "USD account conversion unavailable"
        report.data_gaps.append(block_new_entries)
    report.equity = acct.equity
    held = broker.positions()
    exposure = sum(p.qty * (float(data[s]["close"].iloc[-1]) if s in data else p.avg_price) for s, p in held.items())
    cash = acct.cash

    # Portfolio risk: today's equity change against the first pass of the
    # session (the daily loss circuit-breaker) and the open heat.
    if not state.day_equity or state.day_equity.get("date") != report.asof:
        state.day_equity = {"date": report.asof, "equity": float(acct.equity)}
    day_open = float(state.day_equity.get("equity") or 0.0)
    report.day_pnl_pct = (acct.equity - day_open) / day_open if day_open > 0 else None
    report.heat_pct = portfolio_heat(state, data, acct.equity)
    if cfg.risk.daily_loss_limit_pct > 0 and report.day_pnl_pct is not None and report.day_pnl_pct <= -cfg.risk.daily_loss_limit_pct:
        state.day_equity["loss_latched"] = True
        report.actions.append(f"RISK daily loss limit hit: equity {report.day_pnl_pct:+.2%} since the first pass today - no new entries this session, exits still managed")

    if (state.day_equity or {}).get("loss_latched"):
        for sym in list(state.pending):
            if not cancel_pending(broker, state, sym):
                report.actions.append(f"WAIT {sym}: entry cancellation/reconciliation pending")
                continue
            state.pending.pop(sym)
            prior_pending.pop(sym, None)
    # 4. New ideas: detect, then run the full checklist and size each one.
    detectors = default_detectors()
    # Intraday, with today's volume projected to full-day pace, the detector
    # is allowed to fire at the (usually lower) confirmation ratio; the gate
    # below still demands that pace before anything is bought.
    detect_cfg = cfg
    if gated and live.pace_applied and en.confirm_volume_ratio < cfg.breakout.min_breakout_volume_ratio:
        detect_cfg = cfg.with_overrides({"breakout.min_breakout_volume_ratio": en.confirm_volume_ratio})
    triggered: list[Signal] = []
    watch: list[Signal] = []
    scanned: set[str] = set()
    for sym, df in data.items():
        if sym in cfg.auxiliary_symbols or sym in held or df.index[-1] != asof:
            continue
        if scan_symbols is not None and sym not in scan_symbols:
            continue
        scanned.add(sym)
        fired = [sig for det in detectors if (sig := det.detect_last(sym, df, detect_cfg)) is not None]
        if fired:
            triggered.append(max(fired, key=lambda s: s.score))
            continue
        idea = BreakoutDetector().watchlist(sym, df, cfg)
        if idea is not None:
            watch.append(idea)
    triggered.sort(key=lambda s: s.score, reverse=True)
    watch.sort(key=lambda s: s.score, reverse=True)
    report.triggered, report.watchlist = triggered, watch
    candidates = {s.symbol for s in triggered + watch}

    # A focused pass only spends context/LLM budget on names that can trigger
    # this session; the rest of the arming list is left as it is.
    plan_watch = watch
    def pending_policy_changed(sym: str) -> bool:
        pending = prior_pending.get(sym)
        if pending is None or regime_state is None:
            return False
        desired = regime_state.entry_scale(cfg, pending.setup)
        previous = (pending.features or {}).get("regime_risk_multiplier", 1.0)
        return desired is None or desired != previous

    if not full_scan:
        near: list[Signal] = []
        for sig in watch:
            dist = float(sig.details.get("distance_to_pivot_pct", 0.0) or 0.0) / 100.0  # detector reports percent
            if abs(dist) <= sched.trigger_distance_pct or pending_policy_changed(sig.symbol):
                near.append(sig)
            elif sig.symbol in state.arming or sig.symbol in prior_pending:
                report.skipped_far.append(sig.symbol)
        plan_watch = near
        if report.skipped_far:
            report.actions.append(f"FOCUS {len(report.skipped_far)} armed names > {sched.trigger_distance_pct:.0%} from pivot left as they are")

    # Resting buy-stops for names that are no longer candidates are cancelled
    # now; a focused pass spares names that are still armed but too far away
    # to be re-planned this pass.
    planned_symbols = {s.symbol for s in triggered + plan_watch}
    for sym, pend in list(prior_pending.items()):
        if sym in planned_symbols:
            continue
        if not full_scan and not pending_policy_changed(sym) and (sym in report.skipped_far or (sym in state.arming and sym not in scanned)):
            continue  # still armed: too far to re-plan, or no fresh bar to judge it on
        if not cancel_pending(broker, state, sym):
            continue
        state.pending.pop(sym, None)
        prior_pending.pop(sym)
        report.actions.append(f"CANCEL untriggered entry {sym}")
    # Every surviving buy-stop occupies a slot until this pass replaces or drops it.
    slots = cfg.risk.max_positions - len(held) - len(prior_pending)

    if regime_state is not None:
        report.regime_details = regime_state.to_dict()
        report.regime_size_mult = regime_state.breadth_multiplier(cfg)
        if report.regime_size_mult != 1.0:
            report.actions.append(f"REGIME weak breadth: new-entry risk x{report.regime_size_mult:.2f}; other gates still apply")

    # Adaptive risk from the trade journal.
    report.recent_r = recent_r_multiples(state)
    risk_evidence = [float(r["r_multiple"]) for r in state.closed if r.get("evidence") == "broker_verified" and _num(r.get("r_multiple")) is not None]
    risk_mult = min(1.0, adaptive_risk_multiplier(risk_evidence, cfg))  # performance alone never increases the base risk budget
    if state.policy.get("stage") == "canary":
        risk_mult *= min(1.0, max(0.01, cfg.autonomy.canary_risk_fraction))
    report.risk_mult = risk_mult
    if risk_mult != 1.0:
        report.actions.append(f"RISK x{risk_mult:.2f}: last {len(report.recent_r[-cfg.adaptive.lookback_trades:])} trades averaged {np.mean(report.recent_r[-cfg.adaptive.lookback_trades:]):+.2f}R")

    # Live context (news, social, options flow, events) for the best candidates only.
    contexts: dict[str, ContextReport] = {}
    if gatherer is not None and cfg.context.enabled and (triggered or plan_watch) and not halted:
        ranked = sorted(triggered + plan_watch, key=lambda s: s.score, reverse=True)
        try:
            contexts = gatherer.gather([s.symbol for s in ranked])
        except Exception as exc:  # pragma: no cover - never let context kill the cycle
            report.actions.append(f"WARN context gathering failed: {exc}")
    report.context = {k: v.to_dict() for k, v in contexts.items()}

    portfolio = {
        "equity": acct.equity,
        "cash": cash,
        "open_positions": len(state.managed),
        "pending_entries": len(state.pending),
        "max_positions": cfg.risk.max_positions,
        "held_symbols": sorted(state.managed),
        "recent_r_multiples": report.recent_r[-10:],
        "risk_multiplier": risk_mult,
        "broker": report.broker,
        "portfolio_heat_pct": round(report.heat_pct, 4),
        "day_pnl_pct": round(report.day_pnl_pct, 4) if report.day_pnl_pct is not None else None,
    }

    committee_budget = {"left": cfg.committee.max_plans_per_cycle}

    def risk_blocked(sig: Signal, plan: TradePlan, entry_mode: str, immediate: bool) -> bool:
        """Apply the portfolio gates; records the refusal as a shadow and returns True when blocked."""
        committed = sum(p.qty * p.trigger for s, p in prior_pending.items() if s != plan.symbol)
        resize_for_budget(plan, cfg, acct.equity, exposure + committed, cash - committed)
        reasons = portfolio_gate(plan, state, cfg, report.heat_pct, report.day_pnl_pct, acct.equity)
        if not plan.ok:
            reasons.append("remaining capital cannot fund this plan")
        current_halt = entry_guard() if entry_guard else None
        if current_halt:
            report.halted = current_halt
            reasons.append(current_halt)
        if not reasons:
            return False
        record_shadow(state, cfg, "no_slot", sig, plan, report.asof, reasons, features_for(sig, plan, entry_mode), immediate=immediate)
        report.risk_blocked.append({"symbol": sig.symbol, "setup": sig.setup, "reasons": reasons})
        report.actions.append(f"RISK {sig.symbol} ({sig.setup}) not opened: {'; '.join(reasons)}")
        return True

    def plan_for(sig: Signal, entry: float | None, triggered_now: bool) -> TradePlan:
        df = data[sig.symbol]
        # A triggered setup was judged on the bar before the trigger; re-check the same bar.
        ctx_row = df.iloc[-2] if triggered_now and len(df) > 1 else df.iloc[-1]
        plan = build_plan(
            sig, df.iloc[-1], cfg, acct.equity, exposure, cash, report.regime_ok, entry_override=entry, context_row=ctx_row,
            context=contexts.get(sig.symbol), risk_mult=risk_mult, regime_note=report.regime_note,
            context_expected=gatherer is not None and cfg.context.enabled,
            regime_state=regime_state,
        )
        if block_new_entries:
            plan.checks["price_data_fresh"] = False
            plan.data_gaps.append(block_new_entries)
        plan = apply_committee(plan, cfg, committee_budget)
        if cfg.reviewer.enabled and (triggered_now or cfg.reviewer.review_watchlist):
            before = plan.shares
            plan = apply_reviewer(
                plan, cfg, report.regime_ok, report.regime_note, portfolio,
                entry_mode="market now" if triggered_now else "buy-stop at pivot for the next session",
            )
            v = plan.reviewer or {}
            if "error" in v:
                report.actions.append(f"REVIEW {sig.symbol}: unavailable ({v['error']}) -> {plan.notes.get('reviewer')}")
            elif v:
                resized = f", size x{v['sizeMultiplier']:.2f} -> {plan.shares} sh" if plan.shares != before else ""
                report.actions.append(f"REVIEW {sig.symbol}: {v['action']} {v['confidence']:.0%} [{cfg.reviewer.mode}] {v['thesis'][:120]}{resized}")
        return plan

    if halted:
        # Nothing is planned while the kill switch is on (no context / LLM budget spent either);
        # detection still feeds the arming list so a resume picks up where it left off.
        triggered_plans: list[TradePlan] = []
        watch_plans: list[TradePlan] = []
        if triggered or plan_watch:
            report.actions.append(f"HALT {len(triggered)} triggered and {len(plan_watch)} watch setups not planned")
    else:
        triggered_plans = sorted((plan_for(sig, float(data[sig.symbol]["close"].iloc[-1]), True) for sig in triggered), key=lambda p: p.score, reverse=True)
        watch_plans = sorted((plan_for(sig, None, False) for sig in plan_watch), key=lambda p: p.score, reverse=True)
    sig_by_symbol = {s.symbol: s for s in triggered + plan_watch}
    if state.policy.get("model"):
        from .outcome_model import predict
        for plan in triggered_plans + watch_plans:
            sig = sig_by_symbol[plan.symbol]
            features = entry_features(sig, plan, data[sig.symbol].iloc[-1], mode, "model", report.scan, live, report.regime_ok, report.regime_note, risk_mult)
            expected = predict(state.policy["model"], features)
            if expected is not None:
                plan.score = float(plan.score) + expected
                plan.checks["learned_expected_return"] = expected >= -0.25
                plan.notes["predicted_r"] = round(expected, 3)
        triggered_plans.sort(key=lambda p: p.score, reverse=True)
        watch_plans.sort(key=lambda p: p.score, reverse=True)


    def drop_prior(sym: str, why: str) -> bool:
        """Cancel a resting buy-stop that this pass is replacing or abandoning; frees its slot."""
        nonlocal slots
        if sym in prior_pending:
            if not cancel_pending(broker, state, sym):
                return False
            prior_pending.pop(sym, None)
            state.pending.pop(sym, None)
            slots += 1
            state.checkpoint()
            report.actions.append(f"CANCEL untriggered entry {sym} ({why})")
        return True

    scan_label = label or ("nightly" if full_scan else "focused")

    def features_for(sig: Signal, plan: TradePlan, entry_mode: str) -> dict:
        source = str((state.arming.get(sig.symbol) or {}).get("source") or (screen_hits.get(sig.symbol) or {}).get("source") or scan_label)
        features = entry_features(sig, plan, data[sig.symbol].iloc[-1], entry_mode, source, report.scan, live, report.regime_ok, report.regime_note, risk_mult)
        features["policy_version"] = state.policy.get("id", "baseline")
        return features

    for plan in triggered_plans:
        sig = sig_by_symbol[plan.symbol]
        last = data[sig.symbol].iloc[-1]
        market_mode = "market_confirmed" if gated else "market_triggered"
        if not plan.ok:
            drop_prior(sig.symbol, "plan no longer passes")
            report.rejected.append(plan)
            record_shadow(state, cfg, "rejected", sig, plan, report.asof, plan.failed_checks, features_for(sig, plan, market_mode), immediate=True)
            if plan.failed_checks != ["market_regime"]:
                report.actions.append(f"SKIP {sig.symbol} ({sig.setup}): failed {', '.join(plan.failed_checks)}")
            continue
        if gated:
            holds = confirmation_gates(sig, last, cfg, live)
            if holds:
                report.held.append({"symbol": sig.symbol, "setup": sig.setup, "pivot": sig.pivot, "close": float(last["close"]), "rvol": _num(last.get("rvol_20")), "reasons": holds})
                record_shadow(state, cfg, "held", sig, plan, report.asof, holds, features_for(sig, plan, market_mode), immediate=True)
                report.actions.append(f"HOLD {sig.symbol} ({sig.setup}): {'; '.join(holds)} - stays armed, re-checked next pass")
                continue
        if sig.symbol in prior_pending and not drop_prior(sig.symbol, "triggered now"):
            continue
        if slots <= 0:
            record_shadow(state, cfg, "no_slot", sig, plan, report.asof, ["portfolio full"], features_for(sig, plan, market_mode), immediate=True)
            report.actions.append(f"FULL {sig.symbol}: confirmed but no free slot ({cfg.risk.max_positions} max)")
            continue
        if risk_blocked(sig, plan, market_mode, immediate=True):
            continue
        tag = "qmag-" + uuid.uuid4().hex[:24]
        pending = PendingPlan(
            sig.symbol, sig.setup, report.asof, plan.entry, plan.stop, plan.shares, "", client_tag=tag,
            entry_kind="market", target=plan.partial_target, partial_qty=plan.partial_qty, theme=plan.theme, plan=plan.to_dict(),
            features=features_for(sig, plan, market_mode),
        )
        state.pending[sig.symbol] = asdict(pending)
        state.checkpoint()
        order = broker.market_buy(sig.symbol, plan.shares, tag=tag)
        pending.order_id = order.id
        state.pending[sig.symbol] = asdict(pending)
        state.checkpoint()
        report.plans.append(plan)
        confirmed = f" [confirmed: holding above {sig.pivot:.2f}, {_num(last.get('rvol_20')) or 0:.2f}x {'projected ' if live.pace_applied else ''}volume]" if gated else ""
        report.actions.append(f"BUY {plan.summary()}{confirmed}")
        filled = _await_fill(broker, order, sig.symbol)
        if filled is None:
            state.pending[sig.symbol] = asdict(pending)
            report.actions.append(f"WAIT {sig.symbol}: market buy not yet confirmed; will adopt and protect next cycle")
        else:
            qty, avg = filled
            from .executions import evidence_for
            pos = _adopt(state, pending, qty, avg, report.asof, cfg, evidence=evidence_for(broker))
            ensure_exit_orders(broker, pos, report, state)
        exposure += plan.position_value
        cash -= plan.position_value
        slots -= 1
        report.heat_pct += float(plan.risk_dollars) / acct.equity if acct.equity > 0 else 0.0

    for plan in watch_plans:
        sig = sig_by_symbol[plan.symbol]
        if not plan.ok:
            drop_prior(sig.symbol, "plan no longer passes")
            report.rejected.append(plan)
            record_shadow(state, cfg, "rejected", sig, plan, report.asof, plan.failed_checks, features_for(sig, plan, "buy_stop"), immediate=False)
            if plan.failed_checks != ["market_regime"]:
                report.actions.append(f"SKIP {sig.symbol} (watch): failed {', '.join(plan.failed_checks)}")
            continue
        if not resting_allowed:
            # The plan is complete and armed; the buy happens at market once
            # the focused pass sees the pivot break and hold on volume.
            report.plans.append(plan)
            record_shadow(state, cfg, "armed", sig, plan, report.asof, ["no resting order in " + mode + " mode"], features_for(sig, plan, "buy_stop"), immediate=False)
            when = f"from {en.resting_from} a buy-stop is parked" if mode == "hybrid" else f"buys at market once {sig.pivot:.2f} breaks and holds on >= {en.confirm_volume_ratio:g}x volume pace"
            report.actions.append(f"ARMED {plan.summary()} -> {when}")
            continue
        if mode == "resting" and plan.score < en.resting_min_score:
            drop_prior(sig.symbol, "score below the resting-order threshold")
            report.plans.append(plan)
            report.actions.append(f"ARMED {plan.summary()} -> score {plan.score:.2f} < {en.resting_min_score:g}: no resting order, market entry on confirmation only")
            continue
        prior = prior_pending.get(sig.symbol)
        if prior is not None and _same_entry(prior, sig.entry, plan.stop, plan.shares):
            # The resting order already implements this plan: leave it in place (no churn, no extra API calls).
            prior_pending.pop(sig.symbol)
            rec = state.pending[sig.symbol]
            rec["plan"] = plan.to_dict()
            rec["target"], rec["partial_qty"], rec["theme"] = plan.partial_target, plan.partial_qty, plan.theme
            rec["features"] = features_for(sig, plan, "buy_stop")
            report.plans.append(plan)
            exposure += plan.position_value
            cash -= plan.position_value
            report.actions.append(f"KEEP buy-stop {plan.summary()}")
            continue
        if prior is not None and not drop_prior(sig.symbol, "plan changed"):
            continue
        if slots <= 0:
            record_shadow(state, cfg, "no_slot", sig, plan, report.asof, ["portfolio full"], features_for(sig, plan, "buy_stop"), immediate=False)
            continue
        if risk_blocked(sig, plan, "buy_stop", immediate=False):
            continue
        # Stop-limit: never chase a stock that gaps more than max_gap_pct over the pivot.
        limit = sig.pivot * (1 + cfg.breakout.max_gap_pct)
        tag = "qmag-" + uuid.uuid4().hex[:24]
        pending = PendingPlan(
            sig.symbol, sig.setup, report.asof, sig.entry, plan.stop, plan.shares, "", client_tag=tag,
            entry_kind="buy_stop", target=plan.partial_target, partial_qty=plan.partial_qty, theme=plan.theme, plan=plan.to_dict(),
            features=features_for(sig, plan, "buy_stop"),
        )
        state.pending[sig.symbol] = asdict(pending)
        state.checkpoint()
        order = broker.buy_stop_bracket(sig.symbol, plan.shares, sig.entry, plan.stop, limit=limit, tag=tag)
        pending.order_id = order.id
        plan.notes["entry_limit"] = order.limit
        pending.plan = plan.to_dict()
        state.pending[sig.symbol] = asdict(pending)
        state.checkpoint()
        report.plans.append(plan)
        exposure += plan.position_value
        cash -= plan.position_value
        slots -= 1
        report.heat_pct += float(plan.risk_dollars) / acct.equity if acct.equity > 0 else 0.0
        state.checkpoint()
        report.actions.append(f"PLAN buy-stop {plan.summary()}" + (f"; entry limit {order.limit:.2f}" if order.limit is not None else ""))

    if not report.regime_ok and (triggered or watch):
        ep_scale = regime_state.entry_scale(cfg, "episodic_pivot") if regime_state else None
        permitted = f"episodic pivots may enter at risk x{ep_scale:.2f}; breakouts blocked" if ep_scale is not None else "no new longs"
        report.actions.append(f"REGIME risk-off ({report.regime_note}): {permitted}")

    # 5. Arming list: the narrowed watchlist the intraday passes concentrate on.
    _update_arming(state, report, triggered, watch, held, screen_hits, cfg, full_scan, label)

    # 6. Learning: score shadow trades against the completed bars of a full
    #    scan, then record tonight's near misses (setups the detector-level
    #    thresholds excluded by one step) so those thresholds can be judged.
    if full_scan and cfg.learning.enabled and state.shadow:
        from .learning import update_shadows

        report.shadows_resolved = update_shadows(state, data, cfg, report.asof)
        if report.shadows_resolved:
            report.actions.append(f"LEARN {report.shadows_resolved} shadow trades resolved against real bars ({sum(1 for s in state.shadow if s.get('status') == 'open')} still open)")
    if full_scan and cfg.learning.enabled and live.bar_complete:
        near = record_near_misses(state, cfg, data, asof, candidates, set(held) | set(state.pending), live, scan_label)
        if near:
            report.actions.append(f"LEARN {near} near-miss setups recorded (would have triggered one step looser on the detector thresholds)")

    report.open_positions = [ManagedPosition(**r) for r in state.managed.values()]
    report.arming = state.arming_sorted()
    state.last_run = report.asof
    if full_scan:
        state.last_full_scan = report.asof
    return report


def _same_entry(prior: PendingPlan, trigger: float, stop: float, qty: int) -> bool:
    """True when a resting buy-stop already matches the freshly built plan."""
    if prior.entry_kind != "buy_stop" or prior.qty != qty:
        return False
    if abs(prior.trigger - trigger) > 0.001 * max(trigger, 1e-9):
        return False
    return abs(prior.stop - stop) <= 0.005 * max(stop, 1e-9)


def _arm_record(sig: Signal, source: str, asof: str, extra: dict | None = None, triggered: bool = False) -> dict:
    rec = {
        "symbol": sig.symbol,
        "setup": sig.setup,
        "pivot": round(float(sig.pivot), 4),
        "entry": round(float(sig.entry), 4),
        "stop": round(float(sig.stop), 4),
        "score": round(float(sig.score), 3),
        "adr_pct": round(float(sig.adr_pct), 4) if sig.adr_pct is not None else None,
        "distance_pct": round(float(sig.details.get("distance_to_pivot_pct", 0.0) or 0.0) / 100.0, 4),
        "triggered": triggered,
        "source": source,
        "armed_on": asof,
        "last_seen": asof,
    }
    if extra:
        rec.update({k: v for k, v in extra.items() if k not in rec})
    return rec


def _update_arming(
    state: TraderState,
    report: CycleReport,
    triggered: list[Signal],
    watch: list[Signal],
    held: dict,
    screen_hits: dict[str, dict],
    cfg: StrategyConfig,
    full_scan: bool,
    label: str | None,
) -> None:
    sched = cfg.schedule
    asof = report.asof
    source = label or ("nightly" if full_scan else "focused")
    seen = {s.symbol: s for s in triggered + watch}

    if full_scan:
        fresh: dict[str, dict] = {}
        for sig in triggered:
            fresh[sig.symbol] = _arm_record(sig, source, asof, triggered=True)
        for sig in watch:
            dist = abs(float(sig.details.get("distance_to_pivot_pct", 0.0) or 0.0)) / 100.0
            if dist <= sched.arming_distance_pct:
                fresh[sig.symbol] = _arm_record(sig, source, asof)
        # Same-day screener hits stay armed even if the nightly scan does not
        # rank them; names armed by hand from the lookup page stay for a week.
        for sym, rec in state.arming.items():
            if sym in fresh:
                continue
            if rec.get("source") in ("premarket", "movers") and rec.get("armed_on") == asof:
                fresh[sym] = rec
            elif rec.get("source") == "manual" and sym in seen:
                fresh[sym] = {**_arm_record(seen[sym], "manual", rec.get("armed_on", asof), triggered=sym in {s.symbol for s in triggered}), "manual": True}
            elif rec.get("source") == "manual":
                try:
                    age = len(pd.bdate_range(pd.Timestamp(rec.get("armed_on") or asof), pd.Timestamp(asof))) - 1
                except (ValueError, TypeError):
                    age = 99
                if age <= 5:
                    fresh[sym] = rec
        for sym, rec in fresh.items():
            old = state.arming.get(sym)
            if old is not None and old.get("source") == rec.get("source"):
                rec["armed_on"] = old.get("armed_on", asof)
        ranked = sorted(fresh.values(), key=lambda a: float(a.get("score") or 0.0), reverse=True)
        dropped = set(state.arming) - set(fresh)
        state.arming = {a["symbol"]: a for a in ranked[: sched.arming_max_names]}
        report.actions.append(f"ARM {len(state.arming)} names within {sched.arming_distance_pct:.0%} of a pivot (cap {sched.arming_max_names})")
        if dropped:
            report.actions.append(f"ARM dropped {', '.join(sorted(dropped))}")
    else:
        triggered_syms = {s.symbol for s in triggered}
        for sym, sig in seen.items():
            rec = state.arming.get(sym)
            if rec is not None:
                rec.update(_arm_record(sig, rec.get("source", source), rec.get("armed_on", asof), triggered=sym in triggered_syms))
                rec["last_seen"] = asof
            elif sym in screen_hits:
                hit = screen_hits[sym]
                state.arming[sym] = _arm_record(sig, str(hit.get("source") or source), asof, extra={"screen": hit}, triggered=sym in triggered_syms)
                report.actions.append(f"ARM {sym} from {hit.get('source', source)} screen ({sig.setup}, score {sig.score:.1f})")

    # Filled names leave the arming list; positions are managed, not armed.
    for sym in list(state.arming):
        if sym in held or sym in state.managed:
            state.arming.pop(sym)
    # Screener hits that produced no setup are noted but not armed: nothing is invented.
    if screen_hits:
        no_setup = [s for s in screen_hits if s not in seen and s not in held]
        if no_setup:
            report.actions.append(f"SCREEN {len(no_setup)} hits without a valid setup: {', '.join(sorted(no_setup)[:12])}")
