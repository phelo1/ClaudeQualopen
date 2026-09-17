"""No invented data: missing inputs are flagged, never substituted."""

from datetime import date, datetime, timezone

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from qmag.backtest import prepare_data
from qmag.broker import PaperBroker
from qmag.cli import app
from qmag.config import StrategyConfig
from qmag.context.base import ContextReport
from qmag.dashboard import create_app
from qmag import data as qmag_data
from qmag.data import DATA_KINDS, CachedDailyProvider, make_provider, resolve_data_kind
from qmag.health import STATES, ConnectionRegistry, describe_connections, expected_last_session, price_freshness
from qmag.plan import build_plan
from qmag.regime import regime_snapshot
from qmag.session import SessionSettings, TradingSession
from qmag.setups import BreakoutDetector
from qmag.trader import TraderState, run_cycle
from tests.conftest import make_breakout_frame


def _trend(drift: float, n: int = 260, start: float = 100.0) -> pd.DataFrame:
    idx = pd.bdate_range("2024-01-02", periods=n)
    close = start * (1 + drift) ** pd.Series(range(n), index=idx)
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1e6}, index=idx)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def test_registry_records_and_merges_across_processes(tmp_path):
    path = tmp_path / "connections.json"
    a = ConnectionRegistry(path)
    a.record("broker", True, detail="paper", latency_ms=3)
    b = ConnectionRegistry(path)  # a second process
    b.record("price_data", False, error="ConnectionError: boom", save=False)
    b.record("universe", True, items=2860, save=False)
    b.save()
    merged = ConnectionRegistry(path).records()
    assert merged["broker"]["ok"] and merged["broker"]["last_ok"]
    assert not merged["price_data"]["ok"] and merged["price_data"]["last_error"].startswith("ConnectionError")
    assert merged["price_data"]["consecutive_failures"] == 1 and merged["universe"]["items"] == 2860
    b.record("price_data", True)
    assert ConnectionRegistry(path).records()["price_data"]["consecutive_failures"] == 0


def test_record_sources_aggregates_context_availability():
    reg = ConnectionRegistry(None)
    ok = ContextReport(symbol="A", asof="2026-09-11")
    ok.available.update({"stocktwits": True, "reddit": False})
    ok.errors["reddit"] = "no REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET"
    half = ContextReport(symbol="B", asof="2026-09-11")
    half.available.update({"stocktwits": False, "reddit": False})
    half.errors.update({"stocktwits": "HTTP 429", "reddit": "no REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET"})
    reg.record_sources([ok, half])
    st, rd = reg.get("stocktwits"), reg.get("reddit")
    assert st["ok"] and st["degraded"] and "HTTP 429" in st["detail"]
    assert not rd["ok"] and "REDDIT_CLIENT_ID" in rd["last_error"]


def test_describe_connections_states(tmp_path, monkeypatch):
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY", raising=False)
    cfg = StrategyConfig().with_overrides({"options_flow.enabled": True})
    settings = SessionSettings(data="yfinance", state_dir=tmp_path)
    reg = ConnectionRegistry(tmp_path / "connections.json")
    reg.record("broker", True)
    reg.record("price_data", False, error="timeout")
    conn = describe_connections(settings, cfg, tmp_path, reg)
    by = {c["name"]: c for c in conn["connections"]}
    assert by["broker"]["state"] == "ok"
    assert by["price_data"]["state"] == "error" and by["price_data"]["required"]
    assert by["reddit"]["state"] == "not_configured"
    assert by["unusual_whales"]["state"] == "not_configured" and "UNUSUAL_WHALES_API_KEY" in by["unusual_whales"]["note"]
    assert by["llm_reviewer"]["state"] == "off" and by["stocktwits"]["state"] == "unknown"
    assert conn["overall"] == "error"  # a required connection is down
    assert "simulated" not in conn and "simulated" not in STATES


# --------------------------------------------------------------------------- #
# Price freshness and stale cache
# --------------------------------------------------------------------------- #
def test_expected_last_session_respects_calendar_and_close():
    # Friday 2026-09-11 before the close -> Thursday; after 16:30 NY -> Friday; Saturday -> Friday.
    ny = pd.Timestamp("2026-09-11 12:00", tz="America/New_York").to_pydatetime()
    assert expected_last_session(ny) == date(2026, 9, 10)
    late = pd.Timestamp("2026-09-11 16:45", tz="America/New_York").to_pydatetime()
    assert expected_last_session(late) == date(2026, 9, 11)
    sat = pd.Timestamp("2026-09-12 10:00", tz="America/New_York").to_pydatetime()
    assert expected_last_session(sat) == date(2026, 9, 11)
    # Labor Day Monday 2026-09-07: the expected session is the prior Friday.
    mon = pd.Timestamp("2026-09-07 18:00", tz="America/New_York").to_pydatetime()
    assert expected_last_session(mon) == date(2026, 9, 4)


def test_price_freshness_flags_old_bars():
    df = _trend(0.001)
    fresh = price_freshness({"A": df}, asof=str(df.index[-1].date()))
    assert not fresh["stale"]
    stale = price_freshness({"A": df})  # 2024 data judged against today
    assert stale["stale"] and "stale" in stale["note"] and stale["latest"] == str(df.index[-1].date())
    assert price_freshness({})["stale"]


class _FlakyProvider(CachedDailyProvider):
    """Serves one symbol, fails for the other, then fails entirely on refresh."""

    calls: int = 0

    def _download(self, symbols, start, end):
        self.calls += 1
        if self.calls == 1:
            return {"GOOD": _trend(0.001, n=120)}
        self._note_error("simulated outage")
        return {}


def test_cached_provider_reports_missing_and_stale_instead_of_hiding_them(tmp_path):
    p = _FlakyProvider(cache_dir=tmp_path, max_age_hours=0.0)
    out = p.load(["GOOD", "BAD"])
    assert set(out) == {"GOOD"}
    assert p.stats["missing"] == ["BAD"] and p.stats["downloaded"] == 1
    # Cache is stale (max_age 0) so the refresh runs, fails, and the cached bars are served *marked stale*.
    out2 = p.load(["GOOD"])
    assert "GOOD" in out2 and p.stats["stale"] == ["GOOD"] and p.stats["errors"] == ["simulated outage"]
    # The cache file was not touched, so the next load tries again instead of trusting it.
    p.load(["GOOD"])
    assert p.calls == 3 and p.stats["stale"] == ["GOOD"]


# --------------------------------------------------------------------------- #
# Regime: unknown is never assumed to be risk-on
# --------------------------------------------------------------------------- #
def test_regime_unknown_when_benchmark_missing_or_stale():
    cfg = StrategyConfig().with_overrides({"themes.enabled": False})
    data = {"UP1": _trend(0.003), "UP2": _trend(0.002), "UP3": _trend(0.002)}
    snap = regime_snapshot(data, cfg)
    assert not snap.known and not snap.ok and "QQQ bars not loaded" in snap.missing[0]
    assert "REGIME UNKNOWN" in snap.describe(cfg)

    stale_bench = _trend(0.002).iloc[:-30]  # benchmark stopped six weeks before the universe
    snap2 = regime_snapshot({**data, "QQQ": stale_bench}, cfg)
    assert not snap2.known and "QQQ last bar" in snap2.missing[0]

    good = regime_snapshot({**data, "QQQ": _trend(0.002)}, cfg)
    assert good.known and good.ok and good.benchmark_ok

    vix_cfg = cfg.with_overrides({"regime.max_vix": 30.0})
    snap3 = regime_snapshot({**data, "QQQ": _trend(0.002)}, vix_cfg)
    assert not snap3.known and any("^VIX" in m for m in snap3.missing)


def test_cycle_treats_unknown_regime_as_risk_off_and_records_gap(tmp_path):
    cfg = StrategyConfig().with_overrides({"themes.enabled": False, "context.enabled": False})
    frames = {"ABC": make_breakout_frame(), "FILL": _trend(0.001, n=len(make_breakout_frame()))}
    frames["FILL"].index = frames["ABC"].index
    brk = PaperBroker(tmp_path / "ledger.json", starting_cash=100_000)
    report = run_cycle(frames, brk, cfg, TraderState())
    assert not report.regime_known and not report.regime_ok
    assert any(g.startswith("regime unknown") for g in report.data_gaps)
    assert report.triggered and not report.plans  # a real breakout was found but nothing was bought
    assert all("market_regime" in p.failed_checks for p in report.rejected)
    assert "regime_known" in report.to_dict() and report.to_dict()["data_gaps"]


def test_stale_price_data_blocks_new_entries_but_keeps_exits(tmp_path):
    cfg = StrategyConfig().with_overrides({"themes.enabled": False, "context.enabled": False, "regime.enabled": False})
    frames = {"ABC": make_breakout_frame()}
    brk = PaperBroker(tmp_path / "ledger.json", starting_cash=100_000)
    report = run_cycle(frames, brk, cfg, TraderState(), block_new_entries="price data stale: latest bar 2025-01-10, expected 2026-09-10")
    assert report.data_gaps == ["price data stale: latest bar 2025-01-10, expected 2026-09-10"]
    assert report.triggered and not report.plans
    rej = report.rejected[0]
    assert rej.checks["price_data_fresh"] is False and rej.data_gaps and "stale" in rej.data_gaps[0]
    assert any(a.startswith("DATA price data stale") for a in report.actions)


# --------------------------------------------------------------------------- #
# Plans never size against an assumed balance
# --------------------------------------------------------------------------- #
def test_plan_without_broker_account_is_not_sized():
    cfg = StrategyConfig().with_overrides({"regime.enabled": False, "themes.enabled": False, "context.enabled": False})
    df = prepare_data({"ABC": make_breakout_frame()}, cfg)["ABC"]
    sig = BreakoutDetector().detect_last("ABC", df, cfg)
    assert sig is not None
    plan = build_plan(sig, df.iloc[-1], cfg, equity=100_000, exposure=0.0, cash=100_000, regime_ok=True, equity_known=False)
    assert plan.shares == 0 and plan.risk_dollars == 0 and not plan.ok
    assert plan.checks["broker_account"] is False and "broker account unavailable" in plan.data_gaps[0]
    assert plan.rationale["size"].startswith("NOT SIZED")
    assert plan.to_dict()["data_gaps"]


def test_plan_lists_unavailable_context_sources():
    cfg = StrategyConfig().with_overrides({"regime.enabled": False, "themes.enabled": False, "edge.enabled": False})
    df = prepare_data({"ABC": make_breakout_frame()}, cfg)["ABC"]
    sig = BreakoutDetector().detect_last("ABC", df, cfg)
    ctx = ContextReport(symbol="ABC", asof="2026-09-11")
    ctx.available.update({"news_finviz": True, "stocktwits": False})
    ctx.errors["stocktwits"] = "HTTP 429"
    plan = build_plan(sig, df.iloc[-1], cfg, 100_000, 0.0, 100_000, True, context=ctx)
    assert plan.data_gaps == ["stocktwits unavailable: HTTP 429"]
    no_ctx = build_plan(sig, df.iloc[-1], cfg, 100_000, 0.0, 100_000, True, context=None, context_expected=True)
    assert "live context not gathered" in no_ctx.data_gaps[0]


# --------------------------------------------------------------------------- #
# Session / dashboard / CLI
# --------------------------------------------------------------------------- #
def test_simulated_data_is_refused_everywhere(tmp_path, monkeypatch):
    """There is no synthetic provider: every entry point refuses generated prices, with any broker."""
    for kind in ("synthetic", "simulated", "fake"):
        with pytest.raises(ValueError, match="never uses simulated"):
            TradingSession(SessionSettings(data=kind, state_dir=tmp_path))
    with pytest.raises(ValueError, match="never uses simulated"):
        make_provider("synthetic")
    for args in (["paper", "run"], ["scan"], ["backtest"], ["status"], ["daemon", "--once", "after_close"]):
        out = CliRunner().invoke(app, [*args, "--data", "synthetic", "--state-dir", str(tmp_path)] if args[0] != "scan" and args[0] != "backtest" else [*args, "--data", "synthetic"])
        assert out.exit_code == 1 and "never uses simulated" in out.output, (args, out.output)
    # A saved data-source choice cannot smuggle it in through ``auto`` either.
    monkeypatch.setenv("QMAG_DATA", "synthetic")
    with pytest.raises(ValueError, match="never uses simulated"):
        resolve_data_kind("auto")
    assert "synthetic" not in DATA_KINDS and not hasattr(qmag_data, "SyntheticProvider")


def test_status_page(tmp_path, csv_universe):
    sess = TradingSession(SessionSettings(**csv_universe.session_kwargs(), state_dir=tmp_path / "state", charts=False, overrides=csv_universe.overrides(**{"regime.enabled": False})))
    report = sess.cycle(asof="2024-05-15", label="test")
    payload = sess.last_report()
    assert "simulated" not in payload and payload["data_source"] == "csv" and payload["data_gaps"] == []
    reg = sess.health.records()
    assert reg["broker"]["ok"] and reg["cycle"]["ok"] and reg["price_data"]["detail"].startswith("csv: ")

    client = TestClient(create_app(sess))
    home = client.get("/")
    assert home.status_code == 200 and "SIMULATED" not in home.text and "connections ·" in home.text
    page = client.get("/status")
    assert page.status_code == 200 and "SIMULATED" not in page.text
    for text in ("Connections", "Broker", "Trading cycle", "Test connections now", "NOT CHECKED YET", "href=\"/settings\""):
        assert text in page.text, text
    api = client.get("/api/status").json()
    assert "simulated" not in api and api["overall"] in ("ok", "degraded", "unknown")
    by = {c["name"]: c for c in api["connections"]}
    assert by["price_data"]["state"] == "ok" and by["broker"]["state"] == "ok" and by["news_finviz"]["state"] == "off"
    if report.plans:
        detail = client.get(f"/plan/{report.plans[0].symbol}")
        assert detail.status_code == 200 and "SIMULATED" not in detail.text
    # Probe endpoint runs and records the outcome.
    assert client.post("/api/status/probe").json()["started"] is True
    import time

    for _ in range(50):
        if not client.get("/api/status").json()["probe"]["running"]:
            break
        time.sleep(0.1)
    assert client.get("/api/status").json()["probe"]["running"] is False
    # One connection at a time: synchronous, answers with that connection's fresh record.
    assert 'class="ghost mini test-one" data-name="universe"' in page.text and 'data-name="cycle"' not in page.text.split("Trading cycle")[1][:400]
    one = client.post("/api/status/probe", json={"names": ["universe", "no_such_thing"]}).json()
    assert set(one["connections"]) == {"universe"} and one["connections"]["universe"]["state"] == "ok" and one["unknown"] == ["no_such_thing"]
    assert one["connections"]["universe"]["testable"] is True and by["cycle"]["testable"] is False


def test_status_cli(tmp_path, csv_universe):
    out = CliRunner().invoke(app, ["status", *csv_universe.cli_args(), "--state-dir", str(tmp_path / "state")])
    assert out.exit_code == 0, out.output
    assert "data=csv" in out.output and "Broker" in out.output and "connections.json" in out.output.replace("\n", "") and "SIMULATED" not in out.output
    js = CliRunner().invoke(app, ["status", *csv_universe.cli_args(), "--state-dir", str(tmp_path / "state"), "--json"])
    assert js.exit_code == 0 and '"data_source": "csv"' in js.output and "simulated" not in js.output


def test_lookup_without_setup_never_invents_adr(tmp_path):
    """A short history has no ADR: the chart shows no illustrative stop and the gap is listed."""
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    short = _trend(0.001, n=8)
    short.index = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=8)
    short.to_csv(csv_dir / "SHRT.csv", index_label="date")
    sess = TradingSession(SessionSettings(data="csv", csv_dir=str(csv_dir), state_dir=tmp_path / "state", charts=False, overrides={"regime.enabled": False, "context.enabled": False}))
    out = sess.analyze_symbol("SHRT")
    assert out["status"] == "none" and out["plan"] is None
    assert any("ADR unavailable" in g for g in out["data_gaps"])
    assert "no illustrative stop" in out["chart_note"]
    client = TestClient(create_app(sess))
    page = client.get("/symbol/SHRT")
    assert page.status_code == 200 and "ADR unavailable" in page.text
