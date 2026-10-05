"""`classify` (spec/distribution.py) - the shared rule every adapter's value-list and top-N
paths compute `distribution` through, guarded against a per-adapter copy.
"""

from __future__ import annotations

from dbprint.spec.distribution import classify, summarize


class TestPriorityOrder:
    """SPEC 2.2.5: dominant_value, then long_tail, then imbalanced, then uniform."""

    def test_top_value_at_the_threshold_is_dominant(self) -> None:
        assert classify([95, 5], 100, exhaustive=True) == "dominant_value"

    def test_a_thin_tail_below_the_share_floor_is_long_tail(self) -> None:
        assert classify([10, 8, 5], 100, exhaustive=False) == "long_tail"

    def test_long_tail_is_skipped_on_an_exhaustive_list(self) -> None:
        """An exhaustive list has no tail beyond itself (SPEC 2.2.5)."""

        assert classify([10, 8, 5], 100, exhaustive=True) == "uniform"

    def test_a_lopsided_pair_is_imbalanced(self) -> None:
        assert classify([10, 2], 12, exhaustive=True) == "imbalanced"

    def test_an_even_pair_is_uniform(self) -> None:
        assert classify([6, 6], 12, exhaustive=True) == "uniform"

    def test_empty_counts_is_uniform(self) -> None:
        assert classify([], 100, exhaustive=True) == "uniform"

    def test_zero_non_null_is_uniform(self) -> None:
        assert classify([5], 0, exhaustive=True) == "uniform"

    def test_all_zero_counts_is_uniform(self) -> None:
        assert classify([0, 0], 100, exhaustive=True) == "uniform"


class TestIncoherentRatioIsNeverPublishedAsSound:
    """A ratio `is_incoherent` rejects must not decide `dominant_value` or `long_tail`.

    Phases A and B are separate reads of a live table, so `sum(counts)` can exceed `non_null`.
    """

    def test_a_multi_entry_incoherent_list_falls_back_to_the_count_comparison(self) -> None:
        result = classify([40, 20], 5, exhaustive=True)

        assert result not in ("dominant_value", "long_tail")
        assert result == "uniform"

    def test_the_guard_fires_on_a_non_exhaustive_incoherent_list_too(self) -> None:
        result = classify([40, 20], 5, exhaustive=False)

        assert result not in ("dominant_value", "long_tail")


class TestSingleValueColumn:
    """SPEC 2.2.7: a `cardinality = 1` column reads `dominant_value`, coherent or not."""

    def test_a_coherent_single_value_list_is_dominant(self) -> None:
        assert classify([100], 100, exhaustive=True) == "dominant_value"

    def test_an_incoherent_single_value_list_is_still_dominant(self) -> None:
        """The ratio is unsound, but SPEC 2.2.7's rule needs no denominator."""

        assert classify([60], 5, exhaustive=True) == "dominant_value"

    def test_a_truncated_single_entry_list_proves_no_cardinality(self) -> None:
        """`top_n_values: 1` yields one entry without enumerating the column."""

        assert classify([60], 5, exhaustive=False) != "dominant_value"


class TestSummarize:
    """`summarize` (the `frequencies` field) over the same capped list `classify` reads."""

    def test_empty_counts_is_all_zero(self) -> None:
        result = summarize([])

        assert (result.top, result.bottom, result.listed, result.total) == (0, 0, 0, 0)

    def test_a_single_count_is_its_own_top_and_bottom(self) -> None:
        result = summarize([10])

        assert (result.top, result.bottom, result.listed, result.total) == (10, 10, 1, 10)

    def test_top_and_bottom_are_the_extremes_regardless_of_order(self) -> None:
        result = summarize([4, 9, 1, 6])

        assert (result.top, result.bottom, result.listed, result.total) == (9, 1, 4, 20)


class TestTheSharedPhaseBUsesThisFunction:
    """Every adapter's Phase B assembles through `adapters.base`, so a fix here reaches all eight."""

    def test_the_shared_assembly_carries_no_copy(self) -> None:
        from dbprint.adapters.base import classify_distribution as shared

        assert shared is classify


def test_a_single_row_single_value_list_is_dominant() -> None:
    assert classify([1], 1, exhaustive=True) == "dominant_value"


def test_a_tail_share_of_exactly_the_threshold_is_not_a_long_tail() -> None:
    assert classify([3], 10, exhaustive=False) == "uniform"


def test_an_exhaustive_list_with_one_non_zero_count_is_dominant() -> None:
    assert classify([5, 0], 10, exhaustive=True) == "dominant_value"


def test_a_count_of_one_is_a_non_zero_count() -> None:
    assert classify([3, 1], 4, exhaustive=True) == "imbalanced"
