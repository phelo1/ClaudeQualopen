"""AI providers: any number of vendors, each with its own key and endpoint,
picked per seat on the settings page."""

import os
import json
import stat

import pytest
import yaml
from fastapi.testclient import TestClient

from qmag import providers, reviewer
from qmag.config import CommitteeSettings, ReviewerSettings
from qmag.dashboard import create_app
from qmag.providers import BUILTIN_IDS, PRESETS, ProviderStore, registry_from
from qmag.redact import redact_secrets
from qmag.session import SessionSettings, TradingSession


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "QMAG_LLM_API_KEY", "QMAG_LLM_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    providers.set_active(None)
    yield
    providers.set_active(None)


def _session(tmp_path) -> TradingSession:
    return TradingSession(SessionSettings(data="yfinance", state_dir=tmp_path / "state", charts=False))


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
def test_builtins_always_exist_and_fall_back_to_the_environment():
    reg = registry_from([], env={})
    assert reg.ids()[:2] == list(BUILTIN_IDS) and reg.choices()[0] == "auto"
    g, o = reg.get("gemini"), reg.get("openai")
    assert g.builtin and o.builtin and not g.configured and not o.configured
    assert "no API key saved for Google Gemini" in g.missing()
    assert reg.auto().id == "gemini"  # nothing keyed: the error names a concrete provider

    reg = registry_from([], env={"GOOGLE_API_KEY": "g-env-key", "QMAG_LLM_API_KEY": "o-env-key", "QMAG_LLM_BASE_URL": "http://box:11434/v1"})
    assert reg.get("gemini").api_key == "g-env-key" and reg.get("gemini").key_source == "environment"
    assert reg.get("openai").base_url == "http://box:11434/v1" and reg.get("openai").api_key == "o-env-key"
    assert reg.auto().id == "gemini" and len(reg.configured()) == 2 and set(reg.secret_values()) == {"g-env-key", "o-env-key"}
    # a saved key beats the environment, and the saved row is reported as such
    reg = registry_from([{"id": "gemini", "kind": "gemini", "api_key": "g-saved"}], env={"GEMINI_API_KEY": "g-env"})
    assert reg.get("gemini").api_key == "g-saved" and reg.get("gemini").key_source == "saved"


def test_self_hosted_endpoints_need_no_key_hosted_vendors_do():
    rows = [
        {"id": "ollama", "label": "Ollama", "kind": "openai", "base_url": "http://127.0.0.1:11434/v1"},
        {"id": "xai", "label": "xAI Grok", "kind": "openai", "base_url": "https://api.x.ai/v1", "preset": "xai"},
        {"id": "anthropic", "label": "Anthropic", "kind": "openai", "base_url": "https://api.anthropic.com/v1", "api_key": "sk-ant-1", "preset": "anthropic"},
    ]
    reg = registry_from(rows, env={})
    assert reg.get("ollama").configured and not reg.get("ollama").needs_key
    assert reg.get("xai").needs_key and not reg.get("xai").configured and "no API key saved for xAI Grok" in reg.get("xai").missing()
    assert reg.get("anthropic").configured and reg.auto().id == "ollama"  # built-ins first, then saved order
    pub = reg.get("anthropic").public()
    assert pub["has_key"] and "sk-ant-1" not in json.dumps(pub) and pub["keys_at"] == PRESETS["anthropic"]["keys_at"]
    with pytest.raises(KeyError, match="nope"):
        reg.resolve("nope")


def test_resolve_per_seat_provider_and_model(monkeypatch):
    providers.set_active([
        {"id": "anthropic", "label": "Anthropic Claude", "kind": "openai", "base_url": "https://api.anthropic.com/v1", "api_key": "sk-ant-1"},
        {"id": "ollama", "label": "Ollama", "kind": "openai", "base_url": "http://127.0.0.1:11434/v1", "default_model": "llama3"},
    ])
    r = reviewer.resolve(ReviewerSettings(provider="anthropic", model="claude-sonnet-4-5"))
    assert r.configured and r.kind == "openai" and r.key == "sk-ant-1" and r.describe() == "Anthropic Claude / claude-sonnet-4-5"
    assert reviewer.openai_headers(r.key, r.base_url)["x-api-key"] == "sk-ant-1" and "anthropic-version" in reviewer.openai_headers(r.key, r.base_url)
    assert "x-api-key" not in reviewer.openai_headers("k", "https://api.x.ai/v1")
    # no model chosen on a provider without a default: not usable, says why
    r = reviewer.resolve(ReviewerSettings(provider="anthropic"))
    assert not r.configured and "no model chosen for Anthropic Claude" in r.error
    # provider default model fills in; a per-seat base_url override still applies
    r = reviewer.resolve(ReviewerSettings(provider="ollama"))
    assert r.configured and r.model == "llama3" and r.key is None
    r = reviewer.resolve(ReviewerSettings(provider="ollama", model="m", base_url="http://other:8000/v1/"))
    assert r.base_url == "http://other:8000/v1/"
    r = reviewer.resolve(ReviewerSettings(provider="gone"))
    assert not r.configured and r.provider is None and "gone" in r.error
    # the committee seats each resolve on their own provider
    com = CommitteeSettings(enabled=True, bull_provider="ollama", bear_provider="anthropic", bear_model="claude-x", risk_provider="xai")
    assert reviewer.resolve(type("S", (), {"provider": com.bull_provider, "model": com.bull_model})).configured
    assert reviewer.resolve(type("S", (), {"provider": com.bear_provider, "model": com.bear_model})).describe() == "Anthropic Claude / claude-x"
    assert "xai" in reviewer.resolve(type("S", (), {"provider": com.risk_provider, "model": None})).error


def test_ask_json_sends_to_the_seat_provider(monkeypatch):
    providers.set_active([{"id": "groq", "label": "Groq", "kind": "openai", "base_url": "https://api.groq.com/openai/v1", "api_key": "gsk-1"}])
    seen = {}

    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": json.dumps({"action": "HOLD", "confidence": 0.5, "reasons": ["x"], "adjustments": {}})}}]}

    def fake_post(url, headers=None, json=None, timeout=None, **kw):
        seen.update(url=url, headers=headers, body=json)
        return _R()

    monkeypatch.setattr(reviewer.requests, "post", fake_post)
    v = reviewer.review_trade({"symbol": "ABC", "plan": {}}, ReviewerSettings(enabled=True, provider="groq", model="llama-3.3-70b"))
    assert v["action"] == "HOLD" and v["provider"] == "groq" and v["model"] == "llama-3.3-70b"
    assert seen["url"] == "https://api.groq.com/openai/v1/chat/completions" and seen["headers"]["Authorization"] == "Bearer gsk-1"
    assert seen["body"]["model"] == "llama-3.3-70b"
    with pytest.raises(RuntimeError, match="no API key saved for OpenAI"):
        reviewer.ask_json({"symbol": "ABC"}, ReviewerSettings(provider="openai"), "system")


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #
def test_store_upsert_delete_export_import_round_trip(tmp_path):
    store = ProviderStore(tmp_path)
    prov, notes = store.upsert({"preset": "anthropic", "api_key": "sk-ant-secret"})
    assert prov.id == "anthropic" and prov.base_url == "https://api.anthropic.com/v1" and prov.configured and notes == ["Anthropic Claude: key saved"]
    assert store.path.exists() and (os.name == "nt" or stat.S_IMODE(store.path.stat().st_mode) == 0o600)
    assert providers.registry().get("anthropic").api_key == "sk-ant-secret"  # activated process-wide

    # blank key keeps the saved one; a masked echo is never stored as a key
    prov, _ = store.upsert({"id": "anthropic", "label": "Claude (bear seat)", "api_key": "••••"})
    assert prov.api_key == "sk-ant-secret" and prov.label == "Claude (bear seat)"
    prov, notes = store.upsert({"id": "anthropic", "clear_key": "1"})
    assert prov.api_key is None and not prov.configured and "forgotten" in notes[0]

    # custom endpoint: needs a URL, http(s) only, no key required for a LAN box
    with pytest.raises(ValueError, match="endpoint URL is required"):
        store.upsert({"preset": "custom", "label": "Lab box"})
    with pytest.raises(ValueError, match="start with http"):
        store.upsert({"preset": "custom", "label": "Lab box", "base_url": "box:8000/v1"})
    prov, _ = store.upsert({"preset": "custom", "label": "Lab box", "base_url": "http://box:8000/v1", "default_model": "qwen"})
    assert prov.id == "lab-box" and prov.configured and prov.default_model == "qwen" and not prov.needs_key
    with pytest.raises(ValueError, match="already exists"):
        store.upsert({"preset": "custom", "label": "Lab box", "base_url": "http://x/v1"})
    with pytest.raises(ValueError, match="give the provider a name"):
        store.upsert({"preset": "custom", "base_url": "http://x/v1"})

    # built-ins can be keyed but not removed
    prov, _ = store.upsert({"id": "openai", "api_key": "sk-openai-1"})
    assert prov.builtin and prov.configured and prov.base_url == "https://api.openai.com/v1"
    with pytest.raises(ValueError, match="built in"):
        store.delete("openai")
    assert store.delete("lab-box") is True and store.delete("lab-box") is False
    assert [r["id"] for r in store.load_rows()] == ["anthropic", "openai"]

    # export without secrets drops the keys; import merges and can carry them
    plain = store.export_rows(include_secrets=False)
    assert all("api_key" not in r for r in plain) and {r["id"] for r in plain} == {"anthropic", "openai"}
    full = store.export_rows(include_secrets=True)
    assert next(r for r in full if r["id"] == "openai")["api_key"] == "sk-openai-1"
    other = ProviderStore(tmp_path / "other")
    assert other.import_rows(full, include_secrets=False) == ["anthropic", "openai"]
    assert other.registry().get("openai").api_key is None
    other.import_rows([{"id": "xai", "label": "xAI", "kind": "openai", "base_url": "https://api.x.ai/v1", "api_key": "xk"}])
    assert other.registry().get("xai").configured and other.registry().get("xai").key_source == "saved"


def test_provider_keys_are_redacted_everywhere(tmp_path):
    store = ProviderStore(tmp_path)
    store.upsert({"preset": "xai", "api_key": "xai-secret-key-value-0123456789"})
    leaky = "HTTP 401 for https://api.x.ai/v1/models with Bearer xai-secret-key-value-0123456789"
    assert "xai-secret-key-value" not in redact_secrets(leaky)


# --------------------------------------------------------------------------- #
# settings page + API
# --------------------------------------------------------------------------- #
def test_settings_page_manages_providers_and_seats_pick_them(tmp_path, monkeypatch):
    sess = _session(tmp_path)
    client = TestClient(create_app(sess))
    page = client.get("/settings").text
    assert "AI providers" in page and 'data-id="gemini"' in page and 'data-id="openai"' in page and 'id="add-provider"' in page
    for label in ("Anthropic Claude", "xAI Grok", "Groq", "OpenRouter", "DeepSeek", "Mistral", "Together", "Ollama"):
        assert label in page

    # add xAI for the bear seat
    r = client.post("/settings/providers", data={"preset": "xai", "api_key": "xai-key-1"}, follow_redirects=False)
    assert r.status_code == 303 and "saved=providers" in r.headers["location"]
    api = client.get("/api/llm/providers").json()
    x = next(p for p in api["providers"] if p["id"] == "xai")
    assert x["configured"] and x["has_key"] and "xai-key-1" not in json.dumps(api) and x["endpoint"] == "https://api.x.ai/v1"
    page = client.get("/settings?saved=providers").text
    assert "AI provider saved" in page and 'data-id="xai"' in page and 'name="committee.bear_provider"' in page and 'value="xai"' in page
    assert sess.providers.path.exists() and (os.name == "nt" or stat.S_IMODE(sess.providers.path.stat().st_mode) == 0o600)

    # the seat can now choose it, and the whole thing resolves
    r = client.post("/settings/strategy", data={"committee.bear_provider": "xai", "committee.bear_model": "grok-4"}, follow_redirects=False)
    assert r.status_code == 303
    assert sess.cfg.committee.bear_provider == "xai" and sess.cfg.committee.bear_model == "grok-4"
    res = reviewer.resolve(type("S", (), {"provider": "xai", "model": "grok-4"}))
    assert res.configured and res.key == "xai-key-1"
    assert "xai" in client.get("/api/settings").json()["strategy"]["committee"]["bear_provider"]

    # the models API lists per provider and never leaks the key
    def fake_get(url, headers=None, params=None, timeout=None, **kw):
        class _R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"data": [{"id": "grok-4"}, {"id": "grok-3-mini"}]}
        assert headers["Authorization"] == "Bearer xai-key-1" and url.startswith("https://api.x.ai/v1/models")
        return _R()

    monkeypatch.setattr(reviewer.requests, "get", fake_get)
    m = client.get("/api/llm/models?provider=xai&refresh=1").json()
    assert m["providers"]["xai"]["ok"] and [x["id"] for x in m["providers"]["xai"]["models"]] == ["grok-4", "grok-3-mini"]
    assert m["keys"]["xai"] is True and m["keys"]["gemini"] is False and m["auto"] == "xai"
    assert "unknown provider" in client.get("/api/llm/models?provider=nope").json()["error"]

    # bad input is refused with a message; built-ins cannot be removed; others can
    r = client.post("/settings/providers", data={"preset": "custom", "label": "Box", "base_url": "ftp://x"}, follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]
    r = client.post("/settings/providers/delete", data={"id": "gemini"}, follow_redirects=False)
    assert "error=" in r.headers["location"]
    r = client.post("/settings/providers/delete", data={"id": "xai"}, follow_redirects=False)
    assert "saved=providers" in r.headers["location"]
    assert "xai" not in {p["id"] for p in client.get("/api/llm/providers").json()["providers"]}
    page = client.get("/settings").text
    assert "removed" in page  # the bear seat still names xai; the page says so instead of silently switching


def test_settings_bundle_carries_providers(tmp_path):
    sess = _session(tmp_path)
    client = TestClient(create_app(sess))
    client.post("/settings/providers", data={"preset": "deepseek", "api_key": "ds-secret-1"}, follow_redirects=False)
    plain_text = client.get("/settings/export").text
    plain = yaml.safe_load(plain_text)
    assert [r["id"] for r in plain["ai_providers"]] == ["deepseek"] and "ds-secret-1" not in plain_text
    full_text = client.get("/settings/export?secrets=1").text
    assert yaml.safe_load(full_text)["ai_providers"][0]["api_key"] == "ds-secret-1"

    other = _session(tmp_path / "two")
    client2 = TestClient(create_app(other))
    r = client2.post("/settings/import", data={"include_secrets": "1"}, files={"file": ("bundle.yaml", full_text, "application/x-yaml")}, follow_redirects=False)
    assert r.status_code == 303, r.text
    assert other.providers.registry().get("deepseek").api_key == "ds-secret-1"
