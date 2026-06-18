"""jobs QueueManager: scored get_due_batch + adaptive mark_scraped (vendored)."""

import json
from datetime import datetime, timezone

import duckdb
import pytest

from config.settings import PriorityTier
from db import queries as db
from pipeline.queue_manager import QueueManager


@pytest.fixture(autouse=True)
def conn():
    import db.connection as db_conn

    c = duckdb.connect(":memory:")
    c.execute("SET TimeZone='UTC'")
    db_conn._local.conn = c
    from db import schema

    schema.migrate()
    yield c
    db_conn._local.conn = None
    c.close()


@pytest.fixture
def queue():
    return QueueManager(scraper=None)


def test_get_due_batch_orders_by_score(queue, conn):
    for o, d in [("AAA", "BBB"), ("CCC", "DDD")]:
        db.upsert_route(o, d, PriorityTier.MED, airline="delta")
    conn.execute("UPDATE routes_queue SET next_scrape_at_utc = now() - INTERVAL '1 hour'")
    conn.execute("UPDATE routes_queue SET decayed_demand = 50 WHERE origin='CCC'")
    batch = queue.get_due_batch(limit=10, airline="delta")
    assert batch[0].origin == "CCC"


def test_mark_scraped_persists_adaptive_state(queue, conn):
    db.upsert_route("ATL", "LAX", PriorityTier.MED, airline="delta")
    conn.execute("UPDATE routes_queue SET next_scrape_at_utc = now() - INTERVAL '1 hour'")
    job = queue.get_due_batch(limit=5, airline="delta")[0]
    now = datetime(2026, 6, 18, 12, tzinfo=timezone.utc)
    changed = queue.mark_scraped(job, {"economy": 9000}, now)
    assert changed is True  # no prior cheapest
    row = conn.execute(
        "SELECT interval_h, last_cheapest FROM routes_queue WHERE origin='ATL' AND dest='LAX'"
    ).fetchone()
    assert row[0] is not None
    assert json.loads(row[1]) == {"economy": 9000}
