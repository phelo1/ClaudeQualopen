"""Broker abstraction.

``PaperBroker``  keeps a JSON ledger on disk and simulates fills from the bars
                 you feed it – zero credentials, safe to run anywhere.
``AlpacaBroker`` routes the same calls to Alpaca (paper endpoint by default).
                 Requires ``pip install qmag[alpaca]`` and the env vars
                 ``ALPACA_API_KEY`` / ``ALPACA_SECRET_KEY``.
``IBKRBroker``   talks to a running TWS / IB Gateway through ``ib_async``.
                 Requires ``pip install qmag[ibkr]``; paper port 7497/4002.
``MT5Broker``    drives a MetaTrader 5 terminal (Windows) through the official
                 ``MetaTrader5`` package; stops live on the position, the
                 partial target is managed by the execution controller.

The trader only needs: account equity, current positions, place / cancel a
few order types (market, protective stop, buy-stop-limit bracket, OCO
target+stop). Anything fancier belongs in the trader, not here.
"""

from __future__ import annotations

import json
from .persistence import atomic_text, atomic_json
import logging
import math
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

import pandas as pd

log = logging.getLogger(__name__)


@dataclass
class Account:
    equity: float
    cash: float
    currency: str = "USD"  # the currency the numbers are in (converted to USD when the broker gives a rate)


@dataclass
class BrokerPosition:
    symbol: str
    qty: int
    avg_price: float


@dataclass
class Order:
    id: str
    symbol: str
    side: str  # buy | sell
    qty: int
    kind: str  # market | stop | buy_stop_bracket
    trigger: float | None = None  # stop price for stop orders / buy-stop trigger
    stop_loss: float | None = None  # attached protective stop for brackets
    limit: float | None = None  # max fill price for buy-stop-limit entries (don't chase gaps)
    status: str = "open"  # open | filled | cancelled
    fill_price: float | None = None
    filled_at: str | None = None
    tag: str = ""
    filled_qty: int | None = None
    fees: float | None = None
    parent_id: str = ""


class Broker(Protocol):
    name: str

    def account(self) -> Account: ...
    def positions(self) -> dict[str, BrokerPosition]: ...
    def open_orders(self) -> list[Order]: ...
    def buy_stop_bracket(self, symbol: str, qty: int, trigger: float, stop_loss: float, limit: float | None = None, tag: str = "") -> Order: ...
    def market_buy(self, symbol: str, qty: int, tag: str = "") -> Order: ...
    def market_sell(self, symbol: str, qty: int, tag: str = "") -> Order: ...
    def stop_sell(self, symbol: str, qty: int, stop_price: float, tag: str = "") -> Order: ...
    def oco_sell(self, symbol: str, qty: int, limit_price: float, stop_price: float, tag: str = "") -> Order: ...
    def cancel_orders(self, symbol: str | None = None) -> int: ...


def orders_equivalent(existing: list[Order], desired: list[Order], tol: float = 0.011) -> bool:
    """True when the resting sell orders already implement the desired exit plan."""
    if len(existing) != len(desired):
        return False
    pool = list(existing)
    for want in desired:
        match = None
        for have in pool:
            same_kind = have.kind == want.kind and have.qty == want.qty
            same_trigger = (have.trigger is None) == (want.trigger is None) and (have.trigger is None or abs(have.trigger - want.trigger) <= tol)
            same_limit = (have.limit is None) == (want.limit is None) and (have.limit is None or abs(have.limit - want.limit) <= tol)
            if same_kind and same_trigger and same_limit:
                match = have
                break
        if match is None:
            return False
        pool.remove(match)
    return True


# --------------------------------------------------------------------------- #
# Paper broker
# --------------------------------------------------------------------------- #
@dataclass
class _Ledger:
    cash: float
    positions: dict[str, dict] = field(default_factory=dict)
    orders: list[dict] = field(default_factory=list)
    fills: list[dict] = field(default_factory=list)
    last_prices: dict[str, float] = field(default_factory=dict)
    observed_bars: dict[str, dict] = field(default_factory=dict)


class PaperBroker:
    """Local paper-trading ledger.

    Fills are computed from *real* market bars (nothing is generated): market
    orders fill immediately at the last known price (or at the price
    supplied via ``mark``). Resting buy-stop / stop-sell orders fill when
    ``mark`` is called with a bar that trades through them.
    """

    name = "paper"

    def __init__(self, state_path: str | Path = "paper_state/ledger.json", starting_cash: float = 100_000.0, slippage_bps: float = 5.0, commission_per_share: float = 0.0):
        self.path = Path(state_path)
        self.slip = slippage_bps / 10_000
        self.commission_per_share = commission_per_share
        if self.path.exists():
            raw = json.loads(self.path.read_text())
            self.ledger = _Ledger(**raw)
        else:
            self.ledger = _Ledger(cash=starting_cash)
            self._save()

    # -- persistence ---------------------------------------------------------
    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(self.path, asdict(self.ledger))

    def reload(self) -> None:
        if self.path.exists():
            self.ledger = _Ledger(**json.loads(self.path.read_text(encoding="utf-8")))

    # -- read side -----------------------------------------------------------
    def account(self) -> Account:
        value = self.ledger.cash
        for sym, p in self.ledger.positions.items():
            value += p["qty"] * self.ledger.last_prices.get(sym, p["avg_price"])
        return Account(equity=value, cash=self.ledger.cash)

    def positions(self) -> dict[str, BrokerPosition]:
        return {s: BrokerPosition(s, int(p["qty"]), float(p["avg_price"])) for s, p in self.ledger.positions.items() if p["qty"] > 0}

    def open_orders(self) -> list[Order]:
        return [Order(**o) for o in self.ledger.orders if o["status"] == "open"]

    # -- order entry -----------------------------------------------------------
    def _new(self, **kwargs) -> Order:
        order = Order(id=uuid.uuid4().hex[:10], **kwargs)
        self.ledger.orders.append(asdict(order))
        return order

    def buy_stop_bracket(self, symbol: str, qty: int, trigger: float, stop_loss: float, limit: float | None = None, tag: str = "") -> Order:
        o = self._new(symbol=symbol, side="buy", qty=qty, kind="buy_stop_bracket", trigger=trigger, stop_loss=stop_loss, limit=limit, tag=tag)
        self._save()
        return o

    def market_buy(self, symbol: str, qty: int, tag: str = "") -> Order:
        o = self._new(symbol=symbol, side="buy", qty=qty, kind="market", tag=tag)
        px = self.ledger.last_prices.get(symbol)
        if px is not None:
            self._fill(o, px * (1 + self.slip), pd.Timestamp.now("UTC").isoformat())
        self._save()
        return o

    def market_sell(self, symbol: str, qty: int, tag: str = "") -> Order:
        o = self._new(symbol=symbol, side="sell", qty=qty, kind="market", tag=tag)
        px = self.ledger.last_prices.get(symbol)
        if px is not None:
            self._fill(o, px * (1 - self.slip), pd.Timestamp.now("UTC").isoformat())
        self._save()
        return o

    def stop_sell(self, symbol: str, qty: int, stop_price: float, tag: str = "") -> Order:
        o = self._new(symbol=symbol, side="sell", qty=qty, kind="stop", trigger=stop_price, tag=tag)
        self._save()
        return o

    def oco_sell(self, symbol: str, qty: int, limit_price: float, stop_price: float, tag: str = "") -> Order:
        """One-cancels-other: sell at ``limit_price`` (profit target) or ``stop_price``."""
        o = self._new(symbol=symbol, side="sell", qty=qty, kind="oco", trigger=stop_price, limit=limit_price, tag=tag)
        self._save()
        return o

    def cancel_orders(self, symbol: str | None = None) -> int:
        n = 0
        for o in self.ledger.orders:
            if o["status"] == "open" and (symbol is None or o["symbol"] == symbol):
                o["status"] = "cancelled"
                n += 1
        self._save()
        return n

    # -- simulation ------------------------------------------------------------
    def _fill(self, order: Order, price: float, when: str) -> None:
        rec = next(o for o in self.ledger.orders if o["id"] == order.id)
        actual = order.qty if order.side == "buy" else min(order.qty, self.ledger.positions.get(order.symbol, {}).get("qty", 0))
        order.filled_qty = actual
        order.fees = actual * self.commission_per_share
        self.ledger.cash -= order.fees
        rec.update(status="filled", fill_price=price, filled_at=when, filled_qty=actual, fees=order.fees)
        order.status, order.fill_price, order.filled_at = "filled", price, when
        pos = self.ledger.positions.setdefault(order.symbol, {"qty": 0, "avg_price": 0.0})
        if order.side == "buy":
            total = pos["qty"] * pos["avg_price"] + order.qty * price
            pos["qty"] += order.qty
            pos["avg_price"] = total / pos["qty"]
            self.ledger.cash -= order.qty * price
        else:
            qty = min(order.qty, pos["qty"])
            pos["qty"] -= qty
            self.ledger.cash += qty * price
            if pos["qty"] == 0:
                del self.ledger.positions[order.symbol]
        self.ledger.fills.append({**asdict(order), "filled_at": when})

    def mark(self, bars: dict[str, pd.Series], when: pd.Timestamp | None = None) -> list[Order]:
        """Feed the latest bar per symbol; fills resting orders that traded through.

        For a filled buy-stop bracket the protective stop is created
        automatically as a resting stop-sell order.
        """
        when = when or pd.Timestamp.now("UTC")
        # Repeated snapshots of a daily candle contain extrema that happened
        # before the order existed. Only new extrema / the newly observed close
        # can trigger orders on a subsequent observation of that same candle.
        fresh = {}
        for sym, source in bars.items():
            bar = source.copy()
            key = str(getattr(source, "name", None) or when.isoformat())
            prior = self.ledger.observed_bars.get(sym)
            if prior and prior["key"] == key:
                close = float(bar["close"])
                bar["open"] = close
                bar["high"] = float(bar["high"]) if float(bar["high"]) > prior["high"] else close
                bar["low"] = float(bar["low"]) if float(bar["low"]) < prior["low"] else close
            self.ledger.observed_bars[sym] = {"key": key, "high": float(source["high"]), "low": float(source["low"])}
            fresh[sym] = bar
        bars = fresh
        filled: list[Order] = []
        for sym, bar in bars.items():
            self.ledger.last_prices[sym] = float(bar["close"])
        for rec in list(self.ledger.orders):
            if rec["status"] != "open" or rec["symbol"] not in bars:
                continue
            bar = bars[rec["symbol"]]
            order = Order(**rec)
            if order.kind == "buy_stop_bracket" and bar["high"] >= order.trigger:
                px = max(float(bar["open"]), order.trigger)
                if order.limit is not None and px > order.limit:
                    # Gapped through the limit: a stop-limit would not fill. Drop it.
                    rec["status"] = "cancelled"
                    continue
                self._fill(order, px * (1 + self.slip), str(when))
                filled.append(order)
                child = self.stop_sell(order.symbol, order.qty, order.stop_loss, tag=f"protective:{order.tag}")
                next(r for r in self.ledger.orders if r["id"] == child.id)["parent_id"] = order.id
                if bar["low"] <= order.stop_loss:
                    self._fill(child, min(float(bar["open"]), order.stop_loss) * (1-self.slip), str(when))
                    filled.append(child)
            elif order.kind == "stop" and bar["low"] <= order.trigger:
                px = min(float(bar["open"]), order.trigger) * (1 - self.slip)
                self._fill(order, px, str(when))
                filled.append(order)
            elif order.kind == "oco":
                # Conservative ordering: if both legs were touched, assume the stop went first.
                if bar["low"] <= order.trigger:
                    px = min(float(bar["open"]), order.trigger) * (1 - self.slip)
                    self._fill(order, px, str(when))
                    filled.append(order)
                elif bar["high"] >= order.limit:
                    px = max(float(bar["open"]), order.limit) * (1 - self.slip)
                    self._fill(order, px, str(when))
                    filled.append(order)
        self._save()
        return filled


# --------------------------------------------------------------------------- #
# Alpaca broker
# --------------------------------------------------------------------------- #
class AlpacaBroker:
    name = "alpaca"

    def __init__(self, paper: bool = True, api_key: str | None = None, secret_key: str | None = None):
        try:
            from alpaca.trading.client import TradingClient
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("Install the Alpaca extra: pip install 'qmag[alpaca]'") from exc
        key = api_key or os.environ.get("ALPACA_API_KEY")
        secret = secret_key or os.environ.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise RuntimeError("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in the environment")
        self.client = TradingClient(key, secret, paper=paper)

    def account(self) -> Account:
        a = self.client.get_account()
        return Account(equity=float(a.equity), cash=float(a.cash))

    def positions(self) -> dict[str, BrokerPosition]:
        return {p.symbol: BrokerPosition(p.symbol, int(float(p.qty)), float(p.avg_entry_price)) for p in self.client.get_all_positions()}

    def open_orders(self) -> list[Order]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        out = []
        for o in self.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True)):
            kind = o.type.value
            trigger = float(o.stop_price) if o.stop_price else None
            limit = float(o.limit_price) if o.limit_price else None
            order_class = getattr(getattr(o, "order_class", None), "value", None) or ""
            if order_class == "oco":
                kind = "oco"
                for leg in o.legs or []:
                    if leg.stop_price:
                        trigger = float(leg.stop_price)
            out.append(
                Order(
                    id=str(o.id),
                    symbol=o.symbol,
                    side=o.side.value,
                    qty=int(float(o.qty or 0)),
                    kind=kind,
                    trigger=trigger,
                    limit=limit,
                    tag=o.client_order_id or "",
                )
            )
        return out

    def buy_stop_bracket(self, symbol: str, qty: int, trigger: float, stop_loss: float, limit: float | None = None, tag: str = "") -> Order:
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import StopLimitOrderRequest, StopLossRequest, StopOrderRequest, TakeProfitRequest

        common = dict(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            stop_price=round(trigger, 2),
            order_class=OrderClass.BRACKET,
            stop_loss=StopLossRequest(stop_price=round(stop_loss, 2)),
            # Bracket orders need a take-profit leg; park it far away, the
            # trader manages the real exit with partials and the MA trail.
            take_profit=TakeProfitRequest(limit_price=round(trigger * 3, 2)),
            client_order_id=tag[:48] if tag else None,
        )
        if limit is not None:
            req = StopLimitOrderRequest(limit_price=round(limit, 2), **common)
        else:
            req = StopOrderRequest(**common)
        o = self.client.submit_order(req)
        return Order(id=str(o.id), symbol=symbol, side="buy", qty=qty, kind="buy_stop_bracket", trigger=trigger, stop_loss=stop_loss, limit=limit, tag=tag)

    def _market(self, symbol: str, qty: int, side_name: str, tag: str) -> Order:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        side = OrderSide.BUY if side_name == "buy" else OrderSide.SELL
        o = self.client.submit_order(MarketOrderRequest(symbol=symbol, qty=qty, side=side, time_in_force=TimeInForce.DAY, client_order_id=tag[:48] or None))
        return Order(id=str(o.id), symbol=symbol, side=side_name, qty=qty, kind="market", tag=tag)

    def market_buy(self, symbol: str, qty: int, tag: str = "") -> Order:
        return self._market(symbol, qty, "buy", tag)

    def market_sell(self, symbol: str, qty: int, tag: str = "") -> Order:
        return self._market(symbol, qty, "sell", tag)

    def stop_sell(self, symbol: str, qty: int, stop_price: float, tag: str = "") -> Order:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import StopOrderRequest

        o = self.client.submit_order(
            StopOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC, stop_price=round(stop_price, 2), client_order_id=tag[:48] or None)
        )
        return Order(id=str(o.id), symbol=symbol, side="sell", qty=qty, kind="stop", trigger=stop_price, tag=tag)

    def oco_sell(self, symbol: str, qty: int, limit_price: float, stop_price: float, tag: str = "") -> Order:
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, StopLossRequest, TakeProfitRequest

        o = self.client.submit_order(
            LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                limit_price=round(limit_price, 2),
                client_order_id=tag[:48] or None,
                order_class=OrderClass.OCO,
                take_profit=TakeProfitRequest(limit_price=round(limit_price, 2)),
                stop_loss=StopLossRequest(stop_price=round(stop_price, 2)),
            )
        )
        return Order(id=str(o.id), symbol=symbol, side="sell", qty=qty, kind="oco", trigger=stop_price, limit=limit_price, tag=tag)

    def cancel_orders(self, symbol: str | None = None) -> int:
        if symbol is None:
            return len(self.client.cancel_orders())
        n = 0
        for o in self.open_orders():
            if o.symbol == symbol:
                self.client.cancel_order_by_id(o.id)
                n += 1
        return n


# --------------------------------------------------------------------------- #
# Interactive Brokers (TWS / IB Gateway via ib_async)
# --------------------------------------------------------------------------- #
class IBKRBroker:
    """Interactive Brokers through a running TWS or IB Gateway.

    Default ports: TWS paper 7497, TWS live 7496, Gateway paper 4002, Gateway
    live 4001. Enable the API in TWS (Global Configuration > API > Settings,
    tick "Enable ActiveX and Socket Clients", add 127.0.0.1 to trusted IPs).
    Requires ``pip install qmag[ibkr]``.
    """

    name = "ibkr"
    _fx_cache: dict[tuple, tuple[pd.Timestamp, float]] = {}

    def __init__(self, paper: bool = True, host: str | None = None, port: int | None = None, client_id: int | None = None):
        try:
            from ib_async import IB
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("Install the IBKR extra: pip install 'qmag[ibkr]'") from exc
        self.host = host or os.environ.get("IBKR_HOST", "127.0.0.1")
        self.port = int(port or os.environ.get("IBKR_PORT") or (7497 if paper else 7496))
        self.client_id = int(client_id or os.environ.get("IBKR_CLIENT_ID", "17"))
        self.paper = paper
        from .ibkr_runtime import prepare_ibkr_loop
        prepare_ibkr_loop()
        self.ib = IB()
        self.ib.connect(self.host, self.port, clientId=self.client_id, readonly=False, timeout=15)
        accounts = list(self.ib.managedAccounts())
        selected = os.environ.get("IBKR_ACCOUNT", "").strip()
        if not selected and len(accounts) == 1:
            selected = accounts[0]
        if not selected or selected not in accounts:
            self.ib.disconnect()
            raise RuntimeError("Select one connected account with IBKR_ACCOUNT; account selection is ambiguous or unavailable")
        self.account_id = selected
        if paper and not selected.startswith("DU"):
            self.ib.disconnect()
            raise RuntimeError("IBKR paper mode requires a paper account (DU prefix); check gateway login")
        self.name = "ibkr-paper" if paper else "ibkr-live"

    # ib_async needs its event loop pumped for fills/positions to update.
    def wait(self, seconds: float) -> None:
        self.ib.sleep(seconds)

    def _contract(self, symbol: str):
        from ib_async import Stock

        c = Stock(symbol, "SMART", "USD")
        self.ib.qualifyContracts(c)
        return c

    def account(self) -> Account:
        """Equity / cash in USD when IB gives a rate, else in the account's base currency.

        ``NetLiquidation`` is reported once, in the base currency (EUR, GBP,
        ... for non-US accounts). ``ExchangeRate`` for USD (base units per
        1 USD) appears in the account values as soon as the account holds
        anything in USD; with it the figures are converted so 0.5 % risk and
        the 25 % cap are applied to dollars, which is what the prices are in.
        """
        rows: dict[str, tuple[float, str]] = {}
        for v in self.ib.accountSummary():
            if getattr(self, "account_id", None) and v.account != self.account_id:
                continue
            if v.tag in ("NetLiquidation", "TotalCashValue") and v.currency not in ("", "BASE"):
                rows.setdefault(v.tag, (float(v.value), v.currency))
        equity, currency = rows.get("NetLiquidation", (0.0, "USD"))
        cash = rows.get("TotalCashValue", (0.0, currency))[0]
        if currency != "USD":
            rate = next((float(v.value) for v in self.ib.accountValues() if v.tag == "ExchangeRate" and v.currency == "USD" and (not getattr(self, "account_id", None) or v.account == self.account_id)), 0.0)
            if not math.isfinite(rate) or rate <= 0:
                rate = self._usd_rate(currency)
            if math.isfinite(rate) and rate > 0:
                equity, cash, currency = equity / rate, cash / rate, "USD"
            else:
                log.warning("IBKR USD conversion unavailable; new USD risk is blocked until the exchange rate is known")
        return Account(equity=equity, cash=cash, currency=currency)

    def _usd_rate(self, currency: str) -> float:
        """Base-currency units per USD, from a recent IB FX midpoint bar.

        Empty foreign-currency wallets may have no USD ExchangeRate account
        value. This is a valuation lookup only; it never converts cash or
        submits an FX order. Missing/stale prices continue to block new risk.
        """
        pairs = {"EUR": "EURUSD", "GBP": "GBPUSD", "AUD": "AUDUSD", "NZD": "NZDUSD",
                 "CAD": "USDCAD", "CHF": "USDCHF", "JPY": "USDJPY", "SEK": "USDSEK",
                 "NOK": "USDNOK", "DKK": "USDDKK", "HKD": "USDHKD", "SGD": "USDSGD"}
        pair = pairs.get(currency)
        if pair is None or not hasattr(self.ib, "reqHistoricalData"):
            return 0.0
        now = pd.Timestamp.now(tz="UTC")
        key = (getattr(self, "host", None), getattr(self, "port", None), getattr(self, "account_id", None), currency)
        cached = self._fx_cache.get(key)
        if cached and 0 <= (now-cached[0]).total_seconds() <= 900:
            return cached[1]
        try:
            from ib_async import Forex
            contract = Forex(pair)
            if not self.ib.qualifyContracts(contract):
                return 0.0
            bars = self.ib.reqHistoricalData(contract, endDateTime="", durationStr="1 D",
                barSizeSetting="5 mins", whatToShow="MIDPOINT", useRTH=False, formatDate=2, timeout=15)
            if not bars:
                return 0.0
            stamp = pd.Timestamp(bars[-1].date)
            price = float(bars[-1].close)
            if stamp.tzinfo is None or not math.isfinite(price) or price <= 0:
                return 0.0
            if not 0 <= (now-stamp).total_seconds() <= 900:
                return 0.0
            rate = 1/price if pair.endswith("USD") else price
            self._fx_cache[key] = (stamp, rate)
            return rate
        except Exception:
            log.warning("IBKR FX valuation lookup failed for %s; conversion remains unavailable", currency)
            return 0.0

    def positions(self) -> dict[str, BrokerPosition]:
        out: dict[str, BrokerPosition] = {}
        for p in self.ib.positions():
            if getattr(self, "account_id", None) and p.account != self.account_id:
                continue
            if p.contract.secType == "STK" and p.position > 0:
                out[p.contract.symbol] = BrokerPosition(p.contract.symbol, int(p.position), float(p.avgCost))
        return out

    def open_orders(self) -> list[Order]:
        out: list[Order] = []
        oca: dict[str, list] = {}
        for t in self.ib.openTrades():
            if t.order.clientId != self.client_id or (getattr(self, "account_id", None) and t.order.account != self.account_id):
                continue
            o = t.order
            if o.ocaGroup:
                oca.setdefault(o.ocaGroup, []).append(t)
                continue
            kind = {"MKT": "market", "LMT": "limit", "STP": "stop", "STP LMT": "buy_stop_bracket" if o.action == "BUY" else "stop_limit"}.get(o.orderType, o.orderType.lower())
            trigger = float(o.auxPrice) if o.orderType in ("STP", "STP LMT") else None
            limit = float(o.lmtPrice) if o.orderType in ("LMT", "STP LMT") else None
            out.append(Order(id=str(o.orderId), symbol=t.contract.symbol, side=o.action.lower(), qty=int(o.totalQuantity), kind=kind, trigger=trigger, limit=limit, tag=o.orderRef or ""))
        for group, trades in oca.items():
            stop = next((t for t in trades if t.order.orderType == "STP"), None)
            lmt = next((t for t in trades if t.order.orderType == "LMT"), None)
            first = trades[0]
            out.append(
                Order(
                    id=",".join(str(t.order.orderId) for t in trades),
                    symbol=first.contract.symbol,
                    side=first.order.action.lower(),
                    qty=int(first.order.totalQuantity),
                    kind="oco",
                    trigger=float(stop.order.auxPrice) if stop else None,
                    limit=float(lmt.order.lmtPrice) if lmt else None,
                    tag=first.order.orderRef or group,
                )
            )
        return out

    def buy_stop_bracket(self, symbol: str, qty: int, trigger: float, stop_loss: float, limit: float | None = None, tag: str = "") -> Order:
        from ib_async import StopLimitOrder, StopOrder

        c = self._contract(symbol)
        lmt = round(limit if limit is not None else trigger * 1.05, 2)
        parent = StopLimitOrder("BUY", qty, lmtPrice=lmt, stopPrice=round(trigger, 2), tif="DAY", transmit=False, account=self.account_id, orderRef=tag[:60])
        parent.orderId = self.ib.client.getReqId()
        child = StopOrder("SELL", qty, stopPrice=round(stop_loss, 2), tif="GTC", account=self.account_id, parentId=parent.orderId, transmit=True, orderRef=f"protective:{symbol}")
        self.ib.placeOrder(c, parent)
        self.ib.placeOrder(c, child)
        return Order(id=str(parent.orderId), symbol=symbol, side="buy", qty=qty, kind="buy_stop_bracket", trigger=trigger, stop_loss=stop_loss, limit=lmt, tag=tag)

    def _market(self, symbol: str, qty: int, action: str, tag: str) -> Order:
        from ib_async import MarketOrder

        trade = self.ib.placeOrder(self._contract(symbol), MarketOrder(action, qty, tif="DAY", account=self.account_id, orderRef=tag[:60]))
        self.ib.sleep(2)
        status = trade.orderStatus.status
        filled = status == "Filled"
        return Order(
            id=str(trade.order.orderId),
            symbol=symbol,
            side=action.lower(),
            qty=qty,
            kind="market",
            filled_qty=int(trade.orderStatus.filled or 0),
            status="filled" if filled else "partial" if trade.orderStatus.filled else "open",
            fill_price=float(trade.orderStatus.avgFillPrice) if trade.orderStatus.filled else None,
            tag=tag,
        )

    def market_buy(self, symbol: str, qty: int, tag: str = "") -> Order:
        return self._market(symbol, qty, "BUY", tag)

    def market_sell(self, symbol: str, qty: int, tag: str = "") -> Order:
        return self._market(symbol, qty, "SELL", tag)

    def stop_sell(self, symbol: str, qty: int, stop_price: float, tag: str = "") -> Order:
        from ib_async import StopOrder

        trade = self.ib.placeOrder(self._contract(symbol), StopOrder("SELL", qty, stopPrice=round(stop_price, 2), tif="GTC", account=self.account_id, orderRef=tag[:60]))
        return Order(id=str(trade.order.orderId), symbol=symbol, side="sell", qty=qty, kind="stop", trigger=stop_price, tag=tag)

    def oco_sell(self, symbol: str, qty: int, limit_price: float, stop_price: float, tag: str = "") -> Order:
        from ib_async import LimitOrder, StopOrder

        c = self._contract(symbol)
        group = f"qmag-{symbol}-{uuid.uuid4().hex[:6]}"
        # ocaType 1: when one leg fills, cancel the other.
        lmt = LimitOrder("SELL", qty, lmtPrice=round(limit_price, 2), tif="GTC", account=self.account_id, ocaGroup=group, ocaType=1, orderRef=tag[:60])
        stp = StopOrder("SELL", qty, stopPrice=round(stop_price, 2), tif="GTC", account=self.account_id, ocaGroup=group, ocaType=1, orderRef=tag[:60])
        t1 = self.ib.placeOrder(c, lmt)
        t2 = self.ib.placeOrder(c, stp)
        return Order(id=f"{t1.order.orderId},{t2.order.orderId}", symbol=symbol, side="sell", qty=qty, kind="oco", trigger=stop_price, limit=limit_price, tag=tag)

    def cancel_orders(self, symbol: str | None = None) -> int:
        n = 0
        for t in self.ib.openTrades():
            if t.order.clientId != self.client_id or (getattr(self, "account_id", None) and t.order.account != self.account_id):
                continue
            if symbol is None or t.contract.symbol == symbol:
                self.ib.cancelOrder(t.order)
                n += 1
        if n:
            self.ib.sleep(1)
        return n


# --------------------------------------------------------------------------- #
# MetaTrader 5 (Windows terminal, netting or hedging account)
# --------------------------------------------------------------------------- #
class MT5Broker:
    """MetaTrader 5 through the official ``MetaTrader5`` Python package.

    MT5 has no native OCO or child orders, so the exit plan is expressed with
    the tools it does have:

    * the protective stop lives on the *position* as its SL (``TRADE_ACTION_SLTP``);
    * the +2R partial is a pending ``SELL_LIMIT`` for the partial quantity;
      when it fills the position shrinks and its SL keeps protecting the rest.

    ``open_orders`` re-synthesises the trader's view (one ``oco`` per pending
    sell-limit plus one ``stop`` for the remainder) so reconciliation works
    unchanged. Quantities are shares; the terminal's ``trade_contract_size``
    and ``volume_step`` convert to lots. Paper vs live is simply which
    account the terminal is logged into (``MT5_LOGIN``/``MT5_SERVER``); use
    ``mt5-live`` to acknowledge a real account on the command line.
    """

    def __init__(self, paper: bool = True, magic: int | None = None, deviation: int = 20):
        from .data import connect_mt5

        self.mt5 = connect_mt5()
        self.paper = paper
        self.name = "mt5-paper" if paper else "mt5-live"
        self.magic = int(magic or os.environ.get("MT5_MAGIC", "260901"))
        self.deviation = deviation
        self.prefix = os.environ.get("MT5_SYMBOL_PREFIX", "")
        self.suffix = os.environ.get("MT5_SYMBOL_SUFFIX", "")
        info = self.mt5.account_info()
        if info is None:
            raise RuntimeError(f"MetaTrader5 not logged in: {self.mt5.last_error()}")
        if info.trade_mode == getattr(self.mt5, "ACCOUNT_TRADE_MODE_REAL", 0) and paper:
            raise RuntimeError("The MT5 terminal is logged into a REAL account; use --broker mt5-live to trade it")

    def wait(self, seconds: float) -> None:
        import time as _t

        _t.sleep(seconds)

    # -- symbol helpers ----------------------------------------------------------
    def _tsym(self, symbol: str) -> str:
        return f"{self.prefix}{symbol}{self.suffix}"

    def _plain(self, terminal_symbol: str) -> str:
        s = terminal_symbol
        if self.prefix and s.startswith(self.prefix):
            s = s[len(self.prefix) :]
        if self.suffix and s.endswith(self.suffix):
            s = s[: -len(self.suffix)]
        return s

    def _info(self, symbol: str):
        tsym = self._tsym(symbol)
        self.mt5.symbol_select(tsym, True)
        info = self.mt5.symbol_info(tsym)
        if info is None:
            raise RuntimeError(f"MT5 symbol not found: {tsym}")
        return info

    def _lots(self, symbol: str, qty: int) -> float:
        info = self._info(symbol)
        size = info.trade_contract_size or 1.0
        step = info.volume_step or 1.0
        lots = max(round((qty / size) / step) * step, info.volume_min or step)
        return float(min(lots, info.volume_max or lots))

    def _shares(self, tsym: str, lots: float) -> int:
        info = self.mt5.symbol_info(tsym)
        return int(round(lots * ((info.trade_contract_size if info else 1.0) or 1.0)))

    def _price(self, symbol: str, side: str) -> float:
        tick = self.mt5.symbol_info_tick(self._tsym(symbol))
        if tick is None:
            raise RuntimeError(f"No tick for {symbol}")
        return float(tick.ask if side == "buy" else tick.bid)

    def _send(self, request: dict):
        result = self.mt5.order_send(request)
        if result is None or result.retcode not in (self.mt5.TRADE_RETCODE_DONE, self.mt5.TRADE_RETCODE_PLACED, self.mt5.TRADE_RETCODE_DONE_PARTIAL):
            raise RuntimeError(f"MT5 order rejected: {getattr(result, 'retcode', None)} {getattr(result, 'comment', self.mt5.last_error())}")
        return result

    # -- read side -----------------------------------------------------------
    def account(self) -> Account:
        a = self.mt5.account_info()
        return Account(equity=float(a.equity), cash=float(a.margin_free), currency=str(a.currency))

    def _long_positions(self) -> list:
        rows = self.mt5.positions_get()
        if rows is None:
            raise RuntimeError(f"MT5 position read failed: {self.mt5.last_error()}")
        return [p for p in rows if p.type == self.mt5.POSITION_TYPE_BUY and p.magic == self.magic]

    def positions(self) -> dict[str, BrokerPosition]:
        out: dict[str, BrokerPosition] = {}
        for p in self._long_positions():
            sym = self._plain(p.symbol)
            qty = self._shares(p.symbol, p.volume)
            if sym in out:  # hedging accounts can hold several tickets per symbol
                prev = out[sym]
                total = prev.qty + qty
                out[sym] = BrokerPosition(sym, total, (prev.avg_price * prev.qty + p.price_open * qty) / total)
            else:
                out[sym] = BrokerPosition(sym, qty, float(p.price_open))
        return out

    def open_orders(self) -> list[Order]:
        out: list[Order] = []
        rows = self.mt5.orders_get()
        if rows is None:
            raise RuntimeError(f"MT5 order read failed: {self.mt5.last_error()}")
        pending = [o for o in rows if o.magic == self.magic]
        sl_by_symbol: dict[str, float] = {}
        held: dict[str, int] = {}
        for p in self._long_positions():
            sym = self._plain(p.symbol)
            held[sym] = held.get(sym, 0) + self._shares(p.symbol, p.volume)
            if p.sl:
                sl_by_symbol[sym] = float(p.sl)
        covered: dict[str, int] = {}
        for o in pending:
            sym = self._plain(o.symbol)
            qty = self._shares(o.symbol, o.volume_current)
            if o.type in (self.mt5.ORDER_TYPE_BUY_STOP, self.mt5.ORDER_TYPE_BUY_STOP_LIMIT):
                out.append(Order(id=str(o.ticket), symbol=sym, side="buy", qty=qty, kind="buy_stop_bracket", trigger=float(o.price_open), stop_loss=float(o.sl) or None,
                                 limit=float(o.price_stoplimit) if o.type == self.mt5.ORDER_TYPE_BUY_STOP_LIMIT else None, tag=o.comment or ""))
            elif o.type == self.mt5.ORDER_TYPE_SELL_LIMIT:
                covered[sym] = covered.get(sym, 0) + qty
                out.append(Order(id=str(o.ticket), symbol=sym, side="sell", qty=qty, kind="oco", trigger=sl_by_symbol.get(sym), limit=float(o.price_open), tag=o.comment or ""))
            else:
                out.append(Order(id=str(o.ticket), symbol=sym, side="sell" if o.type % 2 else "buy", qty=qty, kind=str(o.type), trigger=float(o.price_open), tag=o.comment or ""))
        for sym, sl in sl_by_symbol.items():
            rest = held.get(sym, 0) - covered.get(sym, 0)
            if rest > 0:
                out.append(Order(id=f"sl:{sym}", symbol=sym, side="sell", qty=rest, kind="stop", trigger=sl, tag=f"protective:{sym}"))
        return out

    # -- order entry -----------------------------------------------------------
    def buy_stop_bracket(self, symbol: str, qty: int, trigger: float, stop_loss: float, limit: float | None = None, tag: str = "") -> Order:
        info = self._info(symbol)
        digits = info.digits
        req = {
            "action": self.mt5.TRADE_ACTION_PENDING,
            "symbol": info.name,
            "volume": self._lots(symbol, qty),
            "type": self.mt5.ORDER_TYPE_BUY_STOP_LIMIT if limit is not None else self.mt5.ORDER_TYPE_BUY_STOP,
            "price": round(trigger, digits),
            "sl": round(stop_loss, digits),
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": tag[:31],
            "type_time": self.mt5.ORDER_TIME_DAY,
            "type_filling": self.mt5.ORDER_FILLING_RETURN,
        }
        if limit is not None:
            req["stoplimit"] = round(limit, digits)
        res = self._send(req)
        return Order(id=str(res.order), symbol=symbol, side="buy", qty=qty, kind="buy_stop_bracket", trigger=trigger, stop_loss=stop_loss, limit=limit, tag=tag)

    def _market(self, symbol: str, qty: int, side: str, tag: str) -> Order:
        info = self._info(symbol)
        req = {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": info.name,
            "volume": self._lots(symbol, qty),
            "type": self.mt5.ORDER_TYPE_BUY if side == "buy" else self.mt5.ORDER_TYPE_SELL,
            "price": self._price(symbol, side),
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": tag[:31],
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self.mt5.ORDER_FILLING_IOC,
        }
        if side == "sell":
            # Netting: reduces the aggregate position. Hedging: close against the largest ticket.
            longs = [p for p in self._long_positions() if self._plain(p.symbol) == symbol]
            if longs and getattr(self.mt5.account_info(), "margin_mode", 0) == getattr(self.mt5, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", 2):
                ticket = max(longs, key=lambda p: p.volume)
                req["position"] = ticket.ticket
                # Close one ticket at a time; reconciliation schedules the
                # remaining quantity on the next pass, never opens a short.
                req["volume"] = min(req["volume"], ticket.volume)
            if not longs:
                raise RuntimeError(f"MT5 has no owned long position to close for {symbol}")
        res = self._send(req)
        filled = res.retcode in (self.mt5.TRADE_RETCODE_DONE, self.mt5.TRADE_RETCODE_DONE_PARTIAL) and res.volume > 0
        return Order(
            id=str(res.order),
            symbol=symbol,
            side=side,
            qty=qty,
            kind="market",
            filled_qty=self._shares(info.name, res.volume) if filled else 0,
            status="filled" if filled and self._shares(info.name, res.volume) >= qty else "partial" if filled else "open",
            fill_price=float(res.price) if filled and res.price else None,
            tag=tag,
        )

    def market_buy(self, symbol: str, qty: int, tag: str = "") -> Order:
        return self._market(symbol, qty, "buy", tag)

    def market_sell(self, symbol: str, qty: int, tag: str = "") -> Order:
        return self._market(symbol, qty, "sell", tag)

    def _set_position_sl(self, symbol: str, stop_price: float) -> None:
        info = self._info(symbol)
        for p in self._long_positions():
            if self._plain(p.symbol) != symbol or abs((p.sl or 0.0) - stop_price) < 10 ** -info.digits:
                continue
            self._send({
                "action": self.mt5.TRADE_ACTION_SLTP,
                "symbol": p.symbol,
                "position": p.ticket,
                "sl": round(stop_price, info.digits),
                "tp": p.tp,
                "magic": self.magic,
            })

    def stop_sell(self, symbol: str, qty: int, stop_price: float, tag: str = "") -> Order:
        self._set_position_sl(symbol, stop_price)
        return Order(id=f"sl:{symbol}", symbol=symbol, side="sell", qty=qty, kind="stop", trigger=stop_price, tag=tag)

    native_oco = False

    def oco_sell(self, symbol: str, qty: int, limit_price: float, stop_price: float, tag: str = "") -> Order:
        raise RuntimeError("MT5 uses server-side stops and controller-managed partial targets; no synthetic sell-limit OCO")

    def cancel_orders(self, symbol: str | None = None) -> int:
        """Cancel pending orders. Position stop-losses are left in place: the
        trader re-sets them immediately and a naked position is never wanted."""
        n = 0
        for o in self.mt5.orders_get() or []:
            if o.magic != self.magic or (symbol is not None and self._plain(o.symbol) != symbol):
                continue
            self._send({"action": self.mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
            n += 1
        return n


BROKER_KINDS = ("paper", "alpaca", "alpaca-live", "ibkr", "ibkr-live", "mt5", "mt5-live")
LIVE_BROKERS = ("alpaca-live", "ibkr-live", "mt5-live")


def make_broker(kind: str, **kwargs) -> Broker:
    kind = kind.lower()
    if kind == "paper":
        return PaperBroker(**kwargs)
    if kind in ("alpaca", "alpaca-paper"):
        return AlpacaBroker(paper=True, **kwargs)
    if kind == "alpaca-live":
        return AlpacaBroker(paper=False, **kwargs)
    if kind in ("ibkr", "ibkr-paper"):
        return IBKRBroker(paper=True, **kwargs)
    if kind == "ibkr-live":
        return IBKRBroker(paper=False, **kwargs)
    if kind in ("mt5", "mt5-paper"):
        return MT5Broker(paper=True, **kwargs)
    if kind == "mt5-live":
        return MT5Broker(paper=False, **kwargs)
    raise ValueError(f"Unknown broker '{kind}'. Choose one of {', '.join(BROKER_KINDS)}")
