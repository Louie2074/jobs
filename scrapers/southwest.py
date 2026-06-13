"""
Southwest Airlines Rapid Rewards award availability scraper.

Runs on the BrowserScraper (nodriver/Chrome) transport: Southwest's shopping endpoint is gated by
an F5/Shape Security per-request JS sensor (rotating ee30zvqlwf-* headers). httpx replay is a dead
end (the token flaps 200->403 on reuse and won't transfer routes); the viable path is an in-page
fetch() inside a warmed southwest.com Chrome session, where Shape's JS auto-attaches a fresh sensor
token per request. Cleared the Azure/GitHub-Actions IP 3/3 (probe run 27480837436). Run by
`southwest_browser_scrape.py` on a daily GitHub Actions cron in this (points-pilot-jobs) repo.

No login (anonymous guest search). The request is a flat JSON POST; the response nests, under
data.searchResults.airProducts[].details[], one entry per itinerary, each carrying
fareProducts.ADULT.<FAMILY> price tiers (ALL economy — Southwest is single-cabin). Each fare
family's productId is a pipe-delimited, per-segment packed string encoding the full leg structure:

    <FAMILY>|<fareCode>,<bookingClass>,<orig>,<dest>,<departISO±off>,<arrISO±off>,<mkt>,<op>,<flightNum>,<aircraft>|...

Times carry their UTC offset inline, so no airport->timezone map is needed (unlike Delta). The
field mapping was validated against a real captured response
(tests/fixtures/southwest_SEA-LAX_2026-06-22.json -> 26 records).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from config.settings import TTL_HOURS, PriorityTier
from scrapers.base import FlightRecord
from scrapers.browser import BrowserScraper

logger = logging.getLogger(__name__)

_SHOP_URL = "https://www.southwest.com/api/air-booking/v1/air-booking/page/air/booking/shopping"
_API_KEY = "l7xx944d175ea25f4b9c903a583ea82a1c4c"

# Discounted fare families (Wanna Get Away / Wanna Get Away Plus) -> is_saver. The pricier
# Anytime (ANY*) and Business Select (BUS*) tiers are flexible, not saver.
_SAVER_PREFIXES = ("WGA", "PLU")


def _parse_dt(s: object) -> datetime | None:
    """Parse a Southwest productId ISO timestamp WITH its UTC offset (e.g.
    '2026-06-22T16:55-07:00') into a timezone-aware datetime. None on failure."""
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _parse_segments(product_id: object) -> list[dict]:
    """Decode a productId into an ordered list of segment dicts.

    Format: '<FAMILY>|<seg>|<seg>...' where each <seg> is a comma list:
      fareCode, bookingClass, orig, dest, departISO, arriveISO, mktCarrier, opCarrier, flightNum, aircraft
    Returns [] if the productId is missing or malformed.
    """
    if not isinstance(product_id, str) or "|" not in product_id:
        return []
    segs: list[dict] = []
    for chunk in product_id.split("|")[1:]:  # [0] is the fare family token — skip it
        f = chunk.split(",")
        if len(f) < 10:
            continue
        segs.append(
            {
                "fare_code": f[0],
                "booking_class": f[1],
                "origin": f[2],
                "dest": f[3],
                "depart": _parse_dt(f[4]),
                "arrive": _parse_dt(f[5]),
                "mkt_carrier": f[6],
                "op_carrier": f[7],
                "flight_num": f[8],
                "aircraft": f[9],
            }
        )
    return segs


def _cheapest_available(fare_products: object) -> tuple[str, dict] | None:
    """From a detail's fareProducts.ADULT map, return (family, fareProduct) for the cheapest
    family whose availabilityStatus is AVAILABLE and whose totalFare (POINTS) is > 0.
    None if nothing is bookable on this itinerary."""
    if not isinstance(fare_products, dict):
        return None
    best: tuple[str, dict] | None = None
    best_pts: int | None = None
    for family, fp in fare_products.items():
        if not isinstance(fp, dict) or fp.get("availabilityStatus") != "AVAILABLE":
            continue
        total = (fp.get("fare") or {}).get("totalFare") or {}
        try:
            pts = int(float(total.get("value")))
        except (TypeError, ValueError):
            continue
        if pts <= 0:
            continue
        if best_pts is None or pts < best_pts:
            best = (family, fp)
            best_pts = pts
    return best
