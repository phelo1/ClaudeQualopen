from pathlib import Path
import pandas as pd
import pytest
from qmag.replay import ReplayBroker, load_intraday, replay
from qmag.config import StrategyConfig


def test_market_decision_waits_for_next_bar_and_uses_its_open(tmp_path):
    broker=ReplayBroker(tmp_path/'ledger.json',10000,0,.1)
    first=pd.Timestamp('2026-01-02T14:30Z')
    broker.event_time=first
    broker.event_bars={'ABC':pd.Series({'open':100,'high':101,'low':99,'close':100},name=first)}
    broker.mark({})
    order=broker.market_buy('ABC',10)
    assert not broker.positions() and order.status=='open'
    second=first+pd.Timedelta(minutes=1)
    broker.event_time=second
    broker.event_bars={'ABC':pd.Series({'open':105,'high':106,'low':104,'close':105},name=second)}
    fills=broker.mark({})
    assert fills[0].fill_price==105 and broker.positions()['ABC'].qty==10
    assert broker.account().equity==9999


def test_replay_never_passes_future_bar_or_current_context_to_strategy(tmp_path,monkeypatch):
    times=pd.date_range('2026-01-02T14:30Z',periods=3,freq='min')
    frame=pd.DataFrame({'open':[100.,101.,102.],'high':[101.,102.,200.],'low':[99.,100.,101.],'close':[100.,101.,199.],'volume':100.},index=times)
    cfg=StrategyConfig().with_overrides({'context.enabled':False,'reviewer.enabled':False,'committee.enabled':False})
    observed=[]
    def cycle(data,broker,cfg,state,**kwargs):
        observed.append((data['ABC'].iloc[-1]['high'],kwargs['live'].now))
        broker.mark(data)
    monkeypatch.setattr('qmag.replay.run_cycle',cycle)
    result=replay({'ABC':frame},{},cfg,tmp_path)
    assert [x[0] for x in observed]==[101,102,200]
    assert observed[0][1]==times[0]+pd.Timedelta(minutes=1)
    assert result['promotion_eligible'] is False


def test_intraday_loader_requires_explicit_timezone_and_valid_range(tmp_path):
    file=tmp_path/'ABC.csv'
    file.write_text('timestamp,open,high,low,close,volume\n2026-01-02 09:30,100,102,99,101,1000\n')
    with pytest.raises(ValueError,match='timezone'):load_intraday(tmp_path)
    file.write_text('timestamp,open,high,low,close,volume\n2026-01-02T09:30-05:00,100,99,98,101,1000\n')
    with pytest.raises(ValueError,match='range'):load_intraday(tmp_path)


def test_replay_refuses_present_day_llm_and_missing_historical_context(tmp_path):
    with pytest.raises(ValueError,match='current LLM'):replay({}, {}, StrategyConfig().with_overrides({'reviewer.enabled':True}),tmp_path)
    with pytest.raises(ValueError,match='Historical context'):replay({}, {}, StrategyConfig().with_overrides({'context.enabled':True}),tmp_path)


def test_cli_replay_runs_offline_with_daily_warmup(tmp_path):
    from typer.testing import CliRunner
    from qmag.cli import app
    minute=tmp_path/'minute';minute.mkdir()
    daily=tmp_path/'daily';daily.mkdir()
    dates=pd.bdate_range('2025-01-01',periods=200)
    pd.DataFrame({'date':dates,'open':100.,'high':101.,'low':99.,'close':100.,'volume':1000.}).to_csv(daily/'ABC.csv',index=False)
    (minute/'ABC.csv').write_text('timestamp,open,high,low,close,volume\n2026-01-02T10:30-05:00,100,102,99,101,1000\n2026-01-02T10:31-05:00,101,103,100,102,1000\n')
    config=tmp_path/'replay.yaml'
    StrategyConfig().with_overrides({'context.enabled':False,'themes.enabled':False,'regime.enabled':False,'reviewer.enabled':False,'committee.enabled':False}).save(config)
    result=CliRunner().invoke(app,['replay','--intraday-dir',str(minute),'--daily-dir',str(daily),'--config',str(config),'--output',str(tmp_path/'result')])
    assert result.exit_code==0,result.output+str(result.exception)
    assert (tmp_path/'result'/'replay.json').exists()
