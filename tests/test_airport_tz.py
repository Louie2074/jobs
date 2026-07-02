from config.airport_tz import AIRPORT_TZ as CONFIG_AIRPORT_TZ
from config.routes import all_seeded_routes
from pp_db.airport_tz import AIRPORT_TZ as CASH_AIRPORT_TZ


def _seeded_airports() -> set[str]:
    return {
        airport
        for origin, dest, _airline, _tier in all_seeded_routes()
        for airport in (origin, dest)
    }


def test_eze_timezone_is_available_for_jetblue_and_cash_matching():
    assert CONFIG_AIRPORT_TZ["EZE"] == "America/Argentina/Buenos_Aires"
    assert CASH_AIRPORT_TZ["EZE"] == "America/Argentina/Buenos_Aires"


def test_all_seeded_airports_have_award_timezones():
    missing = sorted(_seeded_airports() - set(CONFIG_AIRPORT_TZ))
    assert missing == []


def test_all_seeded_airports_have_cash_matching_timezones():
    missing = sorted(_seeded_airports() - set(CASH_AIRPORT_TZ))
    assert missing == []
