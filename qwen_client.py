"""Qwen (via Ollama) client for reviewing traffic spikes and explaining charts.

Uses Ollama's POST /api/chat endpoint.  The model tag ``qwen3.6:35b-a3b`` is a
text model, so image explanation for the /mo command is grounded in the
numeric analytics context (the screenshot is attached separately by the bot).

All calls degrade gracefully: if Ollama is unreachable or slow we return a
best-effort fallback instead of raising, so a spike alert is never dropped
just because the reviewer was down.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

import requests

from config import config

log = logging.getLogger("qwen")


@dataclass
class Review:
    verdict: str            # "ABNORMAL", "NORMAL", or "UNKNOWN"
    explanation: str
    ok: bool                # whether the model actually answered


_ANALYST_SYSTEM = (
    "You are a senior Cloudflare Layer-7 DDoS security analyst. You are given a "
    "per-5-minute timeseries of the number of requests that Cloudflare's L7 DDoS "
    "protection MITIGATED (blocked/challenged) for one zone -- i.e. attack "
    "traffic Cloudflare already absorbed. Near-zero is normal (no attack). A "
    "spike means an L7 DDoS attack occurred and was mitigated; judge its scale "
    "and pattern: a large mitigation spike is a significant attack (ABNORMAL), a "
    "small or brief one is a minor/benign mitigation (NORMAL). Do NOT say there "
    "were 'no mitigations' -- the numbers ARE the mitigations. Be concise and "
    "concrete. Finish with exactly 'VERDICT: ABNORMAL' or 'VERDICT: NORMAL'."
)


def _metric_label(kind: str) -> str:
    if kind == "l7ddos":
        return "requests Cloudflare's L7 DDoS engine mitigated (blocked/challenged)"
    return "total HTTP requests"


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
        # Keep the model loaded between calls (avoids slow cold starts) and cap
        # output length so responses come back quickly.
        "keep_alive": "30m",
        "options": {"temperature": 0.2, "num_predict": 400},
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
    from timeutil import fmt as fmt_ts
    return "\n".join(f"  {fmt_ts(t, label=False)}: {int(c):,}" for t, c in recent) or "  (no recent data)"


def review_spike(spike: dict, kind: str = "l7ddos") -> Review:
    """Ask Qwen whether a detected mitigation spike is a significant attack."""
    from timeutil import fmt as fmt_ts
    metric = _metric_label(kind)
    user = (
        f"An L7 DDoS mitigation spike was detected for zone '{config.cf_zone}'.\n"
        f"All counts below are {metric}, per 5-minute bucket. Times are {config.display_tz_label}.\n\n"
        f"Spike bucket time: {fmt_ts(spike.get('ts'))}\n"
        f"Mitigated requests in the spike bucket: {int(spike.get('count', 0)):,}\n"
        f"Recent baseline mean: {spike.get('baseline_mean')} (near-zero = no attack normally)\n"
        f"Recent baseline std dev: {spike.get('baseline_std')}\n"
        f"Alert threshold: {spike.get('threshold')}\n"
        f"Spike is ~{spike.get('ratio')}x the baseline.\n\n"
        "Recent buckets leading into the spike (mitigated requests):\n"
        f"{_series_table(spike.get('recent', []))}\n\n"
        "Assess whether this is a significant L7 DDoS attack (ABNORMAL) or a "
        "minor/benign mitigation (NORMAL). One paragraph, then the verdict line."
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


def explain_current(summary: str, recent: List[Tuple[str, float]], kind: str = "l7ddos") -> str:
    """Explain the current state of the chart for the /mo command."""
    metric = _metric_label(kind)
    system = (
        "You are a Cloudflare Layer-7 DDoS security analyst. The numbers are "
        f"{metric}, per 5-minute bucket (near-zero = no attack; a spike = an "
        "attack Cloudflare mitigated). In 2-4 short sentences, explain the "
        "current situation to a non-expert: how much attack traffic was "
        "mitigated recently, any notable spikes, and whether it looks like a "
        "significant attack or just minor/benign mitigation. Never say there "
        "were 'no mitigations' -- the numbers ARE the mitigations."
    )
    user = (
        f"Zone: {config.cf_zone}\n"
        f"Summary: {summary}\n\n"
        f"Recent buckets (time: {metric}):\n"
        f"{_series_table(recent)}\n\n"
        "Explain what this chart is showing right now."
    )
    ok, content = _chat(system, user)
    if not ok:
        # No usable explanation: log why, return empty so callers simply omit
        # the AI-review section instead of showing an error to the group.
        log.warning("explain_current unavailable: %s", content)
        return ""
    return content
