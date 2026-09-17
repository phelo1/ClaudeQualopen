"""Theme momentum, market-sentiment regime and pluggable sentiment."""

import numpy as np
import pandas as pd
import pytest

from qmag.backtest import prepare_data, run_backtest
from qmag.config import StrategyConfig
from qmag.regime import market_breadth, regime_series, regime_snapshot
from qmag.sentiment import CsvSentimentProvider, attach_sentiment_columns, sentiment_ok
from qmag.setups import BreakoutDetector, EpisodicPivotDetector, momentum_score
from qmag.themes import attach_theme_columns, latest_theme_leaderboard, theme_indices, theme_ok
from tests.conftest import make_breakout_frame, make_ep_frame
from tests.synthetic import synthetic_themes


def _trend_frame(daily_ret: float, n: int = 300, start: float = 100.0) -> pd.DataFrame:
    idx = pd.bdate_range("2024-01-02", periods=n)
    close = start * np.cumprod(np.full(n, 1 + daily_ret))
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1e6}, index=idx)


@pytest.fixture
def themed_data():
    # hot theme: two steady risers; cold theme: two steady fallers; one loner
    data = {
        "HOT1": _trend_frame(0.004),
        "HOT2": _trend_frame(0.003),
        "COLD1": _trend_frame(-0.003),
        "COLD2": _trend_frame(-0.002),
        "LONER": _trend_frame(0.001),
        "QQQ": _trend_frame(0.0005),
    }
    cfg = StrategyConfig().with_overrides({"themes.groups": {"hot": ["HOT1", "HOT2"], "cold": ["COLD1", "COLD2"]}, "themes.min_theme_members": 2})
    return data, cfg


def test_theme_indices_and_ranking(themed_data):
    data, cfg = themed_data
    idx = theme_indices(data, {"hot": ["HOT1", "HOT2"], "cold": ["COLD1", "COLD2"], "missing": ["NOPE", "NADA"]}, min_members=2)
    assert set(idx.columns) == {"hot", "cold"}  # theme with no members present is skipped
    assert idx["hot"].iloc[-1] > idx["cold"].iloc[-1]

    enriched = attach_theme_columns(prepare_data(data, cfg.with_overrides({"themes.enabled": False})), cfg)
    hot, cold, loner = enriched["HOT1"].iloc[-1], enriched["COLD1"].iloc[-1], enriched["LONER"].iloc[-1]
    assert hot["theme"] == "hot" and cold["theme"] == "cold"
    assert hot["theme_pct"] == 1.0 and cold["theme_pct"] == 0.5
    assert hot["theme_breadth"] == 1.0 and cold["theme_breadth"] == 0.0
    assert np.isnan(loner["theme_pct"])

    board = latest_theme_leaderboard(enriched, cfg)
    assert list(board["theme"]) == ["hot", "cold"]


def test_one_rocket_does_not_make_a_theme():
    """Six-name industry: one stock triples, five drift. Mean-based it looked +40 %; the median says flat."""
    rocket = _trend_frame(0.03)  # ~ +88 % over 21 sessions
    flat = {f"F{i}": _trend_frame(0.0002 * (i - 2)) for i in range(5)}  # tiny drifts around zero
    data = {"ROCKET": rocket, **flat, "STEADY1": _trend_frame(0.004), "STEADY2": _trend_frame(0.0035), "STEADY3": _trend_frame(0.003), "QQQ": _trend_frame(0.0005)}
    themes = {"one_rocket": ["ROCKET", *flat], "broad": ["STEADY1", "STEADY2", "STEADY3"], "duo": ["STEADY1", "ROCKET"]}
    cfg = StrategyConfig().with_overrides({"themes.groups": themes})
    board = latest_theme_leaderboard(prepare_data(data, cfg.with_overrides({"themes.enabled": False})), cfg)
    rows = {r["theme"]: r for r in board.to_dict("records")}
    assert "duo" not in rows  # two members are not a theme (min_theme_members = 3)
    assert rows["broad"]["gain_1m"] > 0.05 and abs(rows["one_rocket"]["gain_1m"]) < 0.02
    assert list(board["theme"]) == ["broad", "one_rocket"] and rows["one_rocket"]["members"] == 6
    # The rocket itself is not in a hot theme: it is judged on its own merits, not on a theme it made up.
    enriched = attach_theme_columns(prepare_data(data, cfg.with_overrides({"themes.enabled": False})), cfg)
    assert enriched["ROCKET"].iloc[-1]["theme"] == "one_rocket" and enriched["ROCKET"].iloc[-1]["theme_pct"] < enriched["STEADY1"].iloc[-1]["theme_pct"]


def test_theme_gate_applies_to_breakouts_only(themed_data):
    data, cfg = themed_data
    enriched = prepare_data(data, cfg)
    cold = enriched["COLD1"].iloc[-1]
    hot = enriched["HOT1"].iloc[-1]
    loner = enriched["LONER"].iloc[-1]
    # cold theme: bottom half and zero breadth -> fails the breakout gate
    assert not theme_ok(cold, cfg, "breakout")
    assert theme_ok(cold, cfg, "episodic_pivot")  # EPs are never theme-gated by default
    assert theme_ok(hot, cfg, "breakout")
    assert theme_ok(loner, cfg, "breakout")  # no theme -> passes unless require_theme
    assert not theme_ok(loner, cfg.with_overrides({"themes.require_theme": True}), "breakout")
    # ranking bonus is what separates hot from cold for every setup
    assert momentum_score(hot, cfg) - momentum_score(hot, cfg.with_overrides({"themes.enabled": False})) == pytest.approx(1.0)


def test_breakout_in_cold_theme_is_skipped(cfg):
    frame = make_breakout_frame()
    # Build a cold theme around the breakout stock: two collapsing peers.
    peers = {"P1": _trend_frame(-0.004, n=len(frame)), "P2": _trend_frame(-0.004, n=len(frame))}
    for p in peers.values():
        p.index = frame.index
    # And a hot theme so the cold one ranks in the bottom 30 %.
    hot = {"H1": _trend_frame(0.004, n=len(frame)), "H2": _trend_frame(0.004, n=len(frame)), "H3": _trend_frame(0.004, n=len(frame))}
    for h in hot.values():
        h.index = frame.index
    groups = {"cold": ["X", "P1", "P2"], "hot": ["H1", "H2"], "hot2": ["H2", "H3"], "hot3": ["H1", "H3"]}
    data = {"X": frame, **peers, **hot}
    gated = StrategyConfig().with_overrides({"themes.groups": groups, "themes.min_theme_percentile": 0.3, "themes.min_theme_members": 2})
    ungated = gated.with_overrides({"themes.enabled": False})
    assert BreakoutDetector().detect("X", prepare_data(data, ungated)["X"], ungated)
    assert BreakoutDetector().detect("X", prepare_data(data, gated)["X"], gated) == []


def test_episodic_pivot_ignores_theme_gate():
    frame = make_ep_frame()
    peers = {"P1": _trend_frame(-0.004, n=len(frame)), "P2": _trend_frame(-0.004, n=len(frame))}
    hot = {"H1": _trend_frame(0.004, n=len(frame)), "H2": _trend_frame(0.004, n=len(frame))}
    for f in (*peers.values(), *hot.values()):
        f.index = frame.index
    cfg = StrategyConfig().with_overrides({"themes.groups": {"cold": ["E", "P1", "P2"], "hot": ["H1", "H2"]}, "themes.min_theme_percentile": 0.9, "themes.min_theme_members": 2})
    data = prepare_data({"E": frame, **peers, **hot}, cfg)
    assert len(EpisodicPivotDetector().detect("E", data["E"], cfg)) == 1
    strict = cfg.with_overrides({"themes.apply_to": ["breakout", "episodic_pivot"]})
    assert EpisodicPivotDetector().detect("E", prepare_data({"E": frame, **peers, **hot}, strict)["E"], strict) == []


def test_breadth_regime_gate():
    data = {
        "UP1": _trend_frame(0.003),
        "UP2": _trend_frame(0.002),
        "DN1": _trend_frame(-0.003),
        "DN2": _trend_frame(-0.002),
        "DN3": _trend_frame(-0.002),
        "QQQ": _trend_frame(0.002),
    }
    cfg = StrategyConfig().with_overrides({"themes.enabled": False})
    breadth = market_breadth(data, cfg).dropna()
    assert breadth.iloc[-1] == pytest.approx(0.4)  # 2 of 5 tradeable names above their MA
    ok, gates = regime_series(data, cfg)
    assert gates == {"benchmark": True, "breadth": True, "vix": False}
    assert bool(ok.iloc[-1])  # 40 % meets the default 40 % floor
    strict = cfg.with_overrides({"regime.min_breadth": 0.6})
    ok_strict, _ = regime_series(data, strict)
    assert not bool(ok_strict.iloc[-1])
    snap = regime_snapshot(data, strict)
    assert snap.breadth == pytest.approx(0.4) and snap.breadth_ok is False and snap.benchmark_ok is True
    assert "breadth 40%" in snap.describe(strict)


def test_vix_gate_when_configured():
    data = {"UP1": _trend_frame(0.003), "UP2": _trend_frame(0.002), "QQQ": _trend_frame(0.002), "^VIX": _trend_frame(0.0, start=35.0)}
    cfg = StrategyConfig().with_overrides({"themes.enabled": False, "regime.max_vix": 30.0})
    assert "^VIX" in cfg.auxiliary_symbols
    ok, gates = regime_series(data, cfg)
    assert gates["vix"] and not bool(ok.iloc[-1])
    calm = cfg.with_overrides({"regime.max_vix": 40.0})
    ok_calm, _ = regime_series(data, calm)
    assert bool(ok_calm.iloc[-1])
    # Breadth must not count the VIX series as a stock.
    assert market_breadth(data, cfg).dropna().iloc[-1] == pytest.approx(1.0)


def test_backtest_with_breadth_gate_blocks_entries(synthetic_data):
    cfg = StrategyConfig().with_overrides({"themes.enabled": False, "regime.min_breadth": 1.01})  # impossible threshold
    res = run_backtest(synthetic_data, cfg)
    assert res.regime_gates["breadth"] and res.signals_taken == 0


def test_csv_sentiment_provider_and_gate(tmp_path):
    csv = tmp_path / "sent.csv"
    csv.write_text("date,symbol,score,buzz\n2024-03-01,ABC,0.8,3.0\n2024-03-02,ABC,-0.5,1.0\n2024-03-01,XYZ,0.1,\n")
    frame = _trend_frame(0.001, n=60)
    frame.index = pd.bdate_range("2024-02-01", periods=60)
    cfg = StrategyConfig().with_overrides(
        {"themes.enabled": False, "sentiment.enabled": True, "sentiment.path": str(csv), "sentiment.max_score": 0.7, "sentiment.min_score": -0.2}
    )
    data = attach_sentiment_columns({"ABC": frame.copy(), "XYZ": frame.copy(), "NONE": frame.copy()}, cfg, CsvSentimentProvider(csv))
    abc = data["ABC"]
    assert abc.loc["2024-03-01", "sentiment"] == 0.8
    # 2024-03-02 is a Saturday: its reading applies to Monday 03-04 and is forward-filled 3 days.
    assert abc.loc["2024-03-04", "sentiment"] == -0.5
    assert abc.loc["2024-03-06", "sentiment"] == -0.5
    assert np.isnan(abc.loc["2024-03-12", "sentiment"])
    assert np.isnan(data["NONE"]["sentiment"]).all()

    assert not sentiment_ok(abc.loc["2024-03-01"], cfg)  # too euphoric (crowded-trade guard)
    assert not sentiment_ok(abc.loc["2024-03-04"], cfg)  # too negative
    assert sentiment_ok(data["XYZ"].loc["2024-03-01"], cfg)
    assert sentiment_ok(data["NONE"].iloc[-1], cfg)  # missing sentiment is not a signal


def test_sentiment_csv_validation(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("date,ticker,value\n2024-01-01,ABC,0.1\n")
    with pytest.raises(ValueError):
        CsvSentimentProvider(bad).scores(["ABC"])


def test_synthetic_themes_partition():
    groups = synthetic_themes([f"S{i}" for i in range(20)], n_groups=4)
    assert len(groups) == 4 and sum(len(v) for v in groups.values()) == 20


def test_config_roundtrip_with_context(tmp_path):
    cfg = StrategyConfig().with_overrides({"themes.groups": {"a": ["X", "Y"]}, "themes.min_theme_members": 2, "regime.max_vix": 30.0, "sentiment.enabled": True, "sentiment.path": "s.csv"})
    path = tmp_path / "c.yaml"
    cfg.save(path)
    assert StrategyConfig.load(path) == cfg
