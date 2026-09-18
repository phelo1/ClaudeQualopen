"""IBKR valuation for paper accounts without a USD wallet."""
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

from qmag.broker import IBKRBroker


def broker(monkeypatch, currency="EUR", price=1.25, age=120, account_rate=None):
    monkeypatch.setitem(sys.modules, "ib_async", SimpleNamespace(Forex=lambda pair: SimpleNamespace(pair=pair)))
    IBKRBroker._fx_cache.clear()
    b = object.__new__(IBKRBroker)
    b.account_id = "DU_TEST"
    b.host, b.port = "test", 4002
    calls = []
    def history(contract, **kwargs):
        calls.append(contract.pair)
        return [SimpleNamespace(date=pd.Timestamp.now(tz="UTC")-pd.Timedelta(seconds=age), close=price)]
    b.ib = SimpleNamespace(
        accountSummary=lambda: [SimpleNamespace(account="DU_TEST", tag=t, currency=currency, value=v)
                                for t, v in [("NetLiquidation", "1000"), ("TotalCashValue", "800")]],
        accountValues=lambda: [] if account_rate is None else [SimpleNamespace(account="DU_TEST", tag="ExchangeRate", currency="USD", value=str(account_rate))],
        qualifyContracts=lambda c: [c], reqHistoricalData=history)
    return b, calls


@pytest.mark.parametrize("currency,price,equity,pair", [("EUR",1.25,1250,"EURUSD"),("JPY",150,1000/150,"USDJPY")])
def test_conversion_direction_and_cash(monkeypatch, currency, price, equity, pair):
    b, calls = broker(monkeypatch, currency, price)
    a = b.account()
    assert a.currency == "USD"
    assert a.equity == pytest.approx(equity)
    assert a.cash == pytest.approx(equity*.8)
    assert b.account() == a
    assert calls == [pair]  # repeated account reads reuse only a fresh quote


@pytest.mark.parametrize("price,age", [(1.2,901),(1.2,-60),(0,0),(-1,0),(float("nan"),0),(float("inf"),0)])
def test_invalid_or_stale_fx_keeps_base_currency(monkeypatch, price, age):
    b, _ = broker(monkeypatch, price=price, age=age)
    a = b.account()
    assert a.currency == "EUR" and a.equity == 1000


def test_account_rate_preferred_and_stale_cache_rejected(monkeypatch):
    b, calls = broker(monkeypatch, account_rate=.8)
    assert b.account().equity == pytest.approx(1250)
    assert calls == []
    b.ib.accountValues = lambda: []
    IBKRBroker._fx_cache[(b.host,b.port,b.account_id,"EUR")] = (pd.Timestamp.now(tz="UTC")-pd.Timedelta(hours=1), .5)
    assert b.account().equity == pytest.approx(1250)
    assert calls == ["EURUSD"]


def test_fx_failure_and_unsupported_currency_fail_closed(monkeypatch):
    b, calls = broker(monkeypatch, currency="UNKNOWN")
    assert b.account().currency == "UNKNOWN" and not calls
    b, calls = broker(monkeypatch)
    def fail(*args, **kwargs): raise TimeoutError("fixture")
    b.ib.reqHistoricalData = fail
    assert b.account().currency == "EUR"
