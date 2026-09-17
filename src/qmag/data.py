"""Market data providers.

* ``IBKRProvider``     – daily bars from Interactive Brokers (TWS / Gateway)
  through ``ib_async``. ``--data auto`` prefers it whenever the gateway is
  up, so a paper account doubles as the price feed and the Unusual Whales
  budget is kept for flow / edge. Delayed bars without a subscription,
  concurrent requests, volume scale verified against Yahoo, Yahoo fallback
  for the symbols IB cannot serve (recorded in ``stats``).
* ``UnusualWhalesProvider`` – daily candles from the Unusual Whales API
  (``UNUSUAL_WHALES_API_KEY``); the ``auto`` source when a key is present
  and no gateway answers. One request per symbol, throttled, so whole-market
  sweeps above ``bulk_threshold`` symbols come from Yahoo batches unless
  ``UNUSUAL_WHALES_BULK_THRESHOLD=0``; everything smaller (lookups, probes,
  the benchmark, per-candidate refreshes) is read from Unusual Whales.
* ``YFinanceProvider`` – free daily bars for real research / paper trading.
* ``MT5Provider``      – daily bars from a MetaTrader 5 terminal (Windows only,
  ``pip install qmag[mt5]``); symbols are mapped through a broker suffix.
* ``CsvProvider``      – one ``SYMBOL.csv`` per ticker (date,open,high,low,close,volume)
  that *you* supply.

There is deliberately no simulated or generated price source in the product.
Every bar the scanner, the charts and the trader see was either downloaded
from one of the sources above or read from files the operator placed on disk;
``--data synthetic`` is refused. (The automated test suite generates its own
fixture CSVs under ``tests/`` and loads them through ``CsvProvider``.)

Every network provider sits on the same incremental per-symbol CSV cache
class (``CachedDailyProvider``); Yahoo and MT5 share ``data/cache``, while
Unusual Whales (as-traded) and IBKR (split- but not dividend-adjusted) keep
their own sub-directories so differently adjusted histories are never merged
into one file. The cache is never silently trusted: a symbol whose refresh
failed is served with a ``stale`` mark in ``provider.stats`` and retried on
the next load.

Every provider returns ``dict[symbol, DataFrame]`` with lowercase OHLCV
columns on a naive DatetimeIndex.
"""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol

import pandas as pd

from .indicators import OHLCV, validate_ohlcv

log = logging.getLogger(__name__)


class DataProvider(Protocol):
    def load(self, symbols: Iterable[str], start: str | None = None, end: str | None = None) -> dict[str, pd.DataFrame]: ...


# --------------------------------------------------------------------------- #
# Incremental cache shared by the network providers
# --------------------------------------------------------------------------- #
@dataclass
class CachedDailyProvider:
    """Daily bars with a per-symbol CSV cache; subclasses implement ``_download``.

    * A cached symbol is refreshed *incrementally*: only bars after the last
      cached date (minus a small overlap) are fetched and merged.
    * A cache file younger than ``max_age_hours`` is trusted as-is. Pass a
      small value (e.g. 0.25) for intraday cycles that want today's partial
      bar; the next refresh overwrites the partial bar with the final one.
    """

    cache_dir: Path | None = Path("data/cache")
    max_age_hours: float = 1.0
    overlap_days: int = 7
    # Outcome of the last ``load``: what was asked for, what came back, which
    # symbols were served from a cache that could not be refreshed, and the
    # download errors. Read by the session to report data gaps honestly.
    stats: dict = field(default_factory=dict)

    def _download(self, symbols: list[str], start: str | None, end: str | None) -> dict[str, pd.DataFrame]:
        raise NotImplementedError

    def _note_error(self, message: str) -> None:
        self.stats.setdefault("errors", []).append(message)

    def load(self, symbols: Iterable[str], start: str | None = None, end: str | None = None) -> dict[str, pd.DataFrame]:
        symbols = list(dict.fromkeys(s.upper() for s in symbols))
        self.stats = {"requested": len(symbols), "from_cache": 0, "downloaded": 0, "refreshed": 0, "stale": [], "missing": [], "errors": []}
        out: dict[str, pd.DataFrame] = {}
        full_fetch: list[str] = []
        incremental: dict[str, pd.DataFrame] = {}

        for sym in symbols:
            cached = self._read_cache(sym)
            if cached is None or (start is not None and cached.index[0] > pd.Timestamp(start) + pd.Timedelta(days=7)):
                full_fetch.append(sym)
            elif self._is_fresh(sym, cached, end):
                out[sym] = cached
                self.stats["from_cache"] += 1
            else:
                incremental[sym] = cached

        if full_fetch:
            log.info("Downloading full history for %d symbols", len(full_fetch))
            got = self._download(full_fetch, start=start, end=end)
            for sym, df in got.items():
                out[sym] = df
                self._write_cache(sym, df)
            self.stats["downloaded"] = len(got)
            self.stats["missing"] = [s for s in full_fetch if s not in got]

        if incremental:
            log.info("Refreshing %d cached symbols", len(incremental))
            since = min(df.index[-1] for df in incremental.values()) - pd.Timedelta(days=self.overlap_days)
            fresh = self._download(list(incremental), start=str(since.date()), end=end)
            for sym, cached in incremental.items():
                new = fresh.get(sym)
                if new is not None and not new.empty:
                    merged = pd.concat([cached[cached.index < new.index[0]], new])
                    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
                    self._write_cache(sym, merged)
                    out[sym] = merged
                    self.stats["refreshed"] += 1
                else:
                    # The refresh failed: serve what we have but say so, and do
                    # NOT touch the cache file, so the next load tries again.
                    out[sym] = cached
                    self.stats["stale"].append(sym)

        return {s: df.loc[start:end] for s, df in out.items() if len(df.loc[start:end])}

    # -- disk cache ---------------------------------------------------------
    def _cache_path(self, sym: str) -> Path | None:
        return None if self.cache_dir is None else self.cache_dir / f"{sym}.csv"

    def _read_cache(self, sym: str) -> pd.DataFrame | None:
        path = self._cache_path(sym)
        if path is None or not path.exists():
            return None
        try:
            df = pd.read_csv(path, index_col=0, parse_dates=True)
        except Exception:
            return None
        if df.empty:
            return None
        return validate_ohlcv(df)

    def _is_fresh(self, sym: str, cached: pd.DataFrame, end: str | None) -> bool:
        if end is not None:
            return cached.index[-1] >= pd.Timestamp(end) - pd.Timedelta(days=4)
        path = self._cache_path(sym)
        if path is None:
            return True
        age_hours = (time.time() - path.stat().st_mtime) / 3600
        recent = cached.index[-1] >= pd.Timestamp.today().normalize() - pd.Timedelta(days=4)
        return recent and age_hours < self.max_age_hours

    def _write_cache(self, sym: str, df: pd.DataFrame) -> None:
        path = self._cache_path(sym)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(path)



# --------------------------------------------------------------------------- #
# yfinance
# --------------------------------------------------------------------------- #
def _yfinance_pool(threads: int) -> None:
    """Give yfinance a worker pool of the requested size.

    yfinance hands each ticker to ``multitasking``, whose default pool is
    created at import time with one slot per CPU core - and a pool of one
    slot runs everything synchronously.  ``yf.download(threads=N)`` only
    updates the global default for *future* pools, so on a small VM a
    whole-market pull would crawl one ticker at a time.  Creating a named
    pool makes it the active one for the calls that follow.
    """
    try:
        import multitasking

        pool = multitasking.config["POOLS"].get("qmag-yfinance")
        if pool is None or pool.get("threads") != max(2, threads):
            multitasking.createPool(name="qmag-yfinance", threads=max(2, threads), engine="thread")
        else:
            multitasking.config["POOL_NAME"] = "qmag-yfinance"
    except Exception as exc:  # pragma: no cover - only a speed optimisation
        log.debug("could not size the yfinance pool: %s", exc)


@dataclass
class YFinanceProvider(CachedDailyProvider):
    """Daily bars from Yahoo.

    Symbols are downloaded in batches (``batch_size``) so a whole-market
    universe of several thousand names is a few minutes, not an hour.
    """

    batch_size: int = 250
    # yfinance sizes its pool from the CPU count (2 threads on a 1-vCPU VM); the
    # work is network-bound, so pin a sensible number regardless of the host.
    download_threads: int = 8

    def _download(self, symbols: list[str], start: str | None, end: str | None) -> dict[str, pd.DataFrame]:
        import yfinance as yf  # imported lazily: optional at test time

        out: dict[str, pd.DataFrame] = {}
        for i in range(0, len(symbols), self.batch_size):
            batch = symbols[i : i + self.batch_size]
            threads = max(1, min(self.download_threads, len(batch)))
            _yfinance_pool(threads)
            try:
                raw = yf.download(batch, start=start, end=end, auto_adjust=True, progress=False, group_by="ticker", threads=threads)
            except Exception as exc:  # network hiccup: skip the batch, keep going, but record it
                log.warning("Batch download failed (%s...): %s", batch[0], exc)
                self._note_error(f"batch {batch[0]}..{batch[-1]} ({len(batch)} symbols): {type(exc).__name__}: {exc}")
                continue
            if raw is None or raw.empty:
                self._note_error(f"batch {batch[0]}..{batch[-1]} ({len(batch)} symbols): empty response")
                continue
            for sym in batch:
                if isinstance(raw.columns, pd.MultiIndex):
                    if sym not in raw.columns.get_level_values(0):
                        continue
                    frame = raw[sym]
                else:
                    frame = raw
                frame = frame.rename(columns=str.lower)
                if frame.empty or "close" not in frame or frame["close"].dropna().empty:
                    continue
                try:
                    out[sym] = validate_ohlcv(frame)
                except ValueError:
                    continue
        return out


# --------------------------------------------------------------------------- #
# Unusual Whales daily candles
# --------------------------------------------------------------------------- #
def uw_timeframe(start: str | None, end: str | None) -> str:
    """UW ``timeframe`` string ('45D', '3M', '2Y') covering ``start``..``end``."""
    end_ts = pd.Timestamp(end) if end else pd.Timestamp.today().normalize()
    if not start:
        return "2Y"
    days = max(int((end_ts - pd.Timestamp(start)).days), 1)
    if days <= 60:
        return f"{days}D"
    if days <= 730:
        return f"{math.ceil(days / 30)}M"
    return f"{math.ceil(days / 365)}Y"


def uw_candles_to_frame(rows: list[dict]) -> pd.DataFrame | None:
    """Rows from ``/stock/{t}/ohlc/1d`` -> validated OHLCV frame (None when empty)."""
    from .uw import fnum

    recs = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        raw_date = r.get("date") or r.get("start_time") or r.get("end_time")
        if not raw_date:
            continue
        o, h, l, c = fnum(r.get("open")), fnum(r.get("high")), fnum(r.get("low")), fnum(r.get("close"))
        if None in (o, h, l, c):
            continue
        v = fnum(r.get("volume"))
        if v is None or (v == 0 and fnum(r.get("total_volume"))):
            v = fnum(r.get("total_volume")) or 0.0
        try:
            day = pd.Timestamp(str(raw_date)[:10])
        except (TypeError, ValueError):
            continue
        recs.append((day, o, h, l, c, v))
    if not recs:
        return None
    df = pd.DataFrame(recs, columns=["date", "open", "high", "low", "close", "volume"]).set_index("date").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df.index.name = None
    try:
        return validate_ohlcv(df)
    except ValueError:
        return None


@dataclass
class UnusualWhalesProvider(CachedDailyProvider):
    """Daily bars from ``GET /api/stock/{ticker}/ohlc/1d`` (as traded, not split-adjusted).

    Every symbol is one request under the shared throttle, run on a few
    threads. A whole-market sweep (~3,000 names) is therefore 25-30 minutes,
    so loads with more than ``bulk_threshold`` symbols use the Yahoo batch
    downloader for the sweep and record ``stats["bulk_source"]``; set
    ``UNUSUAL_WHALES_BULK_THRESHOLD=0`` to insist on Unusual Whales for
    everything. Failures are recorded per symbol in ``stats["errors"]`` and
    the symbol is left out (or served stale from the cache, flagged).
    """

    bulk_threshold: int | None = None
    workers: int = 4
    # Yahoo candles are split-adjusted and Unusual Whales candles are as
    # traded, so the two must never be merged into the same cache file: the
    # bulk sweep keeps its own directory.
    yahoo_cache_dir: Path | None = None
    _client: object | None = None

    def client(self):
        if self._client is None:
            from .uw import default_client

            self._client = default_client()
        return self._client

    def _threshold(self) -> int:
        if self.bulk_threshold is not None:
            return int(self.bulk_threshold)
        return int(os.environ.get("UNUSUAL_WHALES_BULK_THRESHOLD") or 300)

    def load(self, symbols: Iterable[str], start: str | None = None, end: str | None = None) -> dict[str, pd.DataFrame]:
        symbols = list(dict.fromkeys(s.upper() for s in symbols))
        thr = self._threshold()
        if thr > 0 and len(symbols) > thr:
            log.info("Unusual Whales: %d symbols exceed the bulk threshold (%d); sweeping via Yahoo batches", len(symbols), thr)
            bulk = YFinanceProvider(cache_dir=self.yahoo_cache_dir, max_age_hours=self.max_age_hours)
            out = bulk.load(symbols, start, end)
            self.stats = dict(bulk.stats, bulk_source="yfinance", bulk_symbols=len(symbols))
            return out
        return super().load(symbols, start, end)

    def _download(self, symbols: list[str], start: str | None, end: str | None) -> dict[str, pd.DataFrame]:
        self.stats.setdefault("uw_symbols", 0)
        self.stats["uw_symbols"] += len(symbols)
        tf = uw_timeframe(start, end)
        params = {"timeframe": tf, "limit": 2500}
        if end:
            params["end_date"] = str(pd.Timestamp(end).date())
        client = self.client()
        out: dict[str, pd.DataFrame] = {}

        def one(sym: str) -> tuple[str, pd.DataFrame | None, str | None]:
            resp = client.get(f"/stock/{sym}/ohlc/1d", params)
            if not resp.ok:
                return sym, None, resp.error
            rows = resp.data if isinstance(resp.data, list) else []
            df = uw_candles_to_frame(rows)
            return sym, df, None if df is not None else "no daily candles returned"

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=max(1, self.workers)) as pool:
            for i, (sym, df, err) in enumerate(pool.map(one, symbols)):
                if df is not None:
                    out[sym] = df
                elif err:
                    self._note_error(f"{sym}: {err}")
                if i % 200 == 199:
                    log.info("Unusual Whales candles: %d / %d symbols", i + 1, len(symbols))
        return out


def _duration_from(start: str | None, end: str | None) -> tuple[pd.Timestamp, int]:
    """(end timestamp, calendar days back to ``start``) with sane defaults."""
    end_ts = pd.Timestamp(end) if end else pd.Timestamp.today().normalize() + pd.Timedelta(days=1)
    start_ts = pd.Timestamp(start) if start else end_ts - pd.Timedelta(days=400)
    return end_ts, max(int((end_ts - start_ts).days), 1)


# --------------------------------------------------------------------------- #
# Interactive Brokers historical data
# --------------------------------------------------------------------------- #
IBKR_DEFAULT_PORT = 7497
IBKR_VOLUME_SCALE_FILE = "ibkr_volume_scale.json"
_IBKR_REACH: dict[str, tuple[float, bool]] = {}


def ibkr_endpoint() -> tuple[str, int]:
    """(host, port) of TWS / IB Gateway from ``IBKR_HOST`` / ``IBKR_PORT``."""
    host = os.environ.get("IBKR_HOST") or "127.0.0.1"
    try:
        port = int(os.environ.get("IBKR_PORT") or IBKR_DEFAULT_PORT)
    except ValueError:
        port = IBKR_DEFAULT_PORT
    return host, port


def ibkr_gateway_reachable(host: str | None = None, port: int | None = None, timeout: float = 1.5, ttl: float = 60.0) -> bool:
    """True when something accepts TCP connections on the gateway port.

    A plain socket probe (no API login), cached for ``ttl`` seconds because
    the dashboard asks on every request. This says "the gateway is up", not
    "the login succeeded" - the health probe covers the rest.
    """
    import socket

    h, p = ibkr_endpoint()
    host, port = host or h, int(port or p)
    key = f"{host}:{port}"
    now = time.time()
    hit = _IBKR_REACH.get(key)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]
    try:
        with socket.create_connection((host, port), timeout=timeout):
            ok = True
    except OSError:
        ok = False
    _IBKR_REACH[key] = (now, ok)
    return ok


_IBKR_DATA_BLOCK: dict = {"until": 0.0, "reason": ""}
IBKR_DATA_BLOCK_SECONDS = 3600.0


class IBKRDataUnavailable(RuntimeError):
    """IB answered but will not serve bars to this account (no market-data
    permissions, no route, gateway not logged in)."""


def block_ibkr_data(reason: str, seconds: float = IBKR_DATA_BLOCK_SECONDS) -> None:
    """Keep ``--data auto`` off IBKR for a while after IB refused to serve bars,
    so every cycle is not spent discovering the same missing permission."""
    _IBKR_DATA_BLOCK.update(until=time.time() + seconds, reason=reason)
    log.warning("IBKR price data set aside for %d min: %s", int(seconds // 60), reason)


def ibkr_data_blocked() -> str | None:
    """The reason IBKR data is currently set aside, or None."""
    return _IBKR_DATA_BLOCK["reason"] if time.time() < _IBKR_DATA_BLOCK["until"] else None


def ibkr_data_available() -> bool:
    """Should ``--data auto`` pick IBKR? ``ib_async`` importable, the gateway
    port answering, no recent refusal and ``IBKR_PREFER_DATA`` not ``no``."""
    if (os.environ.get("IBKR_PREFER_DATA") or "yes").strip().lower() in ("no", "0", "false", "off"):
        return False
    try:
        import ib_async  # noqa: F401
    except ImportError:
        return False
    if ibkr_data_blocked():
        return False
    return ibkr_gateway_reachable()


def ib_symbol(sym: str) -> str:
    """qmag/Yahoo ticker -> IB local symbol (``BRK-B`` / ``BRK.B`` -> ``BRK B``)."""
    return sym.upper().replace("-", " ").replace(".", " ").strip()


@dataclass
class IBKRProvider(CachedDailyProvider):
    """Daily TRADES bars from TWS / IB Gateway via ``ib_async``.

    * Own client id (``IBKR_DATA_CLIENT_ID``, default 18) next to the trading
      client; connects for the duration of one ``load`` and disconnects, so
      the gateway's nightly restart never leaves a dead socket behind.
    * Requests run concurrently (``concurrency`` in flight, a small pause
      between launches) so a whole-market cold load is minutes, not hours;
      incremental refreshes are quick.
    * Market data type 3 (delayed) is requested first, so a paper account
      without paid subscriptions still gets end-of-day bars; with
      subscriptions IB serves real-time bars regardless.
    * IB reports US stock volume in lots of 100 on some gateway versions and
      in shares on others. The scale is measured once against Yahoo's
      benchmark volume and persisted (``IBKR_VOLUME_MULTIPLIER`` overrides);
      if it cannot be measured the load is served by Yahoo instead of
      writing volumes that might be 100x off.
    * Symbols IB cannot serve (no security definition, no permission) are
      filled from Yahoo, recorded in ``stats["fallback"]`` - real bars from a
      named source, never a substitute value.
    * IB bars are split- but not dividend-adjusted, Yahoo's are both, so IB
      keeps its own cache directory (``cache_dir``) and Yahoo its own
      (``yahoo_cache_dir``); the two are never merged into one file.
    """

    host: str | None = None
    port: int | None = None
    client_id: int | None = None
    pacing_seconds: float = 0.1
    concurrency: int | None = None
    request_timeout: float = 45.0
    market_data_type: int | None = None
    exchange: str = "SMART"
    currency: str = "USD"
    yahoo_cache_dir: Path | None = None
    fallback: bool | None = None
    volume_multiplier: float | None = None
    benchmark: str = "SPY"
    _ib: object | None = None
    _ib_errors: dict = field(default_factory=dict)
    _wrapper_level: int = logging.NOTSET

    # -- connection ---------------------------------------------------------
    def endpoint(self) -> tuple[str, int]:
        h, p = ibkr_endpoint()
        return self.host or h, int(self.port or p)

    def _connect(self):
        if self._ib is not None and self._ib.isConnected():
            return self._ib
        try:
            from ib_async import IB
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("Install the IBKR extra: pip install 'qmag[ibkr]'") from exc
        host, port = self.endpoint()
        client_id = int(self.client_id or os.environ.get("IBKR_DATA_CLIENT_ID", "18"))
        ib = IB()
        ib.errorEvent += self._on_error
        # ib_async logs every "no security definition" / "no data" answer at
        # ERROR level; a whole-market sweep would flood the journal with the
        # same few messages. They are counted in ``stats["ibkr_errors"]``.
        wrapper_log = logging.getLogger("ib_async.wrapper")
        self._wrapper_level = wrapper_log.level
        wrapper_log.setLevel(logging.CRITICAL)
        ib.connect(host, port, clientId=client_id, readonly=True, timeout=15)
        mdt = int(self.market_data_type or os.environ.get("IBKR_MARKET_DATA_TYPE") or 3)
        try:
            ib.reqMarketDataType(mdt)
        except Exception as exc:  # pragma: no cover - cosmetic
            log.debug("reqMarketDataType(%s) failed: %s", mdt, exc)
        self._ib = ib
        return ib

    def _disconnect(self) -> None:
        ib, self._ib = self._ib, None
        if ib is not None:
            try:
                ib.disconnect()
            except Exception:  # pragma: no cover
                pass
            logging.getLogger("ib_async.wrapper").setLevel(getattr(self, "_wrapper_level", logging.NOTSET))

    def _on_error(self, reqId, errorCode, errorString, *_rest) -> None:
        # Informational codes (farm connection status etc.) are noise.
        if int(errorCode) in (2104, 2106, 2107, 2108, 2158, 2119, 2100, 2103, 2105, 2110, 2137):
            return
        key = f"error {errorCode}: {str(errorString).strip()}"
        self._ib_errors[key] = self._ib_errors.get(key, 0) + 1

    # -- scale of the volume column ------------------------------------------
    def _configured_multiplier(self) -> float | None:
        raw = self.volume_multiplier if self.volume_multiplier is not None else os.environ.get("IBKR_VOLUME_MULTIPLIER")
        if raw in (None, "", "auto"):
            return None
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None

    def _scale_path(self) -> Path | None:
        return None if self.cache_dir is None else Path(self.cache_dir) / IBKR_VOLUME_SCALE_FILE

    def _saved_scale(self) -> dict | None:
        path = self._scale_path()
        if path is None or not path.exists():
            return None
        try:
            import json

            saved = json.loads(path.read_text())
            if time.time() - float(saved.get("measured_at", 0)) < 7 * 86400:
                return saved
        except Exception:
            return None
        return None

    def _saved_multiplier(self) -> float | None:
        saved = self._saved_scale()
        if saved is None:
            return None
        self._note_volume_share(float(saved["multiplier"]), float(saved.get("measured_ratio") or 0))
        return float(saved["multiplier"])

    def _note_volume_share(self, multiplier: float, ratio: float) -> None:
        # IB's historical volume counts lit-exchange prints only (no TRF /
        # dark-pool volume), so even at the right scale it is ~55-75 % of the
        # consolidated figure Yahoo reports. Surface that share so the operator
        # knows the $-volume floor is effectively stricter on IB bars.
        if ratio > 0:
            self.stats["volume_share_of_consolidated"] = round(multiplier / ratio, 2)

    def _save_multiplier(self, multiplier: float, ratio: float) -> None:
        path = self._scale_path()
        if path is None:
            return
        import json

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"multiplier": multiplier, "measured_ratio": ratio, "measured_at": time.time(), "benchmark": self.benchmark}))

    def _yahoo(self, max_age_hours: float | None = None) -> "YFinanceProvider":
        return YFinanceProvider(cache_dir=self.yahoo_cache_dir, max_age_hours=self.max_age_hours if max_age_hours is None else max_age_hours)

    def _measure_multiplier(self, ib_bench: pd.DataFrame | None) -> float | None:
        """Compare IB's benchmark volume with Yahoo's; 1 or 100, else None."""
        if ib_bench is None or ib_bench.empty:
            return None
        try:
            ref = self._yahoo(max_age_hours=24.0).load([self.benchmark], start=str(ib_bench.index[0].date())).get(self.benchmark)
        except Exception as exc:
            self._note_error(f"volume scale check: Yahoo {self.benchmark} failed: {type(exc).__name__}: {exc}")
            return None
        if ref is None or ref.empty:
            return None
        both = ib_bench[["volume"]].join(ref[["volume"]], how="inner", lsuffix="_ib", rsuffix="_ref")
        both = both[(both["volume_ib"] > 0) & (both["volume_ref"] > 0)].tail(15)
        if len(both) < 3:
            return None
        ratio = float((both["volume_ref"] / both["volume_ib"]).median())
        multiplier = 100.0 if 50 <= ratio <= 200 else 1.0 if 0.5 <= ratio <= 2 else None
        if multiplier is not None:
            self._save_multiplier(multiplier, ratio)
            self._note_volume_share(multiplier, ratio)
            log.info("IBKR volume scale: x%g (Yahoo/IB median ratio %.2f on %s)", multiplier, ratio, self.benchmark)
        else:
            self._note_error(f"volume scale check: Yahoo/IB {self.benchmark} volume ratio {ratio:.2f} is neither 1 nor 100")
        return multiplier

    # -- load -----------------------------------------------------------------
    def _fallback_enabled(self) -> bool:
        if self.fallback is not None:
            return self.fallback
        return (os.environ.get("IBKR_DATA_FALLBACK") or "yes").strip().lower() not in ("no", "0", "false", "off")

    def load(self, symbols: Iterable[str], start: str | None = None, end: str | None = None) -> dict[str, pd.DataFrame]:
        symbols = list(dict.fromkeys(s.upper() for s in symbols))
        self._ib_errors = {}
        try:
            out = super().load(symbols, start, end)
        except Exception as exc:
            # Gateway down / login not finished / connection dropped mid-load:
            # say so, and serve the load from Yahoo rather than nothing.
            log.warning("IBKR data unavailable (%s: %s)", type(exc).__name__, exc)
            self._disconnect()
            if isinstance(exc, IBKRDataUnavailable):
                block_ibkr_data(str(exc))
            if not self._fallback_enabled():
                raise
            bulk = self._yahoo()
            out = bulk.load(symbols, start, end)
            self.stats = dict(bulk.stats, source="yfinance", ibkr_error=f"{type(exc).__name__}: {exc}")
            return out
        finally:
            self._disconnect()
        if self._ib_errors:
            top = sorted(self._ib_errors.items(), key=lambda kv: -kv[1])[:5]
            self.stats["ibkr_errors"] = {k: n for k, n in top}
        missing = [s for s in symbols if s not in out]
        if missing and self._fallback_enabled():
            yahoo = self._yahoo()
            filled = yahoo.load(missing, start, end)
            out.update(filled)
            self.stats["fallback"] = {"source": "yfinance", "requested": len(missing), "loaded": len(filled)}
            self.stats["missing"] = [s for s in missing if s not in filled]
            for err in yahoo.stats.get("errors", []):
                self._note_error(f"yahoo fallback: {err}")
        return out

    def _download(self, symbols: list[str], start: str | None, end: str | None) -> dict[str, pd.DataFrame]:
        ib = self._connect()
        end_ts, days = _duration_from(start, end)
        duration = f"{math.ceil(days / 365)} Y" if days > 365 else f"{days} D"
        end_str = "" if end is None else end_ts.strftime("%Y%m%d 23:59:59")
        multiplier = self._configured_multiplier() or self._saved_multiplier()
        # Preflight: one request for the benchmark. An account IB will not
        # serve (no market-data permissions, "No Route Found", gateway not
        # logged in) fails here with one request instead of one per symbol.
        raw = self._fetch(ib, [self.benchmark], duration, end_str)
        if raw.get(self.benchmark) is None:
            why = next(iter(self._ib_errors), "no bars returned")
            raise IBKRDataUnavailable(
                f"IB returned no {self.benchmark} bars ({why}) - the account has no market-data permission for US stocks "
                "(share market data with the paper account / add a US stock feed in Client Portal)"
            )
        raw.update(self._fetch(ib, [s for s in symbols if s != self.benchmark], duration, end_str))
        # A few names come back empty for reasons a second try fixes: an
        # ambiguous SMART contract (e.g. TEVA) wants a primary exchange, and
        # the odd request simply times out. Bounded so a dead feed does not
        # triple the sweep.
        retry = [s for s in symbols if s not in raw]
        if retry and len(retry) <= max(50, len(symbols) // 20):
            for primary in ("NYSE", "NASDAQ"):
                raw.update(self._fetch(ib, [s for s in retry if s not in raw], duration, end_str, primary_exchange=primary))
                if all(s in raw for s in retry):
                    break
        if multiplier is None:
            multiplier = self._measure_multiplier(raw.get(self.benchmark))
            if multiplier is None:
                raise RuntimeError(
                    f"could not verify IBKR's volume scale against Yahoo ({self.benchmark}); set IBKR_VOLUME_MULTIPLIER=1 or 100"
                )
        self.stats["volume_multiplier"] = multiplier
        out: dict[str, pd.DataFrame] = {}
        for sym in symbols:
            df = raw.get(sym)
            if df is None or df.empty:
                continue
            if multiplier != 1.0:
                df = df.assign(volume=df["volume"] * multiplier)
            try:
                out[sym] = validate_ohlcv(df)
            except ValueError:
                continue
        return out

    def _fetch(self, ib, symbols: list[str], duration: str, end_str: str, primary_exchange: str = "") -> dict[str, pd.DataFrame]:
        """Concurrent historical requests -> raw OHLCV frames (volume as IB sent it)."""
        import asyncio

        from ib_async import Stock

        limit = int(self.concurrency or os.environ.get("IBKR_DATA_CONCURRENCY") or 8)
        sem = asyncio.Semaphore(max(1, limit))
        done = 0

        async def one(sym: str):
            nonlocal done
            async with sem:
                contract = Stock(ib_symbol(sym), self.exchange, self.currency, primaryExchange=primary_exchange)
                try:
                    bars = await asyncio.wait_for(
                        ib.reqHistoricalDataAsync(
                            contract, endDateTime=end_str, durationStr=duration, barSizeSetting="1 day", whatToShow="TRADES", useRTH=True, formatDate=1
                        ),
                        timeout=self.request_timeout,
                    )
                except asyncio.TimeoutError:
                    self._note_error(f"{sym}: IBKR request timed out after {self.request_timeout:g}s")
                    bars = None
                except Exception as exc:
                    self._note_error(f"{sym}: {type(exc).__name__}: {exc}")
                    bars = None
                if self.pacing_seconds:
                    await asyncio.sleep(self.pacing_seconds)
            done += 1
            if done % 250 == 0:
                log.info("IBKR history: %d / %d symbols", done, len(symbols))
            return sym, bars

        async def everything():
            # gather() inside the running loop, so the tasks bind to the loop
            # ib_async drives rather than whichever loop is current here.
            return await asyncio.gather(*[one(s) for s in symbols])

        results = ib.run(everything()) if symbols else []
        out: dict[str, pd.DataFrame] = {}
        for sym, bars in results:
            df = ib_bars_to_frame(bars)
            if df is not None:
                out[sym] = df
        return out


def ib_bars_to_frame(bars) -> pd.DataFrame | None:
    """``BarData`` list -> OHLCV frame on a naive daily index (None when empty)."""
    if not bars:
        return None
    rows = []
    for b in bars:
        try:
            day = pd.Timestamp(b.date)
        except (TypeError, ValueError):
            continue
        rows.append((day.normalize().tz_localize(None) if day.tzinfo else day.normalize(), float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume)))
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"]).set_index("date").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df.index.name = None
    # IB marks "no trade" with -1 volume/prices on some bars.
    df = df[(df["close"] > 0) & (df["volume"] >= 0)]
    return df if len(df) else None


# --------------------------------------------------------------------------- #
# MetaTrader 5 (Windows terminal)
# --------------------------------------------------------------------------- #
@dataclass
class MT5Provider(CachedDailyProvider):
    """Daily bars from a running MetaTrader 5 terminal.

    Brokers list US stocks under their own names (``AAPL.NAS``, ``#AAPL``,
    ``AAPL.US``...). Set ``MT5_SYMBOL_PREFIX`` / ``MT5_SYMBOL_SUFFIX`` so the
    NASDAQ ticker maps onto the terminal symbol; the frames returned are keyed
    by the plain ticker so the rest of the pipeline is unchanged. Login
    details come from ``MT5_LOGIN`` / ``MT5_PASSWORD`` / ``MT5_SERVER``
    (optional if the terminal is already logged in) and ``MT5_PATH``.
    """

    symbol_prefix: str | None = None
    symbol_suffix: str | None = None

    def _mt5(self):
        return connect_mt5()

    def terminal_symbol(self, sym: str) -> str:
        prefix = self.symbol_prefix if self.symbol_prefix is not None else os.environ.get("MT5_SYMBOL_PREFIX", "")
        suffix = self.symbol_suffix if self.symbol_suffix is not None else os.environ.get("MT5_SYMBOL_SUFFIX", "")
        return f"{prefix}{sym}{suffix}"

    def _download(self, symbols: list[str], start: str | None, end: str | None) -> dict[str, pd.DataFrame]:
        mt5 = self._mt5()
        end_ts, days = _duration_from(start, end)
        start_dt = (end_ts - pd.Timedelta(days=days)).to_pydatetime()
        end_dt = end_ts.to_pydatetime()
        out: dict[str, pd.DataFrame] = {}
        for sym in symbols:
            tsym = self.terminal_symbol(sym)
            if not mt5.symbol_select(tsym, True):
                log.debug("MT5 symbol not available: %s", tsym)
                continue
            rates = mt5.copy_rates_range(tsym, mt5.TIMEFRAME_D1, start_dt, end_dt)
            if rates is None or len(rates) == 0:
                continue
            df = mt5_rates_to_frame(rates)
            try:
                out[sym] = validate_ohlcv(df)
            except ValueError:
                continue
        return out


def mt5_rates_to_frame(rates) -> pd.DataFrame:
    """Convert MetaTrader's structured ``copy_rates_*`` array to OHLCV."""
    df = pd.DataFrame(rates)
    df = df.set_index(pd.to_datetime(df["time"], unit="s"))
    df.index.name = None
    real = df["real_volume"] if "real_volume" in df else pd.Series(0, index=df.index)
    volume = real.where(real > 0, df["tick_volume"]) if "tick_volume" in df else real
    return pd.DataFrame({"open": df["open"], "high": df["high"], "low": df["low"], "close": df["close"], "volume": volume.astype(float)})


_MT5 = None


def connect_mt5():
    """Import and initialise the MetaTrader5 package once per process."""
    global _MT5
    if _MT5 is not None:
        return _MT5
    try:
        import MetaTrader5 as mt5
    except ImportError as exc:  # pragma: no cover - Windows-only optional dependency
        raise RuntimeError("MetaTrader 5 needs the Windows terminal and: pip install 'qmag[mt5]'") from exc
    kwargs: dict = {}
    if os.environ.get("MT5_PATH"):
        kwargs["path"] = os.environ["MT5_PATH"]
    if os.environ.get("MT5_LOGIN"):
        kwargs.update(login=int(os.environ["MT5_LOGIN"]), password=os.environ.get("MT5_PASSWORD", ""), server=os.environ.get("MT5_SERVER", ""))
    if not mt5.initialize(**kwargs):
        raise RuntimeError(f"MetaTrader5.initialize failed: {mt5.last_error()}")
    _MT5 = mt5
    return mt5


# --------------------------------------------------------------------------- #
# CSV directory
# --------------------------------------------------------------------------- #
@dataclass
class CsvProvider:
    directory: Path

    def load(self, symbols: Iterable[str], start: str | None = None, end: str | None = None) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        for sym in symbols:
            path = Path(self.directory) / f"{sym.upper()}.csv"
            if not path.exists():
                log.warning("Missing CSV for %s at %s", sym, path)
                continue
            df = pd.read_csv(path)
            df.columns = [c.strip().lower() for c in df.columns]
            date_col = "date" if "date" in df.columns else df.columns[0]
            df = df.set_index(pd.to_datetime(df[date_col]))
            out[sym.upper()] = validate_ohlcv(df).loc[start:end]
        return out


DATA_KINDS = ("auto", "unusual_whales", "yfinance", "ibkr", "mt5", "csv")
SIMULATED_KINDS = ("synthetic", "simulated", "fake", "random", "demo")

NO_SIMULATED_DATA = (
    "qmag never uses simulated or generated prices. Choose a market data source (" + ", ".join(DATA_KINDS[1:]) + ") or "
    "supply your own CSV files; there is no synthetic provider in the product."
)


def resolve_data_kind(kind: str) -> str:
    """Normalise a ``--data`` choice to a provider name.

    ``auto`` -> the saved data source (``QMAG_DATA``, set from the settings
    page) when there is one; else IBKR when ``ib_async`` is installed and
    TWS / IB Gateway is answering on ``IBKR_HOST:IBKR_PORT`` (so the paid
    Unusual Whales budget is kept for flow and edge data); else Unusual
    Whales when a key is present; else Yahoo. Aliases are normalised; any
    simulated / generated source is refused.
    """
    k = (kind or "auto").strip().lower()
    if k == "auto":
        saved = (os.environ.get("QMAG_DATA") or "auto").strip().lower()
        if saved != "auto":
            return resolve_data_kind(saved)
        if ibkr_data_available():
            return "ibkr"
        from .uw import has_key

        return "unusual_whales" if has_key() else "yfinance"
    if k in SIMULATED_KINDS:
        raise ValueError(NO_SIMULATED_DATA)
    if k in ("uw", "unusualwhales", "unusual-whales"):
        return "unusual_whales"
    if k == "yahoo":
        return "yfinance"
    if k == "ib":
        return "ibkr"
    if k == "metatrader":
        return "mt5"
    return k


def make_provider(kind: str, **kwargs) -> DataProvider:
    kind = resolve_data_kind(kind)
    cache = dict(cache_dir=Path(kwargs.get("cache_dir", "data/cache")), max_age_hours=float(kwargs.get("max_age_hours", 1.0)))
    if kind == "unusual_whales":
        root = cache["cache_dir"]
        return UnusualWhalesProvider(
            cache_dir=root / "uw", max_age_hours=cache["max_age_hours"], yahoo_cache_dir=root, bulk_threshold=kwargs.get("bulk_threshold")
        )
    if kind in ("yfinance", "yahoo"):
        return YFinanceProvider(**cache)
    if kind in ("ibkr", "ib"):
        root = cache["cache_dir"]
        return IBKRProvider(
            cache_dir=root / "ibkr", max_age_hours=cache["max_age_hours"], yahoo_cache_dir=root,
            host=kwargs.get("host"), port=kwargs.get("port"), client_id=kwargs.get("client_id"),
        )
    if kind in ("mt5", "metatrader"):
        return MT5Provider(**cache)
    if kind == "csv":
        return CsvProvider(directory=Path(kwargs["directory"]))
    raise ValueError(f"Unknown data provider '{kind}'. Choose one of {', '.join(DATA_KINDS)}")
