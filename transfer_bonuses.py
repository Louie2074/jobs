#!/usr/bin/env python3
"""
transfer_bonuses — scrape travel-on-points.com for current transfer bonuses.

Snapshot-replaces the `transfer_bonuses` table in MotherDuck for all airlines
tracked in `transfer_partners`. Runs on GitHub Actions cron (twice monthly) or
on-demand via workflow_dispatch.

Fail-closed: HTTP non-2xx or parse error → raises → non-zero exit → workflow
failure notification. Zero bonuses is valid — deletes all tracked bonuses and
inserts nothing (no active bonuses right now).

Requires MOTHERDUCK_TOKEN. BETTERSTACK_SOURCE_TOKEN enables metrics/log shipping.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import time
from datetime import date, datetime

import duckdb
import httpx
from bs4 import BeautifulSoup

from obs import flush, install_log_shipping, ship_metric

logger = logging.getLogger("transfer_bonuses")

SOURCE_URL = "https://travel-on-points.com/current-point-transfer-bonuses/"

# Site's "Point Program" cell text → bank_programs.id in MotherDuck.
# Keys are lowercased for case-insensitive lookup.
BANK_MAP: dict[str, int] = {
    "american express": 2,
    "amex": 2,
    "chase": 1,
    "chase ultimate rewards": 1,
    "capital one": 3,
    "capital one miles": 3,
    "citi": 4,
    "citi thankyou": 4,
    "citi thankyou points": 4,
    "bilt": 5,
    "bilt rewards": 5,
    "marriott bonvoy": 6,
    "marriott": 6,
    "wells fargo": 7,
    "wells fargo rewards": 7,
}

# Site's "Airline / Hotel Program" cell text → transfer_partners.airline_code.
# Hotel programs (Marriott Bonvoy as destination, Wyndham, etc.) are absent
# from this map — rows that don't match are silently skipped.
AIRLINE_MAP: dict[str, str] = {
    "air canada aeroplan": "AC",
    "aeroplan": "AC",
    "air france/klm flying blue": "AF",
    "air france": "AF",
    "flying blue": "AF",
    "alaska airlines": "AS",
    "alaska": "AS",
    "mileage plan": "AS",
    "american airlines": "AA",
    "avianca lifemiles": "AV",
    "lifemiles": "AV",
    "jetblue": "B6",
    "jetblue trueblue": "B6",
    "trueblue": "B6",
    "british airways": "BA",
    "british airways executive club": "BA",
    "cathay pacific asia miles": "CX",
    "cathay pacific": "CX",
    "asia miles": "CX",
    "delta skymiles": "DL",
    "delta": "DL",
    "aer lingus": "EI",
    "etihad": "EY",
    "hawaiian": "HA",
    "iberia": "IB",
    "ana": "NH",
    "all nippon airways": "NH",
    "qatar airways": "QR",
    "singapore airlines krisflyer": "SQ",
    "singapore airlines": "SQ",
    "krisflyer": "SQ",
    "turkish airlines miles&smiles": "TK",
    "turkish airlines": "TK",
    "united mileageplus": "UA",
    "united": "UA",
    "mileageplus": "UA",
    "virgin atlantic flying club": "VS",
    "virgin atlantic": "VS",
    "southwest rapid rewards": "WN",
    "southwest": "WN",
}


def parse_bonuses(html: str, today: date | None = None) -> list[dict]:
    """Parse the first <table> on the page into a list of bonus records.

    Each record is a dict with keys:
        bank_program_id (int), airline_code (str), bonus_pct (int),
        starts_at (date), ends_at (date), notes (str | None)

    Rows whose bank or airline destination is not in the respective map are
    silently skipped (hotel programs, unknown bank programs). Raises ValueError
    if no <table> is found — the page structure changed.
    """
    if today is None:
        today = date.today()

    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        raise ValueError("No <table> found on the page — page structure may have changed")

    records: list[dict] = []
    rows = table.find_all("tr")
    for row in rows[1:]:  # rows[0] is the header
        cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
        if len(cells) < 4:
            continue
        bank_raw, bonus_raw, airline_raw, end_date_raw = (
            cells[0], cells[1], cells[2], cells[3]
        )

        # Bank lookup
        bank_id = BANK_MAP.get(bank_raw.lower().strip())
        if bank_id is None:
            logger.debug("Skipping unknown bank %r", bank_raw)
            continue

        # Airline lookup — strip trailing asterisks/footnote markers first
        airline_clean = re.sub(r"[*†‡§]+$", "", airline_raw).strip()
        airline_code = AIRLINE_MAP.get(airline_clean.lower())
        if airline_code is None:
            logger.debug("Skipping non-airline destination %r", airline_raw)
            continue

        # Bonus pct — "25%" → 25
        try:
            bonus_pct = int(bonus_raw.strip().rstrip("%"))
        except ValueError:
            logger.warning("Unexpected bonus_rate %r — skipping row", bonus_raw)
            continue

        # End date — "6/30/26" → date(2026, 6, 30)
        try:
            ends_at = datetime.strptime(end_date_raw.strip(), "%m/%d/%y").date()
        except ValueError:
            logger.warning("Unexpected end_date %r — skipping row", end_date_raw)
            continue

        # Store original cell text in notes if it was altered (e.g. trailing *)
        notes: str | None = airline_raw if airline_raw != airline_clean else None

        records.append({
            "bank_program_id": bank_id,
            "airline_code": airline_code,
            "bonus_pct": bonus_pct,
            "starts_at": today,
            "ends_at": ends_at,
            "notes": notes,
        })

    return records


def reconcile(
    conn: duckdb.DuckDBPyConnection,
    records: list[dict],
    dry_run: bool = False,
) -> tuple[int, int]:
    """Snapshot-replace transfer_bonuses for all airlines tracked in transfer_partners.

    Deletes every row whose airline_code appears in transfer_partners, then
    inserts the freshly-scraped records. Returns (rows_deleted, rows_inserted).

    In dry_run mode: no DELETE/INSERT — returns (0, 0) and logs what would happen.
    """
    if dry_run:
        count = conn.execute(
            "SELECT COUNT(*) FROM transfer_bonuses "
            "WHERE airline_code IN (SELECT DISTINCT airline_code FROM transfer_partners)"
        ).fetchone()[0]
        logger.info(
            "[dry-run] Would delete %d row(s) and insert %d row(s).",
            count, len(records),
        )
        return 0, 0

    deleted = conn.execute(
        "DELETE FROM transfer_bonuses "
        "WHERE airline_code IN (SELECT DISTINCT airline_code FROM transfer_partners)"
    ).fetchone()[0]

    inserted = 0
    if records:
        conn.executemany(
            """
            INSERT INTO transfer_bonuses
                (bank_program_id, airline_code, bonus_pct, starts_at, ends_at, notes,
                 created_at_utc, updated_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, now(), now())
            """,
            [
                (
                    r["bank_program_id"], r["airline_code"], r["bonus_pct"],
                    r["starts_at"], r["ends_at"], r["notes"],
                )
                for r in records
            ],
        )
        inserted = len(records)

    logger.info("Deleted %d row(s), inserted %d row(s).", deleted, inserted)
    return deleted, inserted
