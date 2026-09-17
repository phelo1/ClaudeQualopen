"""Unusual options flow (Unusual Whales): on/off switch, fetch + scoring with mocked endpoints,
gates, rationale, reviewer bundle, dashboard panel and the `qmag flow` CLI."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from qmag.backtest import prepare_data
from qmag.config import OptionsFlowSettings, StrategyConfig
from qmag.context import sources
from qmag.context.base import ContextReport
from qmag.context.gather import ContextGatherer
from qmag.plan import build_plan, context_checks
from qmag.reviewer import build_edge_bundle
from qmag.setups import BreakoutDetector
from tests.conftest import make_breakout_frame

NOW = datetime(2026, 9, 11, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def breakout():
    cfg = StrategyConfig().with_overrides({"regime.enabled": False, "themes.enabled": False})
    df = prepare_data({"ABC": make_breakout_frame()}, cfg)["ABC"]
    sig = BreakoutDetector().detect_last("ABC", df, cfg)
    assert sig is not None
    return cfg, df, sig


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


VOLUME_ROW = {
    "date": "2026-09-11", "bullish_premium": 900_000, "bearish_premium": 200_000, "call_premium": 1_400_000, "put_premium": 400_000,
    "call_volume": 32_000, "avg_30_day_call_volume": 10_000, "put_volume": 7_000, "avg_30_day_put_volume": 8_000,
}
ALERTS = [
    # bullish: call bought at the ask, sweep, opening
    {"type": "call", "strike": "60", "expiry": "2026-10-16", "created_at": _iso(0.2), "total_premium": 620_000, "total_ask_side_prem": 600_000,
     "total_bid_side_prem": 20_000, "has_sweep": True, "volume": 4100, "open_interest": 900, "volume_oi_ratio": 4.56, "underlying_price": 53.1,
     "alert_rule": "RepeatedHitsAscendingFill", "total_size": 2000},
    # bullish: put SOLD at the bid
    {"type": "put", "strike": "45", "expiry": "2026-10-16", "created_at": _iso(1.0), "total_premium": 150_000, "total_ask_side_prem": 10_000,
     "total_bid_side_prem": 140_000, "has_sweep": False, "volume": 900, "open_interest": 1200, "volume_oi_ratio": 0.75, "underlying_price": 53.1},
    # bearish: put bought at the ask
    {"type": "put", "strike": "50", "expiry": "2026-09-25", "created_at": _iso(0.5), "total_premium": 90_000, "total_ask_side_prem": 90_000,
     "total_bid_side_prem": 0, "has_sweep": False, "volume": 500, "open_interest": 100, "volume_oi_ratio": 5.0, "underlying_price": 53.1},
    # too small -> dropped
    {"type": "call", "strike": "55", "expiry": "2026-09-18", "created_at": _iso(0.1), "total_premium": 12_000, "total_ask_side_prem": 12_000},
    # too old -> dropped
    {"type": "call", "strike": "70", "expiry": "2026-12-18", "created_at": _iso(9), "total_premium": 900_000, "total_ask_side_prem": 900_000},
    # too far out -> dropped
    {"type": "call", "strike": "80", "expiry": "2027-06-18", "created_at": _iso(0.1), "total_premium": 800_000, "total_ask_side_prem": 800_000},
]


class _Resp:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status

    def raise_for_status(self):
        if self.status >= 400:
            raise sources.requests.HTTPError(f"{self.status} error")

    def json(self):
        return self.payload


def _mock_uw(monkeypatch, *, alerts_status=200, unusual_status=200, volume_status=200, seen=None):
    seen = seen if seen is not None else {}

    def fake_get(url, headers=None, params=None, timeout=None):
        seen.setdefault("calls", []).append((url, params))
        assert headers["Authorization"] == "Bearer uw-test"
        if url.endswith("/options-volume"):
            return _Resp({"data": [VOLUME_ROW]}, volume_status)
        if url.endswith("/option-trades/flow-alerts"):
            return _Resp({"data": ALERTS}, alerts_status)
        if url.endswith("/flow-alerts"):  # legacy per-ticker endpoint
            return _Resp({"data": ALERTS[:1]})
        if url.endswith("/unusualness"):
            return _Resp({"data": {"opt_vol_pctile": 96.5, "opt_vol_decile": 10, "stock_vol_pctile": 80}}, unusual_status)
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(sources.requests, "get", fake_get)
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "uw-test")
    return seen


# --------------------------------------------------------------------------- #
# config: paid feed is on by default (the desk runs on an Unusual Whales key), switchable; old keys still load
# --------------------------------------------------------------------------- #
def test_options_flow_is_on_by_default_switchable_and_old_keys_migrate():
    assert StrategyConfig().options_flow.enabled is True
    cfg = StrategyConfig.from_dict({"context": {"flow_enabled": False, "flow_min_score": -0.3, "bogus": 1}, "options_flow": {"min_premium": 25_000}})
    assert cfg.options_flow.enabled is False and cfg.options_flow.min_score == -0.3 and cfg.options_flow.min_premium == 25_000
    assert StrategyConfig().with_overrides({"options_flow.enabled": False}).options_flow.enabled is False


def test_gatherer_never_calls_unusual_whales_when_off(monkeypatch):
    monkeypatch.setattr("qmag.context.gather.fetch_unusual_whales", lambda *a, **k: pytest.fail("paid endpoint must not be called when off"))
    monkeypatch.setattr("qmag.context.gather.compute_edge", lambda *a, **k: pytest.fail("edge score must not run when the flow scan is off"))
    for name in ("fetch_finviz", "fetch_yahoo", "fetch_stocktwits", "fetch_reddit"):
        monkeypatch.setattr(f"qmag.context.gather.{name}", lambda *a, **k: ([], []) if name in ("fetch_stocktwits", "fetch_reddit") else None)
    cfg = StrategyConfig().with_overrides({"options_flow.enabled": False})
    rep = ContextGatherer(cfg).one("ABC", NOW)
    assert "unusual_whales" not in rep.available and rep.flow_score is None and rep.weights["flow"] == 1.0 and rep.edge is None

    calls = []
    monkeypatch.setattr("qmag.context.gather.fetch_unusual_whales", lambda rep, settings=None, now=None: calls.append((rep.symbol, settings.min_premium)))
    on = cfg.with_overrides({"options_flow.enabled": True, "options_flow.min_premium": 75_000, "options_flow.weight": 0.5, "edge.enabled": False})
    rep = ContextGatherer(on).one("ABC", NOW)
    assert calls == [("ABC", 75_000)] and rep.weights["flow"] == 0.5
    ContextGatherer(on).one("ABC", NOW, flow=False)
    assert len(calls) == 1  # per-cycle cap path skips the paid call


def test_gather_caps_paid_calls_to_top_ranked(monkeypatch, tmp_path):
    for name in ("fetch_finviz", "fetch_yahoo", "fetch_stocktwits", "fetch_reddit"):
        monkeypatch.setattr(f"qmag.context.gather.{name}", lambda *a, **k: ([], []) if name in ("fetch_stocktwits", "fetch_reddit") else None)
    flow_calls = []
    monkeypatch.setattr("qmag.context.gather.fetch_unusual_whales", lambda rep, settings=None, now=None: flow_calls.append(rep.symbol))
    cfg = StrategyConfig().with_overrides({"options_flow.enabled": True, "options_flow.max_symbols_per_cycle": 2, "edge.enabled": False})
    g = ContextGatherer(cfg, cache_path=tmp_path / "c.json", workers=1)
    out = g.gather(["A", "B", "C", "D"], NOW)
    assert set(out) == {"A", "B", "C", "D"} and sorted(flow_calls) == ["A", "B"]


# --------------------------------------------------------------------------- #
# fetch + scoring against mocked endpoints
# --------------------------------------------------------------------------- #
def test_fetch_unusual_whales_scans_and_scores(monkeypatch):
    seen = _mock_uw(monkeypatch)
    f = OptionsFlowSettings(enabled=True, min_premium=50_000, lookback_days=3, max_dte=90)
    rep = ContextReport(symbol="ABC", asof="2026-09-11")
    sources.fetch_unusual_whales(rep, settings=f, now=NOW)

    urls = [u for u, _ in seen["calls"]]
    assert urls == [f"{sources.UW_BASE}/stock/ABC/options-volume", f"{sources.UW_BASE}/option-trades/flow-alerts", f"{sources.UW_BASE}/stock/ABC/unusualness"]
    params = seen["calls"][1][1]
    assert params["ticker_symbol"] == "ABC" and params["unusual"] == "true" and params["min_premium"] == 50_000
    assert params["max_dte"] == 90 and params["newer_than"] == "2026-09-08" and params["limit"] == 200

    assert rep.available["unusual_whales"] is True and "unusual_whales" not in rep.errors
    assert rep.flow_score is not None and rep.flow_score > 0.5
    assert rep.flow_unusual is True
    assert rep.flow_call_premium == 1_400_000 and rep.flow_bull_premium == 900_000 and rep.flow_bear_premium == 200_000
    assert rep.flow_call_vol_ratio == 3.2 and rep.flow_put_vol_ratio == pytest.approx(0.88, abs=0.01)
    assert rep.flow_opt_vol_pctile == 96.5
    # 3 kept (small, old and far-dated dropped); 2 bullish (ask-side call + bid-side put), 1 bearish, 1 sweep
    assert (rep.flow_alerts, rep.flow_bull_alerts, rep.flow_bear_alerts, rep.flow_sweeps) == (3, 2, 1, 1)
    top = rep.flow_trades[0]
    assert top["premium"] == 620_000 and top["type"] == "call" and top["strike"] == 60.0 and top["direction"] == "bull"
    assert top["side"] == "ask" and top["sweep"] is True and top["vol_oi"] == 4.56 and top["otm_pct"] == pytest.approx(13.0, abs=0.1)
    assert top["dte"] == 35 and top["rule"] == "RepeatedHitsAscendingFill"
    assert rep.flow_trades[1]["direction"] == "bull" and rep.flow_trades[1]["side"] == "bid" and rep.flow_trades[1]["type"] == "put"
    assert rep.flow_trades[2]["direction"] == "bear"
    assert "3 unusual trades" in rep.flow_note and "3.2x" in rep.flow_note and "96th percentile" in rep.flow_note
    # survives the cache round-trip
    again = ContextReport.from_dict(json.loads(json.dumps(rep.to_dict(), default=str)))
    assert again.flow_trades == rep.flow_trades and again.flow_unusual and again.weights == rep.weights


def test_fetch_unusual_whales_degrades_per_endpoint(monkeypatch):
    # No key: nothing is called.
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY", raising=False)
    monkeypatch.setattr(sources.requests, "get", lambda *a, **k: pytest.fail("must not call without a key"))
    rep = ContextReport(symbol="ABC", asof="d")
    sources.fetch_unusual_whales(rep, now=NOW)
    assert rep.available["unusual_whales"] is False and "UNUSUAL_WHALES_API_KEY" in rep.errors["unusual_whales"]

    # Alerts endpoint fails -> legacy per-ticker fallback; unusualness fails -> still available, error noted.
    _mock_uw(monkeypatch, alerts_status=404, unusual_status=500)
    rep = ContextReport(symbol="ABC", asof="d")
    sources.fetch_unusual_whales(rep, settings=OptionsFlowSettings(enabled=True), now=NOW)
    assert rep.available["unusual_whales"] is True and rep.flow_alerts == 1 and rep.flow_opt_vol_pctile is None
    assert "flow_alerts" in rep.errors["unusual_whales"] and "unusualness" in rep.errors["unusual_whales"]

    # Everything fails -> unavailable.
    _mock_uw(monkeypatch, alerts_status=500, unusual_status=500, volume_status=500)
    monkeypatch.setattr(sources, "_uw_get", lambda *a, **k: (a[3].__setitem__(a[4], "HTTPError: 500"), None)[1])
    rep = ContextReport(symbol="ABC", asof="d")
    sources.fetch_unusual_whales(rep, settings=OptionsFlowSettings(enabled=True), now=NOW)
    assert rep.available["unusual_whales"] is False and rep.flow_score is None


def test_flow_score_direction_and_percentile_amplification():
    bearish = [{"type": "put", "total_premium": 500_000, "total_ask_side_prem": 500_000, "created_at": _iso(0.1), "expiry": "2026-10-16"}]
    rep = ContextReport(symbol="X", asof="d")
    s = sources.flow_score_from({"bullish_premium": 100_000, "bearish_premium": 900_000}, bearish, rep, now=NOW)
    assert s is not None and s < -0.6 and rep.flow_bear_alerts == 1 and rep.flow_unusual
    amplified = sources.flow_score_from({"bullish_premium": 100_000, "bearish_premium": 900_000}, bearish, None, now=NOW, opt_vol_pctile=99)
    damped = sources.flow_score_from({"bullish_premium": 100_000, "bearish_premium": 900_000}, bearish, None, now=NOW, opt_vol_pctile=10)
    assert amplified < s < damped
    assert sources.flow_score_from(None, [], None, now=NOW) is None
    # weights: switching flow off in the blend removes it from the composite
    rep = ContextReport(symbol="X", asof="d", news_score=0.5, flow_score=-0.9)
    assert rep.composite < 0
    rep.weights = {"news": 1.0, "social": 0.6, "flow": 0.0}
    assert rep.composite == 0.5


# --------------------------------------------------------------------------- #
# how it feeds the edge: gates, rationale, reviewer bundle
# --------------------------------------------------------------------------- #
def _ctx(**kw) -> ContextReport:
    rep = ContextReport(symbol="ABC", asof="2026-09-11")
    rep.available["unusual_whales"] = True
    for k, v in kw.items():
        setattr(rep, k, v)
    return rep


def test_flow_gates(breakout):
    cfg, df, sig = breakout
    off = cfg.with_overrides({"options_flow.enabled": False})
    assert "options_flow" not in context_checks(sig, _ctx(flow_score=-0.9), off)
    on = cfg.with_overrides({"options_flow.enabled": True, "options_flow.min_score": -0.3, "edge.enabled": False})
    assert context_checks(sig, _ctx(flow_score=-0.9), on)["options_flow"] is False
    assert context_checks(sig, _ctx(flow_score=0.1), on)["options_flow"] is True
    assert context_checks(sig, _ctx(), on)["options_flow"] is True  # missing passes
    req = on.with_overrides({"options_flow.require_bullish": True, "options_flow.bullish_threshold": 0.2, "options_flow.min_alerts": 1})
    assert context_checks(sig, _ctx(), req)["unusual_flow_bullish"] is False  # missing FAILS when required
    assert context_checks(sig, _ctx(flow_score=0.6, flow_bull_alerts=0), req)["unusual_flow_bullish"] is False
    assert context_checks(sig, _ctx(flow_score=0.6, flow_bull_alerts=2), req)["unusual_flow_bullish"] is True


def test_rationale_and_bundle_describe_unusual_trades(breakout, monkeypatch):
    cfg, df, sig = breakout
    _mock_uw(monkeypatch)
    on = cfg.with_overrides({"options_flow.enabled": True, "edge.enabled": False})
    ctx = ContextReport(symbol="ABC", asof="2026-09-11")
    sources.fetch_unusual_whales(ctx, settings=on.options_flow, now=NOW)
    plan = build_plan(sig, df.iloc[-1], on, 100_000, 0.0, 100_000, True, context=ctx)
    r = plan.rationale
    assert "Options flow leans bullish" in r["context"] and "Largest unusual trades" in r["context"]
    assert "$620k of 2026-10-16 60C bought at the ask (sweep, vol/OI 4.6, 13% OTM)" in r["context"]
    assert "$150k of 2026-10-16 45P sold at the bid" in r["context"]
    assert "unusual bullish options activity (2 trades, 1 sweeps)" in r["bull_case"]
    assert plan.ok and plan.checks["options_flow"] is True
    bundle = build_edge_bundle(plan.to_dict(), True)
    flow = bundle["options_flow"]
    assert flow["unusual_activity"] is True and flow["option_volume_percentile"] == 96.5 and flow["bullish_trades"] == 2
    assert flow["largest_trades"][0]["premium"] == 620_000 and flow["largest_trades"][0]["direction"] == "bull"

    # Required-but-absent flow is called out and rejects the plan.
    req = on.with_overrides({"options_flow.require_bullish": True})
    quiet = ContextReport(symbol="ABC", asof="2026-09-11")
    quiet.available["unusual_whales"] = True
    rejected = build_plan(sig, df.iloc[-1], req, 100_000, 0.0, 100_000, True, context=quiet)
    assert not rejected.ok and rejected.failed_checks == ["unusual_flow_bullish"]
    assert "Options tape is quiet" in rejected.rationale["context"] and "REJECTED: the strategy requires bullish unusual flow" in rejected.rationale["context"]

    # Off: the rationale says so and no flow gate exists.
    off_cfg = cfg.with_overrides({"options_flow.enabled": False})
    off_plan = build_plan(sig, df.iloc[-1], off_cfg, 100_000, 0.0, 100_000, True, context=ContextReport(symbol="ABC", asof="d"))
    assert "Options flow scan is off" in off_plan.rationale["context"] and "options_flow" not in off_plan.checks


# --------------------------------------------------------------------------- #
# dashboard + CLI
# --------------------------------------------------------------------------- #
def test_dashboard_shows_unusual_trades_and_switch_state(tmp_path, monkeypatch, csv_universe):
    from qmag.dashboard import create_app
    from qmag.session import SessionSettings, TradingSession

    _mock_uw(monkeypatch)
    sess = TradingSession(SessionSettings(**csv_universe.session_kwargs(), state_dir=tmp_path / "state", charts=False, overrides=csv_universe.overrides(**{"regime.enabled": False, "options_flow.enabled": False})))
    report = sess.cycle(asof="2024-05-15", label="test")
    assert report.plans
    # Switched off: header pill says so, plan page explains the switch.
    client = TestClient(create_app(sess))
    home = client.get("/desk")
    assert "options flow · off" in home.text
    sym = report.plans[0].symbol
    # Inject a real (mocked) Unusual Whales scan into the saved report as if the flow scan had been on.
    payload = json.loads(sess.report_path.read_text())
    ctx = ContextReport(symbol=sym, asof="2024-05-15")
    sources.fetch_unusual_whales(ctx, settings=OptionsFlowSettings(enabled=True), now=NOW)
    for p in payload["plans"]:
        if p["symbol"] == sym:
            p["context"] = ctx.to_dict()
    payload["config"]["options_flow"]["enabled"] = True
    sess.report_path.write_text(json.dumps(payload, default=str))

    page = client.get(f"/plan/{sym}")
    assert page.status_code == 200
    assert "Unusual options flow" in page.text and "UNUSUAL ACTIVITY" in page.text
    assert "60C" in page.text and "bought @ ask" in page.text and "sold @ bid" in page.text and "sweep" in page.text
    assert "3 unusual trades" in page.text and "trades ≥ $50k in the last 3d" in page.text
    home = client.get("/desk")
    assert "· unusual" in home.text  # context strip flags it on the plan card

    on = TradingSession(SessionSettings(**csv_universe.session_kwargs(), state_dir=tmp_path / "state", charts=False, overrides=csv_universe.overrides(**{"regime.enabled": False})))  # default: on
    assert "options flow · on" in TestClient(create_app(on)).get("/desk").text
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY")
    assert "options flow · no key" in TestClient(create_app(on)).get("/desk").text


def test_flow_cli(monkeypatch):
    from typer.testing import CliRunner

    from qmag.cli import app

    _mock_uw(monkeypatch)
    # Keep the CLI's relative lookback anchored to the fixture's date.
    real_fetch = sources.fetch_unusual_whales
    monkeypatch.setattr(sources, "fetch_unusual_whales", lambda rep, settings, now: real_fetch(rep, settings=settings, now=NOW))
    out = CliRunner().invoke(app, ["flow", "abc", "--min-premium", "50000"])
    assert out.exit_code == 0, out.output
    assert "ABC" in out.output and "UNUSUAL ACTIVITY" in out.output and "60C" in out.output and "sweep" in out.output
    assert "scan is on" in out.output  # default config: the trader uses it
    raw = CliRunner().invoke(app, ["flow", "ABC", "--json"])
    assert raw.exit_code == 0 and '"flow_unusual": true' in raw.output and '"flow_trades"' in raw.output

    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY")
    out = CliRunner().invoke(app, ["flow", "ABC"])
    assert out.exit_code == 1 and "UNUSUAL_WHALES_API_KEY" in out.output

    # --options-flow / --no-options-flow flip the switch for a session-based command
    from qmag.cli import _flow_override

    assert _flow_override(None) == {} and _flow_override(True) == {"options_flow.enabled": True} and _flow_override(False) == {"options_flow.enabled": False}
