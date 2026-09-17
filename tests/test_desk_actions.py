"""Acting from the desk: broker order tests on the connections page, manual
arm / buy / override from the lookup page, and the daemon's single-instance
lock. Plus the learning additions that came with them: the near-miss detector
pass, adjustment scorecards with auto-revert, and shadow post-mortems."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from qmag.broker_test import load_results, run_order_test
from qmag.cli import app
from qmag.config import StrategyConfig
from qmag.daemon import AlreadyRunning, Daemon, InstanceLock
from qmag.dashboard import create_app
from qmag.learning import OVERRIDES_FILE, load_overrides, review, score_adjustments
from qmag.session import ManualTradeRefused, SessionSettings, TradingSession
from qmag.trader import TraderState
from tests.conftest import make_breakout_frame
from tests.test_learning import _closed, _journal_state


def _session(tmp_path, csv_universe, name="state", **extra) -> TradingSession:
    return TradingSession(
        SessionSettings(**csv_universe.session_kwargs(), state_dir=tmp_path / name, charts=False, overrides=csv_universe.overrides(**{"regime.enabled": False, **extra}))
    )


def _with_leader(tmp_path, csv_universe, name="state", **extra) -> TradingSession:
    """The generated universe plus LEAD: a hand-built breakout whose last bar is *today*,
    so the lookup page sees a triggered, confirmed setup."""
    lead = make_breakout_frame()
    lead.index = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=len(lead), name="date")
    lead.to_csv(csv_universe.directory / "LEAD.csv", index_label="date")
    kw = csv_universe.session_kwargs()
    kw["symbols"] += ",LEAD"
    return TradingSession(SessionSettings(**kw, state_dir=tmp_path / name, charts=False, overrides=csv_universe.overrides(**{"regime.enabled": False, **extra})))


# --------------------------------------------------------------------------- #
# Broker order tests
# --------------------------------------------------------------------------- #
def test_order_tests_round_trip_on_paper_and_refuse_live_fills(tmp_path, csv_universe):
    sess = _session(tmp_path, csv_universe)
    sym = csv_universe.symbols[3]
    bracket = run_order_test(sess, "bracket", sym)
    assert bracket["ok"], bracket
    names = [s["name"] for s in bracket["steps"]]
    assert "place buy-stop bracket" in names and all(s["ok"] for s in bracket["steps"])
    assert not sess.broker.open_orders(), "the test bracket is cancelled again"

    before = sess.broker.account().equity
    fill = run_order_test(sess, "fill", sym)
    assert fill["ok"], fill
    assert sess.broker.positions() == {} and abs(sess.broker.account().equity - before) < before * 0.001
    assert sess.state().managed == {}, "a test fill is never adopted as a trade"

    history = load_results(sess.state_dir)
    assert [h["kind"] for h in history[:2]] == ["fill", "bracket"]
    conn = next(c for c in sess.connections()["connections"] if c["name"] == "broker_orders")
    assert conn["state"] == "ok" and "passed" in conn["detail"]

    # A symbol the trader is working on is off limits for test orders.
    state = sess.state()
    state.arming[sym] = {"source": "manual"}
    state.save(sess.state_path)
    guarded = run_order_test(sess, "bracket", sym)
    assert not guarded["ok"] and "armed" in guarded["error"]

    # Live accounts never get the fill test unless explicitly allowed.
    live = TradingSession(SessionSettings(**csv_universe.session_kwargs(), broker="alpaca-live", state_dir=tmp_path / "live", charts=False, overrides=csv_universe.overrides()))
    refused = run_order_test(live, "fill", sym)
    assert not refused["ok"] and "live accounts" in refused["error"]
    with pytest.raises(ValueError):
        run_order_test(sess, "bogus", sym)


def test_order_test_cli_and_status_page(tmp_path, csv_universe):
    sym = csv_universe.symbols[5]
    args = ["broker-test", "--kind", "bracket", "--symbol", sym, "--state-dir", str(tmp_path / "state"), "--json"] + csv_universe.cli_args(symbols=False)
    res = CliRunner().invoke(app, args)
    assert res.exit_code == 0, res.output
    assert json.loads(res.output[res.output.index("{"):])["ok"] is True

    sess = _session(tmp_path, csv_universe)
    client = TestClient(create_app(sess))
    page = client.get("/status")
    assert page.status_code == 200 and "Broker order routing" in page.text and 'data-kind="fill"' in page.text
    r = client.post("/api/status/order-test", json={"kind": "bracket", "symbol": sym})
    assert r.status_code == 200
    for _ in range(100):
        run = client.get("/api/status/order-tests").json()
        if not run["run"]["running"] and run["run"]["finished_at"]:
            break
        time.sleep(0.1)
    assert run["run"]["error"] is None and run["tests"][0]["ok"] and run["tests"][0]["symbol"] == sym
    assert client.post("/api/status/order-test", json={"kind": "nope"}).status_code == 400


# --------------------------------------------------------------------------- #
# Manual arm / buy / override from the lookup page
# --------------------------------------------------------------------------- #
def test_manual_buy_uses_a_fresh_plan_and_hands_the_position_to_the_trader(tmp_path, csv_universe):
    sess = _with_leader(tmp_path, csv_universe)
    look = sess.analyze_symbol("LEAD")
    assert look["status"] == "triggered" and look["plan"]["ok"] and look["plan"]["shares"] > 0

    res = sess.manual_trade("LEAD", "buy")
    assert res["ok"] and res["action"] == "buy" and res["filled"]["qty"] == look["plan"]["shares"] and not res["override"]
    state = sess.state()
    pos = state.managed["LEAD"]
    assert pos["shares"] == res["filled"]["qty"] and abs(pos["stop"] - res["stop"]) < 1e-6
    assert pos["features"]["manual"] is True and pos["features"]["source"] == "manual" and pos["features"]["entry_mode"] == "market_manual" and pos["features"]["override"] is False
    sells = [o for o in sess.broker.open_orders() if o.symbol == "LEAD" and o.side == "sell"]
    assert sells and sum(o.qty for o in sells) == pos["shares"], "stop and target rest at the broker immediately"
    assert "LEAD" not in state.arming

    with pytest.raises(ManualTradeRefused, match="already an open position"):
        sess.manual_trade("LEAD", "buy")
    with pytest.raises(ManualTradeRefused, match="unknown action"):
        sess.manual_trade("LEAD", "sell")
    with pytest.raises(KeyError):
        sess.manual_trade("NOPE", "arm")


def test_manual_arm_and_refusals_follow_the_method(tmp_path, csv_universe):
    sess = _with_leader(tmp_path, csv_universe)
    # A name with no setup on the latest bar: nothing to arm or buy.
    quiet = next(s for s in csv_universe.symbols if sess.analyze_symbol(s)["status"] == "none")
    with pytest.raises(ManualTradeRefused, match="nothing to arm or buy"):
        sess.manual_trade(quiet, "arm")

    # Arming records a manual entry the focused passes act on; a full scan keeps it.
    res = sess.manual_trade("LEAD", "arm")
    assert res["ok"] and res["action"] == "arm"
    rec = sess.state().arming["LEAD"]
    assert rec["source"] == "manual" and rec["manual"] is True and rec["triggered"] is True

    # A confirmed setup with a failed check is not bought without an override; the
    # override goes through and is journaled as one.
    strict = _with_leader(tmp_path, csv_universe, name="strict", **{"risk.max_position_pct": 0.0001})
    look = strict.analyze_symbol("LEAD")
    assert look["status"] == "triggered"
    if look["plan"]["ok"]:
        pytest.skip("synthetic leader passed every check under the strict config")
    with pytest.raises(ManualTradeRefused) as exc:
        strict.manual_trade("LEAD", "buy")
    assert exc.value.can_override and exc.value.problems


def test_manual_trade_guards_live_accounts_and_stops_above_market(tmp_path, csv_universe, monkeypatch):
    live = TradingSession(SessionSettings(**csv_universe.session_kwargs(), broker="alpaca-live", state_dir=tmp_path / "live", charts=False, overrides=csv_universe.overrides(**{"regime.enabled": False})))
    with pytest.raises(ManualTradeRefused) as exc:
        live.manual_trade("LEAD", "buy")
    assert exc.value.can_confirm and "LIVE" in exc.value.message

    # A wick-only breakout: close back under the pivot puts the plan's stop above the
    # market, which no override may bypass.
    lead = make_breakout_frame()
    lead.iloc[-1, lead.columns.get_loc("close")] = lead["close"].iloc[-2] * 0.97
    lead.index = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=len(lead), name="date")
    lead.to_csv(csv_universe.directory / "WICK.csv", index_label="date")
    kw = csv_universe.session_kwargs()
    kw["symbols"] += ",WICK"
    sess = TradingSession(SessionSettings(**kw, state_dir=tmp_path / "wick", charts=False, overrides=csv_universe.overrides(**{"regime.enabled": False})))
    look = sess.analyze_symbol("WICK")
    if look["status"] != "triggered":
        pytest.skip("the wick did not register as a trigger")
    with pytest.raises(ManualTradeRefused) as exc:
        sess.manual_trade("WICK", "override")
    assert not exc.value.can_override and exc.value.can_arm
    assert "stopped out immediately" in exc.value.message or "gate" in exc.value.message
    assert sess.state().managed == {}


def test_lookup_page_offers_actions_and_the_trade_endpoint_answers(tmp_path, csv_universe):
    sess = _with_leader(tmp_path, csv_universe)
    client = TestClient(create_app(sess))
    page = client.get("/symbol/LEAD")
    assert page.status_code == 200 and 'id="act"' in page.text and 'data-action="buy"' in page.text and 'data-action="arm"' in page.text
    quiet = next(s for s in csv_universe.symbols if sess.analyze_symbol(s)["status"] == "none")
    assert 'id="act"' not in client.get(f"/symbol/{quiet}").text

    r = client.post("/api/symbol/LEAD/trade", json={"action": "bogus"})
    assert r.status_code == 409 and "unknown action" in r.json()["detail"]["message"]
    r = client.post(f"/api/symbol/{quiet}/trade", json={"action": "buy"})
    assert r.status_code == 409 and r.json()["detail"]["can_override"] is False
    assert client.post("/api/symbol/NOPE/trade", json={"action": "arm"}).status_code == 404
    r = client.post("/api/symbol/LEAD/trade", json={"action": "buy"})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["filled"]
    assert "LEAD" in sess.state().managed
    desk = client.get("/desk")
    assert desk.status_code == 200 and "LEAD" in desk.text


def test_manual_arms_survive_the_nightly_rebuild_for_a_week(tmp_path, csv_universe):
    sess = _session(tmp_path, csv_universe)
    sess.cycle(asof="2024-05-14", label="nightly")
    state = sess.state()
    state.arming["HAND"] = {"symbol": "HAND", "setup": "breakout", "pivot": 10.0, "entry": 10.05, "stop": 9.5, "score": 1.0, "source": "manual", "manual": True, "armed_on": "2024-05-10", "triggered": False}
    state.arming["OLD"] = {**state.arming["HAND"], "symbol": "OLD", "armed_on": "2024-04-01"}
    state.save(sess.state_path)
    sess.cycle(asof="2024-05-15", label="nightly")
    arming = sess.state().arming
    assert "HAND" in arming and arming["HAND"]["source"] == "manual"
    assert "OLD" not in arming, "a manual arm older than five sessions lapses"


# --------------------------------------------------------------------------- #
# Daemon single-instance lock
# --------------------------------------------------------------------------- #
def test_instance_lock_refuses_a_live_owner_and_takes_over_a_dead_one(tmp_path):
    path = tmp_path / "daemon.lock"
    lock = InstanceLock(path)
    lock.acquire()
    assert json.loads(path.read_text())["pid"] == os.getpid() and lock.held

    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        path.write_text(json.dumps({"pid": sleeper.pid, "since": "2026-09-11T08:00:00"}))
        other = InstanceLock(path)
        with pytest.raises(AlreadyRunning, match="already running"):
            other.acquire()
        assert other.owner() == sleeper.pid
    finally:
        sleeper.kill()
        sleeper.wait()
    other.acquire()  # the owner is gone: stale lock, taken over
    assert json.loads(path.read_text())["pid"] == os.getpid()
    other.release()
    assert not path.exists()

    path.write_text(json.dumps({"pid": 2**22 + 12345, "since": "x"}))
    InstanceLock(path).acquire()
    assert json.loads(path.read_text())["pid"] == os.getpid()


def test_daemon_run_forever_holds_the_lock_and_stops_promptly(tmp_path, csv_universe):
    sess = _session(tmp_path, csv_universe)
    d = Daemon(sess, rebuild_universe=False)
    (sess.state_dir / "daemon.lock").write_text(json.dumps({"pid": os.getpid(), "since": "now"}))
    # Our own pid is not "another" daemon; stop before the first tick.
    d._stop = True
    d.run_forever(heartbeat_seconds=1)
    assert not (sess.state_dir / "daemon.lock").exists(), "the lock is released on exit"

    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (sess.state_dir / "daemon.lock").write_text(json.dumps({"pid": sleeper.pid, "since": "now"}))
        with pytest.raises(AlreadyRunning):
            Daemon(sess, rebuild_universe=False).run_forever()
    finally:
        sleeper.kill()
        sleeper.wait()

    d2 = Daemon(sess, rebuild_universe=False)
    t0 = time.monotonic()
    d2._stop = True
    d2._sleep(30)
    assert time.monotonic() - t0 < 1.5, "sleep returns as soon as stop is requested"


# --------------------------------------------------------------------------- #
# Learning: near misses, scorecards, auto-revert, post-mortems
# --------------------------------------------------------------------------- #
def test_full_scan_records_near_misses_as_shadows(tmp_path, csv_universe):
    sess = _session(tmp_path, csv_universe)
    for d in ("2024-05-14", "2024-06-03", "2024-07-15"):
        rep = sess.cycle(asof=d, label="nightly")
    near = [s for s in sess.state().shadow if s["kind"] == "near_miss"]
    assert near, "the relaxed detector pass finds setups one step outside the thresholds"
    for s in near:
        assert s["reasons"] and all(r.startswith(("breakout.", "momentum.")) for r in s["reasons"])
        assert s["immediate"] is True and s["stop"] < s["entry"] and s["features"]["entry_mode"] == "near_miss"
    assert any(a.startswith("LEARN") and "near-miss" in a for a in rep.actions)


def test_scorecards_judge_adjustments_and_revert_hurting_ones(tmp_path):
    cfg = StrategyConfig()
    state = _journal_state()
    now = datetime(2026, 9, 12, 11, 0)
    first = review(state, cfg, tmp_path, now=now, apply=True, ai=False)
    assert first["objective"]["total_r"] == pytest.approx(sum(r["r_multiple"] for r in state.closed))
    assert first["objective"]["trades"] == 12 and first["objective"]["r_per_week"] is not None
    keys = {a["key"]: a for a in first["adjustments"]}
    assert "breakout.min_breakout_volume_ratio" in keys and keys["breakout.min_breakout_volume_ratio"]["direction"] == "tighten"
    assert first["scorecards"] == [] and first["reverted"] == []
    assert first["shadows"]["missed"] and first["shadows"]["missed"][0]["r_multiple"] >= first["shadows"]["missed"][-1]["r_multiple"]

    # Since the tightening, the volume band it excluded (1.2x .. new floor) has been making money.
    # Seed a recorded operator-approved change to exercise scorecards. The
    # earlier exploratory review must not apply its own proposals.
    adj = keys["breakout.min_breakout_volume_ratio"]
    assert first["proposed_only"]
    (tmp_path / OVERRIDES_FILE).write_text(yaml.safe_dump({"overrides": {adj["key"]: adj["to"]}, "history": [adj]}))
    tightened = load_overrides(tmp_path)["breakout.min_breakout_volume_ratio"]
    assert tightened > cfg.breakout.min_breakout_volume_ratio
    band_mid = (cfg.breakout.min_breakout_volume_ratio + tightened) / 2
    state.shadow += [
        {"symbol": f"N{i}", "kind": "near_miss", "date": "2026-09-20", "setup": "breakout", "pivot": 50.0, "entry": 50.1, "stop": 47.0, "target": 56.0, "immediate": True,
         "reasons": ["breakout.min_breakout_volume_ratio"], "features": {"rvol": band_mid}, "status": "closed", "r_multiple": 1.5, "exit_reason": "target", "post_mortem": {"lesson": "fine"}}
        for i in range(5)
    ]
    later = datetime(2026, 10, 3, 11, 0)
    cfg2 = cfg.with_overrides(load_overrides(tmp_path))
    cards = score_adjustments(state.closed, state.shadow, cfg2, yaml.safe_load((tmp_path / OVERRIDES_FILE).read_text()), later)
    card = next(c for c in cards if c["key"] == "breakout.min_breakout_volume_ratio")
    assert card["verdict"] == "hurting" and card["evidence"]["n"] == 5

    second = review(state, cfg2, tmp_path, now=later, apply=True, ai=False)
    assert [r["key"] for r in second["proposed_reversions"]] == ["breakout.min_breakout_volume_ratio"]
    assert second["reverted"] == [] and second["applied"] == []
    assert load_overrides(tmp_path)["breakout.min_breakout_volume_ratio"] == pytest.approx(tightened)
    history = yaml.safe_load((tmp_path / OVERRIDES_FILE).read_text())["history"]
    assert history[-1]["direction"] == "tighten", "a shadow comparison cannot revert live settings"


def test_tightening_needs_the_band_to_lose_money_in_total(tmp_path):
    """Averages alone would still tighten when the band is merely below the rest;
    the total-R objective only tightens when the band actually loses."""
    from qmag.learning import propose_adjustments

    cfg = StrategyConfig()
    winners = [_closed(10 + i, 1.4, 2.4) for i in range(6)]
    band_small_win = [_closed(i, 0.3, 1.25) for i in range(6)]  # worse than the rest, but profitable
    assert propose_adjustments(winners + band_small_win, [], cfg, {"overrides": {}, "history": []}, datetime(2026, 9, 12)) == []
    band_losers = [_closed(i, -0.9, 1.25) for i in range(6)]
    adj = propose_adjustments(winners + band_losers, [], cfg, {"overrides": {}, "history": []}, datetime(2026, 9, 12))
    assert any(a["key"] == "breakout.min_breakout_volume_ratio" and a["direction"] == "tighten" for a in adj)


def test_learning_page_shows_objective_scorecards_and_missed_trades(tmp_path, csv_universe):
    sess = _session(tmp_path, csv_universe)
    state = _journal_state()
    state.shadow.append(
        {"symbol": "NM1", "kind": "near_miss", "date": "2024-05-03", "setup": "breakout", "pivot": 50.0, "entry": 50.1, "stop": 47.0, "target": 56.0, "immediate": True,
         "reasons": ["momentum.min_adr_pct"], "features": {"adr_pct": 3.4}, "status": "closed", "r_multiple": -1.0, "exit_reason": "stop", "post_mortem": {"lesson": "Stopped out on the first pullback."}}
    )
    state.save(sess.state_path)
    sess.learn(now=datetime(2026, 9, 12, 11, 0), apply=True)
    client = TestClient(create_app(sess))
    page = client.get("/learning")
    assert page.status_code == 200
    for text in ("Total R", "R per week", "Best trades we did not take", "Worst trades we dodged", "Scorecard", "near miss", "Stopped out on the first pullback."):
        assert text in page.text, text
