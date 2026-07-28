"""Display-timezone helpers.

Cloudflare returns bucket timestamps in UTC (e.g. '2026-07-09T14:20:00Z').
For display we convert to the configured offset (default GMT+8) so times match
what the user sees in the dashboard's local view.
"""
from __future__ import annotations

import datetime
from typing import List, Optional, Sequence, Tuple

from config import config


def _tz() -> datetime.timezone:
    return datetime.timezone(datetime.timedelta(hours=config.display_tz_offset))


def parse_utc(ts: str) -> Optional[datetime.datetime]:
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.datetime.strptime(ts, fmt).replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
    return None


def to_local_dt(ts: str) -> Optional[datetime.datetime]:
    dt = parse_utc(ts)
    return dt.astimezone(_tz()) if dt else None


def fmt(ts: str, date: bool = True, label: bool = True) -> str:
    """Format a UTC iso timestamp in the display timezone, e.g. '2026-07-09 22:20 GMT+8'."""
    dt = to_local_dt(ts)
    if dt is None:
        return ts
    s = dt.strftime("%Y-%m-%d %H:%M" if date else "%H:%M")
    return f"{s} {config.display_tz_label}" if label else s


def window_label(minutes: float) -> str:
    """Human label for a time window: 30 -> '30 min', 360 -> '6h', 90 -> '90 min'."""
    m = int(round(minutes))
    if m >= 60 and m % 60 == 0:
        return f"{m // 60}h"
    return f"{m} min"


def tail_minutes(
    series: Sequence[Tuple[str, float]], minutes: float
) -> List[Tuple[str, float]]:
    """Return the tail of a [(iso_ts, count)] series covering the last `minutes`.

    Sliced by timestamp relative to the latest bucket (not by count), so gaps in
    the series don't widen the window. Used to zoom the chart/peak to a recent
    window while the detector keeps working off the full history it's fed.
    """
    if not series or minutes <= 0:
        return list(series)
    latest = None
    for ts, _ in reversed(series):
        latest = parse_utc(ts)
        if latest is not None:
            break
    if latest is None:
        return list(series)
    cutoff = latest - datetime.timedelta(minutes=minutes)
    out = [(ts, c) for ts, c in series if (parse_utc(ts) or latest) >= cutoff]
    return out or list(series[-1:])
