"""Exercise the installed SDK loop driver; fake only the network boundary."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import pytest

ib_async = pytest.importorskip("ib_async")

from qmag.broker import IBKRBroker
from qmag.data import IBKRProvider


@pytest.fixture
def offline_sdk(monkeypatch):
    async def connect(self, *args, **kwargs):
        # Actually await a Future owned by the executing thread's loop.
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        loop.call_soon(future.set_result, self)
        return await future

    monkeypatch.setattr(ib_async.IB, "connectAsync", connect)
    monkeypatch.setattr(ib_async.IB, "managedAccounts", lambda self: ["DU_TEST"])
    monkeypatch.setattr(ib_async.IB, "reqMarketDataType", lambda *args: None)
    monkeypatch.setattr(ib_async.IB, "accountSummary", lambda self: [
        SimpleNamespace(account="DU_TEST", tag=tag, value="10000", currency="USD")
        for tag in ("NetLiquidation", "TotalCashValue")
    ])
    monkeypatch.delenv("IBKR_ACCOUNT", raising=False)


def test_real_sdk_broker_and_data_use_each_worker_loop(offline_sdk):
    # 2.0.1 cached this loop process-wide: both later worker connections failed.
    main_loop = ib_async.util.getLoop()
    barrier = threading.Barrier(2)

    def work(kind):
        barrier.wait(timeout=10)
        try:
            if kind == "broker":
                broker = IBKRBroker()
                try:
                    assert broker.account().currency == "USD"
                finally:
                    broker.ib.disconnect()
            else:
                provider = IBKRProvider(client_id=1000018)
                try:
                    provider._connect()
                finally:
                    provider._disconnect()
            loop = asyncio.get_event_loop_policy().get_event_loop()
            assert loop is ib_async.util.getLoop()
            assert loop is not main_loop
            return loop
        finally:
            asyncio.get_event_loop_policy().get_event_loop().close()
            asyncio.set_event_loop(None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(work, kind) for kind in ("broker", "data")]
        loops = [job.result(timeout=15) for job in jobs]
    assert loops[0] is not loops[1]


@pytest.mark.parametrize("kind", ["broker", "data"])
def test_real_sdk_replaces_closed_worker_loop(offline_sdk, kind):
    def work():
        closed = asyncio.new_event_loop()
        asyncio.set_event_loop(closed)
        closed.close()
        try:
            if kind == "broker":
                broker = IBKRBroker()
                try:
                    assert broker.account().equity == 10000
                finally:
                    broker.ib.disconnect()
            else:
                provider = IBKRProvider()
                try:
                    provider._connect()
                finally:
                    provider._disconnect()
            assert ib_async.util.getLoop() is not closed
        finally:
            asyncio.get_event_loop_policy().get_event_loop().close()
            asyncio.set_event_loop(None)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(work).result(timeout=15)


def test_runtime_rejects_sdk_bound_to_another_loop_before_connect(offline_sdk, monkeypatch):
    main_loop = ib_async.util.getLoop()
    monkeypatch.setattr(ib_async.util, "getLoop", lambda: main_loop)

    def work():
        try:
            with pytest.raises(RuntimeError, match="SDK selected another thread"):
                IBKRBroker()
        finally:
            asyncio.get_event_loop_policy().get_event_loop().close()
            asyncio.set_event_loop(None)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(work).result(timeout=15)


def test_runtime_refuses_sync_broker_on_running_web_loop(offline_sdk):
    async def work():
        with pytest.raises(RuntimeError, match="outside an already running"):
            IBKRBroker()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, work()).result(timeout=15)
