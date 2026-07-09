"""Adaptive spike detection with persistent de-duplication.

A bucket (time-slice of the traffic series) is a *spike* when its request
count exceeds ``mean + std_multiplier * std`` of the preceding baseline
window AND clears an absolute floor.  Detected spikes are de-duplicated by
their bucket timestamp and persisted to disk, so:

  * the same spike is never alerted twice (across polls or restarts), and
  * a brand-new spike is always alerted, even right after a restart.

On the very first run (no state yet) we "prime" on the history already on the
chart: every existing spike bucket is recorded as seen *without* alerting,
so we don't spam the group with hours-old peaks -- except the most recent
bucket, which stays eligible so an attack in progress at startup still fires.
"""
from __future__ import annotations

import json
import os
import statistics
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Sequence, Tuple


@dataclass
class Spike:
    ts: str                 # bucket timestamp (ISO 8601, sortable)
    count: float            # request count in the bucket
    baseline_mean: float
    baseline_std: float
    threshold: float
    ratio: float            # count / max(baseline_mean, 1) -- "how many x baseline"
    recent: List[Tuple[str, float]] = field(default_factory=list)  # tail for context

    def as_dict(self) -> dict:
        return asdict(self)


class SpikeDetector:
    def __init__(
        self,
        std_multiplier: float = 4.0,
        baseline_window: int = 20,
        min_floor: float = 1000.0,
        warmup: int = 6,
        state_path: str = "state/spikes.json",
        max_remembered: int = 500,
    ) -> None:
        self.std_multiplier = std_multiplier
        self.baseline_window = baseline_window
        self.min_floor = min_floor
        self.warmup = warmup
        self.state_path = state_path
        self.max_remembered = max_remembered

        self._alerted: set[str] = set()
        self._initialized: bool = False
        self._load()

    # ------------------------------------------------------------------ state
    def _load(self) -> None:
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._alerted = set(data.get("alerted", []))
            self._initialized = bool(data.get("initialized", False))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self._alerted = set()
            self._initialized = False

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        # Keep the set bounded: retain the most recent timestamps only.
        if len(self._alerted) > self.max_remembered:
            self._alerted = set(sorted(self._alerted)[-self.max_remembered:])
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"alerted": sorted(self._alerted), "initialized": self._initialized}, fh, indent=2)
        os.replace(tmp, self.state_path)

    # -------------------------------------------------------------- detection
    def _baseline_stats(self, window: List[Tuple[str, float]]) -> Tuple[float, float, int]:
        """Robust mean/std of a baseline window.

        Prior spikes must not poison the baseline (otherwise one attack blinds
        the detector to the next).  We therefore (a) drop any bucket already
        flagged as a spike, and (b) trim the top ~10% of the remaining values
        before computing mean + std.  Returns (mean, std, n_used).
        """
        base = [c for t, c in window if t not in self._alerted]
        if len(base) < self.warmup:
            return 0.0, 0.0, len(base)
        base_sorted = sorted(base)
        k = max(1, len(base_sorted) // 10)
        trimmed = base_sorted[:-k] if len(base_sorted) > k else base_sorted
        if not trimmed:
            trimmed = base_sorted
        mean = statistics.fmean(trimmed)
        std = statistics.pstdev(trimmed) if len(trimmed) > 1 else 0.0
        return mean, std, len(base)

    def _all_spikes(self, series: Sequence[Tuple[str, float]]) -> List[Spike]:
        """Return every bucket in ``series`` that qualifies as a spike."""
        series = sorted(series, key=lambda p: p[0])
        spikes: List[Spike] = []
        for i, (ts, count) in enumerate(series):
            if i < self.warmup:
                continue
            window = list(series[max(0, i - self.baseline_window):i])
            mean, std, n_used = self._baseline_stats(window)
            if n_used < self.warmup:
                continue
            threshold = mean + self.std_multiplier * std
            if count >= self.min_floor and count > threshold and count > mean:
                spikes.append(
                    Spike(
                        ts=ts,
                        count=count,
                        baseline_mean=round(mean, 2),
                        baseline_std=round(std, 2),
                        threshold=round(threshold, 2),
                        ratio=round(count / max(mean, 1.0), 2),
                        recent=[(t, c) for t, c in series[max(0, i - 12):i + 1]],
                    )
                )
        return spikes

    def find_new_spikes(self, series: Sequence[Tuple[str, float]]) -> List[Spike]:
        """Return spikes not previously alerted, and record them as alerted.

        ``series`` is a list of ``(iso_timestamp, count)`` tuples for the
        current window (order does not matter; it is sorted internally).
        """
        if not series:
            return []

        spikes = self._all_spikes(series)
        latest_ts = max(ts for ts, _ in series)

        if not self._initialized:
            # Prime: swallow historical spikes, but let the newest bucket fire.
            new = [s for s in spikes if s.ts >= latest_ts]
            for s in spikes:
                self._alerted.add(s.ts)
            self._initialized = True
            self._save()
            return new

        new = [s for s in spikes if s.ts not in self._alerted]
        if new:
            for s in new:
                self._alerted.add(s.ts)
            self._save()
        return new
