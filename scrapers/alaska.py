"""
Alaska Airlines Mileage Plan award availability scraper.

Uses Alaska's public SvelteKit search endpoint — no login or auth token required.
The endpoint returns newline-delimited JSON in SvelteKit's dehydrated format:
  line 0: base data chunk (airports, infrastructure)
  line 1: header/footer chunk
  line 2: i18n strings chunk
  line 3: flight results chunk  ← the one we care about

The dehydrated format stores all values in a flat array; the root object
contains integer indices that reference positions in that array.

Rate limiting: minimum 2s between requests (configurable).
Retry: 4 attempts with exponential backoff + jitter via tenacity.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone

from config.settings import TTL_HOURS, PriorityTier
from scrapers.base import FlightRecord, HttpScraper, ScraperBlockedError

logger = logging.getLogger(__name__)

_API_URL = "https://www.alaskaair.com/search/results/__data.json"

# Back-compat alias: the block exception + circuit breaker now live in scrapers.base. The
# scheduler, queue_manager, and the api service still import AlaskaBlockedError from here.
AlaskaBlockedError = ScraperBlockedError


# Alaska solution keys → canonical cabin class
# Keys follow the pattern <PRICING_TYPE>_<CABIN>
CABIN_MAP: dict[str, str] = {
    "REFUNDABLE_FIRST": "first",
    "SAVER_FIRST": "first",
    "FIRST": "first",
    "REFUNDABLE_PREMIUM": "premium_economy",
    "SAVER_PREMIUM": "premium_economy",
    "PREMIUM": "premium_economy",
    "REFUNDABLE_MAIN": "economy",
    "SAVER_MAIN": "economy",
    "MAIN": "economy",
    "SAVER": "economy",  # revenue search uses a bare SAVER key for cheapest main cabin
    "REFUNDABLE_BUSINESS": "business",
    "SAVER_BUSINESS": "business",
    "BUSINESS": "business",
}


def _parse_iso(s: object) -> datetime | None:
    """Parse an ISO 8601 string to a timezone-aware datetime. Returns None on failure."""
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _deref(val: object, flat: list, depth: int = 0) -> object:
    """
    Resolve a value from SvelteKit's dehydrated flat-array format.

    Rule: integers appearing as object/array values are INDEX references.
    Once we dereference an index and get a primitive (int, str, float,
    bool, None), that IS the final value — we do NOT follow it further.
    Only dicts and lists inside the flat array trigger another recursion.

    -1 is SvelteKit's sentinel for null/undefined.
    """
    if depth > 20:
        return val
    if isinstance(val, int) and val != -1 and 0 <= val < len(flat):
        actual = flat[val]
        if isinstance(actual, dict):
            return {k: _deref(v, flat, depth + 1) for k, v in actual.items()}
        if isinstance(actual, list):
            return [_deref(v, flat, depth + 1) for v in actual]
        # Primitive (int, str, float, bool, None) — return as-is
        return actual
    if isinstance(val, dict):
        return {k: _deref(v, flat, depth + 1) for k, v in val.items()}
    if isinstance(val, list):
        return [_deref(v, flat, depth + 1) for v in val]
    return val


class AlaskaScraper(HttpScraper):
    """
    Scraper for Alaska Airlines Mileage Plan award availability.

    No authentication required — Alaska's search page is public.
    Hits the SvelteKit __data.json endpoint used by the browser. All HTTP resilience
    (priming, pacing, retry, 403/406 back-off, circuit breaker) is inherited from
    HttpScraper; this class only builds the request and parses the dehydrated response.

    Usage:
        scraper = AlaskaScraper()
        records = scraper.scrape("SEA", "JFK", date(2026, 8, 1))
    """

    airline_code = "AS"
    program_name = "Mileage Plan"
    source = "alaska"

    # Alaska runs daily with a large route set; raise the per-tick batch so the
    # scheduler clears the queue each day. Worst-case run (28 × ~20 dates ×
    # 6s × 1.5 ≈ 84 min) still fits inside the 90-min stagger slot. See
    # docs/superpowers/specs/2026-06-14-scraper-route-expansion-design.md.
    max_routes_per_run = 28

    # Search mode: award sends ShoppingMethod=onlineaward; the cash subclass sets None to omit
    # it (revenue is the endpoint's default). Only this + the award-only OT/UPG/RequestType differ.
    shopping_method: str | None = "onlineaward"

    # Exact headers Alaska's endpoint expects (note the same-site Referer).
    base_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",  # br needs the `brotli` package to decode
        "sec-ch-ua": '"Chromium";v="136", "Google Chrome";v="136", "Not-A.Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "priority": "u=1, i",  # Chrome sends this on fetch/XHR
        "Referer": "https://www.alaskaair.com/",
    }
    # Alaska's WAF (Akamai _abck/bm_sz) wants a homepage hit first to seed clearance cookies.
    prime_url = "https://www.alaskaair.com/"

    @staticmethod
    def _flight_number_for_row(flight_data: list, row: dict) -> str:
        """First-segment flight number as "<carrier> <num>", else "UNKNOWN".

        Single source of truth for the flight identity, shared by the award and cash
        scrapers so cash_fares.flight_number matches flights.raw_flight_number exactly.
        """
        segs_idx = row.get("segments")
        segs_raw = flight_data[segs_idx] if isinstance(segs_idx, int) else []
        if not segs_raw:
            return "UNKNOWN"
        first_seg = flight_data[segs_raw[0]] if isinstance(segs_raw[0], int) else None
        if not isinstance(first_seg, dict):
            return "UNKNOWN"
        pub_idx = first_seg.get("publishingCarrier")
        if isinstance(pub_idx, int):
            pub = flight_data[pub_idx]
            if isinstance(pub, dict):
                code = _deref(pub.get("carrierCode"), flight_data)
                fnum = _deref(pub.get("flightNumber"), flight_data)
                if code and fnum:
                    return f"{code} {fnum}"
        return "UNKNOWN"

    def fetch_raw(self, origin: str, dest: str, travel_date: date) -> dict:
        """
        GET Alaska's __data.json endpoint and return parsed chunks.

        Resilience (priming, pacing, retry, 403/406 back-off, circuit breaker) lives in
        HttpScraper._request. Returns {} on a non-blocking skip (404) or an empty/non-JSON
        body. Raises ScraperBlockedError after repeated blocks; raises 429/5xx for tenacity.
        """
        if self.shopping_method:
            params = {
                "A": "1",
                "O": origin,
                "D": dest,
                "OD": travel_date.strftime("%Y-%m-%d"),
                "OT": "Anytime",
                "RT": "false",
                "UPG": "none",
                "ShoppingMethod": self.shopping_method,
                "RequestType": "Calendar",
                "locale": "en-us",
                "x-sveltekit-invalidated": "11",
            }
        else:
            # Revenue (cash) search — the real cash request omits ShoppingMethod/OT/UPG/RequestType.
            params = {
                "A": "1",
                "O": origin,
                "D": dest,
                "OD": travel_date.strftime("%Y-%m-%d"),
                "RT": "false",
                "locale": "en-us",
                "x-sveltekit-invalidated": "11",
            }

        response = self._request("GET", _API_URL, params=params)
        if response is None:
            return {}

        # Response is newline-delimited JSON (SvelteKit streaming format).
        chunks = []
        for line in response.text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                chunks.append(json.loads(line))
            except json.JSONDecodeError:
                logger.debug("[AS] Skipping non-JSON line (%d chars)", len(line))

        if not chunks:
            logger.warning("[AS] Empty response for %s→%s %s", origin, dest, travel_date)
            return {}

        return {"chunks": chunks}

    def normalize(self, raw: dict, origin: str, dest: str, travel_date: date) -> list[FlightRecord]:
        """
        Map Alaska __data.json response → list[FlightRecord].

        The flight results live in the streaming chunk whose root object
        contains a 'rows' key. Each row has a 'solutions' dict keyed by
        cabin type (e.g. REFUNDABLE_FIRST) and a 'segments' list.
        """
        if not raw:
            return []

        chunks = raw.get("chunks", [])
        if not chunks:
            return []

        # Find the flight data chunk: type='chunk', data[0] has 'rows' key
        flight_data: list | None = None
        for chunk in chunks:
            if chunk.get("type") != "chunk":
                continue
            data = chunk.get("data", [])
            if data and isinstance(data[0], dict) and "rows" in data[0]:
                flight_data = data
                break

        if flight_data is None:
            logger.warning("[AS] No flight chunk found for %s→%s %s", origin, dest, travel_date)
            return []

        root = flight_data[0]
        rows_idx = root.get("rows")
        if not isinstance(rows_idx, int):
            return []

        rows_arr = flight_data[rows_idx]
        if not isinstance(rows_arr, list) or not rows_arr:
            return []

        now = datetime.now(timezone.utc)
        ttl_h = TTL_HOURS[PriorityTier.MED]
        expires_at = now + timedelta(hours=ttl_h)
        records: list[FlightRecord] = []

        for row_idx in rows_arr:
            try:
                row = flight_data[row_idx]
                if not isinstance(row, dict):
                    continue

                # --- Stops + flight number from segments ---
                segs_idx = row.get("segments")
                segs_raw = flight_data[segs_idx] if isinstance(segs_idx, int) else []
                stops = max(0, len(segs_raw) - 1)

                raw_fn = self._flight_number_for_row(flight_data, row)
                partner: str | None = None

                # --- Timing, aircraft, layovers from segments ---
                dep_time: datetime | None = None
                arr_time: datetime | None = None
                duration_mins: int | None = None
                aircraft_str: str | None = None
                layover_iatas: list[str] = []
                layover_dur_mins: int | None = None

                if segs_raw:
                    first_seg = flight_data[segs_raw[0]] if isinstance(segs_raw[0], int) else None
                    if isinstance(first_seg, dict):
                        disp_idx = first_seg.get("displayCarrier")
                        if isinstance(disp_idx, int):
                            disp = flight_data[disp_idx]
                            if isinstance(disp, dict):
                                disp_code = _deref(disp.get("carrierCode"), flight_data)
                                if disp_code and str(disp_code) != "AS":
                                    partner = str(disp_code)

                        # Departure time from first segment
                        raw_dep = _deref(first_seg.get("departureDateTime"), flight_data)
                        if raw_dep is None:
                            raw_dep = _deref(first_seg.get("departureTime"), flight_data)
                        dep_time = _parse_iso(raw_dep)

                        # Aircraft type from first segment
                        raw_equip = _deref(first_seg.get("equipmentCode"), flight_data)
                        if raw_equip is None:
                            raw_equip = _deref(first_seg.get("equipment"), flight_data)
                        if raw_equip is None:
                            raw_equip = _deref(first_seg.get("aircraftType"), flight_data)
                        if isinstance(raw_equip, str):
                            aircraft_str = raw_equip[:10]

                    # Arrival time from last segment
                    last_seg = flight_data[segs_raw[-1]] if isinstance(segs_raw[-1], int) else None
                    if isinstance(last_seg, dict):
                        raw_arr = _deref(last_seg.get("arrivalDateTime"), flight_data)
                        if raw_arr is None:
                            raw_arr = _deref(last_seg.get("arrivalTime"), flight_data)
                        arr_time = _parse_iso(raw_arr)

                    # Layover airports + duration from intermediate segments
                    seg_arrivals: list[datetime | None] = []
                    seg_departures: list[datetime | None] = []
                    for seg_ref in segs_raw:
                        seg = flight_data[seg_ref] if isinstance(seg_ref, int) else None
                        if not isinstance(seg, dict):
                            seg_arrivals.append(None)
                            seg_departures.append(None)
                            continue

                        s_dep = _parse_iso(
                            _deref(
                                seg.get("departureDateTime") or seg.get("departureTime"),
                                flight_data,
                            )
                        )
                        s_arr = _parse_iso(
                            _deref(
                                seg.get("arrivalDateTime") or seg.get("arrivalTime"), flight_data
                            )
                        )
                        seg_departures.append(s_dep)
                        seg_arrivals.append(s_arr)

                    for seg_ref in segs_raw[:-1]:  # all except last
                        seg = flight_data[seg_ref] if isinstance(seg_ref, int) else None
                        if isinstance(seg, dict):
                            arr_ap = _deref(seg.get("arrivalAirportCode"), flight_data)
                            if arr_ap is None:
                                arr_ap = _deref(seg.get("arrivalStation"), flight_data)
                            if arr_ap is None:
                                arr_ap = _deref(seg.get("destinationCode"), flight_data)
                            if isinstance(arr_ap, str) and len(arr_ap) == 3:
                                layover_iatas.append(arr_ap.upper())

                    # Layover duration: sum of gaps between consecutive segments
                    if len(segs_raw) > 1:
                        total_layover = 0
                        has_times = False
                        for i in range(len(segs_raw) - 1):
                            prev_arr = seg_arrivals[i]
                            next_dep = seg_departures[i + 1]
                            if prev_arr and next_dep:
                                gap = int((next_dep - prev_arr).total_seconds() / 60)
                                if gap > 0:
                                    total_layover += gap
                                    has_times = True
                        if has_times:
                            layover_dur_mins = total_layover

                    # Total duration: prefer row-level field, fall back to dep→arr delta
                    raw_dur = _deref(row.get("totalDuration"), flight_data)
                    if raw_dur is None:
                        raw_dur = _deref(row.get("duration"), flight_data)
                    if isinstance(raw_dur, (int, float)) and raw_dur > 0:
                        duration_mins = int(raw_dur)
                    elif dep_time and arr_time:
                        duration_mins = int((arr_time - dep_time).total_seconds() / 60)

                next_day_arr = bool(arr_time and arr_time.date() > travel_date)
                layover_airports_str = ",".join(layover_iatas) if layover_iatas else None

                # --- One FlightRecord per available cabin solution ---
                sol_idx = row.get("solutions")
                if not isinstance(sol_idx, int):
                    continue
                sol_dict = flight_data[sol_idx]
                if not isinstance(sol_dict, dict):
                    continue

                for cabin_key, sol_val_idx in sol_dict.items():
                    if sol_val_idx == -1:
                        continue

                    cabin = CABIN_MAP.get(cabin_key.upper())
                    if not cabin:
                        logger.debug("[AS] Unknown cabin key %r — skipping", cabin_key)
                        continue

                    sol = flight_data[sol_val_idx] if isinstance(sol_val_idx, int) else None
                    if not isinstance(sol, dict):
                        continue

                    points = _deref(sol.get("atmosPoints"), flight_data)
                    fees = _deref(sol.get("grandTotal"), flight_data)
                    seats = _deref(sol.get("seatsRemaining"), flight_data)

                    if not isinstance(points, (int, float)) or points <= 0:
                        continue

                    is_saver = cabin_key.upper().startswith("SAVER")

                    raw_fc = _deref(sol.get("bookingCode"), flight_data)
                    if raw_fc is None:
                        raw_fc = _deref(sol.get("fareClass"), flight_data)
                    if raw_fc is None:
                        raw_fc = _deref(sol.get("bookingClass"), flight_data)
                    fare_class = str(raw_fc)[:10] if isinstance(raw_fc, str) and raw_fc else None

                    try:
                        record = FlightRecord(
                            origin=origin.upper(),
                            destination=dest.upper(),
                            date=travel_date,
                            airline=self.airline_code,
                            program=self.program_name,
                            source=self.source,
                            points_cost=int(points),
                            cash_cost=float(fees) if isinstance(fees, (int, float)) else 0.0,
                            cabin_class=cabin,
                            stops=stops,
                            available_seats=int(seats) if isinstance(seats, int) else -1,
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
                            mixed_cabin=False,  # Alaska solution keys cover the whole itinerary
                        )
                        records.append(record)
                    except (ValueError, TypeError) as exc:
                        logger.warning("[AS] Skipping invalid record: %s", exc)
                        continue

            except Exception as exc:
                logger.warning("[AS] Error processing row: %s", exc, exc_info=True)
                continue

        return records
