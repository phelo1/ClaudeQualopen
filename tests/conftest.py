import numpy as np
import pandas as pd
import pytest

from qmag.config import StrategyConfig
from tests.synthetic import CsvUniverse, SyntheticProvider, write_csv_universe


@pytest.fixture(autouse=True)
def _no_ibkr_auto(monkeypatch):
    """``--data auto`` must not pick IBKR just because a gateway happens to be
    running on the machine that runs the tests; tests that want the IBKR
    path set ``IBKR_PREFER_DATA=yes`` themselves."""
    monkeypatch.setenv("IBKR_PREFER_DATA", "no")
    # SettingsStore exports saved keys into os.environ. Each test starts with
    # no paid-feed credentials; otherwise worker ordering can arm a required
    # context gate in an unrelated engine replay. Tests configure their own.
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY", raising=False)


@pytest.fixture
def cfg() -> StrategyConfig:
    return StrategyConfig()


@pytest.fixture
def synthetic_data() -> dict[str, pd.DataFrame]:
    """Generated frames for engine-level tests (backtester, trader replay). Test-only; see tests/synthetic.py."""
    syms = [f"T{i:02d}" for i in range(25)] + ["QQQ"]
    return SyntheticProvider(seed=11, bars=600).load(syms)


@pytest.fixture
def csv_universe(tmp_path) -> CsvUniverse:
    """Generated ``SYMBOL.csv`` files loaded through the product's ordinary CSV provider."""
    return write_csv_universe(tmp_path / "csv")


def make_breakout_frame(n_pre: int = 200, flag_days: int = 20) -> pd.DataFrame:
    """Hand-built leader: quiet base -> sharp impulse -> tight flag -> breakout on volume."""
    rng = np.random.default_rng(0)
    idx = pd.bdate_range("2024-01-02", periods=n_pre + 60 + flag_days + 1)
    closes = []
    px = 20.0
    for _ in range(n_pre):  # flat base
        px *= 1 + rng.normal(0, 0.004)
        closes.append(px)
    for _ in range(60):  # impulse: roughly +150 %
        px *= 1 + 0.016 + rng.normal(0, 0.012)
        closes.append(px)
    top = px
    for k in range(flag_days):  # flag: ~6 % pullback, then grinds back up under the pivot
        if k < 5:
            px = top * (1 - 0.06 * (k + 1) / 5)
        else:
            px = top * 0.94 * (1 + 0.03 * (k - 4) / (flag_days - 5))
        px *= 1 + rng.normal(0, 0.002)
        closes.append(px)
    pivot = top
    closes.append(pivot * 1.04)  # breakout day close

    close = np.array(closes)
    open_ = np.r_[close[0], close[:-1]] * (1 + rng.normal(0, 0.002, len(close)))
    rng_pct = np.full(len(close), 0.06)
    rng_pct[n_pre + 60 : n_pre + 60 + flag_days // 2] = 0.05  # range contracts through the flag
    rng_pct[n_pre + 60 + flag_days // 2 :] = 0.035
    high = np.maximum(open_, close) * (1 + rng_pct / 2)
    low = np.minimum(open_, close) * (1 - rng_pct / 2)
    # Keep the flag highs below the pivot so the impulse top is the pivot.
    high[n_pre + 60 : -1] = np.minimum(high[n_pre + 60 : -1], pivot * 0.995)
    volume = np.full(len(close), 2_000_000.0)
    volume[n_pre + 60 : -1] *= 0.6
    volume[-1] *= 3.0
    open_[-1] = pivot * 0.995
    high[-1] = pivot * 1.05
    low[-1] = pivot * 0.985
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)


def make_ep_frame(n: int = 220, gap: float = 0.25) -> pd.DataFrame:
    """Neglected stock that gaps up 25 % on 8x volume on the last bar."""
    rng = np.random.default_rng(1)
    idx = pd.bdate_range("2024-01-02", periods=n)
    close = 30 * np.cumprod(1 + rng.normal(0, 0.01, n))
    open_ = np.r_[close[0], close[:-1]] * (1 + rng.normal(0, 0.002, n))
    high = np.maximum(open_, close) * 1.012
    low = np.minimum(open_, close) * 0.988
    volume = np.full(n, 1_500_000.0)
    prev = close[-2]
    open_[-1] = prev * (1 + gap)
    close[-1] = open_[-1] * 1.03
    high[-1] = close[-1] * 1.01
    low[-1] = open_[-1] * 0.99
    volume[-1] = 12_000_000.0
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)
