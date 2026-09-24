"""Checks for the daily health report card (see health_report.py).

All read-only: the monitor's and WS client's in-memory state, plus one bounded
HTTP probe each for the Cloudflare token and the Lark app credentials. Nothing
here sends a message, runs a GraphQL poll, or touches Playwright objects (they
are thread-affine and live on the browser monitor's thread).
"""
from __future__ import annotations

import datetime
import os
import time
from typing import Callable, List, Tuple
from urllib.parse import urlparse

import requests

import chart
import cloudflare_api
from config import config
from timeutil import fmt as fmt_ts
from timeutil import parse_utc

_PROBE_TIMEOUT = 10  # seconds, per HTTP probe
_BUCKET_S = 300      # Cloudflare 5-minute buckets
_TOKEN_EXPIRY_WARN_DAYS = 14


def _ago(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 120:
        return f"{s}s ago"
    if s < 7200:
        return f"{s // 60}m ago"
    return f"{s // 3600}h {s % 3600 // 60}m ago"


def _json(resp) -> dict:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def build_checks(lark_bot, monitor) -> List[Tuple[str, Callable]]:
    """(name, zero-arg check) pairs bound to the running LarkBot and monitor."""
    booted = time.time()
    browser = config.cf_mode == "browser"
    poll = max(1, config.poll_interval_seconds)
    # Normal gap between good polls is one interval; a 429 backs off poll*4
    # (api_monitor.run), so allow a couple of those before calling it stale.
    warn_after = max(poll * 10, 300)
    fail_after = max(poll * 60, 1800)

    def poll_age():
        """Seconds since the monitor last got fresh data, or None if never."""
        # Both monitors set it only on real data. Not CloudflareMonitor._last_capture:
        # its stale watchdog resets that on every reload, even with no data.
        last = getattr(monitor, "_last_poll_ok", 0.0)
        return time.time() - last if last else None

    def check_poller():
        if not monitor.is_alive():
            return "fail", f"thread {monitor.name} is not running"
        age = poll_age()
        if age is None:
            since = time.time() - booted
            status = "warn" if since < warn_after else "fail"
            return status, f"no successful poll since start ({_ago(since)})"
        detail = f"last poll {_ago(age)} (every {poll}s)"
        if age > fail_after:
            return "fail", detail
        if age > warn_after:
            return "warn", detail
        return "ok", detail

    def check_data():
        series = monitor.snapshot_series()
        if not series:
            return "warn", "no buckets from Cloudflare yet"
        latest_ts = series[-1][0]
        latest = parse_utc(latest_ts)
        if latest is None:
            return "warn", f"unreadable bucket time {latest_ts!r}"
        peak_ts, peak = max(series, key=lambda p: p[1])
        age = (datetime.datetime.now(datetime.timezone.utc) - latest).total_seconds()
        # Label the window: the series is the full fetch (6h in api mode), not /mo's.
        span_m = int(((latest - (parse_utc(series[0][0]) or latest)).total_seconds() + _BUCKET_S) // 60)
        window = f"{round(span_m / 60)}h" if span_m >= 120 else f"{span_m}m"
        if peak <= 0:  # max() of all-zero buckets is just the first one
            tail = f"no L7 DDoS mitigations in the last {window}"
        else:
            tail = f"{window} L7 DDoS peak {int(peak):,} at {fmt_ts(peak_ts, date=False)}"
        detail = f"{len(series)} buckets, latest {fmt_ts(latest_ts, date=False)} ({_ago(age)}); {tail}"
        return ("warn" if age > 12 * _BUCKET_S else "ok"), detail

    def check_cf_token():
        if browser:
            return None, "disabled by CF_MODE=browser"
        t0 = time.monotonic()
        try:
            r = requests.get(
                f"{config.cf_api_base}/user/tokens/verify",
                headers=cloudflare_api._headers(),
                timeout=_PROBE_TIMEOUT,
            )
        except requests.RequestException as exc:  # type only: some messages echo headers
            return "fail", f"{type(exc).__name__} ({urlparse(config.cf_api_base).hostname})"
        ms = int((time.monotonic() - t0) * 1000)
        body = _json(r)
        result = body.get("result") or {}
        if r.ok and body.get("success"):
            state = result.get("status", "?")
            if state != "active":
                return "fail", f"token is {state}"
            exp = parse_utc(result.get("expires_on") or "")
            if exp is None:
                return "ok", f"active, HTTP 200 in {ms} ms"
            days = (exp - datetime.datetime.now(datetime.timezone.utc)).days
            status = "warn" if days < _TOKEN_EXPIRY_WARN_DAYS else "ok"
            return status, f"active, expires in {days}d (HTTP 200 in {ms} ms)"
        # This endpoint only knows user-owned tokens and rejects account-owned
        # ones, so a rejection while polls keep succeeding is not an outage.
        age = poll_age()
        if r.status_code in (400, 401, 403) and age is not None and age <= warn_after:
            return "ok", f"polls succeed (verify endpoint says HTTP {r.status_code}; account-owned token?)"
        errors = body.get("errors") or [{}]
        msg = errors[0].get("message", "") if isinstance(errors[0], dict) else ""
        return "fail", f"HTTP {r.status_code} {str(msg or r.reason)[:80]}"

    def check_ws():
        ws = getattr(lark_bot, "_ws", None)
        if ws is None:
            return "fail", "WebSocket client not started"
        if not hasattr(ws, "_conn"):
            return None, "connection state not exposed by this lark-oapi version"
        # lark-oapi internal: _conn is None while disconnected / reconnecting.
        if ws._conn is None:
            return "fail", "disconnected (SDK auto-reconnect pending)"
        return "ok", "connected"

    def check_lark_api():
        host = urlparse(config.lark_domain).hostname or "Lark"
        t0 = time.monotonic()
        try:
            r = requests.post(
                f"{config.lark_domain}/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": config.lark_app_id, "app_secret": config.lark_app_secret},
                timeout=_PROBE_TIMEOUT,
            )
        except requests.RequestException as exc:
            return "fail", f"{type(exc).__name__} ({host})"
        ms = int((time.monotonic() - t0) * 1000)
        body = _json(r)
        if body.get("code") == 0 and body.get("tenant_access_token"):
            return "ok", f"tenant token in {ms} ms ({host})"
        return "fail", f"HTTP {r.status_code}, Lark code {body.get('code')}: {str(body.get('msg', ''))[:80]}"

    def check_detector():
        det = getattr(monitor, "detector", None)
        if det is None:
            return None, "monitor has no spike detector"
        # Spike state must persist, or a restart re-alerts old spikes.
        probe = os.path.dirname(os.path.abspath(det.state_path))
        while not os.path.isdir(probe) and os.path.dirname(probe) != probe:
            probe = os.path.dirname(probe)  # _save() creates the dir on first write
        if not os.access(probe, os.W_OK):
            return "fail", f"state dir {config.state_dir} is not writable"
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)
        recent = sorted(ts for ts in list(det._alerted) if (parse_utc(ts) or cutoff) > cutoff)
        if not recent:
            return "ok", "no spike buckets in 24h"
        return "ok", f"{len(recent)} spike bucket(s) in 24h, last {fmt_ts(recent[-1], date=False)}"

    def check_chart():
        if chart._HAVE_MPL:
            return "ok", "matplotlib available"
        return "warn", "matplotlib missing: alert and /mo cards go out without a chart"

    return [
        ("Cloudflare scraper" if browser else "Cloudflare poller", check_poller),
        ("Cloudflare data", check_data),
        ("Cloudflare API token", check_cf_token),
        ("Lark WebSocket", check_ws),
        ("Lark API", check_lark_api),
        ("Spike detector", check_detector),
        ("Chart rendering", check_chart),
    ]
