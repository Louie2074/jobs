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
