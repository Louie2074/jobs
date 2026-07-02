from datetime import date

import yaml

from delta_browser_scrape import _build_plan, _parse_dates_csv

_WF = ".github/workflows/delta-browser-scrape.yml"


class _StubScraper:
    """Stand-in for DeltaScraper carrying the dense/sparse knobs _run_cron reads off it."""

    dense_days = 14
    sparse_step = 4


def test_parse_dates_csv_valid():
    assert _parse_dates_csv("2026-06-20,2026-06-21") == [date(2026, 6, 20), date(2026, 6, 21)]


def test_delta_workflow_shard_matrix_is_consistent():
    with open(_WF) as f:
        wf = yaml.safe_load(f)
    job = wf["jobs"]["scrape"]
    shards = job["strategy"]["matrix"]["shard"]
    env = job["steps"][-1]["env"]
    n = int(env["DELTA_SHARDS"])
    assert shards == list(range(n)), f"matrix {shards} must be range(DELTA_SHARDS={n})"
    assert n >= 6, "Delta runs at least 6 fresh-IP shards"
    assert env["DELTA_SHARD_INDEX"] == "${{ matrix.shard }}"


def test_parse_dates_csv_drops_invalid_and_blanks():
    assert _parse_dates_csv("2026-06-20, ,nonsense,2026-06-22") == [
        date(2026, 6, 20),
        date(2026, 6, 22),
    ]


def test_parse_dates_csv_empty():
    assert _parse_dates_csv("") == []


def test_build_plan_single_route_with_dates():
    pairs, dates = _build_plan("atl", "lax", "2026-06-20,2026-06-21", 5, date(2026, 6, 8))
    # requested direction only (no reverse), exactly the supplied dates
    assert pairs == [("ATL", "LAX")]
    assert dates == [date(2026, 6, 20), date(2026, 6, 21)]


def test_build_plan_single_route_no_dates_falls_back_to_window():
    pairs, dates = _build_plan("ATL", "LAX", "", 3, date(2026, 6, 8))
    assert pairs == [("ATL", "LAX")]
    assert dates == [date(2026, 6, 8), date(2026, 6, 9), date(2026, 6, 10)]


def test_build_plan_single_route_only_shard0_works():
    # On-demand single-route dispatch spawns both matrix shards, but only shard 0 scrapes;
    # other shards no-op so the route isn't scraped twice.
    p0, d0 = _build_plan("ATL", "LAX", "", 5, date(2026, 6, 8), shard_index=0, shards=2)
    p1, d1 = _build_plan("ATL", "LAX", "", 5, date(2026, 6, 8), shard_index=1, shards=2)
    assert p0 == [("ATL", "LAX")] and d0
    assert p1 == [] and d1 == []


def test_delta_cron_uses_queue(monkeypatch):
    import delta_browser_scrape as ep

    calls = {}

    def fake_build_queue_plan(airline, **kw):
        calls.update(airline=airline, **kw)
        return (["JOB"], ["DATE"])

    monkeypatch.setattr(ep.common, "build_queue_plan", fake_build_queue_plan)
    monkeypatch.setattr(ep.common, "run_scrape", lambda *a, **k: 0)
    monkeypatch.setattr("scrapers.delta.DeltaScraper", _StubScraper)

    ep._run_cron(shard_index=0, shards=3)
    assert calls["airline"] == "delta"
    assert calls["max_legs"] == ep.MAX_LEGS_PER_SHARD
    assert calls["shard_index"] == 0
    assert calls["shards"] == 3


class _FixedDate:
    """date stand-in whose .today() is pinned; the entrypoint only calls date.today()."""

    def __init__(self, pinned):
        self._pinned = pinned

    def today(self):
        return self._pinned


def test_delta_cron_uses_dense_sparse_horizon(monkeypatch):
    """The cron path regenerates dates via dense_sparse over the 90d window (NOT
    build_queue_plan's every-day list): bounded count (<= the prior 30 every-day Delta dates),
    dense near-term, reaching ~90 days out."""
    import delta_browser_scrape as ep

    captured = {}
    monkeypatch.setattr(
        ep.common, "build_queue_plan", lambda *a, **k: (["JOB"], ["IGNORED_DATE"])
    )
    monkeypatch.setattr(
        ep.common, "run_scrape",
        lambda scraper, pairs, dates, **kw: captured.update(dates=dates) or 0,
    )
    monkeypatch.setattr("scrapers.delta.DeltaScraper", _StubScraper)

    today = date(2026, 6, 25)
    monkeypatch.setattr(ep, "date", _FixedDate(today))
    monkeypatch.setattr(ep, "SCRAPE_DAYS", 90)  # the workflow sets DELTA_SCRAPE_DAYS=90

    ep._run_cron(shard_index=0, shards=1)

    dates = captured["dates"]
    offsets = sorted((d - today).days for d in dates)
    assert dates != ["IGNORED_DATE"]  # regenerated, not the queue's flat every-day list
    assert offsets[:5] == [0, 1, 2, 3, 4]  # dense near-term, every day
    assert 85 <= offsets[-1] < 90  # reaches the ~90d horizon (exclusive upper bound)
    assert len(dates) <= 30  # bounded under the prior 30 every-day Delta dates
