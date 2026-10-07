"""Per-classification Notes synthesis templates."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from dbprint.engine import notes_synthesis
from dbprint.spec.scope import scope_of


def synthesize(column: dict[str, Any], fk_target: str | None = None, **kwargs: Any) -> str:
    """The Notes text alone for at most one edge; term keys are asserted in test_context_terms."""

    return notes_synthesis.synthesize(column, [fk_target] if fk_target else None, **kwargs).text


def _stats(classification: str, **fields: object) -> dict[str, object]:
    base: dict[str, object] = {
        "classification": classification,
        "null_rate": 0.0,
    }
    base.update(fields)

    return base


class TestCandidateKeySuffix:
    """`inferred.candidate_key` (SPEC 4.2) rides every classification as a suffix."""

    def test_absent_without_the_flag(self) -> None:
        assert synthesize(_stats("text", values=[])) == "text"

    def test_appended_when_the_flag_is_set(self) -> None:
        s = _stats(
            "text",
            values=[{"value": "a1b2", "count": 1}],
            inferred={"candidate_key": True},
        )
        assert synthesize(s) == "values (top 1, covering 100%): 'a1b2' (100%); candidate key"

    def test_names_the_exception(self) -> None:
        s = _stats(
            "text",
            values=[{"value": "a1b2", "count": 1}],
            inferred={"candidate_key": True, "candidate_key_exception": "measured_duplicates"},
        )
        assert synthesize(s) == (
            "values (top 1, covering 100%): 'a1b2' (100%); candidate key (measured duplicates)"
        )

    def test_rides_a_classification_other_than_text_too(self) -> None:
        s = _stats("numeric", range={"min": 1, "max": 9}, inferred={"candidate_key": True})

        assert synthesize(s) == "range: 1 -> 9; candidate key"


class TestBoolean:
    def test_true_false_counts(self) -> None:
        s = _stats(
            "boolean",
            values=[{"value": True, "count": 270}, {"value": False, "count": 10}],
        )
        assert synthesize(s) == "true: 270; false: 10"


class TestCategorical:
    def test_full_enum_when_exhaustive(self) -> None:
        s = _stats(
            "categorical",
            cardinality=3,
            values=[
                {"value": "a", "count": 5},
                {"value": "b", "count": 4},
                {"value": "c", "count": 1},
            ],
            values_coverage=1.0,
        )
        assert synthesize(s) == "values (complete): 'a' (50%), 'b' (40%), 'c' (10%)"

    def test_full_enum_above_the_old_count_limit(self) -> None:
        """Exhaustiveness, not a count, decides - an 8-value complete domain renders in full."""

        values = [{"value": f"v{i}", "count": 1} for i in range(8)]
        s = _stats("categorical", cardinality=8, values=values, values_coverage=1.0)
        out = synthesize(s)

        assert out == "values (complete): " + ", ".join(f"'v{i}' (12.5%)" for i in range(8))
        assert "..." not in out

    def test_shares_are_taken_against_the_column_not_the_listed_rows(self) -> None:
        s = _stats(
            "categorical",
            cardinality=40,
            values=[{"value": "a", "count": 100}, {"value": "b", "count": 100}],
            values_coverage=0.2,
        )

        # 100 of 1000 non-null rows, not 100 of the 200 listed.
        assert "'a' (10%)" in synthesize(s)

    def test_a_truncated_list_shows_its_top_5_and_the_share_they_cover(self) -> None:
        values = [{"value": f"v{i}", "count": 10 - i} for i in range(10)]
        s = _stats("categorical", cardinality=10, values=values, values_coverage=0.42)

        assert synthesize(s) == (
            "values (top 5, covering 30.5%): 'v0' (7.6%), 'v1' (6.9%), 'v2' (6.1%), "
            "'v3' (5.3%), 'v4' (4.6%)"
        )

    def test_an_empty_exhaustive_domain_shows_no_enumeration(self) -> None:
        s = _stats("categorical", cardinality=0, values=[], values_coverage=1.0)

        assert synthesize(s) == "values (complete): none"

    def test_a_truncated_list_mentions_no_entry_it_does_not_show(self) -> None:
        values = [{"value": f"v{i}", "count": 10 - i} for i in range(10)]
        s = _stats("categorical", cardinality=10, values=values, values_coverage=0.42)
        out = synthesize(s, statistics_params={"top_n_values": 3})

        assert "configured" not in out
        assert "total" not in out
        assert "..." not in out
        assert "distinct" not in out

    def test_no_top_n_values_note_when_the_list_is_exhaustive(self) -> None:
        s = _stats(
            "categorical",
            cardinality=2,
            values=[{"value": "a", "count": 1}, {"value": "b", "count": 1}],
            values_coverage=1.0,
        )

        assert "configured" not in synthesize(s, statistics_params={"top_n_values": 3})


class TestForeignKeyCandidate:
    def test_uses_supplied_target(self) -> None:
        s = _stats("foreign_key_candidate")
        assert synthesize(s, fk_target="herbarium.public.herbarium.id") == (
            "FK: herbarium.public.herbarium.id"
        )

    def test_no_target_falls_back(self) -> None:
        assert synthesize(_stats("foreign_key_candidate")) == "FK candidate"


class TestTemporal:
    def test_range_and_freshness(self) -> None:
        s = _stats(
            "temporal",
            range={"min": "2024-01-01", "max": "2026-06-08", "span_days": 889},
            percentiles={"p01": "2024-01-01", "p99": "2026-06-08"},
            freshness={"max_age_days": 1, "classification": "live"},
        )
        out = synthesize(s)
        assert "range: '2024-01-01' -> '2026-06-08' (889 days)" in out
        assert "freshness: live" in out

    def test_a_missing_freshness_block_publishes_no_age_verdict(self) -> None:
        """The three buckets are measurements; none of them means "not measured"."""

        s = _stats(
            "temporal",
            range={"min": "2024-01-01", "max": "2026-06-08", "span_days": 889},
            percentiles={"p01": "2024-01-01", "p99": "2026-06-08"},
        )

        assert synthesize(s) == "range: '2024-01-01' -> '2026-06-08' (889 days)"

    def test_percentiles_inside_the_range_ride_beside_the_true_bounds(self) -> None:
        s = _stats(
            "temporal",
            range={"min": "2001-01-01", "max": "2030-06-01", "span_days": 10743},
            percentiles={"p01": "2020-01-11", "p99": "2022-09-17"},
        )

        assert synthesize(s) == (
            "range: '2001-01-01' -> '2030-06-01' (10743 days); P1-P99: '2020-01-11' -> '2022-09-17'"
        )

    def test_one_differing_percentile_shows_the_whole_band(self) -> None:
        s = _stats(
            "temporal",
            range={"min": "2001-01-01", "max": "2030-06-01"},
            percentiles={"p01": "2001-01-01", "p99": "2022-09-17"},
        )

        assert (
            synthesize(s)
            == "range: '2001-01-01' -> '2030-06-01'; P1-P99: '2001-01-01' -> '2022-09-17'"
        )

    def test_percentiles_without_a_range_are_never_labelled_range(self) -> None:
        s = _stats("temporal", percentiles={"p01": "2020-01-11", "p99": "2022-09-17"})

        assert synthesize(s) == "P1-P99: '2020-01-11' -> '2022-09-17'"

    def test_a_column_with_nothing_measured_names_its_classification(self) -> None:
        """The fallback every other branch already has, so the cell is never blank."""

        assert synthesize(_stats("temporal")) == "temporal"


class TestNumeric:
    def test_range_and_median(self) -> None:
        s = _stats("numeric", range={"min": 0, "max": 100}, percentiles={"p50": 42})
        assert synthesize(s) == "range: 0 -> 100; P50: 42"

    def test_mean_follows_the_median(self) -> None:
        s = _stats("numeric", range={"min": 0, "max": 100}, percentiles={"p50": 42}, mean=51.5)
        assert synthesize(s) == "range: 0 -> 100; P50: 42; mean: 51.5"

    def test_mean_survives_redaction(self) -> None:
        """`mean` is an aggregate, not a cell value (SPEC 2.2.9): it stands beside the marker."""

        s = _stats(
            "numeric",
            range={"min": 0, "max": 100},
            percentiles={"p50": 42},
            mean=51.5,
            redacted="mask",
        )
        assert synthesize(s) == "redacted: mask; mean: 51.5"

    def test_zero_and_negative_counts_follow_mean(self) -> None:
        s = _stats(
            "numeric",
            range={"min": -10, "max": 100},
            percentiles={"p50": 42},
            mean=51.5,
            zero_count=6,
            negative_count=3,
        )
        assert synthesize(s) == "range: -10 -> 100; P50: 42; mean: 51.5; zeros: 6; negatives: 3"

    def test_a_zero_count_of_zero_is_silent(self) -> None:
        """A measured zero share is still worth stating; the absence of one is not."""

        s = _stats("numeric", range={"min": 0, "max": 100}, percentiles={"p50": 42}, zero_count=0)
        assert "zero" not in synthesize(s)


class TestText:
    def test_top_two_when_truncated(self) -> None:
        s = _stats(
            "text",
            values=[
                {"value": "a", "count": 200},
                {"value": "b", "count": 150},
                {"value": "c", "count": 80},
            ],
            values_coverage=0.86,
        )
        assert synthesize(s) == "values (top 3, covering 86%): 'a' (40%), 'b' (30%), 'c' (16%)"

    def test_full_enum_when_exhaustive_takes_the_categorical_shape(self) -> None:
        """One criterion, one shape - `top:` says the opposite of a complete domain."""

        values = [{"value": f"v{i}", "count": 1} for i in range(6)]
        s = _stats("text", cardinality=6, values=values, values_coverage=1.0)
        out = synthesize(s)

        assert out == "values (complete): " + ", ".join(f"'v{i}' (16.7%)" for i in range(6))
        assert "top:" not in out

    def test_empty_count_follows_the_top_list(self) -> None:
        s = _stats(
            "text",
            values=[{"value": "a", "count": 200}, {"value": "b", "count": 150}],
            values_coverage=0.86,
            empty_count=40,
        )
        assert (
            synthesize(s)
            == "values (top 2, covering 86%): 'a' (49.1%), 'b' (36.9%); empty strings: 40"
        )

    def test_empty_count_reaches_a_prose_column_with_no_value_list(self) -> None:
        """SPEC 2.2.3's prose exemption drops the value list, not the census beside it."""

        s = _stats("text", values=[], empty_count=12)
        assert synthesize(s) == "text; empty strings: 12"

    def test_prose_publishes_no_list_regardless_of_coverage(self) -> None:
        """A prose column carries no `values` at all (SPEC 2.2.3 footnote), coverage or not."""

        assert synthesize(_stats("text", values=[], values_coverage=1.0)) == "text"

    def test_a_truncated_text_list_names_no_configured_limit(self) -> None:
        s = _stats(
            "text",
            values=[{"value": "a", "count": 200}, {"value": "b", "count": 150}],
            values_coverage=0.86,
        )

        assert "configured" not in synthesize(s, statistics_params={"top_n_values": 2})


class TestJson:
    def test_label_only(self) -> None:
        assert synthesize(_stats("json")) == "json"


class TestBinary:
    def test_the_length_reads_as_bytes(self) -> None:
        s = _stats(
            "binary",
            sql_type="bytea",
            length={"min": 16, "max": 16, "avg": 16.0, "p95": 16.0},
        )

        assert synthesize(s) == "binary; length: 16 -> 16 bytes (avg 16.0)"

    def test_a_binary_key_reads_its_length_as_bytes_too(self) -> None:
        s = _stats(
            "foreign_key_candidate",
            sql_type="BINARY(16)",
            length={"min": 16, "max": 16, "avg": 16.0, "p95": 16.0},
        )

        assert synthesize(s) == "FK candidate; length: 16 -> 16 bytes (avg 16.0)"


class TestSpatial:
    def test_kinds_srids_and_extent(self) -> None:
        s = _stats(
            "spatial",
            sql_type="geometry",
            geometry={
                "kinds": [{"kind": "point", "count": 39}, {"kind": "polygon", "count": 6}],
                "srids": [{"srid": 4326, "count": 45}],
                "dimensions": [{"dimensions": "xy", "count": 45}],
                "empty_count": 1,
            },
            extent={"min_x": 16.601, "min_y": 49.2, "max_x": 16.649, "max_y": 49.206},
        )

        assert synthesize(s) == (
            "spatial; point: 39; polygon: 6; srid: 4326; extent x: 16.601 -> 16.649; "
            "extent y: 49.2 -> 49.206"
        )

    def test_a_withheld_extent_leaves_the_geometry(self) -> None:
        s = _stats(
            "spatial",
            sql_type="geometry",
            redacted="mask",
            geometry={
                "kinds": [{"kind": "point", "count": 3}],
                "dimensions": [{"dimensions": "xy", "count": 3}],
                "empty_count": 0,
            },
        )

        assert synthesize(s).startswith("spatial; point: 3")
        assert "extent" not in synthesize(s)


class TestVector:
    def test_unit_normalized_embeddings_name_the_cheaper_operator(self) -> None:
        s = _stats(
            "vector",
            sql_type="vector(768)",
            dimension={"min": 768, "max": 768},
            norm={"min": 0.999999, "max": 1.000001},
            zero_count=0,
        )

        assert synthesize(s) == ("vector; dimension: 768; unit-normalized")

    def test_mixed_dimensions_and_zero_vectors(self) -> None:
        s = _stats(
            "vector",
            sql_type="vector",
            dimension={"min": 384, "max": 1536},
            norm={"min": 0.4, "max": 12.0},
            zero_count=3,
        )

        assert synthesize(s) == "vector; mixed dimension: 384 -> 1536; zero vectors: 3"


class TestUnsupported:
    def test_shows_sql_type(self) -> None:
        s = _stats("unsupported", sql_type="bytea")
        assert synthesize(s) == "bytea"


class TestTheNullsFact:
    """A nullable column states its measured null share or `none`; a NOT NULL one, nothing."""

    def test_a_share_under_one_percent_is_stated(self) -> None:
        s = _stats("numeric", nullable=True, null_count=8, null_rate=0.004)

        assert synthesize(s).endswith("; nulls: 0.4%")
        assert "nullable" not in synthesize(s)

    def test_a_larger_share_is_stated(self) -> None:
        s = _stats("numeric", nullable=True, null_count=400, null_rate=0.2)

        assert synthesize(s) == "numeric; nulls: 20%"

    def test_a_nullable_column_with_no_nulls_says_none(self) -> None:
        assert synthesize(_stats("numeric", nullable=True, null_count=0)) == "numeric; nulls: none"

    def test_a_scoped_none_carries_the_clause(self) -> None:
        scope = scope_of({"scope": {"rows_scanned": 90, "sample": 0.1}})
        s = _stats("numeric", nullable=True, null_count=0)

        assert synthesize(s, scope=scope) == "numeric; nulls: none over the rows scanned"

    def test_a_share_near_every_row_never_reads_as_every_row(self) -> None:
        s = _stats("numeric", nullable=True, null_count=9996, null_rate=0.9996)

        assert synthesize(s) == "numeric; nulls: 99.96%"

    def test_a_not_null_column_states_nothing(self) -> None:
        assert synthesize(_stats("numeric", nullable=False, null_count=0)) == "numeric"

    def test_a_not_null_column_that_measured_nulls_shows_them(self) -> None:
        s = _stats("numeric", nullable=False, null_count=3, null_rate=0.03)

        assert synthesize(s) == "numeric; nulls: 3%"

    def test_a_part_states_a_share_of_its_occurrences_and_never_none(self) -> None:
        part = {"classification": "numeric", "occurrences": 10, "null_count": 0, "null_rate": 0.0}

        assert "nulls" not in synthesize(part)

    def test_an_unmeasured_null_count_states_nothing(self) -> None:
        s = _stats("numeric", nullable=True, unmeasured=["null_count", "null_rate"])
        s.pop("null_rate")

        assert synthesize(s) == "numeric; unmeasured: null_count, null_rate"


class TestDistribution:
    """`distribution` (SPEC 2.2.5) reaches every cell schema-required to carry it, redacted or not."""

    def test_numeric_carries_the_shape_word(self) -> None:
        s = _stats(
            "numeric",
            range={"min": 0, "max": 100},
            percentiles={"p50": 42},
            distribution="imbalanced",
        )
        assert synthesize(s) == "range: 0 -> 100; P50: 42; distribution: imbalanced"

    def test_numeric_absent_when_not_measured(self) -> None:
        s = _stats("numeric", range={"min": 0, "max": 100}, percentiles={"p50": 42})
        assert "imbalanced" not in synthesize(s)
        assert "uniform" not in synthesize(s)

    def test_numeric_survives_redaction(self) -> None:
        s = _stats(
            "numeric",
            range={"min": 0, "max": 100},
            percentiles={"p50": 42},
            distribution="long_tail",
            redacted="mask",
        )
        assert synthesize(s) == "redacted: mask; distribution: long tail"

    def test_temporal_carries_the_shape_word(self) -> None:
        s = _stats(
            "temporal",
            range={"min": "2024-01-01", "max": "2026-06-08", "span_days": 889},
            percentiles={"p01": "2024-01-01", "p99": "2026-06-08"},
            distribution="dominant_value",
        )
        out = synthesize(s)
        assert out.endswith("; distribution: dominant value")

    def test_categorical_carries_the_shape_word(self) -> None:
        s = _stats(
            "categorical",
            cardinality=2,
            values=[{"value": "a", "count": 1}, {"value": "b", "count": 1}],
            values_coverage=1.0,
            distribution="imbalanced",
        )
        assert synthesize(s) == "values (complete): 'a' (50%), 'b' (50%); distribution: imbalanced"

    def test_categorical_absent_when_not_measured(self) -> None:
        s = _stats(
            "categorical",
            cardinality=2,
            values=[{"value": "a", "count": 1}, {"value": "b", "count": 1}],
            values_coverage=1.0,
        )
        assert "imbalanced" not in synthesize(s)

    def test_categorical_survives_redaction(self) -> None:
        s = _stats(
            "categorical",
            cardinality=2,
            values=[{"value": "a", "count": 1}, {"value": "b", "count": 1}],
            values_coverage=1.0,
            distribution="uniform",
            redacted="mask",
        )
        assert synthesize(s) == (
            "redacted: mask; values (complete): withheld (50%), withheld (50%); distribution: uniform"
        )

    def test_text_carries_the_shape_word(self) -> None:
        s = _stats(
            "text",
            values=[{"value": "a", "count": 200}, {"value": "b", "count": 150}],
            values_coverage=0.86,
            distribution="long_tail",
        )
        assert synthesize(s) == (
            "values (top 2, covering 86%): 'a' (49.1%), 'b' (36.9%); distribution: long tail"
        )

    def test_text_absent_when_not_measured(self) -> None:
        s = _stats(
            "text",
            values=[{"value": "a", "count": 200}, {"value": "b", "count": 150}],
            values_coverage=0.86,
        )
        assert "long tail" not in synthesize(s)

    def test_text_survives_redaction(self) -> None:
        s = _stats(
            "text",
            values=[{"value": "a", "count": 200}, {"value": "b", "count": 150}],
            values_coverage=0.86,
            distribution="imbalanced",
            redacted="drop",
        )
        assert synthesize(s) == (
            "redacted: drop; values (top 2, covering 86%): withheld (49.1%), withheld (36.9%); "
            "distribution: imbalanced"
        )

    def test_fk_candidate_carries_only_the_shape_word(self) -> None:
        """The one word beside the target, not the full categorical treatment."""

        s = _stats("foreign_key_candidate", distribution="imbalanced")
        assert synthesize(s, fk_target="public.a.id") == "FK: public.a.id; distribution: imbalanced"

    def test_fk_candidate_absent_when_not_measured(self) -> None:
        s = _stats("foreign_key_candidate")
        assert synthesize(s, fk_target="public.a.id") == "FK: public.a.id"

    def test_fk_candidate_hints_only_mode_is_unaffected(self) -> None:
        """The docs site already renders `distribution` as its own badge - not repeated here."""

        s = _stats("foreign_key_candidate", distribution="imbalanced")
        out = synthesize(s, fk_target="public.a.id", hints_only=True)

        assert "imbalanced" not in out
        assert out == "FK: public.a.id"


class TestLooksLikeSuffix:
    def test_appended_when_detected(self) -> None:
        s = _stats(
            "text",
            values=[{"value": "a@b.com", "count": 1}],
            inferred={"looks_like": "email"},
        )
        assert synthesize(s).endswith("; looks like: email")

    def test_absent_when_nothing_matched(self) -> None:
        s = _stats("text", values=[{"value": "x", "count": 1}])
        assert "looks like" not in synthesize(s)

    def test_survives_redaction(self) -> None:
        """SPEC 2.2.9: detection runs over values that are never persisted."""

        s = _stats(
            "text",
            values=[{"value": None, "count": 1}],
            inferred={"looks_like": "email"},
            redacted="drop",
        )
        assert "looks like: email" in synthesize(s)

    def test_looks_like_sample_size_rides_the_verdict(self) -> None:
        s = _stats(
            "text",
            values=[{"value": "a@b.com", "count": 1}],
            inferred={"looks_like": "email"},
        )
        out = synthesize(s, statistics_params={"looks_like_sample_size": 500})

        assert out.endswith("; looks like: email (drawn 500)")

    def test_no_sample_size_note_without_configured_params(self) -> None:
        s = _stats(
            "text",
            values=[{"value": "a@b.com", "count": 1}],
            inferred={"looks_like": "email"},
        )
        assert synthesize(s).endswith("; looks like: email")

    def test_the_evidence_a_verdict_rests_on_rides_beside_it(self) -> None:
        """The same `sampled`/`matched` pair `search_columns` already carries (SPEC 4.1.3)."""

        s = _stats(
            "text",
            values=[{"value": "a@b.com", "count": 1}],
            inferred={"looks_like": "email", "sampled": 1000, "matched": 998},
        )
        assert synthesize(s).endswith("; looks like: email (998 of 1000 sampled)")

    def test_a_weak_verdict_is_distinguishable_from_a_strong_one(self) -> None:
        weak = _stats(
            "text",
            values=[{"value": "a@b.com", "count": 1}],
            inferred={"looks_like": "email", "sampled": 1000, "matched": 3},
        )
        strong = _stats(
            "text",
            values=[{"value": "a@b.com", "count": 1}],
            inferred={"looks_like": "email", "sampled": 1000, "matched": 998},
        )
        assert "3 of 1000 sampled" in synthesize(weak)
        assert "998 of 1000 sampled" in synthesize(strong)
        assert synthesize(weak) != synthesize(strong)

    def test_evidence_and_the_configured_cap_ride_together(self) -> None:
        s = _stats(
            "text",
            values=[{"value": "a@b.com", "count": 1}],
            inferred={"looks_like": "email", "sampled": 500, "matched": 480},
        )
        out = synthesize(s, statistics_params={"looks_like_sample_size": 500})

        assert out.endswith("; looks like: email (480 of 500 sampled, 500 configured)")


class TestLooksLikeCandidateSuffix:
    """SPEC 4.1.3's near-miss: `looks_like_candidate_share` is a share of the sample, never a
    row-grain figure, so the sentence must name its population.
    """

    def test_the_share_names_its_population_as_sampled_values(self) -> None:
        s = _stats(
            "text",
            values=[{"value": "x", "count": 1}],
            inferred={"looks_like_candidate": "email", "looks_like_candidate_share": 0.53},
        )
        assert synthesize(s).endswith("; near: email (53% of sampled values, no verdict)")

    def test_worded_near_never_looks_like(self) -> None:
        """A reader scanning for a verdict must not mistake this for one (SPEC 4.1.3)."""

        s = _stats(
            "text",
            values=[{"value": "x", "count": 1}],
            inferred={"looks_like_candidate": "email", "looks_like_candidate_share": 0.53},
        )
        assert "looks like" not in synthesize(s)


class TestCoverageMethodSuffix:
    """SPEC 2.2.4: `bounded` means the coverage figure is a clamp, not a raw measurement."""

    def test_bounded_is_stated(self) -> None:
        s = _stats(
            "categorical",
            cardinality=2,
            values=[{"value": "a", "count": 1}, {"value": "b", "count": 1}],
            values_coverage=1.0,
            values_coverage_method="bounded",
        )
        assert synthesize(s).endswith("; coverage bounded")

    def test_measured_is_silent(self) -> None:
        s = _stats(
            "categorical",
            cardinality=2,
            values=[{"value": "a", "count": 1}, {"value": "b", "count": 1}],
            values_coverage=1.0,
            values_coverage_method="measured",
        )
        assert "bounded" not in synthesize(s)

    def test_dropped_in_hints_only_mode(self) -> None:
        """The docs site carries its own `coverage_method` badge; notes should not repeat it."""

        s = _stats(
            "categorical",
            cardinality=2,
            values=[{"value": "a", "count": 1}, {"value": "b", "count": 1}],
            values_coverage=1.0,
            values_coverage_method="bounded",
            inferred={"candidate_key": True},
        )
        out = synthesize(s, hints_only=True)

        assert "candidate key" in out
        assert "bounded" not in out


class TestSensitivitySuffix:
    def test_phrased_as_a_detection(self) -> None:
        s = _stats(
            "categorical",
            cardinality=0,
            values=[],
            values_coverage=1.0,
            inferred={"sensitivity": "contact"},
        )
        assert synthesize(s).endswith("; detected: contact")

    def test_absent_when_nothing_detected(self) -> None:
        s = _stats("categorical", cardinality=0, values=[], values_coverage=1.0)
        assert "detected" not in synthesize(s)


class TestEpochUnitSuffix:
    def test_appended_when_detected(self) -> None:
        s = _stats("numeric", range={"min": 1, "max": 9}, inferred={"epoch_unit": "seconds"})
        assert synthesize(s).endswith("; epoch: seconds")

    def test_absent_when_not_detected(self) -> None:
        s = _stats("numeric", range={"min": 1, "max": 9})
        assert "epoch" not in synthesize(s)


class TestUnmeasuredSuffix:
    """SPEC 2.2.4: the one absence a reader must not take as a property of the column."""

    def test_names_the_fields_the_run_could_not_obtain(self) -> None:
        s = _stats("temporal", unmeasured=["distribution", "frequencies", "values"])

        assert "; unmeasured: distribution, frequencies, values" in synthesize(s)

    def test_it_leads_the_other_qualifiers(self) -> None:
        """It says the rest of the line describes a partial read, so it cannot trail one."""

        s = _stats(
            "temporal",
            null_rate=0.25,
            null_count=25,
            unmeasured=["distribution"],
        )
        line = synthesize(s)

        assert line.index("unmeasured") < line.index("null")

    def test_absent_when_every_read_answered(self) -> None:
        assert "unmeasured" not in synthesize(_stats("temporal", range={"min": "a", "max": "b"}))


class TestUnrepresentableSuffix:
    def test_names_the_affected_fields(self) -> None:
        s = _stats(
            "temporal",
            range={"min": "1970-01-01", "max": "52030-01-01", "span_days": 15376234},
            unrepresentable=["max"],
        )
        assert synthesize(s).endswith("; unrepresentable: max")

    def test_absent_when_every_bound_is_representable(self) -> None:
        s = _stats("temporal", range={"min": "2024-01-01", "max": "2024-06-08", "span_days": 159})
        assert "unrepresentable" not in synthesize(s)


class TestPhysicalLayoutKeySuffix:
    def test_suffix_added_when_marked(self) -> None:
        s = _stats(
            "numeric",
            range={"min": 0, "max": 1},
            percentiles={"p50": 0},
            physical_layout_key=True,
        )
        assert synthesize(s).endswith("; cluster/partition key")

    def test_no_suffix_when_unmarked(self) -> None:
        s = _stats("numeric", range={"min": 0, "max": 1}, percentiles={"p50": 0})
        assert "cluster/partition key" not in synthesize(s)

    def test_suffix_precedes_the_null_rate_suffix(self) -> None:
        s = _stats(
            "numeric",
            range={"min": 0, "max": 1},
            percentiles={"p50": 0},
            physical_layout_key=True,
            null_rate=0.123,
            null_count=123,
        )
        assert synthesize(s).endswith("; cluster/partition key; nulls: 12.3%")


class TestARedactedColumnIsNotRenderedAsMeasured:
    """SPEC 2.2.9: a redacted value must never render as a measured NULL or zero."""

    @pytest.mark.parametrize("primitive", ["mask", "drop", "hash"])
    def test_a_populated_boolean_never_reports_zero_of_both(self, primitive: str) -> None:
        s = _stats(
            "boolean",
            redacted=primitive,
            values=[{"count": 270}, {"count": 10}],
        )
        out = synthesize(s)

        assert "true: 0" not in out
        assert "withheld (270), withheld (10)" in out

    @pytest.mark.parametrize("primitive", ["mask", "drop", "hash"])
    def test_the_cell_names_the_primitive(self, primitive: str) -> None:
        s = _stats("boolean", redacted=primitive, values=[{"count": 3}, {"count": 1}])

        assert f"redacted: {primitive}" in synthesize(s)

    @pytest.mark.parametrize("primitive", ["mask", "drop", "hash"])
    def test_a_categorical_shows_counts_and_no_literal(
        self,
        primitive: str,
    ) -> None:
        s = _stats(
            "categorical",
            redacted=primitive,
            cardinality=5,
            values=[{"count": 5}, {"count": 4}, {"count": 1}],
        )
        out = synthesize(s)

        assert out == (
            f"redacted: {primitive}; values (top 3, covering 100%): withheld (50%), "
            "withheld (40%), withheld (10%)"
        )
        assert "NULL" not in out

    @pytest.mark.parametrize("primitive", ["mask", "drop", "hash"])
    def test_a_text_column_shows_counts_without_fabricated_literals(self, primitive: str) -> None:
        s = _stats("text", redacted=primitive, values=[{"count": 412}, {"count": 98}])
        out = synthesize(s)

        assert "NULL" not in out
        assert "withheld (80.8%), withheld (19.2%)" in out

    @pytest.mark.parametrize("primitive", ["mask", "hash"])
    def test_substituted_bounds_are_not_presented_as_a_range(self, primitive: str) -> None:
        """A masked maximum still looks like a maximum; SPEC 2.2.9 forbids ordering it."""

        substitute = "[redacted]" if primitive == "mask" else "3f2a9c1e"
        s = _stats(
            "temporal",
            redacted=primitive,
            range={"min": substitute, "max": substitute, "span_days": 889},
            percentiles={"p01": substitute, "p99": substitute},
            freshness={"max_age_days": 1, "classification": "live"},
        )
        out = synthesize(s)

        assert "range" not in out
        assert substitute not in out

    def test_a_temporal_column_keeps_the_measurements_redaction_leaves_true(self) -> None:
        s = _stats(
            "temporal",
            redacted="mask",
            range={"min": "[redacted]", "max": "[redacted]", "span_days": 889},
            freshness={"max_age_days": 1, "classification": "live"},
        )
        out = synthesize(s)

        assert "span: 889 days" in out
        assert "freshness: live" in out

    @pytest.mark.parametrize("primitive", ["mask", "hash"])
    def test_a_numeric_column_shows_no_substituted_bound(self, primitive: str) -> None:
        substitute = "[redacted]" if primitive == "mask" else "b70d5518"
        s = _stats(
            "numeric",
            redacted=primitive,
            range={"min": substitute, "max": substitute},
            percentiles={"p50": substitute},
        )
        out = synthesize(s)

        assert substitute not in out
        assert "range" not in out

    def test_no_dropped_value_ever_renders_as_null(self) -> None:
        """An entry with no `value` key means dropped, not null - the two must stay distinct."""

        for classification in ("boolean", "categorical", "text"):
            s = _stats(
                classification,
                redacted="drop",
                cardinality=2,
                values=[{"count": 9}, {"count": 8}],
            )

            assert "NULL" not in synthesize(s), classification

    def test_the_null_rate_suffix_still_renders(self) -> None:
        """`null_rate` is untouched by redaction and is the one place NULL belongs."""

        s = _stats(
            "categorical",
            redacted="drop",
            cardinality=3,
            values=[{"count": 5}],
            null_rate=0.25,
            null_count=25,
        )

        assert synthesize(s).endswith("; nulls: 25%")

    def test_a_redacted_column_still_reports_its_candidate_key(self) -> None:
        """SPEC 2.2.9: detection describes the column, not the emitted literals."""

        s = _stats(
            "text",
            redacted="hash",
            values=[{"count": 1}],
            inferred={"candidate_key": True},
        )

        assert synthesize(s).endswith("; candidate key")

    @pytest.mark.parametrize(
        ("classification", "fields"),
        [
            ("boolean", {"values": [{"value": True, "count": 270}, {"value": False, "count": 10}]}),
            (
                "categorical",
                {"cardinality": 3, "values": [{"value": "a", "count": 5}]},
            ),
            ("text", {"values": [{"value": "a", "count": 200}]}),
            ("numeric", {"range": {"min": 0, "max": 100}, "percentiles": {"p50": 42}}),
        ],
    )
    def test_an_unredacted_column_is_untouched(
        self,
        classification: str,
        fields: dict[str, object],
    ) -> None:
        """The marker gates every branch; a column carrying none renders unmodified."""

        plain = synthesize(_stats(classification, **fields))
        marked = synthesize(_stats(classification, redacted="mask", **fields))

        assert plain != marked
        assert "redacted" not in plain

    def test_a_malformed_marker_is_not_rendered_as_a_primitive(self) -> None:
        """The marker comes from parsed YAML, which a hand can edit."""

        s = _stats("categorical", redacted=True, cardinality=2, values=[{"value": "a", "count": 2}])

        assert "redacted" not in synthesize(s)


class TestHintsOnly:
    """`hints_only=True` (dbprint docs) drops what a caller's own cells already show."""

    def test_drops_the_classification_dispatched_base(self) -> None:
        s = _stats("numeric", range={"min": 0, "max": 100}, percentiles={"p50": 42})

        assert synthesize(s, hints_only=True) == ""

    def test_drops_null_rate(self) -> None:
        s = _stats("categorical", cardinality=3, values=[{"count": 5}], null_rate=0.25)

        assert "null" not in synthesize(s, hints_only=True)

    def test_drops_unrepresentable(self) -> None:
        s = _stats("temporal", unrepresentable=["max"])

        assert "unrepresentable" not in synthesize(s, hints_only=True)

    def test_keeps_the_fk_target(self) -> None:
        s = _stats("foreign_key_candidate")

        assert synthesize(s, "specimen_loan.id (declared)", hints_only=True) == (
            "FK: specimen_loan.id (declared)"
        )

    def test_keeps_candidate_key_with_no_leading_comma(self) -> None:
        s = _stats("numeric", range={"min": 1, "max": 9}, inferred={"candidate_key": True})

        assert synthesize(s, hints_only=True) == "candidate key"

    def test_keeps_looks_like_and_sensitivity_together(self) -> None:
        s = _stats(
            "text",
            values=[{"count": 1}],
            inferred={"looks_like": "email", "sensitivity": "contact"},
        )

        assert synthesize(s, hints_only=True) == "looks like: email; detected: contact"

    def test_a_plain_column_with_no_hints_is_empty(self) -> None:
        s = _stats("numeric", range={"min": 0, "max": 9}, percentiles={"p50": 4}, null_rate=0.1)

        assert synthesize(s, hints_only=True) == ""


class TestSpellingGroups:
    """SPEC 2.2.4: a group is one category, on a sampled list as much as an exhaustive one."""

    def test_an_exhaustive_list_folds_the_member_into_its_canonical(self) -> None:
        s = _stats(
            "categorical",
            cardinality=3,
            values=[
                {"value": "Active", "count": 90},
                {"value": "Retired", "count": 50},
                {"value": "ACTIVE", "count": 10, "spelling_of": "Active"},
            ],
            values_coverage=1.0,
        )

        assert (
            synthesize(s) == "values (complete): 'Active' (66.7%, 2 spellings), 'Retired' (33.3%)"
        )

    def test_a_sampled_list_marks_the_group_too(self) -> None:
        s = _stats(
            "categorical",
            cardinality=40,
            rows_scanned=200,
            values=[
                {"value": "Active", "count": 90},
                {"value": "Retired", "count": 50},
                {"value": "ACTIVE", "count": 10, "spelling_of": "Active"},
            ],
            values_coverage=0.75,
        )
        notes = synthesize(s)

        assert "'Active' (50%, 2 spellings)" in notes
        assert "ACTIVE" not in notes.replace("Active", "")

    def test_a_spelling_whose_canonical_is_not_listed_stands_as_its_own_value(self) -> None:
        s = _stats(
            "categorical",
            cardinality=3,
            values=[
                {"value": "Active", "count": 80},
                {"value": "Retired", "count": 15},
                {"value": "dormant", "count": 5, "spelling_of": "Dormant"},
            ],
            values_coverage=1.0,
        )

        assert synthesize(s) == "values (complete): 'Active' (80%), 'Retired' (15%), 'dormant' (5%)"


class TestScopedClaims:
    """Each claim one unread row could falsify carries the clause; measurements do not."""

    _SCOPE = scope_of({"scope": {"rows_scanned": 90, "sample": 0.1}})

    def test_a_complete_list_carries_the_clause(self) -> None:
        column = {
            "classification": "categorical",
            "cardinality": 2,
            "values": [{"value": "open", "count": 60}, {"value": "closed", "count": 30}],
            "values_coverage": 1.0,
        }

        assert synthesize(column, scope=self._SCOPE).startswith(
            "values (complete over the rows scanned): 'open' (66.7%), 'closed' (33.3%)",
        )
        assert synthesize(column).startswith("values (complete): 'open' (66.7%), 'closed' (33.3%)")

    def test_a_candidate_key_carries_the_clause(self) -> None:
        column = {"classification": "numeric", "inferred": {"candidate_key": True}}

        assert "candidate key over the rows scanned" in synthesize(column, scope=self._SCOPE)

    def test_a_freshness_verdict_carries_the_clause_and_a_range_does_not(self) -> None:
        column = {
            "classification": "temporal",
            "range": {"min": "2020-01-01", "max": "2020-02-01"},
            "freshness": {"classification": "dormant", "max_age_days": 900},
        }
        note = synthesize(column, scope=self._SCOPE)

        assert "freshness: dormant over the rows scanned" in note
        assert "range: '2020-01-01' -> '2020-02-01'" in note


class TestTheFactGrammar:
    """Facts split on `; `, list entries on `, `; every string or timestamp is an SQL literal."""

    def test_separators_and_quotes_inside_a_value_stay_inside_its_literal(self) -> None:
        s = _stats(
            "categorical",
            cardinality=4,
            values=[
                {"value": "collector's pick", "count": 1},
                {"value": "sub; species", "count": 1},
                {"value": "variety, wild", "count": 1},
                {"value": "rank: form", "count": 1},
            ],
            values_coverage=1.0,
        )

        assert synthesize(s) == (
            "values (complete): 'collector''s pick' (25%), 'sub; species' (25%), "
            "'variety, wild' (25%), 'rank: form' (25%)"
        )

    def test_a_timestamp_loaded_as_a_datetime_keeps_the_artifact_spelling(self) -> None:
        s = _stats(
            "temporal",
            range={
                "min": datetime(2024, 1, 3, tzinfo=UTC),
                "max": datetime(2024, 10, 23, tzinfo=UTC),
            },
        )

        assert synthesize(s) == "range: '2024-01-03T00:00:00Z' -> '2024-10-23T00:00:00Z'"

    def test_a_control_character_is_double_quoted_with_escapes(self) -> None:
        s = _stats("text", values=[{"value": "dry\nseed", "count": 3}], values_coverage=0.5)

        assert synthesize(s) == 'values (top 1, covering 50%): "dry\\nseed" (50%)'

    def test_a_numeric_string_reads_apart_from_the_number(self) -> None:
        s = _stats(
            "categorical",
            cardinality=2,
            values=[{"value": "10", "count": 2}, {"value": 10, "count": 1}],
            values_coverage=1.0,
        )

        assert synthesize(s) == "values (complete): '10' (66.7%), 10 (33.3%)"

    @pytest.mark.parametrize(
        ("fields", "fact"),
        [
            ({"distribution": "long_tail"}, "distribution: long tail"),
            ({"distribution": "dominant_value"}, "distribution: dominant value"),
            ({"inferred": {"looks_like": "country_code"}}, "looks like: country code"),
            ({"inferred": {"sensitivity": "personal_name"}}, "detected: personal name"),
            (
                {
                    "inferred": {
                        "candidate_key": True,
                        "candidate_key_exception": "measured_duplicates",
                    },
                },
                "candidate key (measured duplicates)",
            ),
            ({"inferred": {"epoch_unit": "milliseconds"}}, "epoch: milliseconds"),
            (
                {
                    "inferred": {
                        "looks_like_candidate": "postal_code",
                        "looks_like_candidate_share": 0.5,
                    },
                },
                "near: postal code (50% of sampled values, no verdict)",
            ),
        ],
    )
    def test_an_enum_value_prints_as_words(self, fields: dict[str, object], fact: str) -> None:
        facts = synthesize(_stats("numeric", range={"min": 1, "max": 9}, **fields)).split("; ")

        assert fact in facts

    def test_a_freshness_verdict_prints_as_a_word(self) -> None:
        s = _stats("temporal", freshness={"classification": "live", "max_age_days": 1})

        assert synthesize(s) == "freshness: live"

    def test_a_percentile_field_takes_its_label_in_a_field_list(self) -> None:
        s = _stats("temporal", unrepresentable=["max", "p99", "p01"])

        assert synthesize(s).endswith("; unrepresentable: max, P99, P1")


class TestEveryEdgeOfAColumn:
    """The FK fact lists each edge a human left standing, on whatever classification carries it."""

    def test_several_edges_form_one_list_in_the_order_given(self) -> None:
        targets = ["public.herbarium.id (declared)", "public.vault.id (measured)"]
        s = _stats("numeric", range={"min": 1, "max": 9})

        assert notes_synthesis.synthesize(s, targets).text == (
            "FK: public.herbarium.id (declared), public.vault.id (measured); range: 1 -> 9"
        )

    def test_a_composite_entry_stays_one_entry(self) -> None:
        targets = ["(site_id, plot_no) -> public.plot.(site_id, plot_no) (declared)"]

        assert notes_synthesis.synthesize(_stats("foreign_key_candidate"), targets).text == (
            "FK: (site_id, plot_no) -> public.plot.(site_id, plot_no) (declared)"
        )

    def test_a_column_of_another_classification_with_no_edge_states_none(self) -> None:
        assert (
            "FK"
            not in notes_synthesis.synthesize(_stats("numeric", range={"min": 1, "max": 9})).text
        )


class TestADominantValueIsNamed:
    """`dominant_value` names the value and its share of the non-null scanned rows."""

    def test_on_a_categorical_column(self) -> None:
        s = _stats(
            "categorical",
            cardinality=2,
            values=[{"value": "species", "count": 95}, {"value": "genus", "count": 5}],
            values_coverage=1.0,
            distribution="dominant_value",
        )

        assert synthesize(s).endswith("; distribution: dominant value 'species' (95%)")

    def test_on_a_numeric_column_over_the_table_row_count(self) -> None:
        s = _stats(
            "numeric",
            null_count=0,
            values=[{"value": 0, "count": 1920}, {"value": 7, "count": 3}],
            distribution="dominant_value",
        )

        out = notes_synthesis.synthesize(s, row_count=2000).text

        assert out.endswith("; distribution: dominant value 0 (96%)")

    def test_a_redacted_column_names_no_literal(self) -> None:
        s = _stats(
            "categorical",
            cardinality=2,
            redacted="mask",
            values=[{"count": 1950}, {"count": 50}],
            values_coverage=1.0,
            distribution="dominant_value",
        )

        assert synthesize(s) == (
            "redacted: mask; values (complete): withheld (97.5%), withheld (2.5%); "
            "distribution: dominant value withheld (97.5%)"
        )

    def test_a_redacted_column_with_no_list_still_names_its_primitive(self) -> None:
        s = _stats("categorical", cardinality=40, redacted="hash")

        assert synthesize(s).startswith("redacted: hash; distinct: 40")

    def test_a_redacted_fk_candidate_names_its_primitive_and_withholds_its_list(self) -> None:
        s = _stats(
            "foreign_key_candidate",
            cardinality=2,
            redacted="mask",
            values=[{"count": 1950}, {"count": 50}],
            values_coverage=1.0,
            distribution="dominant_value",
        )

        assert synthesize(s).startswith(
            "FK candidate; redacted: mask; values (complete): withheld (97.5%), withheld (2.5%)",
        )


class TestATruncatedListOnEveryClassification:
    def test_a_numeric_frequency_list_shows_its_top_values(self) -> None:
        s = _stats(
            "numeric",
            range={"min": 0, "max": 59},
            null_count=200,
            null_rate=0.2,
            values=[{"value": v, "count": 14} for v in range(21, 27)],
        )

        assert notes_synthesis.synthesize(s, row_count=1000).text == (
            "range: 0 -> 59; values (top 5, covering 8.8%): 21 (1.8%), 22 (1.8%), 23 (1.8%), "
            "24 (1.8%), 25 (1.8%); nulls: 20%"
        )


class TestTheCensusAsShares:
    def test_each_member_is_a_share_of_the_non_null_scanned_rows(self) -> None:
        s = _stats(
            "numeric",
            sql_type="double precision",
            null_count=0,
            zero_count=100,
            negative_count=60,
            quantized_count=560,
        )

        assert notes_synthesis.synthesize(s, row_count=2000).text == (
            "zeros: 5%; negatives: 3%; whole numbers: 28%"
        )

    def test_an_integer_type_states_no_whole_numbers(self) -> None:
        s = _stats("numeric", sql_type="NUMBER(38,0)", null_count=0, quantized_count=2000)

        assert "whole numbers" not in notes_synthesis.synthesize(s, row_count=2000).text

    def test_a_temporal_census_counts_midnights(self) -> None:
        s = _stats("temporal", null_count=1000, null_rate=0.5, quantized_count=200)

        assert notes_synthesis.synthesize(s, row_count=2000).text == "at midnight: 20%; nulls: 50%"

    def test_a_part_takes_its_own_occurrences_as_the_population(self) -> None:
        part = {"classification": "text", "occurrences": 40, "null_count": 8, "empty_count": 8}

        assert notes_synthesis.synthesize(part).text == "text; empty strings: 25%"
