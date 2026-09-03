"""Freshness duration parser + manifest evaluation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from dbprint.engine.freshness import (
    DurationError,
    StaleEntry,
    evaluate,
    format_age,
    format_threshold,
    parse_duration,
)


class TestParseDuration:
    def test_days(self) -> None:
        assert parse_duration("7d") == 7.0

    def test_hours(self) -> None:
        assert parse_duration("12h") == 0.5

    def test_minutes(self) -> None:
        assert parse_duration("30m") == pytest.approx(30 / (24 * 60))

    def test_seconds(self) -> None:
        assert parse_duration("60s") == pytest.approx(60 / (24 * 3600))

    def test_case_insensitive(self) -> None:
        assert parse_duration("7D") == 7.0

    def test_invalid_grammar_raises(self) -> None:
        with pytest.raises(DurationError):
            parse_duration("1d12h")  # compound forms rejected

    def test_invalid_unit_raises(self) -> None:
        with pytest.raises(DurationError):
            parse_duration("7x")


_NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)


def _manifest_with(*entries: tuple[str, str | None]) -> dict[str, object]:
    return {
        "tables": {fqn: {"profiled_at": pa} for fqn, pa in entries},
    }


def _ts(now: datetime, age_days: float) -> str:
    return (now - timedelta(days=age_days)).isoformat().replace("+00:00", "Z")


class TestEvaluate:
    def test_empty_manifest_returns_empty(self) -> None:
        assert evaluate({}, 7.0, _NOW) == []

    def test_recent_entries_not_stale(self) -> None:
        manifest = _manifest_with(("a", _ts(_NOW, 2.0)))
        assert evaluate(manifest, 7.0, _NOW) == []

    def test_aged_entry_flagged(self) -> None:
        manifest = _manifest_with(("a", _ts(_NOW, 10.0)))
        stale = evaluate(manifest, 7.0, _NOW)
        assert len(stale) == 1
        assert stale[0].fqn == "a"
        assert stale[0].age_days == pytest.approx(10.0)

    def test_missing_profiled_at_treated_as_unknown(self) -> None:
        manifest = _manifest_with(("a", None))
        stale = evaluate(manifest, 7.0, _NOW)
        assert len(stale) == 1
        assert stale[0].age_days == float("inf")

    def test_returns_StaleEntry_dataclass(self) -> None:
        manifest = _manifest_with(("a", _ts(_NOW, 10.0)))
        stale = evaluate(manifest, 7.0, _NOW)
        assert isinstance(stale[0], StaleEntry)


class TestPerTableThreshold:
    """`threshold_for` is how a per-table threshold reaches `evaluate`; without it every
    entry is judged against the one run-level number.
    """

    def test_each_entry_is_judged_against_its_own_threshold(self) -> None:
        manifest = _manifest_with(("a", _ts(_NOW, 3.0)), ("b", _ts(_NOW, 3.0)))
        thresholds = {"a": 1.0, "b": 30.0}

        stale = evaluate(manifest, 7.0, _NOW, threshold_for=thresholds.__getitem__)

        assert [(s.fqn, s.max_age_days) for s in stale] == [("a", 1.0)]

    def test_the_entry_carries_the_threshold_that_judged_it(self) -> None:
        manifest = _manifest_with(("a", _ts(_NOW, 10.0)))

        stale = evaluate(manifest, 7.0, _NOW, threshold_for=lambda _fqn: 2.0)

        assert stale[0].max_age_days == 2.0

    def test_a_resolver_overrides_the_run_level_value(self) -> None:
        """The positional value must not leak past a resolver that answered."""

        manifest = _manifest_with(("a", _ts(_NOW, 10.0)))

        assert evaluate(manifest, 7.0, _NOW, threshold_for=lambda _fqn: 30.0) == []

    def test_an_unknown_age_still_carries_its_threshold(self) -> None:
        manifest = _manifest_with(("a", None))

        stale = evaluate(manifest, 7.0, _NOW, threshold_for=lambda _fqn: 2.0)

        assert stale[0].age_days == float("inf")
        assert stale[0].max_age_days == 2.0

    def test_without_a_resolver_every_entry_takes_the_run_level_value(self) -> None:
        manifest = _manifest_with(("a", _ts(_NOW, 10.0)), ("b", _ts(_NOW, 10.0)))

        stale = evaluate(manifest, 7.0, _NOW)

        assert {s.max_age_days for s in stale} == {7.0}


class TestFormatAge:
    def test_days_and_hours(self) -> None:
        assert format_age(2.5) == "2d 12h"

    def test_sub_day_in_hours(self) -> None:
        assert format_age(0.25) == "6.0h"

    def test_infinity_renders_unknown(self) -> None:
        assert format_age(float("inf")) == "unknown"


class TestFormatThreshold:
    def test_a_value_from_a_flag_round_trips(self) -> None:
        """A threshold is a configured value - `--max-age 12h` must render back as `12h`,
        not the compound `12.0h`/`0d 12h` shape `format_age` uses for a measured age.
        """

        for text in ("7d", "12h", "30m", "45s"):
            assert format_threshold(parse_duration(text)) == text

    def test_whole_days_render_as_days_not_hours(self) -> None:
        assert format_threshold(2.0) == "2d"

    def test_sub_day_renders_in_the_finest_exact_unit(self) -> None:
        assert format_threshold(0.5) == "12h"
