"""Order-routing tests for the connections page and the CLI.

A broker "connection OK" only proves the key works. These tests exercise the
same order calls the trader makes, end to end, and report every step:

* **bracket** - place a 1-share buy-stop bracket above the market, confirm
  the broker acknowledges it, cancel it, confirm it is gone. A price jump
  can still trigger it; this exercises the order structure the
  ``resting`` / ``hybrid`` entry modes rely on, and where IBKR / MT5 differ
  most from Alpaca.
* **fill** - 1-share market buy, confirm the position, market sell, confirm
  flat. Paper accounts only (``allow_live`` is the explicit CLI override);
  real brokers need regular trading hours for the market orders to fill.

Every test is tagged ``qmag-test`` so it can be told apart from trading, and
the symbol is refused if the trader holds or has an order resting on it.
Results are recorded on the ``broker_orders`` connection and kept in
``order_tests.json`` in the state directory.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import pandas as pd

from .market_calendar import NY
from .persistence import serialized

if TYPE_CHECKING:  # pragma: no cover
    from .session import TradingSession

TEST_TAG = "qmag-test"
RESULTS_FILE = "order_tests.json"
KINDS = ("bracket", "fill")
DEFAULT_SYMBOL = "SPY"


class _Steps:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def run(self, name: str, fn: Callable[[], Any], detail: Callable[[Any], str] | str = "") -> Any:
        t0 = time.perf_counter()
        try:
            out = fn()
        except Exception as exc:
            self.rows.append({"name": name, "ok": False, "detail": f"{type(exc).__name__}: {exc}", "ms": round((time.perf_counter() - t0) * 1000)})
            raise
        text = detail(out) if callable(detail) else detail
        self.rows.append({"name": name, "ok": True, "detail": text, "ms": round((time.perf_counter() - t0) * 1000)})
        return out

    def fail(self, name: str, why: str) -> None:
        self.rows.append({"name": name, "ok": False, "detail": why, "ms": 0})
        raise RuntimeError(why)


def _wait(broker: Any, seconds: float) -> None:
    getattr(broker, "wait", time.sleep)(seconds)


def _no_open_orders(broker: Any, symbol: str, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        left = [o for o in broker.open_orders() if o.symbol == symbol]
        if not left:
            return True
        if time.monotonic() >= deadline:
            return False
        _wait(broker, 1.0)


@serialized
def run_order_test(session: "TradingSession", kind: str = "bracket", symbol: str | None = None, allow_live: bool = False, now: datetime | None = None) -> dict:
    """Run one order test against the session's broker and record the result."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    symbol = (symbol or DEFAULT_SYMBOL).upper().strip()
    s = session.s
    steps = _Steps()
    result: dict[str, Any] = {
        "kind": kind, "symbol": symbol, "broker": s.broker, "live": bool(s.live), "started_at": pd.Timestamp.now("UTC").isoformat(timespec="seconds"),
        "steps": steps.rows, "ok": False, "error": None, "note": None,
    }
    try:
        if kind == "fill" and s.live and not allow_live:
            steps.fail("guard", "the fill test buys and sells real shares; it is disabled on live accounts (CLI: --allow-live)")
        halted = session.halted()
        if halted:
            steps.fail("guard", f"{halted}; no orders of any kind while the kill switch is on (qmag resume)")
        state = session.state()
        if symbol in state.managed or symbol in state.pending or symbol in state.arming:
            steps.fail("guard", f"{symbol} is held, pending or armed by the trader; pick another test symbol")
        broker = steps.run("connect", lambda: session.broker, lambda b: f"{getattr(b, 'name', s.broker)} ({'LIVE' if s.live else 'paper'})")
        acct = steps.run("account", broker.account, lambda a: f"equity {a.equity:,.2f}, cash {a.cash:,.2f}")
        held = broker.positions()
        if symbol in held:
            steps.fail("guard", f"{symbol} is already held at the broker ({held[symbol].qty} sh); the test needs a symbol you do not own")
        if any(o.symbol == symbol for o in broker.open_orders()):
            steps.fail("guard", f"{symbol} already has open orders at the broker; cancel them first or pick another symbol")

        from .session import load_frames

        def _bars():
            frames, _ = load_frames(s.data, s.csv_dir, None, symbol, session.cfg, None, None, max_age_hours=1.0, cache_dir=s.cache_dir, stats={})
            if symbol not in frames or frames[symbol].empty:
                raise RuntimeError(f"no bars for {symbol} from {s.data}")
            return frames[symbol]

        df = steps.run("reference price", _bars, lambda d: f"last close {float(d['close'].iloc[-1]):.2f} on {d.index[-1].date()} ({s.data})")
        last = float(df["close"].iloc[-1])
        if hasattr(broker, "mark"):  # the paper ledger needs a last price to fill market orders
            broker.mark({symbol: df.iloc[-1]})

        if kind == "bracket":
            trigger, stop_loss, limit = round(last * 1.5, 2), round(last * 1.3, 2), round(last * 1.53, 2)
            order = steps.run(
                "place buy-stop bracket", lambda: broker.buy_stop_bracket(symbol, 1, trigger, stop_loss, limit=limit, tag=f"{TEST_TAG}:bracket"),
                lambda o: f"order {o.id}: 1 sh stop {trigger:.2f} limit {limit:.2f}, protective stop {stop_loss:.2f} (50% above the market - cannot fill)",
            )

            def _seen():
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    mine = [o for o in broker.open_orders() if o.symbol == symbol]
                    if mine:
                        return mine
                    _wait(broker, 1.0)
                raise RuntimeError("the broker does not list the order as open")

            steps.run("broker lists it", _seen, lambda m: f"{len(m)} open order(s) for {symbol}: " + ", ".join(f"{o.kind} {o.side} {o.qty}" for o in m))
            steps.run("cancel", lambda: broker.cancel_orders(symbol), lambda n: f"{n} order(s) cancelled")

            def _gone() -> str:
                if not _no_open_orders(broker, symbol):
                    raise RuntimeError(f"{symbol} still has open orders after cancel")
                return "no open orders left"

            steps.run("confirm cancelled", _gone, lambda t: t)
            result["note"] = f"order {order.id} placed and cancelled; nothing filled, equity unchanged at {acct.equity:,.2f}"
        else:
            if not hasattr(broker, "mark"):
                from .trader import LiveClock

                clock = LiveClock(now=now or datetime.now(NY))
                if not clock.in_session:
                    steps.fail("market hours", "the fill test needs regular trading hours for the market orders to fill")
            from .trader import _await_fill

            order = steps.run("market buy 1 share", lambda: broker.market_buy(symbol, 1, tag=f"{TEST_TAG}:fill"), lambda o: f"order {o.id} {o.status}")
            filled = _await_fill(broker, order, symbol, timeout=20.0)
            if filled is None:
                broker.cancel_orders(symbol)
                if symbol in broker.positions():
                    broker.market_sell(symbol, broker.positions()[symbol].qty, tag=f"{TEST_TAG}:cleanup")
                steps.fail("fill confirmed", "no fill reported within 20 s; order cancelled and any position flattened")
            qty, avg = filled
            steps.rows.append({"name": "fill confirmed", "ok": True, "detail": f"{qty} sh @ {avg:.2f}", "ms": 0})
            steps.run("position visible", lambda: broker.positions().get(symbol), lambda p: f"{p.qty} sh @ {p.avg_price:.2f}" if p else "not visible")
            sell = steps.run("market sell", lambda: broker.market_sell(symbol, qty, tag=f"{TEST_TAG}:flatten"), lambda o: f"order {o.id} {o.status}")

            def _flat() -> str:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if symbol not in broker.positions():
                        return "position closed"
                    _wait(broker, 1.0)
                raise RuntimeError(f"{symbol} still held after the sell ({sell.id}); close it manually")

            steps.run("flat again", _flat, lambda t: t)
            broker.cancel_orders(symbol)
            after = broker.account()
            result["note"] = f"round trip {qty} sh @ {avg:.2f}: equity {acct.equity:,.2f} -> {after.equity:,.2f} (spread / slippage only)"
        result["ok"] = True
    except Exception as exc:
        result["error"] = str(exc)
        if not steps.rows or steps.rows[-1]["ok"]:
            steps.rows.append({"name": "failed", "ok": False, "detail": f"{type(exc).__name__}: {exc}", "ms": 0})
    result["finished_at"] = pd.Timestamp.now("UTC").isoformat(timespec="seconds")
    _record(session, result)
    return result


def _record(session: "TradingSession", result: dict) -> None:
    detail = f"{result['kind']} test on {result['symbol']}: " + ("passed" if result["ok"] else "FAILED") + (f" - {result['note']}" if result.get("note") and result["ok"] else "")
    session.health.record("broker_orders", result["ok"], detail=detail, error=None if result["ok"] else result["error"], items=len(result["steps"]))
    if not result["ok"]:
        session.alerts.failure(f"Broker order test ({result['kind']} on {result['symbol']}, {result['broker']})", str(result["error"]))
    path = Path(session.state_dir) / RESULTS_FILE
    history = load_results(session.state_dir)
    history.insert(0, result)
    path.write_text(json.dumps(history[:20], indent=2, default=str))


def load_results(state_dir: Path | str) -> list[dict]:
    path = Path(state_dir) / RESULTS_FILE
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []
