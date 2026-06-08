"""
American Airlines AAdvantage award availability scraper.

PARKED — not wired into the scheduler or seeded in config/routes.py. AA's session-mint
step (`POST /booking/find-flights`) is Akamai-blocked for automation ("Access Denied"); it
needs the genji_engine browser + residential-proxy bypass before it can run (see
CHECKPOINT). The implementation below is kept so re-enabling is straightforward once the
session bootstrap is solved.

Hits AA's JSON itinerary API (``POST /booking/api/search/itinerary``) — the same endpoint
the aa.com React booking app calls. The request body is the *decoded* ``searchRequest``
object (JSON, NOT the form-encoded ``searchRequest=...&requestType=itinerary`` body the
full-page navigation uses). The response is structured JSON with a top-level ``slices``
array; each slice carries ``segments`` (with ``legs``) and a ``pricingDetail`` list (one
entry per cabin/product).

All HTTP resilience (priming, pacing, retry, 403/406 back-off, circuit breaker) is
inherited from HttpScraper; this class only builds the request and maps the response.

NOTE: AA sits behind Akamai Bot Manager. From a server IP the call returns HTTP 200 but
``{"error":"309", "slices":[]}`` (no browser-generated session sensor), so the field
mapping below was validated against AA's documented shape + a synthesized fixture, NOT a
real award response. Re-validate field names against a real capture before relying on it.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from config.settings import TTL_HOURS, PriorityTier
from scrapers.base import FlightRecord, HttpScraper

logger = logging.getLogger(__name__)

# AA's JSON itinerary search API (what the booking React app calls).
_API_URL = "https://www.aa.com/booking/api/search/itinerary"

# AA productType / cabin label → canonical cabin class.
CABIN_MAP: dict[str, str] = {
    "COACH": "economy",
    "MAIN": "economy",
    "MAIN_CABIN": "economy",
    "ECONOMY": "economy",
    "PREMIUM_ECONOMY": "premium_economy",
    "PREMIUMECONOMY": "premium_economy",
    "PREMIUM_COACH": "premium_economy",
    "BUSINESS": "business",
    "FIRST": "first",
}


def _parse_local(s: object) -> datetime | None:
    """Parse AA's local datetime string to a NAIVE local datetime. Returns None on failure.

    AA returns wall-clock airport-local times (e.g. ``2026-07-08T08:00:00.000``). These are
    LOCAL times, so we keep them naive — do NOT attach/convert a timezone.
    """
    if not isinstance(s, str) or not s:
        return None
    txt = s.strip()
    # Strip a trailing Z / explicit offset if present (still treat the clock time as local).
    if txt.endswith("Z"):
        txt = txt[:-1]
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    return dt.replace(tzinfo=None)


def _build_search_request(origin: str, dest: str, travel_date: date) -> dict:
    """Build the decoded ``searchRequest`` object AA's itinerary API expects (one-way award)."""
    return {
        "metadata": {"selectedProducts": [], "tripType": "OneWay", "udo": {}},
        "passengers": [{"type": "adult", "count": 1}],
        "requestHeader": {"clientId": "AAcom"},
        "slices": [
            {
                "allCarriers": True,
                "cabin": "",
                "connectionCity": None,
                "departureDate": travel_date.strftime("%Y-%m-%d"),
                "destination": dest.upper(),
                "destinationNearbyAirports": False,
                "maxStops": None,
                "origin": origin.upper(),
                "originNearbyAirports": False,
            }
        ],
        "tripOptions": {
            "corporateBooking": False,
            "fareType": "Lowest",
            "locale": "en_US",
            "pointOfSale": "",
            "searchType": "Award",
            "enableBenefits": True,
        },
        "loyaltyInfo": None,
        "version": "",
        "queryParams": {
            "sliceIndex": 0,
            "sessionId": "",
            "solutionSet": "",
            "solutionId": "",
            "sort": "CARRIER",
        },
    }


class AmericanScraper(HttpScraper):
    """
    Scraper for American Airlines AAdvantage award availability.

    Builds the decoded ``searchRequest`` JSON body and POSTs it to AA's itinerary API,
    then maps the ``slices``/``pricingDetail`` response into FlightRecords (one per
    available cabin product per slice).

    Usage:
        scraper = AmericanScraper()
        records = scraper.scrape("SEA", "JFK", date(2026, 7, 8))
    """

    airline_code = "AA"
    program_name = "AAdvantage"
    source = "american"

    # Exact headers AA's JSON API expects. The httpx client manages cookies; do NOT hardcode
    # the giant Cookie string.
    base_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",  # br needs the `brotli` package to decode
        "Content-Type": "application/json",
        "sec-ch-ua": '"Chromium";v="136", "Google Chrome";v="136", "Not-A.Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "priority": "u=1, i",
        "Origin": "https://www.aa.com",
        "Referer": "https://www.aa.com/booking/choose-flights/1",
    }
    # AA's Akamai Bot Manager wants a homepage hit first to seed clearance cookies.
    prime_url = "https://www.aa.com/"

    # Extra-conservative while we validate the cURL + parsing; ramp up once proven.
    min_delay_s = 12.0
    block_threshold = 4
    refresh_interval_min = 120
    scrape_days_ahead = 21
    dense_days = 10
    sparse_step = 4

    def fetch_raw(self, origin: str, dest: str, travel_date: date) -> dict:
        """
        POST AA's itinerary API with the decoded searchRequest body and return parsed JSON.

        Resilience (priming, pacing, retry, 403/406 back-off, circuit breaker) lives in
        HttpScraper._request. Returns {} on a non-blocking skip (404) or an empty/non-JSON
        body. Raises ScraperBlockedError after repeated blocks; raises 429/5xx for tenacity.
        """
        body = _build_search_request(origin, dest, travel_date)
        response = self._request("POST", _API_URL, json=body)
        if response is None:
            return {}

        try:
            data = response.json()
        except ValueError:
            logger.warning("[AA] Non-JSON response for %s→%s %s", origin, dest, travel_date)
            return {}

        if not isinstance(data, dict) or not data.get("slices"):
            return {}

        return data

    def normalize(self, raw: dict, origin: str, dest: str, travel_date: date) -> list[FlightRecord]:
        """
        Map AA itinerary JSON → list[FlightRecord].

        Each slice is one itinerary; its ``pricingDetail`` lists one entry per cabin product.
        We emit one FlightRecord per available product (award points > 0).
        """
        if not raw:
            return []

        slices = raw.get("slices")
        if not isinstance(slices, list) or not slices:
            return []

        now = datetime.now(timezone.utc)
        ttl_h = TTL_HOURS[PriorityTier.MED]
        expires_at = now + timedelta(hours=ttl_h)
        records: list[FlightRecord] = []

        for sl in slices:
            try:
                if not isinstance(sl, dict):
                    continue

                segments = sl.get("segments")
                segments = segments if isinstance(segments, list) else []

                # Flatten legs across segments (a connection has multiple segments/legs).
                legs: list[dict] = []
                flight_numbers: list[str] = []
                for seg in segments:
                    if not isinstance(seg, dict):
                        continue
                    flight = seg.get("flight")
                    if isinstance(flight, dict):
                        carrier = flight.get("carrierCode")
                        num = flight.get("flightNumber")
                        if carrier and num:
                            flight_numbers.append(f"{carrier} {num}")
                    seg_legs = seg.get("legs")
                    if isinstance(seg_legs, list):
                        legs.extend(leg for leg in seg_legs if isinstance(leg, dict))

                # --- Stops ---
                stops_raw = sl.get("stops")
                if isinstance(stops_raw, int):
                    stops = max(0, stops_raw)
                else:
                    # Fall back: stops = (#legs - 1), clamped at 0.
                    stops = max(0, len(legs) - 1)

                # --- Flight number(s) ---
                raw_fn = "+".join(flight_numbers) if flight_numbers else "UNKNOWN"

                # --- Departure / arrival local times (prefer slice-level, fall back to legs) ---
                dep_time = _parse_local(sl.get("departureDateTime"))
                if dep_time is None and legs:
                    dep_time = _parse_local(legs[0].get("departureDateTime"))
                arr_time = _parse_local(sl.get("arrivalDateTime"))
                if arr_time is None and legs:
                    arr_time = _parse_local(legs[-1].get("arrivalDateTime"))

                # --- Aircraft (first leg) ---
                aircraft_str: str | None = None
                if legs:
                    ac = legs[0].get("aircraft")
                    if isinstance(ac, dict):
                        code = ac.get("code") or ac.get("name")
                        if isinstance(code, str) and code:
                            aircraft_str = code[:10]
                    elif isinstance(ac, str) and ac:
                        aircraft_str = ac[:10]

                # --- Duration ---
                duration_mins: int | None = None
                raw_dur = sl.get("durationInMinutes")
                if raw_dur is None:
                    raw_dur = sl.get("duration")
                if isinstance(raw_dur, (int, float)) and raw_dur > 0:
                    duration_mins = int(raw_dur)
                elif dep_time and arr_time:
                    duration_mins = int((arr_time - dep_time).total_seconds() / 60)

                # --- Layovers (intermediate leg arrival airports + gap durations) ---
                layover_iatas: list[str] = []
                for leg in legs[:-1]:  # all except the final leg
                    arr_ap = leg.get("destinationAirport") or leg.get("destination")
                    if isinstance(arr_ap, dict):
                        arr_ap = arr_ap.get("code")
                    if isinstance(arr_ap, str) and len(arr_ap) == 3:
                        layover_iatas.append(arr_ap.upper())

                layover_dur_mins: int | None = None
                if len(legs) > 1:
                    total_layover = 0
                    has_times = False
                    for i in range(len(legs) - 1):
                        prev_arr = _parse_local(legs[i].get("arrivalDateTime"))
                        next_dep = _parse_local(legs[i + 1].get("departureDateTime"))
                        if prev_arr and next_dep:
                            gap = int((next_dep - prev_arr).total_seconds() / 60)
                            if gap > 0:
                                total_layover += gap
                                has_times = True
                    if has_times:
                        layover_dur_mins = total_layover

                layover_airports_str = ",".join(layover_iatas) if layover_iatas else None
                next_day_arr = bool(dep_time and arr_time and arr_time.date() > dep_time.date())

                # --- One FlightRecord per available cabin product ---
                pricing = sl.get("pricingDetail")
                pricing = pricing if isinstance(pricing, list) else []

                for prod in pricing:
                    if not isinstance(prod, dict):
                        continue

                    cabin_key = prod.get("productType") or prod.get("cabin")
                    cabin = CABIN_MAP.get(str(cabin_key).upper()) if cabin_key else None
                    if not cabin:
                        logger.debug("[AA] Unknown product/cabin %r — skipping", cabin_key)
                        continue

                    points = prod.get("perPassengerAwardPoints")
                    if points is None:
                        award = prod.get("award")
                        if isinstance(award, dict):
                            points = award.get("miles") or award.get("points")
                    if not isinstance(points, (int, float)) or points <= 0:
                        continue  # no award space for this product

                    fees_obj = prod.get("perPassengerTaxesAndFees")
                    fees: float = 0.0
                    if isinstance(fees_obj, dict):
                        amt = fees_obj.get("amount")
                        if isinstance(amt, (int, float)):
                            fees = float(amt)
                    elif isinstance(fees_obj, (int, float)):
                        fees = float(fees_obj)

                    seats_raw = prod.get("seatsRemaining")
                    if seats_raw is None:
                        seats_raw = prod.get("availableSeats")
                    seats = int(seats_raw) if isinstance(seats_raw, int) else -1

                    fc_raw = prod.get("extendedFareCode") or prod.get("fareCode")
                    fare_class = str(fc_raw)[:10] if isinstance(fc_raw, str) and fc_raw else None

                    is_saver = bool(prod.get("isSaver", False))

                    mixed_cabin = bool(prod.get("mixedCabin", False))

                    try:
                        record = FlightRecord(
                            origin=origin.upper(),
                            destination=dest.upper(),
                            date=travel_date,
                            airline=self.airline_code,
                            program=self.program_name,
                            source=self.source,
                            points_cost=int(points),
                            cash_cost=fees,
                            cabin_class=cabin,
                            stops=stops,
                            available_seats=seats,
                            scraped_at_utc=now,
                            expires_at_utc=expires_at,
                            raw_flight_number=raw_fn,
                            partner_airline=None,
                            departure_time_local=dep_time,
                            arrival_time_local=arr_time,
                            duration_minutes=duration_mins,
                            aircraft_type=aircraft_str,
                            is_saver=is_saver,
                            fare_class=fare_class,
                            layover_airports=layover_airports_str,
                            layover_duration_minutes=layover_dur_mins,
                            next_day_arrival=next_day_arr,
                            mixed_cabin=mixed_cabin,
                        )
                        records.append(record)
                    except (ValueError, TypeError) as exc:
                        logger.warning("[AA] Skipping invalid record: %s", exc)
                        continue

            except Exception as exc:  # noqa: BLE001 — one bad slice must not kill the batch
                logger.warning("[AA] Error processing slice: %s", exc, exc_info=True)
                continue

        return records
