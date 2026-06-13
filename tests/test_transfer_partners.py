"""Unit tests for transfer_partners.py — no network, no MotherDuck required.

parse_partners: pure HTML → list[dict], tested with a fixture mirroring the
thriftytraveler.com structure (per-bank heading + table: Program | Type |
Transfer Ratio | Transfer Time).

reconcile: full-table snapshot-replace, tested with an in-memory DuckDB.
"""

from __future__ import annotations

import duckdb
import pytest

from transfer_partners import (
    _match_airline,
    _parse_ratio,
    parse_partners,
    reconcile,
)

# ---------------------------------------------------------------------------
# _match_airline — distinctive whole-word keyword match against the site's
# varied, per-bank "Program" cell text. Names below are verbatim from the live
# thriftytraveler.com page.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "program,code,program_name",
    [
        # Tracked airlines under suffixed / per-bank-varying display names that an
        # exact-string lookup dropped (the bug that motivated keyword matching).
        ("Alaska Airlines Mileage Plan", "AS", "Mileage Plan"),  # Bilt→Alaska — key case
        ("United MileagePlus", "UA", "MileagePlus"),
        ("British Airways Avios", "BA", "British Airways Avios"),
        ("British Airways Executive Club", "BA", "British Airways Avios"),
        ("Air France/KLM Flying Blue", "AF", "Flying Blue"),
        ("Aer Lingus AerClub", "EI", "Aer Lingus AerClub"),
        ("Aer Lingus Avios", "EI", "Aer Lingus AerClub"),
        ("Cathay Pacific AsiaMiles", "CX", "Asia Miles"),
        ("Iberia Avios", "IB", "Iberia Plus"),
        ("Turkish Miles & Smiles", "TK", "Miles&Smiles"),
        ("Southwest Rapid Rewards", "WN", "Rapid Rewards"),
        ("Virgin Atlantic Flying Club", "VS", "Virgin Atlantic"),
        ("Singapore", "SQ", "KrisFlyer"),  # bare, no "Air"/"Airlines" suffix
        ("ANA Mileage Club", "NH", "ANA Mileage Club"),  # whole-word "ana"
    ],
)
def test_match_airline_tracked_variants(program, code, program_name):
    assert _match_airline(program) == (code, program_name)


@pytest.mark.parametrize(
    "program",
    [
        "Emirates",            # EK — untracked
        "Emirates Skywards",
        "Qantas",              # QF — must NOT match \\bana\\b
        "AeroMexico",          # AM — must NOT match "aeroplan"
        "EVA Air",
        "Finnair",
        "Thai Airways",        # must NOT match "british airways"
        "TAP Air Portugal",
        "Spirit",
        "Japan Airlines (JAL)",  # JL — untracked
        "Virgin Red",          # NOT Virgin Atlantic — must stay unmatched
    ],
)
def test_match_airline_untracked_returns_none(program):
    assert _match_airline(program) is None


def test_match_airline_ana_does_not_grab_avianca():
    """Avianca resolves to AV, never NH (the \\bana\\b keyword must not over-match)."""
    assert _match_airline("Avianca LifeMiles") == ("AV", "LifeMiles")
    assert _match_airline("Avianca") == ("AV", "LifeMiles")

# ---------------------------------------------------------------------------
# _parse_ratio — site writes "bank : partner"; internal ratio = bank / partner
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1:1", 1.0),
        ("2:1.5", 1.33),       # Capital One → Emirates-style
        ("5:4", 1.25),         # Amex → Cathay-style
        ("1,000:800", 1.25),   # thousands separators
        ("1:1.6", 0.63),       # Amex → AeroMexico-style (bank cheaper than partner)
    ],
)
def test_parse_ratio_valid(raw, expected):
    assert _parse_ratio(raw) == expected


@pytest.mark.parametrize("raw", ["1:0", "0:1", "-1:1", "abc", "1", "100:1"])
def test_parse_ratio_rejected_returns_none(raw):
    # zero/negative/garbage/out-of-band (>10 or <0.1) → None (row dropped + warned)
    assert _parse_ratio(raw) is None


# ---------------------------------------------------------------------------
# HTML fixture — mirrors thriftytraveler.com: per-bank <h2> heading + <table>
# with columns Program | Type | Transfer Ratio | Transfer Time. Contains:
#   - Chase → Singapore Air (airline, 1:1) and World of Hyatt (HOTEL, skip)
#   - Amex → Cathay Pacific (airline, 5:4)
#   - Bilt → Alaska (airline, 1:1)
#   - Marriott section (must be ignored — not a managed bank)
#   - Rove section (must be ignored)
#   - Chase → Emirates (airline, 1:1) — EK not in TRACKED set, must be skipped
# ---------------------------------------------------------------------------
HTML_FIXTURE = """\
<html><body>
<h2>Chase Transfer Partners</h2>
<table>
<tr><th>Program</th><th>Type</th><th>Transfer Ratio</th><th>Transfer Time</th></tr>
<tr><td>Singapore Air</td><td>Airline</td><td>1:1</td><td>1-2 days</td></tr>
<tr><td>World of Hyatt</td><td>Hotel</td><td>1:1</td><td>Instant</td></tr>
<tr><td>Emirates</td><td>Airline</td><td>1:1</td><td>Instant</td></tr>
</table>
<h2>American Express Transfer Partners</h2>
<table>
<tr><th>Program</th><th>Type</th><th>Transfer Ratio</th><th>Transfer Time</th></tr>
<tr><td>Cathay Pacific</td><td>Airline</td><td>5:4</td><td>Instant</td></tr>
</table>
<h2>Bilt Transfer Partners</h2>
<table>
<tr><th>Program</th><th>Type</th><th>Transfer Ratio</th><th>Transfer Time</th></tr>
<tr><td>Alaska</td><td>Airline</td><td>1:1</td><td>Instant</td></tr>
</table>
<h2>Marriott Transfer Partners</h2>
<table>
<tr><th>Program</th><th>Type</th><th>Transfer Ratio</th><th>Transfer Time</th></tr>
<tr><td>Alaska</td><td>Airline</td><td>3:1</td><td>2 days</td></tr>
</table>
<h2>Rove Transfer Partners</h2>
<table>
<tr><th>Program</th><th>Type</th><th>Transfer Ratio</th><th>Transfer Time</th></tr>
<tr><td>United</td><td>Airline</td><td>1:1</td><td>Instant</td></tr>
</table>
</body></html>
"""


def _by_key(records):
    return {(r["bank_program_id"], r["airline_code"]): r for r in records}


def test_parse_maps_airline_rows_to_banks():
    """Chase→SQ, Amex→CX, Bilt→AS land under the right bank ids with right ratios."""
    records, _stats = parse_partners(HTML_FIXTURE)
    recs = _by_key(records)
    assert recs[(1, "SQ")]["transfer_ratio"] == 1.0
    assert recs[(1, "SQ")]["program_name"] == "KrisFlyer"
    assert recs[(2, "CX")]["transfer_ratio"] == 1.25  # 5:4
    assert recs[(5, "AS")]["transfer_ratio"] == 1.0
    # min/increment defaults attached
    assert recs[(5, "AS")]["min_transfer"] == 1000
    assert recs[(5, "AS")]["transfer_increment"] == 1000


def test_parse_skips_hotels_marriott_rove_and_untracked():
    """Hotels, the Marriott + Rove sections, and untracked airlines (Emirates) are dropped."""
    records, stats = parse_partners(HTML_FIXTURE)
    keys = {(r["bank_program_id"], r["airline_code"]) for r in records}
    # Exactly the three valid airline rows survive
    assert keys == {(1, "SQ"), (2, "CX"), (5, "AS")}
    # No bank id 6 (Marriott) and no airline 'EK'/'UA'-from-Rove leaked in
    assert all(r["bank_program_id"] != 6 for r in records)
    assert all(r["airline_code"] != "EK" for r in records)
    # Stats expose the debugging breakdown shipped in the metric.
    assert stats["banks_found"] == 3  # chase, amex, bilt (marriott/rove not managed)
    assert stats["banks_missing"] == 3  # citi, capital one, wells fargo absent in fixture
    assert stats["rows_skipped_hotel"] == 1  # World of Hyatt
    assert stats["rows_skipped_unmapped"] == 1  # Emirates (untracked)


def test_parse_no_managed_tables_raises():
    """A page with no managed bank tables → ValueError (structure changed)."""
    with pytest.raises(ValueError, match="no managed bank tables"):
        parse_partners("<html><body><p>nothing here</p></body></html>")


@pytest.fixture()
def mem_conn():
    """In-memory DuckDB with the transfer_partners schema + a stale Marriott row."""
    conn = duckdb.connect(":memory:")
    conn.execute("SET TimeZone='UTC'")
    conn.execute("""
        CREATE TABLE transfer_partners (
            bank_program_id    SMALLINT     NOT NULL,
            airline_code       VARCHAR(10)  NOT NULL,
            program_name       VARCHAR      NOT NULL,
            transfer_ratio     DECIMAL(5,2) NOT NULL DEFAULT 1.00,
            min_transfer       INTEGER      NOT NULL DEFAULT 1000,
            transfer_increment INTEGER      NOT NULL DEFAULT 1000,
            PRIMARY KEY (bank_program_id, airline_code)
        )
    """)
    # Stale data the snapshot must fully replace — incl. a Marriott (id 6) row.
    conn.execute(
        "INSERT INTO transfer_partners VALUES (6, 'AS', 'Mileage Plan', 3.0, 3000, 3000)"
    )
    conn.execute(
        "INSERT INTO transfer_partners VALUES (1, 'UA', 'MileagePlus', 1.0, 1000, 1000)"
    )
    return conn


def _sample_records():
    return [
        {"bank_program_id": 1, "airline_code": "SQ", "program_name": "KrisFlyer",
         "transfer_ratio": 1.0, "min_transfer": 1000, "transfer_increment": 1000},
        {"bank_program_id": 2, "airline_code": "CX", "program_name": "Asia Miles",
         "transfer_ratio": 1.25, "min_transfer": 1000, "transfer_increment": 1000},
    ]


def test_reconcile_full_snapshot_drops_marriott(mem_conn):
    """All prior rows (incl. Marriott id 6) deleted; only the new records remain."""
    deleted, inserted = reconcile(mem_conn, _sample_records())
    assert deleted == 2
    assert inserted == 2
    rows = mem_conn.execute(
        "SELECT bank_program_id, airline_code FROM transfer_partners ORDER BY ALL"
    ).fetchall()
    assert rows == [(1, "SQ"), (2, "CX")]  # no id 6, no stale UA


def test_reconcile_dry_run_leaves_table_unchanged(mem_conn):
    """--dry-run: returns (0, 0), no writes; stale rows still present."""
    deleted, inserted = reconcile(mem_conn, _sample_records(), dry_run=True)
    assert (deleted, inserted) == (0, 0)
    assert mem_conn.execute("SELECT COUNT(*) FROM transfer_partners").fetchone()[0] == 2
