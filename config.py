"""Central configuration loaded from environment / .env file."""
from __future__ import annotations

import os
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


@dataclass
class Config:
    # Cloudflare
    cf_email: str = field(default_factory=lambda: os.getenv("CF_EMAIL", ""))
    cf_password: str = field(default_factory=lambda: os.getenv("CF_PASSWORD", ""))
    cf_account_id: str = field(default_factory=lambda: os.getenv("CF_ACCOUNT_ID", ""))
    cf_zone: str = field(default_factory=lambda: os.getenv("CF_ZONE", ""))
    cf_analytics_url: str = field(default_factory=lambda: os.getenv("CF_ANALYTICS_URL", ""))
    cf_headless: bool = field(default_factory=lambda: _bool("CF_HEADLESS", True))
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

    # Qwen / Ollama
    ollama_host: str = field(default_factory=lambda: os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/"))
    qwen_model: str = field(default_factory=lambda: os.getenv("QWEN_MODEL", "qwen3.6:35b-a3b"))
    qwen_timeout: int = field(default_factory=lambda: _int("QWEN_TIMEOUT", 120))

    # Spike detection
    spike_std_multiplier: float = field(default_factory=lambda: _float("SPIKE_STD_MULTIPLIER", 4.0))
    spike_baseline_window: int = field(default_factory=lambda: _int("SPIKE_BASELINE_WINDOW", 20))
    spike_min_floor: float = field(default_factory=lambda: _float("SPIKE_MIN_FLOOR", 1000))
    spike_warmup: int = field(default_factory=lambda: _int("SPIKE_WARMUP", 6))

    # Monitor loop
    poll_interval_seconds: int = field(default_factory=lambda: _int("POLL_INTERVAL_SECONDS", 30))
    state_dir: str = field(default_factory=lambda: os.getenv("STATE_DIR", "state"))

    def validate(self) -> list[str]:
        """Return a list of human-readable problems with the configuration."""
        problems = []
        required = {
            "CF_EMAIL": self.cf_email,
            "CF_PASSWORD": self.cf_password,
            "CF_ANALYTICS_URL": self.cf_analytics_url,
            "LARK_APP_ID": self.lark_app_id,
            "LARK_APP_SECRET": self.lark_app_secret,
            "LARK_CHAT_ID": self.lark_chat_id,
        }
        for key, val in required.items():
            if not val:
                problems.append(f"Missing required setting: {key}")
        return problems


config = Config()
