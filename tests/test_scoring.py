"""Unit tests for the pure scoring/cadence functions (no DB)."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pipeline import scoring

NOW = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)


class TestDecayDemand:
    def test_no_prior_search_returns_stored(self):
        assert scoring.decay_demand(5.0, None, NOW, 14) == 5.0

    def test_one_half_life_halves(self):
        last = NOW - timedelta(days=14)
        assert scoring.decay_demand(8.0, last, NOW, 14) == 4.0

    def test_future_timestamp_does_not_grow(self):
        last = NOW + timedelta(days=1)
        assert scoring.decay_demand(8.0, last, NOW, 14) == 8.0


class TestBumpDemand:
    def test_bump_decays_then_adds_one(self):
        last = NOW - timedelta(days=14)
        assert scoring.bump_demand(8.0, last, NOW, 14) == 5.0  # 8 -> 4 -> +1

    def test_bump_from_zero_no_prior(self):
        assert scoring.bump_demand(0.0, None, NOW, 14) == 1.0


class TestChangeRate:
    def test_changed_pulls_toward_one(self):
        assert scoring.update_change_rate(0.5, True, 0.3) == 0.5 * 0.7 + 0.3

    def test_unchanged_pulls_toward_zero(self):
        assert scoring.update_change_rate(0.5, False, 0.3) == 0.5 * 0.7


class TestUpdateInterval:
    def test_changed_halves_clamped_to_lo(self):
        # 8 -> 4 but lo is 8, so clamps up to 8
        assert scoring.update_interval(8.0, True, lo=8, hi=24, step_h=8) == 8.0

    def test_changed_halves_above_lo(self):
        assert scoring.update_interval(24.0, True, lo=8, hi=24, step_h=8) == 12.0

    def test_unchanged_adds_step_clamped_to_hi(self):
        assert scoring.update_interval(20.0, False, lo=8, hi=24, step_h=8) == 24.0


class TestOverdueRatio:
    def test_exactly_due_is_zero(self):
        assert scoring.overdue_ratio(NOW, NOW, 24) == 0.0

    def test_one_interval_overdue_is_one(self):
        due_at = NOW - timedelta(hours=24)
        assert scoring.overdue_ratio(NOW, due_at, 24) == 1.0

    def test_zero_interval_guards_to_one(self):
        assert scoring.overdue_ratio(NOW, NOW, 0) == 1.0


class TestRouteScore:
    def test_weighted_sum_normalized(self):
        s = scoring.route_score(
            effective_demand=10,
            overdue=1.0,
            change_rate=1.0,
            w_demand=0.5,
            w_overdue=0.3,
            w_change=0.2,
            demand_ref=10,
        )
        assert s == 1.0  # all three components saturate to 1

    def test_demand_clamped_above_ref(self):
        s = scoring.route_score(
            effective_demand=999,
            overdue=0.0,
            change_rate=0.0,
            w_demand=0.5,
            w_overdue=0.3,
            w_change=0.2,
            demand_ref=10,
        )
        assert s == 0.5  # demand_norm clamps to 1, others 0


@dataclass
class _Rec:
    cabin_class: str
    points_cost: int | None


class TestCheapestByCabin:
    def test_picks_min_per_cabin(self):
        rows = [_Rec("economy", 20000), _Rec("economy", 12000), _Rec("business", 50000)]
        assert scoring.cheapest_by_cabin(rows) == {"economy": 12000, "business": 50000}

    def test_skips_none_prices(self):
        rows = [_Rec("economy", None), _Rec("economy", 15000)]
        assert scoring.cheapest_by_cabin(rows) == {"economy": 15000}

    def test_empty_rows(self):
        assert scoring.cheapest_by_cabin([]) == {}


class TestDidChange:
    def test_no_prior_is_changed(self):
        assert scoring.did_change({"economy": 12000}, None) is True

    def test_identical_is_unchanged(self):
        assert scoring.did_change({"economy": 12000}, {"economy": 12000}) is False

    def test_price_move_is_changed(self):
        assert scoring.did_change({"economy": 11000}, {"economy": 12000}) is True

    def test_cabin_appeared_is_changed(self):
        assert scoring.did_change({"economy": 12000, "business": 50000}, {"economy": 12000}) is True
