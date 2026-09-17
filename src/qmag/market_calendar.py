"""NYSE trading calendar (holidays, early closes) in America/New_York.

Shared by the daemon scheduler, the price-freshness check and the volume
pace projection. Rules follow the exchange's published holiday schedule:
New Year's Day, Martin Luther King Jr. Day, Presidents' Day, Good Friday,
Memorial Day, Juneteenth, Independence Day, Labor Day, Thanksgiving and
Christmas, with Saturday holidays observed on Friday and Sunday holidays
on Monday (except New Year's Day falling on a Saturday, which is not
observed). Early closes (13:00): the day after Thanksgiving and, when they
are weekdays, July 3 and December 24.
"""

from __future__ import annotations

from datetime import date, time, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


def _easter(year: int) -> date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def nyse_holidays(year: int) -> set[date]:
    easter = _easter(year)
    days = {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),  # MLK
        _nth_weekday(year, 2, 0, 3),  # Presidents
        easter - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),  # Memorial
        _observed(date(year, 6, 19)),  # Juneteenth
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),  # Labor
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed(date(year, 12, 25)),
    }
    # New Year's Day falling on a Saturday is not observed on the prior Friday by NYSE.
    if date(year, 1, 1).weekday() == 5:
        days.discard(date(year - 1, 12, 31))
    return days


def nyse_early_closes(year: int) -> set[date]:
    out = {_nth_weekday(year, 11, 3, 4) + timedelta(days=1)}  # day after Thanksgiving
    for d in (date(year, 7, 3), date(year, 12, 24)):
        if d.weekday() < 5 and d not in nyse_holidays(year):
            out.add(d)
    return out


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in nyse_holidays(d.year)


def market_close(d: date) -> time:
    return time(13, 0) if d in nyse_early_closes(d.year) else time(16, 0)


def parse_hhmm(value: str) -> time:
    """'09:35' -> time(9, 35); raises ValueError on anything else."""
    hh, mm = str(value).strip().split(":")
    return time(int(hh), int(mm))
