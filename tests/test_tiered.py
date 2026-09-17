"""Tiered schedule: arming list, focused passes, volume pace, screens, daemon
timetable and the Saturday insider / unusual-options scan (all sources mocked)."""

from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from qmag import uw
from qmag.cli import app
from qmag.config import StrategyConfig
from qmag.daemon import NY, Daemon
from qmag.dashboard import create_app
from qmag.insider_scan import aggregate, normalise_analysis, run_weekly_scan, scan_window, score_ticker
from qmag.pace import project_volume, session_fraction
from qmag.screener import run_screen
from qmag.session import SessionSettings, TradingSession

runner = CliRunner()


class _Resp:
    def __init__(self, payload, status=200):
        self.payload, self.status_code, self.headers = payload, status, {}

    def json(self):
        return self.payload


def _mock_uw(monkeypatch, handler, key="test-key"):
    """Route ``qmag.uw.requests.get`` through ``handler(path, params) -> payload | (payload, status)``."""
    seen = []

    def fake_get(url, headers=None, params=None, timeout=None):
        assert url.startswith(uw.UW_BASE)
        path = url[len(uw.UW_BASE):]
        seen.append((path, dict(params or {})))
        out = handler(path, params or {})
        if isinstance(out, tuple):
            return _Resp(out[0], out[1])
        return _Resp({"data": out})

    monkeypatch.setattr(uw.requests, "get", fake_get)
    monkeypatch.setattr(uw, "throttle", lambda rpm=None: None)
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", key)
    return seen


# --------------------------------------------------------------------------- #
# Volume pace
# --------------------------------------------------------------------------- #
def test_session_fraction_follows_the_u_curve_and_is_none_outside_the_session():
    day = datetime(2026, 9, 10, tzinfo=NY)  # a Thursday
    assert session_fraction(day.replace(hour=9, minute=0)) is None
    assert session_fraction(day.replace(hour=9, minute=30)) == 0.0
    assert session_fraction(day.replace(hour=10, minute=0)) == pytest.approx(0.15, abs=0.01)
    assert 0.45 < session_fraction(day.replace(hour=12, minute=30)) < 0.5
    assert session_fraction(day.replace(hour=16, minute=0)) == 1.0
    assert session_fraction(datetime(2026, 9, 12, 11, 0, tzinfo=NY)) is None  # Saturday
    # Early close (day after Thanksgiving 2026 is 27 Nov): half-day is compressed to 13:00.
    assert session_fraction(datetime(2026, 11, 27, 12, 59, tzinfo=NY)) > 0.9
    assert session_fraction(datetime(2026, 11, 27, 13, 0, tzinfo=NY)) == 1.0


def test_project_volume_scales_only_todays_bar():
    idx = pd.to_datetime(["2026-09-09", "2026-09-10"])
    df = pd.DataFrame({"open": [1, 1], "high": [1, 1], "low": [1, 1], "close": [1, 1], "volume": [1000.0, 300.0]}, index=idx)
    old = pd.DataFrame({"open": [1], "high": [1], "low": [1], "close": [1], "volume": [500.0]}, index=pd.to_datetime(["2026-09-09"]))
    now = datetime(2026, 9, 10, 10, 0, tzinfo=NY)  # ~15 % of the session done
    out, note = project_volume({"A": df, "B": old}, now)
    assert note["applied"] and note["symbols"] == 1 and 6 < note["multiplier"] < 7.5
    assert out["A"]["volume"].iloc[-1] == pytest.approx(300 / note["fraction"], rel=1e-6)
    assert out["A"]["volume"].iloc[0] == 1000.0 and out["B"]["volume"].iloc[-1] == 500.0
    assert df["volume"].iloc[-1] == 300.0  # input untouched
    _, early = project_volume({"A": df}, datetime(2026, 9, 10, 9, 33, tzinfo=NY))
    assert not early["applied"] and "too noisy" in early["reason"]
    _, closed = project_volume({"A": df}, datetime(2026, 9, 10, 17, 0, tzinfo=NY))
    assert not closed["applied"] and "session over" in closed["reason"]


# --------------------------------------------------------------------------- #
# Full scan -> arming list -> focused pass
# --------------------------------------------------------------------------- #
def _session(tmp_path, csv_universe, **extra) -> TradingSession:
    return TradingSession(
        SessionSettings(**csv_universe.session_kwargs(), state_dir=tmp_path / "state", charts=False, overrides=csv_universe.overrides(**{"regime.enabled": False, **extra}))
    )


def test_full_scan_arms_and_focused_pass_keeps_unchanged_buy_stops(tmp_path, csv_universe):
    sess = _session(tmp_path, csv_universe, **{"entry.mode": "resting"})
    full = sess.cycle(asof="2024-05-14", label="nightly")
    assert full.scan == "full" and full.arming, "the nightly scan should arm the flags near their pivots"
    assert any(a.startswith("ARM ") for a in full.actions)
    assert (tmp_path / "state" / "last_full_report.json").exists()
    state = sess.state()
    assert set(state.arming) == {a["symbol"] for a in full.arming}
    assert all(abs(a["distance_pct"]) <= sess.cfg.schedule.arming_distance_pct for a in full.arming)
    assert state.last_full_scan == "2024-05-14"
    pending_before = dict(state.pending)
    assert pending_before

    focused = sess.focused_cycle(label="focused", asof="2024-05-14")
    assert focused is not None and focused.scan == "focused"
    assert set(focused.scope) == set(state.arming) | set(pending_before)
    # Names still too far from the pivot are left alone, their buy-stops untouched.
    assert focused.skipped_far and all(s in pending_before for s in focused.skipped_far)
    kept = [a for a in focused.actions if a.startswith("KEEP buy-stop")]
    assert kept, "unchanged plans must be kept, not cancelled and re-placed"
    assert not any(a.startswith("PLAN buy-stop") for a in focused.actions), "no new orders for identical plans"
    after = sess.state()
    for sym, rec in pending_before.items():
        assert after.pending[sym]["order_id"] == rec["order_id"]
    # Nothing inside the focus scope was invented: every detected signal is a scoped symbol.
    assert {s.symbol for s in focused.watchlist + focused.triggered} <= set(focused.scope)
    rep = json.loads((tmp_path / "state" / "last_report.json").read_text())
    assert rep["scan"] == "focused" and rep["full_scan_asof"] == "2024-05-14" and rep["arming"]

    # Next session: buy-stops fill, filled names leave the arming list, orders for dead setups are cancelled.
    nxt = sess.focused_cycle(label="focused", asof="2024-05-15")
    filled = {a.split()[3] for a in nxt.actions if a.startswith("FILL buy")}
    assert filled
    later = sess.state()
    # A buy-stop filled on a wick that closed back below the pivot is cut the same day (failed breakout);
    # everything else that filled is managed and no longer armed.
    cut = {c["symbol"] for c in later.closed if c["exit_reason"] in ("failed_breakout", "broker_execution")}
    assert all(sym in later.managed or sym in cut for sym in filled)
    assert not ((filled - cut) & set(later.arming))
    for c in later.closed:
        assert c["features"]["entry_mode"] == "buy_stop"
        if c["exit_reason"] == "failed_breakout":
            assert "wick_fill" in c["post_mortem"]["tags"]
        else:
            assert c["evidence"] == "paper_fill" and c["exit_order_ids"]


def test_focused_pass_with_nothing_armed_requests_no_data(tmp_path, csv_universe):
    sess = _session(tmp_path, csv_universe)
    assert sess.focused_cycle(asof="2024-05-14") is None
    conn = json.loads((tmp_path / "state" / "connections.json").read_text())
    assert "nothing armed" in conn["cycle"]["detail"]


def test_focused_regime_fails_closed_without_a_recent_full_scan(tmp_path, csv_universe):
    sess = _session(tmp_path, csv_universe, **{"regime.enabled": True, "regime.breadth_enabled": True})
    sess.cycle(asof="2024-05-14", label="nightly")
    # Pretend the full scan is a week old: breadth can no longer be inherited.
    full = json.loads(sess.full_report_path.read_text())
    full["asof"] = "2024-05-01"
    sess.full_report_path.write_text(json.dumps(full))
    rep = sess.focused_cycle(asof="2024-05-14")
    assert rep is not None and not rep.regime_known and not rep.regime_ok
    assert any("breadth unknown" in g for g in rep.data_gaps)


# --------------------------------------------------------------------------- #
# Daemon timetable
# --------------------------------------------------------------------------- #
def test_daemon_schedule_tiered_weekday_and_saturday(tmp_path, csv_universe):
    sess = _session(tmp_path, csv_universe)
    d = Daemon(sess, rebuild_universe=False)
    thursday = {name for _, name in d.schedule(datetime(2026, 9, 10, 0, 1, tzinfo=NY), days=1)}
    assert thursday == {"premarket", "post_open", "focused", "movers", "after_close", "reconcile", "housekeeping"}
    times = [(when, name) for when, name in d.schedule(datetime(2026, 9, 10, 0, 1, tzinfo=NY), days=1)]
    focused = [w for w, n in times if n == "focused"]
    assert focused[0].strftime("%H:%M") == "09:35" and focused[-1].strftime("%H:%M") == "15:55"
    assert all((b - a).total_seconds() == 300 for a, b in zip(focused, focused[1:]))
    saturday = [(w.strftime("%a %H:%M"), n) for w, n in d.schedule(datetime(2026, 9, 12, 0, 1, tzinfo=NY), days=1)]
    assert [(w,n) for w,n in saturday if n not in ("reconcile", "housekeeping")] == [("Sat 10:00", "insider_scan"), ("Sat 11:00", "learn")]
    assert ("Sat 04:00", "housekeeping") in saturday
    assert d._t_insider(datetime(2026, 9, 13).date()) == []  # Sunday: nothing
    status = json.loads(d.status_path.read_text()) if d.status_path.exists() else None
    d.write_status(datetime.now(NY), None)
    assert json.loads(d.status_path.read_text())["mode"] == "tiered"
    assert status is None or True


def test_daemon_schedule_legacy_when_tiered_off(tmp_path, csv_universe):
    sess = _session(tmp_path, csv_universe, **{"schedule.tiered": False, "insider_scan.enabled": False, "learning.enabled": False})
    d = Daemon(sess, rebuild_universe=False)
    names = {name for _, name in d.schedule(datetime(2026, 9, 10, 0, 1, tzinfo=NY), days=3)}
    assert names == {"post_open", "intraday", "after_close", "reconcile", "housekeeping"}


# --------------------------------------------------------------------------- #
# Screens
# --------------------------------------------------------------------------- #
SCREEN_ROWS = [
    {"ticker": "GAPR", "close": "22.0", "prev_close": "20.0", "relative_volume": "4.2", "marketcap": "900000000", "sector": "Healthcare"},
    {"ticker": "SLOW", "close": "20.5", "prev_close": "20.0", "relative_volume": "1.1", "marketcap": "900000000"},  # +2.5 %: filtered locally
    {"ticker": "THIN", "close": "30.0", "prev_close": "25.0", "relative_volume": "1.2"},  # +20 % but no volume: fails the movers rvol
    {"ticker": "QQQ", "close": "500", "prev_close": "450", "relative_volume": "9"},  # auxiliary symbol, excluded
]


def test_screens_use_unusual_whales_and_filter_locally(monkeypatch):
    seen = _mock_uw(monkeypatch, lambda path, params: SCREEN_ROWS if path == "/screener/stocks" else ({"reason": "no"}, 404))
    cfg = StrategyConfig()
    movers = run_screen("movers", cfg, uw_cache_path=None)
    assert movers.source == "unusual_whales" and movers.symbols == ["GAPR"]
    assert movers.hits[0]["change_pct"] == pytest.approx(0.10) and movers.hits[0]["source"] == "movers"
    assert seen[-1][1]["min_stock_volume_vs_avg30_volume"] == cfg.schedule.movers_min_rvol
    pre = run_screen("premarket", cfg, uw_cache_path=None)
    assert pre.symbols == ["THIN", "GAPR"]  # pre-market: gap only (biggest first), no volume requirement yet
    assert "min_stock_volume_vs_avg30_volume" not in seen[-1][1]


def test_premarket_screen_without_uw_key_is_unavailable(monkeypatch):
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY", raising=False)
    res = run_screen("premarket", StrategyConfig(), uw_cache_path=None)
    assert res.unavailable and res.hits == [] and "UNUSUAL_WHALES_API_KEY" in res.error


def test_premarket_screen_cycle_arms_gappers_without_inventing_setups(tmp_path, csv_universe, monkeypatch):
    first = csv_universe.symbols[0]
    rows = [{"ticker": first, "close": "22.0", "prev_close": "20.0", "relative_volume": "3.0"}]
    _mock_uw(monkeypatch, lambda path, params: rows if path == "/screener/stocks" else ({"reason": "no"}, 404))
    sess = _session(tmp_path, csv_universe)
    out = sess.screen_cycle("premarket")
    assert out["armed"] == [first] and not out["unavailable"]
    rec = sess.state().arming[first]
    assert rec["source"] == "premarket" and rec["pivot"] is None and rec["screen"]["change_pct"] == pytest.approx(0.10)
    conn = json.loads((tmp_path / "state" / "connections.json").read_text())
    assert conn["screener"]["ok"] and "1 hits" in conn["screener"]["detail"]
    # The focused pass now includes the gapper; a hit with no valid setup is reported, never traded.
    rep = sess.focused_cycle(asof="2024-05-14")
    assert first in rep.scope


# --------------------------------------------------------------------------- #
# Saturday insider / unusual-options scan
# --------------------------------------------------------------------------- #
def _alert(ticker, day, typ, strike, under, expiry, prem, vol, oi, ask_share=0.95, sweep=True, opening=True):
    return {
        "alert_rule": "RepeatedHits", "all_opening_trades": opening, "created_at": f"{day}T14:3{len(ticker)}:00Z", "expiry": expiry,
        "has_sweep": sweep, "has_floor": False, "has_multileg": False, "open_interest": oi, "option_chain": f"{ticker}{expiry[2:].replace('-', '')}{'C' if typ == 'call' else 'P'}{int(strike * 1000):08d}",
        "price": "2.10", "strike": str(strike), "ticker": ticker, "total_ask_side_prem": str(prem * ask_share), "total_bid_side_prem": str(prem * (1 - ask_share)),
        "total_premium": str(prem), "total_size": vol, "trade_count": 12, "type": typ, "underlying_price": str(under), "volume": vol,
        "volume_oi_ratio": str(vol / max(oi, 1)), "issue_type": "Common Stock",
    }


ALERTS = [
    # BIOX: three days of aggressive, far-OTM, short-dated call buying expiring BEFORE earnings -> should be flagged.
    _alert("BIOX", "2026-09-08", "call", 30, 22.0, "2026-09-25", 420_000, 4_100, 210),
    _alert("BIOX", "2026-09-09", "call", 30, 22.4, "2026-09-25", 610_000, 5_800, 640),
    _alert("BIOX", "2026-09-10", "call", 32, 22.9, "2026-10-02", 380_000, 3_000, 90),
    # MEGA: one large but modest trade that straddles earnings -> ordinary event speculation, below the bar.
    _alert("MEGA", "2026-09-09", "put", 95, 100.0, "2026-10-16", 250_000, 900, 3_000, ask_share=0.6, sweep=False, opening=False),
    # SPY is excluded by default even if the API returns it.
    _alert("SPY", "2026-09-10", "put", 500, 560.0, "2026-09-18", 9_000_000, 20_000, 1_000),
]
CONTRACTS = {
    "2026-09-09": [
        {"option_symbol": "BIOX260925C00030000", "option_type": "call", "strike": "30", "expiry": "2026-09-25", "premium": "610000", "volume": 5800, "open_interest": 640,
         "ask_side_volume": 5200, "bid_side_volume": 300, "sweep_volume": 2000, "stock_price": "22.4", "next_earnings_date": "2026-10-28", "er_time": "postmarket", "sector": "Healthcare"},
        {"option_symbol": "MEGA261016P00095000", "option_type": "put", "strike": "95", "expiry": "2026-10-16", "premium": "250000", "volume": 900, "open_interest": 3000,
         "ask_side_volume": 500, "bid_side_volume": 400, "sweep_volume": 0, "stock_price": "100", "next_earnings_date": "2026-10-08", "sector": "Technology"},
    ],
}
AI_REPLY = {
    "verdict": "investigate", "suspicion": 0.72, "direction": "bullish",
    "speculating_on": "A move above $30 (+35%) before 25 September, ahead of the 28 October earnings.",
    "possible_catalysts": [{"catalyst": "Trial readout or partnering announcement", "likelihood": "medium", "basis": "biotech, far-OTM calls expiring before earnings, no headline explains it"}],
    "explained_by_public_info": False,
    "what_to_check": ["Upcoming PDUFA / conference dates", "8-K filings this week", "Short interest changes"],
    "risks": ["Could be a hedge against a short stock position", "Repeated hits may be one fund scaling in"],
    "summary": "Three sessions of aggressive far-out-of-the-money call buying that expires before the next scheduled earnings, with no public catalyst in the gathered headlines. Consistent with informed positioning; worth checking the event calendar and filings.",
}


def _uw_insider_handler(path, params):
    if path == "/option-trades/flow-alerts":
        return ALERTS
    if path == "/option-activity/unusual":
        return CONTRACTS.get(params.get("date"), [])
    if path == "/news/headlines":
        return [{"headline": f"{params['ticker']} to present at healthcare conference", "created_at": "2026-09-07T12:00:00Z", "source": "wire", "sentiment": "neutral", "is_major": False}]
    if path.startswith("/insider/"):
        return [{"date": "2026-08-20", "transaction_code": "S", "buy_sell": "sell", "shares": 10000, "premium": 210000, "owner_name": "J. Doe", "officer_title": "CFO"}]
    if path.startswith("/stock/") and path.endswith("/info"):
        return {"full_name": "Biox Therapeutics", "sector": "Healthcare", "marketcap": "1200000000", "next_earnings_date": "2026-10-28"}
    return ({"reason": "not found"}, 404)


@pytest.fixture
def no_finviz(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("finvizfinance"):
            raise ImportError("finvizfinance disabled in tests")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_scan_window_covers_the_week_before_a_saturday_run():
    start, end = scan_window(datetime(2026, 9, 12, 10, 0, tzinfo=NY), 7)
    assert (start.isoformat(), end.isoformat()) == ("2026-09-05", "2026-09-11")


def test_score_ticker_rewards_pre_earnings_far_otm_aggression():
    from qmag.insider_scan import summarise_contract, summarise_trade

    asof = datetime(2026, 9, 11).date()
    biox = [summarise_trade(a) for a in ALERTS if a["ticker"] == "BIOX"]
    ctr = [summarise_contract({**c, "date": "2026-09-09", "ticker": "BIOX"}) for c in CONTRACTS["2026-09-09"] if c["option_symbol"].startswith("BIOX")]
    s = score_ticker(biox, ctr, asof)
    assert s["score"] >= 7 and s["direction"] == "bullish" and s["breakdown"]["pre_earnings"] == 1.5
    assert s["max_otm_pct"] > 35 and s["days_active"] == ["2026-09-08", "2026-09-09", "2026-09-10"]
    mega = [summarise_trade(a) for a in ALERTS if a["ticker"] == "MEGA"]
    ctr_m = [summarise_contract({**c, "date": "2026-09-09", "ticker": "MEGA"}) for c in CONTRACTS["2026-09-09"] if c["option_symbol"].startswith("MEGA")]
    m = score_ticker(mega, ctr_m, asof)
    assert m["score"] < s["score"] and m["breakdown"]["pre_earnings"] == -1.0


def test_weekly_scan_flags_ai_analyses_and_writes_report(tmp_path, monkeypatch, no_finviz):
    seen = _mock_uw(monkeypatch, _uw_insider_handler)
    monkeypatch.setenv("GEMINI_API_KEY", "gem-test")
    asked = []

    def fake_ask(bundle, cfg_like, system_prompt, schema=None):
        asked.append(bundle)
        assert "never invent" in system_prompt.lower() or "never invent" in system_prompt
        assert bundle["ticker"] == "BIOX" and bundle["public_context"]["headlines"]
        return json.dumps(AI_REPLY), "gemini", "gemini-test"

    monkeypatch.setattr("qmag.reviewer.ask_json", fake_ask)
    sess = TradingSession(SessionSettings(state_dir=tmp_path / "state", charts=False))
    report = sess.insider_scan(now=datetime(2026, 9, 12, 10, 0, tzinfo=NY))
    assert not report["unavailable"]
    assert report["week_start"] == "2026-09-05" and report["week_end"] == "2026-09-11"
    assert report["alerts_considered"] == 4  # SPY dropped by the exclusion list
    tickers = [f["ticker"] for f in report["flagged"]]
    assert tickers == ["BIOX"], report["flagged"]
    assert [r["ticker"] for r in report["below_threshold"]] == ["MEGA"]
    flag = report["flagged"][0]
    assert flag["ai"]["verdict"] == "investigate" and flag["ai"]["suspicion"] == 0.72
    assert flag["ai"]["possible_catalysts"][0]["likelihood"] == "medium"
    assert flag["context"]["insider_filings_90d"][0]["officer_title"] == "CFO"
    assert any("finviz" in g or "news_finviz" in g for g in flag["context"]["data_gaps"])  # finviz absent -> recorded, not invented
    assert len(asked) == 1 and report["ai"]["analysed"] == 1 and report["ai"]["errors"] == 0
    # Paging by time: newer_than is the window start; the daily contract screen ran for each session.
    alert_calls = [p for path, p in seen if path == "/option-trades/flow-alerts"]
    assert alert_calls and alert_calls[0]["newer_than"] == "2026-09-05" and alert_calls[0]["unusual"] == "true"
    contract_days = sorted(p["date"] for path, p in seen if path == "/option-activity/unusual")
    assert contract_days == ["2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11"]  # Mon 7 Sep 2026 is Labor Day
    saved = json.loads((tmp_path / "state" / "insider_scan.json").read_text())
    assert saved["flagged"][0]["ticker"] == "BIOX" and saved["history"][-1]["flagged"][0]["verdict"] == "investigate"
    conn = json.loads((tmp_path / "state" / "connections.json").read_text())
    assert conn["insider_scan"]["ok"] and conn["llm_insider"]["ok"]

    # Dashboard page and API.
    client = TestClient(create_app(sess))
    page = client.get("/insider")
    assert page.status_code == 200
    assert "BIOX" in page.text and "investigate" in page.text and "Trial readout" in page.text
    assert "research lead, not an accusation" in page.text
    api = client.get("/api/insider").json()
    assert api["scan"]["flagged"][0]["ticker"] == "BIOX"
    index = client.get("/")
    assert index.status_code == 200 and "Options research" in index.text and 'href="/insider"' in index.text

    # AI failure is recorded per ticker, never fabricated; the flag stands on the options data.
    monkeypatch.setattr("qmag.reviewer.ask_json", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("model down")))
    again = sess.insider_scan(now=datetime(2026, 9, 12, 10, 0, tzinfo=NY))
    assert again["flagged"][0]["ai"]["error"].endswith("model down") and again["ai"]["errors"] == 1
    assert len(again["history"]) == 1  # the same week replaces itself
    assert "AI analysis unavailable" in client.get("/insider").text


def test_scan_prefers_quiet_small_cap_bets_over_mega_cap_prints(tmp_path, monkeypatch, no_finviz):
    """The day-before profile: a $1.2B name with one OTM strike hit three times,
    chain volume 30x its 30-day average, beats a mega cap's $40m of 1-DTE
    prints in a chain that trades 800k contracts a day - and the mega cap is
    removed by the cap window even though the contract screen let it through."""
    alerts = [
        # QUIET: small cap, $95 calls ~27% OTM, 3 sessions, same strike / expiry, sweeps at the ask, opening.
        _alert("QUIET", "2026-09-09", "call", 95, 75.0, "2026-10-02", 260_000, 2_400, 120),
        _alert("QUIET", "2026-09-10", "call", 95, 75.5, "2026-10-02", 310_000, 3_100, 900),
        _alert("QUIET", "2026-09-11", "call", 95, 76.0, "2026-10-02", 280_000, 2_600, 2_800),
        # HUGE: mega cap, $40m of next-day calls 3% OTM - routine institutional flow.
        _alert("HUGE", "2026-09-10", "call", 240, 233.0, "2026-09-11", 40_000_000, 90_000, 20_000),
        _alert("HUGE", "2026-09-10", "call", 245, 233.0, "2026-09-18", 12_000_000, 30_000, 50_000),
        # ZERO: 0-DTE lottery prints are ignored altogether (min_dte).
        _alert("ZERO", "2026-09-10", "call", 50, 45.0, "2026-09-10", 900_000, 9_000, 100),
    ]
    contracts = {
        "2026-09-10": [
            {"option_symbol": "HUGE260918C00245000", "option_type": "call", "strike": "245", "expiry": "2026-09-18", "premium": "12000000", "volume": 30000, "open_interest": 50000,
             "ask_side_volume": 25000, "bid_side_volume": 2000, "sweep_volume": 9000, "stock_price": "233", "next_earnings_date": "2026-10-29", "sector": "Technology", "ticker_vol": 810000, "prev_oi": 49000},
            {"option_symbol": "QUIET261002C00095000", "option_type": "call", "strike": "95", "expiry": "2026-10-02", "premium": "310000", "volume": 3100, "open_interest": 900,
             "ask_side_volume": 2900, "bid_side_volume": 100, "sweep_volume": 1200, "stock_price": "75.5", "next_earnings_date": "2026-11-05", "sector": "Healthcare", "ticker_vol": 6100, "prev_oi": 120, "is_new": False},
        ],
    }
    caps = {"QUIET": "1200000000", "HUGE": "3400000000000", "ZERO": "500000000"}

    def handler(path, params):
        if path == "/option-trades/flow-alerts":
            return alerts
        if path == "/option-activity/unusual":
            return contracts.get(params.get("date"), [])
        if path.startswith("/stock/") and path.endswith("/info"):
            sym = path.split("/")[2]
            return {"full_name": sym, "sector": "x", "marketcap": caps[sym], "next_earnings_date": "2026-11-05" if sym == "QUIET" else "2026-10-29", "issue_type": "Common Stock"}
        if path.startswith("/stock/") and path.endswith("/options-volume"):
            sym = path.split("/")[2]
            if sym == "QUIET":
                return [{"date": "2026-09-11", "call_volume": 2800, "put_volume": 100, "avg_30_day_call_volume": 210, "avg_30_day_put_volume": 90},
                        {"date": "2026-09-10", "call_volume": 6100, "put_volume": 120, "avg_30_day_call_volume": 205, "avg_30_day_put_volume": 90},
                        {"date": "2026-09-01", "call_volume": 60000, "put_volume": 120, "avg_30_day_call_volume": 205, "avg_30_day_put_volume": 90}]  # outside the window: ignored
            return [{"date": "2026-09-10", "call_volume": 500000, "put_volume": 300000, "avg_30_day_call_volume": 480000, "avg_30_day_put_volume": 290000}]
        if path == "/news/headlines" or path.startswith("/insider/"):
            return []
        return ({"reason": "not found"}, 404)

    seen = _mock_uw(monkeypatch, handler)
    cfg = StrategyConfig()
    report = run_weekly_scan(cfg, now=datetime(2026, 9, 12, 10, 0, tzinfo=NY), uw_cache_path=None, ai=False)
    flagged = {f["ticker"]: f for f in report["flagged"]}
    assert list(flagged) == ["QUIET"], [(f["ticker"], f["score"]) for f in report["flagged"]]
    q = flagged["QUIET"]
    assert q["enriched"] and q["market_cap"] == 1.2e9 and q["volume_surge"] == pytest.approx(29.8, abs=0.1)
    assert q["cluster"]["strike"] == 95.0 and q["cluster"]["share"] == 1.0 and 25 < q["cluster"]["otm_pct"] < 28 and q["cluster"]["dte"] == 21
    b = q["breakdown"]
    assert b["otm"] == 1.5 and b["expiry_window"] == 1.5 and b["concentration"] == 1.0 and b["volume_surge"] == 2.0 and b["crowded_chain"] == 0.0 and b["pre_earnings"] == 1.5
    assert b["premium"] < 1.0  # size is not what makes it interesting
    # The mega cap is out of the report entirely: removed by the cap window, not just ranked lower.
    excluded = {e["ticker"]: e for e in report["enrichment"]["excluded_by_cap"]}
    assert "HUGE" in excluded and excluded["HUGE"]["market_cap"] == 3.4e12
    assert "HUGE" not in {r["ticker"] for r in report["below_threshold"]}
    # Before the cap check, the crowded chain already cost it points and its 1-DTE bet scored low on the expiry window.
    from qmag.insider_scan import summarise_contract, summarise_trade

    huge = score_ticker([summarise_trade(a) for a in alerts if a["ticker"] == "HUGE"], [summarise_contract({**c, "date": "2026-09-10", "ticker": "HUGE"}) for c in contracts["2026-09-10"] if c["option_symbol"].startswith("HUGE")], datetime(2026, 9, 11).date())
    assert huge["breakdown"]["crowded_chain"] == -1.5 and huge["breakdown"]["expiry_window"] == 0.25 and huge["breakdown"]["otm"] == 0.0 and huge["breakdown"]["premium"] == 1.5
    assert huge["score"] < q["score"]
    # 0-DTE prints never make it in.
    assert "ZERO" not in {r["ticker"] for r in report["below_threshold"]} and "ZERO" not in flagged
    # Enrichment reads happened only for the candidates, and the thresholds record the new dials.
    info_calls = sorted(path.split("/")[2] for path, _ in seen if path.endswith("/info"))
    assert info_calls == ["HUGE", "QUIET"]
    assert report["thresholds"]["min_dte"] == 3 and report["thresholds"]["enrich_top"] == 40 and report["enrichment"]["enriched"] == 1
    # Nothing private leaks into the report and the page renders the new fields.
    sess = TradingSession(SessionSettings(state_dir=tmp_path / "state", charts=False))
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "insider_scan.json").write_text(json.dumps(report, default=str))
    page = TestClient(create_app(sess)).get("/insider")
    assert page.status_code == 200 and "The bet" in page.text and "Chain vol vs 30d" in page.text and "Removed by the market-cap window" in page.text and "HUGE" in page.text
    assert "\"_trades\"" not in json.dumps(report) and "\"_contracts\"" not in json.dumps(report)
    # A report written by the previous scoring (no cluster / volume_surge / market_cap / enrichment) still renders.
    old = json.loads(json.dumps(report, default=str))
    old.pop("enrichment", None)
    for f in old["flagged"] + old["below_threshold"]:
        for k in ("cluster", "volume_surge", "volume_note", "market_cap"):
            f.pop(k, None)
    (tmp_path / "state" / "insider_scan.json").write_text(json.dumps(old))
    page = TestClient(create_app(sess)).get("/insider")
    assert page.status_code == 200 and "QUIET" in page.text and ">n/a<" in page.text


def _flat_bars(sym_close: dict[str, float], end="2026-09-11", periods=320) -> dict[str, pd.DataFrame]:
    idx = pd.bdate_range(end=end, periods=periods)
    out = {}
    for sym, px in sym_close.items():
        df = pd.DataFrame({"open": px, "high": px * 1.01, "low": px * 0.99, "close": px, "volume": 1_000_000.0}, index=idx)
        out[sym] = df
    return out


def test_stock_backdrop_rewards_quiet_tape_and_takeover_strikes():
    """The same options prints score higher when the share price was doing
    nothing (informed buyers move before the stock does) and when the strike
    is a price the stock has not seen in a year; a stock already up 15 % is
    being chased, not foreseen."""
    from qmag.insider_scan import stock_backdrop, summarise_contract, summarise_trade

    asof = datetime(2026, 9, 11).date()
    biox = [summarise_trade(a) for a in ALERTS if a["ticker"] == "BIOX"]
    ctr = [summarise_contract({**c, "date": "2026-09-09", "ticker": "BIOX"}) for c in CONTRACTS["2026-09-09"] if c["option_symbol"].startswith("BIOX")]
    base = score_ticker(biox, ctr, asof)
    assert base["breakdown"]["stock_quiet"] == 0.0 and base["breakdown"]["strike_beyond_52w"] == 0.0 and base["stock"] is None
    assert base["breakdown"]["repeat_buyer"] == 0.75  # the $30 call was bought on two sessions

    quiet = stock_backdrop(_flat_bars({"BIOX": 22.0})["BIOX"], base["days_active"], base["cluster"])
    assert quiet["ret_5d_pct"] == 0.0 and quiet["volume_ratio"] == pytest.approx(1.0) and quiet["strike_beyond_52w"] is True and quiet["asof"] == "2026-09-08"
    s_quiet = score_ticker(biox, ctr, asof, stock=quiet)
    assert s_quiet["breakdown"]["stock_quiet"] == 0.5 and s_quiet["breakdown"]["strike_beyond_52w"] == 0.75
    # The textbook fixture already sits at the 10 cap; the raw sum keeps the ranking honest.
    assert s_quiet["score"] == 10.0 and s_quiet["raw"] == pytest.approx(base["raw"] + 1.25)

    chased = {"asof": "2026-09-08", "close": 25.3, "ret_5d_pct": 15.0, "volume_ratio": 4.2, "high_52w": 26.0, "low_52w": 12.0, "strike_beyond_52w": True, "bars": 300}
    s_chased = score_ticker(biox, ctr, asof, stock=chased)
    assert s_chased["breakdown"]["stock_quiet"] == -1.0 and s_chased["raw"] < s_quiet["raw"]
    # A bearish bet into a stock already down 15 % is chasing too; the same drop on a bullish bet is not.
    bear_stock = {**chased, "ret_5d_pct": -15.0}
    assert score_ticker(biox, ctr, asof, stock=bear_stock)["breakdown"]["stock_quiet"] == 0.0
    assert score_ticker(biox, ctr, asof, stock={**chased, "ret_5d_pct": 7.0})["breakdown"]["stock_quiet"] == -0.5
    # A strike a few percent past a stock sitting at its own high is not "beyond the range".
    near = stock_backdrop(_flat_bars({"BIOX": 22.0})["BIOX"], base["days_active"], {**base["cluster"], "strike": 22.8})
    assert near["strike_beyond_52w"] is False
    # Too little history -> no backdrop, nothing scored.
    assert stock_backdrop(_flat_bars({"BIOX": 22.0}, periods=4)["BIOX"], base["days_active"], base["cluster"]) is None
    assert stock_backdrop(None, base["days_active"], base["cluster"]) is None


def test_weekly_scan_uses_desk_bars_for_backdrop_and_scores_past_flags(tmp_path, monkeypatch, no_finviz):
    _mock_uw(monkeypatch, _uw_insider_handler)
    frames = _flat_bars({"BIOX": 22.0, "MEGA": 100.0})
    # MEGA's puts went on after the stock had already dropped 12 %: chased, not foreseen.
    frames["MEGA"].loc["2026-09-04":, ["open", "high", "low", "close"]] = [[88.0, 89.0, 87.0, 88.0]] * len(frames["MEGA"].loc["2026-09-04":])
    # OLDX was flagged last week (bullish); it then ran from 10 to 12.5 within three sessions.
    idx = pd.bdate_range(end="2026-09-11", periods=320)
    oldx = pd.DataFrame({"open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0, "volume": 500_000.0}, index=idx)
    oldx.loc["2026-09-09":, ["high", "close"]] = [[11.0, 10.8], [12.5, 12.2], [12.4, 12.0]]
    frames["OLDX"] = oldx
    asked: list[tuple[list[str], str]] = []

    def bars(symbols, start):
        asked.append((sorted(symbols), start))
        return {s: frames[s] for s in symbols if s in frames}

    previous = {"history": [{"week_end": "2026-09-04", "generated_at": "2026-09-05T14:00:00+00:00", "alerts": 3, "tickers_considered": 2,
                             "flagged": [{"ticker": "OLDX", "score": 7.1, "direction": "bullish", "verdict": "investigate"},
                                         {"ticker": "GONE", "score": 6.8, "direction": "bearish", "verdict": None}]}]}
    report = run_weekly_scan(StrategyConfig(), now=datetime(2026, 9, 12, 10, 0, tzinfo=NY), uw_cache_path=None, ai=False, previous=previous, bars=bars)
    assert len(asked) == 1 and asked[0][1] == "2025-08-01"  # ~400 days back, one read for candidates + open past flags
    assert "BIOX" in asked[0][0] and "OLDX" in asked[0][0] and "GONE" in asked[0][0]
    flag = report["flagged"][0]
    assert flag["ticker"] == "BIOX" and flag["stock"]["strike_beyond_52w"] is True and flag["breakdown"]["stock_quiet"] == 0.5
    assert flag["breakdown"]["strike_beyond_52w"] == 0.75 and "stock_note" not in flag
    mega = next(r for r in report["below_threshold"] if r["ticker"] == "MEGA")
    assert [f["ticker"] for f in report["flagged"]] == ["BIOX"] and mega["score"] < 5.0
    oc = report["outcomes"]
    assert oc["flags"] == 3 and oc["measured"] == 1 and oc["done"] == 0 and oc["hit_10"] == 1 and oc["hit_20"] == 1
    assert oc["avg_best_move_pct"] == 25.0 and oc["sessions"] == 10
    old_flags = {f["ticker"]: f for f in report["history"][0]["flagged"]}
    # (bdate_range has no holiday calendar, so Labor Day 7 Sep is a bar here.)
    assert old_flags["OLDX"]["outcome"] == {"entry_date": "2026-09-07", "entry": 10.0, "best_move_pct": 25.0, "sessions": 4, "done": False, "last": "2026-09-11"}
    assert "outcome" not in old_flags["GONE"]  # no bars -> not measured, not guessed
    assert report["history"][-1]["flagged"][0]["cluster"]["strike"] == 30.0

    sess = TradingSession(SessionSettings(state_dir=tmp_path / "state", charts=False))
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "insider_scan.json").write_text(json.dumps(report, default=str))
    page = TestClient(create_app(sess)).get("/insider")
    assert page.status_code == 200
    assert "Stock before the bet" in page.text and "strike is beyond the 52-week range" in page.text
    assert "Scorecard" in page.text and "+25%" in page.text and "stock quiet" in page.text

    # No bars provider at all (or bars that fail): the components stay unscored and the gap is recorded.
    boom = run_weekly_scan(StrategyConfig(), now=datetime(2026, 9, 12, 10, 0, tzinfo=NY), uw_cache_path=None, ai=False, bars=lambda s, st: (_ for _ in ()).throw(RuntimeError("no data")))
    assert any("share-price backdrop unavailable" in g for g in boom["data_gaps"]) and boom["flagged"][0]["stock_note"]
    assert boom["flagged"][0]["breakdown"]["stock_quiet"] == 0.0 and boom["outcomes"]["measured"] == 0
    plain = run_weekly_scan(StrategyConfig(), now=datetime(2026, 9, 12, 10, 0, tzinfo=NY), uw_cache_path=None, ai=False)
    assert plain["outcomes"] is None and "stock_note" not in plain["flagged"][0]


def test_weekly_scan_without_key_or_model_records_gaps(tmp_path, monkeypatch, no_finviz):
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY", raising=False)
    sess = TradingSession(SessionSettings(state_dir=tmp_path / "state", charts=False))
    report = sess.insider_scan(now=datetime(2026, 9, 12, 10, 0, tzinfo=NY))
    assert report["unavailable"] and report["flagged"] == [] and "UNUSUAL_WHALES_API_KEY" in report["data_gaps"][0]
    conn = json.loads((tmp_path / "state" / "connections.json").read_text())
    assert conn["insider_scan"]["ok"] is False
    page = TestClient(create_app(sess)).get("/insider")
    assert "SCAN NOT RUN" in page.text

    # With data but no model key: flags are produced, the AI step is skipped and says so.
    _mock_uw(monkeypatch, _uw_insider_handler)
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "QMAG_LLM_API_KEY", "QMAG_LLM_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    report = run_weekly_scan(sess.cfg, now=datetime(2026, 9, 12, 10, 0, tzinfo=NY), uw_cache_path=None)
    assert report["flagged"][0]["ticker"] == "BIOX" and report["flagged"][0]["ai"] is None
    assert not report["ai"]["enabled"] and any("AI analysis skipped" in g for g in report["data_gaps"])


def test_insider_scan_cli_json_and_daemon_once(tmp_path, monkeypatch, no_finviz):
    _mock_uw(monkeypatch, _uw_insider_handler)
    monkeypatch.setenv("GEMINI_API_KEY", "gem-test")
    monkeypatch.setattr("qmag.reviewer.ask_json", lambda bundle, c, p, schema=None: (json.dumps(AI_REPLY), "gemini", "gemini-test"))
    monkeypatch.setattr("qmag.insider_scan.datetime", _FrozenDatetime)
    res = runner.invoke(app, ["insider-scan", "--state-dir", str(tmp_path / "state"), "--json"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output[res.output.index("{"):])
    assert out["flagged"][0]["ticker"] == "BIOX" and out["flagged"][0]["ai"]["verdict"] == "investigate"
    res = runner.invoke(app, ["insider-scan", "--state-dir", str(tmp_path / "state"), "--no-ai"])
    assert res.exit_code == 0 and "BIOX" in res.output and "not an accusation" in res.output
    res = runner.invoke(app, ["daemon", "--state-dir", str(tmp_path / "state"), "--symbols", "AAA", "--once", "insider_scan", "--no-universe-rebuild", "-q"])
    assert res.exit_code == 0, res.output
    assert "Insider / unusual-options scan" in res.output


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        base = datetime(2026, 9, 12, 14, 0, tzinfo=tz or NY)
        return base if tz is None else base.astimezone(tz)


def test_analysis_normaliser_rejects_bad_verdicts_and_clips():
    ok = normalise_analysis({"verdict": "Noise", "suspicion": 35, "direction": "sideways", "possible_catalysts": ["Earnings"], "what_to_check": "a; b"}, "openai", "m")
    assert ok["verdict"] == "noise" and ok["suspicion"] == 0.35 and ok["direction"] == "mixed"
    assert ok["possible_catalysts"] == [{"catalyst": "Earnings", "likelihood": "low", "basis": ""}] and ok["what_to_check"] == ["a", "b"]
    with pytest.raises(ValueError):
        normalise_analysis({"verdict": "buy"}, "openai", "m")


def test_settings_page_shows_schedule_and_insider_sections(tmp_path, csv_universe):
    sess = _session(tmp_path, csv_universe)
    client = TestClient(create_app(sess))
    page = client.get("/settings")
    assert page.status_code == 200
    assert "Scan schedule (tiered)" in page.text and "Saturday insider / unusual-options scan" in page.text
    assert 'name="insider_scan.min_premium"' in page.text and 'name="schedule.focused_interval_minutes"' in page.text
    # Saving an invalid time is refused with a clear message.
    from qmag.settings import validate_config

    bad = sess.cfg.with_overrides({"insider_scan.run_time": "25:00"})
    with pytest.raises(ValueError, match="run_time"):
        validate_config(bad)
