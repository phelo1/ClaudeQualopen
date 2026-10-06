"""Additive LLM reviewer: edge bundle, transports (mocked), verdict parsing, and how the trader uses it."""

from __future__ import annotations

import json

import pytest

from qmag.backtest import prepare_data
from qmag.config import ReviewerSettings, StrategyConfig
from qmag.context.base import ContextReport, Headline
from qmag.plan import apply_reviewer, build_plan
from qmag import reviewer
from qmag.setups import BreakoutDetector
from tests.conftest import make_breakout_frame


@pytest.fixture
def breakout():
    cfg = StrategyConfig().with_overrides({"regime.enabled": False, "themes.enabled": False, "edge.enabled": False})
    df = prepare_data({"ABC": make_breakout_frame()}, cfg)["ABC"]
    sig = BreakoutDetector().detect_last("ABC", df, cfg)
    assert sig is not None
    return cfg, df, sig


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "QMAG_LLM_API_KEY", "QMAG_LLM_BASE_URL"):
        monkeypatch.delenv(k, raising=False)


class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


GOOD = {
    "action": "BUY",
    "confidence": 0.78,
    "thesis": "Tight flag on a leader with a real catalyst; volume confirms.",
    "catalysts": ["Raised guidance", "Sector in play"],
    "risks": ["Crowded on StockTwits", "Earnings in 5 weeks"],
    "invalidation": "Close back inside the flag below the pivot.",
    "sizeNote": "Planned size is fine.",
    "sizeMultiplier": 1.0,
}


# --------------------------------------------------------------------------- #
# bundle
# --------------------------------------------------------------------------- #
def test_edge_bundle_carries_the_whole_edge(breakout):
    cfg, df, sig = breakout
    ctx = ContextReport(symbol="ABC", asof="2026-09-11")
    ctx.news_score, ctx.news_count = 0.6, 4
    ctx.headlines = [Headline(title="Acme raises guidance", source="finviz", when="2026-09-10T12:00:00+00:00", score=0.7, tags=["guidance"])]
    ctx.catalysts = ["guidance"]
    ctx.social_score, ctx.social_messages = 0.4, 30
    ctx.float_shares, ctx.short_float_pct, ctx.industry = 20e6, 18.0, "Software"
    ctx.earnings_date, ctx.days_to_earnings = "2026-10-20", 39
    plan = build_plan(sig, df.iloc[-1], cfg, 100_000, 0.0, 100_000, True, context=ctx, regime_note="QQQ > 20d MA")
    plan.committee = {"verdict": "take", "confidence": 0.7, "bull_case": "b", "bear_case": "r", "risk_review": "x", "size_multiplier": 1.0}
    portfolio = {"equity": 100_000, "open_positions": 2, "recent_r_multiples": [1.2, -1.0]}
    b = reviewer.build_edge_bundle(plan.to_dict(), True, "QQQ > 20d MA", portfolio, entry_mode="buy-stop at pivot")

    assert b["symbol"] == "ABC" and b["setup"]["type"] == "breakout" and b["setup"]["pivot"] == pytest.approx(plan.pivot, abs=1e-4)
    assert b["plan"]["entry"] == pytest.approx(plan.entry, abs=1e-4) and b["plan"]["stop"] == pytest.approx(plan.stop, abs=1e-4)
    assert b["plan"]["shares"] == plan.shares and b["setup"]["flag_days"] == 21
    assert b["plan"]["entry_mode"] == "buy-stop at pivot" and 0 < b["plan"]["stop_pct"] < 15
    assert b["checklist"]["size_positive"] is True and b["failed_checks"] == []
    assert set(b["rationale"]) >= {"entry", "size", "stop", "profit_plan"}
    assert b["news"]["headlines"][0]["title"] == "Acme raises guidance" and b["news"]["catalyst_tags"] == ["guidance"]
    assert b["events"]["next_earnings"] == "2026-10-20" and b["events"]["days_to_earnings"] == 39
    assert b["social"]["messages"] == 30 and b["fundamentals"]["short_float_pct"] == 18.0
    assert b["committee_debate"]["bull_case"] == "b" and b["portfolio"]["open_positions"] == 2
    assert b["market"] == {"regime_ok": True, "regime_note": "QQQ > 20d MA"}
    json.dumps(b, default=str)  # must serialise for the prompt


def test_edge_bundle_without_context_or_committee(breakout):
    cfg, df, sig = breakout
    plan = build_plan(sig, df.iloc[-1], cfg, 100_000, 0.0, 100_000, True)
    plan.committee = {"error": "boom"}
    b = reviewer.build_edge_bundle(plan.to_dict(), False, "risk-off")
    assert "news" not in b and "committee_debate" not in b and b["market"]["regime_ok"] is False


# --------------------------------------------------------------------------- #
# verdict parsing
# --------------------------------------------------------------------------- #
def test_normalise_verdict_shapes_and_tolerances():
    v = reviewer.normalise_verdict(GOOD, "gemini", "m")
    assert v["action"] == "BUY" and v["confidence"] == 0.78 and v["sizeMultiplier"] == 1.0
    assert set(v) == {"action", "confidence", "thesis", "catalysts", "risks", "invalidation", "sizeNote", "sizeMultiplier", "provider", "model"}
    # percent confidence, lowercase action, semicolon lists, fenced JSON with chatter
    fenced = 'Here you go:\n```json\n{"action": "hold", "confidence": 65, "thesis": "meh", "catalysts": "a; b", "risks": [], "invalidation": "", "sizeNote": "", "sizeMultiplier": 1.7}\n```'
    v = reviewer.normalise_verdict(fenced, "openai", "m")
    assert v["action"] == "HOLD" and v["confidence"] == 0.65 and v["catalysts"] == ["a", "b"] and v["sizeMultiplier"] == 1.0
    v = reviewer.normalise_verdict({**GOOD, "sizeMultiplier": "0.5", "risks": list("abcdefghij")}, "openai", "m")
    assert v["sizeMultiplier"] == 0.5 and len(v["risks"]) == 8
    with pytest.raises(ValueError):
        reviewer.normalise_verdict({**GOOD, "action": "MAYBE"}, "gemini", "m")
    with pytest.raises(ValueError):
        reviewer.normalise_verdict("no json here", "gemini", "m")


# --------------------------------------------------------------------------- #
# transports
# --------------------------------------------------------------------------- #
def test_review_trade_gemini_native(monkeypatch):
    seen = {}

    def fake_post(url, params=None, json=None, timeout=None, headers=None, **kw):
        seen.update(url=url, params=params, body=json, timeout=timeout, headers=headers)
        return _Resp({"candidates": [{"content": {"parts": [{"text": json_dumps(GOOD)}]}}]})

    json_dumps = json.dumps
    monkeypatch.setattr(reviewer.requests, "post", fake_post)
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    cfg = ReviewerSettings(enabled=True, provider="auto")
    v = reviewer.review_trade({"symbol": "ABC", "plan": {}}, cfg)
    assert v["action"] == "BUY" and v["provider"] == "gemini" and v["model"] == reviewer.DEFAULT_MODELS["gemini"]
    assert seen["url"].endswith(f"/models/{reviewer.DEFAULT_MODELS['gemini']}:generateContent")
    # the key travels in a header, never in the URL, so HTTP errors cannot quote it
    assert seen["params"] is None and seen["headers"] == {"x-goog-api-key": "g-key"} and "g-key" not in seen["url"]
    gc = seen["body"]["generationConfig"]
    assert gc["responseMimeType"] == "application/json" and gc["responseSchema"]["properties"]["action"]["enum"] == ["BUY", "SELL", "HOLD"]
    assert "Kullam" in seen["body"]["systemInstruction"]["parts"][0]["text"]
    assert json.loads(seen["body"]["contents"][0]["parts"][0]["text"])["symbol"] == "ABC"


def test_review_trade_openai_compatible(monkeypatch):
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None, **kw):
        seen.update(url=url, headers=headers, body=json)
        return _Resp({"choices": [{"message": {"content": json_dumps({**GOOD, "action": "SELL", "confidence": 0.9})}}]})

    json_dumps = json.dumps
    monkeypatch.setattr(reviewer.requests, "post", fake_post)
    monkeypatch.setenv("QMAG_LLM_API_KEY", "local-key")
    cfg = ReviewerSettings(enabled=True, provider="openai", model="llama-3", base_url="https://llm.local/v1/")
    v = reviewer.review_trade({"symbol": "ABC"}, cfg)
    assert v["action"] == "SELL" and v["provider"] == "openai" and v["model"] == "llama-3"
    assert seen["url"] == "https://llm.local/v1/chat/completions" and seen["headers"]["Authorization"] == "Bearer local-key"
    assert seen["body"]["response_format"] == {"type": "json_object"} and seen["body"]["messages"][0]["role"] == "system"


def test_review_trade_never_raises(monkeypatch):
    cfg = ReviewerSettings(enabled=True, provider="gemini")
    v = reviewer.review_trade({"symbol": "ABC"}, cfg)  # no key
    assert "error" in v and "no API key saved for Google Gemini" in v["error"] and v["provider"] == "gemini"

    def boom(*a, **k):
        raise reviewer.requests.ConnectionError("down")

    monkeypatch.setattr(reviewer.requests, "post", boom)
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    v = reviewer.review_trade({"symbol": "ABC"}, ReviewerSettings(enabled=True, provider="openai"))
    assert v["error"].startswith("ConnectionError")

    monkeypatch.setattr(reviewer.requests, "post", lambda *a, **k: _Resp({"choices": [{"message": {"content": "I cannot decide."}}]}))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-recovered")  # new credentials do not inherit the failed key's cooldown
    v = reviewer.review_trade({"symbol": "ABC"}, ReviewerSettings(enabled=True, provider="openai"))
    assert v["error"].startswith("ValueError")


def test_errors_never_contain_the_api_key(monkeypatch, tmp_path):
    """A retired model name used to produce a 404 whose text quoted the URL - including ?key=..."""
    from qmag.health import ConnectionRegistry, _ping_gemini
    from qmag.redact import describe_error, redact_secrets

    key = "AQ.Ab8RN6K_this_is_a_secret_key_value_0123456789"
    monkeypatch.setenv("GEMINI_API_KEY", key)

    class _R404:
        status_code = 404

        def raise_for_status(self):
            raise reviewer.requests.HTTPError(f"404 Client Error: Not Found for url: https://x/models/old:generateContent?key={key}")

    seen = {}

    def fake_post(url, headers=None, **kw):
        seen.update(url=url, headers=headers, params=kw.get("params"))
        return _R404()

    monkeypatch.setattr(reviewer.requests, "post", fake_post)
    v = reviewer.review_trade({"symbol": "ABC"}, ReviewerSettings(enabled=True, provider="gemini", model="gemini-2.0-flash"))
    assert "error" in v and key not in v["error"] and "gemini-2.0-flash" in v["error"] and "settings page" in v["error"]
    assert seen["params"] is None and seen["headers"] == {"x-goog-api-key": key}

    # the health probe: same header transport, same friendly 404
    import requests as _rq

    monkeypatch.setattr(_rq, "get", lambda url, headers=None, **kw: _R404())
    with pytest.raises(RuntimeError, match="not found for this key"):
        _ping_gemini("gemini-2.0-flash", key)

    # any error string that does reach the registry is scrubbed before it is stored
    leaky = f"HTTPError: 404 for url https://x/y?key={key} Bearer sk-proj-abcdefghijklmnopqrstuvwxyz"
    assert key not in redact_secrets(leaky) and "sk-proj" not in redact_secrets(leaky)
    assert describe_error(ValueError(f"bad {key}")) == "ValueError: bad •••"
    reg = ConnectionRegistry(tmp_path / "connections.json")
    rec = reg.record("llm_reviewer", False, detail=f"probe with {key}", error=leaky)
    assert key not in rec["last_error"] and key not in rec["detail"]
    assert key not in (tmp_path / "connections.json").read_text()


def test_resolve_provider_auto_prefers_gemini_when_keyed(monkeypatch):
    # nothing configured: auto names Gemini so the error says what is missing
    assert reviewer.resolve_provider(ReviewerSettings(provider="auto"))[0] == "gemini"
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    assert reviewer.resolve_provider(ReviewerSettings(provider="auto"))[0] == "openai"
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.setenv("GOOGLE_API_KEY", "g")
    p, m, k = reviewer.resolve_provider(ReviewerSettings(provider="auto"))
    assert (p, m, k) == ("gemini", reviewer.DEFAULT_MODELS["gemini"], "g")
    p, m, k = reviewer.resolve_provider(ReviewerSettings(provider="openai", model="x"))
    assert (p, m, k) == ("openai", "x", None)


# --------------------------------------------------------------------------- #
# how the trader uses it
# --------------------------------------------------------------------------- #
def test_verdict_allows_trade_modes():
    buy = {**GOOD, "confidence": 0.7}
    assert reviewer.verdict_allows_trade(buy, ReviewerSettings(enabled=True, mode="advisory")) == (True, "advisory")
    assert reviewer.verdict_allows_trade({**buy, "action": "HOLD"}, ReviewerSettings(enabled=True, mode="advisory"))[0]
    gate = ReviewerSettings(enabled=True, mode="gate", min_confidence=0.6)
    assert reviewer.verdict_allows_trade(buy, gate)[0]
    assert not reviewer.verdict_allows_trade({**buy, "action": "HOLD"}, gate)[0]
    assert not reviewer.verdict_allows_trade({**buy, "action": "SELL"}, gate)[0]
    ok, why = reviewer.verdict_allows_trade({**buy, "confidence": 0.5}, gate)
    assert not ok and "50%" in why
    assert reviewer.verdict_allows_trade({"error": "x"}, gate) == (True, "reviewer unavailable, fail-open")
    closed = ReviewerSettings(enabled=True, mode="gate", fail_closed=True)
    assert reviewer.verdict_allows_trade({"error": "x"}, closed) == (False, "reviewer unavailable, fail-closed")
    assert reviewer.verdict_allows_trade(None, closed)[0] is False


def test_apply_reviewer_advisory_gate_and_size(breakout, monkeypatch):
    cfg, df, sig = breakout
    answers = {"v": GOOD}
    monkeypatch.setattr("qmag.reviewer.review_trade", lambda bundle, rcfg: dict(answers["v"]))

    # Disabled: untouched.
    plan = apply_reviewer(build_plan(sig, df.iloc[-1], cfg, 100_000, 0.0, 100_000, True), cfg, True)
    assert plan.reviewer is None and "reviewer" not in plan.notes

    advisory = cfg.with_overrides({"reviewer.enabled": True, "reviewer.mode": "advisory"})
    plan = apply_reviewer(build_plan(sig, df.iloc[-1], advisory, 100_000, 0.0, 100_000, True), advisory, True)
    full = plan.shares
    assert plan.ok and plan.reviewer["action"] == "BUY" and plan.notes["reviewer"] == "advisory" and "llm_reviewer" not in plan.checks

    gate = cfg.with_overrides({"reviewer.enabled": True, "reviewer.mode": "gate", "reviewer.min_confidence": 0.6})
    plan = apply_reviewer(build_plan(sig, df.iloc[-1], gate, 100_000, 0.0, 100_000, True), gate, True)
    assert plan.ok and plan.checks["llm_reviewer"] is True and plan.shares == full

    answers["v"] = {**GOOD, "action": "HOLD", "confidence": 0.9, "thesis": "Wait for volume."}
    plan = apply_reviewer(build_plan(sig, df.iloc[-1], gate, 100_000, 0.0, 100_000, True), gate, True)
    assert not plan.ok and plan.failed_checks == ["llm_reviewer"] and "HOLD" in plan.notes["reviewer"]
    assert plan.rationale["verdict"].startswith("BLOCKED by the LLM reviewer") and "Wait for volume." in plan.rationale["verdict"]

    answers["v"] = {**GOOD, "confidence": 0.4}
    plan = apply_reviewer(build_plan(sig, df.iloc[-1], gate, 100_000, 0.0, 100_000, True), gate, True)
    assert not plan.ok and "40%" in plan.notes["reviewer"]

    sized = cfg.with_overrides({"reviewer.enabled": True, "reviewer.mode": "gate_and_size"})
    answers["v"] = {**GOOD, "sizeMultiplier": 0.5}
    plan = apply_reviewer(build_plan(sig, df.iloc[-1], sized, 100_000, 0.0, 100_000, True), sized, True)
    assert plan.ok and plan.shares == int(full * 0.5) and plan.partial_qty <= plan.shares
    assert plan.risk_dollars == pytest.approx(plan.shares * plan.risk_per_share) and plan.position_value == pytest.approx(plan.shares * plan.entry)
    assert "x0.50 size" in plan.rationale["size"] and plan.rationale["verdict"].startswith("TAKE")

    # In gate mode a SELL/HOLD does not resize; only BUY with sizeMultiplier < 1 does, and only in gate_and_size.
    answers["v"] = {**GOOD, "sizeMultiplier": 0.5}
    plan = apply_reviewer(build_plan(sig, df.iloc[-1], gate, 100_000, 0.0, 100_000, True), gate, True)
    assert plan.shares == full

    # Reviewer failure: fail-open keeps the plan, fail-closed blocks it.
    answers["v"] = {"error": "RuntimeError: no key", "provider": "gemini", "model": "m"}
    plan = apply_reviewer(build_plan(sig, df.iloc[-1], gate, 100_000, 0.0, 100_000, True), gate, True)
    assert plan.ok and plan.checks["llm_reviewer"] is True and "fail-open" in plan.notes["reviewer"]
    closed = gate.with_overrides({"reviewer.fail_closed": True})
    plan = apply_reviewer(build_plan(sig, df.iloc[-1], closed, 100_000, 0.0, 100_000, True), closed, True)
    assert not plan.ok and plan.failed_checks == ["llm_reviewer"]


def test_apply_reviewer_skips_rejected_plans_unless_asked(breakout, monkeypatch):
    cfg, df, sig = breakout
    calls = []
    monkeypatch.setattr("qmag.reviewer.review_trade", lambda bundle, rcfg: calls.append(bundle["symbol"]) or dict(GOOD))
    on = cfg.with_overrides({"reviewer.enabled": True, "management.max_stop_pct": 0.01})
    rejected = build_plan(sig, df.iloc[-1], on, 100_000, 0.0, 100_000, True)
    assert not rejected.ok
    apply_reviewer(rejected, on, True)
    assert calls == [] and rejected.reviewer is None
    apply_reviewer(rejected, on, True, review_rejected=True)
    assert calls == ["ABC"] and rejected.reviewer["action"] == "BUY"


def test_run_cycle_logs_reviewer_and_respects_gate(monkeypatch, tmp_path):
    """End to end through the trader: a HOLD verdict in gate mode blocks the buy-stop, advisory does not."""
    from qmag.broker import PaperBroker
    from qmag.trader import TraderState, run_cycle

    frames = {"ABC": make_breakout_frame()}
    base = StrategyConfig().with_overrides({"regime.enabled": False, "themes.enabled": False, "reviewer.enabled": True})
    monkeypatch.setattr("qmag.reviewer.review_trade", lambda bundle, rcfg: {**GOOD, "action": "HOLD", "confidence": 0.8, "thesis": "Wait for volume."})

    def cycle(cfg):
        broker = PaperBroker(state_path=tmp_path / f"{cfg.reviewer.mode}.json", starting_cash=100_000)
        return run_cycle(frames, broker, cfg, TraderState())

    advisory = cycle(base.with_overrides({"reviewer.mode": "advisory"}))
    assert any(a.startswith("REVIEW ABC: HOLD 80% [advisory] Wait for volume.") for a in advisory.actions)
    assert advisory.plans and advisory.plans[0].reviewer["action"] == "HOLD" and advisory.plans[0].ok

    gated = cycle(base.with_overrides({"reviewer.mode": "gate"}))
    assert not gated.plans and gated.rejected and gated.rejected[0].failed_checks == ["llm_reviewer"]
    assert any("[gate]" in a for a in gated.actions)
