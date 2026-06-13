import os

# config/settings._require("MOTHERDUCK_TOKEN") runs at import of scrapers.southwest; the parser
# never connects to the DB, so a dummy value is enough to import.
os.environ.setdefault("MOTHERDUCK_TOKEN", "test-dummy-token")

from datetime import datetime

from scrapers.southwest import _parse_segments


def test_parse_segments_nonstop():
    pid = "PLURED|HLCFF4Q,H,SEA,LAX,2026-06-22T16:55-07:00,2026-06-22T21:10-07:00,WN,WN,2396,7S7"
    segs = _parse_segments(pid)
    assert len(segs) == 1
    s = segs[0]
    assert s["origin"] == "SEA"
    assert s["dest"] == "LAX"
    assert s["flight_num"] == "2396"
    assert s["booking_class"] == "H"
    assert s["aircraft"] == "7S7"
    assert s["depart"] == datetime.fromisoformat("2026-06-22T16:55-07:00")
    assert s["arrive"] == datetime.fromisoformat("2026-06-22T21:10-07:00")


def test_parse_segments_connection():
    pid = (
        "PLURED|ULAFF2F,U,SEA,OAK,2026-06-22T10:25-07:00,2026-06-22T12:35-07:00,WN,WN,1713,7M8"
        "|ULAFF2F,U,OAK,LAX,2026-06-22T13:20-07:00,2026-06-22T14:45-07:00,WN,WN,4978,7M8"
    )
    segs = _parse_segments(pid)
    assert [s["dest"] for s in segs] == ["OAK", "LAX"]
    assert [s["flight_num"] for s in segs] == ["1713", "4978"]


def test_parse_segments_malformed_returns_empty():
    assert _parse_segments("no-pipes-here") == []
    assert _parse_segments("") == []
    assert _parse_segments(None) == []


from scrapers.southwest import _cheapest_available


def _fp(status, points):
    fare = {} if points is None else {"totalFare": {"currencyCode": "POINTS", "value": str(points)},
                                      "totalTaxesAndFees": {"currencyCode": "USD", "value": "5.60"}}
    return {"availabilityStatus": status, "fare": fare, "productId": "X|a,b,c,d,e,f,WN,WN,1,7S7"}


def test_cheapest_available_skips_unavailable_and_picks_lowest():
    fps = {
        "WGARED": _fp("UNAVAILABLE", None),
        "PLURED": _fp("AVAILABLE", 37500),
        "ANYRED": _fp("AVAILABLE", 43000),
        "BUSRED": _fp("AVAILABLE", 47000),
    }
    family, fp = _cheapest_available(fps)
    assert family == "PLURED"
    assert fp["fare"]["totalFare"]["value"] == "37500"


def test_cheapest_available_none_when_all_unavailable():
    fps = {"WGARED": _fp("UNAVAILABLE", None), "PLURED": _fp("UNAVAILABLE", None)}
    assert _cheapest_available(fps) is None


def test_cheapest_available_none_on_empty():
    assert _cheapest_available({}) is None


from datetime import date as _date

from scrapers.southwest import SouthwestScraper, _build_request_body


def test_build_request_body_shape():
    body = _build_request_body("sea", "lax", _date(2026, 6, 22))
    assert body["originationAirportCode"] == "SEA"
    assert body["destinationAirportCode"] == "LAX"
    assert body["to"] == "LAX"
    assert body["departureDate"] == "2026-06-22"
    assert body["fareType"] == "POINTS"
    assert body["tripType"] == "oneway"
    assert body["returnDate"] == ""


def test_scraper_identity_and_warm_url():
    s = SouthwestScraper()
    assert s.airline_code == "WN"
    assert s.program_name == "Rapid Rewards"
    assert s.source == "southwest"
    # warm_url is a real southwest.com booking page so Shape's fetch hook arms.
    assert s.warm_url.startswith("https://www.southwest.com/air/booking/select-depart.html")
    s.close()
import json
from datetime import date, datetime
from pathlib import Path

_FIXTURE = Path(__file__).parent / "fixtures" / "southwest_SEA-LAX_2026-06-22.json"


def _records():
    raw = json.loads(_FIXTURE.read_text())
    return SouthwestScraper().normalize(raw, "SEA", "LAX", date(2026, 6, 22))


def test_normalize_record_count():
    # All 26 itineraries in the fixture have at least one AVAILABLE fare family.
    assert len(_records()) == 26


def test_normalize_constants_on_every_row():
    for r in _records():
        assert r.airline == "WN"
        assert r.program == "Rapid Rewards"
        assert r.source == "southwest"
        assert r.cabin_class == "economy"
        assert r.available_seats == -1
        assert r.mixed_cabin is False
        assert r.partner_airline is None
        assert r.origin == "SEA"
        assert r.destination == "LAX"
        assert r.points_cost > 0


def test_normalize_nonstop_record():
    r = next(r for r in _records() if r.raw_flight_number == "WN 2396")
    assert r.stops == 0
    assert r.points_cost == 37500          # WGARED unavailable -> prices at PLURED
    assert r.cash_cost == 5.60
    assert r.fare_class == "H"
    assert r.aircraft_type == "7S7"
    assert r.layover_airports is None
    assert r.layover_duration_minutes is None
    assert r.duration_minutes == 255
    assert r.next_day_arrival is False
    assert r.is_saver is True               # PLURED -> discounted tier
    assert r.departure_time_local == datetime.fromisoformat("2026-06-22T16:55-07:00")


def test_normalize_connection_record():
    r = next(r for r in _records() if r.raw_flight_number == "WN 1713+WN 4978")
    assert r.stops == 1
    assert r.layover_airports == "OAK"
    assert r.layover_duration_minutes == 45   # OAK arr 12:35 -> dep 13:20
    assert r.aircraft_type == "7M8"


def test_normalize_next_day_record():
    r = next(r for r in _records() if r.raw_flight_number == "WN 868")
    assert r.next_day_arrival is True
