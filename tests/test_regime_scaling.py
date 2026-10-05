"""Reduced-risk regime policy must agree across every execution path."""
import json
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from qmag.config import StrategyConfig
from qmag.regime import RegimeSnapshot, regime_snapshot, entry_scale_series
from qmag.plan import build_plan
from qmag.backtest import prepare_data, run_backtest
from qmag.broker import PaperBroker
from qmag.trader import TraderState, run_cycle
from qmag.session import TradingSession, SessionSettings
from qmag.setups import BreakoutDetector, Signal
from tests.conftest import make_breakout_frame


def config(**overrides):
    return StrategyConfig().with_overrides({
        "regime.breadth_mode": "scale", "regime.breadth_scale": .5, "regime.risk_off_ep_scale": .5,
        "context.enabled": False, "themes.enabled": False, "reviewer.enabled": False,
        "committee.enabled": False, "adaptive.enabled": False, "entry.mode": "resting",
        "risk.max_position_pct": 1.0, "risk.slippage_bps": 0.0, **overrides})


def frames():
    leader = make_breakout_frame()
    data = {"ABC": leader}
    for name, first, last in [("QQQ", 90, 110), ("DOWN1", 110, 90), ("DOWN2", 110, 90), ("DOWN3", 110, 90)]:
        df = leader.copy()
        close = np.linspace(first, last, len(df))
        for c in ["open", "close"]: df[c] = close
        df['high'], df['low'] = close * 1.005, close * .995
        data[name] = df
    return data


@pytest.mark.parametrize("mode,ok,scale", [("gate", False, None), ("scale", True, .5)])
def test_weak_breadth_gates_or_scales(mode, ok, scale):
    cfg = config(**{"regime.breadth_mode": mode})
    snap = regime_snapshot(frames(), cfg)
    assert snap.known and snap.breadth == .25 and snap.ok is ok
    assert snap.entry_scale(cfg, "breakout") == scale
    assert entry_scale_series(frames(), cfg, "breakout").iloc[-1] == scale if scale is not None else pd.isna(entry_scale_series(frames(), cfg, "breakout").iloc[-1])


def test_ep_exception_compounds_reductions_but_never_bypasses_unknown_or_vix():
    cfg = config()
    data = frames(); data['QQQ'] = data['DOWN1'].copy()
    snap = regime_snapshot(data, cfg)
    assert not snap.ok and snap.known
    assert snap.entry_scale(cfg, "breakout") is None
    assert snap.entry_scale(cfg, "episodic_pivot") == .25
    assert entry_scale_series(data, cfg, "episodic_pivot").iloc[-1] == .25
    del data['QQQ']
    assert regime_snapshot(data, cfg).entry_scale(cfg, "episodic_pivot") is None
    assert entry_scale_series(data, cfg, "episodic_pivot").isna().all()
    cfg = config(**{"regime.max_vix": 30})
    data = frames(); data['^VIX'] = data['QQQ'].copy()
    assert regime_snapshot(data, cfg).entry_scale(cfg, "episodic_pivot") is None
    assert entry_scale_series(data, cfg, "episodic_pivot").isna().all()


def test_missing_breadth_never_becomes_full_risk_in_scale_mode():
    cfg = config()
    data = {'QQQ': frames()['QQQ']}
    assert not regime_snapshot(data, cfg).known
    assert entry_scale_series(data, cfg, 'episodic_pivot').isna().all()
    assert not regime_snapshot({'QQQ': data['QQQ'].iloc[:5]}, cfg).known


def test_trade_plan_and_actual_paper_entry_use_half_risk(tmp_path):
    data, cfg = frames(), config()
    full = cfg.with_overrides({'regime.breadth_enabled': False})
    results = []
    for label, policy in [('normal', full), ('reduced', cfg)]:
        broker = PaperBroker(tmp_path / label / 'ledger.json', starting_cash=100_000, slippage_bps=0)
        report = run_cycle(data, broker, policy, TraderState())
        plan = next(p for p in report.plans if p.symbol == 'ABC')
        results.append(plan)
        assert broker.positions()['ABC'].qty == plan.shares
        assert report.regime_known and report.regime_ok
    normal, reduced = results
    assert reduced.risk_mult == .5 and reduced.shares == normal.shares // 2
    assert reduced.notes['regime_risk_multiplier'] == .5
    assert 'risk x0.50' in reduced.notes['market_regime_policy']


def test_plan_ep_exception_changes_only_regime_and_size():
    cfg = config(); data = frames(); data['QQQ'] = data['DOWN1'].copy()
    df = prepare_data(data, cfg)['ABC']
    sig = replace(BreakoutDetector().detect_last('ABC', df, cfg), setup='episodic_pivot')
    snap = regime_snapshot(data, cfg)
    plan = build_plan(sig, df.iloc[-1], cfg, 100_000, 0, 100_000, False, risk_mult=.5, regime_state=snap)
    assert plan.checks['market_regime'] and plan.risk_mult == .125
    bad = replace(sig, stop=sig.entry + 1)
    rejected = build_plan(bad, df.iloc[-1], cfg, 100_000, 0, 100_000, False, regime_state=snap)
    assert rejected.checks['market_regime'] and not rejected.ok and 'stop_distance_ok' in rejected.failed_checks


def test_focused_uses_breadth_measurement_not_old_combined_verdict(tmp_path):
    cfg = config(); data = frames()
    session = TradingSession(SessionSettings(state_dir=tmp_path, charts=False))
    down = dict(data); down['QQQ'] = data['DOWN1'].copy()
    previous = regime_snapshot(down, cfg)
    session.full_report_path.write_text(json.dumps({'asof':str(data['QQQ'].index[-1].date()), 'config':cfg.to_dict(), 'regime_details':previous.to_dict(), 'regime_ok':False}))
    current = session._focused_snapshot({'ABC':data['ABC'], 'QQQ':data['QQQ']}, cfg)
    assert current.ok and current.known and current.entry_scale(cfg, 'breakout') == .5
    session.cfg = cfg
    assert session.current_regime({'QQQ':data['QQQ']})[0]
    session.full_report_path.write_text(json.dumps({'asof':str(data['QQQ'].index[-1].date()), 'regime_ok':True}))
    assert not session._focused_snapshot(data, cfg).known
    assert not session.current_regime({'QQQ':data['QQQ']})[2]


@pytest.mark.parametrize('age', [-1, 5])
def test_focused_rejects_future_or_stale_breadth(tmp_path, age):
    cfg=config();data=frames();session=TradingSession(SessionSettings(state_dir=tmp_path,charts=False))
    prior={'asof':str((data['QQQ'].index[-1]-pd.Timedelta(days=age)).date()),'config':cfg.to_dict(),'regime_details':regime_snapshot(data,cfg).to_dict()}
    session.full_report_path.write_text(json.dumps(prior))
    assert not session._focused_snapshot(data,cfg).known


def test_backtest_uses_previous_close_scale_and_matches_sizing(monkeypatch):
    cfg = config(**{'management.max_hold_days':1, 'management.partial_fraction':0.0})
    data = frames(); dates = data['ABC'].index
    signal = Signal(symbol='ABC', date=dates[-4], setup='breakout', pivot=100, entry=100, stop=95, adr_dollar=5, adr_pct=5, score=1)
    # Fixed execution bars keep the sizing comparison independent of detection.
    data['ABC'].loc[dates[-3]:, ['open','high','low','close']] = [100,101,99,100]
    for name in ['DOWN1','DOWN2','DOWN3']:
        data[name].loc[dates[-3]:, ['open','high','low','close']] = [300,301,299,300]
    monkeypatch.setattr('qmag.backtest.collect_signals', lambda *args: {signal.date:[signal]})
    small = run_backtest(data, cfg)
    normal = run_backtest(data, cfg.with_overrides({'regime.breadth_enabled':False}))
    assert small.signals_taken == normal.signals_taken == 1
    assert small.trades[0].shares == normal.trades[0].shares // 2
    assert small.trades[0].shares == 50
    # A missing benchmark cannot be treated as a known risk-off EP exception.
    signal = replace(signal, setup='episodic_pivot')
    del data['QQQ']
    assert run_backtest(data,cfg).signals_taken == 0


@pytest.mark.parametrize('key,value', [('breadth_mode','bad'),('breadth_scale',1.01),('risk_off_ep_scale',-1),('breadth_scale',float('nan'))])
def test_policy_values_are_validated_at_load_and_override(key,value):
    with pytest.raises(ValueError): StrategyConfig.from_dict({'regime':{key:value}})
    with pytest.raises(ValueError): StrategyConfig().with_overrides({'regime.'+key:value})


def test_legacy_reduced_risk_settings_round_trip():
    cfg=StrategyConfig.from_dict({'regime':{'breadth_mode':'scale','breadth_scale':.5,'risk_off_ep_scale':.5}})
    assert StrategyConfig.from_dict(cfg.to_dict()).regime == cfg.regime


def test_regime_change_cancels_unscanned_resting_entry(tmp_path):
    cfg = config()
    data = {s:df.iloc[:-1] for s,df in frames().items()}
    broker = PaperBroker(tmp_path/'ledger.json', starting_cash=100_000, slippage_bps=0)
    state = TraderState()
    run_cycle(data, broker, cfg.with_overrides({'regime.breadth_enabled':False}), state)
    assert 'ABC' in state.pending
    report = run_cycle(data, broker, cfg, state, scan_symbols=set(), full_scan=False, regime=regime_snapshot(data,cfg))
    assert not state.pending
    assert any(a.startswith('CANCEL untriggered entry ABC') for a in report.actions)


def test_legacy_boolean_regime_cannot_authorize_unscaled_risk(tmp_path):
    report = run_cycle(frames(), PaperBroker(tmp_path/'ledger.json'), config(), TraderState(), regime=(True,'legacy',True))
    assert not report.regime_known and not report.plans
    assert report.data_gaps
