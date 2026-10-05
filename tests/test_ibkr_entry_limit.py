"""Broker-observed wide stop-limit rejections, without submitting real orders."""
import itertools
import sys
from types import SimpleNamespace as NS

import pytest

from qmag.broker import IBKRBroker


def fixture_broker(monkeypatch):
    def order(action, qty, **kwargs):
        return NS(action=action,totalQuantity=qty,**kwargs)
    monkeypatch.setitem(sys.modules,'ib_async',NS(StopLimitOrder=order,StopOrder=order))
    b=object.__new__(IBKRBroker); b.account_id='DU_TEST'
    submitted=[]; ids=itertools.count(1)
    b._contract=lambda symbol:symbol
    b.ib=NS(client=NS(getReqId=lambda:next(ids)),placeOrder=lambda c,o:submitted.append(o))
    return b,submitted


@pytest.mark.parametrize('trigger,requested',[(274.09,287.22),(123.58,129.50),(410.32,429.98),(74.85,78.44),(218.48,228.94),(198.74,208.26)])
def test_rejected_wide_offsets_are_capped_with_attached_protection(monkeypatch,trigger,requested):
    b,rows=fixture_broker(monkeypatch)
    result=b.buy_stop_bracket('ABC',10,trigger,trigger*.95,limit=requested,tag='intent')
    parent,child=rows
    assert round(trigger,2)<=parent.lmtPrice<=trigger*1.005
    assert parent.lmtPrice==result.limit
    assert parent.transmit is False and child.transmit is True
    assert child.parentId==parent.orderId and child.totalQuantity==10
    assert child.action=='SELL' and child.stopPrice==round(trigger*.95,2)


def test_tighter_limit_is_never_widened_even_by_rounding(monkeypatch):
    b,rows=fixture_broker(monkeypatch)
    result=b.buy_stop_bracket('ABC',10,100,95,limit=100.019)
    assert result.limit==100.01
    assert b.buy_stop_bracket('ABC',10,100,95).limit==100.5


@pytest.mark.parametrize('trigger,limit',[(100,99),(100,float('nan')),(100,float('inf')),(0,1),(float('nan'),101)])
def test_invalid_prices_submit_nothing(monkeypatch,trigger,limit):
    b,rows=fixture_broker(monkeypatch)
    with pytest.raises(ValueError): b.buy_stop_bracket('ABC',10,trigger,95,limit=limit)
    assert not rows
