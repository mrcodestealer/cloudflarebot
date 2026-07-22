"""Cloudflare Security Analytics monitor (Playwright, own thread).

Logs into the Cloudflare dashboard, opens the Security Analytics page for the
L7 DDoS mitigation service over the last 6 hours, turns on "Live data", and
then *passively intercepts* the GraphQL responses the page itself makes to
read the request-count timeseries -- this way authentication (session cookies
+ CSRF header) is handled by the browser and we never forge a request.

The captured series is fed to the adaptive SpikeDetector every poll interval;
each genuinely new spike triggers the ``on_spike`` callback (Qwen review +
Lark alert).  The same thread also services on-demand ``/mo`` commands (screen-
shot + AI explanation), because Playwright objects are thread-affine.

Everything Playwright-related happens on this single thread.  Cross-thread
requests (e.g. a /mo command arriving on the Lark WebSocket thread) are handed
over through a thread-safe queue.
"""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from browser import (
    DRIVER,
    PWTimeout,
    challenge_active,
    has_clearance,
    persistent_context_kwargs,
    start_virtual_display,
    sync_playwright,
    wait_for_challenge_clear,
)
from config import config
from spike_detector import Spike, SpikeDetector

log = logging.getLogger("cf")

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


# --------------------------------------------------------------------------- #
#  Defensive GraphQL response parsing (shape-based, resilient to key renames)  #
# --------------------------------------------------------------------------- #
def _looks_like_datetime(v) -> bool:
    return isinstance(v, str) and len(v) >= 10 and bool(_ISO_RE.match(v))


def _flatten(d, prefix="") -> Dict[str, object]:
    out: Dict[str, object] = {}
    if isinstance(d, dict):
        for k, v in d.items():
            if isinstance(v, dict):
                out.update(_flatten(v, f"{prefix}{k}."))
            else:
                out[f"{prefix}{k}"] = v
    return out


def _find_count(obj) -> Optional[float]:
    """Prefer an explicit 'count'; else any numeric metric (not a timestamp)."""
    if isinstance(obj, dict):
        if isinstance(obj.get("count"), (int, float)):
            return float(obj["count"])
        for k, v in obj.items():
            if k in ("__typename",):
                continue
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)) and "time" not in k.lower() and "date" not in k.lower():
                return float(v)
            if isinstance(v, dict):
                n = _find_count(v)
                if n is not None:
                    return n
    return None


def _row_ts(row: dict) -> Optional[str]:
    dims = row.get("dimensions", row)
    flat = _flatten(dims)
    # prefer a key that names a datetime dimension
    for k, v in flat.items():
        if "datetime" in k.lower() and _looks_like_datetime(v):
            return v  # type: ignore[return-value]
    for k, v in flat.items():
        if _looks_like_datetime(v):
            return v  # type: ignore[return-value]
    return None


def _is_timeseries(arr: list) -> bool:
    if not arr or not all(isinstance(x, dict) for x in arr):
        return False
    sample = arr[0]
    return _row_ts(sample) is not None and _find_count(sample) is not None


def _collect_timeseries_arrays(body) -> List[Tuple[str, list]]:
    """Walk the JSON and return (key, array) pairs that look like a timeseries."""
    found: List[Tuple[str, list]] = []

    def visit(node, key="") -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, list) and _is_timeseries(v):
                    found.append((k, v))
                visit(v, k)
        elif isinstance(node, list):
            for x in node:
                visit(x, key)

    visit(body)
    return found


_HINTS = ("httpRequestsAdaptiveGroups", "firewallEventsAdaptiveGroups")


def extract_bucket_totals(body) -> Dict[str, float]:
    """Return {iso_bucket: total_request_count} from the best timeseries array.

    Rows for a bucket may be split by securityAction/securitySource; we sum them
    so the total matches the chart's overall line for the applied filter.
    """
    arrays = _collect_timeseries_arrays(body)
    if not arrays:
        return {}
    # Rank: hint-key match first, then most rows.
    arrays.sort(key=lambda kv: (kv[0] not in _HINTS, -len(kv[1])))
    _, best = arrays[0]
    totals: Dict[str, float] = {}
    for row in best:
        ts = _row_ts(row)
        cnt = _find_count(row)
        if ts is None or cnt is None:
            continue
        totals[ts] = totals.get(ts, 0.0) + cnt
    return totals


# --------------------------------------------------------------------------- #
#  Monitor                                                                     #
# --------------------------------------------------------------------------- #
class CloudflareMonitor(threading.Thread):
    def __init__(
        self,
        on_spike: Callable[[Spike, "CloudflareMonitor"], None],
        lark_bot,
    ) -> None:
        super().__init__(name="cf-monitor", daemon=True)
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
        self._series: Dict[str, float] = {}
        self._last_capture = 0.0
        self._cmd_q: "queue.Queue[Tuple[str, str, str, str]]" = queue.Queue()
        self._running = threading.Event()
        self._running.set()
        self._page = None
        self._ctx = None
        self._display = None

    # ------------------------------------------------------------- public API
    def submit_command(self, command: str, args: str, chat_id: str, message_id: str) -> None:
        """Thread-safe: queue a command to run on the Playwright thread."""
        self._cmd_q.put((command, args, chat_id, message_id))

    def stop(self) -> None:
        self._running.clear()

    def snapshot_series(self) -> List[Tuple[str, float]]:
        return sorted(self._series.items(), key=lambda p: p[0])

    # ----------------------------------------------------------- capture hook
    def _on_response(self, response) -> None:
        if "/graphql" not in response.url:
            return
        try:
            body = response.json()
        except Exception:
            return
        totals = extract_bucket_totals(body)
        if not totals:
            return
        for ts, total in totals.items():
            self._series[ts] = total  # upsert: newest snapshot wins for a bucket
        self._last_capture = time.time()
        log.debug("captured %d buckets from %s", len(totals), response.url)

    # --------------------------------------------------------------- browser
    def _launch(self, p):
        self._ctx = p.chromium.launch_persistent_context(**persistent_context_kwargs(config))
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self._page.on("response", self._on_response)

    def _login_if_needed(self) -> None:
        page = self._page
        # A login page shows an email + password field.
        try:
            page.wait_for_timeout(2000)
            if "/login" not in page.url and page.locator('input[type="password"]').count() == 0:
                return
        except Exception:
            pass

        log.info("login page detected; signing in as %s", config.cf_email)
        try:
            email = page.locator('input[type="email"], input[name="email"]').first
            email.wait_for(timeout=15000)
            email.fill(config.cf_email)
            # Some flows reveal the password field only after the email step.
            pw = page.locator('input[type="password"], input[name="password"]').first
            if pw.count() == 0:
                page.get_by_role("button", name=re.compile("next|continue|log ?in", re.I)).first.click()
                page.wait_for_timeout(1500)
                pw = page.locator('input[type="password"], input[name="password"]').first
            pw.wait_for(timeout=15000)
            pw.fill(config.cf_password)
            page.get_by_role("button", name=re.compile("log ?in|sign ?in|continue", re.I)).first.click()
            page.wait_for_load_state("networkidle", timeout=45000)
        except Exception:
            log.exception("login flow hit an error (continuing; may already be authenticated)")

    def _enable_live_data(self) -> None:
        """Best-effort: open the time-range dropdown and turn on 'Live data'.

        Failure is non-fatal -- the staleness watchdog reloads the page so data
        stays fresh even if these selectors drift.
        """
        page = self._page
        try:
            page.get_by_role("button", name=re.compile(r"last .*hour|GMT", re.I)).first.click(timeout=8000)
            page.wait_for_timeout(800)
        except Exception:
            log.info("could not open time-range dropdown (non-fatal)")
            return
        try:
            live = page.get_by_text(re.compile(r"live data", re.I)).first
            live.wait_for(timeout=5000)
            # Click the toggle switch near the "Live data" label.
            toggle = page.locator(
                'xpath=//*[contains(translate(text(),"LIVE DATA","live data"),"live data")]'
                '/following::*[self::button or @role="switch" or contains(@class,"toggle")][1]'
            ).first
            if toggle.count() > 0:
                toggle.click(timeout=4000)
            else:
                live.click(timeout=4000)
            log.info("enabled Live data")
        except Exception:
            log.info("could not toggle Live data (non-fatal)")
        finally:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass

    def _pass_challenge(self) -> None:
        """Wait out a Cloudflare 'Just a moment...' managed challenge if present."""
        if challenge_active(self._page):
            log.info("Cloudflare challenge detected — waiting for it to clear...")
            cleared = wait_for_challenge_clear(self._page, self._ctx, timeout=45)
            log.info("challenge cleared=%s  cf_clearance=%s", cleared, has_clearance(self._ctx))

    def _open_analytics(self) -> None:
        self._page.goto(config.cf_analytics_url, wait_until="domcontentloaded", timeout=60000)
        self._pass_challenge()
        self._login_if_needed()
        # After login we may need to re-open the analytics URL.
        if "security/analytics" not in self._page.url:
            self._page.goto(config.cf_analytics_url, wait_until="domcontentloaded", timeout=60000)
            self._pass_challenge()
        try:
            self._page.wait_for_load_state("networkidle", timeout=30000)
        except PWTimeout:
            pass
        log.info("analytics page ready at %s", self._page.url)
        self._enable_live_data()

    def _reload(self) -> None:
        try:
            log.info("refreshing analytics page (staleness watchdog)")
            self._page.goto(config.cf_analytics_url, wait_until="domcontentloaded", timeout=60000)
            self._login_if_needed()
            self._enable_live_data()
        except Exception:
            log.exception("reload failed")

    # --------------------------------------------------------------- commands
    def _series_summary(self) -> str:
        series = self.snapshot_series()
        if not series:
            return "no data captured yet"
        latest_ts, latest = series[-1]
        peak_ts, peak = max(series, key=lambda p: p[1])
        return (
            f"latest bucket {int(latest):,} req ({latest_ts}); "
            f"6h peak {int(peak):,} req ({peak_ts}); {len(series)} buckets"
        )

    def _handle_mo(self, chat_id: str, message_id: str) -> None:
        from qwen_client import explain_current

        self.lark.add_reaction(message_id, config.lark_reaction_processing)  # 👌 working
        png = None
        try:
            png = self._page.screenshot(full_page=False)
        except Exception:
            log.exception("screenshot failed")

        summary = self._series_summary()
        recent = self.snapshot_series()[-12:]
        explanation = explain_current(summary, recent)

        if png:
            self.lark.send_image(chat_id, png)
        text = f"📊 {config.cf_zone} — Cloudflare L7 DDoS (last 6h)\n{summary}"
        if explanation.strip():
            text += f"\n\n{explanation.strip()}"
        self.lark.send_text(chat_id, text, message_id)
        self.lark.add_reaction(message_id, config.lark_reaction_done)  # ✅ done

    def _handle_command(self, command: str, args: str, chat_id: str, message_id: str) -> None:
        try:
            if command in ("mo", "monitor", "status", "chart"):
                self._handle_mo(chat_id, message_id)
            else:
                self.lark.send_text(
                    chat_id,
                    f"Unknown command '/{command}'. Try /mo for a live chart + AI review.",
                    message_id,
                )
                self.lark.add_reaction(message_id, config.lark_reaction_done)
        except Exception:
            log.exception("command /%s failed", command)
            try:
                self.lark.send_text(chat_id, f"⚠️ /{command} failed: internal error.", message_id)
            except Exception:
                pass

    def _drain_commands(self) -> None:
        while True:
            try:
                cmd = self._cmd_q.get_nowait()
            except queue.Empty:
                return
            self._handle_command(*cmd)

    # ------------------------------------------------------------------- loop
    def run(self) -> None:
        """Supervisor: keep the browser session alive; relaunch on any crash.

        Monitoring must never silently stop, so a failure in launch/login/capture
        is logged and the whole Playwright session is torn down and rebuilt after
        a short back-off, until stop() is called.
        """
        # Headful needs a display; start one virtual display for the whole run.
        if not config.cf_headless:
            self._display = start_virtual_display()

        try:
            while self._running.is_set():
                try:
                    self._run_session()
                except Exception:
                    log.exception("monitor session crashed; relaunching in 15s")
                    if self._running.is_set():
                        time.sleep(15)
        finally:
            if self._display is not None:
                try:
                    self._display.stop()
                except Exception:
                    pass

    def _run_session(self) -> None:
        with sync_playwright() as p:
            self._launch(p)
            try:
                self._open_analytics()
            except Exception:
                log.exception("failed to open analytics page (will keep trying via watchdog)")

            # Prime: capture initial data, then seed the detector so historical
            # peaks already on the chart are not alerted as if brand new.
            self._page.wait_for_timeout(8000)
            self.detector.find_new_spikes(self.snapshot_series())
            self._last_capture = time.time()  # grace period before the stale watchdog
            log.info("monitor primed with %d buckets", len(self._series))

            last_eval = time.time()
            poll = config.poll_interval_seconds
            stale_after = max(60, poll * 3)

            try:
                while self._running.is_set():
                    self._drain_commands()
                    # Pumps Playwright events (live GraphQL responses). A failure
                    # here means the page/context is broken -> bubble up to the
                    # supervisor for a clean relaunch.
                    self._page.wait_for_timeout(1000)

                    now = time.time()
                    if now - last_eval >= poll:
                        last_eval = now
                        try:
                            new_spikes = self.detector.find_new_spikes(self.snapshot_series())
                            for s in new_spikes:
                                log.warning("NEW SPIKE: %s = %s req (%.1fx baseline)", s.ts, int(s.count), s.ratio)
                                self.on_spike(s, self)
                        except Exception:
                            log.exception("spike evaluation error")

                    if now - self._last_capture > stale_after:
                        self._reload()
                        self._last_capture = time.time()  # avoid reload storm
            finally:
                try:
                    self._ctx.close()
                except Exception:
                    pass
