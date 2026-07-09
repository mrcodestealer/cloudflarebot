"""Browser helpers: undetected driver + virtual display + Cloudflare challenge utils.

We drive an *undetected* browser to get past Cloudflare's managed challenge:

* **patchright** (a drop-in, patched Playwright fork) is used if installed — it
  hides the CDP `Runtime.enable` leak and automation fingerprints Cloudflare
  checks. Falls back to vanilla Playwright otherwise.
* Real **Google Chrome** (channel/executable_path), **headful** under an Xvfb
  virtual display (headless is the biggest bot signal), and a **persistent
  profile** so the `cf_clearance` cookie survives.

Per patchright guidance we deliberately do NOT set a custom user_agent/headers;
overriding them re-introduces mismatches Cloudflare detects. The only args we
pass are the ones required to run Chrome headful as root.
"""
from __future__ import annotations

import logging
import os
import sys
import time

log = logging.getLogger("browser")

# Prefer the undetected driver; fall back to vanilla Playwright.
try:
    from patchright.sync_api import sync_playwright, TimeoutError as PWTimeout  # noqa: F401
    DRIVER = "patchright"
except Exception:  # pragma: no cover - patchright optional
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout  # noqa: F401
    DRIVER = "playwright"


def start_virtual_display(size=(1920, 1080)):
    """Start an Xvfb virtual display so headful Chrome works on a headless server.

    Returns the Display handle (keep a reference so it isn't GC'd), or None if a
    display already exists / isn't needed / pyvirtualdisplay isn't installed.
    """
    if sys.platform != "linux" or os.environ.get("DISPLAY"):
        return None
    try:
        from pyvirtualdisplay import Display
    except Exception:
        log.warning("pyvirtualdisplay not installed — headful browser needs a display; "
                    "install it or run with a real DISPLAY")
        return None
    disp = Display(backend="xvfb", visible=False, size=size, color_depth=24)
    disp.start()
    log.info("started virtual display DISPLAY=%s (driver=%s)", os.environ.get("DISPLAY"), DRIVER)
    return disp


def persistent_context_kwargs(config) -> dict:
    kw = dict(
        user_data_dir=".cf_profile",
        headless=config.cf_headless,
        no_viewport=True,
        # Only necessity args: Chrome won't run headful as root without --no-sandbox;
        # --disable-dev-shm-usage avoids crashes on small /dev/shm.
        args=["--no-sandbox", "--disable-dev-shm-usage", "--window-size=1920,1080"],
    )
    if config.cf_chromium_path:
        kw["executable_path"] = config.cf_chromium_path
    else:
        kw["channel"] = "chrome"  # real Google Chrome, best for evasion
    return kw


def challenge_active(page) -> bool:
    """True if the page is showing a Cloudflare 'Just a moment...' challenge."""
    try:
        title = (page.title() or "").lower()
    except Exception:
        title = ""
    if "just a moment" in title or "attention required" in title:
        return True
    try:
        return page.locator(
            "#challenge-form, #cf-challenge-running, iframe[src*='challenges.cloudflare.com']"
        ).count() > 0
    except Exception:
        return False


def has_clearance(context) -> bool:
    try:
        return any(c.get("name") == "cf_clearance" for c in context.cookies())
    except Exception:
        return False


def wait_for_challenge_clear(page, context, timeout: int = 45) -> bool:
    """Poll up to `timeout`s for the managed challenge to auto-solve. True if cleared."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not challenge_active(page):
            return True
        time.sleep(1.0)
    return not challenge_active(page)
