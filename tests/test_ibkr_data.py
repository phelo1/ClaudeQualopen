"""IBKR as the price feed: provider behaviour against a fake ``ib_async`` and
the ``--data auto`` preference for a reachable gateway."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import types
from types import SimpleNamespace

import pandas as pd
import pytest

import qmag.data as data_mod
from qmag.data import (
    IBKR_VOLUME_SCALE_FILE,
    IBKRProvider,
    YFinanceProvider,
    ib_bars_to_frame,
    ib_symbol,
    ibkr_gateway_reachable,
    make_provider,
    resolve_data_kind,
)

DAYS = pd.bdate_range("2025-01-02", periods=30)


def _bars(scale: float = 1.0, base: float = 100.0):
    return [SimpleNamespace(date=d.date(), open=base + i, high=base + i + 1, low=base + i - 1, close=base + i + 0.5, volume=(1_000_000 + i * 1000) / scale) for i, d in enumerate(DAYS)]


class _Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, h):
        self.handlers.append(h)
        return self

    def emit(self, *args):
        for h in self.handlers:
            h(*args)


class FakeIB:
    """Just enough of ``ib_async.IB`` for the provider: connect / disconnect,
    market data type, concurrent historical requests, the error event."""

    served: dict = {}
    refuse: bool = False
    instances: list = []

    def __init__(self):
        self.errorEvent = _Event()
        self.connected = False
        self.requests: list[str] = []
        self.mdt = None
        self.connect_args = None
        FakeIB.instances.append(self)

    def connect(self, host, port, clientId, readonly, timeout):
        if FakeIB.refuse:
            raise ConnectionRefusedError("gateway down")
        self.connected = True
        self.connect_args = (host, port, clientId, readonly)

    def isConnected(self):
        return self.connected

    def disconnect(self):
        self.connected = False

    def reqMarketDataType(self, t):
        self.mdt = t

    async def reqHistoricalDataAsync(self, contract, **kw):
        assert self.connected
        self.requests.append(contract.symbol)
        await asyncio.sleep(0)
        bars = FakeIB.served.get(contract.symbol)
        if bars is None:
            self.errorEvent.emit(len(self.requests), 200, "No security definition has been found for the request", contract)
            return []
        return bars

    def run(self, aw):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(aw)
        finally:
            loop.close()


class FakeStock:
    def __init__(self, symbol, exchange, currency, primaryExchange=""):
        self.symbol, self.exchange, self.currency, self.primaryExchange = symbol, exchange, currency, primaryExchange


@pytest.fixture
def fake_ib(monkeypatch):
    mod = types.ModuleType("ib_async")
    mod.IB, mod.Stock = FakeIB, FakeStock
    monkeypatch.setitem(sys.modules, "ib_async", mod)
    FakeIB.served, FakeIB.refuse, FakeIB.instances = {}, False, []
    data_mod._IBKR_DATA_BLOCK.update(until=0.0, reason="")
    monkeypatch.setenv("IBKR_HOST", "127.0.0.1")
    monkeypatch.setenv("IBKR_PORT", "4002")
    monkeypatch.delenv("IBKR_VOLUME_MULTIPLIER", raising=False)
    yield FakeIB


@pytest.fixture
def fake_yahoo(monkeypatch):
    """Yahoo answers with volumes in shares; records what it was asked for."""
    calls: list[list[str]] = []
    served = {"SPY": _bars(1.0, 500.0), "ZZZ": _bars(1.0, 20.0)}

    def _download(self, symbols, start, end):
        calls.append(list(symbols))
        return {s: ib_bars_to_frame(served[s]) for s in symbols if s in served}

    monkeypatch.setattr(YFinanceProvider, "_download", _download)
    return SimpleNamespace(calls=calls, served=served)


def test_ib_symbol_and_bars_to_frame():
    assert ib_symbol("BRK-B") == "BRK B" and ib_symbol("brk.b") == "BRK B" and ib_symbol("AAPL") == "AAPL"
    df = ib_bars_to_frame(_bars())
    assert list(df.columns) == ["open", "high", "low", "close", "volume"] and len(df) == 30 and df.index.tz is None
    assert ib_bars_to_frame([]) is None and ib_bars_to_frame(None) is None
    # IB marks no-trade bars with -1; they are dropped, not stored as prices.
    bad = _bars() + [SimpleNamespace(date=pd.Timestamp("2025-02-14").date(), open=-1, high=-1, low=-1, close=-1, volume=-1)]
    assert len(ib_bars_to_frame(bad)) == 30


def test_ibkr_provider_scales_volume_maps_symbols_and_fills_gaps_from_yahoo(tmp_path, fake_ib, fake_yahoo):
    # IB serves the benchmark and two names, volume in lots of 100; ZZZ is unknown to IB.
    fake_ib.served = {"SPY": _bars(100.0, 500.0), "AAA": _bars(100.0), "BRK B": _bars(100.0, 400.0)}
    p: IBKRProvider = make_provider("ibkr", cache_dir=tmp_path, max_age_hours=24)
    assert p.cache_dir == tmp_path / "ibkr" and p.yahoo_cache_dir == tmp_path

    out = p.load(["AAA", "BRK-B", "ZZZ"], start="2025-01-01", end="2025-02-14")

    assert set(out) == {"AAA", "BRK-B", "ZZZ"}
    ib = fake_ib.instances[0]
    assert ib.connect_args == ("127.0.0.1", 4002, 18, True) and ib.mdt == 3 and not ib.connected  # disconnected after the load
    assert "BRK B" in ib.requests and "SPY" in ib.requests and "ZZZ" in ib.requests
    # Lots of 100 -> shares, measured against Yahoo's SPY and remembered.
    assert out["AAA"]["volume"].iloc[0] == pytest.approx(1_000_000)
    assert p.stats["volume_multiplier"] == 100.0
    scale = json.loads((tmp_path / "ibkr" / IBKR_VOLUME_SCALE_FILE).read_text())
    assert scale["multiplier"] == 100.0 and 50 <= scale["measured_ratio"] <= 200
    # The gap was filled from Yahoo, said so, and the two caches stayed apart.
    assert p.stats["fallback"] == {"source": "yfinance", "requested": 1, "loaded": 1} and p.stats["missing"] == []
    assert any("error 200" in k for k in p.stats["ibkr_errors"])
    assert (tmp_path / "ibkr" / "AAA.csv").exists() and (tmp_path / "ZZZ.csv").exists() and not (tmp_path / "ibkr" / "ZZZ.csv").exists()
    assert fake_yahoo.calls[-1] == ["ZZZ"]

    # Second load: cache is fresh, no gateway connection at all.
    n = len(fake_ib.instances)
    again = p.load(["AAA", "BRK-B"], start="2025-01-01", end="2025-02-14")
    assert len(fake_ib.instances) == n and again["AAA"].equals(out["AAA"]) and p.stats["from_cache"] == 2


def test_ibkr_provider_honours_explicit_multiplier_and_no_fallback(tmp_path, fake_ib, fake_yahoo, monkeypatch):
    monkeypatch.setenv("IBKR_VOLUME_MULTIPLIER", "1")
    fake_ib.served = {"AAA": _bars(1.0), "SPY": _bars(1.0, 500.0)}
    p = IBKRProvider(cache_dir=tmp_path / "ibkr", yahoo_cache_dir=tmp_path, max_age_hours=24, fallback=False)
    out = p.load(["AAA", "ZZZ"], start="2025-01-01", end="2025-02-14")
    assert set(out) == {"AAA"} and p.stats["missing"] == ["ZZZ"] and "fallback" not in p.stats
    assert out["AAA"]["volume"].iloc[0] == pytest.approx(1_000_000) and fake_yahoo.calls == []  # no scale check needed
    assert fake_ib.instances[0].requests[0] == "SPY"  # the preflight goes first, alone


def test_ibkr_provider_retries_missing_names_with_a_primary_exchange(tmp_path, fake_ib, fake_yahoo, monkeypatch):
    monkeypatch.setenv("IBKR_VOLUME_MULTIPLIER", "1")
    fake_ib.served = {"SPY": _bars(1.0, 500.0), "AAA": _bars(1.0)}

    async def picky(self, contract, **kw):
        self.requests.append(contract.symbol + (f"@{contract.primaryExchange}" if contract.primaryExchange else ""))
        await asyncio.sleep(0)
        if contract.symbol == "TEVA":
            if contract.primaryExchange == "NYSE":
                return _bars(1.0, 15.0)
            self.errorEvent.emit(1, 200, "The contract description specified for TEVA is ambiguous.", contract)
            return []
        return fake_ib.served.get(contract.symbol, [])

    monkeypatch.setattr(FakeIB, "reqHistoricalDataAsync", picky)
    p = IBKRProvider(cache_dir=tmp_path / "ibkr", yahoo_cache_dir=tmp_path, max_age_hours=24, fallback=False)
    out = p.load(["AAA", "TEVA"], start="2025-01-01", end="2025-02-14")
    assert set(out) == {"AAA", "TEVA"} and p.stats["missing"] == []
    reqs = fake_ib.instances[0].requests
    assert reqs[:3] == ["SPY", "AAA", "TEVA"] and reqs[3] == "TEVA@NYSE" and "TEVA@NASDAQ" not in reqs  # bounded retry, stops when done
    assert (tmp_path / "ibkr" / "TEVA.csv").exists()


def test_ibkr_provider_sets_itself_aside_when_ib_refuses_bars(tmp_path, fake_ib, fake_yahoo, monkeypatch):
    """An account without market-data permission: one failed request, Yahoo
    serves the load, and ``auto`` stops choosing IBKR for a while."""
    data_mod._IBKR_DATA_BLOCK.update(until=0.0, reason="")
    fake_ib.served = {"AAA": _bars(1.0)}  # SPY missing -> IB "error 200"/162 on the preflight
    p: IBKRProvider = make_provider("ibkr", cache_dir=tmp_path, max_age_hours=24)
    out = p.load(["AAA", "ZZZ"], start="2025-01-01", end="2025-02-14")
    assert set(out) == {"ZZZ"} and p.stats["source"] == "yfinance"
    assert "no SPY bars" in p.stats["ibkr_error"] and "market-data permission" in p.stats["ibkr_error"]
    assert fake_ib.instances[0].requests == ["SPY"]  # AAA was never asked for
    assert data_mod.ibkr_data_blocked() and "no SPY bars" in data_mod.ibkr_data_blocked()

    monkeypatch.setenv("IBKR_PREFER_DATA", "yes")
    monkeypatch.delenv("QMAG_DATA", raising=False)
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY", raising=False)
    monkeypatch.setattr(data_mod, "ibkr_gateway_reachable", lambda *a, **k: True)
    assert resolve_data_kind("auto") == "yfinance"  # set aside even though the gateway answers
    from qmag.config import StrategyConfig
    from qmag.health import _configured
    from qmag.session import SessionSettings

    _, ok, note = _configured("price_data", SessionSettings(data="yfinance"), StrategyConfig())
    assert ok and "IBKR bars set aside for now" in note
    _, ok, note = _configured("price_data", SessionSettings(data="ibkr"), StrategyConfig())
    assert not ok and "refused bars" in note
    data_mod._IBKR_DATA_BLOCK.update(until=0.0, reason="")
    assert resolve_data_kind("auto") == "ibkr"


def test_ibkr_provider_refuses_unverified_volume_scale(tmp_path, fake_ib, fake_yahoo):
    fake_ib.served = {"AAA": _bars(100.0), "SPY": _bars(7.0, 500.0)}  # neither shares nor lots: something is off
    p: IBKRProvider = make_provider("ibkr", cache_dir=tmp_path, max_age_hours=24)
    out = p.load(["AAA", "ZZZ"], start="2025-01-01", end="2025-02-14")
    # Served entirely by Yahoo, and the report can see why.
    assert set(out) == {"ZZZ"} and p.stats["source"] == "yfinance" and "volume scale" in p.stats["ibkr_error"]
    assert not (tmp_path / "ibkr" / "AAA.csv").exists()

    strict = IBKRProvider(cache_dir=tmp_path / "ibkr", yahoo_cache_dir=tmp_path, max_age_hours=24, fallback=False)
    with pytest.raises(RuntimeError, match="volume scale"):
        strict.load(["AAA"], start="2025-01-01", end="2025-02-14")


def test_ibkr_provider_gateway_down_serves_yahoo_and_says_so(tmp_path, fake_ib, fake_yahoo):
    fake_ib.refuse = True
    p: IBKRProvider = make_provider("ibkr", cache_dir=tmp_path, max_age_hours=24)
    out = p.load(["ZZZ"], start="2025-01-01", end="2025-02-14")
    assert set(out) == {"ZZZ"} and p.stats["source"] == "yfinance" and "ConnectionRefusedError" in p.stats["ibkr_error"]


def test_gateway_probe_and_auto_resolution(monkeypatch):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        data_mod._IBKR_REACH.clear()
        assert ibkr_gateway_reachable("127.0.0.1", port) is True
        monkeypatch.setenv("IBKR_HOST", "127.0.0.1")
        monkeypatch.setenv("IBKR_PORT", str(port))
        monkeypatch.setenv("IBKR_PREFER_DATA", "yes")
        monkeypatch.delenv("QMAG_DATA", raising=False)
        monkeypatch.setitem(sys.modules, "ib_async", types.ModuleType("ib_async"))
        assert resolve_data_kind("auto") == "ibkr"
        monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "uw-test-key-0000000000")
        assert resolve_data_kind("auto") == "ibkr"  # IB wins over Unusual Whales for bars
        monkeypatch.setenv("IBKR_PREFER_DATA", "no")
        assert resolve_data_kind("auto") == "unusual_whales"
        monkeypatch.setenv("IBKR_PREFER_DATA", "yes")
        monkeypatch.setenv("QMAG_DATA", "yfinance")
        assert resolve_data_kind("auto") == "yfinance"  # an explicit choice always wins
    finally:
        srv.close()
    data_mod._IBKR_REACH.clear()
    assert ibkr_gateway_reachable("127.0.0.1", port) is False
    monkeypatch.delenv("QMAG_DATA")
    assert resolve_data_kind("auto") == "unusual_whales"


def test_ibkr_broker_account_in_base_currency_converts_when_a_usd_rate_exists():
    from qmag.broker import IBKRBroker

    class _IB:
        def __init__(self, rate):
            self.rate = rate

        def accountSummary(self):
            return [
                SimpleNamespace(tag="NetLiquidation", value="1000000.00", currency="EUR"),
                SimpleNamespace(tag="TotalCashValue", value="900000.00", currency="EUR"),
                SimpleNamespace(tag="Currency", value="BASE", currency="BASE"),
            ]

        def accountValues(self):
            return [SimpleNamespace(tag="ExchangeRate", value=str(self.rate), currency="USD")] if self.rate else []

    b = object.__new__(IBKRBroker)
    b.ib = _IB(rate=0.0)
    acct = b.account()
    assert acct.equity == 1_000_000.0 and acct.cash == 900_000.0 and acct.currency == "EUR"  # no USD rate yet: honest base figures
    b.ib = _IB(rate=0.8)
    acct = b.account()
    assert acct.equity == pytest.approx(1_250_000.0) and acct.cash == pytest.approx(1_125_000.0) and acct.currency == "USD"


def test_health_note_for_ibkr_data(monkeypatch):
    from qmag.config import StrategyConfig
    from qmag.health import _configured
    from qmag.session import SessionSettings

    monkeypatch.setenv("IBKR_HOST", "127.0.0.1")
    monkeypatch.setenv("IBKR_PORT", "4002")
    data_mod._IBKR_REACH.clear()
    monkeypatch.setattr(data_mod, "ibkr_gateway_reachable", lambda *a, **k: False)
    enabled, ok, note = _configured("price_data", SessionSettings(data="ibkr"), StrategyConfig())
    assert enabled and not ok and "nothing listening on 127.0.0.1:4002" in note
    monkeypatch.setattr(data_mod, "ibkr_gateway_reachable", lambda *a, **k: True)
    enabled, ok, note = _configured("price_data", SessionSettings(data="ibkr"), StrategyConfig())
    assert ok and "delayed" in note and "fall back to Yahoo" in note
