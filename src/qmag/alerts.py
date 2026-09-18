"""Push alerts: the desk tells you when something happened that you would
want to know about without watching the dashboard.

Three channels, all optional and all configured on the settings page:

* **Telegram** - a bot token plus a chat id (``TELEGRAM_BOT_TOKEN`` /
  ``TELEGRAM_CHAT_ID``). Message the bot once so it can reach you.
* **Webhook** - one URL (``QMAG_ALERT_WEBHOOK_URL``) that receives a JSON
  body with ``text`` (Slack), ``content`` (Discord) and structured fields,
  so Slack / Discord / ntfy / a home-automation hook all work unchanged.
* **Email** - through Resend (``RESEND_API_KEY``, ``QMAG_ALERT_EMAIL_TO``,
  ``QMAG_ALERT_EMAIL_FROM`` on a domain verified in your Resend account).
  Email is for things that need you: it carries ``warn`` and ``error``
  alerts (and the watchdog's issue reports); routine ``info`` alerts such
  as fills go to email only with ``QMAG_ALERT_EMAIL_TRADES=1``.

What is pushed: fills and manual entries, exits and partials with their
R-multiple, the kill switch being thrown or cleared, the daily loss limit
tripping, a required data source blocking new entries, a cycle or daemon
task failing, and a failed broker order test. Alerts never carry secrets.

Delivery happens on a background thread so a slow endpoint never delays a
trading cycle; each attempt is recorded in the connection registry under
``alerts`` so the connections page shows whether your phone is actually
reachable. Without a channel the connection is NOT CONFIGURED and nothing is
sent - there is no silent fallback.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

import requests

if TYPE_CHECKING:  # pragma: no cover
    from .health import ConnectionRegistry
    from .trader import CycleReport

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
RESEND_API = "https://api.resend.com/emails"
TIMEOUT = 10.0
LEVELS = ("info", "warn", "error")
LEVEL_MARK = {"info": "•", "warn": "⚠", "error": "✖"}

# Report actions that are worth a push notification, by prefix.
TRADE_PREFIXES = ("BUY ", "FILL buy", "EXIT ", "CLOSED ", "PARTIAL ", "ADOPTED ")


def channels_from_env(env: dict[str, str] | os._Environ = os.environ) -> list[str]:
    out = []
    if env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"):
        out.append("telegram")
    if env.get("QMAG_ALERT_WEBHOOK_URL"):
        out.append("webhook")
    if email_configured(env):
        out.append("email")
    return out


def email_configured(env: dict[str, str] | os._Environ = os.environ) -> bool:
    return bool(env.get("RESEND_API_KEY") and env.get("QMAG_ALERT_EMAIL_TO") and env.get("QMAG_ALERT_EMAIL_FROM"))


def email_wants(level: str, env: dict[str, str] | os._Environ = os.environ) -> bool:
    """Email carries warn / error; info (fills, resumes) only when asked for."""
    return level in ("warn", "error") or str(env.get("QMAG_ALERT_EMAIL_TRADES", "")).lower() in ("1", "true", "yes", "on")


def _html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def email_message(payload: dict, env: dict[str, str] | os._Environ = os.environ) -> dict[str, Any]:
    """The Resend request body for one alert: plain text plus a minimal HTML twin."""
    level = payload.get("level", "info")
    title = payload.get("title", "")
    body = payload.get("body", "") or ""
    tag = {"error": "PROBLEM", "warn": "WARNING", "info": "FYI"}.get(level, "FYI")
    desk = env.get("QMAG_DESK_NAME") or payload.get("source") or "qmag"
    subject = payload.get("subject") or f"[{desk}] {tag}: {title}"[:200]
    text = f"{title}\n\n{body}\n\n-- {desk} · {payload.get('ts', '')}".strip()
    html = (
        "<div style=\"font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;font-size:14px;line-height:1.5;color:#111\">"
        f"<h2 style=\"margin:0 0 12px;font-size:17px\">{_html_escape(title)}</h2>"
        f"<pre style=\"white-space:pre-wrap;font-family:inherit;margin:0 0 16px\">{_html_escape(body)}</pre>"
        f"<p style=\"color:#666;font-size:12px;margin:0\">{_html_escape(desk)} · {_html_escape(str(payload.get('ts', '')))}</p></div>"
    )
    return {"from": env["QMAG_ALERT_EMAIL_FROM"], "to": [a.strip() for a in env["QMAG_ALERT_EMAIL_TO"].split(",") if a.strip()], "subject": subject, "text": text, "html": html}


def describe_channels(env: dict[str, str] | os._Environ = os.environ) -> str:
    ch = channels_from_env(env)
    if not ch:
        return "set RESEND_API_KEY + QMAG_ALERT_EMAIL_TO + QMAG_ALERT_EMAIL_FROM (email), TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID and/or QMAG_ALERT_WEBHOOK_URL"
    parts = []
    if "telegram" in ch:
        parts.append(f"Telegram chat {env.get('TELEGRAM_CHAT_ID')}")
    if "webhook" in ch:
        url = env.get("QMAG_ALERT_WEBHOOK_URL", "")
        host = url.split("//", 1)[-1].split("/", 1)[0]
        parts.append(f"webhook {host}")
    if "email" in ch:
        parts.append(f"email {env.get('QMAG_ALERT_EMAIL_TO', '')}" + ("" if email_wants("info", env) else " (warnings + problems only)"))
    return " + ".join(parts)


class Alerter:
    """Sends alerts over every configured channel and records the outcome.

    ``transport`` can be swapped for tests; it receives ``(channel, payload)``
    and must raise on failure.
    """

    def __init__(self, health: "ConnectionRegistry | None" = None, env: dict[str, str] | os._Environ | None = None, transport: Callable[[str, dict], None] | None = None, source: str = "qmag"):
        self.health = health
        self._env = env
        self._transport = transport or self._http
        self.source = source
        self._sent: dict[str, float] = {}
        self._lock = threading.Lock()
        self.threads: list[threading.Thread] = []

    @property
    def env(self):
        return self._env if self._env is not None else os.environ

    def channels(self) -> list[str]:
        return channels_from_env(self.env)

    @property
    def configured(self) -> bool:
        return bool(self.channels())

    # -- sending -------------------------------------------------------------
    def send(self, title: str, body: str = "", level: str = "info", key: str | None = None, wait: bool = False, force_email: bool = False) -> bool | None:
        """Push one alert. ``key`` de-duplicates within this process (the
        same key is sent once per 12 hours) so a limit that stays tripped is
        not repeated every focused pass. Returns None when no channel is
        configured, otherwise whether every channel accepted it (``wait``)
        or True once the delivery thread has started. ``force_email`` sends
        an info-level alert by email too (the watchdog's reports)."""
        if level not in LEVELS:
            level = "info"
        if not self.configured:
            return None
        if key:
            with self._lock:
                last = self._sent.get(key)
                now = time.time()
                if last is not None and now - last < 12 * 3600:
                    return True
                self._sent[key] = now
        payload = {
            "source": self.source, "level": level, "title": title, "body": body,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "key": key, "force_email": force_email,
        }
        if wait:
            return self._deliver(payload)
        t = threading.Thread(target=self._deliver, args=(payload,), daemon=True, name="qmag-alert")
        self.threads.append(t)
        t.start()
        return True

    def join(self, timeout: float = TIMEOUT + 2) -> None:
        for t in list(self.threads):
            t.join(timeout)
        self.threads = [t for t in self.threads if t.is_alive()]

    def channels_for(self, payload: dict) -> list[str]:
        """Which channels this alert goes to: everything, except that email only
        takes what needs a person (warn / error) unless trades were opted in."""
        return [ch for ch in self.channels() if ch != "email" or email_wants(payload.get("level", "info"), self.env) or payload.get("force_email")]

    def _deliver(self, payload: dict) -> bool:
        ok_all = True
        t0 = time.perf_counter()
        errors = []
        used = self.channels_for(payload)
        for ch in used:
            try:
                self._transport(ch, payload)
            except Exception as exc:  # network / HTTP errors - recorded, never raised into a cycle
                ok_all = False
                errors.append(f"{ch}: {type(exc).__name__}: {str(exc)[:160]}")
        if self.health is not None:
            self.health.record(
                "alerts", ok_all, detail=f"{payload['level']}: {payload['title'][:80]} → {', '.join(used) or 'no channel for this level'}",
                error="; ".join(errors) if errors else None, latency_ms=(time.perf_counter() - t0) * 1000, save=True,
            )
        if errors:
            log.warning("alert delivery failed: %s", "; ".join(errors))
        return ok_all

    def _http(self, channel: str, payload: dict) -> None:
        text = f"{LEVEL_MARK.get(payload['level'], '•')} {payload['title']}" + (f"\n{payload['body']}" if payload.get("body") else "")
        if channel == "telegram":
            token = self.env.get("TELEGRAM_BOT_TOKEN", "")
            r = requests.post(f"{TELEGRAM_API}/bot{token}/sendMessage", json={"chat_id": self.env.get("TELEGRAM_CHAT_ID"), "text": text[:4000], "disable_web_page_preview": True}, timeout=TIMEOUT)
            if r.status_code >= 400:
                # Telegram's error text is useful ("chat not found", "Unauthorized") and never echoes the token.
                raise RuntimeError(f"HTTP {r.status_code}: {(r.json().get('description') if r.headers.get('content-type', '').startswith('application/json') else r.text)[:120]}")
        elif channel == "webhook":
            body = {**payload, "text": text, "content": text[:1900]}
            r = requests.post(self.env["QMAG_ALERT_WEBHOOK_URL"], json=body, timeout=TIMEOUT)
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
        elif channel == "email":
            headers = {"Authorization": f"Bearer {self.env['RESEND_API_KEY']}", "Content-Type": "application/json"}
            if payload.get("key"):
                # the same alert retried within a day is delivered once
                headers["Idempotency-Key"] = f"qmag-alert/{str(payload['key'])[:200]}"
            r = requests.post(RESEND_API, json=email_message(payload, self.env), headers=headers, timeout=TIMEOUT)
            if r.status_code >= 400:
                # Resend's error body is descriptive ("domain is not verified") and never echoes the key.
                try:
                    msg = r.json().get("message") or r.text
                except ValueError:
                    msg = r.text
                raise RuntimeError(f"HTTP {r.status_code}: {str(msg)[:160]}")
        else:  # pragma: no cover
            raise ValueError(f"unknown alert channel {channel}")

    # -- what the desk pushes --------------------------------------------------
    def report(self, report: "CycleReport", label: str) -> list[str]:
        """Push what a finished cycle did that you would want to hear about. Returns the titles sent."""
        if not self.configured:
            return []
        sent: list[str] = []
        trades = [a for a in report.actions if a.startswith(TRADE_PREFIXES)]
        if trades:
            title = f"{len(trades)} trade event{'s' if len(trades) != 1 else ''} ({label}, {report.asof})"
            self.send(title, "\n".join(trades[:15]) + ("\n…" if len(trades) > 15 else ""), key=None)
            sent.append(title)
        for a in report.actions:
            if a.startswith("RISK daily loss limit"):
                self.send("Daily loss limit hit - no new entries today", a, level="warn", key=f"daily-loss:{report.asof}")
                sent.append("daily loss")
            elif a.startswith("DATA ") and ("no new entries" in a or "stale" in a or "risk-off" in a):
                self.send("Data problem is blocking new entries", a, level="warn", key=f"data:{report.asof}:{a[:60]}")
                sent.append("data")
        return sent

    def failure(self, what: str, error: str, key: str | None = None) -> None:
        self.send(f"{what} failed", error[:600], level="error", key=key)

    def halt(self, on: bool, message: str, by: str) -> None:
        if on:
            self.send("TRADING HALTED", f"{message} (by {by})", level="error")
        else:
            self.send("Trading resumed", f"{message} (by {by})", level="info")

    def test(self) -> dict[str, Any]:
        """Send a test message synchronously and report per-channel outcome."""
        if not self.configured:
            return {"ok": False, "channels": [], "error": describe_channels(self.env)}
        payload = {
            "source": self.source, "level": "info", "title": "qmag test alert", "body": "If you can read this, alerts reach you.",
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "force_email": True,  # a test must exercise every channel
        }
        ok = self._deliver(payload)
        rec = self.health.records().get("alerts", {}) if self.health is not None else {}
        return {"ok": ok, "channels": self.channels(), "error": None if ok else rec.get("last_error", "delivery failed")}
