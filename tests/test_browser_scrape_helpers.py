"""Pure (no-DB) unit tests for browser_scrape_common helpers — _tier_for_job + dense_sparse_dates.

These are hermetic (no live pp schema needed), so they live in their own module rather than
test_browser_scrape_common.py, which module-level-skips when DATABASE_URL is unset.
"""

from datetime import date

import browser_scrape_common as common
from browser_scrape_common import _PairJob


class _StubRouteJob:
    def __init__(self, tier):
        self.origin, self.dest, self.tier = "SEA", "JFK", tier


def test_tier_for_job_uses_routejob_tier():
    assert common._tier_for_job(_StubRouteJob("HIGH"), "MED") == "HIGH"
    assert common._tier_for_job(_StubRouteJob("LOW"), "MED") == "LOW"


def test_tier_for_job_defaults_for_ondemand_pairjob():
    # On-demand _PairJobs have no .tier → fall back to the default.
    assert common._tier_for_job(_PairJob("SEA", "JFK"), "MED") == "MED"


def test_dense_sparse_dates_dense_then_sparse():
    # dense_days=3 → days 0,1,2 every day; then sparse_step=2 → 3,5,7,9 up to <max_day=10.
    out = common.dense_sparse_dates(date(2026, 7, 1), dense_days=3, sparse_step=2, max_day=10)
    assert out == [date(2026, 7, d) for d in (1, 2, 3, 4, 6, 8, 10)]


def test_dense_sparse_dates_no_sparse_when_dense_covers_window():
    out = common.dense_sparse_dates(date(2026, 7, 1), dense_days=5, sparse_step=3, max_day=5)
    assert out == [date(2026, 7, d) for d in (1, 2, 3, 4, 5)]


def test_dense_sparse_dates_coarse_tier_for_90d_horizon():
    # max_day=90 → dense 0-13 daily, sparse 14..29 step 3, coarse 30..89 step 7 (3-month horizon).
    out = common.dense_sparse_dates(date(2026, 7, 1), dense_days=14, sparse_step=3, max_day=90)
    offsets = [(d - date(2026, 7, 1)).days for d in out]
    assert offsets[:14] == list(range(14))  # dense daily
    assert 29 in offsets  # sparse tail up to the 30d boundary
    assert [o for o in offsets if o >= 30] == list(range(30, 90, 7))  # coarse: weekly to ~90
    assert 84 <= max(offsets) <= 89  # reaches ~3 months
    assert offsets == sorted(set(offsets))  # sorted, deduped


def test_dense_sparse_dates_no_coarse_below_boundary():
    # max_day=30 (browser airlines) stays dense+sparse only — no coarse dates generated.
    out = common.dense_sparse_dates(date(2026, 7, 1), dense_days=14, sparse_step=3, max_day=30)
    offsets = [(d - date(2026, 7, 1)).days for d in out]
    assert all(o < 30 for o in offsets)
