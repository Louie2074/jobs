"""Pure scoring + adaptive-cadence functions for the priority queue.

No DB, no I/O — unit-tested in isolation. See
docs/superpowers/specs/2026-06-17-adaptive-scrape-scheduling-design.md.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol


class _PricedCabin(Protocol):
    cabin_class: str
    points_cost: int | None


def decay_demand(
    stored: float, last_search_at: datetime | None, now: datetime, half_life_days: float
) -> float:
    """Exponentially-decayed demand evaluated at ``now``. Returns ``stored`` unchanged when
    there is no prior search timestamp or it is in the future (nothing to decay)."""
    if last_search_at is None or stored <= 0:
        return stored
    elapsed_days = (now - last_search_at).total_seconds() / 86400.0
    if elapsed_days <= 0:
        return stored
    return stored * (2.0 ** (-elapsed_days / half_life_days))


def bump_demand(
    stored: float, last_search_at: datetime | None, now: datetime, half_life_days: float
) -> float:
    """Decay the stored demand to ``now``, then add 1 for the new search."""
    return decay_demand(stored, last_search_at, now, half_life_days) + 1.0


def update_change_rate(prev_rate: float, changed: bool, alpha: float) -> float:
    """EWMA of the boolean change signal."""
    return alpha * (1.0 if changed else 0.0) + (1.0 - alpha) * prev_rate


def update_interval(
    prev_interval_h: float, changed: bool, *, lo: float, hi: float, step_h: float
) -> float:
    """AIMD cadence update, clamped to ``[lo, hi]``. Changed -> multiplicative decrease
    (react fast); unchanged -> additive increase (back off slow)."""
    nxt = prev_interval_h * 0.5 if changed else prev_interval_h + step_h
    return max(lo, min(hi, nxt))


def overdue_ratio(now: datetime, next_scrape_at: datetime, interval_h: float) -> float:
    """How far past due, in units of the route's own interval. Guards a zero interval to 1.0."""
    if interval_h <= 0:
        return 1.0
    return (now - next_scrape_at).total_seconds() / 3600.0 / interval_h


def route_score(
    effective_demand: float,
    overdue: float,
    change_rate: float,
    *,
    w_demand: float,
    w_overdue: float,
    w_change: float,
    demand_ref: float,
) -> float:
    """Weighted priority score for ordering due routes; each component normalized to [0,1]."""
    demand_norm = min(1.0, effective_demand / demand_ref) if demand_ref > 0 else 0.0
    overdue_norm = max(0.0, min(1.0, overdue))
    change_norm = max(0.0, min(1.0, change_rate))
    return w_demand * demand_norm + w_overdue * overdue_norm + w_change * change_norm


def cheapest_by_cabin(rows: Iterable["_PricedCabin"]) -> dict[str, int]:
    """Map ``cabin_class`` -> minimum ``points_cost`` over ``rows`` (FlightRecord-like objects
    with ``.cabin_class`` and ``.points_cost``). Rows with a None price are ignored."""
    out: dict[str, int] = {}
    for r in rows:
        price = r.points_cost
        if price is None:
            continue
        cabin = r.cabin_class
        if cabin not in out or price < out[cabin]:
            out[cabin] = price
    return out


def did_change(new_cheapest: dict[str, int], old_cheapest: dict[str, int] | None) -> bool:
    """True if the cheapest-points-by-cabin map differs from the prior scrape (or there is no
    prior). A cabin appearing or disappearing counts as a change."""
    if old_cheapest is None:
        return True
    return new_cheapest != old_cheapest
