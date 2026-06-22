"""
JetBlue TrueBlue award availability scraper.

Uses JetBlue's public Azure-APIM-fronted search endpoint — no login required, but the
``ocp-apim-subscription-key`` header is mandatory (the endpoint returns 401 without it).
The httpx client manages cookies; we do not hardcode the browser Cookie string.

TrueBlue is revenue-based: the points price tracks the cash fare and effectively every seat
is bookable with points. So ``points_cost`` is the *points price of the fare* (from the
offer's FFCURRENCY price), not a fixed award-chart value. We emit one FlightRecord per
bookable fare option per itinerary (each offer = a cabin / branded fare).

Response shape (data.searchResults[].productOffers[]):
  - productOffer.originAndDestination[0]  → the itinerary (departure/arrival/stops/segments)
  - productOffer.offers[]                 → the bookable fares (brand, cabinClass, price, seats)

All HTTP resilience (pacing, retry, 403/406 back-off, circuit breaker) is inherited from
HttpScraper.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config.airport_tz import AIRPORT_TZ
from config.settings import TTL_HOURS, PriorityTier
from scrapers.base import FlightRecord, HttpScraper

logger = logging.getLogger(__name__)

_API_URL = "https://www.jetblue.com/api/ecom/cb-flight-search/v1/search/NGB"

# Azure APIM subscription key — REQUIRED on every request (public, baked into the web app).
_SUBSCRIPTION_KEY = "a5ee654e981b4577a58264fed9b1669c"

# JetBlue's per-fare cash price is expressed in this pseudo-currency = TrueBlue points.
_POINTS_CURRENCY = "FFCURRENCY"
_CASH_CURRENCY = "USD"

# JetBlue offer.cabinClass → canonical cabin class. Mint is sold as "Business".
# (Brand IDs AN/DN/EN/GN are economy bundles — Blue Basic/Blue/Blue Plus/Blue Extra —
# and MN is Mint; cabinClass already collapses them to Economy/Business, which we map here.)
CABIN_MAP: dict[str, str] = {
    "ECONOMY": "economy",
    "BUSINESS": "business",
    "PREMIUM": "premium_economy",
    "FIRST": "first",
}


def _parse_local(s: object, iata: str) -> datetime | None:
    """Parse JetBlue's local datetime string (e.g. '2026-07-02T06:00:00') as the wall-clock
    time at airport `iata`, returning a timezone-AWARE datetime.

    JetBlue reports naive local airport times. We attach the airport's IANA timezone so the
    stored value is a correct UTC instant (matching Alaska's tz-aware times) — otherwise a
    naive value lands in the TIMESTAMPTZ column as UTC, shifting every departure by the
    airport's offset (e.g. 4h at JFK). Returns None on failure or unknown airport.
    """
    if not isinstance(s, str):
        return None
    tz = AIRPORT_TZ.get(iata.upper())
    if tz is None:
        logger.warning("[B6] no timezone for %s — dropping time %r", iata, s)
        return None
    try:
        return datetime.fromisoformat(s).replace(tzinfo=ZoneInfo(tz))
    except ValueError:
        return None


def _price_amount(prices: object, currency: str) -> float | None:
    """Pull the amount for a given currency out of an offer's ``price`` list."""
    if not isinstance(prices, list):
        return None
    for entry in prices:
        if isinstance(entry, dict) and entry.get("currency") == currency:
            amount = entry.get("amount")
            if isinstance(amount, (int, float)):
                return float(amount)
    return None


class JetBlueScraper(HttpScraper):
    """
    Scraper for JetBlue TrueBlue award availability (revenue-based pricing).

    No authentication required beyond the static APIM subscription key. This class only
    builds the POST body and parses the JSON; all HTTP resilience is inherited from
    HttpScraper.

    Usage:
        scraper = JetBlueScraper()
        records = scraper.scrape("JFK", "LAX", date(2026, 7, 2))
    """

    airline_code = "B6"
    program_name = "TrueBlue"
    source = "jetblue"

    # Exact headers JetBlue's endpoint expects. The APIM key is mandatory.
    base_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "ocp-apim-subscription-key": _SUBSCRIPTION_KEY,
        "Origin": "https://www.jetblue.com",
        "Referer": "https://www.jetblue.com/booking/flights",
    }

    # Extra-conservative while we validate the cURL + parsing; ramp up once proven.
    min_delay_s = 12.0
    block_threshold = 4
    refresh_interval_min = 180
    scrape_days_ahead = 30
    dense_days = 10
    sparse_step = 4

    def fetch_raw(self, origin: str, dest: str, travel_date: date) -> dict:
        """
        POST JetBlue's award-search endpoint and return the parsed JSON.

        Resilience (pacing, retry, 403/406 back-off, circuit breaker) lives in
        HttpScraper._request. Returns {} on a non-blocking skip (404) or an empty body.
        """
        body = {
            "awardBooking": True,
            "travelerTypes": [{"type": "ADULT", "quantity": 1}],
            "searchComponents": [
                {
                    "from": origin.upper(),
                    "to": dest.upper(),
                    "date": travel_date.strftime("%Y-%m-%d"),
                }
            ],
        }

        response = self._request("POST", _API_URL, json=body)
        if response is None:
            return {}
        try:
            return response.json()
        except ValueError:
            logger.warning("[B6] Non-JSON response for %s→%s %s", origin, dest, travel_date)
            return {}

    def normalize(self, raw: dict, origin: str, dest: str, travel_date: date) -> list[FlightRecord]:
        """
        Map JetBlue's search response → list[FlightRecord].

        Walks data.searchResults[].productOffers[]; each productOffer pairs one itinerary
        (originAndDestination[0]) with its bookable fares (offers[]). One FlightRecord per
        fare with a points price.
        """
        if not raw:
            return []

        data = raw.get("data")
        if not isinstance(data, dict):
            return []
        search_results = data.get("searchResults")
        if not isinstance(search_results, list):
            return []

        now = datetime.now(timezone.utc)
        ttl_h = TTL_HOURS[PriorityTier.MED]
        expires_at = now + timedelta(hours=ttl_h)
        records: list[FlightRecord] = []

        for result in search_results:
            if not isinstance(result, dict):
                continue
            product_offers = result.get("productOffers")
            if not isinstance(product_offers, list):
                continue

            for product in product_offers:
                try:
                    records.extend(
                        self._records_for_product(
                            product, origin, dest, travel_date, now, expires_at
                        )
                    )
                except Exception as exc:  # noqa: BLE001 — never let one itinerary kill the run
                    logger.warning("[B6] Error processing productOffer: %s", exc, exc_info=True)
                    continue

        return records

    def _records_for_product(
        self,
        product: object,
        origin: str,
        dest: str,
        travel_date: date,
        now: datetime,
        expires_at: datetime,
    ) -> list[FlightRecord]:
        """Build the FlightRecords for one productOffer (one itinerary + its fares)."""
        if not isinstance(product, dict):
            return []

        ods = product.get("originAndDestination")
        if not isinstance(ods, list) or not ods:
            return []
        itin = ods[0]
        if not isinstance(itin, dict):
            return []

        segments = itin.get("flightSegments")
        if not isinstance(segments, list) or not segments:
            return []

        # --- Itinerary-level timing / stops ---
        dep_time = _parse_local((itin.get("departure") or {}).get("date"), origin)
        arr_time = _parse_local((itin.get("arrival") or {}).get("date"), dest)

        stops_raw = itin.get("stops")
        if isinstance(stops_raw, int) and stops_raw >= 0:
            stops = stops_raw
        else:
            stops = max(0, len(segments) - 1)

        dur_raw = itin.get("totalDuration")
        duration_mins: int | None = None
        if isinstance(dur_raw, (int, float)) and dur_raw > 0:
            duration_mins = int(dur_raw)
        elif dep_time and arr_time:
            duration_mins = int((arr_time - dep_time).total_seconds() / 60)

        # --- Flight number chain + aircraft + layovers from segments ---
        flight_nums: list[str] = []
        layover_iatas: list[str] = []
        layover_total = 0
        has_layover = False
        partner: str | None = None
        aircraft_str: str | None = None

        for i, seg in enumerate(segments):
            if not isinstance(seg, dict):
                continue
            info = seg.get("flightInfo") or {}
            carrier = info.get("marketingAirlineCode")
            number = info.get("marketingFlightNumber")
            if carrier and number is not None:
                flight_nums.append(f"{carrier} {number}")
            op = info.get("operatingAirlineCode")
            if op and op != self.airline_code and partner is None:
                partner = str(op)

            if aircraft_str is None:
                ac = seg.get("aircraftCode") or seg.get("aircraft")
                if isinstance(ac, str) and ac:
                    aircraft_str = ac[:10]

            # Layover follows every segment except the last; airport = this seg's arrival.
            if i < len(segments) - 1:
                arr_ap = (seg.get("arrival") or {}).get("airport")
                if isinstance(arr_ap, str) and len(arr_ap) == 3:
                    layover_iatas.append(arr_ap.upper())
                lay = seg.get("layoverDuration")
                if isinstance(lay, (int, float)) and lay > 0:
                    layover_total += int(lay)
                    has_layover = True

        raw_fn = "+".join(flight_nums) if flight_nums else "UNKNOWN"
        layover_airports_str = ",".join(layover_iatas) if layover_iatas else None
        layover_dur_mins = layover_total if has_layover else None
        next_day_arr = bool(arr_time and arr_time.date() > travel_date)

        # --- One FlightRecord per bookable fare ---
        offers = product.get("offers")
        if not isinstance(offers, list):
            return []

        records: list[FlightRecord] = []
        for offer in offers:
            if not isinstance(offer, dict):
                continue
            if offer.get("soldOut"):
                continue

            points = _price_amount(offer.get("price"), _POINTS_CURRENCY)
            if points is None or points <= 0:
                continue  # no points price → not bookable with miles
            cash = _price_amount(offer.get("price"), _CASH_CURRENCY)

            cabin_raw = offer.get("cabinClass")
            cabin = CABIN_MAP.get(str(cabin_raw).upper()) if cabin_raw else None
            if not cabin:
                logger.debug("[B6] Unknown cabinClass %r — skipping", cabin_raw)
                continue

            seats = -1
            seats_obj = offer.get("seatsRemaining")
            if isinstance(seats_obj, dict) and isinstance(seats_obj.get("count"), int):
                seats = seats_obj["count"]

            fare_class: str | None = None
            seg_info = offer.get("offerSegmentInfo")
            if isinstance(seg_info, list) and seg_info and isinstance(seg_info[0], dict):
                bc = seg_info[0].get("bookingClass")
                if isinstance(bc, str) and bc:
                    fare_class = bc[:10]

            brand = offer.get("brand") or {}
            is_saver = isinstance(brand, dict) and brand.get("brandId") == "AN"  # Blue Basic

            try:
                records.append(
                    FlightRecord(
                        origin=origin.upper(),
                        destination=dest.upper(),
                        date=travel_date,
                        airline=self.airline_code,
                        program=self.program_name,
                        source=self.source,
                        points_cost=int(points),
                        cash_cost=cash if cash is not None else 0.0,
                        cabin_class=cabin,
                        stops=stops,
                        available_seats=seats,
                        scraped_at_utc=now,
                        expires_at_utc=expires_at,
                        raw_flight_number=raw_fn,
                        partner_airline=partner,
                        departure_time_local=dep_time,
                        arrival_time_local=arr_time,
                        duration_minutes=duration_mins,
                        aircraft_type=aircraft_str,
                        is_saver=is_saver,
                        fare_class=fare_class,
                        layover_airports=layover_airports_str,
                        layover_duration_minutes=layover_dur_mins,
                        next_day_arrival=next_day_arr,
                        mixed_cabin=False,
                    )
                )
            except (ValueError, TypeError) as exc:
                logger.warning("[B6] Skipping invalid record: %s", exc)
                continue

        return records
