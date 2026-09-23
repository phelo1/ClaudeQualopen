"""Regression coverage for observed provider failures and their status display."""
import json
import multiprocessing
import os
import threading

import pytest

from qmag import uw


CONCURRENCY_ERROR = "You have exceeded 3 concurrent requests, the maximum allowed for your current plan. Upgrade your plan at https://unusualwhales.com/dashboard/api"


class Response:
    def __init__(self, status=200, message=None, headers=None):
        self.status_code = status
        self.message = message
        self.headers = headers or {}

    def json(self):
        return {"message": self.message} if self.message else {"data": [1]}


@pytest.fixture
def isolated_budget(tmp_path, monkeypatch):
    path = tmp_path / "budget.json"
    monkeypatch.setenv("UNUSUAL_WHALES_BUDGET_FILE", str(path))
    monkeypatch.delenv("UNUSUAL_WHALES_DAILY_CAP", raising=False)
    monkeypatch.setattr(uw, "throttle", lambda rpm=None: None)
    return path


@pytest.mark.parametrize("status,message,daily", [
    (429, CONCURRENCY_ERROR, False),
    (429, "Per minute quota exceeded; upgrade your plan", False),
    (402, "Subscription required; upgrade", False),
    (403, "Endpoint not included in plan limit", False),
    (429, "You have exceeded your daily request limit", True),
    (429, "Exceeded 10000 requests per day", True),
])
def test_daily_exhaustion_requires_daily_evidence(status, message, daily):
    assert uw.looks_like_daily_limit(status, message) is daily


def test_concurrent_limit_retries_and_does_not_disable_feed(isolated_budget, monkeypatch):
    replies = [Response(429, CONCURRENCY_ERROR, {"Retry-After": "1"}), Response()]
    monkeypatch.setattr(uw.requests, "get", lambda *a, **k: replies.pop(0))
    waits = []
    monkeypatch.setattr(uw.time, "sleep", waits.append)
    assert uw.UWClient(key="test", cache_path=None).get("/test").ok
    assert waits == [1]
    assert uw.budget().usage()["calls"] == 2
    assert not uw.budget().usage()["paused"]


def test_legacy_false_pause_recovers_without_resetting_cap(isolated_budget, monkeypatch):
    isolated_budget.write_text(json.dumps({"date": uw._utc_today(), "calls": 12,
        "paused_reason": "HTTP 429 " + CONCURRENCY_ERROR, "paused_at": "2026-09-23T10:00:00Z"}))
    monkeypatch.setenv("UNUSUAL_WHALES_DAILY_CAP", "12")
    b = uw.DailyBudget(isolated_budget)
    assert "daily call budget used up" in b.check()
    assert b.usage()["calls"] == 12 and not b.usage()["paused"]
    saved = json.loads(isolated_budget.read_text())
    assert "concurrent requests" in saved["recovered_pause"]["reason"]
    assert uw.DailyBudget(isolated_budget).usage()["calls"] == 12


def test_true_daily_pause_is_preserved(isolated_budget):
    b = uw.DailyBudget(isolated_budget)
    b.count(9)
    b.pause("HTTP 429 daily limit reached")
    assert uw.DailyBudget(isolated_budget).usage()["paused"]
    assert b.usage()["calls"] == 9


def test_long_retry_after_does_not_retry_early(isolated_budget, monkeypatch):
    calls = []
    monkeypatch.setattr(uw.requests, "get", lambda *a, **k: (calls.append(1), Response(429, "slow down", {"Retry-After": "120"}))[1])
    r = uw.UWClient(key="test", cache_path=None).get("/test")
    assert r.status == 429 and not r.ok and len(calls) == 1
    assert not uw.budget().usage()["paused"]


def test_options_flow_obeys_shared_budget_and_temporary_retry(isolated_budget, monkeypatch):
    from qmag.context.sources import _uw_get
    replies = [Response(429, CONCURRENCY_ERROR), Response()]
    monkeypatch.setattr(uw.requests, "get", lambda *a, **k: replies.pop(0))
    monkeypatch.setattr(uw.time, "sleep", lambda seconds: None)
    errors = {}
    headers = {"Authorization": "Bearer explicit-test-key"}
    assert _uw_get("/test", headers, None, errors, "flow") == [1]
    assert not errors and uw.budget().usage()["calls"] == 2
    monkeypatch.setenv("UNUSUAL_WHALES_DAILY_CAP", "2")
    assert _uw_get("/test", headers, None, errors, "flow") is None
    assert "daily call budget used up" in errors["flow"]


def _network_worker(path, active, peak, successes, start):
    os.environ["UNUSUAL_WHALES_BUDGET_FILE"] = path
    os.environ.pop("UNUSUAL_WHALES_DAILY_CAP", None)
    uw.throttle = lambda rpm=None: None

    def get(*args, **kwargs):
        with active.get_lock():
            active.value += 1
            peak.value = max(peak.value, active.value)
        threading.Event().wait(.15)
        with active.get_lock():
            active.value -= 1
        return Response()

    uw.requests.get = get
    start.wait(15)
    if uw.UWClient(key="test", cache_path=None).get("/test").ok:
        with successes.get_lock():
            successes.value += 1


def test_daemon_and_dashboard_share_network_slot(isolated_budget):
    ctx = multiprocessing.get_context("spawn")
    active, peak, successes = (ctx.Value("i", 0) for _ in range(3))
    start = ctx.Event()
    workers = [ctx.Process(target=_network_worker, args=(str(isolated_budget), active, peak, successes, start)) for _ in range(3)]
    try:
        for worker in workers:
            worker.start()
        start.set()
        for worker in workers:
            worker.join(30)
            assert worker.exitcode == 0
        assert successes.value == 3 and peak.value == 1
        assert uw.DailyBudget(isolated_budget).usage()["calls"] == 3
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(5)


def test_operations_reports_partial_failures_without_reviving_old_errors(tmp_path):
    from datetime import datetime, timezone
    from qmag.operations import status
    (tmp_path / "daemon_status.json").write_text(json.dumps({"heartbeat": datetime.now(timezone.utc).isoformat()}))
    (tmp_path / "connections.json").write_text(json.dumps({
        "broker": {"ok": True, "last_error": "old socket failure"},
        "uw_edge": {"ok": True, "degraded": True, "detail": "2/20 available", "last_error": "old unrelated error"},
    }))
    result = status(tmp_path)
    assert result["status"] == "attention"
    assert len(result["issues"]) == 1
    assert result["issues"][0]["code"] == "connection_degraded"
    assert result["issues"][0]["detail"] == "uw_edge: 2/20 available"
