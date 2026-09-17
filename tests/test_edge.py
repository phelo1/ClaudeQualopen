"""Unusual Whales as primary data source + the weighted edge score: client, provider,
config, every feature against mocked endpoints, coverage / threshold gates, plan gaps,
rationale, reviewer bundle, status page, dashboard panel and the `qmag edge` CLI."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from qmag import uw
from qmag.backtest import prepare_data
from qmag.config import DEFAULT_EDGE_WEIGHTS, StrategyConfig
from qmag.context.base import ContextReport
from qmag.context.edge import FEATURES, compute_edge, describe_edge, edge_gaps, summarise
from qmag.context.gather import ContextGatherer
from qmag.data import UnusualWhalesProvider, make_provider, resolve_data_kind, uw_candles_to_frame, uw_timeframe
from qmag.plan import build_plan, context_checks
from qmag.reviewer import build_edge_bundle
from qmag.setups import BreakoutDetector
from tests.conftest import make_breakout_frame

NOW = datetime(2026, 9, 11, 15, 0, tzinfo=timezone.utc)
KEY = "00000000-0000-4000-8000-000000000000"

# --------------------------------------------------------------------------- #
# Mock Unusual Whales: one payload per endpoint, shaped like the documented responses
# --------------------------------------------------------------------------- #
PAYLOADS: dict[str, object] = {
    "/stock/ABC/info": {"has_options": True, "sector": "Technology", "marketcap": "5000000000", "next_earnings_date": "2026-10-28", "avg30_volume": "1000000"},
    "/stock/ABC/stock-state": {"close": "53.10", "prev_close": "51.00", "tape_time": "2026-09-11T14:40:00Z", "total_volume": 3000000},
    "/stock/ABC/net-prem-ticks": [
        {"net_call_premium": "300000", "net_put_premium": "-50000", "tape_time": "2026-09-11T14:35:00Z"},
        {"net_call_premium": "200000", "net_put_premium": "100000", "tape_time": "2026-09-11T14:40:00Z"},
    ],
    "/stock/ABC/oi-change": [
        {"option_symbol": "ABC261016C00060000", "oi_diff_plain": "4000", "curr_oi": 5000, "last_oi": 1000},
        {"option_symbol": "ABC261016P00045000", "oi_diff_plain": "1000", "curr_oi": 2000, "last_oi": 1000},
    ],
    "/stock/ABC/greek-exposure": [
        {"date": "2026-09-09", "call_delta": "1", "put_delta": "-1", "call_gamma": "1", "put_gamma": "-1"},
        {"date": "2026-09-10", "call_delta": "20000000", "put_delta": "-8000000", "call_gamma": "1000000", "put_gamma": "-3000000"},
    ],
    "/stock/ABC/gex-levels": {"call_wall": "60", "put_wall": "45", "gamma_flip": "50", "gamma_magnet": None},
    "/stock/ABC/volatility/option-sentiment": {"latest": {"score": 0.35, "vwks": 0.2, "avar": 0.5}},
    "/stock/ABC/options-pulse": {"sntm_score": 40, "call_txn": 300, "put_txn": 100},
    "/stock/ABC/volatility/term-structure": [
        {"dte": 7, "expiry": "2026-09-18", "volatility": "0.50", "implied_move_perc": 0.04},
        {"dte": 35, "expiry": "2026-10-16", "volatility": "0.50", "implied_move_perc": 0.09},
        {"dte": 63, "expiry": "2026-11-13", "volatility": "0.48", "implied_move_perc": 0.12},
    ],
    "/stock/ABC/historical-risk-reversal-skew": [{"date": f"2026-08-{d:02d}", "risk_reversal": 0.05} for d in range(20, 29)] + [{"date": "2026-09-10", "risk_reversal": 0.02}],
    "/stock/ABC/max-pain": [{"expiry": "2026-09-18", "max_pain": "52"}, {"expiry": "2026-10-16", "max_pain": "50"}],
    "/stock/ABC/volatility/stats": {"iv": "0.50", "rv": "0.45", "iv_rank": "0.4"},
    "/darkpool/ABC": [
        {"price": "53.20", "premium": "2000000", "size": 37500, "nbbo_bid": "53.00", "nbbo_ask": "53.20", "canceled": False, "executed_at": "2026-09-11T14:00:00Z"},
        {"price": "53.05", "premium": "500000", "size": 9400, "nbbo_bid": "53.00", "nbbo_ask": "53.20", "canceled": False, "executed_at": "2026-09-11T14:10:00Z"},
        {"price": "53.00", "premium": "9000000", "size": 170000, "nbbo_bid": "53.00", "nbbo_ask": "53.20", "canceled": True, "executed_at": "2026-09-11T14:12:00Z"},
    ],
    "/stock/ABC/unusualness": {"opt_vol_pctile": 96.5, "stock_vol_pctile": 90, "stock_samples": 90},
    "/shorts/ABC/interest-float/v2": {"si_float": "0.12", "days_to_cover": "3", "total_float": "25000000", "market_date": "2026-08-31"},
    "/insider/ABC/ticker-flow": [
        {"date": "2026-08-20", "buy_sell": "buy", "premium": "500000", "transactions": 2, "uniq_insiders": 2},
        {"date": "2026-08-01", "buy_sell": "sell", "premium": "200000", "transactions": 1, "uniq_insiders": 1},
        {"date": "2025-01-01", "buy_sell": "sell", "premium": "90000000", "transactions": 9},  # outside insider_days
    ],
    "/institution/ABC/ownership": [
        {"report_date": "2026-06-30", "units_change": "3000000", "name": "Alpha Capital"},
        {"report_date": "2026-06-30", "units_change": "-1000000", "name": "Beta Partners"},
        {"report_date": "2026-03-31", "units_change": "-9000000", "name": "Old Quarter"},
    ],
    "/congress/recent-trades": [{"ticker": "ABC", "txn_type": "Buy", "amounts": "$15,001 - $50,000", "transaction_date": "2026-08-15"}],
    "/screener/analysts": [
        {"ticker": "ABC", "action": "upgraded", "recommendation": "buy", "target": "70", "timestamp": "2026-09-01T12:00:00Z"},
        {"ticker": "ABC", "action": "maintained", "recommendation": "hold", "target": "60", "timestamp": "2026-08-20T12:00:00Z"},
    ],
    "/seasonality/ABC/monthly": [{"month": 9, "positive_months_perc": "0.7", "median_change": "0.03", "years": 10}, {"month": 10, "positive_months_perc": "0.5", "years": 10}],
    "/earnings/ABC": [
        {"report_date": "2026-07-29", "actual_eps": "1.20", "street_mean_est": "1.00", "post_earnings_move_1d": "0.08"},
        {"report_date": "2026-10-28", "actual_eps": None, "street_mean_est": "1.10"},
    ],
    "/news/headlines": [
        {"headline": "ABC wins record data-centre contract", "created_at": "2026-09-10T12:00:00Z", "sentiment": "positive", "is_major": True, "source": "wire"},
        {"headline": "ABC faces supplier probe", "created_at": "2026-09-09T12:00:00Z", "sentiment": "negative", "is_major": False, "source": "wire"},
        {"headline": "Ancient news", "created_at": "2026-01-01T12:00:00Z", "sentiment": "positive"},
    ],
    "/market/market-tide": [
        {"timestamp": "2026-09-11T14:30:00Z", "net_call_premium": "100000000", "net_put_premium": "50000000"},
        {"timestamp": "2026-09-11T14:35:00Z", "net_call_premium": "120000000", "net_put_premium": "40000000"},
    ],
    "/market/Technology/sector-tide": [{"timestamp": "2026-09-11T14:35:00Z", "net_call_premium": "30000000", "net_put_premium": "30000000"}],
    "/stock/ABC/ohlc/1d": [
        {"date": "2026-09-08", "open": "50.0", "high": "51.0", "low": "49.5", "close": "50.8", "volume": 1_000_000},
        {"date": "2026-09-09", "open": "50.9", "high": "52.0", "low": "50.5", "close": "51.9", "volume": 1_200_000},
        {"date": "2026-09-10", "open": "52.0", "high": "53.5", "low": "51.8", "close": "53.1", "volume": 1_500_000},
        {"date": "2026-09-10", "open": "52.0", "high": "53.5", "low": "51.8", "close": "53.1", "volume": 1_500_000},  # duplicate day
    ],
}


class _Resp:
    def __init__(self, payload, status=200, headers=None):
        self.payload, self.status_code, self.headers = payload, status, headers or {}

    def json(self):
        return self.payload


def mock_uw(monkeypatch, *, fail: dict[str, int] | None = None, override: dict[str, object] | None = None, key: str | None = KEY):
    """Route ``qmag.uw.requests.get`` to the canned payloads; ``fail`` maps a path to an HTTP status."""
    fail, override = fail or {}, override or {}
    seen: dict = {"calls": []}

    def fake_get(url, headers=None, params=None, timeout=None):
        assert url.startswith(uw.UW_BASE)
        path = url[len(uw.UW_BASE):]
        seen["calls"].append((path, dict(params or {})))
        assert headers["Authorization"] == f"Bearer {key}"
        if path in fail:
            return _Resp({"reason": "Insufficient privileges" if fail[path] == 403 else "boom"}, fail[path])
        if path in override:
            return _Resp({"data": override[path]})
        if path not in PAYLOADS:
            return _Resp({"reason": "not found"}, 404)
        return _Resp({"data": PAYLOADS[path]})

    monkeypatch.setattr(uw.requests, "get", fake_get)
    monkeypatch.setattr(uw, "throttle", lambda rpm=None: None)
    if key:
        monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", key)
    else:
        monkeypatch.delenv("UNUSUAL_WHALES_API_KEY", raising=False)
    return seen


@pytest.fixture
def breakout():
    cfg = StrategyConfig().with_overrides({"regime.enabled": False, "themes.enabled": False})
    df = prepare_data({"ABC": make_breakout_frame()}, cfg)["ABC"]
    sig = BreakoutDetector().detect_last("ABC", df, cfg)
    assert sig is not None
    return cfg, df, sig


def _flow_report(**kw) -> ContextReport:
    rep = ContextReport(symbol="ABC", asof="2026-09-11")
    rep.available["unusual_whales"] = True
    rep.flow_score, rep.flow_note = 0.6, "2 unusual trades, calls 3.2x"
    for k, v in kw.items():
        setattr(rep, k, v)
    return rep


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def test_edge_config_defaults_merge_and_three_level_override():
    cfg = StrategyConfig()
    assert cfg.options_flow.enabled is True and cfg.edge.enabled is True and cfg.edge.gate is True
    assert cfg.edge.threshold == 0.15 and cfg.edge.min_coverage == 0.5
    assert cfg.edge.weights == DEFAULT_EDGE_WEIGHTS and set(cfg.edge.weights) == {f.name for f in FEATURES}
    partial = StrategyConfig.from_dict({"edge": {"threshold": 0.3, "weights": {"flow": 3.0, "congress": 0}}})
    assert partial.edge.threshold == 0.3 and partial.edge.weights["flow"] == 3.0 and partial.edge.weights["congress"] == 0
    assert partial.edge.weights["dark_pool"] == DEFAULT_EDGE_WEIGHTS["dark_pool"]  # untouched keys keep their defaults
    ov = cfg.with_overrides({"edge.weights.flow": 2.0, "edge.gate": False})
    assert ov.edge.weights["flow"] == 2.0 and ov.edge.gate is False and cfg.edge.weights["flow"] == 1.5  # frozen original
    assert StrategyConfig.from_dict(json.loads(json.dumps(ov.to_dict()))).edge.weights["flow"] == 2.0


# --------------------------------------------------------------------------- #
# client + provider
# --------------------------------------------------------------------------- #
def test_uw_client_auth_cache_and_errors(monkeypatch, tmp_path):
    seen = mock_uw(monkeypatch, fail={"/stock/ABC/max-pain": 403})
    c = uw.UWClient(cache_path=tmp_path / "uw.json")
    r = c.get("/stock/ABC/info", ttl=3600)
    assert r.ok and r.data["sector"] == "Technology" and c.calls == 1
    again = c.get("/stock/ABC/info", ttl=3600)
    assert again.cached and again.data == r.data and c.calls == 1 and c.hits == 1
    assert c.get("/stock/ABC/info", ttl=0).cached is False and c.calls == 2  # ttl 0 = always live
    bad = c.get("/stock/ABC/max-pain")
    assert not bad.ok and bad.error == "HTTP 403 Insufficient privileges" and bad.status == 403
    assert not c.get("/nowhere").ok
    c.save_cache()
    fresh = uw.UWClient(cache_path=tmp_path / "uw.json")
    assert fresh.get("/stock/ABC/info", ttl=3600).cached  # survives the disk round-trip
    assert len(seen["calls"]) == 4

    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY")
    nokey = uw.UWClient(cache_path=None)
    assert nokey.get("/stock/ABC/info").error == "no UNUSUAL_WHALES_API_KEY" and len(seen["calls"]) == 4


def test_uw_client_retries_429(monkeypatch):
    attempts = []

    def flaky(url, headers=None, params=None, timeout=None):
        attempts.append(url)
        return _Resp({"reason": "slow down"}, 429, {"Retry-After": "1"}) if len(attempts) < 2 else _Resp({"data": {"ok": 1}})

    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", KEY)
    monkeypatch.setattr(uw.requests, "get", flaky)
    monkeypatch.setattr(uw, "throttle", lambda rpm=None: None)
    slept = []
    monkeypatch.setattr(uw.time, "sleep", lambda s: slept.append(s))
    r = uw.UWClient(cache_path=None).get("/x")
    assert r.ok and r.data == {"ok": 1} and len(attempts) == 2 and slept == [1.0]


def test_resolve_data_kind_and_timeframes(monkeypatch):
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY", raising=False)
    assert resolve_data_kind("auto") == "yfinance" and resolve_data_kind("uw") == "unusual_whales" and resolve_data_kind("yahoo") == "yfinance"
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", KEY)
    assert resolve_data_kind("auto") == "unusual_whales"
    monkeypatch.setenv("QMAG_DATA", "yfinance")
    assert resolve_data_kind("auto") == "yfinance" and resolve_data_kind("uw") == "unusual_whales"
    monkeypatch.delenv("QMAG_DATA")
    for fake in ("synthetic", "simulated", "fake"):
        with pytest.raises(ValueError, match="never uses simulated"):
            resolve_data_kind(fake)
    assert uw_timeframe("2026-08-20", "2026-09-11") == "22D" and uw_timeframe("2025-09-01", "2026-09-11") == "13M"
    assert uw_timeframe("2020-01-01", "2026-09-11") == "7Y" and uw_timeframe(None, None) == "2Y"


def test_uw_candles_to_frame_validates_and_dedupes():
    df = uw_candles_to_frame(PAYLOADS["/stock/ABC/ohlc/1d"])
    assert list(df.columns[:5]) == ["open", "high", "low", "close", "volume"] and len(df) == 3
    assert df.index.is_monotonic_increasing and df["close"].iloc[-1] == 53.1 and df["volume"].dtype.kind in "if"
    assert uw_candles_to_frame([]) is None


def test_unusual_whales_provider_reads_uw_and_sweeps_via_yahoo(monkeypatch, tmp_path):
    seen = mock_uw(monkeypatch)
    p = make_provider("auto", cache_dir=tmp_path / "cache", max_age_hours=0.0, bulk_threshold=2)
    assert isinstance(p, UnusualWhalesProvider) and p.cache_dir == tmp_path / "cache" / "uw"
    frames = p.load(["ABC", "ZZZ"], start="2026-09-01", end="2026-09-11")
    assert set(frames) == {"ABC"} and len(frames["ABC"]) == 3 and p.stats["uw_symbols"] == 2
    assert p.stats["missing"] == ["ZZZ"] and any("ZZZ" in e and "404" in e for e in p.stats["errors"])
    paths = [pth for pth, _ in seen["calls"]]
    assert "/stock/ABC/ohlc/1d" in paths and seen["calls"][0][1]["timeframe"] == "10D" and seen["calls"][0][1]["end_date"] == "2026-09-11"
    assert (tmp_path / "cache" / "uw" / "ABC.csv").exists()

    # Over the bulk threshold: the Yahoo batch downloader does the sweep, in its own cache dir, and the stats say so.
    called = {}

    def fake_yahoo(self, symbols, start, end):
        called["symbols"] = list(symbols)
        idx = pd.bdate_range("2026-08-01", "2026-09-10")
        return {s: pd.DataFrame({"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 1000}, index=idx) for s in symbols}

    monkeypatch.setattr("qmag.data.YFinanceProvider._download", fake_yahoo)
    before = len(seen["calls"])
    frames = p.load(["AAA", "BBB", "CCC"], start="2026-08-01")
    assert set(frames) == {"AAA", "BBB", "CCC"} and called["symbols"] == ["AAA", "BBB", "CCC"]
    assert p.stats["bulk_source"] == "yfinance" and p.stats["bulk_symbols"] == 3 and len(seen["calls"]) == before
    assert (tmp_path / "cache" / "AAA.csv").exists() and not (tmp_path / "cache" / "uw" / "AAA.csv").exists()

    monkeypatch.setenv("UNUSUAL_WHALES_BULK_THRESHOLD", "0")
    p2 = make_provider("unusual_whales", cache_dir=tmp_path / "c2", max_age_hours=0.0)
    p2.load(["ABC", "AAA", "BBB", "CCC"], start="2026-09-01", end="2026-09-11")
    assert "bulk_source" not in p2.stats and p2.stats["uw_symbols"] == 4  # 0 = always Unusual Whales


# --------------------------------------------------------------------------- #
# the score itself
# --------------------------------------------------------------------------- #
def test_compute_edge_scores_every_feature(monkeypatch):
    seen = mock_uw(monkeypatch)
    cfg = StrategyConfig()
    rep = _flow_report()
    out = compute_edge(rep, cfg, uw.UWClient(cache_path=None), NOW)
    f = out["features"]
    assert set(f) == {x.name for x in FEATURES} and out["applicable"] == 22 and out["answered"] == 22 and out["coverage"] == 1.0
    assert out["missing"] == [] and "error" not in out
    assert f["flow"]["score"] == 0.6
    assert f["net_premium"]["score"] == pytest.approx(0.818, abs=0.001) and "net call $500k vs net put $50k" in f["net_premium"]["value"]
    assert f["oi_change"]["score"] == pytest.approx(0.6)
    assert f["dealer_delta"]["score"] == pytest.approx(12 / 28, abs=0.001) and "2026-09-10" in f["dealer_delta"]["value"]  # newest row wins
    assert f["gamma"]["score"] == 1.0 and "negative" in f["gamma"]["value"] and "call wall 60 (+13.0% from spot)" in f["gamma"]["value"]
    assert f["option_sentiment"]["score"] == pytest.approx(0.35) and f["options_pulse"]["score"] == pytest.approx(0.4)
    assert f["skew"]["score"] == 1.0 and "calls bid" in f["skew"]["value"] and "2026-10-16 (35d)" in f["skew"]["value"]
    assert f["max_pain"]["score"] == pytest.approx(-0.207, abs=0.001) and "max pain 52 for 2026-09-18 (7d)" in f["max_pain"]["value"]
    assert f["volatility"]["score"] == pytest.approx(-0.022, abs=0.001) and "term structure normal" in f["volatility"]["value"]
    assert f["dark_pool"]["score"] == pytest.approx(0.7) and "$2.5M in 3 prints" in f["dark_pool"]["value"]  # cancelled print ignored
    assert f["relative_volume"]["score"] == pytest.approx(0.8)
    assert f["short_interest"]["score"] == pytest.approx(0.68) and rep.short_float_pct == 12.0 and rep.float_shares == 25e6
    assert f["insiders"]["score"] == pytest.approx(0.6667, abs=0.001) and "bought $500k (2 tx)" in f["insiders"]["value"]
    assert f["institutions"]["score"] == pytest.approx(0.5) and "quarter 2026-06-30" in f["institutions"]["value"]
    assert f["congress"]["score"] == 1.0 and "1 buys (~$33k)" in f["congress"]["value"]
    assert f["analysts"]["score"] == pytest.approx(0.5) and rep.target_price == 65.0 and "+22% vs spot" in f["analysts"]["value"]
    assert f["seasonality"]["score"] == pytest.approx(0.5) and "September: up in 70% of 10 years" in f["seasonality"]["value"]
    assert f["earnings"]["score"] == 1.0 and rep.earnings_recent_days == 44 and "beat" in f["earnings"]["value"]
    assert f["news"]["score"] == pytest.approx(1 / 3, abs=0.001) and len(rep.headlines) == 2 and rep.news_score is not None
    assert f["market_tide"]["score"] == pytest.approx(0.5) and f["sector_tide"]["score"] == 0.0 and "Technology" in f["sector_tide"]["value"]
    # basics filled from /info and /stock-state
    assert rep.sector == "Technology" and rep.market_cap == 5e9 and rep.earnings_date == "2026-10-28" and rep.days_to_earnings == 47
    assert out["spot"] == 53.1 and out["has_options"] is True
    # weighted aggregate, gate, bookkeeping
    expected = sum(f[n]["weight"] * f[n]["score"] for n in f) / sum(f[n]["weight"] for n in f)
    assert out["score"] == pytest.approx(expected, abs=0.001) and out["score"] > 0.15 and out["passed"] is True
    assert rep.edge_score == out["score"] and rep.available["uw_edge"] is True and "uw_edge" not in rep.errors
    assert out["positives"][0] == "flow" and "max_pain" in out["negatives"]  # ranked by weight x score
    assert out["calls"] == len(seen["calls"]) and out["calls"] <= 26 and out["cached"] == 0
    # composite blends it in with the configured rank weight
    rep.weights = {"news": 1.0, "social": 0.6, "flow": 1.0, "edge": cfg.edge.rank_weight}
    assert rep.composite is not None and rep.composite > 0
    # survives the context-cache round trip
    again = ContextReport.from_dict(json.loads(json.dumps(rep.to_dict(), default=str)))
    assert again.edge["score"] == out["score"] and again.edge_score == out["score"]


def test_compute_edge_missing_is_not_neutral(monkeypatch):
    fails = {"/stock/ABC/net-prem-ticks": 500, "/darkpool/ABC": 403, "/stock/ABC/volatility/option-sentiment": 500}
    mock_uw(monkeypatch, fail=fails, override={"/stock/ABC/options-pulse": {"weird": 1}})
    cfg = StrategyConfig()
    rep = _flow_report()
    out = compute_edge(rep, cfg, uw.UWClient(cache_path=None), NOW)
    f = out["features"]
    assert f["net_premium"]["score"] is None and f["net_premium"]["error"] == "HTTP 500 boom"
    assert f["dark_pool"]["error"] == "HTTP 403 Insufficient privileges"
    assert f["options_pulse"]["score"] is None and "unrecognised response shape" in f["options_pulse"]["error"]
    assert sorted(out["missing"]) == ["dark_pool", "net_premium", "option_sentiment", "options_pulse"]
    total = sum(f[n]["weight"] for n in f)
    lost = sum(f[n]["weight"] for n in out["missing"])
    assert out["coverage"] == pytest.approx((total - lost) / total, abs=0.001) and out["answered"] == 18
    assert "unavailable: " in rep.errors["uw_edge"] and rep.available["uw_edge"] is True  # a score exists, gaps are named
    gaps = edge_gaps(out, cfg)
    assert any("scored as missing, not as neutral" in g and "Net premium today (HTTP 500 boom)" in g for g in gaps)

    # Everything fails (the flow scan too) -> no score, no pass, explicit gap; never zero.
    mock_uw(monkeypatch, fail={p: 500 for p in PAYLOADS})
    rep = ContextReport(symbol="ABC", asof="2026-09-11")
    rep.available["unusual_whales"], rep.errors["unusual_whales"] = False, "options_volume: HTTPError: 500"
    out = compute_edge(rep, cfg, uw.UWClient(cache_path=None), NOW)
    assert out["features"]["flow"]["error"] == "options_volume: HTTPError: 500"
    assert out["score"] is None and out["passed"] is False and out["coverage"] == 0.0 and out["answered"] == 0
    assert rep.edge_score is None and rep.available["uw_edge"] is False
    assert any("no feature answered" in g for g in edge_gaps(out, cfg))
    assert "UNAVAILABLE" in describe_edge(out, cfg) and "gate fails" in describe_edge(out, cfg)

    # No key: nothing is called, the reason is stated.
    mock_uw(monkeypatch, key=None)
    rep = _flow_report()
    out = compute_edge(rep, cfg, uw.UWClient(cache_path=None), NOW)
    assert out["error"] == "no UNUSUAL_WHALES_API_KEY" and out["score"] is None and rep.errors["uw_edge"] == out["error"]
    assert edge_gaps(out, cfg) == ["Unusual Whales edge score not computed: no UNUSUAL_WHALES_API_KEY"]


def test_compute_edge_skips_options_features_without_listed_options(monkeypatch):
    mock_uw(monkeypatch, override={"/stock/ABC/info": {"has_options": False, "sector": "Utilities"}})
    cfg = StrategyConfig()
    out = compute_edge(_flow_report(), cfg, uw.UWClient(cache_path=None), NOW)
    f = out["features"]
    opt = [x.name for x in FEATURES if x.needs_options]
    assert all(f[n]["applicable"] is False and f[n]["score"] is None for n in opt)
    assert out["applicable"] == 22 - len(opt) and out["missing"] == ["sector_tide"]  # Utilities tide not mocked -> honest miss
    assert out["coverage"] < 1.0 and out["has_options"] is False
    assert not any(n in opt for n in out["missing"])  # n/a is neither answered nor missing


def test_weights_zero_switch_features_off_and_never_call_them(monkeypatch):
    seen = mock_uw(monkeypatch)
    cfg = StrategyConfig().with_overrides({"edge.weights.congress": 0, "edge.weights.dark_pool": 0.0, "edge.weights.insiders": 4.0})
    out = compute_edge(_flow_report(), cfg, uw.UWClient(cache_path=None), NOW)
    assert "congress" not in out["features"] and "dark_pool" not in out["features"] and out["features"]["insiders"]["weight"] == 4.0
    assert not any(p.startswith(("/congress", "/darkpool")) for p, _ in seen["calls"])
    s = summarise({"a": {"weight": 1.0, "score": 0.5}, "b": {"weight": 3.0, "score": -0.5}, "c": {"weight": 1.0, "score": None}, "d": {"weight": 9.0, "score": None, "applicable": False}}, cfg)
    assert s["score"] == pytest.approx(-0.25) and s["coverage"] == pytest.approx(0.8) and s["passed"] is False and s["missing"] == ["c"]


def test_features_are_cached_by_ttl_class(monkeypatch, tmp_path):
    seen = mock_uw(monkeypatch)
    cfg = StrategyConfig().with_overrides({"context.cache_minutes": 0.0})  # intraday reads never cached
    client = uw.UWClient(cache_path=tmp_path / "uw.json")
    first = compute_edge(_flow_report(), cfg, client, NOW)
    second = compute_edge(_flow_report(), cfg, client, NOW)
    assert second["cached"] > 0 and second["calls"] < first["calls"] and second["score"] == first["score"]
    live = [p for p, _ in seen["calls"][first["calls"]:]]
    assert "/stock/ABC/net-prem-ticks" in live and "/stock/ABC/stock-state" in live and "/darkpool/ABC" in live  # intraday: re-read
    assert "/insider/ABC/ticker-flow" not in live and "/seasonality/ABC/monthly" not in live  # daily: served from cache
    assert "/market/market-tide" not in live  # shared market reads: 5-minute cache
    assert (tmp_path / "uw.json").exists()
    # With the default 30-minute context cache the intraday reads are held too: one cycle = one read per endpoint.
    compute_edge(_flow_report(), StrategyConfig(), client, NOW)
    fourth = compute_edge(_flow_report(), StrategyConfig(), client, NOW)
    assert fourth["calls"] == 1 and [p for p, _ in seen["calls"][-1:]] == ["/stock/ABC/stock-state"]  # spot is always live


# --------------------------------------------------------------------------- #
# gatherer wiring
# --------------------------------------------------------------------------- #
def test_gatherer_runs_edge_after_flow_and_respects_caps(monkeypatch, tmp_path):
    mock_uw(monkeypatch)
    for name in ("fetch_finviz", "fetch_yahoo", "fetch_stocktwits", "fetch_reddit"):
        monkeypatch.setattr(f"qmag.context.gather.{name}", lambda *a, **k: ([], []) if name in ("fetch_stocktwits", "fetch_reddit") else None)

    def fake_flow(rep, settings=None, now=None):
        rep.available["unusual_whales"], rep.flow_score = True, 0.6

    monkeypatch.setattr("qmag.context.gather.fetch_unusual_whales", fake_flow)
    cfg = StrategyConfig().with_overrides({"options_flow.max_symbols_per_cycle": 3, "edge.max_symbols_per_cycle": 1})
    g = ContextGatherer(cfg, cache_path=tmp_path / "ctx.json", workers=1)
    assert g.paid_caps() == (3, 1) and g.uw.cache_path == tmp_path / "uw_cache.json"
    rep = g.one("ABC", NOW)
    assert rep.edge is not None and rep.edge_score is not None and rep.weights["edge"] == cfg.edge.rank_weight
    assert g.one("ABC", NOW, edge=False).edge is None and g.one("ABC", NOW, flow=False).edge is None
    off = cfg.with_overrides({"edge.enabled": False})
    assert ContextGatherer(off, cache_path=tmp_path / "c2.json").one("ABC", NOW).edge is None
    # gather: symbols beyond the edge cap keep the flow scan but not the edge score (the payloads only exist for ABC)
    out = g.gather(["ABC", "XYZ"], NOW)
    assert out["ABC"].edge is not None and out["XYZ"].flow_score == 0.6 and out["XYZ"].edge is None


# --------------------------------------------------------------------------- #
# gates, gaps, rationale, bundle
# --------------------------------------------------------------------------- #
def _edge(score, coverage=1.0, cfg=None):
    cfg = cfg or StrategyConfig()
    return {
        "score": score, "coverage": coverage, "threshold": cfg.edge.threshold, "min_coverage": cfg.edge.min_coverage, "gate": True,
        "passed": score is not None and score >= cfg.edge.threshold and coverage >= cfg.edge.min_coverage, "answered": 20, "applicable": 22,
        "positives": ["gamma", "flow"], "negatives": ["max_pain"], "missing": ["dark_pool", "congress"],
        "features": {
            "gamma": {"label": "Gamma regime & walls", "group": "options", "weight": 0.5, "score": 0.9, "value": "net gamma -$2.0M", "error": None, "applicable": True},
            "flow": {"label": "Unusual options flow", "group": "options", "weight": 1.5, "score": 0.6, "value": "2 unusual trades", "error": None, "applicable": True},
            "max_pain": {"label": "Max pain", "group": "options", "weight": 0.25, "score": -0.2, "value": "max pain 52", "error": None, "applicable": True},
            "dark_pool": {"label": "Dark pool prints", "group": "tape", "weight": 0.75, "score": None, "value": "", "error": "HTTP 403 Insufficient privileges", "applicable": True},
            "congress": {"label": "Congressional trades", "group": "ownership", "weight": 0.25, "score": None, "value": "", "error": "HTTP 500", "applicable": True},
        },
        "calls": 20, "cached": 3, "asof": "2026-09-11T15:00", "spot": 53.1, "has_options": True,
    }


def test_edge_gates_fail_closed(breakout, monkeypatch):
    cfg, df, sig = breakout
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", KEY)  # a keyed desk: the gate is armed
    ok = context_checks(sig, _flow_report(edge=_edge(0.4)), cfg)
    assert ok["uw_edge"] is True and ok["uw_edge_coverage"] is True
    low = context_checks(sig, _flow_report(edge=_edge(0.05)), cfg)
    assert low["uw_edge"] is False and low["uw_edge_coverage"] is True
    thin = context_checks(sig, _flow_report(edge=_edge(0.4, coverage=0.3)), cfg)
    assert thin["uw_edge"] is True and thin["uw_edge_coverage"] is False
    none = context_checks(sig, _flow_report(edge=None), cfg)
    assert none["uw_edge"] is False and none["uw_edge_coverage"] is False  # never computed -> cannot pass
    unscored = context_checks(sig, _flow_report(edge=_edge(None, coverage=0.0)), cfg)
    assert unscored["uw_edge"] is False and unscored["uw_edge_coverage"] is False
    advisory = context_checks(sig, _flow_report(edge=_edge(-0.9)), cfg.with_overrides({"edge.gate": False}))
    assert "uw_edge" not in advisory and "uw_edge_coverage" not in advisory
    stricter = context_checks(sig, _flow_report(edge=_edge(0.4)), cfg.with_overrides({"edge.threshold": 0.5}))
    assert stricter["uw_edge"] is False
    flow_off = context_checks(sig, _flow_report(edge=_edge(0.4)), cfg.with_overrides({"options_flow.enabled": False}))
    assert "uw_edge" not in flow_off  # the score cannot run without the flow scan, so it is not demanded


def test_edge_gate_is_not_armed_without_the_key(breakout, monkeypatch):
    """No UNUSUAL_WHALES_API_KEY: the score can never be computed, so demanding
    it would reject every plan for ever. The gate stands down and the plan says
    so; nothing else about the checklist changes."""
    from qmag.plan import EDGE_GATE_INACTIVE, build_plan, edge_gate_active

    cfg, df, sig = breakout
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY", raising=False)
    assert edge_gate_active(cfg) is False
    checks = context_checks(sig, _flow_report(edge={"score": None, "coverage": 0.0, "error": "no UNUSUAL_WHALES_API_KEY"}), cfg)
    assert "uw_edge" not in checks and "uw_edge_coverage" not in checks
    rep = _flow_report(edge={"score": None, "coverage": 0.0, "error": "no UNUSUAL_WHALES_API_KEY"})
    rep.available["uw_edge"] = False
    rep.errors["uw_edge"] = "no UNUSUAL_WHALES_API_KEY"
    plan = build_plan(sig, df.iloc[-1], cfg, 100_000, 0, 100_000, True, context=rep, context_expected=True)
    assert plan.ok and EDGE_GATE_INACTIVE in plan.data_gaps
    # the same plan on a keyed desk whose feed did not answer still fails closed
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", KEY)
    assert edge_gate_active(cfg) is True
    plan = build_plan(sig, df.iloc[-1], cfg, 100_000, 0, 100_000, True, context=rep, context_expected=True)
    assert not plan.ok and plan.checks["uw_edge"] is False and EDGE_GATE_INACTIVE not in plan.data_gaps
    assert edge_gate_active(cfg.with_overrides({"edge.gate": False})) is False


def test_plan_carries_edge_gaps_rationale_and_bundle(breakout, monkeypatch):
    cfg, df, sig = breakout
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", KEY)
    ctx = _flow_report(edge=_edge(0.4))
    ctx.edge_score = 0.4
    plan = build_plan(sig, df.iloc[-1], cfg, 100_000, 0.0, 100_000, True, context=ctx)
    assert plan.ok and plan.checks["uw_edge"] and plan.checks["uw_edge_coverage"]
    assert any("Dark pool prints (HTTP 403 Insufficient privileges)" in g and "not as neutral" in g for g in plan.data_gaps)
    assert not any(g.startswith("uw_edge unavailable") for g in plan.data_gaps)
    r = plan.rationale
    assert "Unusual Whales edge score +0.40 (threshold +0.15; 20/22 features answered, coverage 100%)" in r["context"]
    assert "PASSES the entry threshold" in r["context"] and "gamma regime & walls +0.90" in r["context"]
    assert "gamma regime & walls +0.90 (Unusual Whales)" in r["bull_case"] and "max pain -0.20 (Unusual Whales)" in r["bear_case"]
    bundle = build_edge_bundle(plan.to_dict(), True)
    e = bundle["unusual_whales_edge"]
    assert e["score"] == 0.4 and e["threshold"] == 0.15 and e["passed"] is True and e["unavailable"] == ["dark_pool", "congress"]
    assert e["features"]["gamma"] == {"score": 0.9, "weight": 0.5, "value": "net gamma -$2.0M", "error": None}

    weak = build_plan(sig, df.iloc[-1], cfg, 100_000, 0.0, 100_000, True, context=_flow_report(edge=_edge(-0.1)))
    assert not weak.ok and weak.failed_checks == ["uw_edge"]
    assert "FAILS the +0.15 entry threshold" in weak.rationale["context"] and "REJECTED: the Unusual Whales edge score is below" in weak.rationale["context"]
    thin = build_plan(sig, df.iloc[-1], cfg, 100_000, 0.0, 100_000, True, context=_flow_report(edge=_edge(0.4, coverage=0.2)))
    assert thin.failed_checks == ["uw_edge_coverage"] and "coverage below 50%" in thin.rationale["context"]
    assert any("coverage 20% is below the 50% required" in g for g in thin.data_gaps)
    missing = build_plan(sig, df.iloc[-1], cfg, 100_000, 0.0, 100_000, True, context=_flow_report())
    assert sorted(missing.failed_checks) == ["uw_edge", "uw_edge_coverage"]
    assert any("not computed for this symbol" in g for g in missing.data_gaps) and "UNAVAILABLE" in missing.rationale["context"]


# --------------------------------------------------------------------------- #
# status page, dashboard, CLI
# --------------------------------------------------------------------------- #
def test_status_lists_every_feature_and_folds_outcomes(tmp_path, monkeypatch):
    from qmag.health import GROUP_LABELS, SPECS, describe_connections
    from qmag.session import SessionSettings, TradingSession

    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY", raising=False)
    sess = TradingSession(SessionSettings(data="auto", state_dir=tmp_path / "s", charts=False))
    assert sess.s.data == "yfinance" and sess.requested_data == "auto"
    names = {sp.name for sp in SPECS}
    assert "uw_edge" in names and {f"uw:{f.name}" for f in FEATURES} <= names and GROUP_LABELS["uw"] == "Unusual Whales edge features"
    conn = describe_connections(sess.s, sess.cfg, tmp_path / "s", sess.health)
    by = {c["name"]: c for c in conn["connections"]}
    assert by["uw_edge"]["state"] == "not_configured" and by["uw:gamma"]["state"] == "not_configured" and by["price_data"]["note"].startswith("Yahoo")
    assert conn["overall"] in ("degraded", "error") and any(g["key"] == "uw" for g in conn["groups"])

    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", KEY)
    sess = TradingSession(SessionSettings(data="auto", state_dir=tmp_path / "s", charts=False, overrides={"edge.weights.congress": 0}))
    assert sess.s.data == "unusual_whales"
    rep = _flow_report(edge=_edge(0.4))
    rep.available["uw_edge"] = True
    sess.health.record_sources([rep])
    conn = describe_connections(sess.s, sess.cfg, tmp_path / "s", sess.health)
    by = {c["name"]: c for c in conn["connections"]}
    assert by["price_data"]["note"].startswith("Unusual Whales daily candles") and "Yahoo batches" in by["price_data"]["note"]
    assert by["uw_edge"]["state"] == "ok" and "gate: score >= +0.15" in by["uw_edge"]["note"]
    assert by["uw:gamma"]["state"] == "ok" and by["uw:gamma"]["note"].startswith("weight 0.5") and "hedging amplifies" in by["uw:gamma"]["note"]
    assert by["uw:dark_pool"]["state"] == "error" and by["uw:dark_pool"]["last_error"] == "HTTP 403 Insufficient privileges"
    assert by["uw:congress"]["state"] == "off" and "weight 0" in by["uw:congress"]["note"]
    assert by["uw:net_premium"]["state"] == "unknown"  # not in this report at all


def test_dashboard_shows_edge_panel_and_pill(tmp_path, monkeypatch, csv_universe):
    from qmag.dashboard import create_app
    from qmag.session import SessionSettings, TradingSession

    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", KEY)
    sess = TradingSession(SessionSettings(**csv_universe.session_kwargs(), state_dir=tmp_path / "state", charts=False, overrides=csv_universe.overrides(**{"regime.enabled": False})))
    report = sess.cycle(asof="2024-05-15", label="test")
    assert report.plans
    client = TestClient(create_app(sess))
    home = client.get("/desk")
    assert "uw edge · on · gate ≥ +0.15" in home.text
    sym = report.plans[0].symbol
    payload = json.loads(sess.report_path.read_text())
    ctx = _flow_report(edge=_edge(0.4))
    ctx.symbol, ctx.edge_score = sym, 0.4
    for p in payload["plans"]:
        if p["symbol"] == sym:
            p["context"] = ctx.to_dict()
    sess.report_path.write_text(json.dumps(payload, default=str))
    page = client.get(f"/plan/{sym}")
    assert page.status_code == 200
    assert "Unusual Whales edge score" in page.text and "+0.40" in page.text and "PASS" in page.text
    assert "Gamma regime &amp; walls" in page.text and "Dark pool prints" in page.text and "HTTP 403 Insufficient privileges" in page.text
    assert "Options positioning &amp; flow" in page.text and "20/22" in page.text
    assert "UW edge" in client.get("/desk").text  # context strip cell on the plan card
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY")
    assert "uw edge · no key" in TestClient(create_app(sess)).get("/desk").text
    off = TradingSession(SessionSettings(**csv_universe.session_kwargs(), state_dir=tmp_path / "state", charts=False, overrides=csv_universe.overrides(**{"regime.enabled": False, "edge.enabled": False})))
    assert "uw edge · off" in TestClient(create_app(off)).get("/desk").text


def test_edge_cli(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from qmag.cli import app

    mock_uw(monkeypatch)
    out = CliRunner().invoke(app, ["edge", "abc", "--state-dir", str(tmp_path)])
    assert out.exit_code == 0, out.output
    assert "ABC" in out.output and "Gamma regime" in out.output and "vs threshold +0.15" in out.output and "PASSES" in out.output
    raw = CliRunner().invoke(app, ["edge", "ABC", "--json", "--state-dir", str(tmp_path)])
    data = json.loads(raw.output)
    assert raw.exit_code == 0 and data["passed"] is True and data["features"]["net_premium"]["score"] == pytest.approx(0.818, abs=0.001)
    rules = CliRunner().invoke(app, ["edge", "ABC", "--rules"])
    assert rules.exit_code == 0 and "/darkpool/{t}" in rules.output and "22 features enabled" in rules.output
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY")
    out = CliRunner().invoke(app, ["edge", "ABC", "--state-dir", str(tmp_path)])
    assert out.exit_code == 1 and "UNUSUAL_WHALES_API_KEY" in out.output
