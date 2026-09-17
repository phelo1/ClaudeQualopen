"""AI providers: where each language-model seat sends its request.

A *provider* is one vendor endpoint plus the key for it. There are two
transport kinds:

* ``gemini`` - Google's native REST API (``generativelanguage.googleapis.com``),
  which enforces the JSON schema server-side.
* ``openai`` - any OpenAI-compatible ``/chat/completions`` endpoint: OpenAI
  itself, Anthropic, xAI, Groq, OpenRouter, DeepSeek, Mistral, Together, a
  local Ollama or vLLM, or anything else that speaks the same protocol.

Providers live in ``<state-dir>/llm_providers.json`` (owner-only file
permissions, like ``settings.env``) and are edited on the settings page
under **AI providers**. The built-in ``gemini`` and ``openai`` entries always
exist and fall back to ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY`` and
``OPENAI_API_KEY`` / ``QMAG_LLM_API_KEY`` + ``QMAG_LLM_BASE_URL`` from the
environment, so an existing desk keeps working. Every AI seat (trade
reviewer, the three committee seats, insider analyst, post-mortem coach,
advisor) names a provider id and a model; ``auto`` means the first provider
that has a key.

Nothing here guesses: a seat whose provider has no key, or whose model is
not chosen, is reported as not usable and the call is never made.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

FILE_NAME = "llm_providers.json"
KINDS = ("gemini", "openai")

# Vendors people actually use, with the endpoint each one documents for the
# OpenAI-compatible protocol. ``default_model`` is left empty where model
# names change too often to hard-code: the settings page lists what the key
# can call and the operator picks one.
PRESETS: dict[str, dict[str, Any]] = {
    "gemini": {"label": "Google Gemini", "kind": "gemini", "base_url": None, "default_model": "gemini-3.5-flash",
               "keys_at": "https://aistudio.google.com/app/apikey", "note": "Google's native API; the JSON schema is enforced server-side."},
    "openai": {"label": "OpenAI", "kind": "openai", "base_url": "https://api.openai.com/v1", "default_model": "gpt-4o-mini",
               "keys_at": "https://platform.openai.com/api-keys", "note": "GPT and o-series models."},
    "anthropic": {"label": "Anthropic Claude", "kind": "openai", "base_url": "https://api.anthropic.com/v1", "default_model": None,
                  "keys_at": "https://console.anthropic.com/settings/keys", "note": "Claude models through Anthropic's OpenAI-compatible endpoint."},
    "xai": {"label": "xAI Grok", "kind": "openai", "base_url": "https://api.x.ai/v1", "default_model": None,
            "keys_at": "https://console.x.ai/", "note": "Grok models."},
    "groq": {"label": "Groq", "kind": "openai", "base_url": "https://api.groq.com/openai/v1", "default_model": None,
             "keys_at": "https://console.groq.com/keys", "note": "Fast hosted open-weight models (Llama, Mixtral, Gemma...)."},
    "openrouter": {"label": "OpenRouter", "kind": "openai", "base_url": "https://openrouter.ai/api/v1", "default_model": None,
                   "keys_at": "https://openrouter.ai/keys", "note": "One key for hundreds of models from many vendors."},
    "deepseek": {"label": "DeepSeek", "kind": "openai", "base_url": "https://api.deepseek.com/v1", "default_model": None,
                 "keys_at": "https://platform.deepseek.com/api_keys", "note": "DeepSeek chat / reasoner models."},
    "mistral": {"label": "Mistral", "kind": "openai", "base_url": "https://api.mistral.ai/v1", "default_model": None,
                "keys_at": "https://console.mistral.ai/api-keys", "note": "Mistral and Codestral models."},
    "together": {"label": "Together AI", "kind": "openai", "base_url": "https://api.together.xyz/v1", "default_model": None,
                 "keys_at": "https://api.together.ai/settings/api-keys", "note": "Hosted open-weight models."},
    "ollama": {"label": "Ollama (this machine)", "kind": "openai", "base_url": "http://127.0.0.1:11434/v1", "default_model": None,
               "keys_at": None, "note": "A local Ollama server; no key needed. Pull a model first (ollama pull ...)."},
    "custom": {"label": "Custom OpenAI-compatible endpoint", "kind": "openai", "base_url": None, "default_model": None,
               "keys_at": None, "note": "vLLM, LM Studio, LiteLLM, Azure OpenAI or any other server that speaks /v1/chat/completions."},
}
BUILTIN_IDS = ("gemini", "openai")

# Hosted vendors always need a key; a self-hosted or local endpoint may not.
_HOSTED_HOSTS = ("googleapis.com", "openai.com", "anthropic.com", "x.ai", "groq.com", "openrouter.ai", "deepseek.com", "mistral.ai", "together.xyz", "azure.com")

_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    return _SLUG.sub("-", (text or "").strip().lower()).strip("-")[:40] or "provider"


def _host(url: str | None) -> str:
    try:
        return (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""


def needs_key(kind: str, base_url: str | None) -> bool:
    if kind == "gemini":
        return True
    host = _host(base_url)
    return any(host == h or host.endswith("." + h) for h in _HOSTED_HOSTS)


@dataclass
class Provider:
    id: str
    label: str
    kind: str = "openai"
    base_url: str | None = None
    api_key: str | None = None
    default_model: str | None = None
    preset: str | None = None
    builtin: bool = False
    key_source: str = "unset"  # saved | environment | unset

    @property
    def needs_key(self) -> bool:
        return needs_key(self.kind, self.base_url)

    @property
    def configured(self) -> bool:
        """Usable: has a key, or is a self-hosted endpoint that needs none."""
        if self.kind == "gemini":
            return bool(self.api_key)
        if not self.base_url:
            return False
        return bool(self.api_key) or not self.needs_key

    @property
    def endpoint(self) -> str:
        return "generativelanguage.googleapis.com" if self.kind == "gemini" else (self.base_url or "").rstrip("/")

    def missing(self) -> str:
        """Why the provider is not usable, in one line (empty when it is)."""
        if self.configured:
            return ""
        if self.kind == "gemini":
            return f"no API key saved for {self.label} (settings → AI providers)"
        if not self.base_url:
            return f"{self.label}: no endpoint URL"
        return f"no API key saved for {self.label} (settings → AI providers)"

    def public(self) -> dict[str, Any]:
        """The row the settings page shows: the key is masked, never echoed."""
        from .settings import mask

        return {
            "id": self.id, "label": self.label, "kind": self.kind, "base_url": self.base_url, "endpoint": self.endpoint,
            "default_model": self.default_model, "preset": self.preset, "builtin": self.builtin,
            "has_key": bool(self.api_key), "key_masked": mask(self.api_key), "key_source": self.key_source,
            "needs_key": self.needs_key, "configured": self.configured, "missing": self.missing(),
            "keys_at": (PRESETS.get(self.preset or self.id) or {}).get("keys_at"),
        }


class ProviderRegistry:
    """The providers a process can use, in display order (built-ins first)."""

    def __init__(self, providers: list[Provider]):
        self._by_id: dict[str, Provider] = {}
        for p in providers:
            self._by_id[p.id] = p

    def all(self) -> list[Provider]:
        return list(self._by_id.values())

    def ids(self) -> list[str]:
        return list(self._by_id)

    def choices(self) -> list[str]:
        return ["auto", *self.ids()]

    def get(self, pid: str | None) -> Provider | None:
        return self._by_id.get((pid or "").strip().lower())

    def configured(self) -> list[Provider]:
        return [p for p in self.all() if p.configured]

    def auto(self) -> Provider:
        """``auto``: the first provider with a key - Gemini, then OpenAI, then
        the others in saved order; with none configured, Gemini (so the
        message names a concrete provider and what it lacks)."""
        for pid in BUILTIN_IDS:
            p = self._by_id.get(pid)
            if p and p.configured:
                return p
        for p in self.all():
            if p.configured:
                return p
        return self._by_id.get("gemini") or next(iter(self.all()))

    def resolve(self, pid: str | None) -> Provider:
        """A provider by id; ``auto`` / blank picks. Raises ``KeyError`` with a
        readable message for an id that no longer exists."""
        key = (pid or "auto").strip().lower()
        if key in ("", "auto"):
            return self.auto()
        p = self._by_id.get(key)
        if p is None:
            raise KeyError(f"unknown AI provider '{pid}' - it was removed or never added (settings → AI providers)")
        return p

    def secret_values(self) -> list[str]:
        return [p.api_key for p in self.all() if p.api_key and len(p.api_key) >= 6]


def _builtin(pid: str, saved: dict[str, Any] | None, env: dict[str, str] | os._Environ) -> Provider:
    preset = PRESETS[pid]
    saved = saved or {}
    if pid == "gemini":
        env_key = env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY")
        env_url = None
    else:
        env_key = env.get("OPENAI_API_KEY") or env.get("QMAG_LLM_API_KEY")
        env_url = env.get("QMAG_LLM_BASE_URL") or None
    key = saved.get("api_key") or None
    source = "saved" if key else "environment" if env_key else "unset"
    return Provider(
        id=pid, label=saved.get("label") or preset["label"], kind=preset["kind"],
        base_url=(saved.get("base_url") or env_url or preset["base_url"]) if pid != "gemini" else None,
        api_key=key or env_key or None, default_model=saved.get("default_model") or preset["default_model"],
        preset=pid, builtin=True, key_source=source,
    )


def registry_from(saved: list[dict[str, Any]] | None, env: dict[str, str] | os._Environ | None = None) -> ProviderRegistry:
    """Build a registry from the saved rows plus the environment fallbacks."""
    env = os.environ if env is None else env
    by_id = {str(r.get("id")): r for r in (saved or []) if isinstance(r, dict) and r.get("id")}
    out: list[Provider] = [_builtin(pid, by_id.get(pid), env) for pid in BUILTIN_IDS]
    for pid, r in by_id.items():
        if pid in BUILTIN_IDS:
            continue
        kind = r.get("kind") if r.get("kind") in KINDS else "openai"
        out.append(Provider(
            id=pid, label=str(r.get("label") or pid), kind=kind, base_url=(r.get("base_url") or None) if kind == "openai" else None,
            api_key=r.get("api_key") or None, default_model=r.get("default_model") or None, preset=r.get("preset") or None,
            builtin=False, key_source="saved" if r.get("api_key") else "unset",
        ))
    return ProviderRegistry(out)


class ProviderStore:
    """``llm_providers.json`` of one state directory."""

    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / FILE_NAME

    def load_rows(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not read %s: %s", self.path, exc)
            return []
        rows = data.get("providers") if isinstance(data, dict) else data
        return [r for r in (rows or []) if isinstance(r, dict) and r.get("id")]

    def save_rows(self, rows: list[dict[str, Any]]) -> Path:
        from .settings import write_private

        write_private(self.path, json.dumps({"providers": rows}, indent=2) + "\n")
        return self.path

    def registry(self) -> ProviderRegistry:
        return registry_from(self.load_rows())

    def activate(self) -> ProviderRegistry:
        rows = self.load_rows()
        set_active(rows)
        return registry_from(rows)

    # ---- edits from the settings page ------------------------------------
    def upsert(self, form: dict[str, str]) -> tuple[Provider, list[str]]:
        """Add or update one provider from the settings form.

        ``id`` blank = new provider (id from the label). A blank key keeps
        the saved one; ``clear_key`` forgets it. Returns (provider, notes).
        Raises ``ValueError`` with a readable message on bad input.
        """
        rows = self.load_rows()
        by_id = {r["id"]: r for r in rows}
        pid = slugify(form["id"]) if (form.get("id") or "").strip() else ""
        preset_id = (form.get("preset") or "").strip().lower() or None
        preset = PRESETS.get(preset_id or "") or {}
        label = (form.get("label") or "").strip() or (preset.get("label") if preset_id != "custom" else "") or ""
        is_new = not pid or pid not in by_id and pid not in BUILTIN_IDS
        if is_new:
            if not label:
                raise ValueError("give the provider a name")
            pid = pid or (preset_id if preset_id and preset_id not in ("custom",) and preset_id not in by_id and preset_id not in BUILTIN_IDS else slugify(label))
            if pid in by_id or pid in BUILTIN_IDS:
                raise ValueError(f"a provider with the id '{pid}' already exists - edit it instead")
        row = dict(by_id.get(pid) or {"id": pid})
        kind = preset.get("kind") if preset else (row.get("kind") or ("gemini" if pid == "gemini" else "openai"))
        if pid in BUILTIN_IDS:
            kind = PRESETS[pid]["kind"]
        if kind not in KINDS:
            raise ValueError(f"unknown provider kind '{kind}'")
        base_url = (form.get("base_url") or "").strip() or None
        if kind == "openai":
            if base_url is None and not is_new and "base_url" not in form:
                base_url = row.get("base_url")
            if base_url is None:
                base_url = (preset or PRESETS.get(row.get("preset") or "") or {}).get("base_url")
            if base_url is None and pid != "openai":
                raise ValueError(f"{label or pid}: an endpoint URL is required (e.g. https://api.example.com/v1)")
            if base_url is not None and not re.match(r"^https?://", base_url):
                raise ValueError(f"{label or pid}: the endpoint must start with http:// or https://")
        else:
            base_url = None
        notes: list[str] = []
        raw_key = (form.get("api_key") or "").strip()
        clear = str(form.get("clear_key") or "") in ("1", "on", "true")
        if clear:
            row.pop("api_key", None)
            notes.append(f"{label or pid}: saved key forgotten")
        elif raw_key and not raw_key.startswith("•"):
            row["api_key"] = raw_key
            notes.append(f"{label or pid}: key saved")
        row.update({"label": label or row.get("label") or pid, "kind": kind, "base_url": base_url, "preset": preset_id or row.get("preset") or (pid if pid in PRESETS else None)})
        dm = (form.get("default_model") or "").strip()
        if dm:
            row["default_model"] = dm
        elif "default_model" in form:
            row.pop("default_model", None)
        if pid in by_id:
            rows = [row if r["id"] == pid else r for r in rows]
        else:
            rows.append(row)
        self.save_rows(rows)
        reg = self.activate()
        return reg.resolve(pid), notes

    def delete(self, pid: str) -> bool:
        pid = (pid or "").strip().lower()
        if pid in BUILTIN_IDS:
            raise ValueError(f"'{pid}' is built in and cannot be removed - clear its key instead")
        rows = self.load_rows()
        keep = [r for r in rows if r["id"] != pid]
        if len(keep) == len(rows):
            return False
        self.save_rows(keep)
        self.activate()
        return True

    def export_rows(self, include_secrets: bool) -> list[dict[str, Any]]:
        out = []
        for r in self.load_rows():
            row = dict(r)
            if not include_secrets:
                row.pop("api_key", None)
            out.append(row)
        return out

    def import_rows(self, rows: list[dict[str, Any]], include_secrets: bool = True) -> list[str]:
        """Merge bundle rows into the file; returns the ids touched."""
        current = {r["id"]: r for r in self.load_rows()}
        touched: list[str] = []
        for r in rows or []:
            if not isinstance(r, dict) or not r.get("id"):
                continue
            pid = slugify(str(r["id"]))
            row = dict(current.get(pid) or {"id": pid})
            for k in ("label", "kind", "base_url", "preset", "default_model"):
                if k in r:
                    row[k] = r[k]
            if include_secrets and r.get("api_key"):
                row["api_key"] = str(r["api_key"])
            current[pid] = row
            touched.append(pid)
        if touched:
            self.save_rows(list(current.values()))
            self.activate()
        return touched


# --------------------------------------------------------------------------- #
# The process-wide active rows (set by the session when settings load). The
# registry is rebuilt on every read so environment fallbacks stay current.
# --------------------------------------------------------------------------- #
_lock = threading.Lock()
_active_rows: list[dict[str, Any]] | None = None


def set_active(reg_or_rows: ProviderRegistry | list[dict[str, Any]] | None) -> None:
    global _active_rows
    rows: list[dict[str, Any]] | None
    if reg_or_rows is None:
        rows = None
    elif isinstance(reg_or_rows, ProviderRegistry):
        rows = [
            {"id": p.id, "label": p.label, "kind": p.kind, "base_url": p.base_url, "preset": p.preset, "default_model": p.default_model,
             **({"api_key": p.api_key} if p.key_source == "saved" and p.api_key else {})}
            for p in reg_or_rows.all()
        ]
    else:
        rows = list(reg_or_rows)
    with _lock:
        _active_rows = rows


def registry() -> ProviderRegistry:
    """The active registry; without a session, the built-ins from the environment."""
    with _lock:
        rows = _active_rows
    return registry_from(rows or [])
