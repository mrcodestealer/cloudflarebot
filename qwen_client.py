"""Qwen (via Ollama) client for reviewing traffic spikes and explaining charts.

Uses Ollama's POST /api/chat endpoint.  The model tag ``qwen3.6:35b-a3b`` is a
text model, so image explanation for the /mo command is grounded in the
numeric analytics context (the screenshot is attached separately by the bot).

All calls degrade gracefully: if Ollama is unreachable or slow we return a
best-effort fallback instead of raising, so a spike alert is never dropped
just because the reviewer was down.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

import requests

from config import config


@dataclass
class Review:
    verdict: str            # "ABNORMAL", "NORMAL", or "UNKNOWN"
    explanation: str
    ok: bool                # whether the model actually answered


_ANALYST_SYSTEM = (
    "You are a senior Cloudflare Layer-7 DDoS security analyst. You are given "
    "request-rate telemetry from Cloudflare Security Analytics for a single "
    "zone. Decide whether a detected traffic spike looks like a genuine "
    "anomaly / likely attack, or normal traffic variation (e.g. a marketing "
    "burst, cron job, or benign crawl). Be concise and concrete. Always finish "
    "your reply with a final line in exactly this form: 'VERDICT: ABNORMAL' or "
    "'VERDICT: NORMAL'."
)


def _chat(system: str, user: str, timeout: Optional[int] = None) -> Tuple[bool, str]:
    """Return (ok, content). ok=False means the model did not answer."""
    url = f"{config.ollama_host}/api/chat"
    payload = {
        "model": config.qwen_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": {"temperature": 0.2},
    }
    try:
        resp = requests.post(url, json=payload, timeout=timeout or config.qwen_timeout)
        resp.raise_for_status()
        data = resp.json()
        content = (data.get("message") or {}).get("content", "").strip()
        if not content:
            return False, "(model returned an empty response)"
        return True, content
    except requests.exceptions.RequestException as exc:
        return False, f"(could not reach Qwen/Ollama: {exc})"
    except ValueError as exc:  # JSON decode
        return False, f"(bad response from Qwen/Ollama: {exc})"


def _series_table(recent: List[Tuple[str, float]]) -> str:
    return "\n".join(f"  {t}: {int(c):,} req" for t, c in recent) or "  (no recent data)"


def review_spike(spike: dict) -> Review:
    """Ask Qwen whether a detected spike is abnormal."""
    user = (
        "A traffic spike was detected on the Cloudflare Security Analytics "
        f"(L7 DDoS) chart for zone '{config.cf_zone}'.\n\n"
        f"Spike bucket time (UTC): {spike.get('ts')}\n"
        f"Spike request count: {int(spike.get('count', 0)):,}\n"
        f"Recent baseline mean: {spike.get('baseline_mean')}\n"
        f"Recent baseline std dev: {spike.get('baseline_std')}\n"
        f"Alert threshold (mean + N*std): {spike.get('threshold')}\n"
        f"Spike is ~{spike.get('ratio')}x the baseline.\n\n"
        "Recent buckets leading into the spike:\n"
        f"{_series_table(spike.get('recent', []))}\n\n"
        "Is this spike abnormal / a likely attack? Give a one-paragraph "
        "assessment, then the verdict line."
    )
    ok, content = _chat(_ANALYST_SYSTEM, user)
    verdict = "UNKNOWN"
    m = re.search(r"VERDICT:\s*(ABNORMAL|NORMAL)", content, re.IGNORECASE)
    if m:
        verdict = m.group(1).upper()
    elif ok:
        # Model answered but didn't follow the format; infer from keywords.
        low = content.lower()
        if any(k in low for k in ("attack", "abnormal", "anomal", "malicious", "ddos")):
            verdict = "ABNORMAL"
        elif "normal" in low:
            verdict = "NORMAL"
    return Review(verdict=verdict, explanation=content, ok=ok)


def explain_current(summary: str, recent: List[Tuple[str, float]]) -> str:
    """Explain the current state of the chart for the /mo command."""
    system = (
        "You are a Cloudflare Layer-7 DDoS security analyst. In 2-4 short "
        "sentences, explain the current traffic situation for a zone to a "
        "non-expert: overall level, any recent spikes or anomalies, and "
        "whether it looks like normal traffic or a possible attack."
    )
    user = (
        f"Zone: {config.cf_zone}\n"
        f"Summary: {summary}\n\n"
        "Recent request buckets (time: count):\n"
        f"{_series_table(recent)}\n\n"
        "Explain what this chart is showing right now."
    )
    ok, content = _chat(system, user)
    if not ok:
        return f"(AI explanation unavailable) {content}"
    return content
