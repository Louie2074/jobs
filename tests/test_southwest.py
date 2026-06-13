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
