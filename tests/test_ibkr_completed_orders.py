from dataclasses import asdict
from types import SimpleNamespace as NS

import pandas as pd
import pytest

from qmag.config import StrategyConfig
from qmag.executions import BrokerView, poll, reconcile
from qmag.trader import TraderState, PendingPlan


def completed(tag='intent',perm=123,account='DU_TEST',filled=0,status='Cancelled'):
    return NS(order=NS(orderId=0,clientId=0,permId=perm,account=account,orderRef=tag,
                      totalQuantity=0 if filled else 10,filledQuantity=filled,action='BUY',parentId=0,ocaGroup=''),
              orderStatus=NS(filled=0,avgFillPrice=0,status=status),contract=NS(symbol='ABC'))


def execution(execid='a',shares=4,cum=4,account='DU_TEST',client=17):
    return NS(execution=NS(execId=execid,orderId=7,clientId=client,permId=123,acctNumber=account,
                          shares=shares,price=100,cumQty=cum,avgPrice=100),
              commissionReport=NS(currency='USD',commission=.5),time=pd.Timestamp('2026-10-05T16:27Z'))


def broker(rows,fills=()):
    return NS(name='ibkr-paper',client_id=17,account_id='DU_TEST',
              ib=NS(trades=lambda:[],reqCompletedOrders=lambda **_:rows,reqExecutions=lambda:list(fills)))


def test_completed_cancellation_recovers_unique_intent_without_replacing_order_id():
    b=broker([completed()])
    state=TraderState(pending={'ABC':asdict(PendingPlan('ABC','breakout','2026-10-05',100,95,10,'7',client_tag='intent'))})
    actions=[]
    reconcile(b,state,StrategyConfig(),'2026-10-05',actions)
    assert state.reconciliation['ok'] and not state.pending
    assert state.executions['7']['status']=='cancelled'
    assert state.executions['7']['filled']==0


def test_completed_fill_recovers_from_permanent_id_and_cumulative_execution():
    f1,f2=execution(),execution('b',6,10)
    view=BrokerView(broker([completed(filled=10,status='Filled')],[f1,f2,f2]))
    result=view.order('7')
    assert result.id=='7' and result.requested==result.filled==10
    assert result.average==100 and result.fees==1 and result.status=='filled'
    assert result.child_ids==[]  # zero parent IDs must not link unrelated completed orders


def test_persisted_tag_recovers_completed_exit_after_disconnect():
    state=TraderState(executions={'7':{'id':'7','tag':'intent','status':'open','filled':0}})
    row=poll(BrokerView(broker([completed()])),state,'7')
    assert row['id']=='7' and row['status']=='cancelled'


def test_missing_execution_prices_remain_unknown_not_zero_fills():
    view=BrokerView(broker([completed(filled=10,status='Filled')]))
    with pytest.raises(ValueError,match='execution price'): view.order('7','intent')


def test_other_account_and_other_client_executions_cannot_claim_order():
    assert BrokerView(broker([completed(account='DU_OTHER')])).order('7','intent') is None
    for f in [execution(account='DU_OTHER'),execution(client=99)]:
        assert BrokerView(broker([completed(filled=4,status='Filled')],[f])).order('7') is None


def test_ambiguous_tag_never_picks_an_arbitrary_order():
    with pytest.raises(ValueError,match='Ambiguous'):
        BrokerView(broker([completed(),completed(perm=124)])).order('7','intent')


def test_partial_history_recovers_quantity_but_keeps_missing_fees_unknown():
    result=BrokerView(broker([completed(filled=10,status='Filled')],[execution('b',6,10)])).order('7')
    assert result.filled==10 and result.average==100 and result.fees is None
