"""One-shot Cloudflare page diagnostic.

Run this on the server to see *exactly* what the browser lands on when it opens
the analytics URL — login page, a bot challenge, an SSO/redirect, or the real
chart — and whether any analytics GraphQL responses are captured.

IMPORTANT: stop the service first so it isn't holding the browser profile:
    systemctl stop cloudflarebot.service
    python diagnose.py
    systemctl start cloudflarebot.service      # when done

Outputs a text diagnosis plus state/debug/diagnose.png and .html for a closer look.
"""
from __future__ import annotations

import logging
import os
import re

from playwright.sync_api import sync_playwright

from config import config
from cloudflare_monitor import extract_bucket_totals

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("diag")

graphql_urls: list[str] = []
parsed_points = {"n": 0}


def main() -> None:
    debug_dir = os.path.join(config.state_dir, "debug")
    os.makedirs(debug_dir, exist_ok=True)

    with sync_playwright() as p:
        kw = dict(
            user_data_dir=".cf_profile",
            headless=config.cf_headless,
            viewport={"width": 1600, "height": 900},
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        )
        if config.cf_chromium_path:
            kw["executable_path"] = config.cf_chromium_path
            log.info("using system Chromium: %s", config.cf_chromium_path)
        ctx = p.chromium.launch_persistent_context(**kw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_resp(r):
            if "/graphql" in r.url:
                graphql_urls.append(r.url)
                try:
                    t = extract_bucket_totals(r.json())
                    parsed_points["n"] += len(t)
                except Exception:
                    pass

        page.on("response", on_resp)

        log.info("navigating to: %s", config.cf_analytics_url)
        page.goto(config.cf_analytics_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)

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
        challenge = any(k in body for k in (
            "verify you are human", "checking your browser", "just a moment",
            "are you a robot", "captcha", "cf-challenge", "needs to review the security",
        ))
        verify_code = any(k in body for k in ("verification code", "verify your identity", "we sent a code", "one-time"))
        analytics = any(k in body for k in (
            "requests mitigated", "security analytics", "mitigated by cloudflare",
            "traffic analysis", "requests served",
        ))

        log.info("=" * 60)
        log.info("landed URL : %s", url)
        log.info("page title : %s", title)
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

        # Verdict
        if analytics and parsed_points["n"] > 0:
            log.info("VERDICT: logged in and capturing data ✔  (0-bucket issue is elsewhere)")
        elif challenge:
            log.info("VERDICT: blocked by a Cloudflare bot challenge in headless mode.")
        elif verify_code:
            log.info("VERDICT: Cloudflare wants an email verification code (new device).")
        elif has_email or has_pw or "/login" in url:
            log.info("VERDICT: sitting on the LOGIN page — session not established.")
        elif analytics and parsed_points["n"] == 0:
            log.info("VERDICT: analytics page loaded but no timeseries parsed — parser/endpoint mismatch.")
        else:
            log.info("VERDICT: unknown state — inspect diagnose.png / diagnose.html.")

        ctx.close()


if __name__ == "__main__":
    main()
