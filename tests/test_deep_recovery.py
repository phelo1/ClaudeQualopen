from datetime import datetime,timezone,timedelta
from dataclasses import asdict
from types import SimpleNamespace as NS
import json
import pytest
from qmag.executions import BrokerView,poll
from qmag.ibkr_evidence import IBKREvidence
from qmag.trader import TraderState
from qmag.watchdog import check,inspect
from tests.test_ibkr_completed_orders import completed,execution,broker


def test_oco_sibling_shared_tag_cannot_override_permanent_identity():
    first=completed(tag='shared',perm=123,filled=4,status='Filled')
    sibling=completed(tag='shared',perm=124)
    result=BrokerView(broker([first,sibling],[execution()])).order('7','shared')
    assert result.filled==4 and result.permanent_id==123
    assert BrokerView(broker([first,sibling])).order('8','shared',124).status=='cancelled'


def test_callbacks_survive_expired_broker_history_and_preserve_account_scope(tmp_path):
    row=completed(filled=4,status='Filled');row.order.clientId=17;row.order.orderId=7
    row.order.totalQuantity=4;row.orderStatus.filled=4;row.orderStatus.avgFillPrice=100;row.fills=[execution()]
    evidence=IBKREvidence(tmp_path/'evidence.json','DU_TEST',17);evidence.capture(row)
    reloaded=IBKREvidence(tmp_path/'evidence.json','DU_TEST',17)
    b=broker([]);b.evidence=reloaded
    snap=BrokerView(b).order('7');assert snap.filled==4 and snap.average==100 and snap.fees==.5
    with pytest.raises(ValueError):IBKREvidence(tmp_path/'evidence.json','DU_OTHER',17)
    row.order.account='DU_OTHER';row.orderStatus.filled=0;evidence.capture(row)
    assert BrokerView(b).order('7').filled==4


def test_history_timeout_uses_only_real_retained_evidence_and_does_not_repeat(tmp_path):
    row=completed(filled=4,status='Filled');row.order.clientId=17;row.order.orderId=7
    row.order.totalQuantity=4;row.orderStatus.filled=4;row.orderStatus.avgFillPrice=100;row.fills=[execution()]
    b=broker([]);b.evidence=IBKREvidence(tmp_path/'evidence.json','DU_TEST',17);b.evidence.capture(row)
    calls=[]
    def timed_out(**kwargs):calls.append(1);raise TimeoutError()
    b.ib.reqCompletedOrders=timed_out;b.ib.RequestTimeout=20
    assert BrokerView(b).order('7').filled==4
    assert BrokerView(b).order('unknown') is None
    assert len(calls)==1 and b.ib.RequestTimeout==20 and b.history_error


def test_saved_commission_survives_fresh_immutable_sdk_execution(tmp_path):
    from collections import namedtuple
    old=execution();fresh=namedtuple('Fill','execution commissionReport time')(old.execution,NS(currency='',commission=0),old.time)
    row=completed(filled=4,status='Filled');row.order.clientId=17;row.order.orderId=7
    row.fills=[old]
    b=broker([],[fresh]);b.evidence=IBKREvidence(tmp_path/'evidence.json','DU_TEST',17);b.evidence.capture(row)
    assert BrokerView(b).order('7').fees==.5


def test_empty_options_and_missing_earnings_dates_are_unavailable_not_neutral():
    from qmag.context.edge import f_option_sentiment,f_options_pulse,f_earnings
    ctx=NS(sym='ABC',now=datetime.now(timezone.utc),get=lambda *a:NS(ok=True,data={'latest':None,'history':[],'intraday':[]}))
    assert 'no options' in f_option_sentiment(ctx).error
    assert 'no options' in f_options_pulse(ctx).error
    ctx.get=lambda *a:NS(ok=True,data=[{'report_date':None,'actual_eps':1}])
    assert f_earnings(ctx).error=='no past earnings reports'


def test_saved_permanent_identity_disambiguates_unfilled_cancelled_oco_leg():
    state=TraderState(executions={'8':{'id':'8','filled':0,'tag':'shared','status':'open','permanent_id':124}})
    row=poll(BrokerView(broker([completed(tag='shared'),completed(tag='shared',perm=124)])),state,'8')
    assert row['permanent_id']==124 and row['status']=='cancelled'


def test_watchdog_restarts_only_stalled_daemon_and_observes_cooldown(tmp_path):
    now=datetime.now(timezone.utc)
    (tmp_path/'daemon_status.json').write_text(json.dumps({'heartbeat':(now-timedelta(minutes=5)).isoformat(),'running':'reconcile','pid':1}))
    calls=[]
    def run(args,**kwargs):calls.append(args);return NS(stdout='active\n')
    assert check(tmp_path,True,run,now)['recovery']=='restarted'
    assert calls[-1]==['sudo','-n','systemctl','restart','qmag-daemon']
    assert check(tmp_path,True,run,now)['recovery']=='restart_cooldown'
    assert sum('restart' in c for c in calls)==1


def test_watchdog_respects_admin_stop_and_long_full_scan(tmp_path):
    now=datetime.now(timezone.utc)
    (tmp_path/'daemon_status.json').write_text(json.dumps({'heartbeat':(now-timedelta(minutes=10)).isoformat(),'running':'after_close'}))
    assert not inspect(tmp_path,now)['stalled']
    assert check(tmp_path,True,lambda *a,**k:NS(stdout='inactive'),now+timedelta(hours=2))['recovery']=='administratively_stopped'


def test_connections_and_watchdog_agree_on_long_running_task_health():
    from qmag.health import _daemon_record
    row={'heartbeat':(datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat(),'running':'after_close','tasks':[]}
    assert _daemon_record(row)['ok']
    row['running']='reconcile'
    assert not _daemon_record(row)['ok']


def test_llm_billing_and_rate_limit_cooldowns_prevent_repeated_calls(monkeypatch):
    from qmag import llm_transport as transport
    calls=[]
    monkeypatch.setattr(transport.requests,'post',lambda *a,**k:(calls.append(1) or NS(status_code=402,headers={})))
    transport.post('https://example.test/v1/chat/completions',headers={'Authorization':'Bearer test'},json={},timeout=1)
    with pytest.raises(RuntimeError,match='402'):
        transport.post('https://example.test/v1/chat/completions',headers={'Authorization':'Bearer test'},json={},timeout=1)
    assert calls==[1]
