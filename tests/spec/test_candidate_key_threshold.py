"""SPEC 4.2's candidate-key threshold: `compute_cardinality_ratio` / `is_candidate_key`
(spec/classification.py), and the exception marker they gate.

Uniqueness is not a classification: no `classify()` or `_pre_classify` reads a ratio, and
`_detect_columns` is the sole caller of `is_candidate_key`.
"""

from __future__ import annotations

import pytest

from dbprint.spec.classification import (
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

        assert raw < 0.9999
        assert rounded == 0.9999

    def test_the_floor_does_not_approach_the_candidate_key_threshold(self) -> None:
        """A floored near-zero ratio must stay far below 0.9999, not drift toward it."""

        ratio = compute_cardinality_ratio(1, 10_000_000)

        assert ratio == 0.000001
        assert is_candidate_key(1, ratio) is False


class TestCandidateKeyException:
    """SPEC 4.2's exception marker, at the ratio boundaries `is_candidate_key` shares."""

    def test_just_below_one_measured_exact(self) -> None:
        result = compute_candidate_key_exception(999999, 0.999999, "exact", 1000000, 0)

        assert result == "measured_duplicates"

    def test_just_below_one_estimated_approximate(self) -> None:
        result = compute_candidate_key_exception(999999, 0.999999, "approximate", 1000000, 0)

        assert result == "estimated"
