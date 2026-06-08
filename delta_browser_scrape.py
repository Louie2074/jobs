"""Standalone Delta browser scrape for the points-pilot-jobs GitHub Actions runner.

GitHub's Azure runner IPs clear Delta's Akamai edge block (HTTP 200) where Fly's IP gets 444,
so the nodriver browser scrape runs here. The packages under scrapers/ config/ db/ pipeline/
are vendored from points-pilot-scrapers (see VENDORED_DELTA.md) so this repo is self-contained —
no cross-repo checkout / PAT needed. (scrapers/browser.py + scrapers/delta.py are canonical here;
the rest are copies of the scraper repo's shared modules.)

One-shot: scrapes the most-popular Delta routes (both directions) over a near-term date window
via one warmed Chrome session, normalizes, and upserts into MotherDuck `flights`, then exits.
Suitable for a manual workflow_dispatch or a cron. Tunable via env: DELTA_SCRAPE_DAYS (default 5).
"""

import logging
import os
import sys
from datetime import date, timedelta


class _SuppressHashlibWarnings(logging.Filter):
    """Drop the blake2b/blake2s errors some Python builds' hashlib emits on import."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "blake2" not in record.getMessage()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logging.getLogger().handlers[0].addFilter(_SuppressHashlibWarnings())
logger = logging.getLogger("delta_browser_scrape")

# Most-popular Delta routes (ATL megahub + busiest transcons); both directions are scraped.
DELTA_ROUTES: list[tuple[str, str]] = [
    ("ATL", "LAX"),
    ("ATL", "MCO"),
    ("ATL", "LGA"),
    ("JFK", "LAX"),
    ("ATL", "SEA"),
    ("ATL", "DEN"),
    ("ATL", "FLL"),
    ("ATL", "BOS"),
    ("LAX", "SEA"),
    ("ATL", "DFW"),
]
SCRAPE_DAYS = int(os.getenv("DELTA_SCRAPE_DAYS", "5"))  # near-term window, scraped every day


def main() -> None:
    try:
        from config.settings import PriorityTier  # noqa: F401 — also triggers env validation
    except RuntimeError as exc:
        logger.critical("Configuration error: %s", exc)
        sys.exit(1)

    from db.connection import close_connection
    from db.queries import upsert_flights
    from db.schema import migrate
    from pipeline.normalizer import filter_valid, stamp_expiry
    from scrapers.base import ScraperBlockedError
    from scrapers.delta import DeltaScraper

    migrate()  # idempotent; ensures the flights table exists
    logger.info("Schema ready")

    pairs: list[tuple[str, str]] = []
    for origin, dest in DELTA_ROUTES:
        pairs.append((origin, dest))
        pairs.append((dest, origin))
    dates = [date.today() + timedelta(days=i) for i in range(SCRAPE_DAYS)]
    logger.info(
        "Scraping %d routes × %d dates (DELTA_SCRAPE_DAYS=%d)", len(pairs), len(dates), SCRAPE_DAYS
    )

    scraper = DeltaScraper()
    total = 0
    blocked = False
    try:
        for origin, dest in pairs:
            if blocked:
                break
            route_recs = 0
            for travel in dates:
                try:
                    recs = scraper.scrape(origin, dest, travel)
                except ScraperBlockedError as exc:
                    logger.warning("Akamai blocked (%s) — aborting run (rows so far persist)", exc)
                    blocked = True
                    break
                except Exception as exc:  # noqa: BLE001 — one route/date must not sink the run
                    logger.error("Error scraping %s→%s %s: %s", origin, dest, travel, exc)
                    continue
                stamped = stamp_expiry(filter_valid(recs), PriorityTier.MED)
                if stamped:
                    upsert_flights(stamped)
                    route_recs += len(stamped)
                    total += len(stamped)
            logger.info("%s→%s: %d records", origin, dest, route_recs)
    finally:
        scraper.close()
        close_connection()
    logger.info("=== done — %d Delta records upserted (blocked=%s) ===", total, blocked)


if __name__ == "__main__":
    main()
