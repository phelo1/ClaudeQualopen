"""Exclude impossible long-trade simulations without erasing their audit trail."""
from __future__ import annotations

import math


def geometry_error(row: dict) -> str | None:
    try:
        entry, stop = float(row['entry']), float(row['stop'])
        target = float(row['target']) if row.get('target') is not None else None
    except (KeyError, TypeError, ValueError):
        return 'Missing or non-numeric shadow entry, stop or target'
    if not all(math.isfinite(x) for x in (entry, stop)) or not 0 < stop < entry:
        return 'Long shadow requires finite prices with 0 < stop < entry'
    if target is not None and (not math.isfinite(target) or target <= entry):
        return 'Long shadow target must be finite and above entry'
    return None


def quarantine_invalid(shadows: list[dict], asof: str) -> int:
    count = 0
    for row in shadows:
        if row.get('status') == 'invalid':
            continue
        reason = geometry_error(row)
        if reason is None:
            continue
        # Keep the original result for diagnosis, never as a training label.
        original = {'status': row.get('status')}
        for key in ('r_multiple', 'mfe_r', 'mae_r', 'fill', 'exit_price', 'exit_reason',
                    'exited_on', 'resolved_on', 'hold_days', 'triggered_on', 'post_mortem'):
            if key in row:
                original[key] = row.pop(key)
        row.update(status='invalid', invalid_reason=reason, invalid_on=asof, invalid_result=original)
        count += 1
    return count
