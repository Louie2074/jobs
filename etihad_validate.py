"""No-DB validation for the Etihad Guest scraper on the Azure IP.

Runs EtihadScraper (warm digital.etihad.com -> award deep-link -> in-page DOM extraction) against
US->AUH and prints the records, proving real per-cabin miles come back. Thin entrypoint over
`validate_common.run_validation` — see that module for the shared harness.
"""

from validate_common import run_validation

ROUTES = [("JFK", "AUH"), ("ORD", "AUH")]


def _make_scraper():
    # Deferred import: runs under run_validation's watchdog (imports settings; never hits the DB).
    from scrapers.etihad import EtihadScraper

    return EtihadScraper()


if __name__ == "__main__":
    # watchdog 300s < the workflow's `timeout -s KILL 360` so this fires first, logs intact.
    run_validation(
        label="Etihad", scraper_factory=_make_scraper, routes=ROUTES, watchdog_s=300, sample=8
    )
