"""Shared no-DB validation harness for the browser award scrapers (run on the Azure IP).

Each `<airline>_validate.py` is a thin entrypoint — a scraper factory + route list (+ optional
watchdog/sample tuning) → `run_validation()`. Mirrors `browser_scrape_common` for the scrape
jobs: the per-airline files stay tiny and the run plan lives here once.

`run_validation` warms the scraper, scrapes each route ONCE (no DB write), prints the records,
and exits non-zero if zero records come back — so a `workflow_dispatch` run is a green/red
end-to-end check before (or after) wiring the DB cron. Unbuffered prints + a hard `os._exit`
watchdog keep logs intact past any nodriver teardown hang.

The scraper import is deferred into the factory so it runs UNDER the watchdog — an import that
hangs (settings, nodriver) still gets killed with logs intact. The import loads settings only;
this never touches the DB.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Sequence
from datetime import date, timedelta


def _p(msg: str) -> None:
    sys.stdout.write(f">>> {msg}\n")
    sys.stdout.flush()


def _fmt(r: object) -> str:
    """One record → a compact log line. Tolerates fields a given scraper leaves unset
    (Turkish has no cash fare; Etihad has no seat count)."""
    seats = getattr(r, "available_seats", None)
    seats_s = "?" if seats in (None, -1) else str(seats)
    cash = getattr(r, "cash_cost", None)
    cash_s = f" ${cash}" if cash is not None else ""
    return (
        f"     {r.cabin_class:<9} {r.origin}->{r.destination} {r.points_cost:>7} pts "
        f"seats={seats_s}{cash_s} stops={r.stops} dep={r.departure_time_local} "
        f"{r.raw_flight_number}"
    )


def run_validation(
    *,
    label: str,
    scraper_factory: Callable[[], object],
    routes: Sequence[tuple[str, str]],
    watchdog_s: int = 300,
    days_ahead: int = 21,
    sample: int = 8,
) -> None:
    """Scrape each route once (no DB), print the records, `os._exit(1)` if zero total.

    `scraper_factory` should import + construct the scraper (deferred so the import runs
    under the watchdog). `watchdog_s` must be a touch below the workflow's `timeout -s KILL`
    so this fires first and its logs survive.
    """

    def _watchdog() -> None:
        time.sleep(watchdog_s)
        _p(f"WATCHDOG {watchdog_s}s — exiting (hung)")
        os._exit(3)

    threading.Thread(target=_watchdog, daemon=True).start()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )

    dt = date.today() + timedelta(days=days_ahead)
    _p(f"loading {label} scraper")
    sc = scraper_factory()
    total = 0
    try:
        for origin, dest in routes:
            _p(f"scrape {origin}-{dest} {dt}")
            try:
                recs = sc.scrape(origin, dest, dt)
            except Exception as exc:  # noqa: BLE001 — a route failure shouldn't abort the rest
                _p(f"  FAILED: {type(exc).__name__}: {exc}")
                continue
            _p(f"  -> {len(recs)} records")
            total += len(recs)
            for r in recs[:sample]:
                _p(_fmt(r))
    finally:
        _p("closing scraper")
        sc.close()

    _p(f"TOTAL records: {total}")
    if total == 0:
        _p("VALIDATION FAILED — 0 records")
        sys.stdout.flush()
        os._exit(1)
    _p("VALIDATION OK")
    sys.stdout.flush()
    # nodriver leaves keepalive tasks/threads on its loop that block a clean interpreter exit
    # (the run otherwise hangs on teardown); force the process down now that we have the result.
    os._exit(0)
