"""Local scheduler/dashboard supervisor. No credentials are passed on argv."""
from __future__ import annotations
from datetime import datetime, timezone
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .daemon import InstanceLock
from .persistence import atomic_json


def run(*, broker, data, state_dir: Path, config=None, port=8765, live_confirmed=False):
    state_dir = Path(state_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    lock = InstanceLock(state_dir / "supervisor.lock")
    lock.acquire()
    common = ["--broker", broker, "--data", data, "--state-dir", str(state_dir)]
    if config:
        common += ["--config", str(Path(config).resolve())]
    commands = {"daemon": [*common, *(["--yes-live"] if live_confirmed else [])], "dashboard": [*common, "--host", "127.0.0.1", "--port", str(port)]}
    children, counts, next_start, logs, started = {}, {}, {}, {}, {}
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        while not stopping:
            now = time.monotonic()
            for name, args in commands.items():
                child = children.get(name)
                if child and name == 'daemon' and child.poll() is None and now-started[name] > 300:
                    from .watchdog import inspect
                    health = inspect(state_dir)
                    if health['stalled'] and health.get('pid', child.pid) == child.pid:
                        child.terminate()
                        try:
                            child.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait()
                if child and child.poll() is not None:
                    children.pop(name)
                    logs.pop(name).close()
                    counts[name] = (counts.get(name, 0)+1) if now-started[name] < 300 else 1
                    next_start[name] = now + min(300, 5*2**min(counts[name], 6))
                if name not in children and now >= next_start.get(name, 0):
                    path = state_dir / f"{name}.log"
                    if path.exists() and path.stat().st_size > 10_000_000:
                        os.replace(path, path.with_suffix(".previous.log"))
                    logs[name] = path.open("a", encoding="utf-8")
                    children[name] = subprocess.Popen([sys.executable, "-m", "qmag.cli", name, *args], stdout=logs[name], stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                    started[name] = now
            atomic_json(state_dir / "supervisor.json", {"at": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(), "children": {n:p.pid for n,p in children.items()}, "restarts": counts})
            time.sleep(1)
    finally:
        for child in children.values():
            child.terminate()
        for child in children.values():
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        for log in logs.values():
            log.close()
        lock.release()
