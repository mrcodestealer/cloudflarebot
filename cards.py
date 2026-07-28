"""Lark interactive message cards for alerts and /mo replies.

A card is a JSON object sent with msg_type="interactive". We use a colored
header (red = abnormal, green = normal), structured fields, the AI explanation,
and — when available — the rendered 6-hour chart embedded via its image_key.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from config import config
from timeutil import fmt as fmt_ts
from timeutil import window_label


def _md(content: str) -> dict:
    return {"tag": "lark_md", "content": content}


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
    image_key: Optional[str] = None,
    mention_ids: Optional[Sequence[str]] = None,
    mention_note: str = "",
) -> dict:
    elements = [
        {"tag": "div", "fields": [
            {"is_short": True, "text": _md(f"**🕒 Time**\n{fmt_ts(spike.ts)}")},
            {"is_short": True, "text": _md(f"**📈 Peak**\n{int(spike.count):,} req / 5-min")},
        ]},
        _img_element(image_key, "6h L7 DDoS chart"),
    ]
    mention = _mention_md(mention_ids, mention_note)
    if mention:
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": _md(f"🔔 {mention}")})
    return _card("orange", f"⚠️ Cloudflare L7 DDoS spike — {config.cf_zone}", elements)


def mo_card(series: List[Tuple[str, float]], image_key: Optional[str] = None) -> dict:
    """Status card for /mo — 🕒 Time + 🔺 6h Peak + chart. Blue, no @mention, no AI.

    Informational (never tags anyone) and deliberately AI-free so /mo stays instant.
    """
    wl = window_label(config.chart_window_minutes)
    title = f"📊 Cloudflare L7 DDoS — {config.cf_zone} (last {wl})"
    if not series:
        return _card("blue", title, [{"tag": "div", "text": _md("no data captured yet")}])
    latest_ts, _ = series[-1]
    peak = max(c for _, c in series)
    elements = [
        {"tag": "div", "fields": [
            {"is_short": True, "text": _md(f"**🕒 Time**\n{fmt_ts(latest_ts)}")},
            {"is_short": True, "text": _md(f"**🔺 {wl} Peak**\n{int(peak):,}")},
        ]},
        _img_element(image_key, f"{wl} L7 DDoS chart"),
    ]
    return _card("blue", title, elements)


