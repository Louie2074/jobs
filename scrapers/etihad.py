"""Etihad Guest award availability scraper (DOM-based).

Runs on the BrowserScraper (nodriver/Chrome) transport. Etihad's award flow lives in the Amadeus
DXP SPA on ``digital.etihad.com`` behind Akamai **and** Imperva ABP — a plain httpx request, and
even a top-level CDP navigation from a "cold" residential session, draws the "Pardon Our
Interruption" ABP block. The GitHub Actions (Azure) IP clears the flow inside a warmed Chrome
session (proven 2026-06-16). Canonical home for the Etihad browser scraper; daily GH Actions cron
in this (points-pilot-jobs) repo, like Delta/Southwest/Turkish.

**Why DOM, not an API:** unlike Turkish, the availability JSON is NOT reachable as a page fetch.
Recon (2026-06-16) proved the per-search XHR/fetch is invisible to a page-context interceptor
(0 captures), invisible to the CDP Network layer even with the service worker bypassed (0 bodies),
and is not inlined in any SSR script blob — the pricing is rendered straight into the Angular
Material DOM. So we drive the public award deep-link
(``/book/search?...&TRIP_FLOW_TYPE=AVAILABILITY&FLOW=AWARD``, which redirects to the
``/book/cart-new/upsell`` fare-selection page) and extract the rendered result cards.

Each ``<ey-bound-card-new>`` card carries stable hooks: ``#departureTime``/``#arrivalTime`` and
``#departureLocation``/``#arrivalLocation`` (``title`` = IATA), ``.arrival-days-difference``
(``+1``), ``.total-duration``, ``.direct-flight`` (present ⇒ nonstop), ``.flight-number`` spans
(one per leg, ``EY 2`` / connection ``EY 8324`` ``EY 8``), and per-cabin
``<button data-testid="cabin:Economy|Business|First">`` whose
``.price-first-section .price[data-amount]`` is the RAW award miles and
``.remaining-nonconverted-miles .price[data-amount]`` is the cash in CENTS. One award search
returns every available cabin per card, so we emit one record per (card × priced cabin).

The whole extraction (in-page wait-for-render + read) runs inside a SINGLE tab.evaluate per scrape
— nodriver 0.50.3 trips "cannot call get() concurrently" across multiple CDP ops in one scrape.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config.airport_tz import AIRPORT_TZ
from config.settings import TTL_HOURS, PriorityTier
from scrapers.base import FlightRecord, ScraperBlockedError
from scrapers.browser import BrowserScraper

logger = logging.getLogger(__name__)

# data-testid cabin token (after "cabin:") → our canonical cabin_class.
_CABIN_MAP: dict[str, str] = {
    "economy": "economy",
    "premiumeconomy": "premium_economy",
    "premium economy": "premium_economy",
    "premium": "premium_economy",
    "business": "business",
    "first": "first",
}

_HHMM = re.compile(r"(\d{1,2}):(\d{2})")
_DUR = re.compile(r"(\d+)\s*h(?:\s*(\d+)\s*m)?", re.IGNORECASE)
_FN_CARRIER = re.compile(r"^([A-Z0-9]{2})\b")


def _parse_hhmm_on(day: date, s: object, iata: str) -> datetime | None:
    """"15:45" → tz-aware datetime at `iata` on `day`. None on bad input/unmapped airport
    (foreign endpoints are often unmapped — the time is simply dropped, not fatal)."""
    if not isinstance(s, str):
        return None
    m = _HHMM.search(s)
    tz = AIRPORT_TZ.get(iata.upper()) if iata else None
    if not m or tz is None:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 23 or mm > 59:
        return None
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=ZoneInfo(tz))


def _parse_duration_mins(s: object) -> int | None:
    """"12h 40m" → 760; "13h" → 780; None/garbage → None."""
    if not isinstance(s, str):
        return None
    m = _DUR.search(s)
    if not m:
        return None
    hours = int(m.group(1))
    mins = int(m.group(2)) if m.group(2) else 0
    total = hours * 60 + mins
    return total if total > 0 else None


def _day_offset(s: object) -> int:
    """"+1" → 1, "+2" → 2, "" / None → 0 (the arrival-days-difference badge)."""
    if not isinstance(s, str):
        return 0
    m = re.search(r"\d+", s)
    return int(m.group(0)) if m else 0


def _carrier(flight_number: str) -> str | None:
    m = _FN_CARRIER.match(flight_number.strip())
    return m.group(1) if m else None


class EtihadScraper(BrowserScraper):
    """Scraper for Etihad Guest award availability (no login; DOM-scraped via the award deep-link)."""  # noqa: E501

    airline_code = "EY"
    program_name = "Etihad Guest"
    source = "etihad"

    # Warm the Amadeus DXP host so Imperva/Akamai trust the session before the deep-link nav.
    # digital.etihad.com root returns a 404 page but still seeds the anti-bot cookies. Headful
    # under xvfb — the ABP scores headless harshly.
    warm_url = "https://digital.etihad.com/"
    headless = False
    nav_wait_s = 12.0  # dwell after warming so the Akamai/Imperva sensor settles

    # After navigating the deep-link the SPA redirects (/book/search → /book/cart-new/upsell) and
    # renders; wait this long for the redirect+initial paint, then a SINGLE in-page evaluate polls
    # up to _render_poll_s for the result cards (one CDP op per scrape — see module docstring).
    nav_settle_s = 18.0
    _render_poll_s = 20.0

    # Conservative cadence (mirrors Delta/Turkish): light window, gentle pacing.
    min_delay_s = 8.0
    block_threshold = 4
    refresh_interval_min = 360  # 6 hours
    scrape_days_ahead = 21
    dense_days = 10
    sparse_step = 4
    max_routes_per_run = 12

    def _ensure_loop(self):
        """Drive the browser on nodriver's OWN event loop (see TurkishScraper) — using a fresh
        new_event_loop() trips ``cannot call get() concurrently`` on multi-route runs."""
        import nodriver as uc

        if self._loop is None or self._loop.is_closed():
            self._loop = uc.loop()
            asyncio.set_event_loop(self._loop)
        return self._loop

    @staticmethod
    def _deeplink(origin: str, dest: str, travel_date: date) -> str:
        """The public award deep-link for one one-way search (params captured from the real
        'Fly with miles' widget redirect). CABIN=E is just the entry cabin — the results page
        renders every available cabin per flight."""
        d = travel_date.strftime("%Y%m%d") + "0000"
        return (
            "https://digital.etihad.com/book/search?LANGUAGE=EN&CHANNEL=DESKTOP"
            f"&B_LOCATION={origin.upper()}&E_LOCATION={dest.upper()}"
            "&TRIP_TYPE=O&CABIN=E&TRAVELERS=ADT&TRIP_FLOW_TYPE=AVAILABILITY"
            f"&SITE_EDITION=EN-US&DATE_1={d}&WDS_ENABLE_MILES_TOGGLE=TRUE&FLOW=AWARD"
        )

    def _extract_js(self) -> str:
        """One self-contained in-page async script: wait (up to _render_poll_s) for the result
        cards to render, then read each ``<ey-bound-card-new>`` into a plain object. Returns
        JSON: ``{"cards":[...]}``, ``{"blocked":true}`` (Imperva interruption), or ``{"cards":[]}``.
        ONE tab.evaluate per scrape — see module docstring."""
        return (
            "(async () => {"
            f"  const DEADLINE=Date.now()+{int(self._render_poll_s * 1000)};"
            "   const sleep=ms=>new Promise(r=>setTimeout(r,ms));"
            "   const num=s=>{const v=parseInt(String(s||'').replace(/[^0-9]/g,''),10);"
            "     return isNaN(v)?null:v;};"
            "   const tx=e=>e?(e.textContent||'').replace(/\\u00a0/g,' ').replace(/\\s+/g,' ').trim():null;"  # noqa: E501
            "   let cards=[];"
            "   while(Date.now()<DEADLINE){"
            "     if(/Pardon Our Interruption/i.test(document.documentElement.innerHTML))"
            "       return JSON.stringify({blocked:true});"
            "     cards=[...document.querySelectorAll('ey-bound-card-new')];"
            "     if(cards.length>0){await sleep(1200);"
            "       cards=[...document.querySelectorAll('ey-bound-card-new')];break;}"
            "     await sleep(2000);"
            "   }"
            "   const out=[];"
            "   for(const card of cards){"
            "     const q=s=>card.querySelector(s);"
            "     const depL=q('#departureLocation'), arrL=q('#arrivalLocation');"
            "     const fns=[...card.querySelectorAll('.flight-number')]"
            "       .map(e=>(e.textContent||'').replace(/\\u00a0/g,' ').replace(/\\s+/g,' ').trim())"  # noqa: E501
            "       .filter(Boolean);"
            "     const cabins=[];"
            "     for(const btn of card.querySelectorAll('[data-testid^=\"cabin:\"]')){"
            "       const cab=(btn.getAttribute('data-testid')||'').split(':')[1]||'';"
            "       const mp=btn.querySelector('.price-first-section .price[data-amount]');"
            "       const cp=btn.querySelector('.remaining-nonconverted-miles .price[data-amount]');"  # noqa: E501
            "       const miles=mp?num(mp.getAttribute('data-amount')):null;"
            "       const cashCents=cp?num(cp.getAttribute('data-amount')):null;"
            "       if(miles)cabins.push({cabin:cab,miles:miles,cashCents:cashCents});"
            "     }"
            "     out.push({depTime:tx(q('#departureTime')),arrTime:tx(q('#arrivalTime')),"
            "       origin:depL?depL.getAttribute('title'):null,"
            "       dest:arrL?arrL.getAttribute('title'):null,"
            "       dayDiff:tx(card.querySelector('.arrival-days-difference')),"
            "       duration:tx(card.querySelector('.total-duration')),"
            "       nonstop:!!card.querySelector('.direct-flight'),"
            "       flightNumbers:fns,cabins:cabins});"
            "   }"
            "   return JSON.stringify({cards:out});"
            "})()"
        )

    async def fetch_raw(self, origin: str, dest: str, travel_date: date) -> dict:
        """Navigate the per-search award deep-link in the warmed session, let the SPA redirect +
        render, then run ONE in-page DOM extraction. Returns the parsed dict ({} on transport
        failure / non-JSON); raises ScraperBlockedError when Imperva interrupts repeatedly."""
        tab = await self._ensure_browser()
        await asyncio.sleep(random.uniform(self.min_delay_s, self.min_delay_s * 2))  # pacing
        try:
            await tab.get(self._deeplink(origin, dest, travel_date))
        except Exception as exc:  # noqa: BLE001 — nav hiccup → soft empty this call
            logger.warning("[EY] nav failed %s→%s: %s", origin, dest, exc)
            return {}
        await tab.sleep(self.nav_settle_s)  # let /book/search → /book/cart-new/upsell settle
        out = await tab.evaluate(self._extract_js(), await_promise=True)
        if not isinstance(out, str):
            logger.warning("[EY] in-page evaluate returned non-str (JS error?): %r", out)
            return {}
        try:
            data = json.loads(out)
        except (ValueError, TypeError):
            return {}
        if not isinstance(data, dict):
            return {}
        if data.get("blocked"):
            self._consecutive_blocks += 1
            if self._consecutive_blocks >= self.block_threshold:
                raise ScraperBlockedError(
                    f"{self._consecutive_blocks} consecutive Imperva blocks from {self.source}"
                )
            return {}
        self._consecutive_blocks = 0
        return data

    def normalize(
        self, raw: dict, origin: str, dest: str, travel_date: date
    ) -> list[FlightRecord]:
        """Map the extracted cards → FlightRecords: one per (card × priced cabin)."""
        if not isinstance(raw, dict):
            return []
        cards = raw.get("cards")
        if not isinstance(cards, list):
            return []

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=TTL_HOURS[PriorityTier.MED])
        seen: dict[tuple[str, str], FlightRecord] = {}  # (flight_no, cabin) — dedup brand dups

        for card in cards:
            if not isinstance(card, dict):
                continue
            try:
                for rec in self._records_for_card(
                    card, origin, dest, travel_date, now, expires_at
                ):
                    seen.setdefault((rec.raw_flight_number, rec.cabin_class), rec)
            except Exception as exc:  # noqa: BLE001 — one bad card must not sink the run
                logger.warning("[EY] error on card: %s", exc, exc_info=True)
        return list(seen.values())

    def _records_for_card(
        self, card: dict, origin: str, dest: str, travel_date: date,
        now: datetime, expires_at: datetime,
    ) -> list[FlightRecord]:
        cabins = [c for c in (card.get("cabins") or []) if isinstance(c, dict)]
        if not cabins:
            return []

        fns = [str(n).strip() for n in (card.get("flightNumbers") or []) if str(n).strip()]
        raw_fn = "+".join(fns) if fns else "UNKNOWN"
        stops = max(0, len(fns) - 1) if fns else (0 if card.get("nonstop") else 1)

        day_off = _day_offset(card.get("dayDiff"))
        dep_time = _parse_hhmm_on(travel_date, card.get("depTime"), origin)
        arr_time = _parse_hhmm_on(travel_date + timedelta(days=day_off), card.get("arrTime"), dest)
        duration = _parse_duration_mins(card.get("duration"))

        carriers = {c for c in (_carrier(n) for n in fns) if c}
        partner = next((c for c in carriers if c and c != self.airline_code), None)

        records: list[FlightRecord] = []
        for cab in cabins:
            cabin = _CABIN_MAP.get(str(cab.get("cabin", "")).strip().lower())
            miles = cab.get("miles")
            if cabin is None or not isinstance(miles, int) or miles <= 0:
                continue
            cash_cents = cab.get("cashCents")
            cash = round(cash_cents / 100, 2) if isinstance(cash_cents, (int, float)) else 0.0
            try:
                records.append(
                    FlightRecord(
                        origin=origin.upper(),
                        destination=dest.upper(),
                        date=travel_date,
                        airline=self.airline_code,
                        program=self.program_name,
                        source=self.source,
                        points_cost=miles,
                        cash_cost=cash,  # taxes/fees (USD); from the +USD figure on the fare
                        cabin_class=cabin,
                        stops=stops,
                        available_seats=-1,  # not shown on the fare-selection card
                        scraped_at_utc=now,
                        expires_at_utc=expires_at,
                        raw_flight_number=raw_fn,
                        partner_airline=partner,
                        departure_time_local=dep_time,
                        arrival_time_local=arr_time,
                        duration_minutes=duration,
                        aircraft_type=None,
                        is_saver=False,
                        fare_class=None,
                        layover_airports=None,
                        layover_duration_minutes=None,
                        next_day_arrival=day_off > 0,
                        mixed_cabin=False,
                    )
                )
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "[EY] dropping invalid %s record %s→%s: %s", cabin, origin, dest, exc
                )
        return records
