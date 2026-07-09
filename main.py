"""cloudflarebot — Cloudflare L7 DDoS spike monitor with a Lark/Feishu bot.

Architecture
------------
* Thread A (main): Lark WebSocket subscription (persistent connection). Receives
  ``im.message.receive_v1`` events; on "@bot /mo" it hands a job to Thread B.
* Thread B (cf-monitor): owns Playwright. Logs into the Cloudflare dashboard,
  watches the live 6h Security Analytics (L7 DDoS) chart, detects new spikes,
  and services /mo screenshot commands.
* Spikes -> Qwen (Ollama) review -> alert posted to the Lark group. The review
  runs on a short-lived worker thread so the monitor loop never stalls.

Playwright is thread-affine, so all browser work stays on Thread B; the WS
handler only enqueues.
"""
from __future__ import annotations

import logging
import sys
import threading
import time

from config import config
from cloudflare_monitor import CloudflareMonitor
from lark_bot import LarkBot
from qwen_client import review_spike

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")

_MO_ALIASES = {"mo", "monitor", "status", "chart"}

_VERDICT_ICON = {"ABNORMAL": "🚨", "NORMAL": "✅", "UNKNOWN": "⚠️"}


def _format_alert(spike, review) -> str:
    icon = _VERDICT_ICON.get(review.verdict, "⚠️")
    lines = [
        f"{icon} Cloudflare L7 DDoS spike — {config.cf_zone}",
        f"• Time (UTC): {spike.ts}",
        f"• Requests: {int(spike.count):,} in one bucket",
        f"• Baseline: {int(spike.baseline_mean):,} avg (±{int(spike.baseline_std):,}), "
        f"~{spike.ratio}× normal",
        f"• Qwen verdict: {review.verdict}",
        "",
        review.explanation.strip() or "(no AI assessment available)",
    ]
    return "\n".join(lines)


def main() -> int:
    problems = config.validate()
    if problems:
        for p in problems:
            log.error("CONFIG: %s", p)
        log.error("Fix the .env file and retry.")
        return 1

    lark_bot = LarkBot(config)

    def on_spike(spike, _monitor) -> None:
        """Review a new spike with Qwen and alert the group (off the monitor thread)."""
        def work() -> None:
            try:
                review = review_spike(spike.as_dict())
                lark_bot.send_text(config.lark_chat_id, _format_alert(spike, review))
                log.info("alerted spike %s (verdict=%s)", spike.ts, review.verdict)
            except Exception:
                log.exception("failed to alert spike %s", spike.ts)

        threading.Thread(target=work, name="spike-alert", daemon=True).start()

    monitor = CloudflareMonitor(on_spike=on_spike, lark_bot=lark_bot)

    def command_handler(command: str, args: str, chat_id: str, message_id: str) -> None:
        """Runs on the WS thread — keep it non-blocking."""
        if command in _MO_ALIASES:
            monitor.submit_command(command, args, chat_id, message_id)
        else:
            def reply() -> None:
                lark_bot.send_text(
                    chat_id,
                    f"Unknown command '/{command}'. Tag me with /mo for a live "
                    f"chart snapshot + AI review.",
                    message_id,
                )
            threading.Thread(target=reply, daemon=True).start()

    lark_bot.command_handler = command_handler

    log.info("starting Cloudflare monitor thread...")
    monitor.start()

    # Give the browser a moment to launch/login before opening the WS.
    time.sleep(2)

    try:
        lark_bot.start()  # blocks forever, auto-reconnects
    except KeyboardInterrupt:
        log.info("shutting down...")
    finally:
        monitor.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
