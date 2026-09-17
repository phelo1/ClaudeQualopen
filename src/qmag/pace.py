"""Time-of-day volume pace for intraday (partial) daily bars.

The breakout rule asks for volume >= 1.2x the 20-day average *for the day*.
At 10:30 a stock has traded perhaps a quarter of its usual day, so the raw
partial-bar volume ratio says "no volume" for almost every name until late
afternoon - and a fast mover is gone by then. The focused intraday cycle
therefore judges volume on **pace**: the volume so far divided by the share
of a normal session's volume that is usually done by this time of day.

The cumulative curve below is the well-known U-shaped US equity profile
(heavy open, quiet lunch, heavy close), scaled to the session length on
early-close days. It is a projection and is labelled as one everywhere it
is used: the raw partial volume is kept alongside, and before
``min_fraction`` of the session has elapsed the projection is too noisy to
trust, so the raw count is left untouched (buy-stop brackets parked
overnight still fill at the broker; only the *volume-confirmed* market
entry waits).
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

import pandas as pd

from .market_calendar import NY, is_trading_day, market_close

OPEN = time(9, 30)

# minutes since the open (of a 390-minute session) -> share of the day's volume done
_CURVE: tuple[tuple[int, float], ...] = (
    (0, 0.0), (5, 0.035), (15, 0.09), (30, 0.15), (45, 0.20), (60, 0.24), (90, 0.31), (120, 0.37),
    (150, 0.42), (180, 0.47), (210, 0.52), (240, 0.57), (270, 0.62), (300, 0.68), (330, 0.75),
    (360, 0.85), (375, 0.92), (390, 1.0),
)


def session_fraction(now: datetime) -> float | None:
    """Share of a normal day's volume usually traded by ``now`` (America/New_York).

    None outside a trading session (pre-market, after the close, weekends,
    holidays), so callers never project on a day that is not trading.
    """
    ny = now.astimezone(NY) if now.tzinfo else now.replace(tzinfo=NY)
    d = ny.date()
    if not is_trading_day(d):
        return None
    close = market_close(d)
    if ny.time() < OPEN:
        return None
    if ny.time() >= close:
        return 1.0
    session_minutes = (datetime.combine(d, close) - datetime.combine(d, OPEN)).total_seconds() / 60
    elapsed = (datetime.combine(d, ny.time().replace(tzinfo=None)) - datetime.combine(d, OPEN)).total_seconds() / 60
    x = elapsed * 390.0 / session_minutes  # early closes: same shape, compressed
    for (x0, y0), (x1, y1) in zip(_CURVE, _CURVE[1:]):
        if x <= x1:
            return round(y0 + (y1 - y0) * (x - x0) / (x1 - x0), 4)
    return 1.0


def project_volume(frames: dict[str, pd.DataFrame], now: datetime, min_fraction: float = 0.08) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    """Scale today's partial bar volume to full-day pace.

    Only bars dated today (NY) are touched, only during the session and only
    once ``min_fraction`` of it has elapsed. Returns new frames and a note::

        {"applied": bool, "fraction": 0.31, "multiplier": 3.2, "symbols": 41, "reason": "..."}
    """
    fraction = session_fraction(now)
    ny = now.astimezone(NY) if now.tzinfo else now.replace(tzinfo=NY)
    today: date = ny.date()
    note: dict[str, Any] = {"applied": False, "fraction": fraction, "multiplier": None, "symbols": 0, "asof": str(today)}
    if fraction is None:
        note["reason"] = "outside the trading session: bars are complete, no projection"
        return frames, note
    if fraction >= 1.0:
        note["reason"] = "session over: bars are complete, no projection"
        return frames, note
    if fraction < min_fraction:
        note["reason"] = f"only {fraction:.0%} of the session elapsed (< {min_fraction:.0%}): projection too noisy, raw partial volume used"
        return frames, note
    mult = 1.0 / fraction
    out: dict[str, pd.DataFrame] = {}
    touched = 0
    for sym, df in frames.items():
        if len(df) and pd.Timestamp(df.index[-1]).date() == today:
            df = df.copy()
            df.iloc[-1, df.columns.get_loc("volume")] = float(df["volume"].iloc[-1]) * mult
            touched += 1
        out[sym] = df
    note.update(applied=touched > 0, multiplier=round(mult, 2), symbols=touched)
    note["reason"] = (
        f"{fraction:.0%} of the session elapsed: today's volume on {touched} symbols projected x{mult:.2f} to full-day pace"
        if touched else "no symbol has a bar for today yet"
    )
    return out, note
