"""Push alerts: the desk tells you when something happened that you would
want to know about without watching the dashboard.

Two channels, both optional and both configured on the settings page:

* **Telegram** - a bot token plus a chat id (``TELEGRAM_BOT_TOKEN`` /
  ``TELEGRAM_CHAT_ID``). Message the bot once so it can reach you.
* **Webhook** - one URL (``QMAG_ALERT_WEBHOOK_URL``) that receives a JSON
  body with ``text`` (Slack), ``content`` (Discord) and structured fields,
  so Slack / Discord / ntfy / a home-automation hook all work unchanged.

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
    return out


def describe_channels(env: dict[str, str] | os._Environ = os.environ) -> str:
    ch = channels_from_env(env)
    if not ch:
        return "set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID and/or QMAG_ALERT_WEBHOOK_URL"
    parts = []
    if "telegram" in ch:
        parts.append(f"Telegram chat {env.get('TELEGRAM_CHAT_ID')}")
    if "webhook" in ch:
        url = env.get("QMAG_ALERT_WEBHOOK_URL", "")
        host = url.split("//", 1)[-1].split("/", 1)[0]
        parts.append(f"webhook {host}")
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
    def send(self, title: str, body: str = "", level: str = "info", key: str | None = None, wait: bool = False) -> bool | None:
        """Push one alert. ``key`` de-duplicates within this process (the
        same key is sent once per 12 hours) so a limit that stays tripped is
        not repeated every focused pass. Returns None when no channel is
        configured, otherwise whether every channel accepted it (``wait``)
        or True once the delivery thread has started."""
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
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
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

    def _deliver(self, payload: dict) -> bool:
        ok_all = True
        t0 = time.perf_counter()
        errors = []
        for ch in self.channels():
            try:
                self._transport(ch, payload)
            except Exception as exc:  # network / HTTP errors - recorded, never raised into a cycle
                ok_all = False
                errors.append(f"{ch}: {type(exc).__name__}: {str(exc)[:160]}")
        if self.health is not None:
            self.health.record(
                "alerts", ok_all, detail=f"{payload['level']}: {payload['title'][:80]} → {', '.join(self.channels())}",
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
        payload = {"source": self.source, "level": "info", "title": "qmag test alert", "body": "If you can read this, alerts reach you.", "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        ok = self._deliver(payload)
        rec = self.health.records().get("alerts", {}) if self.health is not None else {}
        return {"ok": ok, "channels": self.channels(), "error": None if ok else rec.get("last_error", "delivery failed")}
