from datetime import date

from southwest_browser_scrape import SOUTHWEST_ROUTES, _build_plan, _parse_dates_csv


def test_parse_dates_csv_valid_and_invalid():
    assert _parse_dates_csv("2026-06-20, ,nonsense,2026-06-22") == [
        date(2026, 6, 20),
        date(2026, 6, 22),
    ]


def test_build_plan_single_route():
    pairs, dates = _build_plan("sea", "lax", "2026-06-20,2026-06-21", 5, date(2026, 6, 13))
    assert pairs == [("SEA", "LAX")]  # requested direction only
    assert dates == [date(2026, 6, 20), date(2026, 6, 21)]


def test_build_plan_cron_mode_both_directions():
    pairs, dates = _build_plan("", "", "", 3, date(2026, 6, 13))
    # cron mode: every route scraped in both directions, near-term window
    assert len(pairs) == 2 * len(SOUTHWEST_ROUTES)
    assert ("LAS", "LAX") in pairs and ("LAX", "LAS") in pairs
    assert dates == [date(2026, 6, 13), date(2026, 6, 14), date(2026, 6, 15)]
