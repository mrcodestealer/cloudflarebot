"""Display-timezone helpers.

Cloudflare returns bucket timestamps in UTC (e.g. '2026-07-09T14:20:00Z').
For display we convert to the configured offset (default GMT+8) so times match
what the user sees in the dashboard's local view.
"""
from __future__ import annotations

import datetime
from typing import Optional

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
