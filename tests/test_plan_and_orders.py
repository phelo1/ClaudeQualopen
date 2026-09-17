import pandas as pd

from qmag.backtest import prepare_data, run_backtest
from qmag.broker import Order, PaperBroker, orders_equivalent
from qmag.config import StrategyConfig
from qmag.plan import build_plan, partial_quantity, size_shares
from qmag.setups import BreakoutDetector
from qmag.trader import ManagedPosition, TraderState, _desired_exits, run_cycle


def test_size_shares_respects_risk_and_caps():
    cfg = StrategyConfig().with_overrides({"risk.risk_per_trade_pct": 0.005, "risk.max_position_pct": 0.25})
    shares = size_shares(100_000, 50.0, 47.5, cfg, exposure=0.0, cash=100_000)
    assert shares == 200  # $500 risk / $2.50 per share
    # Position cap binds when the stop is very tight.
    tight = size_shares(100_000, 50.0, 49.9, cfg, exposure=0.0, cash=100_000)
    assert tight == 500  # 25% of equity / $50
    # Cash and exposure room bind too.
    assert size_shares(100_000, 50.0, 47.5, cfg, exposure=0.0, cash=1_000) == 19  # reserve the configured slippage
    assert size_shares(100_000, 50.0, 47.5, cfg, exposure=100_000, cash=100_000) == 0
    assert size_shares(100_000, 50.0, 55.0, cfg, exposure=0.0, cash=100_000) == 0


def test_partial_quantity_keeps_a_runner():
    assert partial_quantity(3, 0.33) == 1
    assert partial_quantity(1, 0.33) == 0
    assert partial_quantity(100, 0.33) == 33
    assert partial_quantity(2, 0.9) == 1


def test_build_plan_has_target_and_checks():
    from tests.conftest import make_breakout_frame

    breakout_frame = make_breakout_frame()
    cfg = StrategyConfig().with_overrides({"regime.enabled": False, "themes.enabled": False})
    data = prepare_data({"ABC": breakout_frame}, cfg)
    df = data["ABC"]
    sig = BreakoutDetector().detect_last("ABC", df, cfg)
    assert sig is not None
    plan = build_plan(sig, df.iloc[-1], cfg, equity=100_000, exposure=0.0, cash=100_000, regime_ok=True)
    assert plan.ok, plan.failed_checks
    assert plan.stop < plan.entry < plan.partial_target
    r = plan.entry - plan.stop
    assert abs((plan.partial_target - plan.entry) / r - cfg.management.partial_target_r) < 1e-9
    assert 0 < plan.partial_qty < plan.shares
    assert plan.risk_pct <= cfg.risk.risk_per_trade_pct + 1e-9
    assert "1/3 off at" in plan.summary()
    off = build_plan(sig, df.iloc[-1], cfg, equity=100_000, exposure=0.0, cash=100_000, regime_ok=False)
    assert not off.ok and off.failed_checks == ["market_regime"]


def test_backtest_takes_partial_at_target(synthetic_data):
    cfg = StrategyConfig().with_overrides({"regime.enabled": False, "management.partial_target_r": 1.0})
    res = run_backtest(synthetic_data, cfg)
    reasons = {t.exit_reason for t in res.trades}
    assert res.trades
    assert any(t.partial_taken for t in res.trades)
    # With a tight 1R target the partial should regularly fire before the day-3 rule.
    off = StrategyConfig().with_overrides({"regime.enabled": False, "management.partial_target_r": None})
    res_off = run_backtest(synthetic_data, off)
    assert res_off.trades
    assert reasons  # sanity


def test_paper_oco_fills_target_or_stop(tmp_path):
    brk = PaperBroker(tmp_path / "l.json", starting_cash=10_000, slippage_bps=0)
    brk.mark({"XYZ": pd.Series({"open": 10, "high": 10, "low": 10, "close": 10})})
    brk.market_buy("XYZ", 100)
    brk.oco_sell("XYZ", 33, limit_price=12.0, stop_price=9.0)
    brk.stop_sell("XYZ", 67, 9.0)
    # Target touched: 33 shares go at the limit, the rest stays.
    fills = brk.mark({"XYZ": pd.Series({"open": 11, "high": 12.5, "low": 10.8, "close": 12})})
    assert [f.kind for f in fills] == ["oco"] and fills[0].fill_price == 12.0
    assert brk.positions()["XYZ"].qty == 67
    # Then the stop goes.
    fills = brk.mark({"XYZ": pd.Series({"open": 9.5, "high": 9.6, "low": 8.5, "close": 8.6})})
    assert [f.kind for f in fills] == ["stop"]
    assert "XYZ" not in brk.positions()
    # A bar that touches both legs is resolved as a stop (conservative).
    brk.market_buy("XYZ", 10)
    brk.oco_sell("XYZ", 10, limit_price=12.0, stop_price=8.0)
    fills = brk.mark({"XYZ": pd.Series({"open": 9, "high": 13, "low": 7, "close": 9})})
    assert fills[0].fill_price == 8.0


def test_orders_equivalent_ignores_ids_and_order():
    a = [Order("1", "X", "sell", 33, "oco", trigger=9.0, limit=12.0), Order("2", "X", "sell", 67, "stop", trigger=9.0)]
    b = [Order("", "X", "sell", 67, "stop", trigger=9.004), Order("", "X", "sell", 33, "oco", trigger=9.0, limit=12.0)]
    assert orders_equivalent(a, b)
    assert not orders_equivalent(a, b[:1])
    assert not orders_equivalent(a, [Order("", "X", "sell", 67, "stop", trigger=9.5), b[1]])


def test_desired_exits_follow_the_plan():
    pos = ManagedPosition("X", "breakout", "2024-01-01", 10.0, 100, 9.0, 9.0, remaining=100, target=12.0, partial_qty=33)
    kinds = [(o.kind, o.qty) for o in _desired_exits(pos)]
    assert kinds == [("oco", 33), ("stop", 67)]
    pos.partial_done, pos.remaining, pos.stop = True, 67, 10.0
    assert [(o.kind, o.qty, o.trigger) for o in _desired_exits(pos)] == [("stop", 67, 10.0)]


def test_trader_places_oco_and_detects_partial_fill(synthetic_data, tmp_path):
    cfg = StrategyConfig().with_overrides({"regime.enabled": False, "management.partial_target_r": 0.5})
    brk = PaperBroker(tmp_path / "ledger.json", starting_cash=cfg.risk.starting_equity)
    state = TraderState()
    dates = synthetic_data["QQQ"].index[350:420]
    saw_oco = saw_partial = False
    for d in dates:
        sliced = {s: df.loc[:d] for s, df in synthetic_data.items()}
        rep = run_cycle(sliced, brk, cfg, state, asof=d)
        saw_oco |= any(o.kind == "oco" for o in brk.open_orders())
        saw_partial |= any(a.startswith("PARTIAL filled") for a in rep.actions)
        for sym, rec in state.managed.items():
            pos = ManagedPosition(**rec)
            assert pos.remaining == brk.positions()[sym].qty
            if pos.partial_done:
                assert pos.stop >= pos.entry_price
    assert saw_oco, "no OCO target orders were placed"
    assert saw_partial, "no partial fills were detected from the broker"
    # State survives a JSON round trip with the new fields.
    state.save(tmp_path / "trader.json")
    again = TraderState.load(tmp_path / "trader.json")
    assert again.managed.keys() == state.managed.keys()
