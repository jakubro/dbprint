"""Day-count arithmetic (spec/temporal_age.py): the `freshness` rule the validator recomputes."""

from __future__ import annotations

from datetime import UTC, datetime

from dbprint.spec.temporal_age import (
    day_count,
    freshness_classification,
    max_age_days,
    parse_instant,
)


class TestDayCount:
    """SPEC 2.2.4: whole elapsed days, floored - not calendar-boundary crossings."""

    def test_a_six_day_gap_counts_six(self) -> None:
        earlier = datetime(2026, 1, 1, tzinfo=UTC)
        later = datetime(2026, 1, 7, tzinfo=UTC)

        assert day_count(earlier, later) == 6

    def test_a_sub_day_span_floors_to_zero(self) -> None:
        earlier = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)
        later = datetime(2026, 1, 1, 20, 0, 0, tzinfo=UTC)

        assert day_count(earlier, later) == 0

    def test_a_midnight_crossing_under_a_day_still_floors_to_zero(self) -> None:
        """20 minutes elapsed, one calendar boundary crossed - elapsed time wins."""

        earlier = datetime(2026, 1, 1, 23, 50, 0, tzinfo=UTC)
        later = datetime(2026, 1, 2, 0, 10, 0, tzinfo=UTC)

        assert day_count(earlier, later) == 0


class TestParseInstant:
    """Every domain-rendered `range.max` shape SPEC 2.2.4 permits, or explicitly forbids."""

    def test_a_zone_aware_string_parses(self) -> None:
        parsed = parse_instant("2026-08-10T16:47:47Z")

        assert parsed == datetime(2026, 8, 10, 16, 47, 47, tzinfo=UTC)

    def test_a_naive_string_is_read_as_utc(self) -> None:
        """SPEC 2.2.4: a zoneless reading is treated as UTC, not left tz-naive."""

        parsed = parse_instant("2026-08-10T16:47:47")

        assert parsed == datetime(2026, 8, 10, 16, 47, 47, tzinfo=UTC)

    def test_a_date_only_string_parses_at_midnight(self) -> None:
        assert parse_instant("2026-08-10") == datetime(2026, 8, 10, tzinfo=UTC)

    def test_a_bare_year_int_is_read_as_january_first(self) -> None:
        """MySQL YEAR renders as a bare int (SPEC 2.2.4 domain rendering), not an ISO string."""

        assert parse_instant(1960) == datetime(1960, 1, 1, tzinfo=UTC)

    def test_a_datetime_object_passes_through_normalized(self) -> None:
        """A driver-native value, read before the artifact's own string rendering."""

        naive = datetime(2026, 8, 10, 16, 47, 47)  # noqa: DTZ001 - the case under test

        assert parse_instant(naive) == datetime(2026, 8, 10, 16, 47, 47, tzinfo=UTC)

    def test_a_bool_is_not_a_year(self) -> None:
        """`bool` is an `int` subclass in Python - excluded explicitly."""

        assert parse_instant(True) is None

    def test_a_time_only_reading_does_not_parse(self) -> None:
        assert parse_instant("16:47:47") is None

    def test_an_infinity_sentinel_does_not_parse(self) -> None:
        assert parse_instant("infinity") is None

    def test_a_bc_year_does_not_parse(self) -> None:
        assert parse_instant("0001-01-01T00:00:00 BC") is None

    def test_a_year_outside_the_representable_range_does_not_parse(self) -> None:
        assert parse_instant("52030-01-01T00:00:00") is None

    def test_none_does_not_parse(self) -> None:
        assert parse_instant(None) is None


class TestMaxAgeDays:
    """SPEC 2.2.4: `max(0, day_count(max(column), profiled_at))`."""

    def test_six_days_old(self) -> None:
        assert max_age_days("2026-08-11T00:00:00Z", "2026-08-17T00:00:00Z") == 6

    def test_a_future_value_clamps_to_zero(self) -> None:
        assert max_age_days("3000-01-01T00:00:00Z", "2026-08-17T00:00:00Z") == 0

    def test_an_unparseable_bound_reads_zero_without_arithmetic(self) -> None:
        assert max_age_days("16:47:47", "2026-08-17T00:00:00Z") == 0

    def test_an_absent_bound_reads_zero(self) -> None:
        assert max_age_days(None, "2026-08-17T00:00:00Z") == 0

    def test_a_naive_max_against_a_utc_profiled_at_does_not_raise(self) -> None:
        """The cross-domain comparison this field introduces - a real regression risk."""

        assert max_age_days("2026-08-11T00:00:00", "2026-08-17T00:00:00Z") == 6

    def test_a_year_typed_bound_computes_an_age(self) -> None:
        assert max_age_days(1960, "2026-08-17T00:00:00Z") > 0


class TestFreshnessClassification:
    """SPEC 2.2.4 thresholds: live < 7, stale < 90, dormant >= 90."""

    def test_zero_days_is_live(self) -> None:
        assert freshness_classification(0) == "live"

    def test_six_days_is_live(self) -> None:
        assert freshness_classification(6) == "live"

    def test_seven_days_is_stale(self) -> None:
        assert freshness_classification(7) == "stale"

    def test_eighty_nine_days_is_stale(self) -> None:
        assert freshness_classification(89) == "stale"

    def test_ninety_days_is_dormant(self) -> None:
        assert freshness_classification(90) == "dormant"
