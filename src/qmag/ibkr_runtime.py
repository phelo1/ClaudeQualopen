"""Check and recover the synchronous IBKR worker's asyncio runtime."""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


def prepare_ibkr_loop() -> asyncio.AbstractEventLoop:
    """Repair absent/closed thread loops; reject SDKs that bind another loop.

    Must run in the synchronous worker before constructing an IB connection.
    Never move an existing connection or pending order to another thread.
    """
    try:
        loop = asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        loop = None
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        log.info("IBKR runtime recovered: initialized this worker's event loop")
    if loop.is_running():
        raise RuntimeError("Synchronous IBKR calls must run in a worker thread, outside an already running event loop")

    from ib_async import util

    if util.getLoop() is not loop:
        raise RuntimeError(
            "IBKR runtime check failed: the SDK selected another thread's event loop. "
            "Install qmag[ibkr] with ib_async>=2.1.0,<3 and the compatible dependency snapshot, then restart the app"
        )
    return loop
