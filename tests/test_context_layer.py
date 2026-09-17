"""Context scoring, plan gates, rationale text, LLM committee plumbing and adaptive risk."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from qmag.backtest import prepare_data
from qmag.config import StrategyConfig
from qmag.context.base import ContextCache, ContextReport, Headline
from qmag.context.scoring import score_headline, score_social
from qmag.context.sources import _num, _parse_finviz_earnings, finalize_news, finalize_social, flow_score_from
from qmag.plan import adaptive_risk_multiplier, apply_committee, build_plan, context_checks
from qmag.setups import BreakoutDetector
from qmag.trader import ManagedPosition, recent_r_multiples, TraderState
from tests.conftest import make_breakout_frame


@pytest.fixture
def breakout():
    cfg = StrategyConfig().with_overrides({"regime.enabled": False, "themes.enabled": False, "edge.enabled": False})
    df = prepare_data({"ABC": make_breakout_frame()}, cfg)["ABC"]
    sig = BreakoutDetector().detect_last("ABC", df, cfg)
    assert sig is not None
    return cfg, df, sig


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #
def test_headline_scoring_and_catalyst_tags():
    s_up, tags_up = score_headline("Acme raises full-year guidance after record quarter; FDA approval for lead drug")
    s_dn, tags_dn = score_headline("Acme announces $150M public offering of common stock; shares slide")
    assert s_up > 0.2 and "fda" in tags_up and ({"guidance", "earnings"} & set(tags_up))
    assert s_dn < -0.2 and "offering" in tags_dn
    neutral, tags = score_headline("Acme to present at investor conference")
    assert -0.3 < neutral < 0.3


def test_social_scoring_uses_platform_label_then_text():
    assert score_social("loading more here, this rips", "Bullish") > 0.5
    assert score_social("dumping this bag", "Bearish") < -0.5
    assert score_social("to the moon, squeeze incoming") > 0
    assert score_social("puts printing, this is a rug") < 0


def test_finviz_parsing_helpers():
    assert _num("18.05M") == pytest.approx(18_050_000)
    assert _num("2.3B") == pytest.approx(2.3e9)
    assert _num("17.63%") == pytest.approx(17.63)
    assert _num("-") is None and _num(None) is None
    now = datetime(2026, 9, 11, tzinfo=timezone.utc)
    assert _parse_finviz_earnings("Nov 03 BMO", now) == "2026-11-03"
    assert _parse_finviz_earnings("Aug 04 AMC", now) == "2026-08-04"
    assert _parse_finviz_earnings("-", now) is None


def test_finalize_news_weights_recent_headlines_more():
    now = datetime(2026, 9, 11, 12, tzinfo=timezone.utc)
    rep = ContextReport(symbol="X", asof="2026-09-11")
    rep.headlines = [
        Headline(when="2026-09-11T10:00:00+00:00", title="fresh good", source="t", url="", score=0.8, tags=["guidance"]),
        Headline(when="2026-09-01T10:00:00+00:00", title="stale bad", source="t", url="", score=-0.8, tags=["offering"]),
    ]
    finalize_news(rep, now)
    assert rep.news_count == 2 and rep.news_score > 0.5
    assert rep.catalysts == ["guidance", "offering"] or set(rep.catalysts) == {"guidance", "offering"}


def test_finalize_social_requires_min_messages():
    rep = ContextReport(symbol="X", asof="d")
    finalize_social(rep, [0.8, 0.8], ["a", "b"], min_messages=5)
    assert rep.social_score is None and rep.social_messages == 2
    rep2 = ContextReport(symbol="X", asof="d")
    finalize_social(rep2, [0.8, 0.8, -0.8, 0.9, 0.7], ["m"] * 5, min_messages=5)
    assert rep2.social_score == pytest.approx(0.48) and rep2.social_messages == 5 and rep2.social_samples == ["m"] * 4


def test_flow_score_from_mock_unusual_whales_payloads():
    rep = ContextReport(symbol="X", asof="d")
    vol = {"bullish_premium": 900_000, "bearish_premium": 100_000, "call_premium": 1_200_000, "put_premium": 300_000,
           "call_volume": 30_000, "avg_30_day_call_volume": 10_000, "put_volume": 8_000, "avg_30_day_put_volume": 9_000}
    alerts = [
        {"type": "call", "has_sweep": True, "total_ask_side_prem": 250_000},
        {"type": "put", "has_sweep": False, "total_ask_side_prem": 50_000},
    ]
    score = flow_score_from(vol, alerts, rep)
    assert score is not None and score > 0.5
    assert rep.flow_alerts == 2 and rep.flow_sweeps == 1 and rep.flow_call_premium == 1_200_000
    bearish = flow_score_from({"bullish_premium": 100, "bearish_premium": 900}, [{"type": "put", "total_premium": 1e6}])
    assert bearish is not None and bearish < -0.5
    assert flow_score_from(None, []) is None


def test_context_cache_roundtrip_and_ttl(tmp_path):
    cache = ContextCache(tmp_path / "ctx.json", ttl_minutes=30)
    rep = ContextReport(symbol="ABC", asof="2026-09-11", news_score=0.4, fetched_at=datetime.now(timezone.utc).timestamp())
    cache.put(rep)
    cache.save()
    again = ContextCache(tmp_path / "ctx.json", ttl_minutes=30)
    hit = again.get("ABC")
    assert hit is not None and hit.news_score == 0.4
    stale = ContextReport(symbol="OLD", asof="d", fetched_at=datetime.now(timezone.utc).timestamp() - 3 * 3600)
    again.put(stale)
    assert again.get("OLD") is None


# --------------------------------------------------------------------------- #
# plan gates, rationale, committee
# --------------------------------------------------------------------------- #
def _ctx(**kw) -> ContextReport:
    rep = ContextReport(symbol="ABC", asof="2026-09-11")
    for k, v in kw.items():
        setattr(rep, k, v)
    return rep


def test_context_checks_gate_earnings_news_and_crowding(breakout):
    cfg, df, sig = breakout
    ok = context_checks(sig, _ctx(days_to_earnings=10, news_score=0.1, social_score=0.3, social_messages=20), cfg)
    assert all(ok.values()) and set(ok) >= {"earnings_window", "news_flow", "social_crowding"}
    assert context_checks(sig, _ctx(days_to_earnings=2), cfg)["earnings_window"] is False
    assert context_checks(sig, _ctx(news_score=-0.9, news_count=3), cfg)["news_flow"] is False
    # Missing data always passes; a disabled layer adds no checks.
    assert all(context_checks(sig, _ctx(), cfg).values())
    assert context_checks(sig, _ctx(days_to_earnings=0), cfg.with_overrides({"context.enabled": False})) == {}


def test_build_plan_attaches_context_score_and_rationale(breakout):
    cfg, df, sig = breakout
    ctx = _ctx(news_score=0.6, news_count=4, catalysts=["guidance"], social_score=0.2, social_messages=40, social_bullish=25, social_bearish=5,
               float_shares=20e6, short_float_pct=22.0, sector="Technology", industry="Semiconductors", days_to_earnings=40)
    plain = build_plan(sig, df.iloc[-1], cfg, 100_000, 0.0, 100_000, True)
    rich = build_plan(sig, df.iloc[-1], cfg, 100_000, 0.0, 100_000, True, context=ctx, regime_note="QQQ > 20d MA, breadth 61%")
    assert rich.ok and rich.score > plain.score
    r = rich.rationale
    assert set(r) >= {"setup", "entry", "size", "stop", "profit_plan", "context", "bull_case", "bear_case", "verdict"}
    assert f"{rich.shares} shares" in r["size"] and "ADR" in r["stop"] and "OCO" in r["profit_plan"]
    assert "low float" in r["context"] and "short interest" in r["context"] and "guidance" in r["context"]
    assert r["verdict"].startswith("TAKE")
    d = rich.to_dict()
    assert d["context"]["news_score"] == 0.6 and d["ok"] is True and d["failed_checks"] == []


def test_build_plan_rejects_absurdly_wide_stops(breakout):
    cfg, df, sig = breakout
    wide = cfg.with_overrides({"management.max_stop_pct": 0.01})
    plan = build_plan(sig, df.iloc[-1], wide, 100_000, 0.0, 100_000, True)
    assert not plan.ok and "stop_distance_ok" in plan.failed_checks
    assert "REJECTED" in plan.rationale["stop"] and "stop_distance_ok" in plan.rationale["verdict"]
    assert not plan.rationale["verdict"].startswith("TAKE")


def test_adaptive_risk_multiplier_and_sizing(breakout):
    cfg, df, sig = breakout
    assert adaptive_risk_multiplier([], cfg) == 1.0
    assert adaptive_risk_multiplier([-1.0] * 3, cfg) == 1.0  # fewer than min_trades
    assert adaptive_risk_multiplier([-1.0, -1.0, -0.5, 0.2, -1.0, -0.8], cfg) == cfg.adaptive.cold_risk_mult
    assert adaptive_risk_multiplier([2.0, 1.5, -1.0, 3.0, 0.5, 1.0], cfg) == cfg.adaptive.hot_risk_mult
    base = build_plan(sig, df.iloc[-1], cfg, 100_000, 0.0, 100_000, True)
    cold = build_plan(sig, df.iloc[-1], cfg, 100_000, 0.0, 100_000, True, risk_mult=0.5)
    hot = build_plan(sig, df.iloc[-1], cfg, 100_000, 0.0, 100_000, True, risk_mult=3.0)
    assert cold.shares < base.shares
    assert hot.risk_pct <= cfg.adaptive.max_risk_per_trade_pct + 1e-9  # capped even when hot
    assert "scaled x0.50" in cold.rationale["size"]


def test_apply_committee_uses_mocked_llm_and_respects_veto(breakout, monkeypatch):
    cfg, df, sig = breakout
    calls = []

    def fake_review(plan_dict, rationale, context, ccfg):
        calls.append(ccfg.risk_model)
        return {"verdict": "reduce", "confidence": 0.7, "size_multiplier": 0.5, "bull_case": "b", "bear_case": "r", "risk_review": "x", "model": ccfg.risk_model}

    monkeypatch.setattr("qmag.llm.committee_review", fake_review)
    on = cfg.with_overrides({"committee.enabled": True, "committee.can_veto": False, "committee.risk_model": "test-model"})
    plan = apply_committee(build_plan(sig, df.iloc[-1], on, 100_000, 0.0, 100_000, True), on)
    assert calls == ["test-model"] and plan.committee["verdict"] == "reduce" and plan.ok
    full = plan.shares
    veto = on.with_overrides({"committee.can_veto": True})
    reduced = apply_committee(build_plan(sig, df.iloc[-1], veto, 100_000, 0.0, 100_000, True), veto)
    assert reduced.shares == int(full * 0.5) and reduced.ok

    monkeypatch.setattr("qmag.llm.committee_review", lambda *a, **k: {"verdict": "reject", "confidence": 0.9, "size_multiplier": 0.0, "model": "m"})
    rejected = apply_committee(build_plan(sig, df.iloc[-1], veto, 100_000, 0.0, 100_000, True), veto)
    assert not rejected.ok and rejected.failed_checks == ["committee"]
    # A seat that could not answer never changes the plan, and says so.
    monkeypatch.setattr("qmag.llm.committee_review", lambda *a, **k: {"error": "Bear researcher: RuntimeError: boom", "seats": {}})
    unruled = apply_committee(build_plan(sig, df.iloc[-1], veto, 100_000, 0.0, 100_000, True), veto)
    assert unruled.ok and unruled.shares == full and unruled.notes["committee"].startswith("no ruling")
    # The per-cycle budget: the second plan is noted, not reviewed.
    monkeypatch.setattr("qmag.llm.committee_review", lambda *a, **k: {"verdict": "take", "confidence": 0.5, "size_multiplier": 1.0})
    budget = {"left": 1}
    first = apply_committee(build_plan(sig, df.iloc[-1], veto, 100_000, 0.0, 100_000, True), veto, budget)
    second = apply_committee(build_plan(sig, df.iloc[-1], veto, 100_000, 0.0, 100_000, True), veto, budget)
    assert first.committee and second.committee is None and "not convened" in second.notes["committee"]
    # Disabled: never called.
    monkeypatch.setattr("qmag.llm.committee_review", lambda *a, **k: pytest.fail("should not be called"))
    apply_committee(build_plan(sig, df.iloc[-1], cfg, 100_000, 0.0, 100_000, True), cfg)


def test_committee_is_three_separate_seats_with_their_own_models(monkeypatch):
    import qmag.llm as llm
    from qmag.config import CommitteeSettings

    seen = []

    def fake_ask(bundle, sc, system, schema=None):
        seen.append((sc.provider, sc.model, sorted(bundle), system[:20]))
        if "BULL" in system:
            return '{"case": "tight flag, sector leader", "key_points": ["a"], "confidence": 0.7}', sc.provider, sc.model
        if "BEAR" in system:
            assert bundle["bull_case"]["case"] == "tight flag, sector leader"  # the bear read the bull first
            return '{"case": "earnings in 4 days", "key_points": ["b"], "confidence": 0.6}', sc.provider, sc.model
        assert bundle["bull_case"]["case"] and bundle["bear_case"]["case"]
        return '{"verdict": "REDUCE", "size_multiplier": 0.5, "confidence": 0.8, "risk_review": "half size into the print", "decisive_factor": "event risk"}', sc.provider, sc.model

    monkeypatch.setattr("qmag.reviewer.ask_json", fake_ask)
    cfg = CommitteeSettings(enabled=True, bull_provider="gemini", bull_model="g-1", bear_provider="openai", bear_model="o-1", risk_provider="gemini", risk_model="g-2")
    out = llm.committee_review({"symbol": "ABC", "rationale": {}, "context": {}, "notes": {"x": 1}}, {"setup": "s"}, {"headlines": []}, cfg)
    assert [(p, m) for p, m, _, _ in seen] == [("gemini", "g-1"), ("openai", "o-1"), ("gemini", "g-2")]
    assert seen[0][2] == ["facts"] and seen[1][2] == ["bull_case", "facts"] and seen[2][2] == ["bear_case", "bull_case", "facts"]
    assert out["verdict"] == "reduce" and out["size_multiplier"] == 0.5 and out["confidence"] == 0.8 and out["decisive_factor"] == "event risk"
    assert out["bull_case"] == "tight flag, sector leader" and out["bear_case"] == "earnings in 4 days" and out["seats"]["bear"]["model"] == "o-1"

    # Debate off: the bear writes blind.
    seen.clear()
    llm.committee_review({"symbol": "ABC"}, {}, None, CommitteeSettings(enabled=True, debate=False, bull_model="m", bear_model="m", risk_model="m"))
    assert seen[1][2] == ["facts"]

    # One seat failing = no ruling, the other seats are kept, and the key never appears in the error.
    def flaky(bundle, sc, system, schema=None):
        if "BEAR" in system:
            raise RuntimeError("401 for key sk-secret-key-value-123456")
        return '{"case": "x", "key_points": [], "confidence": 0.5, "verdict": "take", "size_multiplier": 1, "risk_review": "r", "decisive_factor": "d"}', sc.provider, sc.model

    monkeypatch.setattr("qmag.reviewer.ask_json", flaky)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-key-value-123456")
    out = llm.committee_review({"symbol": "ABC"}, {}, None, cfg)
    assert "verdict" not in out and out["error"].startswith("Bear researcher:") and "sk-secret" not in out["error"]
    assert out["seats"]["bull"]["case"] == "x" and "error" in out["seats"]["bear"]


def test_legacy_single_model_committee_yaml_becomes_three_openai_seats():
    from qmag.config import StrategyConfig

    cfg = StrategyConfig.from_dict({"context": {"llm_enabled": True, "llm_can_veto": True, "llm_model": "gpt-x", "llm_base_url": "https://llm.local/v1"}})
    c = cfg.committee
    assert c.enabled and c.can_veto
    assert (c.bull_provider, c.bear_provider, c.risk_provider) == ("openai", "openai", "openai")
    assert c.bull_model == c.bear_model == c.risk_model == "gpt-x" and c.risk_base_url == "https://llm.local/v1"
    assert not hasattr(cfg.context, "llm_enabled")


# --------------------------------------------------------------------------- #
# journal
# --------------------------------------------------------------------------- #
def test_close_record_journals_realised_r():
    pos = ManagedPosition(symbol="ABC", setup="breakout", entry_date="2026-01-05", entry_price=100.0, shares=90, initial_stop=95.0, stop=95.0, pivot=99.5, remaining=90, target=110.0, partial_qty=30)
    pos.book_sale(30, 110.0)  # partial at +2R
    pos.remaining = 60
    rec = pos.close_record("2026-01-20", "trail_ma", 60, 104.0)
    # pnl = 30*10 + 60*4 = 540 ; risk = 90 * 5 = 450 -> 1.2R
    assert rec["pnl"] == 540.0 and rec["r_multiple"] == pytest.approx(1.2)
    assert rec["exit_reason"] == "trail_ma" and rec["closed_on"] == "2026-01-20"
    state = TraderState()
    state.closed.extend([rec, {**rec, "r_multiple": -1.0}, {**rec, "r_multiple": None}])
    assert recent_r_multiples(state) == [1.2, -1.0]
