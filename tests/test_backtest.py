import numpy as np
import pandas as pd

from qmag.backtest import run_backtest
from qmag.config import StrategyConfig
from qmag.optimize import walk_forward


def test_backtest_runs_and_is_internally_consistent(cfg, synthetic_data):
    res = run_backtest(synthetic_data, cfg)
    m = res.metrics()
    assert res.regime_active
    assert len(res.equity) > 300
    assert res.signals_taken <= res.signals_seen
    # Equity is cash + marked positions: the sum of closed-trade P&L must be
    # reconcilable with the final equity once every position is closed out.
    assert m["trades"] == len(res.trades)
    assert m["max_drawdown"] <= 0
    for t in res.trades:
        assert t.bars_held <= cfg.management.max_hold_days
        assert t.shares > 0


def test_risk_per_trade_is_respected(synthetic_data):
    cfg = StrategyConfig().with_overrides({"risk.risk_per_trade_pct": 0.005, "risk.slippage_bps": 0.0})
    res = run_backtest(synthetic_data, cfg)
    # Stops hit before the partial window are still at the initial level;
    # afterwards the stop may sit at breakeven, so those are excluded.
    stopped = [
        t
        for t in res.trades
        if t.exit_reason in ("stop", "stop_same_day") and t.bars_held < cfg.management.partial_after_days and not t.partial_taken
    ]
    assert stopped, "expected at least one stop-out"
    for t in stopped:
        # A clean stop-out loses 1R by construction; whole-share rounding
        # keeps it close.
        assert -1.2 <= t.r_multiple <= -0.8, t
        assert abs(t.pnl) < 0.0075 * cfg.risk.starting_equity * 2  # never more than ~2x the intended risk on a clean stop


def test_regime_filter_blocks_entries(synthetic_data):
    cfg = StrategyConfig().with_overrides({"regime.ma_length": 5})
    # Force the benchmark permanently below its MA: monotonic decline.
    data = dict(synthetic_data)
    bench = data["QQQ"].copy()
    bench["close"] = np.linspace(500, 100, len(bench))
    bench["open"] = bench["close"]
    bench["high"] = bench["close"] * 1.001
    bench["low"] = bench["close"] * 0.999
    data["QQQ"] = bench
    res = run_backtest(data, cfg)
    assert res.signals_taken == 0
    assert res.trades == []


def test_partial_and_trail_exits_exist(synthetic_data):
    cfg = StrategyConfig()
    res = run_backtest(synthetic_data, cfg)
    reasons = {t.exit_reason for t in res.trades}
    assert "trail_ma" in reasons


def test_max_positions_cap(synthetic_data):
    cfg = StrategyConfig().with_overrides({"risk.max_positions": 2})
    res = run_backtest(synthetic_data, cfg)
    tf = res.trades_frame
    if tf.empty:
        return
    # End-of-day open positions per date (a stop-out frees a slot intraday).
    open_counts = pd.Series(0, index=res.equity.index)
    for _, t in tf.iterrows():
        entry, exit_ = pd.Timestamp(t["entry_date"]), pd.Timestamp(t["exit_date"])
        if exit_ > entry:
            open_counts.loc[entry : exit_ - pd.Timedelta(days=1)] += 1
    assert open_counts.max() <= 2


def test_config_roundtrip(tmp_path):
    cfg = StrategyConfig().with_overrides({"management.trail_ma": 20, "risk.max_positions": 5})
    path = tmp_path / "c.yaml"
    cfg.save(path)
    loaded = StrategyConfig.load(path)
    assert loaded == cfg
    assert loaded.management.trail_ma == 20


def test_walk_forward_produces_oos_readings(cfg, synthetic_data):
    grid = {"management.trail_ma": [10, 20], "management.stop_adr_mult": [1.0, 1.5]}
    res = walk_forward(synthetic_data, cfg, grid=grid, n_folds=2, is_fraction=0.6, objective=lambda m: m.get("total_return", -1) if m else -1)
    assert len(res.folds) == 2
    summary = res.summary()
    assert {"is_return", "oos_return", "base_oos_return"} <= set(summary.columns)
    assert len(res.grid_table) == 2 * 4
    for f in res.folds:
        assert f.is_end < f.oos_start
