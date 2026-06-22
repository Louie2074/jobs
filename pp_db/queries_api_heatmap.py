"""API-only read queries (Postgres / SQLAlchemy) — two helpers: ``get_heatmap`` and
``get_cash_fares``.

Both are read-only and live only in the API copy (the flexible-date heatmap + the cash-fare
lookup that feeds CPP). Each takes an explicit SQLAlchemy ``Connection`` as its first arg.

Dialect notes:
  * Reproduced with ``text()`` so the GROUP BY / aggregate / ORDER BY / LIMIT are explicit.
    Tables live in schema ``pp``.
  * ``current_date`` and ``now()`` filter on the real session clock
    (``f.date >= current_date`` and, for cash, ``expires_at_utc > now()``).
  * NO float/round expression in either query — ``get_heatmap`` aggregates the INTEGER
    ``points_cost`` (MIN) and a COUNT (both yield Python ``int``), and ``get_cash_fares``
    selects the NUMERIC ``cash_price`` column verbatim (Decimal). So there is no ``::float8``
    keystone cast to apply here (unlike ``get_flights``' ``cpp``).
  * ``*_utc`` columns are naive TIMESTAMP; the engine pins the session to UTC so ``> now()``
    compares correctly.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import Connection, text


def get_heatmap(
    conn: Connection,
    origin: str,
    destination: str,
    date_from: date,
    date_to: date,
    cabin_class: str | None = None,
    airline: str | None = None,
    max_stops: int | None = None,
    max_points: int | None = None,
    passengers: int = 1,
) -> list[dict[str, Any]]:
    """Per-day cheapest-award summary for a route over a date window — the flexible-date heatmap.

    Returns one row per day that has flights,
    ``{date, min_points, flight_count}``. Freshness matches ``get_flights``
    (``f.date >= current_date``, not expires-based). Grouped by day and ordered ``f.date ASC``.

    Honours the same per-flight filters the list view applies so the calendar can't disagree with
    it: ``cabin_class`` / ``airline`` (exact match), ``max_stops`` (``f.stops <= max_stops``;
    ``max_stops=0`` is nonstop, so guard on ``is not None`` — 0 is falsy), ``max_points`` (the TOTAL
    party budget ``points_cost * passengers <= max_points``, matching ``get_flights``), and
    ``passengers`` (keep only flights that can seat the whole party, or the ``-1`` unknown-count
    sentinel). The aggregated MIN/COUNT therefore reflect only the filtered flights.
    """
    filters = [
        "f.origin = :origin",
        "f.destination = :destination",
        "f.date BETWEEN :date_from AND :date_to",
        "f.date >= current_date",
    ]
    params: dict[str, Any] = {
        "origin": origin,
        "destination": destination,
        "date_from": date_from,
        "date_to": date_to,
    }

    if cabin_class:
        filters.append("f.cabin_class = :cabin_class")
        params["cabin_class"] = cabin_class
    if airline:
        filters.append("f.airline = :airline")
        params["airline"] = airline
    if max_stops is not None:
        # 0 = nonstop; guard on `is not None` so the nonstop case isn't dropped by falsiness.
        filters.append("f.stops <= :max_stops")
        params["max_stops"] = max_stops
    if max_points:
        # Budget is the TOTAL for the whole party (identical to get_flights at passengers=1).
        filters.append("f.points_cost * :passengers <= :max_points")
        params["passengers"] = passengers
        params["max_points"] = max_points
    if passengers > 1:
        # Only keep flights that can seat the whole party; -1 = scraper didn't stamp a count.
        filters.append("(f.available_seats >= :passengers OR f.available_seats < 0)")
        params["passengers"] = passengers

    sql = text(
        f"""
        SELECT f.date AS date, MIN(f.points_cost) AS min_points, COUNT(*) AS flight_count
        FROM pp.flights f
        WHERE {" AND ".join(filters)}
        GROUP BY f.date
        ORDER BY f.date ASC
        """
    )
    columns = ["date", "min_points", "flight_count"]
    rows = conn.execute(sql, params).fetchall()
    return [dict(zip(columns, row, strict=False)) for row in rows]


def get_cash_fares(
    conn: Connection,
    origin: str,
    destination: str,
    date_from: date,
    date_to: date,
    airline: str | None = None,
    cabin_class: str | None = None,
) -> list[dict[str, Any]]:
    """Return fresh (non-expired) cash fares for a route + date range.

    Filtered on the date window, ``date >= current_date`` and ``expires_at_utc > now()``,
    optionally by airline / cabin. Ordered by date ASC then cash_price ASC, capped at 200 rows.
    ``cash_price`` is selected verbatim (NUMERIC → Decimal, so no cast).
    """
    filters = [
        "origin = :origin",
        "destination = :destination",
        "date BETWEEN :date_from AND :date_to",
        "date >= current_date",
        "expires_at_utc > now()",
    ]
    params: dict[str, Any] = {
        "origin": origin,
        "destination": destination,
        "date_from": date_from,
        "date_to": date_to,
    }
    if airline:
        filters.append("airline = :airline")
        params["airline"] = airline
    if cabin_class:
        filters.append("cabin_class = :cabin_class")
        params["cabin_class"] = cabin_class

    where = " AND ".join(filters)
    sql = text(
        f"""
        SELECT origin, destination, date, airline, cabin_class,
               flight_number, cash_price, currency, scraped_at_utc AS scraped_at
        FROM pp.cash_fares
        WHERE {where}
        ORDER BY date ASC, cash_price ASC
        LIMIT 200
        """
    )
    columns = [
        "origin",
        "destination",
        "date",
        "airline",
        "cabin_class",
        "flight_number",
        "cash_price",
        "currency",
        "scraped_at",
    ]
    rows = conn.execute(sql, params).fetchall()
    return [dict(zip(columns, row, strict=False)) for row in rows]
