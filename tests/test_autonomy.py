from dataclasses import asdict
from datetime import datetime, timezone
from types import SimpleNamespace as NS
import json
import zipfile
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from qmag import autonomy
from qmag.config import StrategyConfig
from qmag.outcome_model import train, predict
from qmag.persistence import atomic_json
from qmag.trader import TraderState, CycleReport, LiveClock, ManagedPosition


def test_background_research_uses_independent_readonly_client(tmp_path, monkeypatch):
    from qmag.session import TradingSession, SessionSettings
    seen = []
    def load(session, **kwargs):
        seen.append(session.s.data_client_id)
        assert kwargs['history_bars'] == session.cfg.autonomy.min_history_days
        return {}, session.cfg
    monkeypatch.setattr(TradingSession,"load",load)
    monkeypatch.setattr(autonomy,"research",lambda *args: {"status":"collecting"})
    session=TradingSession(SessionSettings(data="csv",state_dir=tmp_path))
    assert session.start_research()["started"]
    session._research_thread.join(timeout=5)
    assert not session._research_thread.is_alive()
    assert len(seen)==1 and seen[0]>=1000000 and session.s.data_client_id is None


def test_model_fits_weights_with_purged_later_validation():
    rows=[]
    for i,d in enumerate(pd.bdate_range('2024-01-01',periods=160)):
        x=(i%17)/8-1
        rows.append({'entry_date':str(d.date()),'closed_on':str((d+pd.Timedelta(days=2)).date()),'evidence':'broker_verified','r_multiple':2*x,'features':{'rvol':x,'future_profit':999}})
    model=train(rows,[])
    assert model['status']=='candidate' and model['validation_mse']<model['baseline_mse']*.2
    assert model['trained_through']<model['validation_from'] and any(abs(v)>.5 for v in model['weights'])
    assert predict(model,{'rvol':1})>predict(model,{'rvol':-1})
    assert 'future_profit' not in model['features']


def test_shadows_cannot_supply_validation_and_estimates_are_excluded():
    rows=[{'date':str(d.date()),'resolved_on':str((d+pd.Timedelta(days=1)).date()),'status':'closed','r_multiple':1,'features':{'rvol':2}} for d in pd.bdate_range('2024-01-01',periods=100)]
    rows = [dict(r, entry=50., stop=45., target=60.) for r in rows]
    assert train([],rows)['status']=='collecting'
    assert train([dict(r,evidence='estimated') for r in rows],[])['records']==0


def test_intraday_polls_count_as_one_session_and_negative_candidate_fails():
    rows=[{'date':str(d.date()),'at':f'{d.date()}T15:00Z','baseline':1+i*.001,'candidate':1+i*.0001} for i,d in enumerate(pd.bdate_range('2026-01-01',periods=30))]
    result=autonomy.paired_interval(rows+rows)
    assert result['days']==30 and result['upper']<0


def test_deployment_rolls_back_between_experiments(tmp_path):
    state=autonomy.read(tmp_path)
    state['active']={'id':'candidate','equity_peak':100000,'previous':{'id':'baseline','overrides':{},'model':None}}
    atomic_json(tmp_path/autonomy.FILE,state)
    result=autonomy.monitor_deployment(tmp_path,StrategyConfig(),90000)
    assert result['active']['id']=='baseline' and result['history'][-1]['event']=='rolled_back'


def test_research_waits_for_fresh_holdout(tmp_path,monkeypatch):
    cfg=StrategyConfig()
    dates=pd.bdate_range('2023-01-01',periods=500)
    state=autonomy.read(tmp_path);state['last_holdout_end']=str(dates[-1].date())
    atomic_json(tmp_path/autonomy.FILE,state)
    def forbidden(*a,**k):raise AssertionError('consumed holdout must not be inspected')
    monkeypatch.setattr('qmag.backtest.run_backtest',forbidden)
    result=autonomy.research(tmp_path,{'ABC':pd.DataFrame({'close':100},index=dates)},cfg,[],[])
    assert result['status']=='collecting' and 'consumed' in result['reason']
    assert autonomy.read(tmp_path)['model_training']['records'] == 0


def test_forward_trial_promotes_after_new_evidence_and_invalidates_changed_config(tmp_path,monkeypatch):
    cfg=StrategyConfig().with_overrides({'context.enabled':False})
    state=autonomy.read(tmp_path)
    start=pd.Timestamp('2026-01-02T16:00:00Z')
    obs=[{'at':(start+pd.Timedelta(days=i)).isoformat(),'date':str((start+pd.Timedelta(days=i)).date()),'baseline':1.,'candidate':1+i*.002} for i in range(25)]
    state['trial']={'id':'candidate','status':'forward','created_at':start.isoformat(),'discovery_end':'2026-01-01','base_hash':autonomy.fingerprint(cfg),'baseline':state['active'],'candidate':{'id':'candidate','kind':'outcome_model','base_hash':autonomy.fingerprint(cfg),'overrides':{},'model':None},'config':cfg.to_dict(),'observations':obs,'attempts':1,'coverage_gaps':[]}
    atomic_json(tmp_path/autonomy.FILE,state)
    def cycle(frames,broker,config,book,**kw):
        candidate='candidate' == broker.path.parent.name
        broker.ledger.cash=106000 if candidate else 100000;broker._save()
        book.closed=[{} for _ in range(35)]
        return CycleReport('2026-01-28',True,broker.account().equity)
    monkeypatch.setattr('qmag.trader.run_cycle',cycle)
    report=CycleReport('2026-01-28',True,100000)
    live=LiveClock(now=datetime(2026,1,28,16,tzinfo=timezone.utc))
    result=autonomy.observe(tmp_path,{},cfg,report,live,True,[])
    assert result['active']['id']=='candidate' and result['active']['stage']=='canary'
    assert result['trial']['status']=='canary'
    changed=cfg.with_overrides({'breakout.max_gap_pct':.07})
    report.asof='2026-01-29';live.now=datetime(2026,1,29,16,tzinfo=timezone.utc)
    result=autonomy.observe(tmp_path,{},changed,report,live,True,[])
    assert result['trial']['status']=='invalidated' and result['active']['id']=='baseline'


def test_operations_backup_excludes_credentials_and_preserves_journal(tmp_path):
    from qmag.operations import housekeeping,status
    atomic_json(tmp_path/'trader.json',{'closed':[{'symbol':'ABC','pnl':5}]})
    (tmp_path/'settings.env').write_text('SECRET=private')
    before=(tmp_path/'trader.json').read_bytes()
    result=housekeeping(tmp_path)
    with zipfile.ZipFile(tmp_path/'backups'/result['backup']) as backup:
        assert 'trader.json' in backup.namelist() and 'settings.env' not in backup.namelist()
    assert (tmp_path/'trader.json').read_bytes()==before
    assert status(tmp_path)['issues'][0]['code']=='daemon_not_healthy'


def test_operations_learning_monitor_routes_and_origin_boundary(tmp_path):
    from qmag.session import TradingSession,SessionSettings
    from qmag.dashboard import create_app
    session=TradingSession(SessionSettings(data='csv',state_dir=tmp_path,charts=False))
    client=TestClient(create_app(session))
    for path in ['/operations','/monitor','/learning','/api/operations','/api/autonomy','/api/monitor']:
        assert client.get(path).status_code==200,path
    assert client.post('/api/maintenance',headers={'Origin':'https://evil.test'}).status_code==403
    assert client.post('/api/maintenance').status_code==200


def test_monitor_captures_actual_levels_and_unrealized_pnl(tmp_path):
    from qmag.monitor import capture,records
    dates=pd.bdate_range('2026-01-01',periods=70)
    frame=pd.DataFrame({'open':105.,'high':110.,'low':100.,'close':108.,'volume':1000.},index=dates)
    pos=ManagedPosition('ABC','breakout','2026-01-02',100,10,95,97,remaining=6,target=115,realised=440)
    state=TraderState(managed={'ABC':asdict(pos)})
    capture(tmp_path,{'ABC':frame},CycleReport(str(dates[-1].date()),True,100000),state)
    row=records(tmp_path,state)[0]
    assert row['unrealized']==48 and row['realized']==40 and row['levels']['stop']==97 and row['bars'][-1]['sma_50']==108


def test_research_reserves_holdout_before_validation_and_registers_manifest(tmp_path,monkeypatch):
    cfg=StrategyConfig().with_overrides({'autonomy.train_outcome_model':False})
    dates=pd.bdate_range('2023-01-01',periods=500)
    frames={'ABC':pd.DataFrame({'open':100.,'high':101.,'low':99.,'close':100.,'volume':1000.},index=dates)}
    calls=[]
    def backtest(data,config,start,end):
        calls.append((data['ABC'].index[-1],start,end))
        if end==str(dates[-1].date()):
            assert autonomy.read(tmp_path)['last_holdout_end']==end
        changed=autonomy.fingerprint(config)!=autonomy.fingerprint(cfg)
        return NS(metrics=lambda:{'trades':20,'total_return':.03 if changed else .01,'max_drawdown':-.01,'profit_factor':float('inf')})
    monkeypatch.setattr('qmag.backtest.run_backtest',backtest)
    result=autonomy.research(tmp_path,frames,cfg,[],[])
    state=autonomy.read(tmp_path)
    assert result['status']=='candidate' and state['trial']['status']=='forward'
    assert calls[0][0]==dates[-61]
    assert (tmp_path/'autonomy'/state['trial']['id']/'data_manifest.json').exists()


def test_scheduler_runs_simultaneous_jobs_without_skipping(tmp_path,monkeypatch):
    import qmag.daemon as module
    from qmag.session import TradingSession,SessionSettings
    from datetime import timedelta,time
    current=[datetime(2026,1,2,9,0,tzinfo=module.NY)]
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return current[0]
    session=TradingSession(SessionSettings(data='csv',state_dir=tmp_path,charts=False))
    daemon=module.Daemon(session,rebuild_universe=False)
    calls=[]
    def second():calls.append('b');daemon.stop()
    daemon.tasks=[module.Task('a',lambda d:[time(9,1)],lambda:calls.append('a')),module.Task('b',lambda d:[time(9,1)],second)]
    monkeypatch.setattr(module,'datetime',Clock)
    monkeypatch.setattr(session,'reload_settings',lambda:False)
    monkeypatch.setattr(daemon,'_sleep',lambda seconds:current.__setitem__(0,current[0]+timedelta(seconds=seconds)))
    daemon._loop(60)
    assert calls==['a','b']
