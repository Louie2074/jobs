from datetime import date

from scrapers.etihad import (
    EtihadScraper,
    _carrier,
    _day_offset,
    _parse_duration_mins,
    _parse_hhmm_on,
)

TRAVEL = date(2026, 7, 8)


def _cabin(name, miles, cash_cents):
    return {"cabin": name, "miles": miles, "cashCents": cash_cents}


def _card(
    dep="15:45", arr="12:25", origin="JFK", dest="AUH", day="+1", dur="12h 40m",
    nonstop=True, fns=("EY 2",), cabins=None,
):
    return {
        "depTime": dep, "arrTime": arr, "origin": origin, "dest": dest, "dayDiff": day,
        "duration": dur, "nonstop": nonstop, "flightNumbers": list(fns),
        "cabins": cabins if cabins is not None
        else [_cabin("Economy", 83625, 22490), _cabin("Business", 540375, 57990)],
    }


def _raw(*cards):
    return {"cards": list(cards)}


# ---------------------------------------------------------------- pure helpers
def test_parse_hhmm_attaches_origin_timezone():
    dt = _parse_hhmm_on(TRAVEL, "15:45", "JFK")
    assert dt is not None and dt.tzinfo is not None
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2026, 7, 8, 15, 45)


def test_parse_hhmm_unmapped_or_bad_is_none():
    assert _parse_hhmm_on(TRAVEL, "12:25", "ZZZ") is None  # unmapped airport
    assert _parse_hhmm_on(TRAVEL, "", "JFK") is None
    assert _parse_hhmm_on(TRAVEL, "99:99", "JFK") is None


def test_auh_is_mapped():
    # AUH must be in the tz map or AUH-origin return legs drop their departure time
    assert _parse_hhmm_on(TRAVEL, "19:20", "AUH") is not None


def test_parse_duration():
    assert _parse_duration_mins("12h 40m") == 760
    assert _parse_duration_mins("13h") == 780
    assert _parse_duration_mins("18h 45m") == 1125
    assert _parse_duration_mins("") is None


def test_day_offset():
    assert _day_offset("+1") == 1 and _day_offset("+2") == 2
    assert _day_offset("") == 0 and _day_offset(None) == 0


def test_carrier_prefix():
    assert _carrier("EY 2") == "EY" and _carrier("EY 8324") == "EY"


# ---------------------------------------------------------------- normalize
def test_normalize_reads_distinct_per_cabin_prices():
    # the per-cabin bug guard: economy 83,625 and business 540,375 must NOT collapse to one price
    recs = EtihadScraper().normalize(_raw(_card()), "JFK", "AUH", TRAVEL)
    by_cabin = {r.cabin_class: r for r in recs}
    assert set(by_cabin) == {"economy", "business"}
    assert by_cabin["economy"].points_cost == 83625
    assert by_cabin["business"].points_cost == 540375
    eco = by_cabin["economy"]
    assert eco.origin == "JFK" and eco.destination == "AUH" and eco.airline == "EY"
    assert eco.source == "etihad" and eco.program == "Etihad Guest"
    assert eco.stops == 0 and eco.raw_flight_number == "EY 2" and eco.available_seats == -1
    assert eco.departure_time_local is not None  # JFK mapped
    assert eco.next_day_arrival is True and eco.duration_minutes == 760


def test_normalize_cash_cents_to_dollars():
    eco = next(r for r in EtihadScraper().normalize(_raw(_card()), "JFK", "AUH", TRAVEL)
               if r.cabin_class == "economy")
    assert eco.cash_cost == 224.90  # data-amount 22490 cents → $224.90


def test_normalize_connection_sets_stops_and_flight_numbers():
    card = _card(
        dur="18h 45m", nonstop=False, fns=("EY 8324", "EY 8"),
        cabins=[_cabin("Economy", 114375, 22940), _cabin("Business", 690375, 58440)],
    )
    recs = EtihadScraper().normalize(_raw(card), "JFK", "AUH", TRAVEL)
    r = recs[0]
    assert r.stops == 1 and r.raw_flight_number == "EY 8324+EY 8"


def test_normalize_arrival_uses_day_offset_for_date():
    eco = next(r for r in EtihadScraper().normalize(_raw(_card()), "JFK", "AUH", TRAVEL)
               if r.cabin_class == "economy")
    # +1 day arrival → arrival local date is the day after departure
    assert eco.arrival_time_local is not None
    assert eco.arrival_time_local.date() == date(2026, 7, 9)


def test_normalize_skips_unpriced_and_unknown_cabins():
    card = _card(cabins=[
        _cabin("Economy", None, 22490),     # unpriced (miles None) → skip
        _cabin("WeirdCabin", 99000, 1000),  # unknown cabin → skip
    ])
    assert EtihadScraper().normalize(_raw(card), "JFK", "AUH", TRAVEL) == []


def test_normalize_partner_airline_when_non_ey_leg():
    card = _card(fns=("EY 8324", "AA 100"), cabins=[_cabin("Economy", 90000, 10000)])
    rec = EtihadScraper().normalize(_raw(card), "JFK", "AUH", TRAVEL)[0]
    assert rec.partner_airline == "AA"


def test_normalize_handles_blocked_and_empty():
    sc = EtihadScraper()
    assert sc.normalize({"blocked": True}, "JFK", "AUH", TRAVEL) == []
    assert sc.normalize({"cards": []}, "JFK", "AUH", TRAVEL) == []
    assert sc.normalize({}, "JFK", "AUH", TRAVEL) == []


# ---------------------------------------------------------------- request builder
def test_deeplink_encodes_route_and_date():
    url = EtihadScraper._deeplink("JFK", "AUH", TRAVEL)
    assert "B_LOCATION=JFK" in url and "E_LOCATION=AUH" in url
    assert "DATE_1=202607080000" in url and "FLOW=AWARD" in url
    assert "digital.etihad.com/book/search" in url


def test_extract_js_targets_card_hooks():
    js = EtihadScraper()._extract_js()
    assert "ey-bound-card-new" in js and "data-testid" in js
    assert "price-first-section" in js and "remaining-nonconverted-miles" in js
    assert "Pardon Our Interruption" in js
