"""Render the traffic timeseries to a PNG for the /mo command.

Replaces the (bot-blocked) dashboard screenshot with a chart drawn from the same
data. Degrades gracefully: if matplotlib isn't installed, returns None and the
caller sends a text-only reply.
"""
from __future__ import annotations

import io
import logging
from typing import List, Optional, Tuple

log = logging.getLogger("chart")

try:
    import matplotlib
    matplotlib.use("Agg")  # headless backend
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from datetime import datetime
    _HAVE_MPL = True
except Exception:  # pragma: no cover
    _HAVE_MPL = False


def _parse(ts: str):
    from datetime import datetime
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    return None


def render_series_png(
    series: List[Tuple[str, float]],
    title: str,
    highlight_ts: Optional[str] = None,
) -> Optional[bytes]:
    """Render (iso_ts, count) points to PNG bytes. None if matplotlib missing."""
    if not _HAVE_MPL or not series:
        return None
    xs, ys = [], []
    for ts, c in series:
        dt = _parse(ts)
        if dt is not None:
            xs.append(dt)
            ys.append(c)
    if not xs:
        return None

    fig, ax = plt.subplots(figsize=(10, 4), dpi=110)
    ax.plot(xs, ys, color="#f6821f", linewidth=1.8)
    ax.fill_between(xs, ys, color="#f6821f", alpha=0.15)

    if highlight_ts:
        hdt = _parse(highlight_ts)
        if hdt is not None:
            ax.axvline(hdt, color="#d64545", linestyle="--", linewidth=1.2)

    ax.set_title(title, fontsize=12, fontweight="bold")
    ax.set_ylabel("Requests / 5 min")
    ax.grid(True, alpha=0.25)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    fig.autofmt_xdate()
    ax.margins(x=0)

    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()
