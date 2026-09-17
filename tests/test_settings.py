"""Settings page: save / download / upload strategy parameters and API keys."""

import os
import stat

import pytest
import yaml
from fastapi.testclient import TestClient

from qmag.config import DEFAULT_EDGE_WEIGHTS, StrategyConfig
from qmag.dashboard import create_app
from qmag.session import SessionSettings, TradingSession
from qmag.settings import (
    CLEAR_TOKEN,
    ENV_FIELDS,
    SECRET_NAMES,
    SettingsStore,
    config_from_form,
    config_from_yaml,
    describe_config,
    dump_env,
    mask,
    parse_env,
    quick_fields,
)

KEY = "uw-live-key-ABCDEFGH1234"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for f in ENV_FIELDS:
        monkeypatch.delenv(f.name, raising=False)


def _session(tmp_path, **kw) -> TradingSession:
    kw.setdefault("overrides", {"context.enabled": False, "regime.enabled": False})
    return TradingSession(SessionSettings(data="yfinance", state_dir=tmp_path / "state", charts=False, **kw))


# --------------------------------------------------------------------------- #
# store primitives
# --------------------------------------------------------------------------- #
def test_env_file_roundtrip_and_permissions(tmp_path):
    store = SettingsStore(tmp_path)
    path = store.save_env({"UNUSUAL_WHALES_API_KEY": KEY, "IBKR_PORT": "4002", "REDDIT_USER_AGENT": "qmag by me #1", "EMPTY": ""})
    assert path == tmp_path / "settings.env"
    assert (os.name == "nt" or stat.S_IMODE(path.stat().st_mode) == 0o600)
    text = path.read_text()
    assert "EMPTY" not in text and 'REDDIT_USER_AGENT="qmag by me #1"' in text
    assert store.load_env() == {"UNUSUAL_WHALES_API_KEY": KEY, "IBKR_PORT": "4002", "REDDIT_USER_AGENT": "qmag by me #1"}
    assert parse_env("export A='x y'\n# comment\nB=1\nbad line\n") == {"A": "x y", "B": "1"}
    assert dump_env({}).endswith("\n")


def test_apply_env_exports_saved_values_and_unsets_cleared_ones(tmp_path):
    store = SettingsStore(tmp_path)
    store.save_env({"UNUSUAL_WHALES_API_KEY": KEY})
    store.apply_env()
    assert os.environ["UNUSUAL_WHALES_API_KEY"] == KEY
    store.save_env({})
    store.apply_env()
    assert "UNUSUAL_WHALES_API_KEY" not in os.environ
    # A value inherited from the shell is left alone when the file never held it.
    os.environ["ALPACA_API_KEY"] = "from-shell"
    store.apply_env()
    assert os.environ["ALPACA_API_KEY"] == "from-shell"
    view = {r["name"]: r for r in store.env_view()}
    assert view["ALPACA_API_KEY"]["source"] == "environment" and view["ALPACA_API_KEY"]["display"] == mask("from-shell")
    assert view["UNUSUAL_WHALES_API_KEY"]["source"] == "unset" and view["UNUSUAL_WHALES_API_KEY"]["display"] == ""
    del os.environ["ALPACA_API_KEY"]


def test_mask_never_reveals_short_secrets():
    assert mask("") == "" and mask("short") == "••••" and mask(KEY) == "••••1234"


def test_form_merge_keeps_secrets_when_blank_and_clears_on_request(tmp_path):
    store = SettingsStore(tmp_path)
    store.save_env({"UNUSUAL_WHALES_API_KEY": KEY, "IBKR_PORT": "7497"})
    values, changed = store.update_env_from_form({"UNUSUAL_WHALES_API_KEY": "", "IBKR_PORT": "4002", "QMAG_DATA": "yfinance"})
    assert values["UNUSUAL_WHALES_API_KEY"] == KEY and values["IBKR_PORT"] == "4002" and values["QMAG_DATA"] == "yfinance"
    assert sorted(changed) == ["IBKR_PORT", "QMAG_DATA"]
    values, changed = store.update_env_from_form({"UNUSUAL_WHALES_API_KEY": "••••1234"})  # the mask itself is never saved
    assert values["UNUSUAL_WHALES_API_KEY"] == KEY and changed == []
    values, changed = store.update_env_from_form({"UNUSUAL_WHALES_API_KEY__clear": "1", "IBKR_PORT": ""})
    assert "UNUSUAL_WHALES_API_KEY" not in values and "IBKR_PORT" not in values and set(changed) == {"UNUSUAL_WHALES_API_KEY", "IBKR_PORT"}
    values, _ = store.update_env_from_form({"UNUSUAL_WHALES_API_KEY": CLEAR_TOKEN})
    assert "UNUSUAL_WHALES_API_KEY" not in values
    with pytest.raises(ValueError, match="not a number"):
        store.update_env_from_form({"IBKR_PORT": "abc"})
    with pytest.raises(ValueError, match="not one of"):
        store.update_env_from_form({"QMAG_DATA": "synthetic"})


# --------------------------------------------------------------------------- #
# strategy form
# --------------------------------------------------------------------------- #
def test_describe_config_covers_every_field_and_quick_fields_exist():
    cfg = StrategyConfig().with_overrides({"edge.threshold": 0.3})
    sections = describe_config(cfg)
    assert [s["name"] for s in sections] == list(cfg.to_dict())
    total = sum(len(s["fields"]) for s in sections)
    assert total == sum(len(v) for v in cfg.to_dict().values())
    edge = next(s for s in sections if s["name"] == "edge")
    assert edge["changed"] == 1 and next(f for f in edge["fields"] if f["name"] == "threshold")["changed"]
    weights = next(f for f in edge["fields"] if f["name"] == "weights")
    assert weights["kind"] == "weights" and weights["value"] == DEFAULT_EDGE_WEIGHTS
    assert all("section" in q for q in quick_fields(sections)) and len(quick_fields(sections)) >= 10
    assert all(s["doc"] and "(" not in s["doc"][:15] for s in sections)  # prose, never a dataclass repr


def test_config_from_form_coerces_types_and_validates():
    base = StrategyConfig()
    cfg = config_from_form(
        {
            "risk.max_positions": "5", "edge.threshold": "0.25", "regime.enabled": "0", "edge.gate": "1",
            "management.partial_target_r": "", "themes.apply_to": "breakout, episodic_pivot", "edge.weights.flow": "2",
            "edge.weights.congress": "", "themes.groups": "semis: [NVDA, AMD]", "reviewer.mode": "gate",
        },
        base,
    )
    assert cfg.risk.max_positions == 5 and cfg.edge.threshold == 0.25 and cfg.regime.enabled is False and cfg.edge.gate is True
    assert cfg.management.partial_target_r is None and cfg.themes.apply_to == ["breakout", "episodic_pivot"]
    assert cfg.edge.weights["flow"] == 2.0 and cfg.edge.weights["congress"] == 0.0 and cfg.edge.weights["gamma"] == DEFAULT_EDGE_WEIGHTS["gamma"]
    assert cfg.themes.groups == {"semis": ["NVDA", "AMD"]} and cfg.reviewer.mode == "gate"
    assert cfg.momentum == base.momentum  # untouched sections keep their values
    with pytest.raises(ValueError, match="not a number"):
        config_from_form({"risk.max_positions": "many"}, base)
    with pytest.raises(ValueError, match="max_positions must be at least 1"):
        config_from_form({"risk.max_positions": "0"}, base)
    with pytest.raises(ValueError, match="threshold must be between"):
        config_from_form({"edge.threshold": "3"}, base)
    with pytest.raises(ValueError, match="weights cannot be negative"):
        config_from_form({"edge.weights.flow": "-1"}, base)
    with pytest.raises(ValueError, match="not valid YAML"):
        config_from_yaml("risk: [unclosed")
    with pytest.raises(ValueError, match="reviewer.mode must be"):
        config_from_yaml("reviewer:\n  mode: yolo\n")


# --------------------------------------------------------------------------- #
# session integration
# --------------------------------------------------------------------------- #
def test_session_loads_saved_settings_and_reloads_when_files_change(tmp_path):
    state = tmp_path / "state"
    store = SettingsStore(state)
    store.save_config(StrategyConfig().with_overrides({"risk.max_positions": 3}))
    store.save_env({"UNUSUAL_WHALES_API_KEY": KEY, "QMAG_DATA": "unusual_whales"})
    sess = TradingSession(SessionSettings(data="auto", state_dir=state, charts=False, overrides={"context.enabled": False}))
    assert sess.cfg.risk.max_positions == 3 and sess.s.data == "unusual_whales" and os.environ["UNUSUAL_WHALES_API_KEY"] == KEY
    assert sess.config_source == str(store.yaml_path)
    # Another process edits the files: the next reload picks both up.
    store.save_config(StrategyConfig().with_overrides({"risk.max_positions": 4}))
    store.save_env({"QMAG_DATA": "yfinance"})
    assert sess.reload_settings() is True
    assert sess.cfg.risk.max_positions == 4 and sess.s.data == "yfinance" and "UNUSUAL_WHALES_API_KEY" not in os.environ
    assert sess.reload_settings() is False  # nothing changed since
    # CLI overrides still apply on top of the saved file.
    sess2 = TradingSession(SessionSettings(data="yfinance", state_dir=state, charts=False, overrides={"context.enabled": False, "risk.max_positions": 9}))
    assert sess2.cfg.risk.max_positions == 9


def test_saved_yaml_takes_over_from_cli_config(tmp_path):
    cli_cfg = tmp_path / "cli.yaml"
    StrategyConfig().with_overrides({"risk.max_positions": 2}).save(cli_cfg)
    sess = _session(tmp_path, config=cli_cfg)
    assert sess.cfg.risk.max_positions == 2 and sess.config_source == str(cli_cfg)
    sess.store.save_config(StrategyConfig().with_overrides({"risk.max_positions": 6}))
    sess.reload_settings()
    assert sess.cfg.risk.max_positions == 6 and sess.config_source == str(sess.store.yaml_path)
    sess.store.reset_config()
    sess.reload_settings()
    assert sess.cfg.risk.max_positions == 2


# --------------------------------------------------------------------------- #
# dashboard
# --------------------------------------------------------------------------- #
def test_settings_page_is_linked_and_renders(tmp_path):
    sess = _session(tmp_path)
    client = TestClient(create_app(sess))
    home = client.get("/")
    assert 'href="/settings"' in home.text and "settings" in home.text
    assert 'href="/settings"' in client.get("/status").text
    page = client.get("/settings")
    assert page.status_code == 200
    for text in ("Quick settings", "Connections &amp; API keys", "Download &amp; upload", "All strategy parameters", "Advanced: strategy as YAML",
                 "UNUSUAL_WHALES_API_KEY", "ALPACA_SECRET_KEY", "AI providers", "Google Gemini", "Find a parameter", "built-in defaults"):
        assert text in page.text, text
    api = client.get("/api/settings").json()
    assert api["strategy"]["risk"]["max_positions"] == 8 and {r["name"] for r in api["connections"]} == {f.name for f in ENV_FIELDS}


def test_language_models_card_and_live_model_list(tmp_path, monkeypatch):
    """Each LLM use has its own provider + model field on one card, and the model
    field is fed by the models the saved key can actually call."""
    from qmag import reviewer

    sess = _session(tmp_path)
    client = TestClient(create_app(sess))
    page = client.get("/settings").text
    for text in ("Language models", "Trade reviewer", "Insider-flow analyst", "Post-mortem coach", "Analyst committee",
                 'name="reviewer.model"', 'name="insider_scan.model"', 'name="learning.model"', 'name="committee.bear_model"', 'name="reviewer.provider"',
                 "/api/llm/models", "no key", "/settings/providers", 'id="add-provider"', "Anthropic"):
        assert text in page, text

    # No keys: the API says so without calling anyone.
    r = client.get("/api/llm/models").json()
    assert r["keys"] == {"gemini": False, "openai": False}
    assert r["providers"]["gemini"]["ok"] is False and "no API key saved for Google Gemini" in r["providers"]["gemini"]["error"]

    # Gemini key saved: /v1beta/models is read with the key in a header and filtered to generateContent models.
    key = "AQ.Ab8RN6K_secret_secret_secret_0123456789"
    seen = {}

    class _R:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    def fake_get(url, headers=None, params=None, timeout=None, **kw):
        seen.update(url=url, headers=headers, params=params)
        if "generativelanguage" in url:
            return _R({"models": [
                {"name": "models/gemini-3.5-flash", "displayName": "Gemini 3.5 Flash", "supportedGenerationMethods": ["generateContent"], "inputTokenLimit": 1000000},
                {"name": "models/gemini-3.1-pro-preview", "displayName": "Gemini 3.1 Pro Preview", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/embedding-001", "supportedGenerationMethods": ["embedContent"]},
                {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
            ]})
        return _R({"data": [{"id": "gpt-4o-mini", "owned_by": "openai"}, {"id": "gpt-5.6", "owned_by": "openai"}]})

    monkeypatch.setattr(reviewer.requests, "get", fake_get)
    sess.store.save_env({"GEMINI_API_KEY": key})
    r = client.get("/api/llm/models?refresh=1").json()
    assert r["keys"]["gemini"] is True
    g = r["providers"]["gemini"]
    assert g["ok"] and [m["id"] for m in g["models"]] == ["gemini-3.5-flash", "gemini-2.5-flash", "gemini-3.1-pro-preview"] and g["default"] == "gemini-3.5-flash"
    assert g["label"] == "Google Gemini" and r["auto"] == "gemini"
    assert seen["headers"] == {"x-goog-api-key": key} and key not in seen["url"] and "key" not in (seen["params"] or {})
    assert r["providers"]["openai"]["ok"] is False  # still no OpenAI key

    # A failing provider reports a redacted error and never raises.
    def boom(url, **kw):
        raise RuntimeError(f"denied for url ?key={key}")

    monkeypatch.setattr(reviewer.requests, "get", boom)
    sess.store.save_env({"GEMINI_API_KEY": key, "OPENAI_API_KEY": "sk-proj-openai_secret_secret_secret"})
    r = client.get("/api/llm/models?refresh=1").json()
    assert not r["providers"]["gemini"]["ok"] and key not in r["providers"]["gemini"]["error"] and "denied" in r["providers"]["gemini"]["error"]
    assert not r["providers"]["openai"]["ok"] and "sk-proj" not in r["providers"]["openai"]["error"]

    # Saving from the card writes the per-use provider / model.
    r = client.post("/settings/strategy", data={"reviewer.provider": "gemini", "reviewer.model": "gemini-3.5-flash", "insider_scan.model": "gpt-5.6", "insider_scan.provider": "openai"}, follow_redirects=False)
    assert r.status_code == 303
    saved = yaml.safe_load(sess.store.yaml_path.read_text())
    assert saved["reviewer"]["model"] == "gemini-3.5-flash" and saved["insider_scan"] == {"model": "gpt-5.6", "provider": "openai"} or saved["insider_scan"]["model"] == "gpt-5.6"
    assert sess.cfg.reviewer.model == "gemini-3.5-flash" and sess.cfg.insider_scan.provider == "openai"


def test_save_quick_settings_and_all_parameters(tmp_path):
    sess = _session(tmp_path)
    client = TestClient(create_app(sess))
    r = client.post("/settings/strategy", data={"edge.threshold": "0.3", "risk.max_positions": "5", "edge.gate": ["0", "0"], "reviewer.fail_closed": ["0", "1"]}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings?saved=strategy"
    assert sess.cfg.edge.threshold == 0.3 and sess.cfg.risk.max_positions == 5 and sess.cfg.edge.gate is False and sess.cfg.reviewer.fail_closed is True
    # Command-line overrides (here regime.enabled=False) stay on top of the saved file and are listed on the page.
    r2 = client.post("/settings/strategy", data={"regime.enabled": ["0", "1"]}, follow_redirects=False)
    assert yaml.safe_load(sess.store.yaml_path.read_text())["regime"]["enabled"] is True and sess.cfg.regime.enabled is False
    assert "regime.enabled" in client.get("/settings").text
    saved = yaml.safe_load(sess.store.yaml_path.read_text())
    assert saved["edge"]["threshold"] == 0.3 and saved["risk"]["max_positions"] == 5
    page = client.get("/settings?saved=strategy")
    assert "Strategy saved" in page.text and 'value="0.3"' in page.text and "uw edge · no key · advisory" in page.text
    # Invalid input: nothing written, error shown.
    r = client.post("/settings/strategy", data={"risk.risk_per_trade_pct": "0.5"}, follow_redirects=False)
    assert r.status_code == 303 and "risk_per_trade_pct" in r.headers["location"]
    assert sess.cfg.risk.risk_per_trade_pct == 0.005 and yaml.safe_load(sess.store.yaml_path.read_text())["risk"]["risk_per_trade_pct"] == 0.005
    assert "Strategy not saved" in client.get(r.headers["location"]).text
    # Weights grid.
    r = client.post("/settings/strategy", data={"edge.weights.flow": "3", "edge.weights.congress": "0"}, follow_redirects=False)
    assert sess.cfg.edge.weights["flow"] == 3.0 and sess.cfg.edge.weights["congress"] == 0.0
    # Reset deletes the file and returns to defaults.
    r = client.post("/settings/reset", follow_redirects=False)
    assert r.status_code == 303 and not sess.store.yaml_path.exists() and sess.cfg == StrategyConfig().with_overrides({"context.enabled": False, "regime.enabled": False})


def test_save_yaml_validates_and_applies(tmp_path):
    sess = _session(tmp_path)
    client = TestClient(create_app(sess))
    bad = client.post("/settings/yaml", data={"yaml": "risk:\n  max_positions: 0\n"})
    assert bad.status_code == 400 and "max_positions must be at least 1" in bad.text and "max_positions: 0" in bad.text
    assert not sess.store.yaml_path.exists()
    ok = client.post("/settings/yaml", data={"yaml": "risk:\n  max_positions: 3\nedge:\n  weights:\n    flow: 2.5\n"})
    assert ok.status_code == 200 and "Strategy YAML saved" in ok.text
    assert sess.cfg.risk.max_positions == 3 and sess.cfg.edge.weights["flow"] == 2.5 and sess.cfg.edge.weights["gamma"] == DEFAULT_EDGE_WEIGHTS["gamma"]


def test_save_connections_masks_secrets_and_switches_data_source(tmp_path):
    sess = TradingSession(SessionSettings(data="auto", state_dir=tmp_path / "state", charts=False, overrides={"context.enabled": False, "regime.enabled": False}))
    assert sess.s.data == "yfinance"
    client = TestClient(create_app(sess))
    r = client.post("/settings/connections", data={"UNUSUAL_WHALES_API_KEY": KEY, "QMAG_DATA": "auto", "IBKR_PORT": "4002", "ALPACA_API_KEY": ""}, follow_redirects=False)
    assert r.status_code == 303 and "saved=connections" in r.headers["location"]
    env_path = sess.store.env_path
    assert (os.name == "nt" or stat.S_IMODE(env_path.stat().st_mode) == 0o600) and f"UNUSUAL_WHALES_API_KEY={KEY}" in env_path.read_text()
    assert os.environ["UNUSUAL_WHALES_API_KEY"] == KEY and sess.s.data == "unusual_whales"  # auto now resolves to Unusual Whales
    page = client.get(r.headers["location"])
    assert KEY not in page.text and "••••1234" in page.text and "unusual_whales · paper" in page.text and "uw edge · on" in page.text
    assert "updated IBKR_PORT, QMAG_DATA, UNUSUAL_WHALES_API_KEY" in page.text.replace("&amp;", "&") or "updated" in page.text
    # Blank secret keeps the key; the forget box removes it and the source drops back to Yahoo.
    client.post("/settings/connections", data={"UNUSUAL_WHALES_API_KEY": "", "IBKR_PORT": "4002"}, follow_redirects=False)
    assert os.environ["UNUSUAL_WHALES_API_KEY"] == KEY
    client.post("/settings/connections", data={"UNUSUAL_WHALES_API_KEY__clear": "1"}, follow_redirects=False)
    assert "UNUSUAL_WHALES_API_KEY" not in os.environ and "UNUSUAL_WHALES_API_KEY" not in env_path.read_text() and sess.s.data == "yfinance"
    # The API never returns the secret.
    client.post("/settings/connections", data={"UNUSUAL_WHALES_API_KEY": KEY}, follow_redirects=False)
    assert KEY not in client.get("/api/settings").text
    bad = client.post("/settings/connections", data={"IBKR_PORT": "seven"}, follow_redirects=False)
    assert "not+a+number" in bad.headers["location"] or "not%20a%20number" in bad.headers["location"]


def test_export_and_import_bundle(tmp_path):
    sess = _session(tmp_path)
    client = TestClient(create_app(sess))
    client.post("/settings/strategy", data={"edge.threshold": "0.35", "risk.max_positions": "4"}, follow_redirects=False)
    client.post("/settings/connections", data={"UNUSUAL_WHALES_API_KEY": KEY, "ALPACA_API_KEY": "AKIA-alpaca-key-1234", "IBKR_PORT": "4002", "QMAG_DATA": "unusual_whales"}, follow_redirects=False)

    plain = client.get("/settings/export")
    assert plain.status_code == 200 and plain.headers["content-disposition"].startswith('attachment; filename="qmag-settings-')
    bundle = yaml.safe_load(plain.text)
    assert bundle["kind"] == "qmag-settings" and bundle["strategy"]["edge"]["threshold"] == 0.35
    assert bundle["connections"] == {"IBKR_PORT": "4002", "QMAG_DATA": "unusual_whales"} and bundle["includes_secrets"] is False
    assert sorted(bundle["secrets_omitted"]) == ["ALPACA_API_KEY", "UNUSUAL_WHALES_API_KEY"] and KEY not in plain.text

    with_keys = client.get("/settings/export?secrets=1")
    assert "with-keys" in with_keys.headers["content-disposition"] and KEY in with_keys.text and "CONTAINS API KEYS" in with_keys.text
    assert set(SECRET_NAMES) >= set(yaml.safe_load(with_keys.text)["secrets_omitted"]) == set()

    # Import on a fresh desk, keys included.
    other = TradingSession(SessionSettings(data="auto", state_dir=tmp_path / "other", charts=False, overrides={"context.enabled": False}))
    c2 = TestClient(create_app(other))
    r = c2.post("/settings/import", files={"file": ("bundle.yaml", with_keys.text.encode(), "text/yaml")}, data={"include_secrets": "1"}, follow_redirects=False)
    assert r.status_code == 303 and "saved=import" in r.headers["location"]
    assert other.cfg.edge.threshold == 0.35 and other.cfg.risk.max_positions == 4
    assert other.store.load_env()["UNUSUAL_WHALES_API_KEY"] == KEY and other.store.load_env()["ALPACA_API_KEY"] == "AKIA-alpaca-key-1234" and other.s.data == "unusual_whales"
    # Import without keys keeps whatever is saved locally.
    other.store.save_env({"UNUSUAL_WHALES_API_KEY": "local-key-stays-put-9999"})
    r = c2.post("/settings/import", files={"file": ("bundle.yaml", with_keys.text.encode(), "text/yaml")}, data={}, follow_redirects=False)
    assert other.store.load_env()["UNUSUAL_WHALES_API_KEY"] == "local-key-stays-put-9999" and other.store.load_env()["IBKR_PORT"] == "4002"
    # A plain strategy YAML (the --config format) is accepted too.
    r = c2.post("/settings/import", files={"file": ("strategy.yaml", b"risk:\n  max_positions: 2\n", "text/yaml")}, follow_redirects=False)
    assert other.cfg.risk.max_positions == 2 and "plain%20strategy%20YAML" in r.headers["location"]
    # Garbage is rejected and nothing changes.
    r = c2.post("/settings/import", files={"file": ("x.yaml", b"- just\n- a list\n", "text/yaml")}, follow_redirects=False)
    assert "Import%20failed" in r.headers["location"] and other.cfg.risk.max_positions == 2
    r = c2.post("/settings/import", files={"file": ("x.yaml", b"risk:\n  max_positions: 0\n", "text/yaml")}, follow_redirects=False)
    assert "Import%20failed" in r.headers["location"] and other.cfg.risk.max_positions == 2
    r = c2.post("/settings/import", data={}, follow_redirects=False)
    assert "choose%20a%20settings%20file" in r.headers["location"]
