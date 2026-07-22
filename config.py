"""Central configuration loaded from environment / .env file."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _str_set(name: str) -> frozenset[str]:
    """Parse a comma/whitespace-separated env var into a set of tokens."""
    raw = os.getenv(name, "") or ""
    return frozenset(t for t in re.split(r"[,\s]+", raw.strip()) if t)


def _str_list(name: str) -> tuple[str, ...]:
    """Parse a comma/whitespace-separated env var into an ordered, de-duped tuple.

    Order is preserved (unlike ``_str_set``) so, e.g., @mentions render in a
    stable sequence across sends.
    """
    raw = os.getenv(name, "") or ""
    out: list[str] = []
    for t in re.split(r"[,\s]+", raw.strip()):
        if t and t not in out:
            out.append(t)
    return tuple(out)


@dataclass
class Config:
    # Cloudflare
    cf_email: str = field(default_factory=lambda: os.getenv("CF_EMAIL", ""))
    cf_password: str = field(default_factory=lambda: os.getenv("CF_PASSWORD", ""))
    cf_account_id: str = field(default_factory=lambda: os.getenv("CF_ACCOUNT_ID", ""))
    cf_zone: str = field(default_factory=lambda: os.getenv("CF_ZONE", ""))
    cf_analytics_url: str = field(default_factory=lambda: os.getenv("CF_ANALYTICS_URL", ""))
    cf_headless: bool = field(default_factory=lambda: _bool("CF_HEADLESS", True))
    # Data source: "api" (Cloudflare GraphQL Analytics API, recommended) or
    # "browser" (scrape the dashboard — only viable from a residential IP).
    cf_mode: str = field(default_factory=lambda: os.getenv("CF_MODE", "api").strip().lower())
    # API mode:
    cf_api_token: str = field(default_factory=lambda: os.getenv("CF_API_TOKEN", ""))
    cf_api_base: str = field(default_factory=lambda: os.getenv("CF_API_BASE", "https://api.cloudflare.com/client/v4").rstrip("/"))
    cf_zone_tag: str = field(default_factory=lambda: os.getenv("CF_ZONE_TAG", ""))
    # Optional: path to a system Chromium, used if the bundled build won't run
    # (e.g. glibc too old on RHEL8/al8). Empty = use Playwright's bundled browser.
    cf_chromium_path: str = field(default_factory=lambda: os.getenv("CF_CHROMIUM_PATH", ""))

    # Lark / Feishu
    lark_app_id: str = field(default_factory=lambda: os.getenv("LARK_APP_ID", ""))
    lark_app_secret: str = field(default_factory=lambda: os.getenv("LARK_APP_SECRET", ""))
    lark_verification_token: str = field(default_factory=lambda: os.getenv("LARK_VERIFICATION_TOKEN", ""))
    lark_encrypt_key: str = field(default_factory=lambda: os.getenv("LARK_ENCRYPT_KEY", ""))
    # Lark (larksuite.com) vs Feishu (feishu.cn). This tenant is on Lark SG.
    lark_domain: str = field(default_factory=lambda: os.getenv("LARK_DOMAIN", "https://open.larksuite.com").rstrip("/"))
    lark_chat_id: str = field(default_factory=lambda: os.getenv("LARK_CHAT_ID", ""))
    # Bot's own open_id, used to confirm the bot (not just anyone) was @-mentioned.
    lark_bot_open_id: str = field(default_factory=lambda: os.getenv("LARK_BOT_OPEN_ID", ""))
    lark_reaction_processing: str = field(default_factory=lambda: os.getenv("LARK_REACTION_PROCESSING", "OK"))
    lark_reaction_done: str = field(default_factory=lambda: os.getenv("LARK_REACTION_DONE", "DONE"))
    # Lark user open_ids (ou_...) to @-mention on a spike alert, so a human is
    # pinged to look ("@Alice kindly check"). Rendered as real <at> mentions in
    # the card. Comma/space-separated; empty = mention nobody. Get an open_id by
    # DMing the bot "/whoami".
    alert_mention_open_ids: tuple = field(default_factory=lambda: _str_list("ALERT_MENTION_OPEN_IDS"))
    # Short note shown after the @mentions on an alert card.
    alert_mention_note: str = field(default_factory=lambda: os.getenv("ALERT_MENTION_NOTE", "kindly check"))

    # Remote deploy (/deploy command). Only Lark user open_ids in this allowlist,
    # messaging the bot in a 1:1 PM, may trigger a git pull + service restart.
    # Empty = nobody is authorized (the command is effectively disabled).
    admin_open_ids: frozenset = field(default_factory=lambda: _str_set("ADMIN_OPEN_IDS"))
    # Service name and the exact command used to restart it. The default uses
    # systemd-run so the restart runs in its own transient unit (survives this
    # process being killed by the restart). The service runs as root, so no sudo
    # is needed; prefix with 'sudo ' if you run it as a non-root user.
    deploy_service: str = field(default_factory=lambda: os.getenv("DEPLOY_SERVICE", "cloudflarebot.service"))
    deploy_restart_cmd: str = field(default_factory=lambda: os.getenv("DEPLOY_RESTART_CMD", ""))

    # Qwen / Ollama
    ollama_host: str = field(default_factory=lambda: os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/"))
    qwen_model: str = field(default_factory=lambda: os.getenv("QWEN_MODEL", "qwen3.6:35b-a3b"))
    qwen_timeout: int = field(default_factory=lambda: _int("QWEN_TIMEOUT", 300))

    # Spike detection
    spike_std_multiplier: float = field(default_factory=lambda: _float("SPIKE_STD_MULTIPLIER", 4.0))
    spike_baseline_window: int = field(default_factory=lambda: _int("SPIKE_BASELINE_WINDOW", 20))
    spike_min_floor: float = field(default_factory=lambda: _float("SPIKE_MIN_FLOOR", 1000))
    spike_warmup: int = field(default_factory=lambda: _int("SPIKE_WARMUP", 6))
    # On startup, still alert spikes within this many minutes (so a restart during
    # or right after an attack doesn't swallow it as "history").
    spike_prime_grace_minutes: float = field(default_factory=lambda: _float("SPIKE_PRIME_GRACE_MIN", 20.0))

    # Monitor loop
    poll_interval_seconds: int = field(default_factory=lambda: _int("POLL_INTERVAL_SECONDS", 30))
    state_dir: str = field(default_factory=lambda: os.getenv("STATE_DIR", "state"))

    # Display timezone (Cloudflare data is UTC; shown in this offset). Default GMT+8.
    display_tz_offset: float = field(default_factory=lambda: _float("DISPLAY_TZ_OFFSET", 8.0))
    display_tz_label: str = field(default_factory=lambda: os.getenv("DISPLAY_TZ_LABEL", "GMT+8"))

    def validate(self) -> list[str]:
        """Return a list of human-readable problems with the configuration."""
        problems = []
        required = {
            "LARK_APP_ID": self.lark_app_id,
            "LARK_APP_SECRET": self.lark_app_secret,
            "LARK_CHAT_ID": self.lark_chat_id,
        }
        if self.cf_mode == "browser":
            required["CF_EMAIL"] = self.cf_email
            required["CF_PASSWORD"] = self.cf_password
            required["CF_ANALYTICS_URL"] = self.cf_analytics_url
        else:  # api mode
            required["CF_API_TOKEN"] = self.cf_api_token
            if not (self.cf_zone_tag or self.cf_zone):
                problems.append("CF_MODE=api needs CF_ZONE_TAG (or CF_ZONE to look it up)")
        for key, val in required.items():
            if not val:
                problems.append(f"Missing required setting: {key}")
        return problems


config = Config()
