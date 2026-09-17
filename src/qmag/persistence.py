"""Atomic files and a reentrant, process-wide writer lease for each desk.

The lease coordinates the CLI, dashboard and daemon on one machine. State must
live on a local disk: this is not a distributed lock or an order transaction.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}
_depth = threading.local()


def atomic_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        with temp.open('w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def atomic_json(path: Path, value) -> None:
    atomic_text(path, json.dumps(value, indent=2, default=str, allow_nan=False))


@contextmanager
def desk_lock(directory: Path, timeout: float = 120):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    key = os.path.normcase(str(directory))
    with _guard:
        mutex = _locks.setdefault(key, threading.RLock())
    if not mutex.acquire(timeout=timeout):
        raise TimeoutError('This desk is busy. Try again when the active operation finishes.')
    depths = getattr(_depth, 'values', None)
    if depths is None:
        depths = _depth.values = {}
    stream = None
    locked = False
    try:
        if not depths.get(key):
            stream = (directory / '.writer.lock').open('a+b')
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b'0')
                stream.flush()
            deadline = time.monotonic() + timeout
            while True:
                try:
                    stream.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Another process is writing this desk.')
                    time.sleep(.05)
        depths[key] = depths.get(key, 0) + 1
        try:
            yield
        finally:
            depths[key] -= 1
    finally:
        if stream:
            if locked:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()
        mutex.release()


def serialized(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with desk_lock(self.state_dir):
            # A different process may have changed the paper ledger since the
            # cached broker was created. Refresh only at the outer boundary.
            if not getattr(self, '_inside_write', False):
                broker = getattr(self, '_broker', None)
                if broker is not None and hasattr(broker, 'reload'):
                    broker.reload()
                self._inside_write = True
                try:
                    return method(self, *args, **kwargs)
                finally:
                    # IBKR order IDs are scoped to the desk's fixed client ID.
                    # Release its socket before another process/thread holding
                    # this same desk lease connects (dashboard and daemon).
                    broker = getattr(self, '_broker', None)
                    if broker is not None and str(getattr(broker, 'name', '')).startswith('ibkr') and hasattr(broker, 'ib'):
                        try:
                            broker.ib.disconnect()
                        except Exception:
                            pass  # discard the socket and reconnect at the next operation
                        finally:
                            self._broker = None
                    self._inside_write = False
            return method(self, *args, **kwargs)
    return wrapped
