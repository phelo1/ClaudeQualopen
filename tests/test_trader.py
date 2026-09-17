import pandas as pd

from qmag.broker import PaperBroker
from qmag.config import StrategyConfig
from qmag.trader import TraderState, run_cycle


def test_paper_broker_fills_buy_stop_and_attaches_stop(tmp_path):
    brk = PaperBroker(tmp_path / "ledger.json", starting_cash=10_000, slippage_bps=0)
    brk.buy_stop_bracket("ABC", 10, trigger=100.0, stop_loss=95.0, tag="t")
    # Bar does not reach the trigger: nothing happens.
    assert brk.mark({"ABC": pd.Series({"open": 98, "high": 99, "low": 97, "close": 98})}) == []
    fills = brk.mark({"ABC": pd.Series({"open": 99, "high": 102, "low": 98, "close": 101})})
    assert len(fills) == 1 and fills[0].fill_price == 100.0
    assert brk.positions()["ABC"].qty == 10
    stops = [o for o in brk.open_orders() if o.kind == "stop"]
    assert len(stops) == 1 and stops[0].trigger == 95.0
    # Stop gets hit on a gap down: fills at the open (worse than the stop).
    fills = brk.mark({"ABC": pd.Series({"open": 93, "high": 94, "low": 92, "close": 93})})
    assert fills and fills[0].fill_price == 93.0
    assert "ABC" not in brk.positions()
    assert abs(brk.account().equity - (10_000 - 10 * 100 + 10 * 93)) < 1e-6


def test_paper_broker_stop_limit_does_not_chase_gap(tmp_path):
    brk = PaperBroker(tmp_path / "ledger.json", starting_cash=10_000, slippage_bps=0)
    brk.buy_stop_bracket("GAP", 10, trigger=100.0, stop_loss=95.0, limit=105.0)
    fills = brk.mark({"GAP": pd.Series({"open": 112, "high": 115, "low": 110, "close": 113})})
    assert fills == []
    assert brk.positions() == {}
    assert brk.open_orders() == []  # the stop-limit is dropped, not left resting


def test_paper_broker_state_persists(tmp_path):
    path = tmp_path / "ledger.json"
    PaperBroker(path, starting_cash=5_000).market_buy("XYZ", 1)  # no price known: stays open
    again = PaperBroker(path)
    assert again.account().cash == 5_000
    assert len(again.open_orders()) == 1


def _replay(data, cfg, tmp_path, dates):
    brk = PaperBroker(tmp_path / "ledger.json", starting_cash=cfg.risk.starting_equity)
    state = TraderState()
    reports = []
    for d in dates:
        sliced = {s: df.loc[:d] for s, df in data.items()}
        sliced = {s: df for s, df in sliced.items() if len(df)}
        reports.append(run_cycle(sliced, brk, cfg, state, asof=d))
    return brk, state, reports


def test_trader_replay_places_and_manages_positions(synthetic_data, tmp_path):
    cfg = StrategyConfig().with_overrides({"regime.enabled": False, "entry.mode": "resting"})
    dates = synthetic_data["QQQ"].index[350:470]
    brk, state, reports = _replay(synthetic_data, cfg, tmp_path, dates)

    actions = [a for r in reports for a in r.actions]
    assert any(a.startswith("PLAN buy-stop") for a in actions), "no breakout plans were made"
    assert any(a.startswith("FILL buy") or a.startswith("BUY") for a in actions), "no entries happened"
    assert any(a.startswith("CANCEL untriggered") for a in actions)
    # Everything the broker holds must be known to the trader with a resting stop.
    for sym in brk.positions():
        assert sym in state.managed
        assert any(o.symbol == sym and o.kind == "stop" for o in brk.open_orders())
    # No orphaned buy-stops: pending plans are re-created every cycle.
    assert all(o.kind != "buy_stop_bracket" or o.symbol in state.pending for o in brk.open_orders())
    assert state.last_run == str(dates[-1].date())
    assert brk.account().equity > 0


def test_trader_respects_regime(synthetic_data, tmp_path):
    cfg = StrategyConfig().with_overrides({"regime.ma_length": 5})
    data = dict(synthetic_data)
    bench = data["QQQ"].copy()
    import numpy as np

    bench["close"] = np.linspace(500, 100, len(bench))
    for col in ("open", "high", "low"):
        bench[col] = bench["close"]
    data["QQQ"] = bench
    dates = data["QQQ"].index[350:380]
    brk, state, reports = _replay(data, cfg, tmp_path, dates)
    assert all(not r.regime_ok for r in reports)
    assert brk.positions() == {}
    assert not any(a.startswith("PLAN") or a.startswith("BUY") for r in reports for a in r.actions)
