"""Unusual Whales API client shared by the price provider, the context layer,
the edge score, the CLI and the connection probe.

* bearer auth from ``UNUSUAL_WHALES_API_KEY`` (``https://unusualwhales.com/dashboard/api``)
* one process-wide throttle (``UNUSUAL_WHALES_RPM``, default 100 requests per
  minute) so a whole-market refresh or a 20-feature edge scan never trips
  the account's limit; HTTP 429 answers are retried after the server's
  ``Retry-After``
* a small on-disk TTL cache so facts that change once a day (short interest,
  insider filings, seasonality, earnings history ...) are fetched once a day
  and intraday reads (net premium, dark pool prints ...) once per context
  window

Nothing here invents data: every call returns ``UWResponse`` with either the
decoded ``data`` or an ``error`` string, and callers record the outcome.
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from .persistence import atomic_json, desk_lock
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)

UW_BASE = "https://api.unusualwhales.com/api"
DEFAULT_RPM = 100
DEFAULT_TIMEOUT = 15
UA = {"User-Agent": "qmag/0.1 (+https://github.com/; research tool)"}
DEFAULT_CACHE = Path("data/cache/uw_cache.json")


def api_key() -> str | None:
    return os.environ.get("UNUSUAL_WHALES_API_KEY") or None


def has_key() -> bool:
    return bool(api_key())


def fnum(x: Any) -> float | None:
    """UW returns most numbers as strings; '' / None / garbage -> None."""
    if x is None or x == "":
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


# --------------------------------------------------------------------------- #
# Throttle
# --------------------------------------------------------------------------- #
class _Throttle:
    """Sliding-window limiter: at most ``rpm`` requests in any 60-second window."""

    def __init__(self, rpm: int):
        self.rpm = max(int(rpm), 1)
        self._times: deque[float] = deque()
        self._lock = threading.RLock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._times and now - self._times[0] >= 60.0:
                    self._times.popleft()
                if len(self._times) < self.rpm:
                    self._times.append(now)
                    return
                wait = 60.0 - (now - self._times[0]) + 0.05
            time.sleep(min(max(wait, 0.05), 5.0))


_throttles: dict[int, _Throttle] = {}
_throttle_lock = threading.Lock()


def throttle(rpm: int | None = None) -> None:
    """Block until one more request is allowed under the process-wide limit."""
    rpm = int(rpm or os.environ.get("UNUSUAL_WHALES_RPM") or DEFAULT_RPM)
    with _throttle_lock:
        t = _throttles.get(rpm)
        if t is None:
            t = _throttles[rpm] = _Throttle(rpm)
    t.acquire()


# --------------------------------------------------------------------------- #
# Daily budget
# --------------------------------------------------------------------------- #
DEFAULT_BUDGET_FILE = Path("data/cache/uw_budget.json")
_DAILY_WORDS = ("daily", "per day", "quota", "plan limit", "exceeded your", "upgrade")


def _utc_today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


class DailyBudget:
    """Cross-process count of Unusual Whales calls per UTC day, with an
    optional cap (``UNUSUAL_WHALES_DAILY_CAP``, 0 = uncapped) and a pause
    for the rest of the day when the API says the plan's daily limit is
    exhausted. While blocked, every call returns an error instead of
    hammering the API - and every caller records that error as a data gap,
    so the report says *why* the flow score is empty rather than
    substituting anything. The file lives next to the response cache so the
    daemon, the dashboard and the CLI share one count.
    """

    def __init__(self, path: Path | None = DEFAULT_BUDGET_FILE):
        self.path = Path(path) if path is not None else None
        self._mem: dict[str, Any] = {}
        self._lock = threading.RLock()

    @property
    def cap(self) -> int:
        try:
            return max(int(os.environ.get("UNUSUAL_WHALES_DAILY_CAP") or 0), 0)
        except ValueError:
            return 0

    def _load(self) -> dict[str, Any]:
        rec: dict[str, Any] = {}
        if self.path is not None and self.path.exists():
            try:
                raw = json.loads(self.path.read_text())
                if isinstance(raw, dict):
                    rec = raw
            except (OSError, json.JSONDecodeError):
                rec = {}
        else:
            rec = dict(self._mem)
        if rec.get("date") != _utc_today():
            rec = {"date": _utc_today(), "calls": 0, "paused_reason": None, "paused_at": None}
        return rec

    def _save(self, rec: dict[str, Any]) -> None:
        self._mem = rec
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(self.path, rec)
        except OSError as exc:  # pragma: no cover - a full disk must not break a scan
            log.warning("uw budget not saved: %s", exc)

    def check(self) -> str | None:
        """The reason calls are blocked right now, or None."""
        with self._lock, (desk_lock(self.path.parent / ".budget-writer") if self.path else nullcontext()):
            rec = self._load()
        if rec.get("paused_reason"):
            return f"Unusual Whales paused until 00:00 UTC: daily limit reached at {str(rec.get('paused_at'))[11:16]} UTC ({rec['paused_reason']})"
        cap = self.cap
        if cap and int(rec.get("calls", 0)) >= cap:
            return f"Unusual Whales daily call budget used up ({rec['calls']}/{cap} today, UNUSUAL_WHALES_DAILY_CAP); resumes 00:00 UTC"
        return None

    def reserve(self) -> str | None:
        """Atomically check and reserve one request, including retries."""
        with self._lock, (desk_lock(self.path.parent / ".budget-writer") if self.path else nullcontext()):
            reason = self.check()
            if reason:
                return reason
            self.count()
            return None

    def count(self, n: int = 1) -> int:
        with self._lock, (desk_lock(self.path.parent / ".budget-writer") if self.path else nullcontext()):
            rec = self._load()
            rec["calls"] = int(rec.get("calls", 0)) + n
            self._save(rec)
            return rec["calls"]

    def pause(self, reason: str) -> str:
        with self._lock, (desk_lock(self.path.parent / ".budget-writer") if self.path else nullcontext()):
            rec = self._load()
            if not rec.get("paused_reason"):
                rec["paused_reason"] = reason[:160]
                rec["paused_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                self._save(rec)
                log.warning("Unusual Whales paused for the rest of the UTC day: %s", reason)
        return self.check() or ""

    def usage(self) -> dict[str, Any]:
        with self._lock, (desk_lock(self.path.parent / ".budget-writer") if self.path else nullcontext()):
            rec = self._load()
        cap = self.cap
        return {
            "date": rec["date"], "calls": int(rec.get("calls", 0)), "cap": cap or None,
            "remaining": (max(cap - int(rec.get("calls", 0)), 0) if cap else None),
            "paused": bool(rec.get("paused_reason")), "paused_reason": rec.get("paused_reason"), "paused_at": rec.get("paused_at"),
            "blocked": self.check(),
        }


_budget: DailyBudget | None = None
_budget_lock = threading.Lock()


def budget() -> DailyBudget:
    """The process-wide budget (file location from ``UNUSUAL_WHALES_BUDGET_FILE`` when set)."""
    global _budget
    with _budget_lock:
        path = Path(os.environ["UNUSUAL_WHALES_BUDGET_FILE"]) if os.environ.get("UNUSUAL_WHALES_BUDGET_FILE") else DEFAULT_BUDGET_FILE
        if _budget is None or _budget.path != path:
            _budget = DailyBudget(path)
        return _budget


def looks_like_daily_limit(status: int | None, reason: str) -> bool:
    text = (reason or "").lower()
    if status in (402,) and text:
        return True
    return status in (429, 402, 403) and any(w in text for w in _DAILY_WORDS)


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
@dataclass
class UWResponse:
    data: Any = None  # the ``data`` member of the JSON body (or the body itself)
    body: Any = None  # the full decoded body (some endpoints carry ``date`` etc. at the top level)
    error: str | None = None
    status: int | None = None
    cached: bool = False
    latency_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None


class UWClient:
    """Thin GET client with throttle, 429 retry and a JSON TTL cache.

    ``cache_path=None`` disables the disk cache (tests). The cache key is the
    path plus the sorted query string, so two features that read the same
    endpoint with the same parameters share one request.
    """

    def __init__(self, key: str | None = None, cache_path: Path | None = DEFAULT_CACHE, rpm: int | None = None, timeout: int = DEFAULT_TIMEOUT):
        self.key = key or api_key()
        self.cache_path = Path(cache_path) if cache_path is not None else None
        self.rpm = rpm
        self.timeout = timeout
        self._cache: dict[str, dict[str, Any]] | None = None
        self._lock = threading.Lock()
        self.calls = 0  # network requests made by this client (for cost reporting)
        self.hits = 0
        self.blocked = 0  # requests refused locally because the daily budget is used up / paused

    # -- cache -------------------------------------------------------------
    def _load_cache(self) -> dict[str, dict[str, Any]]:
        if self._cache is None:
            self._cache = {}
            if self.cache_path is not None and self.cache_path.exists():
                try:
                    raw = json.loads(self.cache_path.read_text())
                    if isinstance(raw, dict):
                        self._cache = raw
                except (OSError, json.JSONDecodeError):
                    self._cache = {}
        return self._cache

    def save_cache(self) -> None:
        if self.cache_path is None or self._cache is None:
            return
        with self._lock:
            cutoff = time.time() - 3 * 86400
            self._cache = {k: v for k, v in self._cache.items() if v.get("at", 0) > cutoff}
            try:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.cache_path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(self._cache, default=str))
                tmp.replace(self.cache_path)
            except OSError as exc:  # a full disk must not break a scan
                log.warning("uw cache not saved: %s", exc)

    @staticmethod
    def cache_key(path: str, params: dict | None) -> str:
        q = "&".join(f"{k}={params[k]}" for k in sorted(params)) if params else ""
        return f"{path}?{q}"

    # -- requests ----------------------------------------------------------
    def get(self, path: str, params: dict | None = None, ttl: float = 0.0) -> UWResponse:
        """GET ``path`` (relative to ``/api``). ``ttl`` seconds > 0 serves a
        cached copy that young; failures are never cached and never raised."""
        if not self.key:
            return UWResponse(error="no UNUSUAL_WHALES_API_KEY")
        params = {k: v for k, v in (params or {}).items() if v is not None}
        key = self.cache_key(path, params)
        if ttl > 0:
            with self._lock:
                hit = self._load_cache().get(key)
            if hit and time.time() - float(hit.get("at", 0)) < ttl:
                self.hits += 1
                return UWResponse(data=hit.get("data"), body=hit.get("body"), cached=True)
        blocked = budget().check()
        if blocked:
            self.blocked += 1
            return UWResponse(error=blocked, status=None)
        headers = {"Authorization": f"Bearer {self.key}", "Accept": "application/json", **UA}
        t0 = time.perf_counter()
        status: int | None = None
        for attempt in range(3):
            throttle(self.rpm)
            blocked = budget().reserve()
            if blocked:
                self.blocked += 1
                return UWResponse(error=blocked, status=None)
            self.calls += 1
            try:
                r = requests.get(f"{UW_BASE}{path}", headers=headers, params=params, timeout=self.timeout)
            except Exception as exc:
                return UWResponse(error=f"{type(exc).__name__}: {exc}", latency_ms=(time.perf_counter() - t0) * 1000)
            status = getattr(r, "status_code", None)
            if status == 429 and attempt < 2 and not looks_like_daily_limit(status, _error_reason(r)):
                wait = fnum(getattr(r, "headers", {}).get("Retry-After")) if hasattr(r, "headers") else None
                time.sleep(min(max(wait or 5.0, 1.0), 30.0))
                continue
            break
        latency = (time.perf_counter() - t0) * 1000
        if status is not None and status >= 400:
            reason = _error_reason(r)
            if looks_like_daily_limit(status, reason):
                # The plan's daily allowance is gone: stop calling until the UTC day rolls over,
                # and say so in every response so the gap is recorded, not papered over.
                return UWResponse(error=budget().pause(f"HTTP {status} {reason}".strip()), status=status, latency_ms=latency)
            return UWResponse(error=f"HTTP {status}" + (f" {reason}" if reason else ""), status=status, latency_ms=latency)
        try:
            body = r.json()
        except Exception as exc:
            return UWResponse(error=f"bad JSON: {type(exc).__name__}", status=status, latency_ms=latency)
        data = body.get("data", body) if isinstance(body, dict) else body
        if ttl > 0:
            with self._lock:
                self._load_cache()[key] = {"at": time.time(), "data": data, "body": body if isinstance(body, dict) and body.get("data") is not None and len(body) > 1 else None}
        return UWResponse(data=data, body=body, status=status, latency_ms=latency)


def _error_reason(r: Any) -> str:
    try:
        j = r.json()
        return str(j.get("reason") or j.get("message") or j.get("error") or "") if isinstance(j, dict) else ""
    except Exception:
        return ""


_default_client: UWClient | None = None


def default_client() -> UWClient:
    """One shared client per process for callers that have no session (CLI, provider)."""
    global _default_client
    if _default_client is None or _default_client.key != api_key():
        _default_client = UWClient()
    return _default_client
