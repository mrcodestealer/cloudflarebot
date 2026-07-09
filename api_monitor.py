"""API-based monitor: poll the Cloudflare GraphQL Analytics API for spikes.

No browser — so no Cloudflare bot challenge, no login, no thread-affinity
constraints. Polls the L7 DDoS timeseries every POLL_INTERVAL_SECONDS, feeds the
adaptive SpikeDetector, and fires on_spike for each genuinely new spike. Handles
the /mo command on a worker thread (renders a chart + AI explanation).
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, List, Tuple

import cloudflare_api
from chart import render_series_png
from config import config
from spike_detector import Spike, SpikeDetector

log = logging.getLogger("cf")


class ApiMonitor(threading.Thread):
    def __init__(self, on_spike: Callable[[Spike, "ApiMonitor"], None], lark_bot) -> None:
        super().__init__(name="cf-api-monitor", daemon=True)
        self.on_spike = on_spike
        self.lark = lark_bot
        self.detector = SpikeDetector(
            std_multiplier=config.spike_std_multiplier,
            baseline_window=config.spike_baseline_window,
            min_floor=config.spike_min_floor,
            warmup=config.spike_warmup,
            state_path=f"{config.state_dir}/spikes.json",
            prime_grace_minutes=config.spike_prime_grace_minutes,
        )
        self._series: List[Tuple[str, float]] = []
        self._kind = "l7ddos"
        self._running = threading.Event()
        self._running.set()

    # ------------------------------------------------------------- public API
    def snapshot_series(self) -> List[Tuple[str, float]]:
        return list(self._series)

    def submit_command(self, command: str, args: str, chat_id: str, message_id: str) -> None:
        # No browser here, so commands can run on their own worker thread.
        threading.Thread(
            target=self._handle_command, args=(command, args, chat_id, message_id),
            name="cf-command", daemon=True,
        ).start()

    def stop(self) -> None:
        self._running.clear()

    # --------------------------------------------------------------- polling
    def _poll(self) -> None:
        result = cloudflare_api.fetch_series(hours=6)
        self._series = result["series"]
        self._kind = result["kind"]

    # -------------------------------------------------------------- commands
    def _series_summary(self) -> str:
        series = self.snapshot_series()
        if not series:
            return "no data captured yet"
        latest_ts, latest = series[-1]
        peak_ts, peak = max(series, key=lambda p: p[1])
        label = "L7 DDoS mitigations" if self._kind == "l7ddos" else "total requests"
        return (
            f"{label}: latest {int(latest):,} ({latest_ts}); "
            f"6h peak {int(peak):,} ({peak_ts}); {len(series)} buckets"
        )

    def _handle_mo(self, chat_id: str, message_id: str) -> None:
        from qwen_client import explain_current

        self.lark.add_reaction(message_id, config.lark_reaction_processing)  # 👌 working
        try:
            self._poll()  # freshest data for the snapshot
        except Exception:
            log.exception("refresh for /mo failed; using cached data")

        series = self.snapshot_series()
        summary = self._series_summary()
        peak_ts = max(series, key=lambda p: p[1])[0] if series else None
        title = f"{config.cf_zone} — Cloudflare L7 DDoS (last 6h)"
        png = render_series_png(series, title, highlight_ts=peak_ts)
        explanation = explain_current(summary, series[-12:])

        if png:
            self.lark.send_image(chat_id, png)
        self.lark.send_text(chat_id, f"📊 {title}\n{summary}\n\n{explanation}", message_id)
        self.lark.add_reaction(message_id, config.lark_reaction_done)  # ✅ done

    def _handle_command(self, command: str, args: str, chat_id: str, message_id: str) -> None:
        try:
            if command in ("mo", "monitor", "status", "chart"):
                self._handle_mo(chat_id, message_id)
            else:
                self.lark.send_text(
                    chat_id, f"Unknown command '/{command}'. Try /mo for a live chart + AI review.",
                    message_id,
                )
        except Exception:
            log.exception("command /%s failed", command)
            try:
                self.lark.send_text(chat_id, f"⚠️ /{command} failed: internal error.", message_id)
            except Exception:
                pass

    # ------------------------------------------------------------------- loop
    def run(self) -> None:
        poll = config.poll_interval_seconds
        # Prime once so historical peaks aren't alerted as new.
        try:
            self._poll()
            self.detector.find_new_spikes(self.snapshot_series())
            log.info("API monitor primed with %d buckets (kind=%s)", len(self._series), self._kind)
        except Exception:
            log.exception("initial poll failed; will retry")

        while self._running.is_set():
            self._running.wait(poll)
            if not self._running.is_set():
                break
            try:
                self._poll()
                for s in self.detector.find_new_spikes(self.snapshot_series()):
                    log.warning("NEW SPIKE: %s = %s req (%.1fx baseline)", s.ts, int(s.count), s.ratio)
                    self.on_spike(s, self)
            except Exception:
                log.exception("poll/spike-eval error")
