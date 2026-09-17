from datetime import date, datetime, time

import pandas as pd
from fastapi.testclient import TestClient

from qmag.backtest import prepare_data
from qmag.charts import ChartLevels, chart_signal, render_chart
from qmag.config import StrategyConfig
from qmag.daemon import NY, Daemon, Task, _after_close, _intraday, is_trading_day, market_close, nyse_holidays
from qmag.dashboard import create_app
from qmag.session import SessionSettings, TradingSession
from qmag.setups import BreakoutDetector
from qmag.universe import parse_listings
from tests.conftest import make_breakout_frame

NASDAQ_TXT = """Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N
QQQ|Invesco QQQ Trust, Series 1|G|N|N|100|Y|N
ZTEST|NASDAQ TEST STOCK|G|Y|N|100|N|N
ABCDW|Some Corp - Warrant|G|N|N|100|N|N
ABCDU|Some Corp - Unit|G|N|N|100|N|N
SMCI|Super Micro Computer, Inc. - Common Stock|Q|N|N|100|N|N
File Creation Time: 0101202500:00|||||||
"""

OTHER_TXT = """ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
NVDA|NVIDIA Corporation Common Stock|N|NVDA|N|100|N|NVDA
SPY|SPDR S&P 500 ETF Trust|P|SPY|Y|100|N|SPY
BRK.B|Berkshire Hathaway Inc. Class B|N|BRK B|N|100|N|BRK/B
GS-PA|Goldman Sachs Depositary Shares Preferred Series A|N|GS pA|N|100|N|GS-A
ASTS|AST SpaceMobile, Inc. Class A Common Stock|Q|ASTS|N|100|N|ASTS
XYZ|Acme Acquisition Corp Rights|N|XYZ|N|100|N|XYZ
File Creation Time: 0101202500:00|||||||
"""


def test_parse_listings_keeps_common_stocks_only():
    listed = parse_listings(NASDAQ_TXT, OTHER_TXT)
    syms = set(listed["symbol"])
    assert {"AAPL", "SMCI", "NVDA", "ASTS"} <= syms
    for bad in ("QQQ", "SPY", "ZTEST", "ABCDW", "ABCDU", "BRK.B", "GS-PA", "XYZ"):
        assert bad not in syms, bad


def test_chart_renders_setup_with_levels(tmp_path):
    cfg = StrategyConfig().with_overrides({"regime.enabled": False, "themes.enabled": False})
    df = prepare_data({"ABC": make_breakout_frame()}, cfg)["ABC"]
    sig = BreakoutDetector().detect_last("ABC", df, cfg)
    assert sig is not None
    out = chart_signal(df, sig, tmp_path, target=sig.entry + 2 * (sig.entry - sig.stop), shares=10, note="test plan")
    assert out.exists() and out.stat().st_size > 20_000
    assert out.name == f"ABC_{pd.Timestamp(sig.date).date()}.png"
    plain = render_chart(df, "ABC", ChartLevels(entry=100.0, stop=95.0, target=110.0, setup="manual"), tmp_path / "plain.png")
    assert plain.exists()


def test_nyse_calendar():
    h = nyse_holidays(2026)
    assert date(2026, 1, 1) in h and date(2026, 1, 19) in h  # New Year, MLK
    assert date(2026, 4, 3) in h  # Good Friday 2026
    assert date(2026, 5, 25) in h and date(2026, 7, 3) in h  # Memorial Day, July 4 observed (Sat -> Fri)
    assert date(2026, 11, 26) in h and date(2026, 12, 25) in h
    assert not is_trading_day(date(2026, 9, 12))  # Saturday
    assert is_trading_day(date(2026, 9, 11))
    assert market_close(date(2026, 11, 27)) == time(13, 0)  # day after Thanksgiving
    assert _after_close(date(2026, 11, 27)) == [time(13, 20)]
    assert _intraday(date(2026, 11, 27))[-1] == time(12, 45)
    assert _intraday(date(2026, 9, 11))[-1] == time(15, 45)
    assert _intraday(date(2026, 9, 13)) == []


def test_task_next_after_skips_weekends_and_holidays():
    task = Task("after_close", _after_close, lambda: None)
    friday_evening = datetime(2026, 9, 11, 17, 0, tzinfo=NY)
    assert task.next_after(friday_evening) == datetime(2026, 9, 14, 16, 20, tzinfo=NY)
    before_labor_day = datetime(2026, 9, 5, 9, 0, tzinfo=NY)  # Saturday; Monday 7th is Labor Day
    assert task.next_after(before_labor_day).date() == date(2026, 9, 8)


def test_session_cycle_writes_report_and_charts(tmp_path, csv_universe):
    sess = TradingSession(
        SessionSettings(**csv_universe.session_kwargs(), state_dir=tmp_path / "state", charts=True, overrides=csv_universe.overrides(**{"regime.enabled": False}))
    )
    report = sess.cycle(asof="2024-05-15", label="test")
    assert report.plans, "expected at least one plan on the generated CSV tape"
    payload = sess.last_report()
    assert payload["label"] == "test" and payload["universe_size"] > 0
    assert set(payload["charts"]) >= {p.symbol for p in report.plans}
    assert (tmp_path / "state" / "trader.json").exists()

    client = TestClient(create_app(sess))
    page = client.get("/desk")
    assert page.status_code == 200
    first = report.plans[0].symbol
    assert first in page.text and "Trade plans" in page.text
    snap = client.get("/api/snapshot").json()
    assert snap["settings"]["broker"] == "paper" and snap["plans"]
    assert client.get(f"/chart/{first}").status_code == 200
    assert client.get("/chart/NOPE").status_code == 404
    assert "Trade journal" in page.text and "Why: entry, size, stop, profit plan" in page.text
    detail = client.get(f"/plan/{first}")
    assert detail.status_code == 200 and "Justification" in detail.text and "Profit taking" in detail.text
    assert client.get("/plan/NOPE").status_code == 404
    # On-demand lookup works without a context layer (switched off for the generated tickers).
    lookup = client.get(f"/symbol/{first}")
    assert lookup.status_code == 200 and "live lookup" in lookup.text
    api = client.get(f"/api/symbol/{first}").json()
    assert api["symbol"] == first and api["chart"].startswith("/charts/") and api["status"] in ("triggered", "watch", "none")

    d = Daemon(sess, rebuild_universe=False)
    # Fri 11 Sep 2026 08:00 .. Mon 14 Sep: the tiered weekday tasks plus the Saturday insider scan.
    names = {name for _, name in d.schedule(datetime(2026, 9, 11, 8, 0, tzinfo=NY), days=3)}
    assert names == {"premarket", "post_open", "focused", "movers", "after_close", "insider_scan", "learn"}
    d.run_task("after_close")
    task = next(t for t in d.tasks if t.name == "after_close")
    assert task.runs == 1 and task.last_error is None
    assert (tmp_path / "state" / "daemon_status.json").exists()


def test_dashboard_and_cli_show_llm_reviewer_verdict(tmp_path, monkeypatch, csv_universe):
    verdict = {
        "action": "HOLD", "confidence": 0.72, "thesis": "Volume is thin for a leader; wait for confirmation.",
        "catalysts": ["Sector rotation"], "risks": ["Thin volume", "Earnings in 12 days"], "invalidation": "Close below the pivot.",
        "sizeNote": "Half size if taken.", "sizeMultiplier": 0.5,
    }
    monkeypatch.setattr("qmag.reviewer.review_trade", lambda bundle, rcfg: dict(verdict))
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    sess = TradingSession(
        SessionSettings(
            **csv_universe.session_kwargs(), state_dir=tmp_path / "state", charts=False,
            overrides=csv_universe.overrides(**{"regime.enabled": False, "reviewer.enabled": True, "reviewer.mode": "gate_and_size", "reviewer.review_watchlist": True}),
        )
    )
    report = sess.cycle(asof="2024-05-15", label="test")
    reviewed = [p for p in report.plans + report.rejected if p.reviewer]
    assert reviewed, "reviewer should have run on the candidates"
    assert all(not p.ok and "llm_reviewer" in p.failed_checks for p in reviewed)  # HOLD blocks in gate mode
    assert any(a.startswith("REVIEW ") and "HOLD 72% [gate_and_size]" in a for a in report.actions)

    client = TestClient(create_app(sess))
    home = client.get("/desk")
    assert home.status_code == 200 and "reviewer · gate + size" in home.text
    sym = reviewed[0].symbol
    detail = client.get(f"/plan/{sym}")
    assert detail.status_code == 200
    assert "LLM reviewer" in detail.text and "HOLD" in detail.text and "Volume is thin" in detail.text
    assert "Earnings in 12 days" in detail.text and "Close below the pivot." in detail.text and "Half size if taken." in detail.text
    # The live lookup runs on the latest generated bar, where a setup may or may not exist;
    # when it does, the reviewer verdict must ride along (review_rejected applies on desk checks).
    api = client.get(f"/api/symbol/{sym}").json()
    if api["plan"] is not None:
        assert api["plan"]["reviewer"]["action"] == "HOLD"
        assert "LLM reviewer" in client.get(f"/symbol/{sym}").text
    else:
        assert client.get(f"/symbol/{sym}").status_code == 200

    from typer.testing import CliRunner

    from qmag.cli import app

    cli = csv_universe.cli_args(symbols=False, **{"regime.enabled": False, "reviewer.enabled": True, "reviewer.mode": "gate_and_size"})
    out = CliRunner().invoke(app, ["review", sym, *cli, "--state-dir", str(tmp_path / "state"), "--mode", "advisory"])
    assert out.exit_code == 0, out.output
    bundle = CliRunner().invoke(app, ["review", sym, *cli, "--state-dir", str(tmp_path / "state"), "--bundle-only"])
    assert bundle.exit_code == 0, bundle.output
    if api["plan"] is not None:
        assert "HOLD" in out.output and "Volume is thin" in out.output and "Half size" in out.output
        assert '"checklist"' in bundle.output and '"rationale"' in bundle.output
    else:
        assert "nothing to review" in out.output


def test_review_cli_on_a_fresh_breakout(tmp_path, monkeypatch):
    """`qmag review` on a hand-built breakout that ends today: bundle, verdict table and gate sizing."""
    import yaml
    from typer.testing import CliRunner

    from qmag.cli import app

    frame = make_breakout_frame()
    frame.index = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=len(frame))
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    frame.rename_axis("date").to_csv(csv_dir / "ABC.csv")
    cfg_path = tmp_path / "qmag.yaml"
    cfg_path.write_text(yaml.safe_dump({"regime": {"enabled": False}, "themes": {"enabled": False}, "context": {"enabled": False}}))
    verdict = {"action": "BUY", "confidence": 0.81, "thesis": "Clean flag, leader, volume confirms.", "catalysts": ["Guidance raise"],
               "risks": ["Market extended"], "invalidation": "Loses the pivot.", "sizeNote": "Take 60%.", "sizeMultiplier": 0.6,
               "provider": "gemini", "model": "gemini-3.5-flash"}
    monkeypatch.setattr("qmag.reviewer.review_trade", lambda bundle, rcfg: dict(verdict))
    common = ["review", "ABC", "--data", "csv", "--csv-dir", str(csv_dir), "--config", str(cfg_path), "--state-dir", str(tmp_path / "state")]

    bundle = CliRunner().invoke(app, common + ["--bundle-only"])
    assert bundle.exit_code == 0, bundle.output
    assert '"symbol": "ABC"' in bundle.output and '"checklist"' in bundle.output and '"rationale"' in bundle.output and '"stop_pct"' in bundle.output

    out = CliRunner().invoke(app, common + ["--mode", "gate_and_size"])
    assert out.exit_code == 0, out.output
    assert "BUY" in out.output and "81%" in out.output and "x0.60" in out.output and "Clean flag" in out.output
    assert "Guidance raise" in out.output and "Loses the pivot." in out.output and "Take 60%." in out.output
    assert "reviewer BUY (81%)" in out.output

    raw = CliRunner().invoke(app, common + ["--json"])
    assert raw.exit_code == 0, raw.output
    assert '"action": "BUY"' in raw.output and '"sizeMultiplier": 0.6' in raw.output
