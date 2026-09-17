"""Per-symbol fundamentals snapshot (finviz) and the industry themes built from it.

The curated ``universe/themes.yaml`` lists the narratives a human would name
(AI infrastructure, GLP-1, uranium...). finviz's industry taxonomy covers the
*whole* market, so every liquid stock also belongs to an auto-theme such as
``Industry: Semiconductors`` and can be ranked against its peers even when
nobody has written a theme for it yet.

``qmag universe build`` refreshes ``universe/fundamentals.csv`` (sector,
industry, market cap, float, short interest, next earnings) alongside the
ticker list; the daemon does the same on its weekly rebuild.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd

from .universe import UNIVERSE_DIR

log = logging.getLogger(__name__)

FUNDAMENTALS_FILE = UNIVERSE_DIR / "fundamentals.csv"
COLUMNS = ["symbol", "sector", "industry", "market_cap", "float_shares", "short_float_pct", "earnings", "updated"]


def fetch_one(symbol: str, now: datetime | None = None) -> dict | None:
    """One finviz quote page -> flat row. Returns None when finviz has no page."""
    from .context.sources import _num, _parse_finviz_earnings

    try:
        from finvizfinance.quote import finvizfinance as Quote
    except ImportError as exc:  # pragma: no cover - dependency is declared, but be explicit
        raise RuntimeError("pip install finvizfinance") from exc
    now = now or datetime.now(timezone.utc)
    fund = Quote(symbol).ticker_fundament() or {}
    if not fund.get("Sector"):
        return None
    return {
        "symbol": symbol,
        "sector": fund.get("Sector") or "",
        "industry": fund.get("Industry") or "",
        "market_cap": _num(fund.get("Market Cap")),
        "float_shares": _num(fund.get("Shs Float")),
        "short_float_pct": _num(fund.get("Short Float")),
        "earnings": _parse_finviz_earnings(fund.get("Earnings"), now) or "",
        "updated": date.today().isoformat(),
    }


def fetch_fundamentals(
    symbols: Iterable[str],
    workers: int = 4,
    pause: float = 0.05,
    retries: int = 3,
    progress: Callable[[str], None] | None = None,
    fetch: Callable[[str], dict | None] = fetch_one,
) -> pd.DataFrame:
    """Threaded, polite crawl of finviz quote pages with backoff on throttling."""
    symbols = list(dict.fromkeys(s.upper() for s in symbols))
    rows: list[dict] = []
    lock = threading.Lock()
    done = 0

    def job(sym: str) -> dict | None:
        delay = 2.0
        for attempt in range(retries):
            try:
                row = fetch(sym)
                time.sleep(pause)
                return row
            except Exception as exc:  # 429 / connection resets: back off and retry
                msg = str(exc).lower()
                if attempt == retries - 1 or not any(k in msg for k in ("429", "too many", "timed out", "connection", "503", "502")):
                    log.debug("fundamentals %s: %s", sym, exc)
                    return None
                time.sleep(delay)
                delay *= 2
        return None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(job, s): s for s in symbols}
        for fut in as_completed(futures):
            row = fut.result()
            with lock:
                done += 1
                if row:
                    rows.append(row)
                if progress and done % 100 == 0:
                    progress(f"fundamentals {done}/{len(symbols)} ({len(rows)} found)")
    df = pd.DataFrame(rows, columns=COLUMNS)
    return df.sort_values("symbol").reset_index(drop=True)


def save_fundamentals(df: pd.DataFrame, path: str | Path = FUNDAMENTALS_FILE, merge_existing: bool = True) -> Path:
    """Write the snapshot; by default keep rows for symbols that failed this time."""
    path = Path(path)
    if merge_existing and path.exists():
        old = load_fundamentals(path)
        if old is not None and not old.empty:
            df = pd.concat([old[~old["symbol"].isin(df["symbol"])], df]).sort_values("symbol")
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return path


def load_fundamentals(path: str | Path | None = None) -> pd.DataFrame | None:
    file = Path(path) if path else FUNDAMENTALS_FILE
    if not file.exists():
        return None
    try:
        df = pd.read_csv(file, dtype={"symbol": str, "sector": str, "industry": str, "earnings": str, "updated": str})
    except Exception:
        return None
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = None
    df["symbol"] = df["symbol"].str.upper()
    return df.drop_duplicates("symbol", keep="last").reset_index(drop=True)


INDUSTRY_PREFIX = "Industry: "


def industry_themes(fund: pd.DataFrame | None, symbols: Iterable[str] | None = None, min_members: int = 4) -> dict[str, list[str]]:
    """One theme per finviz industry with at least ``min_members`` (optionally restricted to ``symbols``)."""
    if fund is None or fund.empty:
        return {}
    df = fund[fund["industry"].notna() & (fund["industry"].astype(str).str.strip() != "")]
    if symbols is not None:
        wanted = {s.upper() for s in symbols}
        df = df[df["symbol"].isin(wanted)]
    out: dict[str, list[str]] = {}
    for industry, group in df.groupby("industry"):
        members = sorted(group["symbol"].tolist())
        if len(members) >= min_members:
            out[f"{INDUSTRY_PREFIX}{industry}"] = members
    return out


def facts_for(fund: pd.DataFrame | None, symbol: str) -> dict:
    """Cached sector / industry / float / short-interest for one symbol (empty dict if unknown)."""
    if fund is None or fund.empty:
        return {}
    row = fund[fund["symbol"] == symbol.upper()]
    if row.empty:
        return {}
    r = row.iloc[0]
    return {k: (None if pd.isna(r[k]) else r[k]) for k in ("sector", "industry", "market_cap", "float_shares", "short_float_pct", "earnings")}
