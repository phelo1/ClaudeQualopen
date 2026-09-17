"""Generated price paths for the automated test suite - and only for it.

qmag itself contains no simulated-data provider: every price the product
scans, charts or trades comes from a market data source (Unusual Whales,
Yahoo, IBKR, MetaTrader) or from CSV files the operator supplies. These
regime-switching random walks exist so the engine can be exercised
deterministically without network access. Tests write them to a temporary
directory as ``SYMBOL.csv`` files and load them through the ordinary CSV
provider, exactly like any other user-supplied data.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from qmag.config import StrategyConfig
from qmag.data import OHLCV


def synthetic_universe(n: int = 80) -> list[str]:
    return [f"SYN{i:03d}" for i in range(n)]


def synthetic_themes(symbols: list[str], n_groups: int = 8) -> dict[str, list[str]]:
    """Round-robin the tickers into themes so the theme filter has groups to rank."""
    groups: dict[str, list[str]] = {f"theme_{k:02d}": [] for k in range(n_groups)}
    for i, sym in enumerate(sorted(symbols)):
        groups[f"theme_{i % n_groups:02d}"].append(sym)
    return {k: v for k, v in groups.items() if len(v) >= 2}


@dataclass
class SyntheticProvider:
    """Regime-switching price paths with impulses, flags, breakouts and gaps.

    Deterministic per (seed, symbol) so tests are reproducible.
    """

    seed: int = 7
    bars: int = 750
    start: str = "2023-01-02"

    def load(self, symbols: Iterable[str], start: str | None = None, end: str | None = None) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        for sym in symbols:
            sym = sym.upper()
            sym_seed = (self.seed * 1_000_003 + sum(ord(c) * (31**k) for k, c in enumerate(sym))) % (2**32)
            out[sym] = self._simulate(np.random.default_rng(sym_seed), benchmark=sym in {"QQQ", "SPY"} or sym.startswith("^")).loc[start:end]
        return out

    def _simulate(self, rng: np.random.Generator, benchmark: bool) -> pd.DataFrame:
        idx = pd.bdate_range(self.start, periods=self.bars)
        n = len(idx)
        price = 20.0 * float(np.exp(rng.normal(0, 0.6)))
        base_vol = 0.012 if benchmark else float(rng.uniform(0.025, 0.05))
        base_volume = float(rng.uniform(0.5e6, 8e6))

        opens = np.empty(n)
        highs = np.empty(n)
        lows = np.empty(n)
        closes = np.empty(n)
        volumes = np.empty(n)

        regime = "chop"
        remaining = int(rng.integers(20, 60))
        prev_close = price
        for t in range(n):
            if remaining <= 0:
                if benchmark:
                    regime = rng.choice(["trend", "chop", "down"], p=[0.5, 0.35, 0.15])
                    remaining = int(rng.integers(30, 90))
                elif regime == "impulse":
                    regime = "flag"
                    remaining = int(rng.integers(10, 40))
                elif regime == "flag":
                    regime = rng.choice(["impulse", "chop", "down"], p=[0.45, 0.35, 0.20])
                    remaining = int(rng.integers(8, 30))
                else:
                    regime = rng.choice(["impulse", "chop", "down"], p=[0.35, 0.4, 0.25])
                    remaining = int(rng.integers(15, 60))
            remaining -= 1

            if regime in ("impulse", "trend"):
                drift, vol, vol_mult = (0.004 if benchmark else 0.012), base_vol * 1.2, 1.4
            elif regime == "flag":
                drift, vol, vol_mult = -0.001, base_vol * 0.55, 0.7
            elif regime == "down":
                drift, vol, vol_mult = -0.005, base_vol * 1.3, 1.1
            else:
                drift, vol, vol_mult = 0.0, base_vol, 1.0

            gap = 0.0
            volume_spike = 1.0
            if not benchmark and rng.random() < 0.012:  # episodic-pivot style event
                gap = float(rng.uniform(0.10, 0.35)) * (1 if rng.random() < 0.75 else -1)
                volume_spike = float(rng.uniform(4, 12))
                if gap > 0:
                    regime, remaining = "impulse", int(rng.integers(10, 30))

            o = prev_close * (1 + gap + rng.normal(0, vol * 0.3))
            c = o * float(np.exp(drift + rng.normal(0, vol)))
            intrabar = abs(rng.normal(0, vol)) * o
            h = max(o, c) + intrabar * float(rng.uniform(0.2, 1.0))
            l = min(o, c) - intrabar * float(rng.uniform(0.2, 1.0))
            l = max(l, 0.05)
            v = base_volume * vol_mult * volume_spike * float(np.exp(rng.normal(0, 0.35)))

            opens[t], highs[t], lows[t], closes[t], volumes[t] = o, h, l, c, v
            prev_close = c

        df = pd.DataFrame(
            {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes.round()},
            index=idx,
        )
        return df[OHLCV]


@dataclass
class CsvUniverse:
    """A directory of generated ``SYMBOL.csv`` files plus the settings that load it.

    ``session_kwargs`` points a ``SessionSettings`` at the directory through the
    ordinary CSV provider; ``overrides`` switches off the live context layer
    (there is no real news, social or options tape for made-up tickers) and
    gives the theme filter groups to rank. ``config_path`` is the same
    configuration as a YAML file for CLI invocations.
    """

    directory: Path
    symbols: list[str]

    @property
    def symbols_arg(self) -> str:
        return ",".join(self.symbols)

    def session_kwargs(self) -> dict:
        return {"data": "csv", "csv_dir": str(self.directory), "symbols": self.symbols_arg}

    def overrides(self, **extra) -> dict:
        ov = {"context.enabled": False, "themes.groups": synthetic_themes(self.symbols)}
        ov.update(extra)
        return ov

    def config_path(self, **extra) -> Path:
        path = self.directory / "config.yaml"
        StrategyConfig().with_overrides(self.overrides(**extra)).save(path)
        return path

    def cli_args(self, symbols: bool = True, **extra) -> list[str]:
        args = ["--data", "csv", "--csv-dir", str(self.directory), "--config", str(self.config_path(**extra))]
        if symbols:
            args += ["--symbols", self.symbols_arg]
        return args


def write_csv_universe(directory: Path, seed: int = 7, bars: int = 750, n: int = 80, benchmark: str = "QQQ") -> CsvUniverse:
    directory.mkdir(parents=True, exist_ok=True)
    symbols = synthetic_universe(n)
    frames = SyntheticProvider(seed=seed, bars=bars).load(symbols + [benchmark])
    for sym, df in frames.items():
        df.to_csv(directory / f"{sym}.csv", index_label="date")
    return CsvUniverse(directory=directory, symbols=symbols)
