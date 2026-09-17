"""Regression cases for the independent review; fixtures are test-only."""
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
import json
import subprocess
import sys
import threading

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from qmag.broker import Order
from qmag.config import StrategyConfig
from qmag.dashboard import create_app
from qmag.halt import halt_status
from qmag.learning import load_overrides, review, propose_adjustments
from qmag.market_calendar import NY
from qmag.persistence import atomic_json, desk_lock
from qmag.plan import context_checks, size_shares, resize_for_budget, build_plan
from qmag.regime import regime_series, regime_snapshot
from qmag.session import SessionSettings, TradingSession
from qmag.setups import Signal
from qmag.settings import validate_config
from qmag.trader import TraderState, ManagedPosition, CycleReport, submit_exit, portfolio_gate


def signal():
    return Signal('ABC', pd.Timestamp('2026-09-01'), 'breakout', 100, 100, 95, 5, 5, 1, {})


def test_required_context_fails_when_entire_report_is_missing(monkeypatch):
    monkeypatch.setenv('UNUSUAL_WHALES_API_KEY', 'fixture-key')
    cfg = StrategyConfig().with_overrides({'context.enabled':True, 'options_flow.enabled':True, 'options_flow.require_bullish':True, 'edge.enabled':True, 'edge.gate':True})
    checks = context_checks(signal(), None, cfg)
    assert checks['unusual_flow_bullish'] is False and checks['uw_edge'] is False


@pytest.mark.parametrize('bad', [float('inf'), float('-inf'), float('nan')])
def test_nonfinite_capital_and_settings_are_refused(bad):
    cfg = StrategyConfig()
    assert size_shares(bad, 100, 95, cfg, 0, 10000) == 0
    with pytest.raises(ValueError, match='finite'):
        validate_config(cfg.with_overrides({'risk.starting_equity':bad}))


def test_submission_resizes_to_remaining_cash_and_updates_percentages():
    cfg = StrategyConfig().with_overrides({'risk.slippage_bps':0})
    row = pd.Series({'close':100, 'dollar_vol_20':1e8})
    plan = build_plan(signal(), row, cfg, 100000, 0, 100000, True)
    resize_for_budget(plan, cfg, 100000, 0, 250)
    assert plan.shares == 2 and plan.position_value == 200
    assert plan.position_pct == pytest.approx(.002) and plan.risk_pct == pytest.approx(.0001)


def test_cash_sizing_reserves_slippage_and_commission():
    cfg = StrategyConfig().with_overrides({'risk.slippage_bps':100,'risk.commission_per_share':1})
    shares=size_shares(100000, 100, 95, cfg, 0, 1000)
    assert shares * 102 <= 1000


def test_missing_regime_inputs_block_research_and_empty_snapshot():
    cfg = StrategyConfig()
    bars=pd.DataFrame({'close':[100.,101.]},index=pd.date_range('2026-09-01',periods=2))
    allowed,_=regime_series({'ABC':bars},cfg)
    assert not allowed.any()
    assert not regime_snapshot({},cfg).ok
    assert not regime_snapshot({'QQQ':bars},cfg).known


class DelayedBroker:
    name='test-live'
    def __init__(self): self.calls=0
    def cancel_orders(self,symbol): return 0
    def market_sell(self,symbol,qty,tag=''):
        self.calls+=1
        return Order(id='pending-1',symbol=symbol,side='sell',qty=qty,kind='market',status='open')


def test_order_acknowledgement_is_not_a_closed_trade():
    broker=DelayedBroker()
    pos=ManagedPosition('ABC','breakout','2026-09-01',100,10,95,95)
    state=TraderState(managed={'ABC':asdict(pos)})
    report=CycleReport('2026-09-02',False,100000)
    assert not submit_exit(broker,pos,10,'halt',report.asof,state,report)
    assert state.closed == [] and state.managed['ABC']['remaining'] == 10
    assert state.managed['ABC']['pending_exit']['order_id']=='pending-1'
    assert not submit_exit(broker,pos,10,'halt',report.asof,state,report)
    assert broker.calls==1


def test_halt_survives_broker_cancellation_failure(tmp_path):
    session=TradingSession(SessionSettings(data='csv',state_dir=tmp_path,charts=False))
    class Broken:
        def cancel_orders(self,symbol): raise RuntimeError('offline')
    session._broker=Broken()
    TraderState(pending={'ABC':{}}).save(session.state_path)
    with pytest.raises(RuntimeError,match='offline'): session.halt(True,'operator stop')
    assert halt_status(tmp_path)['reason']=='operator stop'


def test_daily_loss_stop_stays_latched_after_a_rebound():
    cfg=StrategyConfig()
    plan=build_plan(signal(),pd.Series({'close':100,'dollar_vol_20':1e8}),cfg,100000,0,100000,True)
    state=TraderState(day_equity={'date':'2026-09-01','equity':100000,'loss_latched':True})
    assert any('latched' in reason for reason in portfolio_gate(plan,state,cfg,0,.01,100000))


def test_atomic_write_does_not_replace_valid_state_on_serialization_failure(tmp_path):
    path=tmp_path/'state.json';atomic_json(path,{'balance':1})
    with pytest.raises(ValueError):atomic_json(path,{'balance':float('nan')})
    assert json.loads(path.read_text())=={'balance':1}


def test_writer_lock_serializes_threads(tmp_path):
    path=tmp_path/'counter.json';atomic_json(path,0)
    def increment():
        for _ in range(15):
            with desk_lock(tmp_path):
                with desk_lock(tmp_path):
                    atomic_json(path,json.loads(path.read_text())+1)
    threads=[threading.Thread(target=increment) for _ in range(3)]
    for t in threads:t.start()
    for t in threads:t.join()
    assert json.loads(path.read_text())==45


def test_writer_lock_excludes_another_process(tmp_path):
    script='from pathlib import Path; from qmag.persistence import desk_lock; import sys\ntry:\n with desk_lock(Path(sys.argv[1]),timeout=.15): pass\nexcept TimeoutError: sys.exit(7)'
    with desk_lock(tmp_path):
        result=subprocess.run([sys.executable,'-c',script,str(tmp_path)],capture_output=True,timeout=15)
    assert result.returncode==7,result.stderr.decode()


def test_learned_overrides_cannot_escape_allowlist_or_rails(tmp_path):
    (tmp_path/'learning_overrides.yaml').write_text('overrides:\n  risk.risk_per_trade_pct: 0.5\n  edge.threshold: .inf\n  breakout.min_breakout_volume_ratio: 99\n  entry.opening_range_minutes: 15\n')
    assert load_overrides(tmp_path)=={'entry.opening_range_minutes':15}


def test_learning_accepts_aware_time_and_does_not_promote_estimates(tmp_path):
    from tests.test_learning import _journal_state
    result=review(_journal_state(),StrategyConfig(),tmp_path,now=datetime(2026,9,16,tzinfo=NY),apply=True,ai=False)
    assert result['adjustments'] and result['proposed_only']
    assert result['applied']==[] and result['evidence']['verified']==0


def test_learning_needs_fresh_verified_evidence_and_applies_at_most_one(tmp_path):
    from tests.test_learning import _closed
    rows=[dict(_closed(i,-1,1.25),evidence='broker_verified') for i in range(35)]
    rows += [dict(_closed(100+i,1,2.4),evidence='broker_verified') for i in range(35)]
    state=TraderState(closed=rows)
    cfg=StrategyConfig()
    result=review(state,cfg,tmp_path,now=datetime(2026,9,1),apply=True,ai=False)
    assert len(result['applied'])==1
    assert result['applied'][0]['direction']=='tighten'
    # After cooldown, the same old evidence cannot move any band again.
    new_cfg=cfg.with_overrides(load_overrides(tmp_path))
    again=review(state,new_cfg,tmp_path,now=datetime(2026,10,1),apply=True,ai=False)
    assert again['applied']==[]


def test_browser_origin_boundary_and_new_pages(tmp_path):
    session=TradingSession(SessionSettings(data='csv',state_dir=tmp_path,charts=False))
    client=TestClient(create_app(session))
    for path in ['/','/desk','/journal','/learning','/status','/settings','/accounts','/advisor','/insider']:
        response=client.get(path)
        assert response.status_code==200,(path,response.text[:200])
        assert response.headers['x-frame-options']=='DENY'
        assert 'ClaudeQual' in response.text
    response=client.post('/api/learning/reset',headers={'Origin':'https://evil.invalid'})
    assert response.status_code==403
    assert client.get('/static/app.css').status_code==200


def test_backtest_uses_next_session_open_not_retrospective_pivot(monkeypatch):
    from qmag import backtest
    bars=pd.DataFrame({'open':[99.,102.,104.],'high':[102.,104.,105.],'low':[98.,101.,103.],'close':[101.,103.,104.], 'volume':[1e6]*3},index=pd.date_range('2026-09-01',periods=3))
    cfg=StrategyConfig().with_overrides({'regime.enabled':False,'risk.slippage_bps':0,'breakout.max_gap_pct':.05})
    monkeypatch.setattr(backtest,'prepare_data',lambda raw,cfg:raw)
    monkeypatch.setattr(backtest,'collect_signals',lambda *args:{bars.index[0]:[signal()]})
    result=backtest.run_backtest({'ABC':bars},cfg)
    assert result.execution_model=='next_open' and result.signals_taken==1
    assert result.equity.iloc[0]==cfg.risk.starting_equity
    assert result.equity.iloc[1]>cfg.risk.starting_equity
