"""Entry modes (confirmed / resting / hybrid), the failed-breakout exit, the
trade journal features and the learning layer (post-mortems, shadow trades,
review, bounded knob adjustments)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml
from typer.testing import CliRunner

from qmag.cli import app
from qmag.config import StrategyConfig
from qmag.learning import (
    KNOB_BY_KEY,
    OVERRIDES_FILE,
    explain_trade,
    load_overrides,
    propose_adjustments,
    review,
    update_shadows,
)
from qmag.market_calendar import NY
from qmag.session import SessionSettings, TradingSession
from qmag.setups import Signal
from qmag.settings import validate_config
from qmag.trader import LiveClock, TraderState, confirmation_gates


def _session(tmp_path, csv_universe, **extra) -> TradingSession:
    return TradingSession(
        SessionSettings(**csv_universe.session_kwargs(), state_dir=tmp_path / "state", charts=False, overrides=csv_universe.overrides(**{"regime.enabled": False, **extra}))
    )


# --------------------------------------------------------------------------- #
# Entry modes
# --------------------------------------------------------------------------- #
def test_confirmed_mode_rests_nothing_and_buys_only_confirmed_breakouts(tmp_path, csv_universe):
    sess = _session(tmp_path, csv_universe)  # entry.mode defaults to confirmed
    assert sess.cfg.entry.mode == "confirmed"
    full = sess.cycle(asof="2024-05-14", label="nightly")
    assert full.entry_mode == "confirmed" and full.arming
    assert any(a.startswith("ARMED ") for a in full.actions), "watch plans are armed, not parked as orders"
    assert not any(a.startswith("PLAN buy-stop") for a in full.actions)
    state = sess.state()
    assert state.pending == {}, "confirmed mode never leaves a resting order overnight"
    assert not any(o.side == "buy" for o in sess.broker.open_orders())
    assert state.shadow and {s["kind"] for s in state.shadow} >= {"armed"}

    # 09:35: inside the opening range and before the volume pace is measurable -> nothing is bought.
    early = sess.focused_cycle(label="focused", asof="2024-05-15", now=datetime(2024, 5, 15, 9, 35, tzinfo=NY))
    assert early is not None and not any(a.startswith("BUY") for a in early.actions)
    assert any(a.startswith("HOLD ") for a in early.actions)
    assert early.held and all(h["reasons"] for h in early.held)
    assert all("opening range" in " ".join(h["reasons"]) or "too early" in " ".join(h["reasons"]) or "extended" in " ".join(h["reasons"]) for h in early.held)
    assert sess.state().pending == {}

    # 10:30: volume projected to full-day pace; a breakout holding above its pivot on pace is bought at market.
    later = sess.focused_cycle(label="focused", asof="2024-05-15", now=datetime(2024, 5, 15, 10, 30, tzinfo=NY))
    assert any(a.startswith("PACE today's volume") for a in later.actions)
    buys = [a for a in later.actions if a.startswith("BUY ")]
    assert buys and all("[confirmed:" in a for a in buys)
    assert any("wick-only" in " ".join(h["reasons"]) for h in later.held), "a name whose price fell back below the pivot is held, not bought"
    state = sess.state()
    assert state.managed
    for pos in state.managed.values():
        f = pos["features"]
        assert f["entry_mode"] == "market_confirmed" and f["entry_time"] == "10:30" and f["rvol_projected"] is True
        assert f["rvol"] >= sess.cfg.entry.confirm_volume_ratio and f["source"] in ("nightly", "focused")
        assert isinstance(f["checks"], dict) and f["failed_checks"] == []
    kinds = {s["kind"] for s in state.shadow}
    assert {"armed", "held"} <= kinds
    rep = json.loads((tmp_path / "state" / "last_report.json").read_text())
    assert rep["entry_mode"] == "confirmed" and rep["held"]


def test_hybrid_mode_parks_buy_stops_only_inside_the_session_window(tmp_path, csv_universe):
    sess = _session(tmp_path, csv_universe, **{"entry.mode": "hybrid", "entry.resting_from": "09:40", "schedule.trigger_distance_pct": 0.15})
    full = sess.cycle(asof="2024-05-14", label="nightly")
    assert not any(a.startswith("PLAN buy-stop") for a in full.actions) and sess.state().pending == {}
    assert any("from 09:40 a buy-stop is parked" in a for a in full.actions)
    before = sess.focused_cycle(label="focused", asof="2024-05-14", now=datetime(2024, 5, 14, 9, 36, tzinfo=NY))
    assert before is not None and not any(a.startswith("PLAN buy-stop") for a in before.actions)
    after = sess.focused_cycle(label="focused", asof="2024-05-14", now=datetime(2024, 5, 14, 10, 0, tzinfo=NY))
    assert any(a.startswith("PLAN buy-stop") for a in after.actions)
    pending = sess.state().pending
    assert pending and all(p["entry_kind"] == "buy_stop" and p["features"]["entry_mode"] == "buy_stop" for p in pending.values())
    # After the close the brackets are cancelled: hybrid never rests orders overnight.
    night = sess.cycle(asof="2024-05-14", label="after_close")
    assert any(a.startswith("CANCEL resting buy-stop") and "hybrid" in a for a in night.actions)
    assert sess.state().pending == {}


def test_resting_mode_respects_min_score_and_places_orders(tmp_path, csv_universe):
    sess = _session(tmp_path, csv_universe, **{"entry.mode": "resting", "entry.resting_min_score": 1e9})
    full = sess.cycle(asof="2024-05-14", label="nightly")
    assert any("no resting order, market entry on confirmation only" in a for a in full.actions)
    assert sess.state().pending == {}
    sess2 = TradingSession(SessionSettings(**csv_universe.session_kwargs(), state_dir=tmp_path / "s2", charts=False, overrides=csv_universe.overrides(**{"regime.enabled": False, "entry.mode": "resting"})))
    full2 = sess2.cycle(asof="2024-05-14", label="nightly")
    assert any(a.startswith("PLAN buy-stop") for a in full2.actions) and sess2.state().pending


def test_confirmation_gates_unit():
    cfg = StrategyConfig()
    sig = Signal(symbol="ABC", date=pd.Timestamp("2024-05-15"), setup="breakout", pivot=100.0, entry=100.2, stop=95.0, adr_dollar=5.0, adr_pct=5.0, score=1.0, details={})
    good = pd.Series({"close": 101.0, "open": 100.0, "high": 102.0, "low": 99.5, "rvol_20": 1.6})
    # Completed bar (no clock): only price and the full-day volume matter.
    assert confirmation_gates(sig, good, cfg, LiveClock()) == []
    wick = good.copy()
    wick["close"] = 99.0
    assert any("wick-only" in r for r in confirmation_gates(sig, wick, cfg, LiveClock()))
    extended = good.copy()
    extended["close"] = 107.0
    assert any("extended" in r for r in confirmation_gates(sig, extended, cfg, LiveClock()))
    thin = good.copy()
    thin["rvol_20"] = 0.7
    assert any("< 1x" in r for r in confirmation_gates(sig, thin, cfg, LiveClock()))
    # Live clock: opening range and unmeasurable pace hold the entry back.
    early = LiveClock(now=datetime(2024, 5, 15, 9, 33, tzinfo=NY), pace={"applied": False, "fraction": 0.03})
    reasons = confirmation_gates(sig, good, cfg, early)
    assert any("opening range" in r for r in reasons) and any("too early" in r for r in reasons)
    paced = LiveClock(now=datetime(2024, 5, 15, 10, 30, tzinfo=NY), pace={"applied": True, "fraction": 0.24, "multiplier": 4.17})
    assert confirmation_gates(sig, good, cfg, paced) == []
    assert paced.minutes_since_open == 60 and paced.time_label() == "10:30" and paced.volume_confirmed
    assert LiveClock(now=datetime(2024, 5, 15, 16, 30, tzinfo=NY)).bar_complete
    assert LiveClock(now=datetime(2024, 5, 18, 12, 0, tzinfo=NY)).bar_complete  # Saturday
    assert LiveClock(now=datetime(2024, 5, 15, 9, 41, tzinfo=NY)).at_or_after("09:40")


# --------------------------------------------------------------------------- #
# Post-mortems and shadow trades
# --------------------------------------------------------------------------- #
def test_explain_trade_tags_from_features_and_outcome():
    rec = {
        "symbol": "ABC", "setup": "breakout", "r_multiple": -0.6, "exit_reason": "failed_breakout", "hold_days": 0, "mfe_r": 0.1, "mae_r": -0.7,
        "features": {"entry_mode": "buy_stop", "rvol": 0.6, "theme_pct": 0.2, "entry_time": "close", "gap_pct": 0.0, "depth": 0.4, "adr_pct": 3.5},
    }
    pm = explain_trade(rec)
    assert {"failed_breakout", "wick_fill", "low_volume", "weak_theme", "deep_flag", "low_adr", "never_worked", "quick_loss"} <= set(pm["tags"])
    assert pm["lesson"].startswith("Avoid:") and "ABC breakout: loser -0.60R" in pm["text"]
    win = {"symbol": "XYZ", "setup": "breakout", "r_multiple": 3.4, "exit_reason": "trail_ma", "hold_days": 12, "mfe_r": 4.0, "mae_r": -0.2,
           "features": {"entry_mode": "market_confirmed", "rvol": 2.5, "theme_pct": 0.9, "entry_time": "10:30", "edge_score": 0.4, "depth": 0.1}}
    pm = explain_trade(win)
    assert {"runner", "strong_volume", "top_theme", "strong_edge", "tight_flag"} <= set(pm["tags"]) and pm["lesson"].startswith("Repeat:")


def _bars(start: str, closes: list[float], spread: float = 1.0) -> pd.DataFrame:
    idx = pd.bdate_range(start, periods=len(closes))
    c = np.array(closes, dtype=float)
    return pd.DataFrame({"open": c - 0.2, "high": c + spread, "low": c - spread, "close": c, "volume": 1e6}, index=idx)


def test_update_shadows_resolves_trigger_stop_target_and_expiry():
    cfg = StrategyConfig().with_overrides({"learning.shadow_max_days": 3, "learning.shadow_hold_days": 5})
    state = TraderState()
    base = {"date": "2024-05-14", "setup": "breakout", "pivot": 100.0, "stop": 95.0, "target": 110.0, "status": "open", "features": {}, "reasons": [], "failed_checks": []}
    state.shadow = [
        {**base, "symbol": "TGT", "kind": "armed", "entry": 100.2, "immediate": False},  # triggers day 2, target day 4
        {**base, "symbol": "STP", "kind": "rejected", "entry": 100.0, "immediate": True},  # fills at the close, stopped next day
        {**base, "symbol": "NVR", "kind": "armed", "entry": 100.2, "immediate": False},  # never trades up: expires
        {**base, "symbol": "GAP", "kind": "armed", "entry": 100.2, "immediate": False},  # gaps 10 % over the pivot: stop-limit would not fill
    ]
    data = {
        "TGT": _bars("2024-05-14", [98, 99, 101, 104, 111, 112]),
        "STP": _bars("2024-05-14", [100, 92, 91, 90, 90, 90]),
        "NVR": _bars("2024-05-14", [98, 97, 96, 95, 94, 93]),
        "GAP": _bars("2024-05-14", [98, 112, 113, 114, 115, 116], spread=0.5),
    }
    n = update_shadows(state, data, cfg, "2024-05-21")
    by = {s["symbol"]: s for s in state.shadow}
    assert by["TGT"]["status"] == "closed" and by["TGT"]["exit_reason"] == "target" and by["TGT"]["r_multiple"] > 1.5 and by["TGT"]["triggered_on"] == "2024-05-16"
    assert by["STP"]["status"] == "closed" and by["STP"]["exit_reason"] == "stop" and by["STP"]["r_multiple"] <= -1.0
    assert by["NVR"]["status"] == "expired"
    assert by["GAP"]["status"] == "expired", "an open above pivot * (1 + max_gap) never fills the hypothetical stop-limit"
    assert n == 4


# --------------------------------------------------------------------------- #
# The review: buckets, lessons, bounded adjustments, overrides layered on the session
# --------------------------------------------------------------------------- #
def _closed(i: int, r: float, rvol: float, theme_pct: float = 0.7, mode: str = "market_confirmed", entry_time: str = "10:30") -> dict:
    return {
        "symbol": f"S{i:02d}", "setup": "breakout", "entry_date": "2024-05-01", "closed_on": "2024-05-10", "exit_reason": "stop" if r < 0 else "trail_ma",
        "entry_price": 100.0, "initial_stop": 95.0, "shares": 10, "r_multiple": r, "pnl": r * 50, "hold_days": 7, "mfe_r": max(r, 0.2), "mae_r": min(r, -0.1),
        "features": {"entry_mode": mode, "rvol": rvol, "theme_pct": theme_pct, "entry_time": entry_time, "setup": "breakout", "source": "nightly", "adr_pct": 5.0, "depth": 0.2, "gap_pct": 0.01, "edge_score": 0.2, "regime_ok": True},
    }


def _journal_state() -> TraderState:
    state = TraderState()
    # Six trades that only just cleared the 1.2x volume rule lost; six on strong volume won.
    state.closed = [_closed(i, -0.9, 1.25) for i in range(6)] + [_closed(10 + i, 1.4, 2.4) for i in range(6)]
    # Shadows the theme check alone blocked would have done well.
    state.shadow = [
        {"symbol": f"T{i}", "kind": "rejected", "date": "2024-05-02", "setup": "breakout", "pivot": 50.0, "entry": 50.1, "stop": 47.0, "target": 56.0, "immediate": False,
         "reasons": ["theme_strength"], "failed_checks": ["theme_strength"], "features": {"theme_pct": 0.25}, "status": "closed", "r_multiple": 1.2, "exit_reason": "target"}
        for i in range(5)
    ]
    return state


def test_review_finds_lessons_and_moves_knobs_one_bounded_step(tmp_path):
    cfg = StrategyConfig()
    state = _journal_state()
    report = review(state, cfg, tmp_path, now=datetime(2026, 9, 12, 11, 0), apply=True, ai=False)
    assert report["status"] == "ok" and report["trades"] == 12
    assert report["overall"]["win_rate"] == 0.5 and abs(report["overall"]["avg_r"] - 0.25) < 1e-6
    texts = " ".join(l["text"] for l in report["lessons"])
    assert "Relative volume at entry = 1.2-1.5x" in texts and "worse" in texts
    assert "Relative volume at entry = 2-3x" in texts and "better" in texts
    assert "Check 'theme_strength' is costly" in texts
    keys = {a["key"]: a for a in report["adjustments"]}
    assert keys["breakout.min_breakout_volume_ratio"]["direction"] == "tighten" and keys["breakout.min_breakout_volume_ratio"]["to"] == pytest.approx(1.3)
    assert keys["themes.min_theme_percentile"]["direction"] == "loosen" and keys["themes.min_theme_percentile"]["to"] == pytest.approx(0.25)
    assert "entry.confirm_volume_ratio" not in keys, "no trade sat in that knob's marginal band"
    assert report["proposed_only"] and report["applied"] == []
    assert report["evidence"]["verified"] == 0
    assert not (tmp_path / OVERRIDES_FILE).exists(), "unverified evidence never changes settings"
    assert (tmp_path / "learning_report.json").exists()
    assert all(rec.get("post_mortem") for rec in state.closed)
    assert report["ai"]["enabled"] is False


def test_adjustments_need_evidence_and_stay_inside_the_band():
    cfg = StrategyConfig()
    state = _journal_state()
    thin = [_closed(i, -1.0, 1.25) for i in range(3)]  # fewer than min_trades overall
    assert propose_adjustments(thin, [], cfg, {"overrides": {}, "history": []}, datetime(2026, 9, 12)) == []
    knob = KNOB_BY_KEY["breakout.min_breakout_volume_ratio"]
    assert knob.tighter(2.5) == 2.5 and knob.looser(1.0) == 1.0  # clamped at the band edges
    at_edge = cfg.with_overrides({"breakout.min_breakout_volume_ratio": 2.5})
    band_trades = [_closed(i, -1.0, 2.55) for i in range(6)] + [_closed(10 + i, 1.0, 3.5) for i in range(6)]
    adj = propose_adjustments(band_trades, [], at_edge, {"overrides": {}, "history": []}, datetime(2026, 9, 12))
    assert not any(a["key"] == "breakout.min_breakout_volume_ratio" for a in adj), "already at the top of its band"
    assert propose_adjustments(state.closed, state.shadow, cfg.with_overrides({"learning.min_lift_r": 5.0}), {"overrides": {}, "history": []}, datetime(2026, 9, 12)) == []


def test_session_layers_learned_overrides_and_reset_restores_settings(tmp_path, csv_universe):
    sess = _session(tmp_path, csv_universe)
    assert sess.cfg.breakout.min_breakout_volume_ratio == pytest.approx(1.2) and sess.learned_overrides == {}
    state = _journal_state()
    state.save(sess.state_path)
    report = sess.learn(ai=False)
    assert report["adjustments"] and sess.learned_overrides == {}  # not applied to the running config until the next reload
    assert report["proposed_only"] and not (sess.state_dir / OVERRIDES_FILE).exists()
    assert sess.cfg.breakout.min_breakout_volume_ratio == pytest.approx(1.2)
    # Existing bounded overrides are respected only with explicit opt-in.
    (sess.state_dir / OVERRIDES_FILE).write_text(yaml.safe_dump({"overrides": {"breakout.min_breakout_volume_ratio": 1.3}}))
    enabled = _session(tmp_path, csv_universe, **{"learning.auto_apply": True, "autonomy.enabled": False})
    assert enabled.cfg.breakout.min_breakout_volume_ratio == pytest.approx(1.3)
    pinned = _session(tmp_path, csv_universe, **{"learning.auto_apply": True, "breakout.min_breakout_volume_ratio": 1.5})
    assert pinned.cfg.breakout.min_breakout_volume_ratio == pytest.approx(1.5)
    assert enabled.reset_learning() is True
    assert enabled.cfg.breakout.min_breakout_volume_ratio == pytest.approx(1.2)


def test_learn_cli_and_learning_page(tmp_path, csv_universe):
    sess = _session(tmp_path, csv_universe)
    _journal_state().save(sess.state_path)
    runner = CliRunner()
    out = runner.invoke(app, ["learn", "--state-dir", str(tmp_path / "state"), "--no-ai"])
    assert out.exit_code == 0, out.output
    assert "Learning review" in out.output and "Adjustments proposed" in out.output and "breakout volume ratio" in out.output
    raw = runner.invoke(app, ["learn", "--state-dir", str(tmp_path / "state"), "--no-ai", "--no-apply", "--json"])
    assert raw.exit_code == 0 and '"proposed_only": true' in raw.output
    from fastapi.testclient import TestClient

    from qmag.dashboard import create_app

    client = TestClient(create_app(sess))
    page = client.get("/learning")
    assert page.status_code == 200 and "Learned values in force" in page.text and "breakout volume ratio" in page.text
    assert client.get("/api/learning").json()["report"]["trades"] == 12
    assert client.get("/").status_code == 200
    assert client.post("/api/learning/reset").json()["reset"] is False
    reset = runner.invoke(app, ["learn", "--state-dir", str(tmp_path / "state"), "--reset"])
    assert reset.exit_code == 0 and "No learned adjustments" in reset.output
    once = runner.invoke(app, ["daemon", "--state-dir", str(tmp_path / "state"), "--symbols", "AAA", "--once", "learn", "--no-universe-rebuild", "-q"])
    assert once.exit_code == 0, once.output


def test_settings_validation_covers_entry_and_learning():
    cfg = StrategyConfig()
    validate_config(cfg)
    with pytest.raises(ValueError):
        validate_config(cfg.with_overrides({"entry.mode": "yolo"}))
    with pytest.raises(ValueError):
        validate_config(cfg.with_overrides({"entry.resting_from": "9am"}))
    with pytest.raises(ValueError):
        validate_config(cfg.with_overrides({"learning.min_trades": 1}))
    with pytest.raises(ValueError):
        validate_config(cfg.with_overrides({"learning.review_weekday": "someday"}))
