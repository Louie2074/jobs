"""Unit tests for cleanup_flights.py — no network, no MotherDuck required.

prune_stale: deletes rows older than yesterday (UTC) from BOTH the `flights` and
`cash_fares` tables. Tested against an in-memory DuckDB seeded with stale + fresh
rows on relative dates, so the assertions never go stale with the calendar.
"""

from __future__ import annotations

import duckdb
import pytest

from cleanup_flights import prune_stale

CLEANUP_TABLES = ("flights", "cash_fares")


def _seed(conn: duckdb.DuckDBPyConnection) -> None:
    # Minimal tables — prune_stale only touches the `date` column. Per table:
    #   id 1 → 5 days ago   (stale, < yesterday → deleted)
    #   id 2 → yesterday    (kept)
    #   id 3 → today        (kept)
    #   id 4 → 5 days ahead (kept)
    for table in CLEANUP_TABLES:
        conn.execute(f"CREATE TABLE {table} (id INTEGER, date DATE)")
        conn.execute(f"INSERT INTO {table} VALUES (1, current_date - INTERVAL '5 days')")
        conn.execute(f"INSERT INTO {table} VALUES (2, current_date - INTERVAL '1 day')")
        conn.execute(f"INSERT INTO {table} VALUES (3, current_date)")
        conn.execute(f"INSERT INTO {table} VALUES (4, current_date + INTERVAL '5 days')")


@pytest.fixture
def conn() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(":memory:")
    c.execute("SET TimeZone='UTC'")
    _seed(c)
    yield c
    c.close()


def test_prune_stale_deletes_past_rows_from_both_tables(conn):
    deleted = prune_stale(conn)

    assert deleted == {"flights": 1, "cash_fares": 1}
    for table in CLEANUP_TABLES:
        ids = [r[0] for r in conn.execute(f"SELECT id FROM {table} ORDER BY id").fetchall()]
        assert ids == [2, 3, 4]  # only the stale row (id 1) is gone; yesterday is kept


def test_prune_stale_dry_run_counts_without_deleting(conn):
    would = prune_stale(conn, dry_run=True)

    assert would == {"flights": 1, "cash_fares": 1}
    for table in CLEANUP_TABLES:
        remaining = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        assert remaining == 4  # dry-run deletes nothing
