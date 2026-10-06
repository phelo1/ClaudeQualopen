"""Broker order snapshots and idempotent execution reconciliation.

Snapshots contain cumulative quantities/notional, never inferred bar prices.
The persisted book survives repeated polls and reconnects; unknown is distinct
from cancelled. Optional SDK imports stay inside their respective adapters.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import math
import time


TERMINAL = {"filled", "cancelled", "rejected", "expired"}


def status(value) -> str:
    value = str(getattr(value, "value", value)).lower().replace("_", "")
    return {"filled": "filled", "canceled": "cancelled", "cancelled": "cancelled",
            "apicancelled": "cancelled", "rejected": "rejected", "inactive": "rejected",
            "expired": "expired", "partiallyfilled": "partial", "partial": "partial"}.get(value, "open")


@dataclass
class OrderSnapshot:
    id: str
    symbol: str
    side: str
    requested: int
    filled: int = 0
    average: float | None = None
    status: str = "open"
    tag: str = ""
    fees: float | None = None  # unknown fees are never silently converted to zero
    currency: str = "USD"
    updated_at: str | None = None
    child_ids: list[str] = field(default_factory=list)
    permanent_id: int = 0

    @property
    def notional(self) -> float:
        return self.filled * float(self.average or 0)

    def validate(self) -> None:
        if self.filled < 0 or self.requested < self.filled:
            raise ValueError(f"Invalid cumulative execution quantity for {self.id}")
        if self.filled and (self.average is None or not math.isfinite(self.average) or self.average <= 0):
            raise ValueError(f"Missing execution price for {self.id}")
        if self.fees is not None and not math.isfinite(self.fees):
            raise ValueError(f"Invalid execution fees for {self.id}")


class BrokerView:
    """A single poll. IBKR data is requested once per view, not once per order."""
    def __init__(self, broker):
        self.broker = broker
        self.name = str(getattr(broker, "name", ""))
        self._ib_trades = None
        self._ib_fills = None

    def order(self, order_id: str = "", tag: str = "", permanent_id: int = 0) -> OrderSnapshot | None:
        b = self.broker
        if hasattr(b, "order_snapshot"):
            result = b.order_snapshot(order_id, tag)
        elif self.name == "paper":
            rows = [r for r in b.ledger.orders if (r["id"] == order_id if order_id else r.get("tag") == tag)]
            if not rows:
                return None
            r = rows[-1]
            result = OrderSnapshot(r["id"], r["symbol"], r["side"], r["qty"],
                                   r.get("filled_qty") if r.get("filled_qty") is not None else (r["qty"] if r["status"] == "filled" else 0),
                                   r.get("fill_price"), status(r["status"]), r.get("tag", ""), r.get("fees") or 0.0,
                                   updated_at=r.get("filled_at"), child_ids=[x["id"] for x in b.ledger.orders if x.get("parent_id") == r["id"]])
        elif self.name.startswith("alpaca"):
            result = self._alpaca(order_id, tag)
        elif self.name.startswith("ibkr"):
            result = self._ibkr(order_id, tag, permanent_id)
        elif self.name.startswith("mt5"):
            result = self._mt5(order_id, tag)
        else:
            return None
        if result is not None:
            result.validate()
        return result

    def _alpaca(self, order_id, tag):
        b = self.broker
        # Errors (including not-found during eventual consistency) propagate:
        # callers retain the intent and retry; they never assume no order exists.
        o = b.client.get_order_by_id(order_id) if order_id else b.client.get_order_by_client_id(tag)
        side = str(getattr(o.side, "value", o.side))
        return OrderSnapshot(str(o.id), o.symbol, side, int(float(o.qty or 0)), int(float(o.filled_qty or 0)),
                             float(o.filled_avg_price) if o.filled_avg_price else None, status(o.status),
                             o.client_order_id or "", updated_at=str(o.updated_at) if o.updated_at else None,
                             child_ids=[str(x.id) for x in (o.legs or [])])

    def _ibkr(self, order_id, tag, permanent_id=0):
        b = self.broker
        if self._ib_trades is None:
            evidence = getattr(b, 'evidence', None)
            saved_trades, saved_fills = evidence.observations() if evidence else ([], [])
            completed = []
            if not getattr(b, 'history_error', None):
                old_timeout = getattr(b.ib, 'RequestTimeout', 20)
                try:
                    b.ib.RequestTimeout = 5
                    completed = list(b.ib.reqCompletedOrders(apiOnly=True))
                except TimeoutError:
                    b.history_error = 'Completed-order history timed out; using retained broker callbacks and current executions. Unknown orders remain unresolved.'
                finally:
                    b.ib.RequestTimeout = old_timeout
            self._ib_trades = saved_trades + list(b.ib.trades()) + completed
            self._ib_fills = saved_fills + list(b.ib.reqExecutions())
        account = getattr(b, "account_id", None)
        def owned_fill(f):
            return f.execution.clientId == b.client_id and (not account or getattr(f.execution, "acctNumber", None) == account)
        owned_fills = [f for f in self._ib_fills if owned_fill(f)]
        permanent_ids = {getattr(f.execution, "permId", 0) for f in owned_fills if str(f.execution.orderId) == order_id}
        if order_id.startswith("perm:"):
            permanent_ids.add(int(order_id.split(":", 1)[1]))
        permanent_ids.discard(0)
        if permanent_id:
            permanent_ids = {permanent_id}
        if not permanent_ids and order_id:
            permanent_ids = {getattr(t.order, 'permId', 0) for t in self._ib_trades
                             if t.order.clientId == b.client_id and str(t.order.orderId) == order_id
                             and (not account or getattr(t.order, 'account', None) == account)} - {0}
        def matches(t):
            o = t.order
            if account and getattr(o, "account", None) != account:
                return False
            if o.clientId == b.client_id and o.orderId:
                return str(o.orderId) == order_id if order_id else bool(tag and o.orderRef == tag)
            # Completed-order callbacks reset clientId/orderId to zero. Recover
            # via account-scoped executions or the unique persisted intent tag.
            return bool(account and not o.orderId and not o.clientId and
                        (getattr(o, "permId", 0) in permanent_ids if permanent_ids else (tag and o.orderRef == tag)))
        trades = [t for t in self._ib_trades if matches(t)]
        if not trades:
            return None  # historical API coverage is finite; keep persisted state
        identities = {getattr(t.order, "permId", 0) for t in trades} - {0}
        if len(identities) > 1:
            raise ValueError("Ambiguous IBKR execution identity")
        t = trades[-1]
        o, st = t.order, t.orderStatus
        # execId de-duplicates executions returned through multiple IB callbacks.
        fills = {f.execution.execId: f for f in owned_fills
                 if (getattr(o, "permId", 0) and getattr(f.execution, "permId", 0) == o.permId) or
                    (o.orderId and f.execution.orderId == o.orderId and f.execution.clientId == o.clientId)}
        # A fresh execution response may precede its commission callback.
        for f in owned_fills:
            prior = getattr(f, 'commissionReport', None)
            current = fills.get(f.execution.execId)
            report = getattr(current, 'commissionReport', None)
            if current and prior and prior.currency and (not report or not report.currency):
                fills[f.execution.execId] = f  # SDK Fill is an immutable named tuple
        def quantity(value):
            value = float(value or 0)
            return int(value) if math.isfinite(value) and 0 <= value < 1e15 else 0
        qty = max(quantity(st.filled), quantity(getattr(o, "filledQuantity", 0)),
                  max((quantity(getattr(f.execution, "cumQty", 0)) for f in fills.values()), default=0))
        avg = float(st.avgFillPrice) if qty and st.avgFillPrice else None
        if qty and avg is None:
            cumulative = [f for f in fills.values() if quantity(getattr(f.execution, "cumQty", 0)) == qty]
            if cumulative:
                avg = float(cumulative[-1].execution.avgPrice)
            elif sum(float(f.execution.shares) for f in fills.values()) == qty:
                avg = sum(float(f.execution.shares)*float(f.execution.price) for f in fills.values())/qty
        original_id = order_id or (str(o.orderId) if o.orderId else
                      next((str(f.execution.orderId) for f in fills.values()), f"perm:{getattr(o, 'permId', 0)}"))
        commissions = [getattr(f, "commissionReport", None) for f in fills.values()]
        complete = bool(fills) and sum(float(f.execution.shares) for f in fills.values()) == qty
        complete = complete and all(c and c.currency == "USD" and math.isfinite(c.commission)
                                    and abs(c.commission) < 1e20 for c in commissions)
        fees = sum(c.commission for c in commissions) if complete else None
        children = [str(x.order.orderId) for x in self._ib_trades if o.orderId and x.order.orderId and x.order.parentId == o.orderId and x.order.clientId == o.clientId]
        if getattr(o, "ocaGroup", ""):
            children += [str(x.order.orderId) for x in self._ib_trades if x.order.orderId and getattr(x.order, "ocaGroup", "") == o.ocaGroup and x.order.clientId == o.clientId and x.order.orderId != o.orderId]
        requested = max(quantity(o.totalQuantity), qty)
        return OrderSnapshot(original_id, t.contract.symbol, o.action.lower(), requested,
                             qty, avg, 'filled' if requested and qty == requested else status(st.status), o.orderRef or "", fees,
                             updated_at=max((str(f.time) for f in fills.values()), default=None), child_ids=list(dict.fromkeys(children)), permanent_id=getattr(o, 'permId', 0))

    def _mt5(self, order_id, tag):
        b, now = self.broker, datetime.now(timezone.utc)
        if order_id:
            rows = b.mt5.orders_get(ticket=int(order_id))
            if rows is None:
                raise RuntimeError(f"MT5 order read failed: {b.mt5.last_error()}")
            if not rows:
                rows = b.mt5.history_orders_get(ticket=int(order_id))
        else:
            rows = b.mt5.history_orders_get(now - timedelta(days=30), now)
            active = b.mt5.orders_get()
            if rows is not None and active is not None:
                rows = [*rows, *active]
                rows = [r for r in rows if r.magic == b.magic and r.comment == tag]
        if rows is None:
            raise RuntimeError(f"MT5 history unavailable: {b.mt5.last_error()}")
        if not rows:
            return None
        o = rows[-1]
        deals = b.mt5.history_deals_get(ticket=o.ticket)
        if deals is None:
            raise RuntimeError(f"MT5 deals unavailable: {b.mt5.last_error()}")
        deals = [d for d in deals if d.type in (b.mt5.DEAL_TYPE_BUY, b.mt5.DEAL_TYPE_SELL)]
        qty = sum(b._shares(o.symbol, d.volume) for d in deals)
        avg = sum(b._shares(o.symbol, d.volume) * d.price for d in deals) / qty if qty else None
        states = {b.mt5.ORDER_STATE_FILLED: "filled", b.mt5.ORDER_STATE_CANCELED: "cancelled",
                  b.mt5.ORDER_STATE_REJECTED: "rejected", b.mt5.ORDER_STATE_EXPIRED: "expired",
                  b.mt5.ORDER_STATE_PARTIAL: "partial"}
        fee = -sum(d.commission + d.fee + d.swap for d in deals) if deals else None
        currency = str(b.mt5.account_info().currency)
        return OrderSnapshot(str(o.ticket), b._plain(o.symbol), "buy" if o.type % 2 == 0 else "sell",
                             b._shares(o.symbol, o.volume_initial), qty, avg, states.get(o.state, "open"),
                             o.comment, fee if currency == "USD" else None, currency,
                             updated_at=datetime.fromtimestamp(o.time_done or o.time_setup, timezone.utc).isoformat())

    def related_exits(self, entry_id: str) -> list[str]:
        if not self.name.startswith("mt5"):
            return []
        b = self.broker
        entries = b.mt5.history_deals_get(ticket=int(entry_id))
        if entries is None:
            raise RuntimeError("MT5 entry deals unavailable")
        result = set()
        for position_id in {d.position_id for d in entries}:
            deals = b.mt5.history_deals_get(position=position_id)
            if deals is None:
                raise RuntimeError("MT5 position deals unavailable")
            result.update(str(d.order) for d in deals if d.type == b.mt5.DEAL_TYPE_SELL)
        return sorted(result)


def capable(broker) -> bool:
    return hasattr(broker, "order_snapshot") or str(getattr(broker, "name", "")).startswith(("paper", "alpaca", "ibkr", "mt5"))


def evidence_for(broker) -> str:
    name = str(getattr(broker, "name", ""))
    return "paper_fill" if name == "paper" or "paper" in name or getattr(broker, "paper", False) else "broker_verified"


def cancel_one(broker, order_id: str) -> bool:
    """Cancel the named order and require a terminal acknowledgement."""
    ids = order_id.split(",")
    if len(ids) > 1:
        return all([cancel_one(broker, oid) for oid in ids])
    if order_id.startswith("sl:"):
        return True  # MT5 protection is attached to the position; never remove it
    snapshot = BrokerView(broker).order(order_id)
    if snapshot and snapshot.status in TERMINAL:
        return True
    name = str(getattr(broker, "name", ""))
    if hasattr(broker, "cancel_order"):
        broker.cancel_order(order_id)
    elif name == "paper":
        for row in broker.ledger.orders:
            if row["id"] == order_id and row["status"] == "open":
                row["status"] = "cancelled"
        broker._save()
    elif name.startswith("alpaca"):
        broker.client.cancel_order_by_id(order_id)
    elif name.startswith("ibkr"):
        trade = next((t for t in broker.ib.openTrades() if str(t.order.orderId) == order_id), None)
        if trade is not None:
            broker.ib.cancelOrder(trade.order)
    elif name.startswith("mt5"):
        broker._send({"action": broker.mt5.TRADE_ACTION_REMOVE, "order": int(order_id)})
    else:
        return False
    wait = getattr(broker, "wait", time.sleep)
    for _ in range(5):
        snap = BrokerView(broker).order(order_id)
        if snap and snap.status in TERMINAL:
            return True
        wait(0.2)
    return False


def poll(view: BrokerView, state, order_id: str, tag: str = "") -> dict | None:
    prior = state.executions.get(order_id)
    kwargs = {'permanent_id': (prior or {}).get('permanent_id', 0)} if view.name.startswith('ibkr') else {}
    snapshot = view.order(order_id, tag or (prior or {}).get("tag", ""), **kwargs)
    if snapshot is None:
        return prior if prior and prior.get("status") in TERMINAL else None
    row = asdict(snapshot)
    prior = state.executions.get(snapshot.id)
    if prior and snapshot.filled < prior["filled"]:
        raise ValueError(f"Execution quantity regressed for {snapshot.id}; reconciliation required")
    if prior and snapshot.filled == prior['filled'] and row['fees'] is None:
        row['fees'] = prior.get('fees')
    state.executions[snapshot.id] = row
    return row


def cancel_pending(broker, state, symbol: str) -> bool:
    """An unresolved or partly-filled entry cannot be discarded as unfilled."""
    row = state.pending.get(symbol)
    if not row:
        return True
    if not capable(broker):
        broker.cancel_orders(symbol)
        return True
    snap = poll(BrokerView(broker), state, row["order_id"], row.get("client_tag", ""))
    if snap is None:
        return False
    row["order_id"] = snap["id"]
    if snap["status"] not in TERMINAL and not cancel_one(broker, snap["id"]):
        return False
    final = poll(BrokerView(broker), state, snap["id"])
    return bool(final and final["status"] in TERMINAL and final["filled"] == 0)


def reconcile(broker, state, cfg, asof, actions) -> set[str]:
    """Rebuild tracked quantities and cash flows from cumulative broker records.

    Returns symbols governed by this reconciler, including unresolved ones.
    Legacy records without entry order IDs remain explicitly estimated.
    """
    from .trader import ManagedPosition, PendingPlan, _adopt
    view = BrokerView(broker)
    protected: set[str] = set()
    problems = []
    for symbol, record in list(state.pending.items()):
        protected.add(symbol)
        try:
            order = poll(view, state, record["order_id"], record.get("client_tag", ""))
            if order is None:
                problems.append(f"{symbol}: entry acknowledgement unknown; retaining intent")
                continue
            record["order_id"] = order["id"]
            if order["filled"]:
                plan = PendingPlan(**record)
                entry_date = str(order.get("updated_at") or asof)[:10]
                pos = _adopt(state, plan, order["filled"], order["average"], entry_date, cfg,
                             evidence=evidence_for(broker))
                pos.entry_order_id = order["id"]
                pos.entry_complete = order["status"] in TERMINAL
                pos.exit_order_ids = order.get("child_ids", [])
                state.managed[symbol] = asdict(pos)
                actions.append(f"RECONCILE {symbol}: {order['filled']} entry shares confirmed")
            elif order["status"] in TERMINAL:
                state.pending.pop(symbol, None)
                actions.append(f"RECONCILE {symbol}: entry {order['status']}")
        except Exception as exc:
            problems.append(f"{symbol}: entry reconciliation {type(exc).__name__}")
    for symbol, record in list(state.managed.items()):
        if not record.get("entry_order_id"):
            if view.name != "paper":
                protected.add(symbol)
                problems.append(f"{symbol}: legacy position lacks entry execution identity; reconcile ownership before new risk")
            continue
        protected.add(symbol)
        pos = ManagedPosition(**record)
        try:
            buy = poll(view, state, pos.entry_order_id)
            if buy is None:
                problems.append(f"{symbol}: entry history unavailable")
                continue
            ids = set(pos.exit_order_ids) | set(buy.get("child_ids", [])) | set(view.related_exits(pos.entry_order_id))
            if pos.pending_exit and not pos.pending_exit.get("order_id"):
                recovered = poll(view, state, "", pos.pending_exit.get("client_tag", ""))
                if recovered is None:
                    problems.append(f"{symbol}: exit acknowledgement unknown; retaining intent")
                else:
                    pos.pending_exit["order_id"] = recovered["id"]
            if pos.pending_exit and pos.pending_exit.get("order_id"):
                ids.update(pos.pending_exit["order_id"].split(","))
            sells = []
            for oid in sorted(ids):
                if oid.startswith("sl:"):
                    continue
                row = poll(view, state, oid)
                if row is None:
                    problems.append(f"{symbol}: exit {oid} is unresolved")
                    continue
                # Bracket parent/child responses may include both buy and sell rows.
                if row["side"] == "sell":
                    sells.append(row)
            sold = sum(r["filled"] for r in sells)
            if sold > buy["filled"]:
                raise ValueError("exit fills exceed recorded entries")
            previously_sold = pos.shares-pos.remaining
            if not previously_sold and (pos.shares != buy["filled"] or pos.entry_price != buy["average"]):
                from .plan import partial_quantity
                target_r = (pos.target-pos.entry_price)/pos.risk_per_share if pos.target is not None else None
                pos.target = buy["average"] + target_r*(buy["average"]-pos.initial_stop) if target_r is not None else None
                pos.partial_qty = partial_quantity(buy["filled"], cfg.management.partial_fraction)
            pos.entry_price, pos.shares = buy["average"], buy["filled"]
            pos.remaining = buy["filled"] - sold
            if sold > previously_sold and pos.remaining > 0:
                actions.append(f"PARTIAL filled {sold-previously_sold} {symbol}; {pos.remaining} left, verified executions")
            pos.entry_complete = buy["status"] in TERMINAL
            pos.realised = sum(r["filled"] * (r["average"] or 0) for r in sells)
            filled_orders = [r for r in [buy, *sells] if r["filled"]]
            pos.fees_known = all(r.get("fees") is not None for r in filled_orders)
            pos.fees = sum(r.get("fees") or 0 for r in filled_orders)
            pos.exit_order_ids = sorted(ids)
            if sold:
                pos.partial_done = True
                if cfg.management.move_stop_to_breakeven:
                    pos.stop = max(pos.stop, pos.entry_price)
            if pos.pending_exit:
                oid = pos.pending_exit.get("order_id", "")
                row = next((r for r in sells if r["id"] == oid), None)
                if row and row["status"] in TERMINAL:
                    pos.pending_exit = None  # rejected/cancelled exits can be safely replanned
            if pos.remaining == 0 and pos.entry_complete:
                if any(p.startswith(f"{symbol}:") for p in problems):
                    state.managed[symbol] = asdict(pos)
                    continue
                # A fully closed position must not leave a sell order behind.
                if not all(cancel_one(broker, r["id"]) for r in sells if r["status"] not in TERMINAL):
                    problems.append(f"{symbol}: waiting for residual exit cancellation")
                    state.managed[symbol] = asdict(pos)
                    continue
                last = next((r for r in reversed(sells) if r["filled"]), None)
                if last:
                    closed_on = str(last.get('updated_at') or asof)[:10]
                    rec = pos.close_record(closed_on, "broker_execution", 0, last["average"])
                    state.closed.append(rec)
                    state.managed.pop(symbol, None)
                    actions.append(f"CLOSED {symbol}: reconciled broker fills, {rec['pnl']:+.2f} USD before any unreported costs")
            else:
                state.managed[symbol] = asdict(pos)
        except Exception as exc:
            problems.append(f"{symbol}: execution reconciliation {type(exc).__name__}")
    # Commissions can arrive after the execution callback or after a close.
    for rec in state.closed[-200:]:
        if not rec.get("entry_order_id") or rec.get("fees_known"):
            continue
        try:
            rows = [poll(view, state, oid) for oid in [rec["entry_order_id"], *rec.get("exit_order_ids", [])]
                    if not oid.startswith("sl:")]
            if all(row is not None for row in rows):
                filled = [row for row in rows if row["filled"]]
                if filled and all(row.get("fees") is not None for row in filled):
                    fees = sum(row["fees"] for row in filled)
                    rec["pnl"] = round(rec["pnl"] + rec.get("fees", 0) - fees, 2)
                    rec["fees"], rec["fees_known"] = fees, True
                    risk = rec["shares"] * max(rec["entry_price"] - rec["initial_stop"], 1e-9)
                    rec["r_multiple"] = round(rec["pnl"] / risk, 3)
                    cost = rec["shares"] * rec["entry_price"]
                    rec["pnl_pct"] = round(rec["pnl"] / cost, 4) if cost else 0.0
        except Exception:
            pass  # retain explicit unknown-cost provenance, retry on later polls
    state.reconciliation = {"asof": asof, "issues": problems, "ok": not problems}
    actions.extend(f"RECONCILIATION {p}" for p in problems)
    state.checkpoint()
    return protected
