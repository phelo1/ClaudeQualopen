"""IBKR / MT5 providers and the MT5 broker, exercised against in-memory fakes."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import pytest

import qmag.data as data_mod
from qmag.broker import BROKER_KINDS, LIVE_BROKERS, MT5Broker, Order, make_broker, orders_equivalent
from qmag.data import DATA_KINDS, CachedDailyProvider, MT5Provider, make_provider, mt5_rates_to_frame
from qmag.trader import _desired_exits, ManagedPosition


# --------------------------------------------------------------------------- #
# Shared cache behaviour
# --------------------------------------------------------------------------- #
class _CountingProvider(CachedDailyProvider):
    calls: list[tuple[list[str], str | None]] = []

    def _download(self, symbols, start, end):
        _CountingProvider.calls.append((list(symbols), start))
        idx = pd.bdate_range(start or "2024-01-01", "2024-02-15")
        return {s: pd.DataFrame({"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 1e6}, index=idx) for s in symbols}


def test_cached_provider_downloads_once_then_reads_cache(tmp_path):
    _CountingProvider.calls = []
    p = _CountingProvider(cache_dir=tmp_path, max_age_hours=24)
    first = p.load(["AAA", "BBB"], start="2024-01-01", end="2024-02-15")
    second = p.load(["AAA", "BBB"], start="2024-01-01", end="2024-02-15")
    assert set(first) == {"AAA", "BBB"} and len(_CountingProvider.calls) == 1
    assert second["AAA"].equals(first["AAA"])
    assert (tmp_path / "AAA.csv").exists()


def test_make_provider_kinds():
    assert set(DATA_KINDS) >= {"yfinance", "ibkr", "mt5", "csv"} and "synthetic" not in DATA_KINDS
    with pytest.raises(ValueError, match="never uses simulated"):
        make_provider("synthetic")
    assert make_provider("ibkr", cache_dir="/tmp/x").__class__.__name__ == "IBKRProvider"
    assert isinstance(make_provider("mt5"), MT5Provider)
    with pytest.raises(ValueError):
        make_provider("bloomberg")


def test_mt5_rates_to_frame_prefers_real_volume():
    rates = np.array(
        [(1_700_000_000, 10.0, 11.0, 9.5, 10.5, 100, 2, 0), (1_700_086_400, 10.5, 12.0, 10.0, 11.5, 120, 2, 5000)],
        dtype=[("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"), ("close", "<f8"), ("tick_volume", "<u8"), ("spread", "<i4"), ("real_volume", "<u8")],
    )
    df = mt5_rates_to_frame(rates)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df["volume"].tolist() == [100.0, 5000.0]
    assert df.index[0] == pd.Timestamp("2023-11-14 22:13:20")


def test_mt5_provider_maps_terminal_symbols(monkeypatch):
    monkeypatch.setenv("MT5_SYMBOL_SUFFIX", ".NAS")
    assert MT5Provider().terminal_symbol("AAPL") == "AAPL.NAS"
    assert MT5Provider(symbol_prefix="#", symbol_suffix="").terminal_symbol("AAPL") == "#AAPL"


# --------------------------------------------------------------------------- #
# Fake MetaTrader5 module
# --------------------------------------------------------------------------- #
@dataclass
class _Info:
    name: str
    digits: int = 2
    trade_contract_size: float = 1.0
    volume_step: float = 1.0
    volume_min: float = 1.0
    volume_max: float = 1e6


@dataclass
class _Tick:
    ask: float
    bid: float


@dataclass
class _Acct:
    equity: float = 100_000.0
    margin_free: float = 80_000.0
    trade_mode: int = 0  # demo
    margin_mode: int = 0  # netting


@dataclass
class _Position:
    ticket: int
    symbol: str
    volume: float
    price_open: float
    sl: float = 0.0
    tp: float = 0.0
    type: int = 0
    magic: int = 260901


@dataclass
class _Pending:
    ticket: int
    symbol: str
    type: int
    volume_current: float
    price_open: float
    sl: float = 0.0
    price_stoplimit: float = 0.0
    comment: str = ""
    magic: int = 260901


@dataclass
class _Result:
    retcode: int
    order: int = 0
    volume: float = 0.0
    price: float = 0.0
    comment: str = "done"


class FakeMT5(types.ModuleType):
    TRADE_ACTION_DEAL, TRADE_ACTION_PENDING, TRADE_ACTION_SLTP, TRADE_ACTION_REMOVE = 1, 5, 6, 8
    ORDER_TYPE_BUY, ORDER_TYPE_SELL, ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT = 0, 1, 2, 3
    ORDER_TYPE_BUY_STOP, ORDER_TYPE_SELL_STOP, ORDER_TYPE_BUY_STOP_LIMIT, ORDER_TYPE_SELL_STOP_LIMIT = 4, 5, 6, 7
    ORDER_TIME_GTC, ORDER_TIME_DAY = 0, 1
    ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 1, 2
    POSITION_TYPE_BUY = 0
    TRADE_RETCODE_DONE, TRADE_RETCODE_PLACED, TRADE_RETCODE_DONE_PARTIAL = 10009, 10008, 10010
    ACCOUNT_TRADE_MODE_REAL = 2
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2

    def __init__(self):
        super().__init__("MetaTrader5")
        self.acct = _Acct()
        self.pos: list[_Position] = []
        self.pend: list[_Pending] = []
        self.sent: list[dict] = []
        self._ticket = 100

    def initialize(self, **kw):
        return True

    def last_error(self):
        return (0, "ok")

    def account_info(self):
        return self.acct

    def symbol_select(self, s, on):
        return True

    def symbol_info(self, s):
        return _Info(name=s)

    def symbol_info_tick(self, s):
        return _Tick(ask=50.10, bid=50.00)

    def positions_get(self):
        return list(self.pos)

    def orders_get(self):
        return list(self.pend)

    def order_send(self, req):
        self.sent.append(req)
        self._ticket += 1
        if req["action"] == self.TRADE_ACTION_DEAL:
            if req["type"] == self.ORDER_TYPE_BUY:
                self.pos.append(_Position(self._ticket, req["symbol"], req["volume"], req["price"]))
            else:
                for p in self.pos:
                    if p.symbol == req["symbol"]:
                        p.volume -= req["volume"]
                self.pos = [p for p in self.pos if p.volume > 0]
            return _Result(self.TRADE_RETCODE_DONE, order=self._ticket, volume=req["volume"], price=req["price"])
        if req["action"] == self.TRADE_ACTION_PENDING:
            self.pend.append(_Pending(self._ticket, req["symbol"], req["type"], req["volume"], req["price"], req.get("sl", 0.0), req.get("stoplimit", 0.0), req.get("comment", "")))
            return _Result(self.TRADE_RETCODE_PLACED, order=self._ticket)
        if req["action"] == self.TRADE_ACTION_SLTP:
            for p in self.pos:
                if p.ticket == req["position"]:
                    p.sl = req["sl"]
            return _Result(self.TRADE_RETCODE_DONE)
        if req["action"] == self.TRADE_ACTION_REMOVE:
            self.pend = [o for o in self.pend if o.ticket != req["order"]]
            return _Result(self.TRADE_RETCODE_DONE)
        return _Result(0)


@pytest.fixture
def fake_mt5(monkeypatch):
    mod = FakeMT5()
    monkeypatch.setitem(sys.modules, "MetaTrader5", mod)
    monkeypatch.setattr(data_mod, "_MT5", None)
    monkeypatch.setenv("MT5_SYMBOL_SUFFIX", ".US")
    yield mod
    data_mod._MT5 = None


def test_mt5_broker_kinds_registered():
    assert "mt5" in BROKER_KINDS and "mt5-live" in LIVE_BROKERS


def test_mt5_broker_refuses_real_account_in_paper_mode(fake_mt5):
    fake_mt5.acct.trade_mode = FakeMT5.ACCOUNT_TRADE_MODE_REAL
    with pytest.raises(RuntimeError):
        make_broker("mt5")
    assert make_broker("mt5-live").name == "mt5-live"


def test_mt5_broker_bracket_market_and_exits(fake_mt5):
    b: MT5Broker = make_broker("mt5")
    assert b.account().equity == 100_000.0

    o = b.buy_stop_bracket("NVDA", 40, trigger=100.0, stop_loss=95.0, limit=101.0, tag="breakout:NVDA")
    req = fake_mt5.sent[-1]
    assert req["symbol"] == "NVDA.US" and req["type"] == FakeMT5.ORDER_TYPE_BUY_STOP_LIMIT
    assert req["price"] == 100.0 and req["stoplimit"] == 101.0 and req["sl"] == 95.0 and req["type_time"] == FakeMT5.ORDER_TIME_DAY
    pending = b.open_orders()
    assert len(pending) == 1 and pending[0].kind == "buy_stop_bracket" and pending[0].symbol == "NVDA" and pending[0].trigger == 100.0
    assert b.cancel_orders("NVDA") == 1 and b.open_orders() == []

    filled = b.market_buy("TSLA", 90, tag="ep:TSLA")
    assert filled.status == "filled" and filled.fill_price == 50.10
    assert b.positions()["TSLA"].qty == 90

    # The trader's exit plan: OCO for the partial + stop for the rest -> SL on the position + SELL_LIMIT.
    pos = ManagedPosition(symbol="TSLA", setup="ep", entry_date="2025-01-02", entry_price=50.1, shares=90, initial_stop=47.0, stop=47.0, pivot=50.0, remaining=90, target=56.3, partial_qty=30)
    desired = _desired_exits(pos)
    for d in desired:
        if d.kind == "oco":
            b.oco_sell("TSLA", d.qty, d.limit, d.trigger, tag="target:TSLA")
        else:
            b.stop_sell("TSLA", d.qty, d.trigger, tag="protective:TSLA")
    existing = [x for x in b.open_orders() if x.side == "sell"]
    assert orders_equivalent(existing, desired), existing
    assert fake_mt5.pos[0].sl == 47.0
    assert sum(1 for r in fake_mt5.sent if r["action"] == FakeMT5.TRADE_ACTION_SLTP) == 1  # second call was a no-op

    # Cancelling leaves the protective SL on the position but removes the pending limit.
    assert b.cancel_orders("TSLA") == 1
    left = b.open_orders()
    assert len(left) == 1 and left[0].kind == "stop" and left[0].qty == 90 and left[0].trigger == 47.0

    b.market_sell("TSLA", 90, tag="close")
    assert b.positions() == {} and b.open_orders() == []


def test_mt5_lot_rounding_with_contract_size(fake_mt5, monkeypatch):
    b: MT5Broker = make_broker("mt5")
    monkeypatch.setattr(fake_mt5, "symbol_info", lambda s: _Info(name=s, trade_contract_size=10.0, volume_step=0.1, volume_min=0.1))
    assert b._lots("XYZ", 37) == pytest.approx(3.7)
    assert b._shares("XYZ.US", 3.7) == 37
