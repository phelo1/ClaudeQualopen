"""Additive LLM trade reviewer (off by default).

Modelled on danilobatson/ai-trading-agent-gemini: instead of asking a model to
*find* trades, hand it everything the engine already knows about one trade -
the "edge bundle" - and demand a strict-JSON verdict:

    {"action": "BUY" | "SELL" | "HOLD", "confidence": 0..1, "thesis": str,
     "catalysts": [str], "risks": [str], "invalidation": str, "sizeNote": str,
     "sizeMultiplier": 0..1}

The bundle contains the setup and its numbers, the sized plan (entry, stop,
partial target, shares, risk), the pass/fail checklist, the deterministic
rationale, news headlines with scores and catalyst tags, earnings dates,
social and options-flow readings, fundamentals (float, short interest,
insiders, institutions), the bull/bear committee debate if it ran, the
market regime, and the portfolio state (open positions, recent realised R,
current risk multiplier). Nothing the engine did not see.

Two transports, chosen by the *provider* each seat names (see
``qmag.providers``: the AI providers card on the settings page, where every
vendor endpoint and its key live):

* **Gemini** native REST with ``responseMimeType: application/json`` and a
  response schema, so the JSON is enforced server-side.
* Any **OpenAI-compatible** chat endpoint (OpenAI, Anthropic, xAI, Groq,
  OpenRouter, DeepSeek, Mistral, Together, Ollama, vLLM...) with
  ``response_format: json_object``.

How the trader uses the verdict is ``reviewer.mode``: advisory (record only),
gate (BUY with enough confidence or no trade), gate_and_size (also scale the
position by ``sizeMultiplier``).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import requests

from .config import ReviewerSettings
from .providers import BUILTIN_IDS, PRESETS, Provider, registry
from .redact import describe_error

log = logging.getLogger(__name__)

ACTIONS = ("BUY", "SELL", "HOLD")

SYSTEM_PROMPT = (
    "You are a senior swing-trading reviewer at a momentum desk that trades Kristjan Kullamägi's playbook: "
    "breakouts from tight flags in momentum leaders and episodic pivots (gap-ups on a genuine catalyst), "
    "1-ADR stops, one third sold at +2R, the rest trailed on the 10/20-day moving average, 0.5% of equity risked per trade. "
    "You receive the complete edge bundle for ONE candidate trade: the setup geometry, the sized plan, the rule checklist, "
    "the desk's written rationale, news headlines with sentiment scores and catalyst tags, earnings dates, social-media and "
    "options-flow readings, fundamentals, the bull/bear committee debate (if any), the market regime and the portfolio state. "
    "Judge whether this specific entry should be taken NOW at the planned size. Rules: use only the facts in the bundle - never "
    "invent prices, news or numbers; a stock in the middle of nowhere is HOLD, not BUY; SELL means the desk should stand aside "
    "or exit because the thesis is broken (e.g. dilution, failed breakout, earnings inside the hold window, crowded euphoria); "
    "be sceptical of unanimous bullish chatter and of gaps without a real catalyst; prefer fewer, cleaner trades. "
    "Answer ONLY with a JSON object with exactly these keys: "
    '"action" ("BUY"|"SELL"|"HOLD"), "confidence" (number 0-1), "thesis" (<=60 words), "catalysts" (list of short strings), '
    '"risks" (list of short strings), "invalidation" (<=40 words: what price/event proves the idea wrong), '
    '"sizeNote" (<=40 words on whether the planned size is right), "sizeMultiplier" (number 0-1; 1 = planned size).'
)

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "action": {"type": "STRING", "enum": list(ACTIONS)},
        "confidence": {"type": "NUMBER"},
        "thesis": {"type": "STRING"},
        "catalysts": {"type": "ARRAY", "items": {"type": "STRING"}},
        "risks": {"type": "ARRAY", "items": {"type": "STRING"}},
        "invalidation": {"type": "STRING"},
        "sizeNote": {"type": "STRING"},
        "sizeMultiplier": {"type": "NUMBER"},
    },
    "required": ["action", "confidence", "thesis", "catalysts", "risks", "invalidation", "sizeNote"],
}

# The built-in providers' fallback models (gemini-2.0-flash was retired in
# June 2026; 3.5-flash is the recommended replacement). Other providers have
# no guessed default: the settings page lists what a key can actually call.
DEFAULT_MODELS = {pid: PRESETS[pid]["default_model"] for pid in BUILTIN_IDS}


# --------------------------------------------------------------------------- #
# Edge bundle
# --------------------------------------------------------------------------- #
def build_edge_bundle(
    plan: dict[str, Any],
    regime_ok: bool,
    regime_note: str = "",
    portfolio: dict[str, Any] | None = None,
    entry_mode: str | None = None,
) -> dict[str, Any]:
    """Everything the engine knows about one candidate, compact enough for one prompt."""
    ctx = plan.get("context") or {}

    def _r(v: Any, nd: int = 4) -> Any:  # keep the prompt short: no 17-digit floats
        return round(v, nd) if isinstance(v, float) else v

    levels = {
        "entry": _r(plan["entry"]),
        "stop": _r(plan["stop"]),
        "stop_pct": round((1 - plan["stop"] / plan["entry"]) * 100, 2) if plan["entry"] else None,
        "partial_target": _r(plan.get("partial_target")),
        "partial_qty": plan.get("partial_qty"),
        "shares": plan["shares"],
        "position_value": round(plan.get("position_value", 0.0), 2),
        "position_pct_of_equity": round(plan.get("position_pct", 0.0) * 100, 2),
        "risk_dollars": round(plan.get("risk_dollars", 0.0), 2),
        "risk_pct_of_equity": round(plan.get("risk_pct", 0.0) * 100, 3),
        "risk_multiplier": plan.get("risk_mult", 1.0),
        "partial_after_days": plan.get("partial_after_days"),
        "trail_ma": plan.get("trail_ma"),
        "max_hold_days": plan.get("max_hold_days"),
        "entry_mode": entry_mode or ("market" if plan.get("setup") == "episodic_pivot" else "buy_stop_at_pivot"),
    }
    bundle: dict[str, Any] = {
        "symbol": plan["symbol"],
        "asof": plan.get("date"),
        "setup": {"type": plan.get("setup"), "pivot": _r(plan.get("pivot")), "score": _r(plan.get("score"), 3), **{k: _r(v) for k, v in (plan.get("notes") or {}).items()}},
        "theme": {"name": plan.get("theme"), "percentile": _r(plan.get("theme_pct"), 3)},
        "plan": levels,
        "checklist": plan.get("checks", {}),
        "failed_checks": plan.get("failed_checks", []),
        "rationale": plan.get("rationale", {}),
        "market": {"regime_ok": regime_ok, "regime_note": regime_note},
        "portfolio": portfolio or {},
    }
    if ctx:
        bundle["news"] = {
            "score": ctx.get("news_score"),
            "count": ctx.get("news_count"),
            "catalyst_tags": ctx.get("catalysts", []),
            "headlines": [
                {"when": str(h.get("when", ""))[:10], "source": h.get("source"), "title": h.get("title"), "score": h.get("score"), "tags": h.get("tags", [])}
                for h in (ctx.get("headlines") or [])[:12]
            ],
        }
        bundle["events"] = {
            "next_earnings": ctx.get("earnings_date"),
            "days_to_earnings": ctx.get("days_to_earnings"),
            "days_since_earnings": ctx.get("earnings_recent_days"),
        }
        bundle["social"] = {
            "score": ctx.get("social_score"),
            "messages": ctx.get("social_messages"),
            "bullish": ctx.get("social_bullish"),
            "bearish": ctx.get("social_bearish"),
            "sources": ctx.get("social_sources", []),
            "samples": (ctx.get("social_samples") or [])[:4],
        }
        bundle["options_flow"] = {
            "score": ctx.get("flow_score"),
            "unusual_activity": ctx.get("flow_unusual"),
            "call_premium": ctx.get("flow_call_premium"),
            "put_premium": ctx.get("flow_put_premium"),
            "bullish_premium": ctx.get("flow_bull_premium"),
            "bearish_premium": ctx.get("flow_bear_premium"),
            "call_volume_vs_30d": ctx.get("flow_call_vol_ratio"),
            "put_volume_vs_30d": ctx.get("flow_put_vol_ratio"),
            "option_volume_percentile": ctx.get("flow_opt_vol_pctile"),
            "unusual_trades": ctx.get("flow_alerts"),
            "bullish_trades": ctx.get("flow_bull_alerts"),
            "bearish_trades": ctx.get("flow_bear_alerts"),
            "sweeps": ctx.get("flow_sweeps"),
            "largest_trades": [
                {k: t.get(k) for k in ("when", "type", "strike", "expiry", "dte", "premium", "side", "direction", "sweep", "vol_oi", "otm_pct", "rule")}
                for t in (ctx.get("flow_trades") or [])[:8]
            ],
            "note": ctx.get("flow_note"),
        }
        edge = ctx.get("edge") or {}
        if edge:
            bundle["unusual_whales_edge"] = {
                "score": edge.get("score"),
                "threshold": edge.get("threshold"),
                "coverage": edge.get("coverage"),
                "min_coverage": edge.get("min_coverage"),
                "passed": edge.get("passed"),
                "features_answered": edge.get("answered"),
                "features_applicable": edge.get("applicable"),
                "unavailable": edge.get("missing", []),
                "features": {
                    name: {"score": r.get("score"), "weight": r.get("weight"), "value": r.get("value"), "error": r.get("error")}
                    for name, r in (edge.get("features") or {}).items()
                    if r.get("applicable", True)
                },
            }
        bundle["fundamentals"] = {
            "sector": ctx.get("sector"),
            "industry": ctx.get("industry"),
            "market_cap": ctx.get("market_cap"),
            "float_shares": ctx.get("float_shares"),
            "short_float_pct": ctx.get("short_float_pct"),
            "insider_transactions_pct": ctx.get("insider_trans_pct"),
            "institutional_ownership_pct": ctx.get("inst_own_pct"),
            "analyst_recommendation_1buy_5sell": ctx.get("analyst_recom"),
            "analyst_target_price": ctx.get("target_price"),
        }
        bundle["context_sources_unavailable"] = [k for k, v in (ctx.get("available") or {}).items() if not v]
    committee = plan.get("committee")
    if committee and "error" not in committee:
        bundle["committee_debate"] = {k: committee.get(k) for k in ("bull_case", "bear_case", "risk_review", "verdict", "confidence", "size_multiplier")}
    return bundle


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #
@dataclass
class Resolved:
    """One seat's request target: the provider it named (or ``auto`` picked),
    the model, and whether the call can be made at all."""

    provider: Provider | None
    model: str | None
    error: str | None = None  # why the seat cannot be used; None when it can

    @property
    def id(self) -> str:
        return self.provider.id if self.provider else "?"

    @property
    def kind(self) -> str:
        return self.provider.kind if self.provider else "?"

    @property
    def key(self) -> str | None:
        return self.provider.api_key if self.provider else None

    @property
    def base_url(self) -> str | None:
        return self.provider.base_url if self.provider else None

    @property
    def label(self) -> str:
        return self.provider.label if self.provider else "?"

    @property
    def configured(self) -> bool:
        return self.error is None

    def describe(self) -> str:
        """``label / model`` for status lines."""
        return f"{self.label} / {self.model or '(no model chosen)'}"


def resolve(cfg: Any) -> Resolved:
    """Where a seat's request goes: ``cfg.provider`` looked up in the active
    provider registry (``auto`` = first provider with a key), ``cfg.model``
    or the provider's default. A per-seat ``base_url`` override still
    applies to OpenAI-compatible providers. Never raises: an unusable seat
    comes back with ``error`` set."""
    reg = registry()
    pid = str(getattr(cfg, "provider", "auto") or "auto")
    try:
        prov = reg.resolve(pid)
    except KeyError as exc:
        return Resolved(None, getattr(cfg, "model", None), str(exc))
    override = getattr(cfg, "base_url", None)
    if override and prov.kind == "openai" and override.rstrip("/") != (prov.base_url or "").rstrip("/"):
        prov = Provider(**{**prov.__dict__, "base_url": override})
    model = getattr(cfg, "model", None) or prov.default_model
    if not prov.configured:
        return Resolved(prov, model, prov.missing())
    if not model:
        return Resolved(prov, None, f"no model chosen for {prov.label} - pick one on the settings page (Language models)")
    return Resolved(prov, model)


def resolve_provider(cfg: ReviewerSettings) -> tuple[str, str, str | None]:
    """(provider id, model, api_key) - the older shape; see :func:`resolve`."""
    r = resolve(cfg)
    return r.id, r.model or "", r.key


def provider_keys() -> dict[str, bool]:
    """Which providers are usable (a key saved, or a self-hosted endpoint that needs none)."""
    return {p.id: p.configured for p in registry().all()}


def openai_headers(key: str | None, base_url: str | None) -> dict[str, str]:
    """Bearer auth for OpenAI-compatible servers; Anthropic's endpoint also
    wants its own header pair, which the others ignore."""
    h = {"Authorization": f"Bearer {key or 'none'}", "Content-Type": "application/json"}
    if "anthropic.com" in (base_url or ""):
        h["x-api-key"] = key or ""
        h["anthropic-version"] = "2023-06-01"
    return h


def list_models(provider: str | Provider, base_url: str | None = None, timeout: int = 20) -> list[dict[str, Any]]:
    """The models the saved key can actually call, newest-looking first.

    Gemini: ``GET /v1beta/models`` filtered to those supporting
    ``generateContent``. OpenAI-compatible: ``GET {base}/models``. Raises on
    transport / key problems (callers redact before showing the error).
    """
    prov = provider if isinstance(provider, Provider) else registry().get(provider)
    if prov is None:
        raise RuntimeError(f"unknown AI provider '{provider}'")
    if prov.kind == "gemini":
        key = prov.api_key
        if not key:
            raise RuntimeError(f"no API key saved for {prov.label}")
        out: list[dict[str, Any]] = []
        token: str | None = None
        for _ in range(10):  # pages
            params: dict[str, Any] = {"pageSize": 200}
            if token:
                params["pageToken"] = token
            r = requests.get("https://generativelanguage.googleapis.com/v1beta/models", headers={"x-goog-api-key": key}, params=params, timeout=timeout)
            r.raise_for_status()
            data = r.json() or {}
            for m in data.get("models", []):
                if "generateContent" not in (m.get("supportedGenerationMethods") or []):
                    continue
                mid = str(m.get("name", "")).removeprefix("models/")
                if not mid:
                    continue
                out.append({"id": mid, "label": m.get("displayName") or mid, "note": (m.get("description") or "")[:160],
                            "input_tokens": m.get("inputTokenLimit"), "output_tokens": m.get("outputTokenLimit")})
            token = data.get("nextPageToken")
            if not token:
                break
        return sorted(out, key=_model_sort_key)
    base = (base_url or prov.base_url or "").rstrip("/")
    if not base:
        raise RuntimeError(f"{prov.label}: no endpoint URL")
    if not prov.api_key and prov.needs_key:
        raise RuntimeError(f"no API key saved for {prov.label}")
    r = requests.get(f"{base}/models", headers=openai_headers(prov.api_key, base), timeout=timeout)
    r.raise_for_status()
    rows = (r.json() or {}).get("data") or []
    out = [{"id": str(m.get("id")), "label": str(m.get("display_name") or m.get("id")), "note": str(m.get("owned_by") or "")} for m in rows if isinstance(m, dict) and m.get("id")]
    return sorted(out, key=_model_sort_key)


def probe_model(res: Resolved, timeout: int = 15) -> str:
    """One cheap request proving the seat can be used: the key works and the
    chosen model exists at that endpoint. Raises with a readable reason."""
    if not res.configured:
        raise RuntimeError(res.error or "not configured")
    prov = res.provider
    assert prov is not None
    if prov.kind == "gemini":
        # The key travels in a header, never in the URL, so a 404 for a retired
        # model name cannot echo it back in the error text.
        r = requests.get(f"https://generativelanguage.googleapis.com/v1beta/models/{res.model}", headers={"x-goog-api-key": prov.api_key}, timeout=timeout)
        if getattr(r, "status_code", None) == 404:
            raise RuntimeError(f"model '{res.model}' not found for this key - pick one from the list on the settings page (Language models card)")
        r.raise_for_status()
        return f"{prov.label}: model {res.model} reachable"
    models = list_models(prov, timeout=timeout)
    ids = [m["id"] for m in models]
    if res.model and ids and res.model not in ids:
        raise RuntimeError(f"model '{res.model}' is not among the {len(ids)} models {prov.label} lists - pick one from the list on the settings page (Language models card)")
    return f"{prov.label} answered ({len(ids)} models listed)"


def _model_sort_key(m: dict[str, Any]) -> tuple:
    """Stable, chat-first ordering: plain release names before previews / experiments,
    higher version numbers first, then alphabetical."""
    mid = m["id"].lower()
    preview = any(t in mid for t in ("preview", "exp", "latest", "tts", "embedding", "image", "audio", "live", "veo", "imagen", "aqa", "learnlm", "realtime", "transcribe", "moderation", "dall-e", "whisper", "davinci", "babbage", "lyria", "robotics", "computer-use", "deep-research", "antigravity", "banana"))
    # The provider's flagship chat family first (gemini-… / gpt-… / o…), then everything else it also serves.
    family = 0 if re.match(r"(gemini-|gpt-|o\d|chatgpt-)", mid) else 1
    nums = re.findall(r"\d+(?:\.\d+)?", mid)
    version = -float(nums[0]) if nums else 0.0
    return (preview, family, version, mid)


def _call_gemini(
    bundle: dict[str, Any], model: str, key: str, timeout: int,
    system_prompt: str = SYSTEM_PROMPT, schema: dict[str, Any] | None = None,
) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": json.dumps(bundle, default=str)[:60000]}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json", "responseSchema": schema or RESPONSE_SCHEMA},
    }
    # Key in a header, never in the URL: HTTP errors quote the URL they hit.
    from .llm_transport import post
    r = post(url, headers={"x-goog-api-key": key}, json=body, timeout=timeout)
    if getattr(r, "status_code", None) == 404:
        raise RuntimeError(f"Gemini model '{model}' not found for this key - pick one from the list on the settings page")
    r.raise_for_status()
    data = r.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


def _call_openai(
    bundle: dict[str, Any], model: str, key: str | None, base_url: str | None, timeout: int,
    system_prompt: str = SYSTEM_PROMPT,
) -> str:
    base = (base_url or "https://api.openai.com/v1").rstrip("/")
    body = {
        "model": model,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(bundle, default=str)[:60000]},
        ],
    }
    from .llm_transport import post
    r = post(f"{base}/chat/completions", headers=openai_headers(key, base), json=body, timeout=timeout)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


# --------------------------------------------------------------------------- #
# Verdict
# --------------------------------------------------------------------------- #
def normalise_verdict(raw: dict[str, Any] | str, provider: str, model: str) -> dict[str, Any]:
    """Coerce a model reply into the strict verdict shape; raise if it is not usable."""
    if isinstance(raw, str):
        text = raw.strip()
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < 0:
            raise ValueError("reviewer reply contains no JSON object")
        raw = json.loads(text[start : end + 1])  # tolerates ``` fences and chatter around the object
    if not isinstance(raw, dict):
        raise ValueError("reviewer reply is not a JSON object")
    action = str(raw.get("action", "")).strip().upper()
    if action not in ACTIONS:
        raise ValueError(f"reviewer action must be one of {ACTIONS}, got {action!r}")
    conf = float(raw.get("confidence", 0.0))
    if conf > 1.0:  # some models answer in percent
        conf = conf / 100.0
    conf = max(0.0, min(1.0, conf))
    mult = raw.get("sizeMultiplier", raw.get("size_multiplier", 1.0))
    try:
        mult = max(0.0, min(1.0, float(mult if mult is not None else 1.0)))
    except (TypeError, ValueError):
        mult = 1.0

    def _list(v: Any) -> list[str]:
        if isinstance(v, str):
            return [s.strip() for s in v.split(";") if s.strip()]
        return [str(x) for x in (v or [])][:8]

    return {
        "action": action,
        "confidence": round(conf, 3),
        "thesis": str(raw.get("thesis", "")).strip(),
        "catalysts": _list(raw.get("catalysts")),
        "risks": _list(raw.get("risks")),
        "invalidation": str(raw.get("invalidation", "")).strip(),
        "sizeNote": str(raw.get("sizeNote", raw.get("size_note", ""))).strip(),
        "sizeMultiplier": round(mult, 3),
        "provider": provider,
        "model": model,
    }


def ask_json(bundle: dict[str, Any], cfg: Any, system_prompt: str, schema: dict[str, Any] | None = None) -> tuple[str, str, str]:
    """Generic strict-JSON call on the reviewer transport.

    ``cfg`` is anything with ``provider`` / ``model`` / ``base_url`` /
    ``timeout_seconds`` (the reviewer settings or the insider-scan settings).
    Returns (raw_text, provider, model); raises on transport / key problems so
    callers decide how to record the failure. Nothing is retried or guessed.
    """
    res = resolve(cfg)
    if not res.configured:
        raise RuntimeError(res.error or "AI provider not configured")
    assert res.model is not None
    timeout = int(getattr(cfg, "timeout_seconds", 60) or 60)
    if res.kind == "gemini":
        return _call_gemini(bundle, res.model, res.key or "", timeout, system_prompt=system_prompt, schema=schema), res.id, res.model
    return _call_openai(bundle, res.model, res.key, res.base_url, timeout, system_prompt=system_prompt), res.id, res.model


def parse_json_object(text: str) -> dict[str, Any]:
    """The first JSON object in a model reply (tolerates ``` fences and chatter)."""
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("reply contains no JSON object")
    raw = json.loads(text[start : end + 1])
    if not isinstance(raw, dict):
        raise ValueError("reply is not a JSON object")
    return raw


def review_trade(bundle: dict[str, Any], cfg: ReviewerSettings) -> dict[str, Any]:
    """Send the edge bundle to the configured model. Never raises: errors come back as {"error": ...}."""
    provider, model, _ = resolve_provider(cfg)
    try:
        text, provider, model = ask_json(bundle, cfg, SYSTEM_PROMPT, RESPONSE_SCHEMA)
        return normalise_verdict(text, provider, model)
    except Exception as exc:
        log.warning("LLM reviewer failed for %s: %s", bundle.get("symbol"), describe_error(exc))
        return {"error": describe_error(exc), "provider": provider, "model": model}


def verdict_allows_trade(verdict: dict[str, Any] | None, cfg: ReviewerSettings) -> tuple[bool, str]:
    """Apply ``mode`` / ``min_confidence`` / ``fail_closed``. Returns (allowed, reason)."""
    if not cfg.enabled or cfg.mode == "advisory":
        return True, "advisory"
    if verdict is None or "error" in (verdict or {}):
        return (not cfg.fail_closed), ("reviewer unavailable, fail-closed" if cfg.fail_closed else "reviewer unavailable, fail-open")
    if verdict["action"] != "BUY":
        return False, f"reviewer says {verdict['action']} ({verdict['confidence']:.0%})"
    if verdict["confidence"] < cfg.min_confidence:
        return False, f"reviewer confidence {verdict['confidence']:.0%} < {cfg.min_confidence:.0%}"
    return True, f"reviewer BUY ({verdict['confidence']:.0%})"
