"""Dashboard password protection: open when unset, locked down when set."""

from __future__ import annotations

import time
import os

import pytest
from fastapi.testclient import TestClient

from qmag.auth import COOKIE, PASSWORD_ENV, DashboardAuth, password_problem
from qmag.dashboard import create_app
from qmag.session import SessionSettings, TradingSession

PW = "correct-horse-battery"


def _session(tmp_path, csv_universe) -> TradingSession:
    return TradingSession(
        SessionSettings(**csv_universe.session_kwargs(), state_dir=tmp_path / "state", charts=False, overrides=csv_universe.overrides(**{"regime.enabled": False}))
    )


def _client(tmp_path, csv_universe) -> TestClient:
    return TestClient(create_app(_session(tmp_path, csv_universe)), follow_redirects=False)


# --------------------------------------------------------------------------- #
def test_tokens_are_signed_and_expire(tmp_path, monkeypatch):
    monkeypatch.setenv(PASSWORD_ENV, PW)
    auth = DashboardAuth(tmp_path)
    tok = auth.issue_token(now=1_000_000)
    assert auth.token_valid(tok, now=1_000_000 + 60)
    assert not auth.token_valid(tok, now=1_000_000 + 8 * 24 * 3600), "expired"
    payload, _, sig = tok.partition(".")
    assert not auth.token_valid(f"{payload}.{sig[:-2]}xx"), "tampered signature"
    assert not auth.token_valid(f"{int(payload) + 99999}.{sig}"), "tampered expiry"
    assert not auth.token_valid(None) and not auth.token_valid("garbage")
    # the secret persists on disk, so a restart keeps sessions valid
    assert DashboardAuth(tmp_path).token_valid(tok, now=1_000_000 + 60)
    assert (os.name == "nt" or (tmp_path / "dashboard_secret").stat().st_mode & 0o777 == 0o600)
    # changing the password signs everyone out
    monkeypatch.setenv(PASSWORD_ENV, PW + "2")
    assert not auth.token_valid(tok, now=1_000_000 + 60)


def test_lockout_after_repeated_failures(tmp_path, monkeypatch):
    monkeypatch.setenv(PASSWORD_ENV, PW)
    auth = DashboardAuth(tmp_path)
    t0 = 5_000_000.0
    for i in range(5):
        ok, msg = auth.check_password("nope", "1.2.3.4", now=t0 + i)
        assert not ok and "Wrong password" in msg
    ok, msg = auth.check_password(PW, "1.2.3.4", now=t0 + 10)
    assert not ok and "Too many attempts" in msg, "even the right password is refused while locked"
    ok, _ = auth.check_password(PW, "5.6.7.8", now=t0 + 10)
    assert ok, "lockout is per client"
    ok, _ = auth.check_password(PW, "1.2.3.4", now=t0 + 16 * 60)
    assert ok, "lockout expires"
    assert auth.locked_for("1.2.3.4", now=t0 + 16 * 60) == 0


def test_password_rules():
    assert password_problem("short") is not None
    assert password_problem("long enough") is None


# --------------------------------------------------------------------------- #
def test_dashboard_is_open_without_a_password(tmp_path, csv_universe, monkeypatch):
    monkeypatch.setenv(PASSWORD_ENV, "")  # recorded so the value the settings page exports is undone at teardown
    client = _client(tmp_path, csv_universe)
    assert client.get("/").status_code == 200
    assert client.get("/api/snapshot").status_code == 200
    r = client.get("/login")
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "Sign out" not in client.get("/").text


def test_dashboard_requires_login_when_password_is_set(tmp_path, csv_universe, monkeypatch):
    monkeypatch.setenv(PASSWORD_ENV, PW)
    client = _client(tmp_path, csv_universe)

    r = client.get("/status?x=1")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Fstatus%3Fx%3D1"
    assert client.get("/api/snapshot").status_code == 401
    assert client.post("/api/run").status_code == 401
    assert client.get("/charts/nothing.png").status_code == 401 or client.get("/charts/nothing.png").status_code == 303
    assert client.get("/healthz").status_code == 200

    page = client.get("/login?next=/status")
    assert page.status_code == 200 and 'name="password"' in page.text

    bad = client.post("/login", data={"password": "wrong", "next": "/status"})
    assert bad.status_code == 401 and "Wrong password" in bad.text and COOKIE not in bad.cookies

    good = client.post("/login", data={"password": PW, "next": "/status"})
    assert good.status_code == 303 and good.headers["location"] == "/status"
    cookie = good.headers["set-cookie"]
    assert COOKIE in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie and "Secure" not in cookie

    assert client.get("/status").status_code == 200
    assert client.get("/api/snapshot").status_code == 200
    assert "Sign out" in client.get("/").text

    out = client.get("/logout")
    assert out.status_code == 303
    client.cookies.clear()
    assert client.get("/").status_code == 303

    # open redirects are not honoured
    evil = client.post("/login", data={"password": PW, "next": "//evil.example/x"})
    assert evil.headers["location"] == "/"


def test_bearer_header_and_secure_cookie_behind_https_proxy(tmp_path, csv_universe, monkeypatch):
    monkeypatch.setenv(PASSWORD_ENV, PW)
    client = _client(tmp_path, csv_universe)
    assert client.get("/api/snapshot", headers={"Authorization": f"Bearer {PW}"}).status_code == 200
    assert client.get("/api/snapshot", headers={"Authorization": "Bearer nope"}).status_code == 401
    r = client.post("https://testserver/login", data={"password": PW, "next": "/"})
    assert r.status_code == 303 and "Secure" in r.headers["set-cookie"]


def test_short_password_is_refused_on_the_settings_page(tmp_path, csv_universe, monkeypatch):
    monkeypatch.setenv(PASSWORD_ENV, "")  # recorded so the value the settings page exports is undone at teardown
    client = _client(tmp_path, csv_universe)
    r = client.post("/settings/connections", data={PASSWORD_ENV: "tiny"})
    assert r.status_code == 303 and ("not+saved" in r.headers["location"] or "not%20saved" in r.headers["location"])
    assert client.get("/").status_code == 200, "still open: nothing was saved"

    r = client.post("/settings/connections", data={PASSWORD_ENV: PW})
    assert r.status_code == 303 and COOKIE in r.headers.get("set-cookie", ""), "the browser that set the password stays signed in"
    assert client.get("/").status_code == 200
    fresh = TestClient(client.app, follow_redirects=False)
    assert fresh.get("/").status_code == 303, "everyone else must sign in"
    assert fresh.post("/login", data={"password": PW, "next": "/"}).status_code == 303


def test_settings_page_lists_the_remote_access_field(tmp_path, csv_universe, monkeypatch):
    monkeypatch.setenv(PASSWORD_ENV, "")  # recorded so the value the settings page exports is undone at teardown
    client = _client(tmp_path, csv_universe)
    html = client.get("/settings").text
    assert "Remote access" in html and PASSWORD_ENV in html
