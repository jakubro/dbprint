"""Turning one grouped scan's flag strings into the SPEC 2.2.10 census.

The dialects differ in how the flag string is built but not in what it means, so the decoding
is shared and tested once here against hand-written rows.
"""

from __future__ import annotations

import pytest

from dbprint.adapters.base import ColumnMeta, null_flags, null_patterns_from_rows


CAP = 3


def _columns(*names: str) -> list[ColumnMeta]:
    return [
        ColumnMeta(name=name, sql_type="integer", nullable=True, default=None, ordinal=i)
        for i, name in enumerate(names, start=1)
    ]


class TestDecoding:
    def test_flags_recover_the_column_names_by_position(self) -> None:
        census = null_patterns_from_rows(
            [("010", 7)],
            _columns("a", "b", "c"),
            rows_scanned=7,
            cap=CAP,
        )

        assert census.patterns[0].columns == ("b",)

    def test_the_fully_populated_rows_are_listed(self) -> None:
        """A reader wants to know how many rows carry no null at all."""

        census = null_patterns_from_rows(
            [("000", 6), ("100", 4)],
            _columns("a", "b", "c"),
            rows_scanned=10,
            cap=CAP,
        )

        assert census.patterns[0].columns == ()
        assert census.patterns[0].count == 6

    def test_names_within_one_pattern_are_sorted(self) -> None:
        """The tie-break reads the array, so its own order has to be settled."""

        census = null_patterns_from_rows(
            [("101", 3)],
            _columns("c", "b", "a"),
            rows_scanned=3,
            cap=CAP,
        )

        assert census.patterns[0].columns == ("a", "c")

    def test_an_always_null_column_appears_in_every_pattern(self) -> None:
        census = null_patterns_from_rows(
            [("100", 6), ("110", 4)],
            _columns("a", "b", "c"),
            rows_scanned=10,
            cap=CAP,
        )

        assert all("a" in pattern.columns for pattern in census.patterns)


class TestOrdering:
    def test_count_descending_wins(self) -> None:
        census = null_patterns_from_rows(
            [("100", 2), ("010", 9)],
            _columns("a", "b", "c"),
            rows_scanned=11,
            cap=CAP,
        )

        assert [p.count for p in census.patterns] == [9, 2]

    def test_equal_counts_break_on_the_name_array_not_the_flag_string(self) -> None:
        """Flag order is positional, so the two disagree unless names are alphabetical."""

        census = null_patterns_from_rows(
            [("100", 5), ("001", 5)],
            _columns("z", "y", "a"),
            rows_scanned=10,
            cap=CAP,
        )

        assert [p.columns for p in census.patterns] == [("a",), ("z",)]


class TestCoverage:
    def test_a_complete_census_covers_everything(self) -> None:
        census = null_patterns_from_rows(
            [("000", 6), ("100", 4)],
            _columns("a", "b", "c"),
            rows_scanned=10,
            cap=CAP,
        )

        assert census.coverage == 1.0

    def test_a_truncated_census_reports_what_the_cap_left(self) -> None:
        """One row beyond the cap is fetched, so truncation is observed, not predicted."""

        rows = [("000", 10), ("100", 8), ("010", 6), ("001", 4)]
        census = null_patterns_from_rows(rows, _columns("a", "b", "c"), rows_scanned=28, cap=CAP)

        assert len(census.patterns) == CAP
        assert census.coverage == pytest.approx(24 / 28, abs=1e-06)
        assert census.coverage < 1

    def test_no_scanned_rows_leaves_nothing_to_share(self) -> None:
        census = null_patterns_from_rows([], _columns("a"), rows_scanned=0, cap=CAP)

        assert census.patterns == ()
        assert census.coverage == 1.0

    def test_an_untruncated_census_short_of_rows_scanned_reports_the_real_quotient(
        self,
    ) -> None:
        """Under the cap, but a phase-A/B disagreement leaves 1 of 10 scanned rows unaccounted."""

        rows = [("000", 6), ("100", 3)]
        census = null_patterns_from_rows(rows, _columns("a", "b", "c"), rows_scanned=10, cap=CAP)

        assert len(census.patterns) < CAP  # untruncated: fewer rows than the cap fetched
        assert census.coverage == pytest.approx(9 / 10, abs=1e-06)
        assert census.coverage < 1.0


class TestCoverageMethod:
    def test_an_untruncated_census_agreeing_with_rows_scanned_is_measured(self) -> None:
        census = null_patterns_from_rows(
            [("000", 6), ("100", 4)],
            _columns("a", "b", "c"),
            rows_scanned=10,
            cap=CAP,
        )

        assert census.coverage_method == "measured"

    def test_an_untruncated_census_disagreeing_with_rows_scanned_is_bounded(self) -> None:
        """SPEC 2.2.10: the census and rows_scanned were not read at the same instant."""

        census = null_patterns_from_rows(
            [("000", 6), ("100", 3)],
            _columns("a", "b", "c"),
            rows_scanned=10,
            cap=CAP,
        )

        assert census.coverage_method == "bounded"

    def test_a_truncated_census_carries_no_coverage_method(self) -> None:
        """Short by the producer's own cap - a different, already-explained condition."""

        rows = [("000", 10), ("100", 8), ("010", 6), ("001", 4)]
        census = null_patterns_from_rows(rows, _columns("a", "b", "c"), rows_scanned=28, cap=CAP)

        assert census.coverage_method is None

    def test_no_scanned_rows_is_measured_not_bounded(self) -> None:
        """0 == 0 agrees, by the same arithmetic every other case uses."""

        census = null_patterns_from_rows([], _columns("a"), rows_scanned=0, cap=CAP)

        assert census.coverage_method == "measured"


class TestFlagExpression:
    def test_the_operator_form_chains_without_an_argument_limit(self) -> None:
        """Postgres rejects a function call past 100 arguments; a wide table has more."""

        expression = null_flags(['"a"', '"b"'], concat=False)

        assert expression == (
            "CASE WHEN \"a\" IS NULL THEN '1' ELSE '0' END || "
            "CASE WHEN \"b\" IS NULL THEN '1' ELSE '0' END"
        )

    def test_the_function_form_is_used_where_the_operator_means_or(self) -> None:
        expression = null_flags(["`a`", "`b`"], concat=True)

        assert expression.startswith("CONCAT(")
        assert "||" not in expression
