import pandas as pd

from qmag.backtest import prepare_data
from qmag.setups import BreakoutDetector, EpisodicPivotDetector
from tests.conftest import make_breakout_frame, make_ep_frame


def test_breakout_detected_on_hand_built_flag(cfg):
    df = prepare_data({"X": make_breakout_frame()}, cfg)["X"]
    signals = BreakoutDetector().detect("X", df, cfg)
    assert signals, "expected a breakout signal on the final bar"
    sig = signals[-1]
    assert sig.date == df.index[-1]
    assert sig.setup == "breakout"
    assert sig.stop < sig.entry
    assert abs(sig.pivot - df["high"].iloc[-22:-1].max()) < 1e-6
    assert sig.details["flag_days"] >= cfg.breakout.min_flag_days


def test_breakout_watchlist_ready_before_trigger(cfg):
    full = make_breakout_frame()
    df = prepare_data({"X": full.iloc[:-1]}, cfg)["X"]  # drop the breakout day
    plan = BreakoutDetector().watchlist("X", df, cfg)
    assert plan is not None
    assert plan.entry > plan.pivot > plan.stop
    assert plan.details["distance_to_pivot_pct"] > 0


def test_breakout_requires_volume(cfg):
    frame = make_breakout_frame()
    frame.loc[frame.index[-1], "volume"] = 100_000.0
    df = prepare_data({"X": frame}, cfg)["X"]
    assert not any(s.date == df.index[-1] for s in BreakoutDetector().detect("X", df, cfg))


def test_breakout_skips_big_gap(cfg):
    frame = make_breakout_frame()
    pivot = frame["high"].iloc[-22:-1].max()
    frame.loc[frame.index[-1], "open"] = pivot * 1.10
    frame.loc[frame.index[-1], "high"] = pivot * 1.12
    df = prepare_data({"X": frame}, cfg)["X"]
    assert not any(s.date == df.index[-1] for s in BreakoutDetector().detect("X", df, cfg))


def test_episodic_pivot_detected(cfg):
    df = prepare_data({"E": make_ep_frame()}, cfg)["E"]
    sigs = EpisodicPivotDetector().detect("E", df, cfg)
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.setup == "episodic_pivot"
    assert sig.details["gap_pct"] > 20
    assert sig.stop < sig.entry <= df["high"].iloc[-1]


def test_episodic_pivot_rejects_extended_stock(cfg):
    frame = make_ep_frame()
    # Make the prior 3 months a +80 % run: the stock is no longer "neglected".
    ramp = pd.Series(range(len(frame)), index=frame.index).clip(lower=len(frame) - 70) - (len(frame) - 70)
    factor = (1 + 0.6 * ramp / 70).to_numpy()
    for col in ("open", "high", "low", "close"):
        frame[col] = frame[col] * factor
    df = prepare_data({"E": frame}, cfg)["E"]
    assert EpisodicPivotDetector().detect("E", df, cfg) == []


def test_episodic_pivot_needs_volume(cfg):
    frame = make_ep_frame()
    frame.loc[frame.index[-1], "volume"] = 2_000_000.0
    df = prepare_data({"E": frame}, cfg)["E"]
    assert EpisodicPivotDetector().detect("E", df, cfg) == []
