"""IATA airport → IANA timezone, for rendering award departure_time_local in the ORIGIN
airport's local wall-clock so it matches Google Flights' displayed local time. Covers every
origin across our four programs (AS + B6 + DL + WN). A MISSING origin makes the cash matcher
skip the route ("No timezone for origin …" in the cash run) so it never gets a cash_fares row,
and makes the award scrapers drop departure times — add an entry before onboarding a new origin
airport. `tests/test_airport_tz.py` guards that every seeded route airport is mapped."""

AIRPORT_TZ: dict[str, str] = {
    "AMS": "Europe/Amsterdam",   # Amsterdam — Delta/KLM (SkyTeam) origin
    "ANC": "America/Anchorage",
    "ATL": "America/New_York",
    "ATH": "Europe/Athens",
    "AUH": "Asia/Dubai",  # Abu Dhabi — Etihad Guest hub (UTC+4, no DST)
    "AUS": "America/Chicago",
    "BCN": "Europe/Madrid",
    "BNA": "America/Chicago",
    "BNE": "Australia/Brisbane",
    "BOI": "America/Boise",
    "BOS": "America/New_York",
    "BUR": "America/Los_Angeles",
    "BWI": "America/New_York",
    "CDG": "Europe/Paris",        # Paris CDG — Delta/Air France (SkyTeam) origin
    "CLT": "America/New_York",
    "CPH": "Europe/Copenhagen",
    "CTA": "Europe/Rome",
    "DAL": "America/Chicago",
    "DCA": "America/New_York",
    "DEN": "America/Denver",
    "DFW": "America/Chicago",
    "DOH": "Asia/Qatar",
    "DUB": "Europe/Dublin",
    "DTW": "America/New_York",
    "EDI": "Europe/London",
    "EWR": "America/New_York",
    "EZE": "America/Argentina/Buenos_Aires",  # Buenos Aires Ezeiza — JetBlue route timezone
    "FAI": "America/Anchorage",
    "FCO": "Europe/Rome",
    "FLL": "America/New_York",
    "FRA": "Europe/Berlin",
    "GEG": "America/Los_Angeles",
    "GRU": "America/Sao_Paulo",   # São Paulo — Delta/LATAM (SkyTeam) origin
    "HEL": "Europe/Helsinki",
    "HKG": "Asia/Hong_Kong",      # Hong Kong — Alaska/Cathay origin
    "HND": "Asia/Tokyo",  # Tokyo Haneda — Alaska partner (JAL) origin
    "HNL": "Pacific/Honolulu",
    "HOU": "America/Chicago",
    "IAD": "America/New_York",
    "IAH": "America/Chicago",
    "ICN": "Asia/Seoul",          # Seoul Incheon — Delta/Korean (SkyTeam) origin
    "IST": "Europe/Istanbul",  # Istanbul — Turkish hub; needed for cash-matcher origin-local time
    "JFK": "America/New_York",
    "KEF": "Atlantic/Reykjavik",
    "KOA": "Pacific/Honolulu",
    "LAS": "America/Los_Angeles",
    "LAX": "America/Los_Angeles",
    "LGA": "America/New_York",
    "LGB": "America/Los_Angeles",
    "LGW": "Europe/London",
    "LHR": "Europe/London",  # London Heathrow — Alaska partner (BA) origin
    "LIH": "Pacific/Honolulu",
    "MAD": "Europe/Madrid",
    "MCO": "America/New_York",
    "MDW": "America/Chicago",
    "MEX": "America/Mexico_City",
    "MIA": "America/New_York",
    "MLA": "Europe/Malta",
    "MSP": "America/Chicago",
    "MSY": "America/Chicago",     # New Orleans — Southwest spoke
    "MXP": "Europe/Rome",
    "NCE": "Europe/Paris",
    "NRT": "Asia/Tokyo",          # Tokyo Narita — Alaska/JAL origin
    "OAK": "America/Los_Angeles",
    "OGG": "Pacific/Honolulu",
    "OLB": "Europe/Rome",
    "ONT": "America/Los_Angeles",
    "OPO": "Europe/Lisbon",
    "ORD": "America/Chicago",
    "PDX": "America/Los_Angeles",
    "PHL": "America/New_York",
    "PHX": "America/Phoenix",
    "PSP": "America/Los_Angeles",
    "RDU": "America/New_York",
    "RSW": "America/New_York",
    "SAN": "America/Los_Angeles",
    "SCL": "America/Santiago",
    "SEA": "America/Los_Angeles",
    "SFO": "America/Los_Angeles",
    "SJC": "America/Los_Angeles",
    "SJU": "America/Puerto_Rico",  # San Juan — JetBlue origin
    "SLC": "America/Denver",
    "SMF": "America/Los_Angeles",  # Sacramento — Southwest focus-city spoke
    "SNA": "America/Los_Angeles",
    "SYD": "Australia/Sydney",     # Sydney — Alaska/Qantas origin
    "TPE": "Asia/Taipei",          # Taipei — Alaska/Starlux origin
    "TPA": "America/New_York",
}
