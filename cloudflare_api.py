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


def _query(zone_tag: str, start: str, end: str) -> str:
    return (
        "{ viewer { zones(filter: {zoneTag: \"" + zone_tag + "\"}) { "
        "httpRequestsAdaptiveGroups(limit: 10000, filter: {datetime_geq: \"" + start + "\", "
        "datetime_leq: \"" + end + "\"}, orderBy: [datetimeFiveMinutes_ASC]) { "
        "count dimensions { datetimeFiveMinutes securitySource } } } } }"
    )


def fetch_series(hours: int = 6) -> Dict:
    """Return the L7 DDoS mitigation timeseries for the last `hours`.

    Returns {"series": [(iso_ts, mitigated_count), ...], "kind": "l7ddos"}.

    We group by 5-minute bucket + securitySource and keep ONLY the l7ddos rows
    (matching the dashboard's mitigation-service=l7ddos view), 0-filling buckets
    with no mitigation. We never monitor total zone traffic — that would false-
    alarm on normal load. On any API error this raises, so the caller keeps its
    last-known series and skips the poll rather than alerting on bad data.
    """
    zone_tag = resolve_zone_tag()
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    start = _iso(now - datetime.timedelta(hours=hours))
    end = _iso(now)

    data = _graphql(_query(zone_tag, start, end))
    rows = data["viewer"]["zones"][0]["httpRequestsAdaptiveGroups"]

    l7ddos: Dict[str, float] = {}
    buckets: set[str] = set()
    for row in rows:
        dims = row.get("dimensions", {})
        ts = dims.get("datetimeFiveMinutes")
        if not ts:
            continue
        buckets.add(ts)
        if dims.get("securitySource") in _MITIGATING_SOURCES:
            l7ddos[ts] = l7ddos.get(ts, 0.0) + float(row.get("count", 0) or 0)

    series = [(ts, l7ddos.get(ts, 0.0)) for ts in sorted(buckets)]
    return {"series": series, "kind": "l7ddos"}
