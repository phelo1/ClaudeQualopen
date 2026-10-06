from dataclasses import asdict
import pandas as pd
from qmag.broker import PaperBroker
from qmag.config import StrategyConfig
from qmag.trader import ManagedPosition, PendingPlan, TraderState, _adopt, run_cycle


def test_market_fill_and_reload_preserve_setup_pivot_instead_of_purchase_reference(tmp_path):
    cfg=StrategyConfig().with_overrides({'regime.enabled':False})
    state=TraderState()
    pending=PendingPlan('ABC','breakout','2026-10-06',110,95,3,'1',entry_kind='market',plan={'pivot':100})
    position=_adopt(state,pending,3,109,'2026-10-06',cfg)
    assert position.pivot==100 and position.entry_price==109 and position.stop==95
    legacy={**asdict(position),'pivot':110}
    assert ManagedPosition(**legacy).pivot==100
    assert ManagedPosition(**{**legacy,'plan':None}).pivot==110


def test_pullback_above_setup_pivot_does_not_trigger_failed_breakout(tmp_path):
    cfg=StrategyConfig().with_overrides({'regime.enabled':False,'themes.enabled':False})
    b=PaperBroker(tmp_path/'ledger.json',slippage_bps=0)
    b.mark({'ABC':pd.Series({'open':110,'high':110,'low':110,'close':110,'volume':100000})})
    entry=b.market_buy('ABC',3)
    pos=ManagedPosition('ABC','breakout','2026-10-06',110,3,95,95,pivot=110,plan={'pivot':100},entry_order_id=entry.id,evidence='paper_fill')
    state=TraderState(managed={'ABC':asdict(pos)})
    def frame(price):
        return pd.DataFrame({'open':[price],'high':[price+1],'low':[price-1],'close':[price],'volume':[100000]},index=pd.DatetimeIndex(['2026-10-06']))
    run_cycle({'ABC':frame(105)},b,cfg,state,asof=pd.Timestamp('2026-10-06'))
    assert b.positions()['ABC'].qty==3 and not state.closed
    run_cycle({'ABC':frame(98)},b,cfg,state,asof=pd.Timestamp('2026-10-06'))
    assert 'ABC' not in b.positions() and len(state.closed)==1
    assert state.closed[0]['exit_reason']=='failed_breakout'
