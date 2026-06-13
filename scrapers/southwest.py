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


class SouthwestScraper(BrowserScraper):
    """Scraper for Southwest Rapid Rewards award availability.

    No authentication (anonymous guest search). The browser transport (warm session, pacing,
    in-page fetch, block handling) is inherited from BrowserScraper; this class only builds the
    request and maps the response to FlightRecords.

    Usage:
        scraper = SouthwestScraper()
        records = scraper.scrape("SEA", "LAX", date(2026, 6, 22))
    """

    airline_code = "WN"
    program_name = "Rapid Rewards"
    source = "southwest"

    headless = False  # headful under xvfb on CI — Shape/Akamai score headless harshly

    # Conservative cadence (mirrors Delta). Only min_delay_s / block_threshold affect the cron
    # runner; the other tier attrs are inert unless Southwest is later added to the scheduler.
    min_delay_s = 12.0
    block_threshold = 4
    refresh_interval_min = 360
    scrape_days_ahead = 21
    dense_days = 10
    sparse_step = 4
    max_routes_per_run = 12

    def __init__(self) -> None:
        super().__init__()
        # Warm a REAL booking page (proven to arm Shape's fetch hook; the homepage was not
        # validated). The date is dynamic so it stays bookable; this warm search is ignored —
        # we only need Shape armed before our own in-page fetch().
        warm_date = (date.today() + timedelta(days=30)).strftime("%Y-%m-%d")
        self.warm_url = (
            "https://www.southwest.com/air/booking/select-depart.html"
            "?adultPassengersCount=1&adultsCount=1"
            f"&departureDate={warm_date}&departureTimeOfDay=ALL_DAY"
            "&destinationAirportCode=LAX&fareType=POINTS&int=HOMEQBOMAIR"
            "&originationAirportCode=SEA&passengerType=ADULT"
            "&returnDate=&returnTimeOfDay=ALL_DAY&to=LAX&tripType=oneway"
        )

    async def fetch_raw(self, origin: str, dest: str, travel_date: date) -> dict:
        """Build the flat shopping request and run it as an in-page fetch() in the warmed Chrome
        session. Returns the parsed JSON dict ({} on a benign/empty/blocked-soft body).
        ScraperBlockedError is raised by _page_fetch after repeated Shape/Akamai blocks."""
        body = _build_request_body(origin, dest, travel_date)
        # `accept` + `content-type` are injected by BrowserScraper._page_fetch; the Apigee
        # gateway headers go here. The Shape ee30zvqlwf-* sensor is added by Southwest's own JS.
        headers = {
            "x-api-key": _API_KEY,
            "x-app-id": "air-booking",
            "x-channel-id": "southwest",
            "x-user-experience-id": "0f836f7f-ddea-465c-b25c-6c4c79463507",
        }
        return await self._page_fetch(_SHOP_URL, body, headers)

    def normalize(self, raw: dict, origin: str, dest: str, travel_date: date) -> list[FlightRecord]:
        """Map Southwest's shopping response -> list[FlightRecord], one per bookable itinerary."""
        if not raw:
            return []
        search = (raw.get("data") or {}).get("searchResults") or {}
        air_products = search.get("airProducts") or []

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=TTL_HOURS[PriorityTier.MED])
        records: list[FlightRecord] = []
        for ap in air_products:
            if not isinstance(ap, dict):
                continue
            for det in ap.get("details") or []:
                if not isinstance(det, dict):
                    continue
                try:
                    rec = self._record_for_detail(det, origin, dest, travel_date, now, expires_at)
                except Exception as exc:  # noqa: BLE001 — one bad itinerary must not sink the run
                    logger.warning("[WN] error parsing itinerary: %s", exc, exc_info=True)
                    continue
                if rec is not None:
                    records.append(rec)
        return records

    def _record_for_detail(
        self,
        det: dict,
        origin: str,
        dest: str,
        travel_date: date,
        now: datetime,
        expires_at: datetime,
    ) -> FlightRecord | None:
        fare_products = (det.get("fareProducts") or {}).get("ADULT") or {}
        chosen = _cheapest_available(fare_products)
        if chosen is None:
            return None
        family, fp = chosen
        fare = fp.get("fare") or {}
        points = int(float((fare.get("totalFare") or {}).get("value")))
        taxes_val = (fare.get("totalTaxesAndFees") or {}).get("value")
        try:
            cash = float(taxes_val)
        except (TypeError, ValueError):
            cash = 0.0

        segs = _parse_segments(fp.get("productId"))
        if not segs:
            return None
        stops = len(segs) - 1
        flight_num = "+".join(f"WN {s['flight_num']}" for s in segs) or "UNKNOWN"
        dep = segs[0]["depart"]
        arr = segs[-1]["arrive"]
        aircraft = (segs[0]["aircraft"] or "")[:10] or None
        fare_class = (segs[0]["booking_class"] or "")[:10] or None

        # Layover airports = the dest of every segment except the last (the connecting points).
        layover_iatas = [s["dest"] for s in segs[:-1] if len(s.get("dest") or "") == 3]
        layover_airports = ",".join(layover_iatas) if layover_iatas else None
        layover_minutes: int | None = None
        if stops > 0:
            total = 0
            ok = False
            for i in range(len(segs) - 1):
                a, b = segs[i]["arrive"], segs[i + 1]["depart"]
                if a and b:
                    total += int((b - a).total_seconds() / 60)
                    ok = True
            layover_minutes = total if ok else None

        dur_raw = det.get("totalDuration")
        try:
            duration_mins = int(dur_raw) if dur_raw is not None else None
        except (TypeError, ValueError):
            duration_mins = None

        is_saver = any(family.upper().startswith(p) for p in _SAVER_PREFIXES)

        try:
            return FlightRecord(
                origin=origin.upper(),
                destination=dest.upper(),
                date=travel_date,
                airline=self.airline_code,
                program=self.program_name,
                source=self.source,
                points_cost=points,
                cash_cost=cash,
                cabin_class="economy",
                stops=stops,
                available_seats=-1,
                scraped_at_utc=now,
                expires_at_utc=expires_at,
                raw_flight_number=flight_num,
                partner_airline=None,
                departure_time_local=dep,
                arrival_time_local=arr,
                duration_minutes=duration_mins,
                aircraft_type=aircraft,
                is_saver=is_saver,
                fare_class=fare_class,
                layover_airports=layover_airports,
                layover_duration_minutes=layover_minutes,
                next_day_arrival=bool(det.get("nextDay")),
                mixed_cabin=False,
            )
        except (ValueError, TypeError) as exc:
            logger.warning("[WN] skipping invalid record: %s", exc)
            return None


def _build_request_body(origin: str, dest: str, travel_date: date) -> dict:
    """Build the flat JSON body for a one-way POINTS search, one adult."""
    o, d = origin.upper(), dest.upper()
    return {
        "adultPassengersCount": "1",
        "adultsCount": "1",
        "departureDate": travel_date.strftime("%Y-%m-%d"),
        "departureTimeOfDay": "ALL_DAY",
        "destinationAirportCode": d,
        "fareType": "POINTS",
        "int": "HOMEQBOMAIR",
        "originationAirportCode": o,
        "passengerType": "ADULT",
        "promoCode": "",
        "returnDate": "",
        "returnTimeOfDay": "ALL_DAY",
        "to": d,
        "tripType": "oneway",
        "application": "air-booking",
        "site": "southwest",
    }
