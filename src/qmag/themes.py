"""Theme / segment momentum.

"The strongest stocks in the strongest themes." Each theme is a group of
tickers. We build an equal-weight index per theme from the universe data we
already have (no extra downloads), rank the themes against each other on a
1m/3m momentum composite, and measure each theme's internal breadth. A stock
inherits the best percentile among the themes it belongs to.

Everything is computed as a time series so backtests see, on every day, only
what was knowable that day.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from .config import StrategyConfig

DEFAULT_THEMES_FILE = Path(__file__).resolve().parents[2] / "universe" / "themes.yaml"

THEME_COLUMNS = ["theme", "theme_pct", "theme_gain_1m", "theme_breadth"]


def load_themes(cfg_or_path: StrategyConfig | str | Path | None = None, symbols: list[str] | None = None) -> dict[str, list[str]]:
    """Theme groups from inline config, an explicit file, or the bundled default.

    With ``themes.use_industries`` and a cached ``universe/fundamentals.csv``
    one auto-theme per finviz industry is added underneath the hand-kept
    themes (restricted to ``symbols`` when given, so a small scan does not
    rank against names it never loaded).
    """
    curated: dict[str, list[str]]
    industries: dict[str, list[str]] = {}
    if isinstance(cfg_or_path, StrategyConfig):
        t = cfg_or_path.themes
        if t.groups:
            curated = {str(name): [str(s).upper() for s in members] for name, members in t.groups.items()}
        else:
            curated = _read_themes_file(t.themes_file)
        if t.use_industries:
            from .fundamentals import industry_themes, load_fundamentals

            industries = industry_themes(load_fundamentals(), symbols, t.min_industry_members)
    else:
        curated = _read_themes_file(cfg_or_path)
    return {**industries, **curated}


def _read_themes_file(path: str | Path | None) -> dict[str, list[str]]:
    file = Path(path) if path else DEFAULT_THEMES_FILE
    if not file.exists():
        raise FileNotFoundError(f"Themes file not found: {file}")
    raw = yaml.safe_load(file.read_text()) or {}
    return {str(name): [str(s).upper() for s in members] for name, members in raw.items()}


def _close_matrix(data: dict[str, pd.DataFrame]) -> pd.DataFrame:
    closes = pd.concat({s: df["close"] for s, df in data.items()}, axis=1).sort_index()
    return closes.ffill()


def theme_indices(data: dict[str, pd.DataFrame], themes: dict[str, list[str]], min_members: int = 3) -> pd.DataFrame:
    """Index per theme (columns) from the *median* member's daily return.

    The median is what makes a theme a theme: it only rises when the typical
    member rises. An equal-weight mean let one stock tripling drag a
    six-name industry to "+100 %" while the other five went nowhere.
    """
    closes = _close_matrix(data)
    rets = closes.pct_change()
    out: dict[str, pd.Series] = {}
    for theme, members in themes.items():
        present = [m for m in members if m in rets.columns]
        if len(present) < min_members:
            continue
        member_rets = rets[present]
        # Require at least ``min_members`` live members per day so a lone
        # early-listed stock does not define the theme.
        enough = member_rets.notna().sum(axis=1) >= min_members
        typical = member_rets.median(axis=1).where(enough)
        out[theme] = (1 + typical.fillna(0)).cumprod().where(typical.notna().cummax())
    return pd.DataFrame(out)


def theme_member_counts(data: dict[str, pd.DataFrame], themes: dict[str, list[str]]) -> dict[str, int]:
    """How many of each theme's members have bars in ``data``."""
    return {theme: sum(1 for m in members if m in data) for theme, members in themes.items()}


def theme_breadth(data: dict[str, pd.DataFrame], themes: dict[str, list[str]], ma_length: int = 20) -> pd.DataFrame:
    """Share of each theme's members closing above their ``ma_length`` SMA."""
    closes = _close_matrix(data)
    ma = closes.rolling(ma_length, min_periods=ma_length).mean()
    above = (closes > ma).astype(float).where(closes.notna() & ma.notna())
    out: dict[str, pd.Series] = {}
    for theme, members in themes.items():
        present = [m for m in members if m in closes.columns]
        if len(present) < 2:
            continue
        out[theme] = above[present].mean(axis=1)
    return pd.DataFrame(out)


def theme_table(data: dict[str, pd.DataFrame], cfg: StrategyConfig, themes: dict[str, list[str]] | None = None) -> pd.DataFrame:
    """Per-date, per-theme metrics in long form: gain_1m, gain_3m, composite, pct, breadth, members."""
    themes = themes if themes is not None else load_themes(cfg, list(data))
    idx = theme_indices(data, themes, min_members=max(2, int(getattr(cfg.themes, "min_theme_members", 3))))
    if idx.empty:
        return pd.DataFrame(columns=["theme", "gain_1m", "gain_3m", "composite", "pct", "breadth", "members"])
    counts = theme_member_counts(data, themes)
    gain_1m = idx / idx.shift(21) - 1
    gain_3m = idx / idx.shift(63) - 1
    composite = gain_1m + 0.5 * gain_3m
    pct = composite.rank(axis=1, pct=True)
    breadth = theme_breadth(data, themes, cfg.regime.breadth_ma_length).reindex(idx.index)

    frames = []
    for theme in idx.columns:
        frames.append(
            pd.DataFrame(
                {
                    "theme": theme,
                    "gain_1m": gain_1m[theme],
                    "gain_3m": gain_3m[theme],
                    "composite": composite[theme],
                    "pct": pct[theme],
                    "breadth": breadth[theme] if theme in breadth else np.nan,
                    "members": counts.get(theme, 0),
                }
            )
        )
    return pd.concat(frames)


def attach_theme_columns(data: dict[str, pd.DataFrame], cfg: StrategyConfig) -> dict[str, pd.DataFrame]:
    """Add ``theme``, ``theme_pct``, ``theme_gain_1m``, ``theme_breadth`` to every frame.

    A stock in several themes takes, on each day, the theme with the highest
    percentile. Stocks in no theme get NaNs (they pass the filter unless
    ``require_theme`` is set).
    """
    themes = load_themes(cfg, list(data))
    table = theme_table(data, cfg, themes)
    membership: dict[str, list[str]] = {}
    for theme, members in themes.items():
        for m in members:
            membership.setdefault(m, []).append(theme)

    by_theme = {t: g.drop(columns="theme") for t, g in table.groupby("theme")} if not table.empty else {}
    out: dict[str, pd.DataFrame] = {}
    for sym, df in data.items():
        df = df.copy()
        for col in THEME_COLUMNS:
            df[col] = np.nan
        df["theme"] = df["theme"].astype(object)
        candidates = [t for t in membership.get(sym, []) if t in by_theme]
        if candidates:
            pcts = pd.concat({t: by_theme[t]["pct"].reindex(df.index) for t in candidates}, axis=1)
            has = pcts.notna().any(axis=1)
            best = pcts.fillna(-1.0).idxmax(axis=1)
            df.loc[has, "theme"] = best[has]
            df.loc[has, "theme_pct"] = pcts.max(axis=1)[has]
            for t in candidates:
                mask = has & (best == t)
                df.loc[mask, "theme_gain_1m"] = by_theme[t]["gain_1m"].astype(float).reindex(df.index)[mask]
                df.loc[mask, "theme_breadth"] = by_theme[t]["breadth"].astype(float).reindex(df.index)[mask]
        out[sym] = df
    return out


def theme_ok(row: pd.Series, cfg: StrategyConfig, setup: str | None = None) -> bool:
    t = cfg.themes
    if not t.enabled or (setup is not None and setup not in t.apply_to):
        return True
    pct = row.get("theme_pct", np.nan)
    if pct is None or (isinstance(pct, float) and np.isnan(pct)):
        return not t.require_theme
    if pct < t.min_theme_percentile:
        return False
    breadth = row.get("theme_breadth", np.nan)
    if breadth is not None and not np.isnan(breadth) and breadth < t.min_theme_breadth:
        return False
    return True


def latest_theme_leaderboard(data: dict[str, pd.DataFrame], cfg: StrategyConfig) -> pd.DataFrame:
    """Themes ranked as of the last date, for the scan report."""
    table = theme_table(data, cfg)
    if table.empty:
        return table
    last = table.index.max()
    board = table[table.index == last].sort_values("composite", ascending=False)
    return board.reset_index(drop=True)
