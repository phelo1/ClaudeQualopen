"""24/7 scheduler: runs the trading routine on the US market clock.

Tiered schedule (``schedule.tiered``, the default; all times America/New_York,
weekdays that are not NYSE holidays):

    08:30, 09:20  pre-market gap scan   one market-wide screener request;
                                        gappers join the arming list
    09:40         post-open full scan   whole universe on the day's first
                                        bars - catches episodic pivots
    09:35 .. close-5 min, every 5 min   focused cycle: arming list, open
                                        positions, pending entries and the
                                        day's screener hits only; volume
                                        judged on pace; exits managed
    10:00 .. close-30 min, every 30 min movers sweep: one screener call for
                                        the day's gainers on relative volume
    close + 20 min  nightly arming scan whole universe on completed bars,
                                        size and chart tomorrow's plans, park
                                        buy-stop brackets, rebuild the arming
                                        list
    Saturday 10:00  insider scan        the week's most unusual options
                                        trades flagged for investigation,
                                        AI catalyst analysis
    Sunday 12:00    universe            rebuild the whole-market ticker list

Legacy schedule (``tiered: false``): 09:45 post-open, 30-minute whole-
universe intraday cycles, after-close cycle, plus the weekend tasks.

Every ``times_on`` callable reads the live strategy config, so changing the
schedule on the settings page takes effect at the next tick without a
restart. The loop is deliberately simple: compute the next due task, sleep
until then (writing a heartbeat file the dashboard shows), run it, catch and
log any exception so one bad cycle never takes the process down. Run it
under systemd, Docker or any supervisor that restarts on failure.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import time as _time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable

from .market_calendar import (  # noqa: F401 - re-exported for callers that import the calendar from here
    NY,
    is_trading_day,
    market_close,
    nyse_early_closes,
    nyse_holidays,
    parse_hhmm,
)
from .session import TradingSession

log = logging.getLogger(__name__)

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
TASK_NAMES = ("premarket", "post_open", "focused", "movers", "intraday", "after_close", "insider_scan", "learn", "universe")


# --------------------------------------------------------------------------- #
# Single instance
# --------------------------------------------------------------------------- #
class AlreadyRunning(RuntimeError):
    """Another daemon holds the lock on this state directory."""

    def __init__(self, path: Path, pid: int, since: str):
        super().__init__(f"another qmag daemon (pid {pid}, since {since}) is already running on {path.parent}; stop it first or use a different --state-dir")
        self.path, self.pid, self.since = path, pid, since


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # access denied means it exists
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


class InstanceLock:
    """A PID file that stops two daemons trading the same state directory.

    A second instance would place every order twice, so ``acquire`` refuses
    when the recorded process is still alive and quietly takes over a lock
    whose owner has died (a crash or a ``kill -9`` leaves the file behind).
    """

    def __init__(self, path: Path):
        self.path = path
        self.held = False

    def read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None

    def owner(self) -> int | None:
        """PID of a live daemon holding this lock, or None."""
        info = self.read() or {}
        pid = int(info.get("pid") or 0)
        return pid if pid != os.getpid() and _pid_alive(pid) else None

    def acquire(self) -> None:
        info = self.read() or {}
        pid = int(info.get("pid") or 0)
        if pid and pid != os.getpid() and _pid_alive(pid):
            raise AlreadyRunning(self.path, pid, str(info.get("since", "?")))
        if pid and pid != os.getpid():
            log.warning("stale daemon lock from pid %s (not running) - taking over", pid)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"pid": os.getpid(), "since": datetime.now(NY).isoformat(timespec="seconds")}))
        self.held = True

    def release(self) -> None:
        if not self.held:
            return
        info = self.read() or {}
        if int(info.get("pid") or 0) == os.getpid():
            try:
                self.path.unlink()
            except OSError:
                pass
        self.held = False


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #
@dataclass
class Task:
    name: str
    times_on: Callable[[date], list[time]]
    run: Callable[[], object]
    last_run: datetime | None = None
    last_error: str | None = None
    runs: int = 0

    def next_after(self, now: datetime) -> datetime:
        day = now.date()
        for _ in range(14):
            for t in sorted(set(self.times_on(day))):
                candidate = datetime.combine(day, t, tzinfo=NY)
                if candidate > now:
                    return candidate
            day += timedelta(days=1)
        return now + timedelta(days=14)


def _every(d: date, start: time, interval_minutes: int, stop_before_close_minutes: int) -> list[time]:
    close = datetime.combine(d, market_close(d)) - timedelta(minutes=stop_before_close_minutes)
    out, t = [], datetime.combine(d, start)
    while t <= close:
        out.append(t.time())
        t += timedelta(minutes=max(int(interval_minutes), 1))
    return out


def _safe_time(value: str, fallback: time) -> time:
    try:
        return parse_hhmm(value)
    except (ValueError, AttributeError):
        return fallback


# Legacy (non-tiered) times, kept importable for tests and the README.
def _post_open(d: date) -> list[time]:
    return [time(9, 45)] if is_trading_day(d) else []


def _intraday(d: date) -> list[time]:
    return _every(d, time(10, 15), 30, 1) if is_trading_day(d) else []


def _after_close(d: date) -> list[time]:
    if not is_trading_day(d):
        return []
    close = datetime.combine(d, market_close(d)) + timedelta(minutes=20)
    return [close.time()]


def _weekly_universe(d: date) -> list[time]:
    return [time(12, 0)] if d.weekday() == 6 else []


@dataclass
class Daemon:
    session: TradingSession
    rebuild_universe: bool = True
    status_path: Path | None = None
    tasks: list[Task] = field(default_factory=list)
    _stop: bool = False

    def __post_init__(self) -> None:
        s = self.session
        self.status_path = self.status_path or s.state_dir / "daemon_status.json"
        self.tasks = [
            Task("premarket", self._t_premarket, lambda: s.screen_cycle("premarket")),
            Task("post_open", self._t_post_open, lambda: s.cycle(max_age_hours=0.0, label="post_open")),
            Task("focused", self._t_focused, lambda: s.focused_cycle(label="focused")),
            Task("movers", self._t_movers, lambda: s.screen_cycle("movers")),
            Task("intraday", self._t_intraday, lambda: s.cycle(max_age_hours=0.25, label="intraday")),
            Task("after_close", self._t_after_close, lambda: s.cycle(max_age_hours=0.0, label="after_close")),
            Task("insider_scan", self._t_insider, lambda: s.insider_scan()),
            Task("learn", self._t_learn, lambda: s.learn()),
        ]
        # The whole-market universe is always built from Yahoo batches (Unusual
        # Whales has no bulk bars endpoint); see UnusualWhalesProvider.
        if self.rebuild_universe and s.s.data in ("yfinance", "unusual_whales") and not s.s.symbols:
            self.tasks.append(Task("universe", _weekly_universe, self._rebuild_universe))

    # -- schedule from the live config -------------------------------------
    @property
    def _sch(self):
        return self.session.cfg.schedule

    def _t_premarket(self, d: date) -> list[time]:
        sch = self._sch
        if not (sch.tiered and sch.premarket_enabled and is_trading_day(d)):
            return []
        return [t for t in (_safe_time(x, time(0, 0)) for x in sch.premarket_times) if t != time(0, 0)]

    def _t_post_open(self, d: date) -> list[time]:
        sch = self._sch
        if not is_trading_day(d):
            return []
        if not sch.tiered:
            return _post_open(d)
        return [_safe_time(sch.post_open_time, time(9, 40))] if sch.post_open_full_scan else []

    def _t_focused(self, d: date) -> list[time]:
        sch = self._sch
        if not (sch.tiered and is_trading_day(d)):
            return []
        return _every(d, _safe_time(sch.focused_start, time(9, 35)), sch.focused_interval_minutes, sch.focused_stop_before_close_minutes)

    def _t_movers(self, d: date) -> list[time]:
        sch = self._sch
        if not (sch.tiered and sch.movers_enabled and is_trading_day(d)):
            return []
        return _every(d, _safe_time(sch.movers_start, time(10, 0)), sch.movers_interval_minutes, sch.movers_stop_before_close_minutes)

    def _t_intraday(self, d: date) -> list[time]:
        return [] if self._sch.tiered else _intraday(d)

    def _t_after_close(self, d: date) -> list[time]:
        return _after_close(d)

    def _t_insider(self, d: date) -> list[time]:
        ins = self.session.cfg.insider_scan
        if not ins.enabled:
            return []
        weekday = WEEKDAYS.index(ins.weekday) if ins.weekday in WEEKDAYS else 5
        return [_safe_time(ins.run_time, time(10, 0))] if d.weekday() == weekday else []

    def _t_learn(self, d: date) -> list[time]:
        ln = self.session.cfg.learning
        if not ln.enabled:
            return []
        weekday = WEEKDAYS.index(ln.review_weekday) if ln.review_weekday in WEEKDAYS else 5
        return [_safe_time(ln.review_time, time(11, 0))] if d.weekday() == weekday else []

    def _rebuild_universe(self) -> object:
        from .data import make_provider
        from .universe import MARKET_UNIVERSE_FILE, build_universe

        out = Path(self.session.s.universe) if self.session.s.universe else MARKET_UNIVERSE_FILE
        provider = make_provider("yfinance", cache_dir=self.session.s.cache_dir, max_age_hours=24)
        result = build_universe(provider, out_path=out)
        if self.session.cfg.themes.use_industries:
            from .fundamentals import FUNDAMENTALS_FILE, fetch_fundamentals, save_fundamentals
            from .universe import load_universe

            try:
                save_fundamentals(fetch_fundamentals(load_universe(out)), FUNDAMENTALS_FILE)
            except Exception as exc:  # a finviz outage must not break the weekly rebuild
                log.warning("Fundamentals refresh failed: %s", exc)
        return result

    # ------------------------------------------------------------------ #
    def schedule(self, now: datetime | None = None, days: int = 7) -> list[tuple[datetime, str]]:
        now = now or datetime.now(NY)
        horizon = now + timedelta(days=days)
        out: list[tuple[datetime, str]] = []
        for task in self.tasks:
            t = now
            while True:
                t = task.next_after(t)
                if t > horizon:
                    break
                out.append((t, task.name))
        return sorted(out)

    def write_status(self, now: datetime, next_task: tuple[datetime, str] | None, running: str | None = None) -> None:
        payload = {
            "heartbeat": now.isoformat(),
            "pid": os.getpid(),
            "broker": self.session.s.broker,
            "live": self.session.s.live,
            "running": running,
            "mode": "tiered" if self._sch.tiered else "legacy",
            "next_task": {"at": next_task[0].isoformat(), "name": next_task[1]} if next_task else None,
            "tasks": [
                {"name": t.name, "runs": t.runs, "last_run": t.last_run.isoformat() if t.last_run else None, "last_error": t.last_error}
                for t in self.tasks
            ],
        }
        self.status_path.write_text(json.dumps(payload, indent=2))

    def run_task(self, name: str) -> object:
        task = next((t for t in self.tasks if t.name == name), None)
        if task is None:
            raise KeyError(f"unknown task '{name}'; choose one of {', '.join(t.name for t in self.tasks)}")
        now = datetime.now(NY)
        self.write_status(now, None, running=name)
        try:
            result = task.run()
            task.last_error = None
            log.info("task %s finished", name)
            return result
        except Exception as exc:
            task.last_error = f"{type(exc).__name__}: {exc}"
            log.error("task %s failed: %s\n%s", name, exc, traceback.format_exc())
            alerts = getattr(self.session, "alerts", None)
            if alerts is not None:
                alerts.failure(f"daemon task {name}", task.last_error, key=f"task:{name}:{now.date()}")
            return None
        finally:
            task.last_run = now
            task.runs += 1

    def stop(self, *_: object) -> None:
        self._stop = True

    def run_forever(self, heartbeat_seconds: int = 60) -> None:
        lock = InstanceLock(self.session.state_dir / "daemon.lock")
        lock.acquire()  # two daemons on one state dir would double every order
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        log.info("daemon started (%s, broker=%s, %s schedule)", "LIVE" if self.session.s.live else "paper", self.session.s.broker, "tiered" if self._sch.tiered else "legacy")
        try:
            self._loop(heartbeat_seconds)
        finally:
            lock.release()

    def _sleep(self, seconds: float) -> None:
        """Sleep in short slices so SIGTERM / Ctrl-C stops the daemon within a second."""
        end = _time.monotonic() + seconds
        while not self._stop:
            left = end - _time.monotonic()
            if left <= 0:
                return
            _time.sleep(min(left, 1.0))

    def _loop(self, heartbeat_seconds: int) -> None:
        while not self._stop:
            now = datetime.now(NY)
            self.session.reload_settings()  # a schedule saved on the settings page applies to the next tick
            upcoming = [(task.next_after(now), task) for task in self.tasks]
            due_at, task = min(upcoming, key=lambda x: x[0])
            log.info("next: %s at %s", task.name, due_at.strftime("%a %Y-%m-%d %H:%M %Z"))
            while not self._stop and datetime.now(NY) < due_at:
                self.write_status(datetime.now(NY), (due_at, task.name))
                remaining = (due_at - datetime.now(NY)).total_seconds()
                self._sleep(max(0.5, min(remaining, heartbeat_seconds)))
            if not self._stop:
                self.run_task(task.name)
        log.info("daemon stopped")
