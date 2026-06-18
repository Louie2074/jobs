"""Validation for the Turkish Miles&Smiles scraper (no DB write) on the Azure IP.

Runs TurkishScraper (warm booking page -> in-page availability fetch, retrying PerimeterX 428
challenges) against US->IST and prints the records. Unbuffered prints + a hard os._exit watchdog
keep logs intact past any hang. Imports need MOTHERDUCK_TOKEN (settings gate) — set a dummy;
never touches the DB.
"""

import logging
import os
import sys
import threading
import time
from datetime import date, timedelta


def P(msg):
    sys.stdout.write(f">>> {msg}\n")
    sys.stdout.flush()


def _watchdog():
    time.sleep(280)
    P("WATCHDOG 280s — exiting (hung)")
    os._exit(3)


threading.Thread(target=_watchdog, daemon=True).start()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)

P("importing TurkishScraper")
from scrapers.turkish import TurkishScraper  # noqa: E402

ROUTES = [("SEA", "IST"), ("JFK", "IST")]


def main():
    dt = date.today() + timedelta(days=21)
    P("instantiating scraper")
    sc = TurkishScraper()
    total = 0
    try:
        for origin, dest in ROUTES:
            P(f"scrape {origin}-{dest} {dt}")
            try:
                recs = sc.scrape(origin, dest, dt)
            except Exception as exc:  # noqa: BLE001
                P(f"  FAILED: {type(exc).__name__}: {exc}")
                continue
            P(f"  -> {len(recs)} records")
            total += len(recs)
            for r in recs[:6]:
                P(
                    f"     {r.cabin_class:<9} {r.origin}->{r.destination} {r.points_cost:>6} pts "
                    f"seats={r.available_seats} stops={r.stops} dep={r.departure_time_local} {r.raw_flight_number}"  # noqa: E501
                )
    finally:
        P("closing scraper")
        sc.close()
    P(f"TOTAL records: {total}")
    if total == 0:
        P("VALIDATION FAILED — 0 records")
        sys.stdout.flush()
        os._exit(1)
    P("VALIDATION OK")
    sys.stdout.flush()
    # nodriver leaves keepalive tasks/threads on its loop that block a clean interpreter exit
    # (the run otherwise hangs on teardown); force the process down now that we have the result.
    os._exit(0)


if __name__ == "__main__":
    main()
