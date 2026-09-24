"""API-based monitor: poll the Cloudflare GraphQL Analytics API for spikes.

No browser — so no Cloudflare bot challenge, no login, no thread-affinity
constraints. Polls the L7 DDoS timeseries every POLL_INTERVAL_SECONDS, feeds the
adaptive SpikeDetector, and fires on_spike for each genuinely new spike. Handles
the /mo command on a worker thread (renders a chart + AI explanation).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, List, Tuple

import requests

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
        # Alert coalescing: suppress repeat alerts within this window (see config).
        self._alert_cooldown_s = max(0.0, config.alert_cooldown_minutes * 60.0)
        self._last_alert_at = 0.0  # time.monotonic() of the last fired alert
        self._last_poll_ok = 0.0  # time.time() of the last successful poll (health report)
        # Stop *event*: clear while running, set to stop. The run loop sleeps via
        # _stop.wait(poll), which blocks while the flag is False — i.e. it really
        # waits the poll interval and wakes early only when stop() is called.
        # (An inverted "running" event here would make wait() return instantly
        # and hot-loop the Cloudflare API into 429s.)
        self._stop = threading.Event()

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
        self._stop.set()

    # --------------------------------------------------------------- polling
    def _poll(self) -> None:
        result = cloudflare_api.fetch_series(hours=6)
        self._series = result["series"]
        self._kind = result["kind"]
        self._last_poll_ok = time.time()

    # -------------------------------------------------------------- commands
    def _series_summary(self) -> str:
        from timeutil import fmt as fmt_ts
        from timeutil import tail_minutes, window_label
        series = tail_minutes(self.snapshot_series(), config.chart_window_minutes)
        if not series:
            return "no data captured yet"
        latest_ts, latest = series[-1]
        peak_ts, peak = max(series, key=lambda p: p[1])
        label = "L7 DDoS mitigations" if self._kind == "l7ddos" else "total requests"
        wl = window_label(config.chart_window_minutes)
        return (
            f"{label}: latest {int(latest):,} ({fmt_ts(latest_ts)}); "
            f"{wl} peak {int(peak):,} ({fmt_ts(peak_ts)}); {len(series)} buckets"
        )

    def _handle_mo(self, chat_id: str, message_id: str) -> None:
        from cards import mo_card
        from timeutil import tail_minutes, window_label

        working = self.lark.react_working(message_id)  # 👌 working
        try:
            self._poll()  # freshest data for the snapshot
        except Exception:
            log.exception("refresh for /mo failed; using cached data")

        # Zoom the chart + peak to the recent window; detection keeps the full series.
        series = tail_minutes(self.snapshot_series(), config.chart_window_minutes)
        summary = self._series_summary()
        peak_ts = max(series, key=lambda p: p[1])[0] if series else None
        title = f"{config.cf_zone} — Cloudflare L7 DDoS (last {window_label(config.chart_window_minutes)})"
        png = render_series_png(series, title, highlight_ts=peak_ts)
        image_key = self.lark.upload_image(png) if png else None

        # Chart + stats only, no AI review — keeps /mo instant (the local model
        # added minutes of latency).
        card = mo_card(series, image_key)
        if not self.lark.send_card(chat_id, card):
            # Fallback to image + text if the card is rejected.
            if png:
                self.lark.send_image(chat_id, png)
            self.lark.send_text(chat_id, f"📊 {title}\n{summary}", message_id)
        self.lark.react_done(message_id, working)  # remove 👌, add ✅

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

    def _maybe_alert(self, new_spikes) -> None:
        """Fire at most one alert per cooldown window for a batch of new spikes.

        A sustained attack produces a spike in several consecutive 5-min buckets;
        without this the group would get one card per bucket. We alert on the most
        severe new bucket and stay quiet for `alert_cooldown_minutes`. The detector
        has already recorded every bucket as seen, so suppressed ones never re-fire.
        """
        if not new_spikes:
            return
        peak = max(new_spikes, key=lambda s: s.count)
        now = time.monotonic()
        if self._last_alert_at and self._alert_cooldown_s and (now - self._last_alert_at) < self._alert_cooldown_s:
            log.info(
                "alert cooldown active (%.0fs left); suppressed %d new spike bucket(s), peak %s=%d req",
                self._alert_cooldown_s - (now - self._last_alert_at), len(new_spikes), peak.ts, int(peak.count),
            )
            return
        self._last_alert_at = now
        log.warning(
            "NEW SPIKE: %s = %d req (%.1fx baseline); %d new bucket(s), alerting peak",
            peak.ts, int(peak.count), peak.ratio, len(new_spikes),
        )
        self.on_spike(peak, self)

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

        while not self._stop.is_set():
            self._stop.wait(poll)  # sleeps the full interval; wakes early only on stop()
            if self._stop.is_set():
                break
            try:
                self._poll()
                self._maybe_alert(self.detector.find_new_spikes(self.snapshot_series()))
            except requests.exceptions.HTTPError as exc:
                resp = getattr(exc, "response", None)
                if resp is not None and resp.status_code == 429:
                    # Rate limited: back off (honor Retry-After when present)
                    # instead of retrying on the next tick and staying limited.
                    try:
                        retry_after = int(resp.headers.get("Retry-After", "0") or 0)
                    except ValueError:
                        retry_after = 0
                    delay = max(retry_after, poll * 4)
                    log.warning("Cloudflare API rate limited (429); backing off %ds", delay)
                    self._stop.wait(delay)
                else:
                    log.exception("poll/spike-eval error")
            except Exception:
                log.exception("poll/spike-eval error")
