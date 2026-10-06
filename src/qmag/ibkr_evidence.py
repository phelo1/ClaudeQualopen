"""Desk-local broker observations retained beyond IB's history window.

Only actual callbacks are stored. The desk writer lease owns this file.
"""
from __future__ import annotations
import json
from pathlib import Path
from types import SimpleNamespace
from .persistence import atomic_json


def namespace(value):
    if isinstance(value, dict):
        return SimpleNamespace(**{k: namespace(v) for k, v in value.items()})
    if isinstance(value, list):
        return [namespace(v) for v in value]
    return value


def fields(obj, names):
    return {n: getattr(obj, n, None) for n in names.split()}


class IBKREvidence:
    def __init__(self, path: Path, account: str, client: int):
        self.path, self.account, self.client = Path(path), account, client
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {
            'account': account, 'client': client, 'trades': {}, 'fills': {}}
        if self.data['account'] != account or self.data['client'] != client:
            raise ValueError('IBKR evidence belongs to another account/client; use its original desk')
        self.error = None

    def capture(self, trade, *_):
        o = trade.order
        if o.account != self.account or o.clientId != self.client or not o.orderId:
            return
        try:
            self.data['trades'][str(o.orderId)] = {
                'order': fields(o, 'orderId clientId permId account orderRef parentId totalQuantity filledQuantity action ocaGroup'),
                'orderStatus': fields(trade.orderStatus, 'filled avgFillPrice status'),
                'contract': fields(trade.contract, 'symbol')}
            for fill in trade.fills:
                e = fill.execution
                if e.acctNumber == self.account and e.clientId == self.client:
                    self.data['fills'][e.execId] = {
                        'execution': fields(e, 'execId orderId clientId permId acctNumber shares price cumQty avgPrice'),
                        'commissionReport': fields(fill.commissionReport, 'currency commission'),
                        'time': str(fill.time)}
            atomic_json(self.path, self.data)
            self.error = None
        except Exception as exc:
            self.error = exc
            raise

    def attach(self, ib):
        for event in (ib.openOrderEvent, ib.orderStatusEvent, ib.execDetailsEvent, ib.commissionReportEvent):
            event += self.capture
        for trade in ib.trades():
            self.capture(trade)

    def observations(self):
        if self.error:
            raise RuntimeError('Failed to persist IBKR execution evidence') from self.error
        return ([namespace(v) for v in self.data['trades'].values()],
                [namespace(v) for v in self.data['fills'].values()])
