"""Walk-forward parameter search.

"Optimising" a discretionary swing method on a single history is the fastest
way to build something that only worked in the past. This module therefore:

* evaluates every parameter combination on an in-sample window,
* picks the best by a robust objective (default: Calmar with a minimum trade
  count, not raw return),
* then reports how that pick did on the *following* out-of-sample window,
* and rolls the windows forward so you get several unbiased OOS readings.

If the OOS results do not roughly resemble the IS results, the "optimised"
parameters are noise. That comparison is the deliverable, not the best row.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd

from .backtest import BacktestResult, run_backtest
from .config import StrategyConfig

log = logging.getLogger(__name__)

Objective = Callable[[dict[str, float]], float]

DEFAULT_GRID: dict[str, list[Any]] = {
    "management.trail_ma": [10, 20],
    "management.stop_adr_mult": [0.75, 1.0, 1.5],
    # 0.0 disables the hard theme gate while keeping the ranking bonus.
    "themes.min_theme_percentile": [0.0, 0.3, 0.5],
    # 0.0 disables the breadth gate (benchmark trend gate stays on).
    "regime.min_breadth": [0.0, 0.4],
}


def objective_calmar(metrics: dict[str, float], min_trades: int = 20) -> float:
    if not metrics or metrics.get("trades", 0) < min_trades:
        return float("-inf")
    return metrics["calmar"] if metrics["max_drawdown"] < 0 else metrics["total_return"]


def objective_expectancy(metrics: dict[str, float], min_trades: int = 20) -> float:
    if not metrics or metrics.get("trades", 0) < min_trades:
        return float("-inf")
    return metrics["expectancy_r"]


OBJECTIVES: dict[str, Objective] = {
    "calmar": objective_calmar,
    "expectancy": objective_expectancy,
    "sharpe": lambda m: m.get("sharpe", float("-inf")) if m.get("trades", 0) >= 20 else float("-inf"),
}


@dataclass
class FoldResult:
    fold: int
    is_start: pd.Timestamp
    is_end: pd.Timestamp
    oos_start: pd.Timestamp
    oos_end: pd.Timestamp
    best_params: dict[str, Any]
    is_metrics: dict[str, float]
    oos_metrics: dict[str, float]
    baseline_oos_metrics: dict[str, float]


@dataclass
class WalkForwardResult:
    folds: list[FoldResult]
    grid_table: pd.DataFrame  # every combination x fold, in-sample objective
    stitched_oos_equity: pd.Series

    def summary(self) -> pd.DataFrame:
        rows = []
        for f in self.folds:
            rows.append(
                {
                    "fold": f.fold,
                    "is": f"{f.is_start.date()}..{f.is_end.date()}",
                    "oos": f"{f.oos_start.date()}..{f.oos_end.date()}",
                    **{f"p:{k.split('.')[-1]}": v for k, v in f.best_params.items()},
                    "is_return": f.is_metrics.get("total_return", 0),
                    "oos_return": f.oos_metrics.get("total_return", 0),
                    "base_oos_return": f.baseline_oos_metrics.get("total_return", 0),
                    "is_calmar": f.is_metrics.get("calmar", 0),
                    "oos_calmar": f.oos_metrics.get("calmar", 0),
                    "oos_trades": f.oos_metrics.get("trades", 0),
                }
            )
        return pd.DataFrame(rows)


def expand_grid(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    keys = list(grid)
    return [dict(zip(keys, combo)) for combo in itertools.product(*(grid[k] for k in keys))]


def walk_forward(
    data: dict[str, pd.DataFrame],
    base_cfg: StrategyConfig,
    grid: dict[str, list[Any]] | None = None,
    n_folds: int = 3,
    is_fraction: float = 0.6,
    objective: str | Objective = "calmar",
    progress: Callable[[str], None] | None = None,
) -> WalkForwardResult:
    if n_folds < 1 or not 0 < is_fraction < 1:
        raise ValueError("n_folds must be positive and is_fraction must be between zero and one")
    grid = DEFAULT_GRID if grid is None else grid
    obj: Objective = OBJECTIVES[objective] if isinstance(objective, str) else objective
    combos = expand_grid(grid)
    if not combos:
        raise ValueError("Every grid parameter needs at least one candidate value")

    all_dates = pd.DatetimeIndex(sorted(set().union(*[set(df.index) for df in data.values()])))
    warm = base_cfg.warmup_bars
    tradeable = all_dates[warm:]
    if len(tradeable) < 200:
        raise ValueError("Need at least ~200 tradeable bars after warm-up for a walk-forward test")

    # Anchored-free rolling folds: each fold spans an equal slice of the
    # tradeable history, split IS / OOS by ``is_fraction``.
    fold_len = len(tradeable) // n_folds
    folds: list[FoldResult] = []
    grid_rows: list[dict[str, Any]] = []
    oos_pieces: list[pd.Series] = []

    for k in range(n_folds):
        seg = tradeable[k * fold_len : (k + 1) * fold_len if k < n_folds - 1 else len(tradeable)]
        split = int(len(seg) * is_fraction)
        is_dates, oos_dates = seg[:split], seg[split:]
        if len(is_dates) < 40 or len(oos_dates) < 20:
            continue

        best_score, best_params, best_is = float("-inf"), {}, {}
        for i, params in enumerate(combos):
            cfg = base_cfg.with_overrides(params)
            res = run_backtest(data, cfg, start=str(is_dates[0].date()), end=str(is_dates[-1].date()))
            m = res.metrics()
            score = obj(m)
            grid_rows.append({"fold": k, **params, "score": score, **{f"is_{kk}": v for kk, v in m.items()}})
            if progress:
                progress(f"fold {k + 1}/{n_folds} combo {i + 1}/{len(combos)} score={score:.3f}")
            if score > best_score:
                best_score, best_params, best_is = score, params, m

        if best_score == float("-inf"):
            log.warning("Fold %d: no parameter set met the minimum-trade threshold", k)
            best_params = {}  # retain the declared baseline; no candidate qualified
            best_is = {}

        oos_cfg = base_cfg.with_overrides(best_params)
        oos_res: BacktestResult = run_backtest(data, oos_cfg, start=str(oos_dates[0].date()), end=str(oos_dates[-1].date()))
        base_res = run_backtest(data, base_cfg, start=str(oos_dates[0].date()), end=str(oos_dates[-1].date()))
        folds.append(
            FoldResult(
                fold=k,
                is_start=is_dates[0],
                is_end=is_dates[-1],
                oos_start=oos_dates[0],
                oos_end=oos_dates[-1],
                best_params=best_params,
                is_metrics=best_is,
                oos_metrics=oos_res.metrics(),
                baseline_oos_metrics=base_res.metrics(),
            )
        )
        oos_pieces.append(oos_res.equity / base_cfg.risk.starting_equity)

    stitched = pd.Series(dtype=float)
    if oos_pieces:
        level = 1.0
        parts = []
        for piece in oos_pieces:
            parts.append(piece * level)
            level = float(parts[-1].iloc[-1])
        stitched = pd.concat(parts)

    return WalkForwardResult(folds=folds, grid_table=pd.DataFrame(grid_rows), stitched_oos_equity=stitched)
