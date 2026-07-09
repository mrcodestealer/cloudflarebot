"""Cloudflare GraphQL Analytics API client (token auth — no browser, no challenge).

Fetches the L7 DDoS request timeseries the dashboard's Security Analytics chart
is built from, using the httpRequestsAdaptiveGroups dataset grouped by 5-minute
buckets. Works from any IP because api.cloudflare.com uses Bearer-token auth and
is not behind the interactive bot challenge that blocks the dashboard.
"""
from __future__ import annotations

import datetime
import logging
from typing import Dict, List, Optional, Tuple

import requests

from config import config

log = logging.getLogger("cfapi")

_MITIGATING_SOURCES = {"l7ddos"}  # what "mitigation-service=l7ddos" maps to

_zone_tag_cache: Optional[str] = None


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.cf_api_token}", "Content-Type": "application/json"}


def resolve_zone_tag() -> str:
    """Return the zone id (tag). Prefer CF_ZONE_TAG; else look it up by name."""
    global _zone_tag_cache
    if config.cf_zone_tag:
        return config.cf_zone_tag
    if _zone_tag_cache:
        return _zone_tag_cache
    r = requests.get(
        f"{config.cf_api_base}/zones",
        headers=_headers(),
        params={"name": config.cf_zone},
        timeout=30,
    )
    r.raise_for_status()
    result = r.json().get("result") or []
    if not result:
        raise RuntimeError(
            f"Zone '{config.cf_zone}' not found via API (token needs Zone:Read, "
            f"or set CF_ZONE_TAG explicitly)."
        )
    _zone_tag_cache = result[0]["id"]
    log.info("resolved zone tag for %s", config.cf_zone)
    return _zone_tag_cache


def _graphql(query: str) -> dict:
    r = requests.post(
        f"{config.cf_api_base}/graphql",
        headers=_headers(),
        json={"query": query},
        timeout=45,
    )
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError(f"GraphQL errors: {body['errors']}")
    return body["data"]


def _iso(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _query(zone_tag: str, start: str, end: str, with_source: bool) -> str:
    dims = "datetimeFiveMinutes securitySource" if with_source else "datetimeFiveMinutes"
    return (
        "{ viewer { zones(filter: {zoneTag: \"" + zone_tag + "\"}) { "
        "httpRequestsAdaptiveGroups(limit: 10000, filter: {datetime_geq: \"" + start + "\", "
        "datetime_leq: \"" + end + "\"}, orderBy: [datetimeFiveMinutes_ASC]) { "
        "count dimensions { " + dims + " } } } } }"
    )


def fetch_series(hours: int = 6) -> Dict:
    """Return the L7 DDoS timeseries for the last `hours`.

    Returns {"series": [(iso_ts, count), ...], "kind": "l7ddos"|"total",
             "total": [(iso_ts, count), ...]}.
    `series` is what we monitor for spikes; when securitySource grouping is
    available we use the l7ddos-attributed counts (matching the dashboard's
    mitigation-service=l7ddos view), otherwise we fall back to total requests.
    """
    zone_tag = resolve_zone_tag()
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    start = _iso(now - datetime.timedelta(hours=hours))
    end = _iso(now)

    rows = None
    with_source = True
    try:
        data = _graphql(_query(zone_tag, start, end, with_source=True))
        rows = data["viewer"]["zones"][0]["httpRequestsAdaptiveGroups"]
    except Exception as exc:
        log.warning("securitySource query failed (%s); retrying without it", exc)
        with_source = False
        data = _graphql(_query(zone_tag, start, end, with_source=False))
        rows = data["viewer"]["zones"][0]["httpRequestsAdaptiveGroups"]

    total: Dict[str, float] = {}
    l7ddos: Dict[str, float] = {}
    for row in rows:
        dims = row.get("dimensions", {})
        ts = dims.get("datetimeFiveMinutes")
        if not ts:
            continue
        cnt = float(row.get("count", 0) or 0)
        total[ts] = total.get(ts, 0.0) + cnt
        if with_source and dims.get("securitySource") in _MITIGATING_SOURCES:
            l7ddos[ts] = l7ddos.get(ts, 0.0) + cnt

    total_series = sorted(total.items())
    if with_source:
        # Ensure every bucket exists in the l7ddos series (0 when no mitigation).
        l7_series = [(ts, l7ddos.get(ts, 0.0)) for ts, _ in total_series]
        kind = "l7ddos"
        series = l7_series
    else:
        kind = "total"
        series = total_series

    return {"series": series, "kind": kind, "total": total_series}
