"""Lark interactive message cards for alerts and /mo replies.

A card is a JSON object sent with msg_type="interactive". We use a colored
header (red = abnormal, green = normal), structured fields, the AI explanation,
and — when available — the rendered 6-hour chart embedded via its image_key.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from config import config
from timeutil import fmt as fmt_ts

_TEMPLATE = {"ABNORMAL": "red", "NORMAL": "green", "UNKNOWN": "orange"}
_ICON = {"ABNORMAL": "🚨", "NORMAL": "✅", "UNKNOWN": "⚠️"}


def _md(content: str) -> dict:
    return {"tag": "lark_md", "content": content}


def _img_element(image_key: Optional[str], alt: str) -> Optional[dict]:
    if not image_key:
        return None
    return {"tag": "img", "img_key": image_key, "alt": {"tag": "plain_text", "content": alt}}


def _card(template: str, title: str, elements: List[dict]) -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
        "elements": [e for e in elements if e],
    }


def spike_card(spike, review, image_key: Optional[str] = None) -> dict:
    verdict = review.verdict
    template = _TEMPLATE.get(verdict, "orange")
    icon = _ICON.get(verdict, "⚠️")
    elements = [
        {"tag": "div", "fields": [
            {"is_short": True, "text": _md(f"**🕒 Time**\n{fmt_ts(spike.ts)}")},
            {"is_short": True, "text": _md(f"**📈 Requests**\n{int(spike.count):,} / 5-min")},
            {"is_short": True, "text": _md(f"**📊 Baseline**\n{int(spike.baseline_mean):,} (±{int(spike.baseline_std):,})")},
            {"is_short": True, "text": _md(f"**🔺 vs normal**\n~{spike.ratio}×")},
        ]},
        {"tag": "hr"},
        {"tag": "div", "text": _md(f"**🤖 Qwen verdict: {verdict}**\n{review.explanation.strip()}")},
        _img_element(image_key, "6h L7 DDoS chart"),
    ]
    return _card(template, f"{icon} Cloudflare L7 DDoS spike — {config.cf_zone}", elements)


def info_card(
    title: str,
    summary: str,
    explanation: str,
    image_key: Optional[str] = None,
    template: str = "blue",
    test: bool = False,
) -> dict:
    if test:
        template = "grey"
        title = "🧪 " + title
    elements = [
        {"tag": "div", "text": _md(summary)},
        _img_element(image_key, "6h L7 DDoS chart"),
        {"tag": "hr"},
        {"tag": "div", "text": _md(f"**🤖 AI review**\n{explanation.strip()}")},
    ]
    return _card(template, title, elements)
