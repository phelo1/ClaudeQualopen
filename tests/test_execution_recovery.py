from dataclasses import asdict
from types import SimpleNamespace as NS
import pandas as pd
import pytest

from qmag.broker import PaperBroker, Order
from qmag.config import StrategyConfig
from qmag.executions import BrokerView, OrderSnapshot, reconcile, cancel_pending
from qmag.trader import TraderState, PendingPlan, ManagedPosition, ensure_exit_orders, CycleReport


class Exchange:
    name = 'fixture-live'
    def __init__(self):
        self.rows = {}
        self.cancelled = []
    def order_snapshot(self, oid, tag=''):
        return self.rows.get(oid) if oid else next((r for r in self.rows.values() if r.tag == tag), None)
    def cancel_order(self, oid):
        self.cancelled.append(oid)
        self.rows[oid].status = 'cancelled'
    def wait(self, _): pass


def test_partial_entries_reconnect_and_fees_are_idempotent(tmp_path):
    broker, cfg = Exchange(), StrategyConfig()
    state = TraderState(pending={'ABC':asdict(PendingPlan('ABC','breakout','2026-01-02',100,95,10,'',client_tag='intent'))})
    state._persistence_path = tmp_path/'trader.json'
    broker.rows['1'] = OrderSnapshot('1','ABC','buy',10,4,100,'partial','intent',1)
    reconcile(broker,state,cfg,'2026-01-02',[])
    assert state.managed['ABC']['remaining'] == 4
    assert state.managed['ABC']['entry_complete'] is False
    state = TraderState.load(tmp_path/'trader.json')
    broker.rows['1'] = OrderSnapshot('1','ABC','buy',10,10,101,'filled','intent',2)
    state.managed['ABC']['exit_order_ids'] = ['2','3']
    broker.rows['2'] = OrderSnapshot('2','ABC','sell',4,4,110,'filled',fees=1)
    broker.rows['3'] = OrderSnapshot('3','ABC','sell',6,0,None,'open')
    reconcile(broker,state,cfg,'2026-01-03',[])
    assert state.managed['ABC']['remaining'] == 6
    assert state.managed['ABC']['realised'] == 440
    broker.rows['3'] = OrderSnapshot('3','ABC','sell',6,6,105,'filled',fees=None)
    reconcile(broker,state,cfg,'2026-01-04',[])
    assert len(state.closed) == 1 and not state.managed
    assert state.closed[0]['pnl'] == 57 and not state.closed[0]['fees_known']
    broker.rows['3'].fees = 2
    reconcile(broker,state,cfg,'2026-01-04',[])
    reconcile(broker,state,cfg,'2026-01-04',[])
    assert len(state.closed) == 1 and state.closed[0]['pnl'] == 55
    assert state.closed[0]['fees_known']


def test_unknown_order_retains_intent_and_partial_cancel_is_not_discardable():
    broker, state = Exchange(), TraderState(pending={'ABC':asdict(PendingPlan('ABC','breakout','2026-01-02',100,95,10,'1'))})
    reconcile(broker,state,StrategyConfig(),'2026-01-02',[])
    assert state.pending and not state.reconciliation['ok']
    broker.rows['1'] = OrderSnapshot('1','ABC','buy',10,3,100,'partial')
    assert not cancel_pending(broker,state,'ABC')
    assert state.pending and broker.cancelled == ['1']


def test_complete_close_cancels_residual_owned_exit():
    broker = Exchange()
    broker.rows = {'1':OrderSnapshot('1','ABC','buy',10,10,100,'filled',fees=0), '2':OrderSnapshot('2','ABC','sell',10,10,95,'filled',fees=0), '3':OrderSnapshot('3','ABC','sell',10)}
    pos = ManagedPosition('ABC','breakout','2026-01-02',100,10,95,95,entry_order_id='1',exit_order_ids=['2','3'])
    state = TraderState(managed={'ABC':asdict(pos)})
    reconcile(broker,state,StrategyConfig(),'2026-01-03',[])
    assert broker.cancelled == ['3'] and len(state.closed) == 1


def test_quantity_regression_blocks_reconciliation():
    b=Exchange(); b.rows['1']=OrderSnapshot('1','ABC','buy',10,5,100,'partial')
    state=TraderState(managed={'ABC':asdict(ManagedPosition('ABC','breakout','2026-01-02',100,10,95,95,entry_order_id='1'))},executions={'1':{'filled':10}})
    reconcile(b,state,StrategyConfig(),'2026-01-03',[])
    assert state.managed['ABC']['remaining']==10 and not state.reconciliation['ok']


def test_paper_costs_and_old_daily_extrema_are_not_reused(tmp_path):
    b=PaperBroker(tmp_path/'ledger.json',10000,0,.1)
    bar=pd.Series({'open':100.,'high':120.,'low':90.,'close':100.},name=pd.Timestamp('2026-01-02'))
    b.mark({'ABC':bar},pd.Timestamp('2026-01-02'))
    buy=b.market_buy('ABC',10,tag='entry')
    b.stop_sell('ABC',10,95,tag='qmag-stop')
    b.mark({'ABC':bar},pd.Timestamp('2026-01-02'))
    assert b.positions()['ABC'].qty==10 and b.account().equity==9999
    bar['low']=89.;bar['close']=94.
    b.mark({'ABC':bar},pd.Timestamp('2026-01-02'))
    assert not b.positions() and BrokerView(b).order(buy.id).fees==1


def test_paper_attached_stop_is_reconciled_even_if_both_fill_in_one_bar(tmp_path):
    b=PaperBroker(tmp_path/'ledger.json',10000,0)
    order=b.buy_stop_bracket('ABC',10,100,95,tag='intent')
    state=TraderState(pending={'ABC':asdict(PendingPlan('ABC','breakout','2026-01-02',100,95,10,order.id))})
    b.mark({'ABC':pd.Series({'open':99,'high':101,'low':94,'close':96})},pd.Timestamp('2026-01-03'))
    reconcile(b,state,StrategyConfig(),'2026-01-03',[])
    assert not state.managed and len(state.closed)==1 and state.closed[0]['pnl']==-50


def test_protection_timeout_does_not_submit_a_duplicate(tmp_path):
    class TimeoutPaper(PaperBroker):
        def stop_sell(self,*a,**kw):
            result=super().stop_sell(*a,**kw)
            raise TimeoutError('ack lost')
    b=TimeoutPaper(tmp_path/'ledger.json')
    p=ManagedPosition('ABC','breakout','2026-01-02',100,10,95,95)
    state=TraderState(managed={'ABC':asdict(p)});state._persistence_path=tmp_path/'trader.json'
    report=CycleReport('2026-01-02',True,100000)
    with pytest.raises(TimeoutError): ensure_exit_orders(b,p,report,state)
    recovered=ManagedPosition(**TraderState.load(tmp_path/'trader.json').managed['ABC'])
    ensure_exit_orders(b,recovered,report,state)
    assert len(b.open_orders())==1 and not recovered.protection_intents


def test_alpaca_cumulative_partial_snapshot_preserves_unknown_costs():
    row=NS(id='a',symbol='ABC',side='buy',qty='10',filled_qty='4',filled_avg_price='101',status='partially_filled',client_order_id='tag',updated_at=pd.Timestamp('2026-01-02T15:30Z'),legs=[])
    broker=NS(name='alpaca-paper',client=NS(get_order_by_id=lambda _:row))
    snap=BrokerView(broker).order('a')
    assert snap.filled==4 and snap.average==101 and snap.status=='partial' and snap.fees is None


def test_ibkr_scopes_client_and_deduplicates_commissions():
    def trade(client):return NS(order=NS(orderId=7,clientId=client,parentId=0,orderRef='tag',totalQuantity=10,action='BUY'),orderStatus=NS(filled=10,avgFillPrice=100,status='Filled'),contract=NS(symbol='ABC'))
    fill=NS(execution=NS(execId='same',orderId=7,clientId=17,shares=10),commissionReport=NS(currency='USD',commission=1),time=pd.Timestamp('2026-01-02T15:30Z'))
    b=NS(name='ibkr-paper',client_id=17,ib=NS(trades=lambda:[trade(99),trade(17)],reqCompletedOrders=lambda **_:[],reqExecutions=lambda:[fill,fill]))
    snap=BrokerView(b).order('7')
    assert snap.fees==1 and snap.filled==10 and snap.updated_at


def test_snapshot_refuses_nonfinite_execution_price():
    with pytest.raises(ValueError): OrderSnapshot('x','ABC','buy',10,10,float('nan'),'filled').validate()


def test_mt5_position_linked_stop_and_deal_costs():
    order=NS(ticket=2,symbol='ABC.US',type=1,volume_initial=10.,state=4,comment='stop',time_done=1767369600,time_setup=1767369500)
    entry=NS(position_id=77,type=0,order=1)
    exit=NS(position_id=77,type=1,order=2,volume=10.,price=95.,commission=-1.,fee=-.1,swap=-.2)
    def deals(**kwargs):
        return [entry] if kwargs.get('ticket')==1 else [exit]
    mt5=NS(orders_get=lambda **_:[],history_orders_get=lambda **_:[order],history_deals_get=deals,account_info=lambda:NS(currency='USD'),DEAL_TYPE_BUY=0,DEAL_TYPE_SELL=1,ORDER_STATE_FILLED=4,ORDER_STATE_CANCELED=2,ORDER_STATE_REJECTED=5,ORDER_STATE_EXPIRED=6,ORDER_STATE_PARTIAL=3)
    broker=NS(name='mt5-paper',mt5=mt5,_shares=lambda s,v:int(v),_plain=lambda s:s.removesuffix('.US'))
    view=BrokerView(broker)
    assert view.related_exits('1')==['2']
    row=view.order('2')
    assert row.filled==10 and row.average==95 and row.fees==pytest.approx(1.3)


def test_cancel_fill_race_prevents_overselling():
    from qmag.trader import submit_exit
    class Race(Exchange):
        def open_orders(self):return [Order('2','ABC','sell',10,'stop',trigger=95,tag='qmag-stop')]
        def positions(self):return {}  # stop filled while cancellation was in flight
        def market_sell(self,*a,**k):raise AssertionError('must reconcile before another sell')
    broker=Race();broker.rows['2']=OrderSnapshot('2','ABC','sell',10,10,95,'filled')
    pos=ManagedPosition('ABC','breakout','2026-01-02',100,10,95,95,entry_order_id='1',exit_order_ids=['2'])
    state=TraderState(managed={'ABC':asdict(pos)})
    assert not submit_exit(broker,pos,10,'close','2026-01-03',state,CycleReport('2026-01-03',True,100000))
    assert state.reconciliation['issues'] and not state.closed
