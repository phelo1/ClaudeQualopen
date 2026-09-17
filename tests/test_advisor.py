"""Desk advisor: plain English -> validated proposals -> applied only on accept."""

from __future__ import annotations

import json

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from qmag import advisor
from qmag.cli import app
from qmag.config import StrategyConfig
from qmag.dashboard import create_app
from qmag.session import SessionSettings, TradingSession


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "QMAG_LLM_API_KEY", "QMAG_LLM_BASE_URL"):
        monkeypatch.delenv(k, raising=False)


def _session(tmp_path, **overrides) -> TradingSession:
    ov = {"context.enabled": False, "regime.enabled": False, **overrides}
    return TradingSession(SessionSettings(data="yfinance", state_dir=tmp_path / "state", charts=False, overrides=ov))


REPLY = {
    "reply": "You are risking 0.5 % per trade with up to 8 positions. Halving the risk per trade and capping positions at 5 cuts the worst-case heat roughly in half while you build a track record.",
    "changes": [
        {"key": "risk.risk_per_trade_pct", "value": "0.0025", "why": "half the current 0.5 % until twenty trades are closed"},
        {"key": "risk.max_positions", "value": "5", "why": "fewer simultaneous bets while sizing down"},
        {"key": "regime.enabled", "value": "true", "why": "trade only when the index is above its moving average"},  # pinned by the CLI here
        {"key": "reviewer.mode", "value": "strict", "why": "not a real choice - must be rejected"},
        {"key": "risk.max_position_pct", "value": "0.25", "why": "already the current value"},
        {"key": "advisor.model", "value": "gpt-5.6", "why": "the advisor may not reconfigure itself"},
        {"key": "made.up", "value": "1", "why": "unknown parameter"},
    ],
    "questions": ["Do you also want the daily loss limit lowered?"],
    "risk_note": "Roughly halves the maximum portfolio heat.",
}


def test_settings_catalogue_covers_every_parameter_with_meaning_and_limits():
    cfg = StrategyConfig()
    cat = advisor.settings_catalogue(cfg, pinned={"regime.enabled": False}, learned={"edge.threshold": 0.15})
    keys = {c["key"] for c in cat}
    assert {"risk.risk_per_trade_pct", "management.trail_ma", "edge.weights.flow", "insider_scan.min_dte", "reviewer.mode"} <= keys
    assert not any(k.startswith("advisor.") for k in keys)  # it does not reconfigure itself
    by = {c["key"]: c for c in cat}
    assert by["risk.risk_per_trade_pct"]["range"].startswith("0 < x <= 0.1") and by["risk.risk_per_trade_pct"]["help"]
    assert by["reviewer.mode"]["choices"] == ["advisory", "gate", "gate_and_size"]
    assert "pinned" in by["regime.enabled"] and "learned" in by["edge.threshold"]
    assert all(c["help"] for c in cat if not c["key"].startswith("edge.weights.")), [c["key"] for c in cat if not c["help"]]
    json.dumps(cat, default=str)


def test_validate_change_enforces_types_ranges_choices_and_pins():
    cfg = StrategyConfig()
    ok = advisor.validate_change(cfg, "risk.risk_per_trade_pct", "0.0025", {})
    assert ok["valid"] and ok["value"] == 0.0025 and ok["current"] == 0.005 and not ok["no_op"]
    assert advisor.validate_change(cfg, "risk.max_positions", 5, {})["value"] == 5
    assert advisor.validate_change(cfg, "regime.enabled", "false", {})["value"] is False
    assert advisor.validate_change(cfg, "insider_scan.exclude_tickers", "SPY, QQQ", {})["value"] == ["SPY", "QQQ"]
    assert "between 0 and 0.1" in advisor.validate_change(cfg, "risk.risk_per_trade_pct", "0.5", {})["problem"]
    assert "must be one of" in advisor.validate_change(cfg, "reviewer.mode", "strict", {})["problem"]
    assert "not a number" in advisor.validate_change(cfg, "risk.max_positions", "many", {})["problem"]
    assert "unknown parameter" in advisor.validate_change(cfg, "made.up", "1", {})["problem"]
    assert "pinned" in advisor.validate_change(cfg, "regime.enabled", "true", {"regime.enabled": False})["problem"]
    assert "own settings" in advisor.validate_change(cfg, "advisor.model", "x", {})["problem"]
    assert advisor.validate_change(cfg, "risk.max_position_pct", "0.25", {})["no_op"] is True


def test_ask_validates_proposals_and_apply_writes_only_accepted_ones(tmp_path, monkeypatch):
    sess = _session(tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "gem-test")
    seen = {}

    def fake_ask(bundle, cfg_like, system_prompt, schema=None):
        seen.update(bundle=bundle, prompt=system_prompt, schema=schema)
        return json.dumps(REPLY), "gemini", "gemini-test"

    monkeypatch.setattr("qmag.reviewer.ask_json", fake_ask)
    ex = advisor.ask_advisor(sess, "I want to risk half as much per trade until I have twenty trades")
    assert ex["error"] is None and ex["provider"] == "gemini" and "Halving" in ex["reply"]
    # The model saw the map and the desk, and was told it never applies anything.
    assert seen["bundle"]["operator_message"].startswith("I want to risk half")
    assert any(c["key"] == "risk.risk_per_trade_pct" and c["value"] == 0.005 for c in seen["bundle"]["settings_map"])
    assert seen["bundle"]["desk"]["desk"]["broker"] == "paper" and "regime.enabled" in seen["bundle"]["desk"]["cli_overrides"]
    assert "never do" in seen["prompt"] and seen["schema"]["required"] == ["reply", "changes", "questions", "risk_note"]
    by = {c["key"]: c for c in ex["changes"]}
    assert by["risk.risk_per_trade_pct"]["status"] == "proposed" and by["risk.max_positions"]["status"] == "proposed"
    assert by["regime.enabled"]["status"] == "rejected" and "pinned" in by["regime.enabled"]["problem"]
    assert by["reviewer.mode"]["status"] == "rejected" and by["made.up"]["status"] == "rejected" and by["advisor.model"]["status"] == "rejected"
    assert by["risk.max_position_pct"]["status"] == "no_change"
    assert ex["questions"] == ["Do you also want the daily loss limit lowered?"]
    # Nothing has changed yet.
    assert sess.cfg.risk.risk_per_trade_pct == 0.005 and not sess.store.yaml_path.exists()
    pending = advisor.pending_changes(advisor.load_transcript(sess))
    assert [c["key"] for c in pending] == ["risk.risk_per_trade_pct", "risk.max_positions"]
    conn = json.loads((tmp_path / "state" / "connections.json").read_text())
    assert conn["llm_advisor"]["ok"] and conn["llm_advisor"]["items"] == 2

    # Accept one of the two: only that one is written.
    res = advisor.apply_changes(sess, [by["risk.risk_per_trade_pct"]["id"]], by="test")
    assert res["written"] and [a["key"] for a in res["applied"]] == ["risk.risk_per_trade_pct"] and res["applied"][0]["was"] == 0.005
    saved = yaml.safe_load(sess.store.yaml_path.read_text())
    assert saved["risk"]["risk_per_trade_pct"] == 0.0025 and saved["risk"]["max_positions"] == 8
    assert sess.cfg.risk.risk_per_trade_pct == 0.0025  # in use at once
    t = advisor.load_transcript(sess)
    statuses = {c["key"]: c["status"] for c in t["exchanges"][0]["changes"]}
    assert statuses["risk.risk_per_trade_pct"] == "applied" and statuses["risk.max_positions"] == "proposed"
    # Applying twice is a no-op with a reason; dismissing removes the rest.
    again = advisor.apply_changes(sess, [by["risk.risk_per_trade_pct"]["id"]])
    assert not again["written"] and again["skipped"][0]["reason"] == "already applied"
    assert advisor.dismiss_changes(sess, [by["risk.max_positions"]["id"]]) == 1
    assert advisor.pending_changes(advisor.load_transcript(sess)) == []
    conn = json.loads((tmp_path / "state" / "connections.json").read_text())
    assert conn["advisor_apply"]["ok"] and "risk.risk_per_trade_pct=0.0025" in conn["advisor_apply"]["detail"]


def test_ask_never_raises_and_records_the_failure(tmp_path, monkeypatch):
    sess = _session(tmp_path)
    ex = advisor.ask_advisor(sess, "hello")  # no key at all
    assert ex["error"] and "no API key saved" in ex["error"] and ex["changes"] == []
    monkeypatch.setenv("GEMINI_API_KEY", "gem-test-secret-value-1234567890")
    monkeypatch.setattr("qmag.reviewer.ask_json", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom ?key=gem-test-secret-value-1234567890")))
    ex = advisor.ask_advisor(sess, "hello again")
    assert ex["error"].startswith("RuntimeError") and "gem-test-secret" not in ex["error"]
    monkeypatch.setattr("qmag.reviewer.ask_json", lambda *a, **k: ("not json at all", "gemini", "m"))
    ex = advisor.ask_advisor(sess, "and again")
    assert ex["error"].startswith("ValueError")
    conn = json.loads((tmp_path / "state" / "connections.json").read_text())
    assert conn["llm_advisor"]["ok"] is False
    assert len(advisor.load_transcript(sess)["exchanges"]) == 3


def test_advisor_page_api_and_cli(tmp_path, monkeypatch):
    sess = _session(tmp_path)
    client = TestClient(create_app(sess))
    page = client.get("/advisor")
    assert page.status_code == 200 and "Desk advisor" in page.text and "No model key" in page.text and "Nothing waiting" in page.text
    assert 'href="/advisor"' in client.get("/").text
    r = client.post("/api/advisor/ask", json={"message": "   "})
    assert r.status_code == 400

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("qmag.reviewer.ask_json", lambda bundle, c, p, schema=None: (json.dumps(REPLY), "openai", "gpt-test"))
    r = client.post("/api/advisor/ask", json={"message": "halve my risk"})
    assert r.status_code == 200
    ex = r.json()["exchange"]
    assert ex["provider"] == "openai" and len(r.json()["pending"]) == 2
    page = client.get("/advisor").text
    assert "Halving the risk" in page and "risk.risk_per_trade_pct" in page and "awaiting your decision" in page and "not applicable" in page and "Do you also want" in page
    ids = [c["id"] for c in r.json()["pending"]]
    r = client.post("/api/advisor/apply", json={"ids": ids})
    assert r.status_code == 200 and len(r.json()["applied"]) == 2 and r.json()["pending"] == []
    assert sess.cfg.risk.max_positions == 5 and sess.cfg.risk.risk_per_trade_pct == 0.0025
    assert "applied" in client.get("/advisor").text
    assert client.get("/api/advisor").json()["pending"] == []
    assert client.post("/api/advisor/clear").json()["cleared"] is True
    assert "No conversation yet" in client.get("/advisor").text

    # CLI: ask, then apply all.
    runner = CliRunner()
    res = runner.invoke(app, ["advise", "--state-dir", str(tmp_path / "state"), "be stricter", "--json"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["exchange"]["error"] is None and all(c["status"] != "proposed" or c["key"] not in ("risk.max_positions", "risk.risk_per_trade_pct") for c in out["exchange"]["changes"])  # both already applied -> no_change
    res = runner.invoke(app, ["advise", "--state-dir", str(tmp_path / "state"), "--apply", "all"])
    assert res.exit_code == 0, res.output
    assert "No pending proposals" in res.output or "applied" in res.output


def test_settings_page_lists_the_advisor_model(tmp_path):
    sess = _session(tmp_path)
    page = TestClient(create_app(sess)).get("/settings").text
    assert "Desk advisor" in page and 'name="advisor.model"' in page and 'name="advisor.provider"' in page
    api = TestClient(create_app(sess)).get("/api/settings").json()
    assert api["strategy"]["advisor"]["enabled"] is True
