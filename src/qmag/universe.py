"""Symbol universes.

Kullamägi scans the *whole* US market for the top 1-2 % momentum names, so
the tool can build that list itself:

* ``fetch_us_listed()`` pulls every NASDAQ / NYSE / NYSE American / Arca /
  BATS listing from Nasdaq Trader's public symbol directories (no key) and
  drops ETFs, test issues, warrants, rights, units, preferreds, notes and
  funds - leaving common stocks and ADRs.
* ``build_universe()`` downloads a year of bars for all of them (batched,
  cached), keeps the tradeable ones (price and dollar-volume floors) and
  writes ``universe/market.txt``.

Once ``universe/market.txt`` exists it becomes the default scan list; the
bundled ``universe/default.txt`` is only the fallback for a fresh checkout.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

import pandas as pd

log = logging.getLogger(__name__)

UNIVERSE_DIR = Path(__file__).resolve().parents[2] / "universe"
DEFAULT_UNIVERSE_FILE = UNIVERSE_DIR / "default.txt"
MARKET_UNIVERSE_FILE = UNIVERSE_DIR / "market.txt"

NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
EXCHANGE_NAMES = {"Q": "NASDAQ", "N": "NYSE", "A": "NYSE American", "P": "NYSE Arca", "Z": "BATS", "V": "IEX"}

_NON_COMMON = re.compile(
    r"\b(?:warrant|warrants|right|rights|unit|units|preferred|preference|depositary shares?, each representing .*preferred|"
    r"notes?|debentures?|bond|fund|trust units|etn|closed[- ]end|\d+(?:\.\d+)?%)\b",
    re.IGNORECASE,
)


def load_universe(path: str | Path | None = None, symbols: str | None = None) -> list[str]:
    if symbols:
        return sorted({s.strip().upper() for s in symbols.split(",") if s.strip()})
    if path:
        file = Path(path)
    else:
        file = MARKET_UNIVERSE_FILE if MARKET_UNIVERSE_FILE.exists() else DEFAULT_UNIVERSE_FILE
    if not file.exists():
        raise FileNotFoundError(f"Universe file not found: {file}")
    out: list[str] = []
    for line in file.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line.upper())
    return sorted(set(out))


# --------------------------------------------------------------------------- #
# Whole-market listing
# --------------------------------------------------------------------------- #
def _get(url: str, timeout: int = 60) -> str:
    import requests

    resp = requests.get(url, timeout=timeout, headers={"User-Agent": "qmag/0.1"})
    resp.raise_for_status()
    return resp.text


def parse_listings(nasdaq_text: str, other_text: str) -> pd.DataFrame:
    """Combine the two Nasdaq Trader directories into symbol / name / exchange rows of common stock."""
    n = pd.read_csv(io.StringIO(nasdaq_text), sep="|")
    o = pd.read_csv(io.StringIO(other_text), sep="|")
    n = n[n["Symbol"].notna() & ~n["Symbol"].astype(str).str.startswith("File Creation")]
    o = o[o["ACT Symbol"].notna() & ~o["ACT Symbol"].astype(str).str.startswith("File Creation")]

    n = n[(n["Test Issue"] == "N") & (n["ETF"] != "Y")]
    o = o[(o["Test Issue"] == "N") & (o["ETF"] != "Y")]
    rows = pd.concat(
        [
            pd.DataFrame({"symbol": n["Symbol"], "name": n["Security Name"], "exchange": "Q"}),
            pd.DataFrame({"symbol": o["ACT Symbol"], "name": o["Security Name"], "exchange": o["Exchange"]}),
        ]
    )
    rows["symbol"] = rows["symbol"].astype(str).str.strip().str.upper()
    rows["name"] = rows["name"].astype(str)
    rows = rows[rows["symbol"].str.fullmatch(r"[A-Z]{1,5}")]
    rows = rows[~rows["name"].str.contains(_NON_COMMON)]
    rows["exchange"] = rows["exchange"].map(EXCHANGE_NAMES).fillna(rows["exchange"])
    return rows.drop_duplicates("symbol").sort_values("symbol").reset_index(drop=True)


def fetch_us_listed() -> pd.DataFrame:
    return parse_listings(_get(NASDAQ_LISTED), _get(OTHER_LISTED))


@dataclass
class UniverseBuildResult:
    listed: int
    downloaded: int
    kept: int
    path: Path
    rejected_price: int
    rejected_volume: int
    rejected_history: int


def build_universe(
    provider,
    out_path: str | Path = MARKET_UNIVERSE_FILE,
    min_price: float = 3.0,
    min_dollar_volume: float = 5_000_000.0,
    min_bars: int = 60,
    limit: int | None = None,
    listings: pd.DataFrame | None = None,
    progress: Callable[[str], None] | None = None,
) -> UniverseBuildResult:
    """Download the whole market and keep the names liquid enough to trade.

    The filters here are deliberately looser than the strategy's momentum
    screen: this list is *what we look at*, the detectors decide what
    qualifies. ``min_bars`` keeps recent IPOs that cannot have a 6-month
    return yet but can already produce episodic pivots.
    """
    listings = listings if listings is not None else fetch_us_listed()
    symbols = listings["symbol"].tolist()
    if limit:
        symbols = symbols[:limit]
    if progress:
        progress(f"{len(symbols)} listed common stocks; downloading history...")

    start = str((pd.Timestamp.today().normalize() - pd.Timedelta(days=400)).date())
    frames = provider.load(symbols, start=start)

    kept: list[tuple[str, float, float]] = []
    rej_price = rej_vol = rej_hist = 0
    for sym, df in frames.items():
        if len(df) < min_bars:
            rej_hist += 1
            continue
        last = df.iloc[-1]
        dollar_vol = float((df["close"] * df["volume"]).tail(20).mean())
        if last["close"] < min_price:
            rej_price += 1
            continue
        if dollar_vol < min_dollar_volume:
            rej_vol += 1
            continue
        kept.append((sym, float(last["close"]), dollar_vol))

    kept.sort(key=lambda r: -r[2])
    names = dict(zip(listings["symbol"], listings["name"]))
    exch = dict(zip(listings["symbol"], listings["exchange"]))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Whole-market universe built {date.today().isoformat()}",
        f"# {len(kept)} of {len(frames)} downloaded ({len(symbols)} listed) passed: price >= ${min_price:g}, "
        f"20d avg $ volume >= ${min_dollar_volume:,.0f}, >= {min_bars} bars",
        "# symbol  # exchange | last close | avg $ volume | name",
    ]
    for sym, px, dv in kept:
        lines.append(f"{sym:<6}  # {exch.get(sym, '?')} | {px:,.2f} | {dv / 1e6:,.1f}M | {names.get(sym, '')[:60]}")
    out_path.write_text("\n".join(lines) + "\n")
    return UniverseBuildResult(len(symbols), len(frames), len(kept), out_path, rej_price, rej_vol, rej_hist)
