"""`spec/sketch.py` - the KMV sketch hash, packing and type-mapping (SPEC 2.2.14).

`TestVectors` locks SPEC.md's vectors; every adapter's in-database hash is checked on them.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from dbprint.spec import sketch as sketch_module
from dbprint.spec.sketch import (
    answerable_count,
    answerable_subset_containment,
    decode_sketch,
    estimate_intersection,
    low64_md5,
    pack_sketch,
    sketch_kind,
)


class TestVectors:
    """SPEC 2.2.14's published test vectors - MD5, low 64 bits, unsigned big-endian."""

    @pytest.mark.parametrize(
        ("canonical", "expected"),
        [
            pytest.param("42", 15584161582054922406, id="integer"),
            pytest.param("-7", 5585573698858288085, id="negative_integer"),
            pytest.param("4.00", 10071140874863616590, id="exact_decimal"),
            pytest.param("hello", 13362634815750784402, id="text"),
            pytest.param("true", 317521853213362953, id="boolean"),
            pytest.param("2026-05-17T22:48:01Z", 11467405332662396900, id="temporal"),
        ],
    )
    def test_low64_md5_matches_the_published_vector(self, canonical: str, expected: int) -> None:
        assert low64_md5(canonical) == expected


class TestSketchKind:
    """Dialect-normalized via `classification.base_type` - no second type-string list."""

    @pytest.mark.parametrize(
        ("sql_type", "kind"),
        [
            pytest.param("integer", "integer", id="postgres_integer"),
            pytest.param("bigint unsigned", "integer", id="mysql_qualified_integer"),
            pytest.param("NUMBER(38,0)", "decimal", id="snowflake_number"),
            pytest.param("numeric(10,2)", "decimal", id="precision_decimal"),
            pytest.param("varchar(255)", "text", id="varchar"),
            pytest.param("uuid", "text", id="uuid"),
            pytest.param("boolean", "boolean", id="boolean"),
            pytest.param("timestamp with time zone", "temporal", id="timestamptz"),
        ],
    )
    def test_a_covered_type_resolves_its_kind(self, sql_type: str, kind: str) -> None:
        assert sketch_kind(sql_type) == kind

    @pytest.mark.parametrize(
        "sql_type",
        [
            pytest.param("double precision", id="float"),
            pytest.param("money", id="money"),
            pytest.param("json", id="json"),
            pytest.param("bytea", id="unsupported"),
        ],
    )
    def test_a_type_with_no_canonical_encoding_resolves_nothing(self, sql_type: str) -> None:
        assert sketch_kind(sql_type) is None


class TestPackAndDecode:
    """The wire form: ascending 8-byte big-endian unsigned integers, base64-encoded."""

    def test_round_trips_in_ascending_order_regardless_of_input_order(self) -> None:
        encoded = pack_sketch([500, 1, 999, 250])

        assert decode_sketch(encoded) == [1, 250, 500, 999]

    def test_an_empty_sketch_round_trips_to_an_empty_list(self) -> None:
        assert decode_sketch(pack_sketch([])) == []

    def test_a_value_at_the_unsigned_64_bit_ceiling_round_trips(self) -> None:
        ceiling = 2**64 - 1

        assert decode_sketch(pack_sketch([ceiling])) == [ceiling]

    def test_invalid_base64_decodes_to_none(self) -> None:
        assert decode_sketch("not valid base64!!!") is None

    def test_a_length_not_a_multiple_of_eight_decodes_to_none(self) -> None:
        assert decode_sketch("QQ==") is None  # decodes to 1 byte, not a multiple of 8


class TestEstimateIntersection:
    """SPEC 2.3.10's bottom-k intersection estimator - `|A n B|`, not `|A|`/`|B|`."""

    def test_disjoint_sketches_estimate_zero(self) -> None:
        assert estimate_intersection([1, 2, 3], [4, 5, 6]) == 0

    def test_identical_sketches_estimate_full_overlap(self) -> None:
        assert estimate_intersection([1, 2, 3], [1, 2, 3]) == 3

    def test_a_subset_estimates_exactly_when_both_sides_are_exact(self) -> None:
        """Neither side reached k, so nothing is thresholded and the match count is true."""

        assert estimate_intersection([1, 2, 3, 4], [2, 4, 6, 8, 10]) == 2

    def test_a_match_at_or_above_the_shared_threshold_does_not_count(self) -> None:
        """`theta` is the tighter of the two horizons, so an exact sketch's reach past its
        partner's threshold is excluded; what survives is still density-scaled, since one
        exact side does not make the estimate exact.
        """

        with patch.object(sketch_module, "K", 3):
            # child is full (k=3) with threshold 30; parent holds 2 < k, so theta = 30.
            # The shared 40 sits above theta and does not count; 20 does, then scales
            # by _HASH_SPACE / 30 - huge, since a 30-wide window says little about 2**64.
            assert estimate_intersection([10, 20, 30], [20, 40]) == 614891469123651712

    def test_a_full_sketch_pair_scales_the_observed_density(self) -> None:
        """Both sides retained k=3, so theta is finite and a match below it is scaled up."""

        with patch.object(sketch_module, "K", 3):
            half_space = sketch_module._HASH_SPACE // 2
            child = [half_space - 1, half_space, half_space + 1]
            parent = [half_space - 1, half_space + 100, half_space + 200]
            # theta = min(max(child), max(parent)) = half_space + 1; the one match below
            # it covers roughly half the hash space, so the density scales it up to 2.
            assert estimate_intersection(child, parent) == 2


class TestAnswerableSubsetContainment:
    """SPEC 2.2.14: an exhaustive child's containment measured directly, no scale-up."""

    def test_an_untruncated_parent_needs_no_threshold(self) -> None:
        """Neither side reached k, so every child hash is answerable."""

        assert answerable_subset_containment([1, 2, 3], [1, 2, 3, 4, 5]) == (1.0, 3)

    def test_full_containment_within_the_answerable_subset(self) -> None:
        with patch.object(sketch_module, "K", 3):
            # parent is truncated (3 = k), theta = max(parent) = 30; both child hashes
            # sit below it, and both are members of the parent.
            assert answerable_subset_containment([10, 20], [10, 20, 30]) == (1.0, 2)

    def test_disjoint_within_the_answerable_subset_reads_zero(self) -> None:
        with patch.object(sketch_module, "K", 3):
            assert answerable_subset_containment([5, 15], [100, 200, 300]) == (0.0, 2)

    def test_partial_overlap_reads_the_true_share(self) -> None:
        with patch.object(sketch_module, "K", 3):
            # theta = max(parent) = 30; all three child hashes are answerable, two match.
            # Keyed off the parent's threshold alone - the child's length coincides with k here.
            result = answerable_subset_containment([5, 15, 25], [5, 20, 25, 30])
            assert result is not None
            ratio, count = result

            assert ratio == pytest.approx(2 / 3)
            assert count == 3

    def test_no_answerable_hash_returns_none(self) -> None:
        """Every child hash sits above the parent's threshold - zero evidence, not zero overlap."""

        with patch.object(sketch_module, "K", 3):
            assert answerable_subset_containment([20, 30], [5, 10, 15]) is None


class TestAnswerableCount:
    """SPEC 2.2.14's denominator, paired with `estimate_intersection`'s truncated-child arm."""

    def test_neither_side_truncated_counts_the_whole_child(self) -> None:
        assert answerable_count([1, 2, 3], [1, 2, 3, 4, 5]) == 3

    def test_the_parents_threshold_being_tighter_bounds_an_exhaustive_child(self) -> None:
        with patch.object(sketch_module, "K", 3):
            # parent truncated at k=3, theta=30; both child hashes sit below it.
            assert answerable_count([10, 20], [10, 20, 30]) == 2

    def test_the_childs_own_threshold_bounds_a_truncated_child(self) -> None:
        """Child itself retained k=3 and is the tighter side - its own max is excluded."""

        with patch.object(sketch_module, "K", 3):
            assert answerable_count([10, 20, 30], [5, 15]) == 2

    def test_zero_when_no_child_hash_falls_below_theta(self) -> None:
        """Same fixture `answerable_subset_containment` reads as no evidence at all."""

        with patch.object(sketch_module, "K", 3):
            assert answerable_count([20, 30], [5, 10, 15]) == 0

    def test_agrees_with_answerable_subset_containment_for_a_genuinely_exhaustive_child(
        self,
    ) -> None:
        """The two entry points converge when the contract holds - child shorter than k."""

        with patch.object(sketch_module, "K", 3):
            child, parent = [5, 15], [5, 20, 25, 30]
            result = answerable_subset_containment(child, parent)
            assert result is not None
            _ratio, count = result

            assert answerable_count(child, parent) == count
