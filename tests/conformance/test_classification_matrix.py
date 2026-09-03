"""The `inferred.*` and `redacted` rows of the SPEC 2.2.3 matrix, in both layers.

The JSON Schema and the invariant checks implement one table; a cell enforced by only one fails.
"""

from __future__ import annotations

from typing import Any

import pytest

from dbprint.conformance import statistics
from dbprint.conformance.schema_validation import check_statistics


PATH = "public/t/statistics.yaml"
FQN = "public.t"

CLASSIFICATIONS = (
    "boolean",
    "json",
    "foreign_key_candidate",
    "categorical",
    "temporal",
    "numeric",
    "text",
    "unsupported",
)

# Every classification but `unsupported` may carry `inferred.sensitivity`.
SENSITIVITY_BEARING = tuple(c for c in CLASSIFICATIONS if c != "unsupported")

# `redacted` is OPTIONAL everywhere the matrix does not mark it forbidden.
REDACTABLE = tuple(c for c in CLASSIFICATIONS if c not in {"json", "unsupported"})

# The classifications whose row carries bounds, which are the cells `drop` removes.
BOUND_BEARING = ("temporal", "numeric")

FORBIDDEN_SUBFIELDS: tuple[tuple[str, str], ...] = (
    ("boolean", "looks_like"),
    ("boolean", "sampled"),
    ("boolean", "matched"),
    ("boolean", "looks_like_candidate"),
    ("boolean", "looks_like_candidate_share"),
    ("boolean", "fk_candidate"),
    ("boolean", "epoch_unit"),
    ("json", "looks_like"),
    ("json", "sampled"),
    ("json", "matched"),
    ("json", "looks_like_candidate"),
    ("json", "looks_like_candidate_share"),
    ("json", "fk_candidate"),
    ("json", "epoch_unit"),
    ("categorical", "fk_candidate"),
    ("temporal", "looks_like"),
    ("temporal", "sampled"),
    ("temporal", "matched"),
    ("temporal", "looks_like_candidate"),
    ("temporal", "looks_like_candidate_share"),
    ("temporal", "fk_candidate"),
    ("temporal", "epoch_unit"),
    ("numeric", "looks_like"),
    ("numeric", "sampled"),
    ("numeric", "matched"),
    ("numeric", "looks_like_candidate"),
    ("numeric", "looks_like_candidate_share"),
    ("numeric", "fk_candidate"),
    ("text", "fk_candidate"),
)

# `numeric` and the three SPEC 4.1.5 sampled classifications may carry it.
EPOCH_UNIT_BEARING = ("categorical", "foreign_key_candidate", "text", "numeric")

# SPEC 4.1.5's sampled classifications - where `looks_like` and its evidence pair may appear.
LOOKS_LIKE_BEARING = ("categorical", "foreign_key_candidate", "text")

_SUBFIELD_VALUES: dict[str, Any] = {
    "looks_like": "email",
    "sampled": 100,
    "matched": 95,
    "looks_like_candidate": "email",
    "looks_like_candidate_share": 0.7,
    "fk_candidate": {"target": "public.other"},
    "epoch_unit": "seconds",
}


class TestSensitivityIsCarriedWhereTheMatrixAllowsIt:
    """`inferred.sensitivity` is O on seven of eight classifications."""

    @pytest.mark.parametrize("classification", CLASSIFICATIONS)
    def test_the_base_column_conforms(self, classification: str) -> None:
        """The controls must be clean, or every rejection below proves nothing."""

        assert _errors(_payload(classification)) == []

    @pytest.mark.parametrize("classification", SENSITIVITY_BEARING)
    def test_a_sensitivity_only_inferred_is_accepted(self, classification: str) -> None:
        """A phone column is personal data whether it classifies numeric or text."""

        column = _column(classification)
        column.setdefault("inferred", {})["sensitivity"] = "contact"

        assert _errors(_payload(classification, column)) == []

    def test_unsupported_carries_no_inferred_at_all(self) -> None:
        """The one classification whose whole row is forbidden."""

        column = _column("unsupported")
        column["inferred"] = {"sensitivity": "contact"}

        assert "stats.forbidden-field-for-classification" in _codes(_payload("unsupported", column))


class TestEpochUnitIsCarriedWhereTheMatrixAllowsIt:
    """`inferred.epoch_unit` is O on `numeric` and the three SPEC 4.1.5 sampled classifications."""

    @pytest.mark.parametrize("classification", EPOCH_UNIT_BEARING)
    def test_an_epoch_unit_only_inferred_is_accepted(self, classification: str) -> None:
        column = _column(classification)
        column.setdefault("inferred", {})["epoch_unit"] = "seconds"

        assert _errors(_payload(classification, column)) == []

    @pytest.mark.parametrize("classification", EPOCH_UNIT_BEARING)
    def test_epoch_unit_coexists_with_sensitivity(self, classification: str) -> None:
        """The two fields answer different questions (SPEC 4.5)."""

        column = _column(classification)
        column.setdefault("inferred", {}).update(
            {"epoch_unit": "milliseconds", "sensitivity": "contact"},
        )

        assert _errors(_payload(classification, column)) == []


class TestLooksLikeEvidenceIsCarriedWhereTheMatrixAllowsIt:
    """`inferred.sampled`/`inferred.matched` follow `looks_like` - allowed wherever it is."""

    @pytest.mark.parametrize("classification", LOOKS_LIKE_BEARING)
    def test_the_evidence_pair_is_accepted_beside_a_verdict(self, classification: str) -> None:
        column = _column(classification)
        column.setdefault("inferred", {}).update(
            {"looks_like": "email", "sampled": 100, "matched": 95},
        )

        assert _errors(_payload(classification, column)) == []

    @pytest.mark.parametrize("classification", LOOKS_LIKE_BEARING)
    def test_the_share_is_recomputable_from_the_pair(self, classification: str) -> None:
        """The two numbers are the whole point - a consumer must be able to divide them."""

        column = _column(classification)
        column.setdefault("inferred", {}).update(
            {"looks_like": "email", "sampled": 100, "matched": 96},
        )
        payload = _payload(classification, column)
        inferred = payload["columns"]["c"]["inferred"]

        assert _errors(payload) == []
        assert inferred["matched"] / inferred["sampled"] == 0.96


class TestLooksLikeCandidateFollowsTheMatrix:
    """`inferred.looks_like_candidate`/`.looks_like_candidate_share` (SPEC 4.1.3): the
    near-miss follows `looks_like`'s own admission, but is mutually exclusive with it.
    """

    @pytest.mark.parametrize("classification", LOOKS_LIKE_BEARING)
    def test_a_near_miss_is_accepted_absent_a_verdict(self, classification: str) -> None:
        column = _column(classification)
        column.setdefault("inferred", {}).update(
            {"looks_like_candidate": "email", "looks_like_candidate_share": 0.7},
        )

        assert _errors(_payload(classification, column)) == []

    @pytest.mark.parametrize("classification", LOOKS_LIKE_BEARING)
    def test_a_near_miss_beside_a_verdict_is_refused(self, classification: str) -> None:
        column = _column(classification)
        column.setdefault("inferred", {}).update(
            {
                "looks_like": "email",
                "sampled": 100,
                "matched": 96,
                "looks_like_candidate": "url",
                "looks_like_candidate_share": 0.6,
            },
        )
        payload = _payload(classification, column)

        assert "stats.looks-like-candidate-with-verdict" in _codes(payload)
        assert check_statistics(payload, PATH) != [], (
            "the schema's own mutual-exclusion rejects it too"
        )

    @pytest.mark.parametrize("classification", LOOKS_LIKE_BEARING)
    def test_a_share_clearing_the_verdict_threshold_is_refused(self, classification: str) -> None:
        column = _column(classification)
        column.setdefault("inferred", {}).update(
            {"looks_like_candidate": "email", "looks_like_candidate_share": 0.95},
        )
        payload = _payload(classification, column)

        assert "stats.looks-like-candidate-at-verdict-threshold" in _codes(payload)

    @pytest.mark.parametrize("classification", LOOKS_LIKE_BEARING)
    def test_a_share_just_under_the_verdict_threshold_conforms(self, classification: str) -> None:
        column = _column(classification)
        column.setdefault("inferred", {}).update(
            {"looks_like_candidate": "email", "looks_like_candidate_share": 0.94},
        )

        assert _errors(_payload(classification, column)) == []


class TestForbiddenInferredSubfields:
    """Cells the matrix marks forbidden, which a flat key test cannot express."""

    @pytest.mark.parametrize(("classification", "subfield"), FORBIDDEN_SUBFIELDS)
    def test_the_invariant_check_names_the_subfield(
        self,
        classification: str,
        subfield: str,
    ) -> None:
        column = _column(classification)
        column.setdefault("inferred", {})[subfield] = _SUBFIELD_VALUES[subfield]
        issues = statistics.check(_payload(classification, column), PATH, FQN)
        forbidden = [i for i in issues if i.code == "stats.forbidden-field-for-classification"]

        assert [i for i in forbidden if f"inferred.{subfield}" in i.detail], [
            i.detail for i in issues
        ]

    @pytest.mark.parametrize(("classification", "subfield"), FORBIDDEN_SUBFIELDS)
    def test_the_schema_refuses_it_too(self, classification: str, subfield: str) -> None:
        """Both layers or neither - a cell one of them misses is how they drift."""

        column = _column(classification)
        column.setdefault("inferred", {})[subfield] = _SUBFIELD_VALUES[subfield]

        assert check_statistics(_payload(classification, column), PATH) != []


class TestCandidateKeyAndException:
    """SPEC 4.2: `candidate_key` and its exception must agree with the ratio.

    `json` is the fixture because its matrix row forbids every cell value, so overriding
    cardinality cannot collide with a values-list or bounds check.
    """

    def test_a_genuinely_unique_column_carries_no_marker(self) -> None:
        column = _column("json")
        column.update(cardinality=10, cardinality_ratio=1.0)
        column["inferred"] = {"candidate_key": True}

        assert _errors(_payload("json", column)) == []

    def test_a_marker_prelogged_at_ratio_one_is_an_error(self) -> None:
        column = _column("json")
        column.update(cardinality=10, cardinality_ratio=1.0)
        column["inferred"] = {
            "candidate_key": True,
            "candidate_key_exception": "measured_duplicates",
        }

        assert "stats.candidate-key-exception-mismatch" in _codes(_payload("json", column))

    def test_measured_duplicates_is_required_on_an_exact_shortfall(self) -> None:
        """cardinality 9999 of 10000 scanned, exact - a producer measured 1 duplicate."""

        column = _column("json")
        column.update(cardinality=9999, cardinality_ratio=0.9999)
        column["inferred"] = {
            "candidate_key": True,
            "candidate_key_exception": "measured_duplicates",
        }

        assert _errors(_payload("json", column, row_count=10000)) == []

    def test_a_missing_marker_on_an_exact_shortfall_is_an_error(self) -> None:
        column = _column("json")
        column.update(cardinality=9999, cardinality_ratio=0.9999)
        column["inferred"] = {"candidate_key": True}

        assert "stats.candidate-key-exception-mismatch" in _codes(
            _payload("json", column, row_count=10000),
        )

    def test_nulls_alone_do_not_earn_measured_duplicates(self) -> None:
        """9999 distinct non-null of 10000 scanned, 1 null: no value repeats."""

        column = _column("json")
        column.update(
            cardinality=9999,
            cardinality_ratio=0.9999,
            nullable=True,
            null_count=1,
            null_rate=0.0001,
        )
        column["inferred"] = {"candidate_key": True}

        assert _errors(_payload("json", column, row_count=10000)) == []

    def test_estimated_is_required_on_an_approximate_shortfall(self) -> None:
        column = _column("json")
        column.update(cardinality=9999, cardinality_ratio=0.9999, cardinality_method="approximate")
        column["inferred"] = {"candidate_key": True, "candidate_key_exception": "estimated"}

        assert _errors(_payload("json", column, row_count=10000)) == []

    def test_measured_duplicates_on_an_approximate_count_is_an_error(self) -> None:
        """The two tiers are not interchangeable - an estimate is never `measured_duplicates`."""

        column = _column("json")
        column.update(cardinality=9999, cardinality_ratio=0.9999, cardinality_method="approximate")
        column["inferred"] = {
            "candidate_key": True,
            "candidate_key_exception": "measured_duplicates",
        }

        assert "stats.candidate-key-exception-mismatch" in _codes(
            _payload("json", column, row_count=10000),
        )

    def test_the_schema_accepts_both_enum_values(self) -> None:
        for value in ("measured_duplicates", "estimated"):
            column = _column("json")
            column.update(cardinality=9999, cardinality_ratio=0.9999)
            column["inferred"] = {"candidate_key": True, "candidate_key_exception": value}

            assert check_statistics(_payload("json", column, row_count=10000), PATH) == []

    def test_the_schema_refuses_an_unknown_value(self) -> None:
        column = _column("json")
        column["inferred"] = {"candidate_key": True, "candidate_key_exception": "duplicated"}

        assert check_statistics(_payload("json", column), PATH) != []

    def test_a_candidate_key_below_the_threshold_is_an_error(self) -> None:
        """`candidate_key` is recomputed from the ratio, not trusted at face value."""

        column = _column("json")
        column["inferred"] = {"candidate_key": True}

        assert "stats.candidate-key-mismatch" in _codes(_payload("json", column))

    def test_a_missing_candidate_key_above_the_threshold_is_an_error(self) -> None:
        column = _column("json")
        column.update(cardinality=10, cardinality_ratio=1.0)

        assert "stats.candidate-key-mismatch" in _codes(_payload("json", column))


class TestRedactedFollowsTheMatrix:
    """A marker announcing a redaction of nothing is a violation, not a no-op."""

    @pytest.mark.parametrize("classification", REDACTABLE)
    def test_a_marker_is_accepted_where_cell_values_exist(self, classification: str) -> None:
        column = _column(classification)
        column["redacted"] = "mask"

        if classification == "temporal":
            # A real producer coarsens both day counts under any marker (SPEC 2.2.9).
            column["freshness"]["max_age_days"] = 0
            column["range"]["span_days"] = 0

        assert _errors(_payload(classification, column)) == []

    @pytest.mark.parametrize("classification", ["json", "unsupported"])
    def test_a_marker_is_refused_where_no_cell_value_exists(self, classification: str) -> None:
        column = _column(classification)
        column["redacted"] = "mask"
        payload = _payload(classification, column)

        assert "stats.forbidden-field-for-classification" in _codes(payload)
        assert check_statistics(payload, PATH) != []


class TestBoundsAreConditionalOnDrop:
    """The matrix's conditional cells: bounds are REQUIRED until `drop` withholds them.

    `range` and `percentiles` are cell values, so `drop` emits neither.
    """

    @pytest.mark.parametrize("classification", BOUND_BEARING)
    def test_a_dropped_column_omits_them(self, classification: str) -> None:
        """The defect: `generate` wrote exactly this and `check` rejected it."""

        column = _dropped(classification)

        if classification == "temporal":
            # A real producer coarsens `max_age_days` under any marker (SPEC 2.2.9).
            column["freshness"]["max_age_days"] = 0

        assert _errors(_payload(classification, column)) == []

    @pytest.mark.parametrize("classification", BOUND_BEARING)
    def test_a_dropped_column_may_not_carry_them_either(self, classification: str) -> None:
        """Forbidden rather than optional: there is no literal left to put there."""

        column = _column(classification)
        column["redacted"] = "drop"
        payload = _payload(classification, column)
        forbidden = [
            i
            for i in statistics.check(payload, PATH, FQN)
            if i.code == "stats.forbidden-field-for-classification"
        ]

        assert {i.spec_ref for i in forbidden} == {"§2.2.9"}
        assert check_statistics(payload, PATH) != []

    @pytest.mark.parametrize("classification", BOUND_BEARING)
    @pytest.mark.parametrize("primitive", ["mask", "hash"])
    def test_a_substituting_primitive_keeps_them(
        self,
        classification: str,
        primitive: str,
    ) -> None:
        """Only `drop` removes a field; the other two replace the literal in place."""

        column = _column(classification)
        column["redacted"] = primitive

        if classification == "temporal":
            # A real producer coarsens both day counts under any marker (SPEC 2.2.9).
            column["freshness"]["max_age_days"] = 0
            column["range"]["span_days"] = 0

        assert _errors(_payload(classification, column)) == []

    @pytest.mark.parametrize("classification", BOUND_BEARING)
    def test_an_unredacted_column_still_owes_them(self, classification: str) -> None:
        """The exception is conditional - without the marker the cells stay REQUIRED."""

        column = _column(classification)
        del column["range"], column["percentiles"]
        payload = _payload(classification, column)

        assert "stats.missing-required-field-for-classification" in _codes(payload)
        assert check_statistics(payload, PATH) != []

    def test_a_dropped_temporal_column_still_owes_freshness(self) -> None:
        """`max_age_days` is derived by arithmetic, not read from a cell, so it survives."""

        column = _dropped("temporal")
        del column["freshness"]

        assert "stats.missing-required-field-for-classification" in _codes(
            _payload("temporal", column),
        )

    def test_a_dropped_temporal_column_sheds_span_days_with_its_range(self) -> None:
        """`span_days` is its own matrix row, and it lives inside the object `drop` removes."""

        assert "range" not in _dropped("temporal")

    @pytest.mark.parametrize("classification", ["boolean", "categorical", "text"])
    def test_a_dropped_value_list_keeps_its_counts(self, classification: str) -> None:
        """The control: elsewhere `drop` empties the literals and removes no field."""

        column = _column(classification)
        column["redacted"] = "drop"
        column["values"] = [{"count": 10}]

        assert _errors(_payload(classification, column)) == []


class TestTheValueListIsConditionalOnProse:
    """The matrix's second conditional cell, and the one row it reaches.

    A prose column's values describe nothing actionable, so the format exempts the list.
    """

    def test_a_prose_text_column_omits_the_list(self) -> None:
        assert _errors(_payload("text", _prose("text"))) == []

    def test_a_prose_text_column_may_not_carry_it_either(self) -> None:
        """Forbidden rather than optional, so the artifact cannot claim a scan it skipped."""

        column = _column("text")
        column["inferred"] = {"looks_like": "prose"}
        payload = _payload("text", column)
        forbidden = [
            i
            for i in statistics.check(payload, PATH, FQN)
            if i.code == "stats.forbidden-field-for-classification"
        ]

        assert {i.spec_ref for i in forbidden} == {"§2.2.3"}
        assert check_statistics(payload, PATH) != []

    def test_a_text_column_with_another_pattern_keeps_the_list(self) -> None:
        column = _column("text")
        column["inferred"] = {"looks_like": "email"}

        assert _errors(_payload("text", column)) == []

    def test_a_text_column_with_no_pattern_still_owes_the_list(self) -> None:
        """The exemption is conditional; without the verdict the cells stay REQUIRED."""

        column = _column("text")
        del column["values"], column["values_coverage"], column["distribution"]
        payload = _payload("text", column)

        assert "stats.missing-required-field-for-classification" in _codes(payload)
        assert check_statistics(payload, PATH) != []

    def test_a_prose_categorical_column_still_owes_the_list(self) -> None:
        """The boundary: the exemption covers `text` and nothing else."""

        payload = _payload("categorical", _prose("categorical"))

        assert "stats.missing-required-field-for-classification" in _codes(payload)
        assert check_statistics(payload, PATH) != []

    def test_a_prose_categorical_column_carrying_the_list_conforms(self) -> None:
        column = _column("categorical")
        column["inferred"] = {"looks_like": "prose"}

        assert _errors(_payload("categorical", column)) == []


class TestLengthFollowsTheColumnType:
    """The matrix's fourth conditional cell: `length` on `categorical`/`foreign_key_candidate`
    tracks `sql_type`, not the classification (SPEC 2.2.3's type-admission footnote).
    """

    @pytest.mark.parametrize("classification", ["foreign_key_candidate", "categorical"])
    def test_a_string_typed_column_owes_it(self, classification: str) -> None:
        """`_column()` already sets `sql_type: TEXT`; removing `length` alone must fail - the type
        condition is cross-field, so only the Python conformance layer catches it.
        """

        column = _column(classification)
        del column["length"]
        payload = _payload(classification, column)

        assert "stats.missing-required-field-for-classification" in _codes(payload)

    @pytest.mark.parametrize("classification", ["foreign_key_candidate", "categorical"])
    def test_a_non_string_typed_column_may_not_carry_it(self, classification: str) -> None:
        column = _column(classification)
        column["sql_type"] = "INTEGER"
        payload = _payload(classification, column)
        forbidden = [
            i
            for i in statistics.check(payload, PATH, FQN)
            if i.code == "stats.forbidden-field-for-classification"
        ]

        assert {i.spec_ref for i in forbidden} == {"§2.2.3"}

    @pytest.mark.parametrize("classification", ["foreign_key_candidate", "categorical"])
    def test_a_non_string_typed_column_without_it_conforms(self, classification: str) -> None:
        column = _column(classification)
        column["sql_type"] = "INTEGER"
        del column["length"]

        assert _errors(_payload(classification, column)) == []

    def test_a_redacted_single_row_column_may_not_carry_it_either(self) -> None:
        """The same last-row inversion `mean`/`sum` avoid (SPEC 2.2.3) - cross-field arithmetic,
        so only the Python conformance layer catches it.
        """

        column = _column("text")
        column["redacted"] = "mask"
        payload = _payload("text", column, row_count=1)
        forbidden = [
            i
            for i in statistics.check(payload, PATH, FQN)
            if i.code == "stats.forbidden-field-for-classification"
        ]

        assert {i.spec_ref for i in forbidden} == {"§2.2.9"}


class TestNormalizedCardinalityFollowsTheMatrix:
    """`normalized_cardinality`: O on the three string-admitting classifications, forbidden
    everywhere else (SPEC 2.2.3), and bounded against `cardinality` when present.
    """

    @pytest.mark.parametrize("classification", ["foreign_key_candidate", "categorical", "text"])
    def test_may_be_carried_on_a_string_typed_column(self, classification: str) -> None:
        column = _column(classification)
        column["normalized_cardinality"] = column["cardinality"]
        payload = _payload(classification, column)

        assert _errors(payload) == []

    @pytest.mark.parametrize(
        "classification",
        ["boolean", "json", "temporal", "numeric", "unsupported"],
    )
    def test_forbidden_outside_the_string_admitting_classifications(
        self,
        classification: str,
    ) -> None:
        column = _column(classification)
        column["normalized_cardinality"] = 1
        payload = _payload(classification, column)

        assert "stats.forbidden-field-for-classification" in _codes(payload)

    def test_exceeding_cardinality_is_an_error(self) -> None:
        column = _column("text")
        column["normalized_cardinality"] = column["cardinality"] + 1
        payload = _payload("text", column)

        assert "stats.normalized-cardinality-exceeds-cardinality" in _codes(payload)

    def test_exceeding_an_approximate_cardinality_conforms(self) -> None:
        """SPEC 2.2.4: an approximate `cardinality` makes the comparison approximate on both
        sides - a stale catalog estimate can legitimately undershoot the exact folded count.
        """

        column = _column("text")
        column["cardinality_method"] = "approximate"
        column["normalized_cardinality"] = column["cardinality"] + 1
        payload = _payload("text", column)

        assert "stats.normalized-cardinality-exceeds-cardinality" not in _codes(payload)

    def test_below_cardinality_conforms(self) -> None:
        column = _column("foreign_key_candidate")
        column["cardinality"] = 5
        column["cardinality_ratio"] = 0.5
        column["values"] = [{"value": f"v{i}", "count": 2} for i in range(5)]
        column["normalized_cardinality"] = 3
        payload = _payload("foreign_key_candidate", column)

        assert _errors(payload) == []


class TestUnrepresentable:
    """The `unrepresentable` row: O on `temporal`, MUST NOT everywhere else."""

    def test_a_marked_bound_on_a_temporal_column_conforms(self) -> None:
        column = _column("temporal")
        column["unrepresentable"] = ["max"]

        assert _errors(_payload("temporal", column)) == []

    def test_a_marked_percentile_on_a_temporal_column_conforms(self) -> None:
        column = _column("temporal")
        column["unrepresentable"] = ["p50"]

        assert _errors(_payload("temporal", column)) == []

    def test_both_bounds_and_a_percentile_conform_together(self) -> None:
        column = _column("temporal")
        column["unrepresentable"] = ["min", "max", "p50"]

        assert _errors(_payload("temporal", column)) == []

    @pytest.mark.parametrize("classification", [c for c in CLASSIFICATIONS if c != "temporal"])
    def test_forbidden_everywhere_but_temporal(self, classification: str) -> None:
        column = _column(classification)
        column["unrepresentable"] = ["max"] if classification != "unsupported" else ["min"]
        payload = _payload(classification, column)

        assert "stats.forbidden-field-for-classification" in _codes(payload)
        assert check_statistics(payload, PATH) != []

    def test_naming_a_field_the_column_never_emitted_is_an_error(self) -> None:
        """The list may only name fields this same column actually published."""

        column = _column("temporal")
        column["unrepresentable"] = ["p99"]  # the fixture emits p50 only

        assert "stats.unrepresentable-names-unemitted-field" in _codes(_payload("temporal", column))

    def test_an_empty_list_is_an_error(self) -> None:
        """Omit the key instead of asserting an empty claim."""

        column = _column("temporal")
        column["unrepresentable"] = []

        assert "stats.unrepresentable-empty" in _codes(_payload("temporal", column))

    def test_a_dropped_temporal_column_may_not_carry_it_either(self) -> None:
        """No bounds are emitted under `redacted: drop`, so nothing is left to mark."""

        column = _dropped("temporal")
        column["unrepresentable"] = ["max"]

        assert "stats.unrepresentable-names-unemitted-field" in _codes(_payload("temporal", column))


class TestSpanDays:
    """`range.span_days` must equal `day_count(range.min, range.max)` (SPEC 2.2.4)."""

    def test_a_disagreeing_span_days_is_an_error(self) -> None:
        column = _column("temporal")
        column["range"] = {
            "min": "2026-01-01T00:00:00Z",
            "max": "2026-01-01T00:00:00Z",
            "span_days": 2562,
        }

        assert "stats.span-days-mismatch" in _codes(_payload("temporal", column))

    def test_a_date_only_column_is_checked_the_same_way(self) -> None:
        """`daily_viability_mv.day`'s own shape: no time-of-day component at all."""

        column = _column("temporal")
        column["range"] = {"min": "2025-01-01", "max": "2032-01-07", "span_days": 2562}
        column["percentiles"] = {"p50": "2028-07-04"}
        # max sits after profiled_at (2026-01-01) - the derived age clamps to 0.
        column["freshness"] = {"max_age_days": 0, "classification": "live"}

        assert _errors(_payload("temporal", column)) == []

    def test_a_wrong_date_only_span_days_is_still_caught(self) -> None:
        column = _column("temporal")
        column["range"] = {"min": "2025-01-01", "max": "2032-01-07", "span_days": 1}

        assert "stats.span-days-mismatch" in _codes(_payload("temporal", column))

    def test_a_masked_column_is_not_checked_against_its_placeholder_bounds(self) -> None:
        """SPEC 2.2.9: `mask` substitutes the bounds; `span_days` is coarsened, not derived."""

        column = _column("temporal")
        column["redacted"] = "mask"
        column["range"] = {"min": "[redacted]", "max": "[redacted]", "span_days": 2562}
        column["percentiles"] = {"p50": "[redacted]"}

        assert "stats.span-days-mismatch" not in _codes(_payload("temporal", column))

    def test_a_hashed_column_is_not_checked_against_its_placeholder_bounds(self) -> None:
        column = _column("temporal")
        column["redacted"] = "hash"
        column["range"] = {"min": "9f2c1a0b3d4e5f60", "max": "1a2b3c4d5e6f7081", "span_days": 2562}
        column["percentiles"] = {"p50": "0011223344556677"}

        assert "stats.span-days-mismatch" not in _codes(_payload("temporal", column))

    def test_a_dropped_column_is_not_checked_it_carries_no_range(self) -> None:
        """No special case needed - `drop` emits neither `range` nor `span_days`."""

        assert "stats.span-days-mismatch" not in _codes(_payload("temporal", _dropped("temporal")))

    def test_a_bound_named_unrepresentable_is_not_checked(self) -> None:
        """A year outside 0001-9999, published verbatim, is not derivable by this rule."""

        column = _column("temporal")
        column["range"] = {"min": "2026-01-01T00:00:00Z", "max": "infinity", "span_days": 3652058}
        column["unrepresentable"] = ["max"]

        assert "stats.span-days-mismatch" not in _codes(_payload("temporal", column))

    def test_a_time_only_column_is_not_checked(self) -> None:
        """`TIME` carries no date; SPEC 2.2.4 fixes its `span_days` at 0 without arithmetic."""

        column = _column("temporal")
        column["sql_type"] = "TIME"
        column["range"] = {"min": "08:00:00", "max": "17:00:00", "span_days": 0}
        column["percentiles"] = {"p50": "12:30:00"}

        assert _errors(_payload("temporal", column)) == []


class TestQuantizedCountFollowsDayResolution:
    """SPEC 2.2.3's day-resolution footnote keys off `sql_type`, not classification alone -
    ClickHouse's `Date32` truncates to itself the same as `Date`.
    """

    def test_a_date32_column_is_not_required_to_carry_it(self) -> None:
        column = _column("temporal")
        column["sql_type"] = "Date32"
        column["range"] = {"min": "2025-01-01", "max": "2032-01-07", "span_days": 2562}
        column["percentiles"] = {"p50": "2028-07-04"}
        column["freshness"] = {"max_age_days": 0, "classification": "live"}

        assert _errors(_payload("temporal", column)) == []


class TestUnredactedSensitiveIsAWarning:
    """`privacy.unredacted-sensitive` (SPEC 4.4.2): a named category published with no marker."""

    # `json`'s matrix row forbids values/range/percentiles, so nothing is published to warn about.
    _PUBLISHES_NOTHING = frozenset({"json"})

    @pytest.mark.parametrize("classification", SENSITIVITY_BEARING)
    def test_a_published_cell_value_warns_unless_the_matrix_forbids_one(
        self,
        classification: str,
    ) -> None:
        column = _column(classification)
        column.setdefault("inferred", {})["sensitivity"] = "contact"
        codes = _codes(_payload(classification, column))

        if classification in self._PUBLISHES_NOTHING:
            assert "privacy.unredacted-sensitive" not in codes
        else:
            assert "privacy.unredacted-sensitive" in codes

    def test_the_code_is_a_warning_that_never_becomes_an_error(self) -> None:
        column = _column("categorical")
        column.setdefault("inferred", {})["sensitivity"] = "contact"
        payload = _payload("categorical", column)
        issues = [*check_statistics(payload, PATH), *statistics.check(payload, PATH, FQN)]
        warning = next(i for i in issues if i.code == "privacy.unredacted-sensitive")

        assert warning.severity == "warning"
        assert _errors(payload) == []

    def test_a_covering_redact_rule_silences_it(self) -> None:
        column = _column("categorical")
        column.setdefault("inferred", {})["sensitivity"] = "contact"
        column["redacted"] = "mask"
        column["values"] = [{"value": "[redacted]", "count": 10}]

        assert "privacy.unredacted-sensitive" not in _codes(_payload("categorical", column))

    def test_a_prose_column_publishes_nothing_and_is_silent(self) -> None:
        """The marker rule's own publication predicate, read from the other side."""

        column = _prose("text")
        column.setdefault("inferred", {})["sensitivity"] = "health"

        assert "privacy.unredacted-sensitive" not in _codes(_payload("text", column))

    def test_a_dropped_column_publishes_nothing_and_is_silent(self) -> None:
        column = _dropped("temporal")
        column["freshness"]["max_age_days"] = 0
        column.setdefault("inferred", {})["sensitivity"] = "date_of_birth"

        assert "privacy.unredacted-sensitive" not in _codes(_payload("temporal", column))

    def test_a_column_with_no_detected_sensitivity_is_silent(self) -> None:
        assert "privacy.unredacted-sensitive" not in _codes(_payload("categorical"))


class TestRedactedDayCountsAreCoarsened:
    """A redacted temporal column's derived day counts are floored to 90 (SPEC 2.2.9)."""

    def test_an_unfloored_max_age_days_is_an_error(self) -> None:
        column = _column("temporal")
        column["redacted"] = "mask"
        column["range"] = {"min": "[redacted]", "max": "[redacted]", "span_days": 0}
        column["percentiles"] = {"p50": "[redacted]"}
        column["freshness"]["max_age_days"] = 91

        assert "stats.uncoarsened-redacted-day-count" in _codes(_payload("temporal", column))

    def test_an_unfloored_span_days_is_an_error(self) -> None:
        column = _column("temporal")
        column["redacted"] = "hash"
        column["range"] = {"min": "9f2c1a0b3d4e5f60", "max": "1a2b3c4d5e6f7081", "span_days": 91}
        column["percentiles"] = {"p50": "0011223344556677"}
        column["freshness"]["max_age_days"] = 0

        assert "stats.uncoarsened-redacted-day-count" in _codes(_payload("temporal", column))

    def test_a_floored_value_is_not_an_error(self) -> None:
        column = _column("temporal")
        column["redacted"] = "mask"
        column["range"] = {"min": "[redacted]", "max": "[redacted]", "span_days": 360}
        column["percentiles"] = {"p50": "[redacted]"}
        column["freshness"]["max_age_days"] = 90

        assert "stats.uncoarsened-redacted-day-count" not in _codes(_payload("temporal", column))

    def test_an_unredacted_column_is_not_checked(self) -> None:
        """The rule is conditional on the marker - an unredacted column keeps the exact count."""

        column = _column("temporal")
        column["freshness"]["max_age_days"] = 1

        assert "stats.uncoarsened-redacted-day-count" not in _codes(_payload("temporal", column))

    def test_a_dropped_column_is_still_checked_on_freshness_alone(self) -> None:
        """`drop` carries no `range`, but `freshness` survives and must still be floored."""

        column = _dropped("temporal")
        column["freshness"]["max_age_days"] = 1

        assert "stats.uncoarsened-redacted-day-count" in _codes(_payload("temporal", column))


def _prose(classification: str) -> dict[str, Any]:
    """A column of `classification` as a producer emits it for inferred prose."""

    column = _column(classification)
    column["inferred"] = {"looks_like": "prose"}
    del column["values"], column["values_coverage"], column["distribution"]

    return column


def _dropped(classification: str) -> dict[str, Any]:
    """A column of `classification` as a producer emits it under `redacted: drop`."""

    column = _column(classification)
    column["redacted"] = "drop"
    del column["range"], column["percentiles"]

    return column


def _errors(payload: dict[str, Any]) -> list[Any]:
    """Error-severity issues from both layers, which is what conformance turns on."""

    issues = [*check_statistics(payload, PATH), *statistics.check(payload, PATH, FQN)]

    return [i for i in issues if i.severity == "error"]


def _codes(payload: dict[str, Any]) -> set[str]:
    return {
        i.code for i in [*check_statistics(payload, PATH), *statistics.check(payload, PATH, FQN)]
    }


def _payload(
    classification: str,
    column: dict[str, Any] | None = None,
    *,
    row_count: int = 10,
) -> dict[str, Any]:
    """A one-column print of the given classification, well-formed outside the matrix.

    The null census follows the column's own `null_count`, so these tests assert on the
    matrix rather than tripping SPEC 2.2.10's absence rule.
    """

    body = column if column is not None else _column(classification)
    payload: dict[str, Any] = {
        "format_version": 1,
        "table": FQN,
        "type": "table",
        "profiled_at": "2026-01-01T00:00:00Z",
        "row_count": row_count,
        "row_count_method": "exact",
    }
    nulls = body.get("null_count") or 0

    if nulls:
        payload["null_patterns"] = {
            "coverage": 1.0,
            "patterns": [
                {"columns": [], "count": row_count - nulls},
                {"columns": ["c"], "count": nulls},
            ],
        }

    payload["grain"] = {"keys": []}
    payload["columns"] = {"c": body}

    return payload


def _column(classification: str) -> dict[str, Any]:
    """A minimal column of one classification, carrying exactly its R fields."""

    universal: dict[str, Any] = {
        "sql_type": "TEXT",
        "nullable": False,
        "null_count": 0,
        "null_rate": 0.0,
        "classification": classification,
    }

    if classification == "unsupported":
        return universal

    counted = {
        **universal,
        "cardinality": 1,
        "cardinality_ratio": 0.1,
        "cardinality_method": "exact",
    }
    values = {"values": [{"value": "a", "count": 10}], "values_coverage": 1.0}
    # One value over ten rows is a dominant value per SPEC 2.2.5.
    distribution = {"distribution": "dominant_value"}

    # `sql_type` is "TEXT" (string-like) above, so `length` is REQUIRED on both these rows
    # too (SPEC 2.2.3's type-admission footnote) - "a" is one character, min/max/avg/p95 agree.
    length = {"length": {"min": 1, "max": 1, "avg": 1.0, "p95": 1.0}}

    if classification == "boolean":
        return {**counted, **values}
    elif classification == "json":
        return counted
    elif classification in ("foreign_key_candidate", "categorical"):
        return {**counted, **values, **distribution, **length}
    elif classification == "text":
        return {**counted, **values, **distribution, "empty_count": 0, **length}
    elif classification == "temporal":
        # max is a day before `_payload`'s profiled_at, so max_age_days is the 1 asserted
        # here; cardinality=1 over 10 rows is a dominant value per SPEC 2.2.5/2.2.7.
        return {
            **counted,
            "range": {"min": "2025-12-30T00:00:00Z", "max": "2025-12-31T00:00:00Z", "span_days": 1},
            "percentiles": {"p50": "2025-12-30T12:00:00Z"},
            "freshness": {"max_age_days": 1, "classification": "live"},
            "values": [{"value": "2025-12-30T12:00:00Z", "count": 10}],
            "distribution": "dominant_value",
            "frequencies": {"top": 10, "bottom": 10, "listed": 1, "total": 10},
        }
    else:
        return {
            **counted,
            "range": {"min": 1, "max": 9},
            "percentiles": {"p50": 5},
            "mean": 5.0,
            "sum": 50.0,
            "zero_count": 0,
            "negative_count": 0,
            "quantized_count": 10,
            "values": [{"value": 5, "count": 10}],
            "distribution": "dominant_value",
            "frequencies": {"top": 10, "bottom": 10, "listed": 1, "total": 10},
        }
