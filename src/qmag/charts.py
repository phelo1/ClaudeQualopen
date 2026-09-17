"""Annotated setup charts.

Every idea the trader plans (or a symbol you ask about) can be rendered as a
PNG: candles, volume, the 10/20/50-day SMAs, the flag that formed the setup,
and the trade levels - pivot, entry, stop and the partial-profit target -
each labelled with its price and R distance. The same picture is used by the
CLI, the daemon's nightly report and the dashboard.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import mplfinance as mpf  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

from .setups import Signal  # noqa: E402

COLOURS = {
    "entry": "#22c55e",
    "stop": "#ef4444",
    "target": "#3b82f6",
    "pivot": "#f59e0b",
    "flag": "#a78bfa",
    "sma_10": "#f472b6",
    "sma_20": "#60a5fa",
    "sma_50": "#facc15",
}

STYLE = mpf.make_mpf_style(
    base_mpf_style="nightclouds",
    marketcolors=mpf.make_marketcolors(up="#22c55e", down="#ef4444", edge="inherit", wick="inherit", volume={"up": "#14532d", "down": "#7f1d1d"}),
    gridstyle=":",
    gridcolor="#334155",
    facecolor="#0f172a",
    figcolor="#0f172a",
    rc={"axes.labelcolor": "#e2e8f0", "xtick.color": "#cbd5e1", "ytick.color": "#cbd5e1", "font.size": 9},
)


@dataclass
class ChartLevels:
    entry: float
    stop: float | None  # None: no stop is drawn (e.g. ADR unknown) rather than a made-up one
    target: float | None = None
    pivot: float | None = None
    shares: int | None = None
    theme: str | None = None
    setup: str = ""
    note: str = ""


def _flag_window(sig: Signal, df: pd.DataFrame) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    days = sig.details.get("flag_days")
    if not days:
        return None
    if sig.date in df.index:
        end_pos = df.index.get_loc(sig.date)
        # A triggered breakout's flag ends the bar before the trigger; a
        # watchlist idea's flag ends on the last bar.
        if sig.details.get("distance_to_pivot_pct") is None and end_pos > 0:
            end_pos -= 1
    else:
        end_pos = len(df) - 1
    start_pos = max(0, end_pos - int(days) + 1)
    return df.index[start_pos], df.index[end_pos]


def render_chart(
    df: pd.DataFrame,
    symbol: str,
    levels: ChartLevels,
    out_path: str | Path,
    sig: Signal | None = None,
    bars: int = 130,
    title_extra: str = "",
) -> Path:
    """Draw the setup and trade plan for ``symbol`` and save it to ``out_path``."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    view = df.iloc[-bars:].copy()
    view.index = pd.DatetimeIndex(view.index)
    ohlc = view[["open", "high", "low", "close", "volume"]].rename(columns=str.capitalize)

    addplots = []
    for col, colour in (("sma_10", COLOURS["sma_10"]), ("sma_20", COLOURS["sma_20"]), ("sma_50", COLOURS["sma_50"])):
        if col in view and view[col].notna().any():
            addplots.append(mpf.make_addplot(view[col], color=colour, width=1.0))

    risk = levels.entry - levels.stop if levels.stop is not None else 0.0
    hlines = [levels.entry]
    colors = [COLOURS["entry"]]
    styles = ["-"]
    if levels.stop is not None:
        hlines.append(levels.stop)
        colors.append(COLOURS["stop"])
        styles.append("-")
    if levels.target is not None:
        hlines.append(levels.target)
        colors.append(COLOURS["target"])
        styles.append("--")
    if levels.pivot is not None and abs(levels.pivot - levels.entry) > 1e-9:
        hlines.append(levels.pivot)
        colors.append(COLOURS["pivot"])
        styles.append(":")

    fig, axes = mpf.plot(
        ohlc,
        type="candle",
        style=STYLE,
        volume=True,
        **({"addplot": addplots} if addplots else {}),
        hlines=dict(hlines=hlines, colors=colors, linestyle=styles, linewidths=1.2, alpha=0.9),
        returnfig=True,
        figsize=(13, 7.5),
        panel_ratios=(4, 1),
        xrotation=0,
        datetime_format="%b %d",
        scale_padding={"left": 0.4, "right": 2.2, "top": 0.6, "bottom": 0.6},
    )
    ax = axes[0]
    fig.subplots_adjust(left=0.05, right=0.86, top=0.93, bottom=0.08, hspace=0.05)

    # Keep every level in view even when the target sits well above recent highs.
    lo_lim, hi_lim = ax.get_ylim()
    wanted = [levels.entry] + ([levels.stop] if levels.stop is not None else []) + ([levels.target] if levels.target is not None else [])
    span = max(hi_lim, *wanted) - min(lo_lim, *wanted)
    ax.set_ylim(min(lo_lim, min(wanted) - 0.02 * span), max(hi_lim, max(wanted) + 0.04 * span))

    # Flag box behind the consolidation that produced the pivot.
    if sig is not None:
        window = _flag_window(sig, view)
        if window is not None and "flag_high" in sig.details:
            start, end = window
            x0 = view.index.get_loc(start)
            x1 = view.index.get_loc(end)
            lo, hi = float(sig.details["flag_low"]), float(sig.details["flag_high"])
            ax.add_patch(Rectangle((x0 - 0.5, lo), x1 - x0 + 1, hi - lo, facecolor=COLOURS["flag"], alpha=0.18, edgecolor=COLOURS["flag"], linewidth=1.0, linestyle="--"))
            ax.annotate(f"flag {int(sig.details['flag_days'])}d, depth {sig.details['depth'] * 100:.0f}%", (x0, lo), xytext=(0, -12), textcoords="offset points", color=COLOURS["flag"], fontsize=8)
        if sig.setup == "episodic_pivot" and sig.date in view.index:
            xg = view.index.get_loc(sig.date)
            ax.annotate(
                f"EP gap +{sig.details.get('gap_pct', 0):.0f}%  rvol {sig.details.get('rvol', 0):.1f}x",
                (xg, float(view.loc[sig.date, 'high'])),
                xytext=(-80, 18),
                textcoords="offset points",
                color=COLOURS["pivot"],
                fontsize=8,
                arrowprops=dict(arrowstyle="->", color=COLOURS["pivot"]),
            )

    # Labelled levels along the right edge.
    xr = len(view) - 1 + 0.6
    def label(y: float, text: str, colour: str) -> None:
        ax.annotate(text, (xr, y), xytext=(4, 0), textcoords="offset points", color=colour, fontsize=8, va="center", ha="left", annotation_clip=False)

    pivot_close = levels.pivot is not None and abs(levels.pivot - levels.entry) / levels.entry < 0.01
    entry_text = f"entry {levels.entry:.2f}" + (f" (pivot {levels.pivot:.2f})" if pivot_close and abs(levels.pivot - levels.entry) > 1e-9 else "")
    label(levels.entry, entry_text, COLOURS["entry"])
    if levels.stop is not None:
        label(levels.stop, f"stop {levels.stop:.2f} (-{(1 - levels.stop / levels.entry) * 100:.1f}%, 1R)", COLOURS["stop"])
    if levels.target is not None and risk > 0:
        label(levels.target, f"target {levels.target:.2f} (+{(levels.target - levels.entry) / risk:.1f}R)", COLOURS["target"])
    if levels.pivot is not None and not pivot_close:
        label(levels.pivot, f"pivot {levels.pivot:.2f}", COLOURS["pivot"])

    parts = [symbol, levels.setup.replace("_", " ")] if levels.setup else [symbol]
    if levels.theme:
        parts.append(f"theme: {levels.theme}")
    if levels.shares:
        parts.append(f"{levels.shares} sh")
    if title_extra:
        parts.append(title_extra)
    fig.suptitle("  |  ".join(parts), color="#e2e8f0", fontsize=12, x=0.02, ha="left")
    if levels.note:
        fig.text(0.02, 0.005, levels.note, color="#94a3b8", fontsize=8)
    ax.set_ylabel("")
    fig.savefig(out_path, dpi=110, facecolor=fig.get_facecolor())
    plt.close(fig)
    return out_path


def chart_signal(df: pd.DataFrame, sig: Signal, out_dir: str | Path, target: float | None = None, shares: int | None = None, note: str = "") -> Path:
    theme = sig.details.get("theme")
    levels = ChartLevels(
        entry=sig.entry,
        stop=sig.stop,
        target=target,
        pivot=sig.pivot,
        shares=shares,
        theme=theme if isinstance(theme, str) else None,
        setup=sig.setup,
        note=note,
    )
    name = f"{sig.symbol}_{pd.Timestamp(sig.date).date().isoformat()}.png"
    return render_chart(df, sig.symbol, levels, Path(out_dir) / name, sig=sig)
