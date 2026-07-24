"""Lark interactive message cards for alerts and /mo replies.

A card is a JSON object sent with msg_type="interactive". We use a colored
header (red = abnormal, green = normal), structured fields, the AI explanation,
and — when available — the rendered 6-hour chart embedded via its image_key.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from config import config
from timeutil import fmt as fmt_ts

_TEMPLATE = {"ABNORMAL": "red", "NORMAL": "green", "UNKNOWN": "orange"}
_ICON = {"ABNORMAL": "🚨", "NORMAL": "✅", "UNKNOWN": "⚠️"}


def _md(content: str) -> dict:
    return {"tag": "lark_md", "content": content}


def _verdict_md(review, verdict: str) -> str:
    """Verdict section for the spike card.

    Includes the model's explanation only when the model actually answered —
    a failed/empty review (ok=False) would otherwise put raw error text like
    '(model returned an empty response)' in the group chat.
    """
    text = f"**🤖 Qwen verdict: {verdict}**"
    if getattr(review, "ok", True) and review.explanation.strip():
        text += f"\n{review.explanation.strip()}"
    return text


def _mention_md(open_ids: Optional[Sequence[str]], note: str) -> str:
    """Build a Lark @-mention line, e.g. '<at id=ou_xxx></at> kindly check'.

    In a Lark interactive card, ``lark_md`` renders ``<at id=ou_...></at>`` as a
    real, notifying @mention. A bare open_id (no tag) would show as plain text,
    which is the bug this replaces. Returns '' when there is nobody to mention.
    """
    ids = [i for i in (open_ids or ()) if i]
    if not ids:
        return ""
    ats = " ".join(f"<at id={i}></at>" for i in ids)
    note = (note or "").strip()
    return f"{ats} {note}".strip()


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


def spike_card(
    spike,
    review,
    image_key: Optional[str] = None,
    mention_ids: Optional[Sequence[str]] = None,
    mention_note: str = "",
) -> dict:
    verdict = review.verdict
    template = _TEMPLATE.get(verdict, "orange")
    icon = _ICON.get(verdict, "⚠️")
    elements = [
        {"tag": "div", "fields": [
            {"is_short": True, "text": _md(f"**🕒 Time**\n{fmt_ts(spike.ts)}")},
            {"is_short": True, "text": _md(f"**📈 Peak**\n{int(spike.count):,} req / 5-min")},
        ]},
        {"tag": "hr"},
        {"tag": "div", "text": _md(_verdict_md(review, verdict))},
        _img_element(image_key, "6h L7 DDoS chart"),
    ]
    mention = _mention_md(mention_ids, mention_note)
    if mention:
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": _md(f"🔔 {mention}")})
    return _card(template, f"{icon} Cloudflare L7 DDoS spike — {config.cf_zone}", elements)


def mo_card(series: List[Tuple[str, float]], image_key: Optional[str] = None) -> dict:
    """Status card for /mo — 🕒 Time + 🔺 6h Peak + chart. Blue, no @mention, no AI.

    Informational (never tags anyone) and deliberately AI-free so /mo stays
    instant; spike alerts are where the Qwen verdict lives.
    """
    title = f"📊 Cloudflare L7 DDoS — {config.cf_zone} (last 6h)"
    if not series:
        return _card("blue", title, [{"tag": "div", "text": _md("no data captured yet")}])
    latest_ts, _ = series[-1]
    peak = max(c for _, c in series)
    elements = [
        {"tag": "div", "fields": [
            {"is_short": True, "text": _md(f"**🕒 Time**\n{fmt_ts(latest_ts)}")},
            {"is_short": True, "text": _md(f"**🔺 6h Peak**\n{int(peak):,}")},
        ]},
        _img_element(image_key, "6h L7 DDoS chart"),
    ]
    return _card("blue", title, elements)


