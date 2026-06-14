from datetime import date

from delta_browser_scrape import DELTA_ROUTES, _build_plan, _parse_dates_csv


def test_parse_dates_csv_valid():
    assert _parse_dates_csv("2026-06-20,2026-06-21") == [date(2026, 6, 20), date(2026, 6, 21)]


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


def test_build_plan_cron_mode_both_directions():
    pairs, dates = _build_plan("", "", "", 2, date(2026, 6, 8))
    # cron mode: every popular route in BOTH directions
    assert ("ATL", "LAX") in pairs
    assert ("LAX", "ATL") in pairs
    assert len(dates) == 2
    assert len(pairs) == 2 * len(DELTA_ROUTES)


def test_delta_routes_count_and_no_reverse_dups():
    assert len(DELTA_ROUTES) == 26
    keys = [frozenset(p) for p in DELTA_ROUTES]
    assert len(keys) == len(set(keys)), "reverse/exact dup in DELTA_ROUTES"
    assert all(len(k) == 2 for k in keys)


def test_delta_covers_msp_demand():
    assert ("MSP", "JFK") in DELTA_ROUTES  # the 0-result gap from the logs


def test_build_plan_cron_shards_partition_all_routes_disjointly():
    # The 3 production shards must TOGETHER cover every route (both directions), be pairwise
    # disjoint, and each stay well under Delta's ~27 directed-leg per-session Akamai ceiling.
    shards = [
        _build_plan("", "", "", 5, date(2026, 6, 8), shard_index=i, shards=3)[0] for i in range(3)
    ]
    for i in range(3):
        for j in range(i + 1, 3):
            assert set(shards[i]).isdisjoint(set(shards[j])), "shards overlap — route scraped twice"
    full, _ = _build_plan("", "", "", 5, date(2026, 6, 8))
    union = set().union(*shards)
    assert union == set(full), "shards must cover every route"
    assert sum(len(s) for s in shards) == len(full) == 2 * len(DELTA_ROUTES)
    assert max(len(s) for s in shards) <= 27, "a shard exceeds the ~27 directed-leg Akamai ceiling"


def test_build_plan_single_route_only_shard0_works():
    # On-demand single-route dispatch spawns both matrix shards, but only shard 0 scrapes;
    # other shards no-op so the route isn't scraped twice.
    p0, d0 = _build_plan("ATL", "LAX", "", 5, date(2026, 6, 8), shard_index=0, shards=2)
    p1, d1 = _build_plan("ATL", "LAX", "", 5, date(2026, 6, 8), shard_index=1, shards=2)
    assert p0 == [("ATL", "LAX")] and d0
    assert p1 == [] and d1 == []
