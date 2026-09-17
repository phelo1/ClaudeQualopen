"""Password protection for the dashboard.

Turned on by setting ``QMAG_DASHBOARD_PASSWORD`` (environment or the settings
page, which writes ``settings.env``).  When it is set every route except the
login page needs a signed session cookie or ``Authorization: Bearer <password>``.

* Sessions are ``expiry.hmac`` tokens signed with a random per-desk secret kept
  in ``<state-dir>/dashboard_secret`` (0600); the signature also covers a hash
  of the password, so changing the password logs every browser out.
* Login attempts are rate limited per client address (5 failures, then a
  15-minute lockout) and compared in constant time.
* The cookie is HttpOnly, SameSite=Lax and marked Secure whenever the request
  arrived over HTTPS (directly or through a proxy that sets X-Forwarded-Proto).

This protects the desk when it is reached through a tunnel or a public
address; it is not a substitute for keeping the bind address on loopback when
you do not need remote access.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

PASSWORD_ENV = "QMAG_DASHBOARD_PASSWORD"
COOKIE = "qmag_session"
SECRET_FILE = "dashboard_secret"
SESSION_SECONDS = 7 * 24 * 3600
MAX_FAILURES = 5
LOCKOUT_SECONDS = 15 * 60
MIN_PASSWORD_LENGTH = 8


def configured_password() -> str | None:
    """The password currently in force, or None when the dashboard is open."""
    pw = os.environ.get(PASSWORD_ENV, "").strip()
    return pw or None


@dataclass
class DashboardAuth:
    state_dir: Path
    _secret: bytes = field(default=b"", init=False, repr=False)
    _failures: dict[str, list[float]] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.state_dir = Path(self.state_dir)
        self._secret = self._load_secret()

    # ------------------------------------------------------------------ #
    def _load_secret(self) -> bytes:
        path = self.state_dir / SECRET_FILE
        try:
            raw = path.read_bytes().strip()
            if len(raw) >= 32:
                return raw
        except FileNotFoundError:
            pass
        self.state_dir.mkdir(parents=True, exist_ok=True)
        raw = secrets.token_hex(32).encode()
        path.write_bytes(raw)
        try:
            path.chmod(0o600)
        except OSError:  # pragma: no cover - platform dependent
            pass
        return raw

    @property
    def enabled(self) -> bool:
        return configured_password() is not None

    # ------------------------------------------------------------------ #
    def _key(self) -> bytes:
        pw = configured_password() or ""
        return hashlib.sha256(self._secret + b"|" + pw.encode()).digest()

    def _sign(self, payload: str) -> str:
        mac = hmac.new(self._key(), payload.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).decode().rstrip("=")

    def issue_token(self, now: float | None = None) -> str:
        expiry = int((now or time.time()) + SESSION_SECONDS)
        payload = str(expiry)
        return f"{payload}.{self._sign(payload)}"

    def token_valid(self, token: str | None, now: float | None = None) -> bool:
        if not token or "." not in token:
            return False
        payload, _, sig = token.partition(".")
        if not payload.isdigit():
            return False
        if int(payload) < (now or time.time()):
            return False
        return hmac.compare_digest(self._sign(payload), sig)

    # ------------------------------------------------------------------ #
    def locked_for(self, client: str, now: float | None = None) -> float:
        """Seconds until this client may try again (0 when it may try now)."""
        now = now or time.time()
        with self._lock:
            times = [t for t in self._failures.get(client, []) if now - t < LOCKOUT_SECONDS]
            self._failures[client] = times
            if len(times) >= MAX_FAILURES:
                return max(0.0, LOCKOUT_SECONDS - (now - times[0]))
        return 0.0

    def check_password(self, attempt: str, client: str, now: float | None = None) -> tuple[bool, str]:
        """Constant-time check with per-client lockout. Returns (ok, message)."""
        now = now or time.time()
        wait = self.locked_for(client, now)
        if wait > 0:
            return False, f"Too many attempts. Try again in {int(wait // 60) + 1} min."
        expected = configured_password()
        if expected is None:
            return True, ""
        ok = hmac.compare_digest(hashlib.sha256(attempt.encode()).digest(), hashlib.sha256(expected.encode()).digest())
        with self._lock:
            if ok:
                self._failures.pop(client, None)
            else:
                self._failures.setdefault(client, []).append(now)
                left = MAX_FAILURES - len(self._failures[client])
                log.warning("dashboard login failed from %s (%d attempts left)", client, max(left, 0))
        if ok:
            return True, ""
        return False, "Wrong password." if left > 0 else f"Wrong password. Locked for {LOCKOUT_SECONDS // 60} min."

    def bearer_valid(self, header: str | None) -> bool:
        if not header or not header.lower().startswith("bearer "):
            return False
        expected = configured_password()
        if expected is None:
            return True
        return hmac.compare_digest(header[7:].strip().encode(), expected.encode())


def password_problem(pw: str) -> str | None:
    """Why a proposed dashboard password is not acceptable, or None when it is."""
    if len(pw) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters."
    return None
