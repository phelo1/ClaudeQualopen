"""Accounts: the per-desk account snapshot, the multi-desk overview, the
dashboard page / API and the CLI."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from qmag.accounts import (
    ACCOUNT_FILE,
    DESKS_ENV,
    build_snapshot,
    desk_overview,
    halt_desk,
    parse_desks,
    read_desk,
    totals_by_currency,
    write_snapshot,
)
from qmag.broker import Account
from qmag.cli import app
from qmag.dashboard import create_app
from qmag.halt import halt_status
from qmag.session import SessionSettings, TradingSession


def _session(tmp_path, csv_universe, name="state", **extra) -> TradingSession:
    return TradingSession(
        SessionSettings(**csv_universe.session_kwargs(), state_dir=tmp_path / name, charts=False, overrides=csv_universe.overrides(**{"regime.enabled": False, "entry.mode": "resting", **extra}))
    )


def _run_until_positions(sess: TradingSession, start="2024-05-14", periods=8) -> str:
    last = None
    for d in pd.bdate_range(start, periods=periods):
        last = str(d.date())
        sess.cycle(asof=last, label="nightly")
        if sess.state().managed:
            break
    assert sess.state().managed, "the synthetic universe should have produced at least one fill"
    return last


@pytest.fixture(autouse=True)
def _no_desks(monkeypatch):
    monkeypatch.delenv(DESKS_ENV, raising=False)
    monkeypatch.delenv("QMAG_DESK_NAME", raising=False)


# --------------------------------------------------------------------------- #
# Snapshot
# --------------------------------------------------------------------------- #
def test_cycle_writes_account_snapshot_with_marked_holdings(tmp_path, csv_universe):
    sess = _session(tmp_path, csv_universe)
    asof = _run_until_positions(sess)
    path = sess.state_dir / ACCOUNT_FILE
    assert path.exists(), "write_report should leave account.json behind"
    snap = json.loads(path.read_text())
    assert snap["broker"] == "paper" and snap["currency"] == "USD" and snap["error"] is None
    acct = sess.broker.account()
    assert snap["equity"] == pytest.approx(acct.equity, abs=0.01)
    assert snap["cash"] == pytest.approx(acct.cash, abs=0.01)
    held = sess.broker.positions()
    rows = {p["symbol"]: p for p in snap["positions"]}
    assert set(rows) == set(held) | set(sess.state().managed)
    for sym, bp in held.items():
        row = rows[sym]
        assert row["qty"] == bp.qty and row["at_broker"] and row["managed"]
        assert row["last"] is not None and row["mark_asof"] == asof, "marks come from the bars the cycle loaded"
        assert row["market_value"] == pytest.approx(row["qty"] * row["last"], abs=0.01)
        assert row["unrealized"] == pytest.approx(row["qty"] * (row["last"] - row["avg_cost"]), abs=0.05)
        assert row["stop"] is not None and row["setup"]
    assert snap["unrealized_known"] is True
    assert snap["market_value"] == pytest.approx(sum(r["market_value"] for r in rows.values() if r["at_broker"]), abs=0.01)
    # Equity is the broker's number: cash + holdings at the ledger's marks.
    assert snap["equity"] == pytest.approx(snap["cash"] + snap["market_value"], rel=0.01)


def test_snapshot_flags_unmanaged_holdings_and_never_estimates_a_mark(tmp_path, csv_universe):
    sess = _session(tmp_path, csv_universe)
    sess.cycle(asof="2024-05-14", label="nightly")
    # A share bought outside qmag (or before it): the broker reports it, the trader does not manage it.
    sess.broker.mark({"ZZZZ": pd.Series({"close": 10.0, "open": 10.0, "high": 10.0, "low": 10.0, "volume": 1})})
    sess.broker.market_buy("ZZZZ", 5, tag="outside")
    snap = build_snapshot(sess, frames={}, fetch_missing=False)
    row = next(p for p in snap["positions"] if p["symbol"] == "ZZZZ")
    assert row["at_broker"] and not row["managed"] and "outside qmag" in row["note"]
    assert row["last"] is None and row["market_value"] is None and row["unrealized"] is None
    assert row["cost_basis"] == pytest.approx(5 * row["avg_cost"], abs=0.01)
    assert snap["unrealized_known"] is False
    assert any("ZZZZ" in g for g in snap["data_gaps"])
    # Realised figures come from the journal, not the broker.
    assert snap["realized_total"] == 0.0 and snap["trades"] == 0


def test_snapshot_records_broker_failure_instead_of_carrying_old_numbers(tmp_path, csv_universe, monkeypatch):
    sess = _session(tmp_path, csv_universe)
    sess.cycle(asof="2024-05-14", label="nightly")
    first = json.loads((sess.state_dir / ACCOUNT_FILE).read_text())
    assert first["equity"] is not None

    def boom():
        raise ConnectionError("gateway down")

    monkeypatch.setattr(type(sess.broker), "account", lambda self: boom())
    snap = write_snapshot(sess, frames={}, fetch_missing=False)
    assert snap["equity"] is None and snap["cash"] is None and snap["positions"] == []
    assert "gateway down" in snap["error"]
    desk = read_desk({"name": "x", "state_dir": str(sess.state_dir), "local": True})
    assert desk["state"] == "error" and "gateway down" in desk["problem"]


def test_snapshot_day_and_realised_pnl(tmp_path, csv_universe):
    sess = _session(tmp_path, csv_universe)
    _run_until_positions(sess)
    state = sess.state()
    now = datetime.now(timezone.utc)
    today = now.astimezone(__import__("qmag.market_calendar", fromlist=["NY"]).NY).date()
    already = sum(float(r.get("pnl") or 0.0) for r in state.closed)
    n_already = len(state.closed)
    state.day_equity = {"date": str(today), "equity": 90_000.0}
    state.closed.append({"symbol": "OLD", "pnl": -250.0, "closed_on": "2020-01-02", "r_multiple": -1.0})
    state.closed.append({"symbol": "NEW", "pnl": 400.0, "closed_on": str(today), "r_multiple": 1.6})
    state.save(sess.state_path)
    snap = build_snapshot(sess, frames={}, fetch_missing=False, now=now)
    assert snap["day_open_equity"] == 90_000.0
    assert snap["day_pnl"] == pytest.approx(snap["equity"] - 90_000.0, abs=0.01)
    assert snap["realized_total"] == pytest.approx(already + 150.0, abs=0.01)
    assert snap["realized_today"] == pytest.approx(400.0)
    assert snap["trades"] == n_already + 2
    # A day_equity from another session is not "today".
    state.day_equity = {"date": "2024-05-14", "equity": 90_000.0}
    state.save(sess.state_path)
    snap = build_snapshot(sess, frames={}, fetch_missing=False, now=now)
    assert snap["day_open_equity"] is None and snap["day_pnl"] is None


def test_snapshot_keeps_base_currency(tmp_path, csv_universe, monkeypatch):
    sess = _session(tmp_path, csv_universe)
    sess.cycle(asof="2024-05-14", label="nightly")
    monkeypatch.setattr(type(sess.broker), "account", lambda self: Account(equity=1_000_000.0, cash=1_000_000.0, currency="EUR"))
    snap = build_snapshot(sess, frames={}, fetch_missing=False)
    assert snap["currency"] == "EUR" and snap["equity"] == 1_000_000.0


# --------------------------------------------------------------------------- #
# Several desks
# --------------------------------------------------------------------------- #
def test_parse_desks_formats(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    desks = parse_desks(f"alpaca={a}|http://127.0.0.1:8856, {b};  ibkr2={a}\n")
    assert [d["name"] for d in desks] == ["alpaca", "b"], "a duplicate path is listed once, a bare path gets its directory name"
    assert desks[0]["url"] == "http://127.0.0.1:8856" and desks[1]["url"] is None
    assert parse_desks(None) == [] and parse_desks("  ,; ") == []


def test_overview_sums_per_currency_and_flags_stale_and_missing_desks(tmp_path, csv_universe, monkeypatch):
    local = _session(tmp_path, csv_universe, name="local")
    local.cycle(asof="2024-05-14", label="nightly")
    other = _session(tmp_path, csv_universe, name="other")
    other.cycle(asof="2024-05-14", label="nightly")
    eur = _session(tmp_path, csv_universe, name="eur")
    eur.cycle(asof="2024-05-14", label="nightly")
    monkeypatch.setattr(type(eur.broker), "account", lambda self: Account(equity=1_000_000.0, cash=999_000.0, currency="EUR"))
    write_snapshot(eur, frames={}, fetch_missing=False)
    # A desk that stopped cycling: its snapshot is old.
    stale_dir = tmp_path / "stale"
    stale_dir.mkdir()
    old = json.loads((other.state_dir / ACCOUNT_FILE).read_text())
    old["asof"] = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(timespec="seconds")
    (stale_dir / ACCOUNT_FILE).write_text(json.dumps(old))
    monkeypatch.setenv(DESKS_ENV, f"other={other.state_dir}|http://127.0.0.1:8856,eur={eur.state_dir},stale={stale_dir},ghost={tmp_path / 'ghost'},me={local.state_dir}")

    ov = desk_overview(local)
    names = [d["name"] for d in ov["desks"]]
    assert names == ["local", "other", "eur", "stale", "ghost"], "the local desk comes first and is never listed twice"
    states = {d["name"]: d["state"] for d in ov["desks"]}
    assert states == {"local": "ok", "other": "ok", "eur": "ok", "stale": "stale", "ghost": "missing"}
    assert ov["desks"][1]["url"] == "http://127.0.0.1:8856"
    assert not ov["desks"][0]["daemon"]["running"], "no daemon wrote a heartbeat in tests"
    usd = next(t for t in ov["totals"] if t["currency"] == "USD")
    assert usd["accounts"] == 2 and set(usd["desks"]) == {"local", "other"}, "the stale desk is excluded from the totals"
    l_snap, o_snap = ov["desks"][0]["snapshot"], ov["desks"][1]["snapshot"]
    assert usd["equity"] == pytest.approx(l_snap["equity"] + o_snap["equity"], abs=0.01)
    assert usd["cash"] == pytest.approx(l_snap["cash"] + o_snap["cash"], abs=0.01)
    eur_t = next(t for t in ov["totals"] if t["currency"] == "EUR")
    assert eur_t["equity"] == 1_000_000.0 and ov["totals"][0]["currency"] == "USD"
    assert any("stale" in p and "h old" in p for p in ov["problems"])
    assert any("ghost" in p and "does not exist" in p for p in ov["problems"])
    assert ov["configured"] is True


def test_totals_skip_desks_without_equity():
    rows = [
        {"name": "a", "snapshot": {"currency": "USD", "equity": 100.0, "cash": 50.0, "market_value": 50.0, "unrealized": 5.0, "unrealized_known": True, "day_pnl": None, "realized_today": 0.0, "realized_total": 1.0, "positions": [{}], "open_orders": 1}, "stale": False},
        {"name": "b", "snapshot": {"currency": "USD", "equity": None, "error": "down"}, "stale": False},
        {"name": "c", "snapshot": None},
    ]
    t = totals_by_currency(rows)
    assert len(t) == 1 and t[0]["accounts"] == 1 and t[0]["equity"] == 100.0 and t[0]["day_pnl"] is None and t[0]["positions"] == 1


def test_halt_another_desk_writes_its_halt_file(tmp_path, csv_universe, monkeypatch):
    local = _session(tmp_path, csv_universe, name="local")
    other = _session(tmp_path, csv_universe, name="other")
    other.cycle(asof="2024-05-14", label="nightly")
    monkeypatch.setenv(DESKS_ENV, f"other={other.state_dir}")
    res = halt_desk(local, "other", True, reason="rebalancing")
    assert res["on"] and "other" in res["message"]
    assert halt_status(other.state_dir)["reason"] == "rebalancing" and halt_status(local.state_dir) is None
    assert other.halted(), "the other desk's session sees the switch on its next pass"
    res = halt_desk(local, "other", False)
    assert not res["on"] and halt_status(other.state_dir) is None
    with pytest.raises(KeyError):
        halt_desk(local, "nobody", True)
    monkeypatch.setenv(DESKS_ENV, f"ghost={tmp_path / 'ghost'}")
    with pytest.raises(FileNotFoundError):
        halt_desk(local, "ghost", True)
    # The local desk goes through the session (alerts, health record).
    res = halt_desk(local, "local", True, reason="local reason")
    assert local.halted() and "local reason" in res["message"]


# --------------------------------------------------------------------------- #
# Dashboard + CLI
# --------------------------------------------------------------------------- #
def test_accounts_page_and_api(tmp_path, csv_universe, monkeypatch):
    sess = _session(tmp_path, csv_universe)
    _run_until_positions(sess)
    other = _session(tmp_path, csv_universe, name="other")
    other.cycle(asof="2024-05-14", label="nightly")
    monkeypatch.setenv(DESKS_ENV, f"other={other.state_dir}|http://127.0.0.1:8856")
    client = TestClient(create_app(sess))

    page = client.get("/accounts")
    assert page.status_code == 200
    html = page.text
    assert "this desk" in html and "other" in html and "http://127.0.0.1:8856" in html
    for sym in sess.broker.positions():
        assert sym in html
    assert "Halt other" in html, "another desk's kill switch is offered from the accounts page"
    assert 'href="/accounts"' in client.get("/desk").text, "the desk page links to the accounts page"

    api = client.get("/api/accounts").json()
    assert [d["name"] for d in api["desks"]] == ["state", "other"]
    assert api["totals"][0]["currency"] == "USD" and api["totals"][0]["accounts"] == 2
    assert api["run"]["running"] is False

    # Refresh rebuilds the local snapshot from the broker.
    (sess.state_dir / ACCOUNT_FILE).unlink()
    assert client.post("/api/accounts/refresh").json()["started"] is True
    for _ in range(50):
        if not client.get("/api/accounts").json()["run"]["running"] and (sess.state_dir / ACCOUNT_FILE).exists():
            break
        time.sleep(0.1)
    assert (sess.state_dir / ACCOUNT_FILE).exists()

    # Halt / resume the other desk through the API.
    r = client.post("/api/accounts/other/halt", json={"on": True, "reason": "from the api"})
    assert r.status_code == 200 and halt_status(other.state_dir)["reason"] == "from the api"
    assert "Resume other" in client.get("/accounts").text
    assert client.post("/api/accounts/other/halt", json={"on": False}).status_code == 200
    assert halt_status(other.state_dir) is None
    assert client.post("/api/accounts/nobody/halt", json={"on": True}).status_code == 404


def test_index_shows_marks_and_open_pnl_from_the_snapshot(tmp_path, csv_universe):
    sess = _session(tmp_path, csv_universe)
    _run_until_positions(sess)
    client = TestClient(create_app(sess))
    snap = client.get("/api/snapshot").json()
    assert snap["account"]["equity"] is not None
    pos = snap["positions"][0]
    assert pos["last"] is not None and pos["unrealized"] is not None
    html = client.get("/desk").text
    assert "Open P&amp;L" in html and "Cash" in html


def test_accounts_cli(tmp_path, csv_universe, monkeypatch):
    sess = _session(tmp_path, csv_universe)
    _run_until_positions(sess)
    other = _session(tmp_path, csv_universe, name="other")
    other.cycle(asof="2024-05-14", label="nightly")
    monkeypatch.setenv(DESKS_ENV, f"other={other.state_dir}")
    runner = CliRunner()
    res = runner.invoke(app, ["accounts", *csv_universe.cli_args(symbols=False), "--state-dir", str(sess.state_dir), "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert [d["name"] for d in data["desks"]] == ["state", "other"]
    res = runner.invoke(app, ["accounts", *csv_universe.cli_args(symbols=False), "--state-dir", str(sess.state_dir), "--refresh"])
    assert res.exit_code == 0, res.output
    assert "USD" in res.output and "other" in res.output and "holdings" in res.output
