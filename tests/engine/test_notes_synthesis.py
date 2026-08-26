"""Per-classification Notes synthesis templates."""

from __future__ import annotations

import pytest

from dbprint.engine.notes_synthesis import synthesize


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
        assert synthesize(s) == "top: a1b2 (1), candidate key"

    def test_names_the_exception(self) -> None:
        s = _stats(
            "text",
            values=[{"value": "a1b2", "count": 1}],
            inferred={"candidate_key": True, "candidate_key_exception": "measured_duplicates"},
        )
        assert synthesize(s) == "top: a1b2 (1), candidate key (measured duplicates)"

    def test_rides_a_classification_other_than_text_too(self) -> None:
        s = _stats("numeric", range={"min": 1, "max": 9}, inferred={"candidate_key": True})

        assert synthesize(s) == "range 1..9, candidate key"


class TestBoolean:
    def test_true_false_counts(self) -> None:
        s = _stats(
            "boolean",
            values=[{"value": True, "count": 270}, {"value": False, "count": 10}],
        )
        assert synthesize(s) == "270 true / 10 false"


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
        assert synthesize(s) == "3 distinct: a / b / c"

    def test_full_enum_above_the_old_count_limit(self) -> None:
        """Exhaustiveness, not a count, decides - an 8-value complete domain renders in full."""

        values = [{"value": f"v{i}", "count": 1} for i in range(8)]
        s = _stats("categorical", cardinality=8, values=values, values_coverage=1.0)
        out = synthesize(s)

        assert out == "8 distinct: " + " / ".join(f"v{i}" for i in range(8))
        assert "%" not in out
        assert "..." not in out

    def test_shares_are_taken_against_the_column_not_the_listed_rows(self) -> None:
        s = _stats(
            "categorical",
            cardinality=40,
            values=[{"value": "a", "count": 100}, {"value": "b", "count": 100}],
            values_coverage=0.2,
        )

        # 100 of 1000 non-null rows, not 100 of the 200 listed.
        assert "a (10%)" in synthesize(s)

    def test_top_3_with_total_when_truncated(self) -> None:
        values = [{"value": f"v{i}", "count": 10 - i} for i in range(10)]
        s = _stats("categorical", cardinality=10, values=values, values_coverage=0.42)
        out = synthesize(s)
        assert out.startswith("10 distinct: v0 ")
        assert "... (10 total)" in out

    def test_an_empty_exhaustive_domain_shows_no_enumeration(self) -> None:
        s = _stats("categorical", cardinality=0, values=[], values_coverage=1.0)

        assert synthesize(s) == "0 distinct"


class TestForeignKeyCandidate:
    def test_uses_supplied_target(self) -> None:
        s = _stats("foreign_key_candidate")
        assert synthesize(s, fk_target="herbarium.public.herbarium.id") == (
            "FK -> herbarium.public.herbarium.id"
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
        assert "range 2024-01-01 -> 2026-06-08 (889 days)" in out
        assert "freshness live" in out

    def test_a_missing_freshness_block_publishes_no_age_verdict(self) -> None:
        """The three buckets are measurements; none of them means "not measured"."""

        s = _stats(
            "temporal",
            range={"min": "2024-01-01", "max": "2026-06-08", "span_days": 889},
            percentiles={"p01": "2024-01-01", "p99": "2026-06-08"},
        )

        assert synthesize(s) == "range 2024-01-01 -> 2026-06-08 (889 days)"

    def test_a_column_with_nothing_measured_names_its_classification(self) -> None:
        """The fallback every other branch already has, so the cell is never blank."""

        assert synthesize(_stats("temporal")) == "temporal"


class TestNumeric:
    def test_range_and_median(self) -> None:
        s = _stats("numeric", range={"min": 0, "max": 100}, percentiles={"p50": 42})
        assert synthesize(s) == "range 0..100, p50=42"


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
        assert synthesize(s) == "top: a (200), b (150)"

    def test_full_enum_when_exhaustive_takes_the_categorical_shape(self) -> None:
        """One criterion, one shape - `top:` says the opposite of a complete domain."""

        values = [{"value": f"v{i}", "count": 1} for i in range(6)]
        s = _stats("text", cardinality=6, values=values, values_coverage=1.0)
        out = synthesize(s)

        assert out == "6 distinct: " + " / ".join(f"v{i}" for i in range(6))
        assert "top:" not in out

    def test_prose_publishes_no_list_regardless_of_coverage(self) -> None:
        """A prose column carries no `values` at all (SPEC 2.2.3 footnote), coverage or not."""

        assert synthesize(_stats("text", values=[], values_coverage=1.0)) == "text"


class TestJson:
    def test_label_only(self) -> None:
        assert synthesize(_stats("json")) == "json"


class TestUnsupported:
    def test_shows_sql_type(self) -> None:
        s = _stats("unsupported", sql_type="bytea")
        assert synthesize(s) == "bytea"


class TestNullRateSuffix:
    def test_suffix_added_at_threshold(self) -> None:
        s = _stats("numeric", range={"min": 0, "max": 1}, percentiles={"p50": 0}, null_rate=0.123)
        assert synthesize(s).endswith(", 12.3% null")

    def test_no_suffix_below_threshold(self) -> None:
        s = _stats("numeric", range={"min": 0, "max": 1}, percentiles={"p50": 0}, null_rate=0.001)
        assert ", null" not in synthesize(s)
        assert "% null" not in synthesize(s)


class TestDistribution:
    """`distribution` (SPEC 2.2.5) reaches numeric and temporal cells, redacted or not."""

    def test_numeric_carries_the_shape_word(self) -> None:
        s = _stats(
            "numeric",
            range={"min": 0, "max": 100},
            percentiles={"p50": 42},
            distribution="imbalanced",
        )
        assert synthesize(s) == "range 0..100, p50=42, imbalanced"

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
        assert synthesize(s) == "redacted (mask), long_tail"

    def test_temporal_carries_the_shape_word(self) -> None:
        s = _stats(
            "temporal",
            range={"min": "2024-01-01", "max": "2026-06-08", "span_days": 889},
            percentiles={"p01": "2024-01-01", "p99": "2026-06-08"},
            distribution="dominant_value",
        )
        out = synthesize(s)
        assert "dominant_value" in out
        assert out.endswith("dominant_value")


class TestLooksLikeSuffix:
    def test_appended_when_detected(self) -> None:
        s = _stats(
            "text",
            values=[{"value": "a@b.com", "count": 1}],
            inferred={"looks_like": "email"},
        )
        assert synthesize(s).endswith(", looks like email")

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
        assert "looks like email" in synthesize(s)


class TestSensitivitySuffix:
    def test_phrased_as_a_detection(self) -> None:
        s = _stats(
            "categorical",
            cardinality=0,
            values=[],
            values_coverage=1.0,
            inferred={"sensitivity": "contact"},
        )
        assert synthesize(s).endswith(", contact detected")

    def test_absent_when_nothing_detected(self) -> None:
        s = _stats("categorical", cardinality=0, values=[], values_coverage=1.0)
        assert "detected" not in synthesize(s)


class TestEpochUnitSuffix:
    def test_appended_when_detected(self) -> None:
        s = _stats("numeric", range={"min": 1, "max": 9}, inferred={"epoch_unit": "seconds"})
        assert synthesize(s).endswith(", epoch (seconds)")

    def test_absent_when_not_detected(self) -> None:
        s = _stats("numeric", range={"min": 1, "max": 9})
        assert "epoch" not in synthesize(s)


class TestUnrepresentableSuffix:
    def test_names_the_affected_fields(self) -> None:
        s = _stats(
            "temporal",
            range={"min": "1970-01-01", "max": "52030-01-01", "span_days": 15376234},
            unrepresentable=["max"],
        )
        assert synthesize(s).endswith(", unrepresentable: max")

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
        assert synthesize(s).endswith(", cluster/partition key")

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
        )
        assert synthesize(s).endswith(", cluster/partition key, 12.3% null")


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

        assert "0 true / 0 false" not in out
        assert "270" in out and "10" in out

    @pytest.mark.parametrize("primitive", ["mask", "drop", "hash"])
    def test_the_cell_names_the_primitive(self, primitive: str) -> None:
        s = _stats("boolean", redacted=primitive, values=[{"count": 3}, {"count": 1}])

        assert f"redacted ({primitive})" in synthesize(s)

    @pytest.mark.parametrize("primitive", ["mask", "drop", "hash"])
    def test_a_categorical_keeps_its_distinct_count_and_shows_no_literal(
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

        assert out.startswith("5 distinct")
        assert "NULL" not in out

    @pytest.mark.parametrize("primitive", ["mask", "drop", "hash"])
    def test_a_text_column_shows_counts_without_fabricated_literals(self, primitive: str) -> None:
        s = _stats("text", redacted=primitive, values=[{"count": 412}, {"count": 98}])
        out = synthesize(s)

        assert "NULL" not in out
        assert "412" in out and "98" in out

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

        assert "889 day span" in out
        assert "freshness live" in out

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
        )

        assert synthesize(s).endswith(", 25% null")

    def test_a_redacted_column_still_reports_its_candidate_key(self) -> None:
        """SPEC 2.2.9: detection describes the column, not the emitted literals."""

        s = _stats(
            "text",
            redacted="hash",
            values=[{"count": 1}],
            inferred={"candidate_key": True},
        )

        assert synthesize(s).endswith(", candidate key")

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
            "FK -> specimen_loan.id (declared)"
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

        assert synthesize(s, hints_only=True) == "looks like email, contact detected"

    def test_a_plain_column_with_no_hints_is_empty(self) -> None:
        s = _stats("numeric", range={"min": 0, "max": 9}, percentiles={"p50": 4}, null_rate=0.1)

        assert synthesize(s, hints_only=True) == ""
