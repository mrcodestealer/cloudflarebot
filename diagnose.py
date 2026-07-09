"""One-shot Cloudflare page diagnostic (uses the same undetected browser stack).

Run on the server to see exactly what the browser lands on when opening the
analytics URL — a bot challenge, a login page, an email-code step, or the real
chart — and whether any analytics GraphQL responses are captured.

IMPORTANT: stop the service first so it isn't holding the browser profile:
    systemctl stop cloudflarebot.service
    python diagnose.py
    systemctl start cloudflarebot.service      # when done

Outputs a text diagnosis plus state/debug/diagnose.png and .html.
"""
from __future__ import annotations

import logging
import os

from browser import (
    DRIVER,
    challenge_active,
    has_clearance,
    persistent_context_kwargs,
    start_virtual_display,
    sync_playwright,
    wait_for_challenge_clear,
)
from config import config
from cloudflare_monitor import extract_bucket_totals

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("diag")

graphql_urls: list[str] = []
parsed_points = {"n": 0}


def main() -> None:
    debug_dir = os.path.join(config.state_dir, "debug")
    os.makedirs(debug_dir, exist_ok=True)
    log.info("driver=%s  headless=%s  chrome=%s", DRIVER, config.cf_headless,
             config.cf_chromium_path or "channel:chrome")

    display = start_virtual_display() if not config.cf_headless else None
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(**persistent_context_kwargs(config))
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            def on_resp(r):
                if "/graphql" in r.url:
                    graphql_urls.append(r.url)
                    try:
                        parsed_points["n"] += len(extract_bucket_totals(r.json()))
                    except Exception:
                        pass

            page.on("response", on_resp)

            log.info("navigating to: %s", config.cf_analytics_url)
            page.goto(config.cf_analytics_url, wait_until="domcontentloaded", timeout=60000)

            if challenge_active(page):
                log.info("challenge present — waiting up to 45s to clear...")
                wait_for_challenge_clear(page, ctx, timeout=45)
            page.wait_for_timeout(5000)

            url = page.url
            try:
                title = page.title()
            except Exception:
                title = "(n/a)"
            has_email = page.locator('input[type="email"], input[name="email"]').count() > 0
            has_pw = page.locator('input[type="password"]').count() > 0
            try:
                body = page.locator("body").inner_text(timeout=5000).lower()
            except Exception:
                body = ""
            challenge = challenge_active(page)
            verify_code = any(k in body for k in ("verification code", "verify your identity", "we sent a code", "one-time"))
            analytics = any(k in body for k in (
                "requests mitigated", "security analytics", "mitigated by cloudflare",
                "traffic analysis", "requests served",
            ))

            log.info("=" * 60)
            log.info("landed URL : %s", url)
            log.info("page title : %s", title)
            log.info("cf_clearance cookie present: %s", has_clearance(ctx))
            log.info("login form?: email=%s password=%s", has_email, has_pw)
            log.info("challenge? : %s   verify-code? : %s", challenge, verify_code)
            log.info("analytics? : %s", analytics)
            log.info("graphql responses seen: %d", len(graphql_urls))
            for u in graphql_urls[:6]:
                log.info("   - %s", u.split("?")[0])
            log.info("timeseries points parsed: %d", parsed_points["n"])
            log.info("-" * 60)
            log.info("first 1200 chars of visible text:")
            print(body[:1200] if body else "(no visible text)")
            log.info("-" * 60)

            shot = os.path.join(debug_dir, "diagnose.png")
            html = os.path.join(debug_dir, "diagnose.html")
            try:
                page.screenshot(path=shot, full_page=True)
            except Exception:
                log.exception("screenshot failed")
            try:
                with open(html, "w", encoding="utf-8") as fh:
                    fh.write(page.content())
            except Exception:
                pass
            log.info("saved: %s  and  %s", shot, html)

            if analytics and parsed_points["n"] > 0:
                log.info("VERDICT: logged in and capturing data ✔")
            elif challenge:
                log.info("VERDICT: STILL blocked by the Cloudflare challenge (likely datacenter-IP reputation).")
            elif verify_code:
                log.info("VERDICT: Cloudflare wants an email verification code (new device).")
            elif has_email or has_pw or "/login" in url:
                log.info("VERDICT: challenge passed, sitting on the LOGIN page — login step needs work.")
            elif analytics and parsed_points["n"] == 0:
                log.info("VERDICT: analytics page loaded but no timeseries parsed — parser/endpoint mismatch.")
            else:
                log.info("VERDICT: unknown state — inspect diagnose.png / diagnose.html.")

            ctx.close()
    finally:
        if display is not None:
            try:
                display.stop()
            except Exception:
                pass


if __name__ == "__main__":
    main()
