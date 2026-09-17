"""Portfolio risk gates, the kill switch, push alerts, the Unusual Whales daily
budget and the time-stop exit."""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import qmag.uw as uw
from qmag.alerts import Alerter, channels_from_env
from qmag.broker import PaperBroker
from qmag.broker_test import run_order_test
from qmag.cli import app
from qmag.config import StrategyConfig
from qmag.dashboard import create_app
from qmag.halt import halt_reason, halt_status, set_halt
from qmag.plan import TradePlan
from qmag.session import ManualTradeRefused, SessionSettings, TradingSession
from qmag.trader import ManagedPosition, TraderState, portfolio_gate, portfolio_heat, run_cycle


def _session(tmp_path, csv_universe, name="state", **extra) -> TradingSession:
    return TradingSession(
        SessionSettings(**csv_universe.session_kwargs(), state_dir=tmp_path / name, charts=False, overrides=csv_universe.overrides(**{"regime.enabled": False, "entry.mode": "resting", **extra}))
    )


def _run_until_positions(sess: TradingSession, start="2024-05-14", periods=8):
    last = None
    for d in pd.bdate_range(start, periods=periods):
        last = d
        sess.cycle(asof=str(d.date()), label="nightly")
        if sess.state().managed:
            break
    assert sess.state().managed, "the synthetic universe should have produced at least one fill"
    return last


@pytest.fixture(autouse=True)
def _no_alert_channels(monkeypatch):
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "QMAG_ALERT_WEBHOOK_URL"):
        monkeypatch.delenv(k, raising=False)


# --------------------------------------------------------------------------- #
# Portfolio risk gates
# --------------------------------------------------------------------------- #
def _plan(symbol="AAA", theme=None, risk_dollars=500.0) -> TradePlan:
    shares = int(risk_dollars / 5)
    return TradePlan(
        symbol=symbol, setup="breakout", date="2024-05-14", entry=100.0, stop=95.0, shares=shares, risk_per_share=5.0, risk_dollars=risk_dollars,
        risk_pct=risk_dollars / 100_000, position_value=shares * 100.0, position_pct=shares * 100.0 / 100_000, partial_qty=shares // 3, partial_target=110.0,
        partial_after_days=3, trail_ma=10, max_hold_days=60, pivot=99.5, theme=theme, theme_pct=None, score=1.0,
    )


def test_portfolio_gate_reasons():
    cfg = StrategyConfig().with_overrides({"risk.max_portfolio_heat_pct": 0.02, "risk.daily_loss_limit_pct": 0.03, "risk.max_positions_per_theme": 1})
    state = TraderState()
    state.managed["BBB"] = {"symbol": "BBB", "theme": "semis"}
    plan = _plan(theme="semis", risk_dollars=1500.0)
    reasons = portfolio_gate(plan, state, cfg, heat_pct=0.01, day_pnl_pct=-0.035, equity=100_000.0)
    text = " | ".join(reasons)
    assert "portfolio heat" in text and "exceed" in text
    assert "daily loss limit" in text
    assert "theme cap" in text and "semis" in text and "BBB" in text
    # Everything inside the limits -> no reasons.
    assert portfolio_gate(_plan(theme="other", risk_dollars=500.0), state, cfg, heat_pct=0.005, day_pnl_pct=-0.01, equity=100_000.0) == []
    # 0 switches each gate off.
    off = StrategyConfig().with_overrides({"risk.max_portfolio_heat_pct": 0, "risk.daily_loss_limit_pct": 0, "risk.max_positions_per_theme": 0})
    assert portfolio_gate(plan, state, off, heat_pct=0.5, day_pnl_pct=-0.5, equity=100_000.0) == []


def test_portfolio_heat_counts_open_risk_and_resting_entries():
    state = TraderState()
    state.managed["AAA"] = {"symbol": "AAA", "setup": "breakout", "entry_date": "2024-01-02", "entry_price": 100.0, "shares": 100, "initial_stop": 95.0, "stop": 96.0, "remaining": 100}
    state.pending["BBB"] = {"symbol": "BBB", "trigger": 50.0, "stop": 48.0, "qty": 200}
    df = pd.DataFrame({"close": [101.0]}, index=pd.to_datetime(["2024-01-05"]))
    heat = portfolio_heat(state, {"AAA": df}, equity=100_000.0)
    # AAA: (101 - 96) * 100 = 500 ; BBB: (50 - 48) * 200 = 400 -> 0.9 % of equity
    assert heat == pytest.approx(0.009)


def test_heat_cap_blocks_entries_and_records_shadows(tmp_path, csv_universe):
    sess = _session(tmp_path, csv_universe, **{"risk.max_portfolio_heat_pct": 0.006})
    rep = sess.cycle(asof="2024-05-14", label="nightly")
    assert rep.risk_blocked, "a 0.6 % heat cap must refuse most of the plans"
    assert all("portfolio heat" in b["reasons"][0] for b in rep.risk_blocked)
    assert any(a.startswith("RISK ") and "not opened" in a for a in rep.actions)
    state = sess.state()
    assert sum(1 for s in state.shadow if s["kind"] == "no_slot") >= len(rep.risk_blocked)
    saved = sess.last_report()
    assert "heat_pct" in saved and saved["risk_blocked"]
    assert state.day_equity and state.day_equity["date"] == "2024-05-14"


def test_theme_cap_message_names_the_holding(tmp_path, csv_universe):
    sess = _session(tmp_path, csv_universe, **{"risk.max_positions_per_theme": 1})
    rep = sess.cycle(asof="2024-05-14", label="nightly")
    themed = [b for b in rep.risk_blocked if "theme cap" in b["reasons"][0]]
    assert themed, "the synthetic themes have several breakouts each; the second one must be refused"


def test_daily_loss_limit_stops_new_entries(tmp_path, csv_universe):
    sess = _session(tmp_path, csv_universe)
    state = sess.state()
    state.day_equity = {"date": "2024-05-14", "equity": 200_000.0}  # today opened far above the current equity
    state.save(sess.state_path)
    rep = sess.cycle(asof="2024-05-14", label="nightly")
    assert rep.day_pnl_pct is not None and rep.day_pnl_pct < -0.03
    assert any(a.startswith("RISK daily loss limit hit") for a in rep.actions)
    assert not rep.plans and sess.state().pending == {}
    assert all("daily loss limit" in b["reasons"][0] for b in rep.risk_blocked)


# --------------------------------------------------------------------------- #
# Kill switch
# --------------------------------------------------------------------------- #
def test_halt_file_roundtrip(tmp_path):
    assert halt_status(tmp_path) is None and halt_reason(tmp_path) is None
    st = set_halt(tmp_path, True, "  broker acting up ", by="dashboard")
    assert st["reason"] == "broker acting up" and st["by"] == "dashboard" and not st["flattened"]
    assert halt_reason(tmp_path).startswith("HALTED by dashboard ") and halt_reason(tmp_path).endswith(": broker acting up")
    assert set_halt(tmp_path, False) is None and not (tmp_path / "halt.json").exists()
    (tmp_path / "halt.json").write_text("{not json")
    assert "unreadable" in halt_status(tmp_path)["reason"], "a corrupt halt file still halts"


def test_halt_stops_entries_keeps_positions_and_cancels_resting_orders(tmp_path, csv_universe):
    sess = _session(tmp_path, csv_universe)
    last = _run_until_positions(sess)
    before = sess.state()
    n_managed = len(before.managed)
    res = sess.halt(True, reason="test", by="cli")
    assert res["on"] and res["flattened"] == [] and sess.halted().endswith(": test")
    nxt = str((last + pd.offsets.BDay()).date())
    rep = sess.cycle(asof=nxt, label="nightly")
    assert rep.halted and rep.plans == []
    assert any(a.startswith("HALT ") for a in rep.actions)
    after = sess.state()
    assert after.pending == {}, "resting buy-stops are cancelled while halted"
    assert not any(o.kind == "buy_stop_bracket" for o in sess.broker.open_orders())
    assert len(after.managed) <= n_managed  # exits still run; nothing new opened
    assert all(any(o.symbol == s and o.kind in ("stop", "oco") for o in sess.broker.open_orders()) for s in after.managed), "open positions keep their exit orders"
    # Manual buys are refused (arming still allowed), order tests are blocked.
    with pytest.raises(ManualTradeRefused) as exc:
        sess.manual_trade(next(iter(csv_universe.symbols)), "buy")
    assert exc.value.can_arm and "HALTED" in str(exc.value)
    ot = run_order_test(sess, kind="bracket", symbol="ZZZT")
    assert not ot["ok"] and "kill switch" in ot["error"]
    # Resume: the file is gone and the next cycle plans again.
    sess.halt(False)
    assert sess.halted() is None
    rep2 = sess.cycle(asof=nxt, label="nightly")
    assert rep2.halted is None


def test_halt_and_flatten_sells_everything_and_journals_it(tmp_path, csv_universe):
    sess = _session(tmp_path, csv_universe)
    _run_until_positions(sess)
    held = set(sess.state().managed)
    res = sess.halt(True, reason="flatten test", by="dashboard", flatten=True)
    assert {f["symbol"] for f in res["flattened"]} == held
    state = sess.state()
    assert state.managed == {} and state.pending == {}
    assert sess.broker.positions() == {}
    closed = [c for c in state.closed if c["exit_reason"] == "halt"]
    assert {c["symbol"] for c in closed} == held and all("r_multiple" in c for c in closed)
    assert halt_status(sess.state_dir)["flattened"] is True


def test_halt_dashboard_and_api(tmp_path, csv_universe):
    sess = _session(tmp_path, csv_universe)
    client = TestClient(create_app(sess))
    assert client.get("/api/halt").json() == {"halted": None}
    assert "TRADING HALTED" not in client.get("/desk").text
    r = client.post("/api/halt", json={"on": True, "reason": "api test"})
    assert r.status_code == 200 and r.json()["status"]["by"] == "dashboard"
    page = client.get("/desk")
    assert "TRADING HALTED" in page.text and "api test" in page.text
    status = client.get("/status").text
    assert "Resume trading" in status and "qmag resume" in status
    # Flattening while halted keeps the original reason.
    r = client.post("/api/halt", json={"on": True, "flatten": True})
    assert r.status_code == 200 and r.json()["status"]["reason"] == "api test" and r.json()["status"]["flattened"]
    r = client.post("/api/halt", json={"on": False})
    assert r.status_code == 200 and client.get("/api/halt").json() == {"halted": None}
    assert "TRADING HALTED" not in client.get("/desk").text


def test_halt_flatten_on_live_needs_confirmation(tmp_path, csv_universe):
    sess = TradingSession(SessionSettings(**csv_universe.session_kwargs(), broker="alpaca-live", state_dir=tmp_path / "live", charts=False, overrides=csv_universe.overrides()))
    client = TestClient(create_app(sess))
    r = client.post("/api/halt", json={"on": True, "flatten": True})
    assert r.status_code == 403 and "LIVE" in r.json()["detail"]
    # A plain halt (no flatten) never touches the broker, so it works without credentials.
    r = client.post("/api/halt", json={"on": True, "reason": "live halt"})
    assert r.status_code == 200 and sess.halted()
    sess.halt(False)


def test_halt_and_resume_cli(tmp_path, csv_universe):
    runner = CliRunner()
    args = csv_universe.cli_args(symbols=False) + ["--state-dir", str(tmp_path / "cli")]
    res = runner.invoke(app, ["halt", "--reason", "cli test", *args])
    assert res.exit_code == 0, res.output
    assert "TRADING HALTED" in res.output and json.loads((tmp_path / "cli" / "halt.json").read_text())["reason"] == "cli test"
    res = runner.invoke(app, ["resume", *args])
    assert res.exit_code == 0 and "TRADING RESUMED" in res.output and not (tmp_path / "cli" / "halt.json").exists()
    res = runner.invoke(app, ["resume", *args])
    assert res.exit_code == 0 and "not halted" in res.output


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #
def test_alerter_without_channels_sends_nothing():
    a = Alerter(None, env={})
    assert not a.configured and a.send("x") is None
    assert a.test()["ok"] is False and "TELEGRAM_BOT_TOKEN" in a.test()["error"]
    assert channels_from_env({"TELEGRAM_BOT_TOKEN": "t"}) == [], "a token without a chat id is not a channel"
    assert channels_from_env({"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "QMAG_ALERT_WEBHOOK_URL": "https://x"}) == ["telegram", "webhook"]


def test_alerter_dedupes_by_key_and_records_health():
    from qmag.health import ConnectionRegistry

    sent: list[tuple[str, str]] = []
    reg = ConnectionRegistry(None)
    a = Alerter(reg, env={"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}, transport=lambda ch, p: sent.append((ch, p["title"])))
    assert a.send("a", key="k", wait=True) is True
    assert a.send("a again", key="k", wait=True) is True and len(sent) == 1, "same key within 12 h is sent once"
    a.send("b", wait=True)
    assert [t for _, t in sent] == ["a", "b"]
    assert reg.records()["alerts"]["ok"] is True

    def boom(ch, p):
        raise RuntimeError("HTTP 401: Unauthorized")

    bad = Alerter(reg, env={"QMAG_ALERT_WEBHOOK_URL": "https://hooks.example/x"}, transport=boom)
    assert bad.send("c", wait=True) is False
    rec = reg.records()["alerts"]
    assert rec["ok"] is False and "webhook: RuntimeError: HTTP 401" in rec["last_error"]
    assert bad.test()["ok"] is False


def test_cycle_and_halt_push_alerts(tmp_path, csv_universe):
    sess = _session(tmp_path, csv_universe)
    got: list[dict] = []
    sess.alerts = Alerter(sess.health, env={"QMAG_ALERT_WEBHOOK_URL": "https://hooks.example/x"}, transport=lambda ch, p: got.append(p))
    _run_until_positions(sess)
    sess.alerts.join()
    titles = [g["title"] for g in got]
    assert any("trade event" in t for t in titles), titles
    body = next(g["body"] for g in got if "trade event" in g["title"])
    assert any(line.startswith(("FILL buy", "BUY ")) for line in body.splitlines())
    sess.halt(True, reason="alert test", by="cli")
    sess.alerts.join()
    assert got[-1]["title"] == "TRADING HALTED" and got[-1]["level"] == "error" and "alert test" in got[-1]["body"]
    sess.halt(False)
    sess.alerts.join()
    assert got[-1]["title"] == "Trading resumed"
    assert not any("hooks.example" in json.dumps(g) for g in got), "payloads never carry the endpoint or a token"


def test_alert_test_endpoint_reports_missing_channels(tmp_path, csv_universe):
    sess = _session(tmp_path, csv_universe)
    client = TestClient(create_app(sess))
    r = client.post("/api/alerts/test")
    assert r.status_code == 502 and r.json()["channels"] == []
    assert "Send a test alert" in client.get("/status").text
    conn = sess.connections()
    alerts = next(c for c in conn["connections"] if c["name"] == "alerts")
    assert alerts["state"] == "not_configured" and "TELEGRAM_BOT_TOKEN" in alerts["note"]


def test_alert_cli_pushes_one_message_and_settings_shows_the_tunnel_address(tmp_path, csv_universe, monkeypatch):
    """`qmag alert` is what deploy/tunnel-watch.sh calls when the quick tunnel
    hands out a new address; the address it records is shown on the settings page."""
    from qmag.cli import app

    state = tmp_path / "state"
    runner = CliRunner()
    monkeypatch.delenv("QMAG_ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    res = runner.invoke(app, ["alert", "--state-dir", str(state), "--data", "csv", "--csv-dir", str(csv_universe.directory), "Dashboard address changed", "https://a-b-c.trycloudflare.com"])
    assert res.exit_code == 2 and "NO CHANNEL" in res.output

    sent: list[dict] = []

    def fake_post(url, json=None, timeout=None):
        sent.append({"url": url, "json": json})
        return type("R", (), {"status_code": 200, "text": "", "headers": {}})()

    monkeypatch.setenv("QMAG_ALERT_WEBHOOK_URL", "https://hooks.example/x")
    monkeypatch.setattr("qmag.alerts.requests.post", fake_post)
    res = runner.invoke(app, ["alert", "--state-dir", str(state), "--data", "csv", "--csv-dir", str(csv_universe.directory), "--level", "warn", "Dashboard address changed", "https://a-b-c.trycloudflare.com"])
    assert res.exit_code == 0 and "DELIVERED" in res.output, res.output
    assert sent[0]["json"]["title"] == "Dashboard address changed" and sent[0]["json"]["level"] == "warn" and "a-b-c.trycloudflare.com" in sent[0]["json"]["text"]

    sess = _session(tmp_path, csv_universe)
    client = TestClient(create_app(sess))
    assert "Public address" not in client.get("/settings").text
    (state / "tunnel_url.txt").write_text("https://a-b-c.trycloudflare.com\n")
    page = client.get("/settings").text
    assert "Public address" in page and 'href="https://a-b-c.trycloudflare.com"' in page
    (state / "tunnel_url.txt").write_text("garbage\n")
    assert "Public address" not in client.get("/settings").text


def test_settings_page_lists_alert_fields_as_secrets():
    from qmag.settings import ENV_BY_NAME, SECRET_NAMES

    assert "TELEGRAM_BOT_TOKEN" in SECRET_NAMES and "QMAG_ALERT_WEBHOOK_URL" in SECRET_NAMES
    assert "TELEGRAM_CHAT_ID" not in SECRET_NAMES and ENV_BY_NAME["TELEGRAM_CHAT_ID"].group == "alerts"


# --------------------------------------------------------------------------- #
# Unusual Whales daily budget
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body or {}, headers or {}

    def json(self):
        return self._body


@pytest.fixture
def uw_env(tmp_path, monkeypatch):
    monkeypatch.setenv("UNUSUAL_WHALES_BUDGET_FILE", str(tmp_path / "budget.json"))
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "not-a-real-key")
    monkeypatch.delenv("UNUSUAL_WHALES_DAILY_CAP", raising=False)
    monkeypatch.setattr(uw, "throttle", lambda rpm=None: None)
    monkeypatch.setattr(uw.time, "sleep", lambda s: None)
    return tmp_path


def test_uw_daily_cap_blocks_locally(uw_env, monkeypatch):
    monkeypatch.setenv("UNUSUAL_WHALES_DAILY_CAP", "2")
    calls: list[str] = []
    monkeypatch.setattr(uw.requests, "get", lambda url, **kw: (calls.append(url), _Resp(200, {"data": [1]}))[1])
    c = uw.UWClient(cache_path=None)
    assert c.get("/a").ok and c.get("/b").ok
    r = c.get("/c")
    assert not r.ok and "daily call budget used up (2/2" in r.error and len(calls) == 2 and c.blocked == 1
    u = uw.budget().usage()
    assert u["calls"] == 2 and u["cap"] == 2 and u["remaining"] == 0 and u["blocked"]
    # Another client in the same process (or another process reading the file) sees the same count.
    assert json.loads((uw_env / "budget.json").read_text())["calls"] == 2
    assert not uw.UWClient(cache_path=None).get("/d").ok


def test_uw_daily_limit_answer_pauses_until_utc_midnight(uw_env, monkeypatch):
    monkeypatch.setattr(uw.requests, "get", lambda url, **kw: _Resp(429, {"message": "You have exceeded your daily request limit"}, {"Retry-After": "1"}))
    c = uw.UWClient(cache_path=None)
    r = c.get("/flow")
    assert not r.ok and "paused until 00:00 UTC" in r.error and "daily request limit" in r.error
    calls: list[str] = []
    monkeypatch.setattr(uw.requests, "get", lambda url, **kw: (calls.append(url), _Resp(200, {"data": 1}))[1])
    r2 = c.get("/other")
    assert not r2.ok and "paused" in r2.error and calls == [], "no network call while paused"
    u = uw.budget().usage()
    assert u["paused"] and u["paused_reason"].startswith("HTTP 429")
    from qmag.health import _uw_budget_note

    assert "paused until 00:00 UTC" in _uw_budget_note()


def test_uw_plain_429_is_retried_not_paused(uw_env, monkeypatch):
    seq = [_Resp(429, {}, {"Retry-After": "0"}), _Resp(200, {"data": {"x": 1}})]
    monkeypatch.setattr(uw.requests, "get", lambda url, **kw: seq.pop(0))
    c = uw.UWClient(cache_path=None)
    r = c.get("/flow")
    assert r.ok and r.data == {"x": 1}
    u = uw.budget().usage()
    assert not u["paused"] and u["calls"] == 2 and u["cap"] is None
    from qmag.health import _uw_budget_note

    assert "2 calls today" in _uw_budget_note() and "no daily cap" in _uw_budget_note()


def test_uw_budget_resets_with_the_utc_day(uw_env):
    b = uw.DailyBudget(uw_env / "b.json")
    b.count(5)
    rec = json.loads((uw_env / "b.json").read_text())
    rec["date"] = "2000-01-01"
    (uw_env / "b.json").write_text(json.dumps(rec))
    assert b.usage()["calls"] == 0, "yesterday's count does not carry over"


# --------------------------------------------------------------------------- #
# Time stop
# --------------------------------------------------------------------------- #
def _flat_after_entry(entry: float, sessions_after: int, drift: float = -0.004) -> pd.DataFrame:
    """300 quiet bars, an entry bar, then ``sessions_after`` bars closing a little under the entry."""
    rng = np.random.default_rng(3)
    n = 300
    close = entry * np.cumprod(1 + rng.normal(0, 0.003, n))
    close = close / close[-1] * entry  # end the history at the entry price
    after = np.array([entry * (1 + drift) for _ in range(sessions_after)])
    c = np.r_[close, entry, after]
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) * 1.004
    l = np.minimum(o, c) * 0.996
    v = np.full(len(c), 1_500_000.0)
    idx = pd.bdate_range("2023-01-02", periods=len(c))
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": v}, index=idx)


def _held(tmp_path, df: pd.DataFrame, entry: float, sessions_after: int, **pos_kw):
    brk = PaperBroker(tmp_path / "ledger.json", starting_cash=100_000.0)
    brk.ledger.last_prices["TS"] = entry
    brk.market_buy("TS", 100)
    entry_date = str(df.index[-1 - sessions_after].date())
    state = TraderState()
    pos = ManagedPosition(symbol="TS", setup="breakout", entry_date=entry_date, entry_price=entry, shares=100, initial_stop=entry - 5.0, stop=entry - 5.0, pivot=entry - 0.5, **pos_kw)
    state.managed["TS"] = pos.__dict__.copy()
    return brk, state


def test_time_stop_cuts_dead_breakout(tmp_path):
    entry = 100.0
    df = _flat_after_entry(entry, sessions_after=5)
    brk, state = _held(tmp_path, df, entry, 5)
    cfg = StrategyConfig().with_overrides({"regime.enabled": False, "management.time_stop_days": 5, "entry.failed_breakout_exit": False})
    rep = run_cycle({"TS": df}, brk, cfg, state, asof=df.index[-1])
    assert "TS" not in state.managed and brk.positions() == {}
    rec = state.closed[-1]
    assert rec["exit_reason"] == "time_stop" and rec["symbol"] == "TS"
    assert any("(time_stop)" in a for a in rep.actions)
    assert "no follow-through" in rec["post_mortem"]["text"]


def test_time_stop_spares_trades_that_showed_follow_through(tmp_path):
    entry = 100.0
    cfg = StrategyConfig().with_overrides({"regime.enabled": False, "management.time_stop_days": 5, "entry.failed_breakout_exit": False})
    # Reached +1.2 R on an earlier session (recorded by that day's pass) -> spared even though it closed back under the entry.
    df = _flat_after_entry(entry, sessions_after=5)
    brk, state = _held(tmp_path, df, entry, 5, mfe_r=1.2)
    run_cycle({"TS": df}, brk, cfg, state, asof=df.index[-1])
    assert "TS" in state.managed and state.managed["TS"]["mfe_r"] >= 1.0
    # Too early: only 3 sessions in.
    df = _flat_after_entry(entry, sessions_after=3)
    brk, state = _held(tmp_path, df, entry, 3)
    run_cycle({"TS": df}, brk, cfg, state, asof=df.index[-1])
    assert "TS" in state.managed
    # Switched off.
    df = _flat_after_entry(entry, sessions_after=8)
    brk, state = _held(tmp_path, df, entry, 8)
    off = cfg.with_overrides({"management.time_stop_days": 0})
    run_cycle({"TS": df}, brk, off, state, asof=df.index[-1])
    assert "TS" in state.managed


def test_backtest_engine_knows_the_time_stop():
    from qmag.backtest import Position

    pos = Position(symbol="X", setup="breakout", entry_date=pd.Timestamp("2024-01-02"), entry_price=100.0, shares=10, stop=95.0, initial_risk=5.0)
    assert pos.mfe_r == 0.0
    from qmag.settings import validate_config

    with pytest.raises(Exception):
        validate_config(StrategyConfig().with_overrides({"management.time_stop_days": -1}))


# --------------------------------------------------------------------------- #
# Hover help
# --------------------------------------------------------------------------- #
def test_every_strategy_setting_has_help_text():
    from qmag.settings import FIELD_HELP, describe_config

    keys = [f["key"] for s in describe_config(StrategyConfig()) for f in s["fields"]]
    missing = [k for k in keys if not FIELD_HELP.get(k)]
    assert not missing, f"settings without a tooltip: {missing}"
    stale = [k for k in FIELD_HELP if k not in keys]
    assert not stale, f"help for parameters that no longer exist: {stale}"


def test_info_tooltips_render_on_every_page(tmp_path, csv_universe):
    sess = _session(tmp_path, csv_universe)
    sess.cycle(asof="2024-05-14", label="nightly")
    client = TestClient(create_app(sess))
    settings_page = client.get("/settings").text
    assert settings_page.count('class="info"') >= 200, "one ⓘ per strategy parameter plus the connection fields"
    assert 'data-tip="Fraction of equity lost if the initial stop is hit. 0.005 = 0.5 %."' in settings_page
    assert 'data-tip="From @BotFather.' in settings_page
    desk = client.get("/desk").text
    assert 'id="tip"' in desk and desk.count('class="info"') >= 10
    assert "risk.max_portfolio_heat_pct" in desk  # the Open heat tooltip names the setting
    status = client.get("/status").text
    assert "missing data is never substituted" in status
    learning = client.get("/learning").text
    assert "Guard rails: the review may move this knob" in learning
