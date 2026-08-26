"""SPEC 4.2's candidate-key threshold: `compute_cardinality_ratio` / `is_candidate_key`
(spec/classification.py), and the exception marker they gate.

Uniqueness is not a classification: no `classify()` or `_pre_classify` reads a ratio, and
`_detect_columns` is the sole caller of `is_candidate_key`.
"""

from __future__ import annotations

import pytest

from dbprint.spec.classification import (
    CANDIDATE_KEY_THRESHOLD,
    compute_candidate_key_exception,
    compute_cardinality_ratio,
    is_candidate_key,
)


# (cardinality, rows_scanned, expected): 9998/9999 rounds to the threshold at six places.
_BOUNDARY_CASES = [
    pytest.param(9997, 9999, False, id="just_outside_below_the_rounding_band"),
    pytest.param(9998, 9999, True, id="inside_the_rounding_band"),
    pytest.param(9999, 10000, True, id="exactly_on_the_raw_threshold"),
    pytest.param(10000, 10000, True, id="well_above_the_threshold"),
    pytest.param(9000, 10000, False, id="well_below_the_threshold"),
    pytest.param(0, 0, False, id="empty_table"),
]


class TestSharedHelper:
    """`compute_cardinality_ratio` + `is_candidate_key` directly."""

    @pytest.mark.parametrize(("cardinality", "rows_scanned", "expected"), _BOUNDARY_CASES)
    def test_boundary_pairs(self, cardinality: int, rows_scanned: int, expected: bool) -> None:
        ratio = compute_cardinality_ratio(cardinality, rows_scanned)

        assert is_candidate_key(cardinality, ratio) is expected

    def test_the_rounding_band_is_real(self) -> None:
        """The raw quotient and the rounded one disagree here - that is the whole point."""

        raw = 9998 / 9999
        rounded = compute_cardinality_ratio(9998, 9999)

        assert raw < CANDIDATE_KEY_THRESHOLD
        assert rounded >= CANDIDATE_KEY_THRESHOLD

    def test_the_floor_does_not_approach_the_candidate_key_threshold(self) -> None:
        """A floored near-zero ratio must stay far below 0.9999, not drift toward it."""

        ratio = compute_cardinality_ratio(1, 10_000_000)

        assert ratio == 0.000001
        assert is_candidate_key(1, ratio) is False


class TestCandidateKeyException:
    """SPEC 4.2's exception marker, at the ratio boundaries `is_candidate_key` shares."""

    def test_ratio_one_carries_no_exception_regardless_of_method(self) -> None:
        for method in ("exact", "approximate"):
            result = compute_candidate_key_exception(10000, 1.0, method, 10000, 0)

            assert result is None

    def test_exactly_on_the_threshold_measured_exact(self) -> None:
        """9999 of 10000 scanned distinct, exact - one duplicate was actually counted."""

        result = compute_candidate_key_exception(9999, 0.9999, "exact", 10000, 0)

        assert result == "measured_duplicates"

    def test_exactly_on_the_threshold_estimated_approximate(self) -> None:
        result = compute_candidate_key_exception(9999, 0.9999, "approximate", 10000, 0)

        assert result == "estimated"

    def test_just_below_one_measured_exact(self) -> None:
        result = compute_candidate_key_exception(999999, 0.999999, "exact", 1000000, 0)

        assert result == "measured_duplicates"

    def test_just_below_one_estimated_approximate(self) -> None:
        result = compute_candidate_key_exception(999999, 0.999999, "approximate", 1000000, 0)

        assert result == "estimated"

    def test_nulls_alone_are_not_measured_duplicates(self) -> None:
        """9 of 10 scanned rows are non-null and every one is distinct - no value repeats."""

        result = compute_candidate_key_exception(9, 0.9, "exact", 10, 1)

        assert result is None
