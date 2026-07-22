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

import datetime
import logging
import sys
import threading
import time

import deployer
from cards import spike_card
from chart import render_series_png
from config import config
from lark_bot import LarkBot
from qwen_client import review_spike
from spike_detector import Spike

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")

_MO_ALIASES = {"mo", "monitor", "status", "chart"}
_DEPLOY_ALIASES = {"deploy", "redeploy", "update", "pull", "git"}

_VERDICT_ICON = {"ABNORMAL": "🚨", "NORMAL": "✅", "UNKNOWN": "⚠️"}


def _format_alert(spike, review) -> str:
    from timeutil import fmt as fmt_ts
    icon = _VERDICT_ICON.get(review.verdict, "⚠️")
    lines = [
        f"{icon} Cloudflare L7 DDoS spike — {config.cf_zone}",
        f"• Time: {fmt_ts(spike.ts)}",
        f"• Peak: {int(spike.count):,} req in one bucket",
        f"• Baseline: {int(spike.baseline_mean):,} avg (±{int(spike.baseline_std):,}), "
        f"~{spike.ratio}× normal",
        f"• Qwen verdict: {review.verdict}",
    ]
    # Include the model's explanation only when it actually answered — never
    # raw failure text like '(model returned an empty response)'.
    if getattr(review, "ok", True) and review.explanation.strip():
        lines += ["", review.explanation.strip()]
    # Plain text can't render a real @mention (that needs the interactive card),
    # so surface only the note here — never the raw open_ids.
    if config.alert_mention_open_ids and config.alert_mention_note.strip():
        lines.append(f"\n🔔 {config.alert_mention_note.strip()}")
    return "\n".join(lines)


def _sample_spike() -> Spike:
    """A realistic synthetic spike used by /testalert to preview the alert."""
    now = datetime.datetime.now(datetime.timezone.utc).replace(second=0, microsecond=0)

    def iso(dt: datetime.datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    recent = [(iso(now - datetime.timedelta(minutes=5 * i)), 1000 + (i % 4) * 40) for i in range(6, 0, -1)]
    return Spike(
        ts=iso(now),
        count=92000,
        baseline_mean=1050.0,
        baseline_std=60.0,
        threshold=1290.0,
        ratio=87.6,
        recent=recent,
    )


def main() -> int:
    problems = config.validate()
    if problems:
        for p in problems:
            log.error("CONFIG: %s", p)
        log.error("Fix the .env file and retry.")
        return 1

    lark_bot = LarkBot(config)

    # If a /deploy just restarted us, post a "back online" confirmation.
    deployer.notify_if_redeployed(lark_bot)

    def on_spike(spike, monitor) -> None:
        """Review a new spike with Qwen and alert the group (off the monitor thread)."""
        def work() -> None:
            try:
                review = review_spike(spike.as_dict(), kind=getattr(monitor, "_kind", "l7ddos"))
                # Render the full 6h chart with the spike bucket marked.
                series = monitor.snapshot_series() or spike.recent
                png = render_series_png(
                    series, f"{config.cf_zone} — Cloudflare L7 DDoS (last 6h)", highlight_ts=spike.ts
                )
                image_key = lark_bot.upload_image(png) if png else None
                card = spike_card(
                    spike, review, image_key,
                    mention_ids=config.alert_mention_open_ids,
                    mention_note=config.alert_mention_note,
                )
                if not lark_bot.send_card(config.lark_chat_id, card):
                    # Fall back to plain text if the card is rejected.
                    lark_bot.send_text(config.lark_chat_id, _format_alert(spike, review))
                log.info("alerted spike %s (verdict=%s, chart=%s)", spike.ts, review.verdict, bool(image_key))
            except Exception:
                log.exception("failed to alert spike %s", spike.ts)

        threading.Thread(target=work, name="spike-alert", daemon=True).start()

    if config.cf_mode == "browser":
        from cloudflare_monitor import CloudflareMonitor
        monitor = CloudflareMonitor(on_spike=on_spike, lark_bot=lark_bot)
        log.info("data source: browser (dashboard scraping)")
    else:
        from api_monitor import ApiMonitor
        monitor = ApiMonitor(on_spike=on_spike, lark_bot=lark_bot)
        log.info("data source: Cloudflare GraphQL Analytics API")

    def run_test_alert(chat_id: str, message_id: str) -> None:
        """/testalert — post a sample spike alert through the real format + Qwen path.

        Runs on its own thread (never the monitor thread) so it can't stall live
        monitoring even if Qwen is slow.
        """
        working = lark_bot.react_working(message_id)  # 👌 working
        try:
            spike = _sample_spike()
            review = review_spike(spike.as_dict())
            png = render_series_png(
                spike.recent, f"{config.cf_zone} — Cloudflare L7 DDoS (sample)", highlight_ts=spike.ts
            )
            image_key = lark_bot.upload_image(png) if png else None
            # Tests never @mention anyone — only genuine spike alerts ping a human.
            card = spike_card(spike, review, image_key)
            card["header"]["title"]["content"] = "🧪 TEST — " + card["header"]["title"]["content"]
            if config.alert_mention_open_ids:
                card["elements"].append({"tag": "hr"})
                card["elements"].append({"tag": "div", "text": {
                    "tag": "lark_md",
                    "content": (
                        f"🔕 test — no one tagged (a real alert would @mention "
                        f"{len(config.alert_mention_open_ids)}: {config.alert_mention_note})"
                    ),
                }})
            if not lark_bot.send_card(config.lark_chat_id, card):
                lark_bot.send_text(config.lark_chat_id, "🧪 TEST ALERT\n\n" + _format_alert(spike, review))
            log.info("sent test alert (qwen ok=%s verdict=%s)", review.ok, review.verdict)
        except Exception:
            log.exception("test alert failed")
            try:
                lark_bot.send_text(chat_id, "⚠️ /testalert failed: internal error.", message_id)
            except Exception:
                pass
        finally:
            lark_bot.react_done(message_id, working)  # remove 👌, add ✅

    def _reply_async(text: str, chat_id: str, message_id: str) -> None:
        threading.Thread(
            target=lambda: lark_bot.send_text(chat_id, text, message_id), daemon=True
        ).start()

    def command_handler(
        command: str,
        args: str,
        chat_id: str,
        message_id: str,
        chat_type: str = "",
        sender_open_id: str = "",
    ) -> None:
        """Runs on the WS thread — keep it non-blocking."""
        if command in _MO_ALIASES:
            monitor.submit_command(command, args, chat_id, message_id)
        elif command in ("testalert", "test"):
            threading.Thread(
                target=run_test_alert, args=(chat_id, message_id), name="test-alert", daemon=True
            ).start()
        elif command == "whoami":
            # Handy for populating ADMIN_OPEN_IDS: tells the caller their open_id.
            _reply_async(f"Your Lark open_id:\n{sender_open_id or '(unknown)'}", chat_id, message_id)
        elif command in _DEPLOY_ALIASES:
            # Admin-only, PM-only: git pull + restart the service.
            if chat_type != "p2p":
                log.info("ignoring /%s outside a 1:1 PM (chat_type=%s)", command, chat_type)
                return
            if not config.admin_open_ids or sender_open_id not in config.admin_open_ids:
                log.warning("unauthorized /%s attempt from open_id=%r", command, sender_open_id)
                _reply_async(
                    "⛔ Not authorized to deploy.\n"
                    f"Your open_id: {sender_open_id or '(unknown)'}\n"
                    "An admin must add it to ADMIN_OPEN_IDS in the server .env.",
                    chat_id,
                    message_id,
                )
                return
            log.info("authorized /%s from open_id=%s", command, sender_open_id)
            threading.Thread(
                target=deployer.run_deploy,
                args=(lark_bot, chat_id, message_id),
                name="deploy",
                daemon=True,
            ).start()
        elif chat_type == "group":
            # Stay silent on unknown commands in a group (only tagged known
            # commands like /mo get a reply — keeps the group uncluttered).
            log.info("ignoring unknown group command '/%s'", command)
        else:
            _reply_async(
                f"Unknown command '/{command}'.\n"
                f"• /mo — live chart + AI review\n"
                f"• /testalert — post a sample spike alert\n"
                f"• /deploy — git pull + restart (admin only)\n"
                f"• /whoami — show your open_id",
                chat_id,
                message_id,
            )

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
