"""No-DB validation for the Turkish Miles&Smiles scraper on the Azure IP.

Runs TurkishScraper (warm booking page -> in-page availability fetch, retrying PerimeterX 428
challenges) against US->IST and prints the records. Thin entrypoint over
`validate_common.run_validation` — see that module for the shared harness.
"""

from validate_common import run_validation

ROUTES = [("SEA", "IST"), ("JFK", "IST")]


def _make_scraper():
    # Deferred import: runs under run_validation's watchdog (imports settings; never hits the DB).
    from scrapers.turkish import TurkishScraper

    return TurkishScraper()


if __name__ == "__main__":
    # watchdog 280s < the workflow's `timeout -s KILL 300` so this fires first, logs intact.
    run_validation(
        label="Turkish", scraper_factory=_make_scraper, routes=ROUTES, watchdog_s=280, sample=6
    )
