"""The resolver reads each absence as SPEC 7.2/7.3 defines it: five states, never found/not-found."""

from __future__ import annotations

import pytest

from dbprint.spec.absence import (
    Absence,
    column_value,
    emits,
    read_column_field,
    read_table_block,
    sample_verdicts,
    verdict_withheld,
)


def _column(**fields: object) -> dict[str, object]:
    return {"sql_type": "VARCHAR", "classification": "categorical", **fields}


def test_an_emitted_empty_list_is_present_not_absent() -> None:
    got = read_column_field(_column(values=[], values_coverage=1.0), "values")

    assert (got.state, got.value) == (Absence.PRESENT, [])


def test_a_key_below_the_threshold_reads_false() -> None:
    got = read_column_field(_column(cardinality_ratio=0.01), "inferred.candidate_key")

    assert (got.state, got.value) == (Absence.OMITTED, False)


def test_a_key_whose_ratio_was_lost_is_unmeasured() -> None:
    column = _column(unmeasured=["cardinality_ratio"])

    assert read_column_field(column, "inferred.candidate_key").state is Absence.UNMEASURED


def test_a_key_with_no_ratio_is_not_applicable() -> None:
    column = _column(classification="json")

    assert read_column_field(column, "inferred.candidate_key").state is Absence.NOT_APPLICABLE


def test_no_pattern_on_an_eligible_column_is_an_omitted_verdict() -> None:
    got = read_column_field(_column(cardinality=40), "inferred.looks_like")

    assert (got.state, got.value) == (Absence.OMITTED, None)


@pytest.mark.parametrize(
    "column",
    [
        _column(cardinality=0),
        _column(classification="numeric", sql_type="INTEGER", cardinality=5),
        _column(classification="boolean", sql_type="BOOLEAN", cardinality=2),
    ],
    ids=["all-null", "numeric", "boolean"],
)
def test_detection_that_never_ran_is_not_applicable(column: dict[str, object]) -> None:
    assert read_column_field(column, "inferred.looks_like").state is Absence.NOT_APPLICABLE


@pytest.mark.parametrize(
    "path",
    ["inferred.looks_like", "inferred.sampled", "inferred.looks_like_candidate_share"],
)
def test_an_undrawn_sample_leaves_the_verdict_and_its_evidence_unmeasured(path: str) -> None:
    column = _column(cardinality=40, unmeasured=["inferred.epoch_unit", "inferred.looks_like"])

    assert read_column_field(column, path).state is Absence.UNMEASURED
    assert column_value(column, "inferred.epoch_unit") is None


def test_a_field_named_unmeasured_wins_over_every_structural_cause() -> None:
    column = _column(classification="boolean", unmeasured=["values", "values_coverage"])

    assert read_column_field(column, "values").state is Absence.UNMEASURED


def test_a_redacted_column_withholds_its_aggregates() -> None:
    column = _column(classification="numeric", redacted="mask")

    assert read_column_field(column, "mean").state is Absence.WITHHELD


def test_drop_withholds_the_bounds_mask_does_not() -> None:
    dropped = _column(classification="numeric", redacted="drop")
    masked = _column(classification="numeric", redacted="mask")

    assert read_column_field(dropped, "range.min").state is Absence.WITHHELD
    assert read_column_field(masked, "range.min").state is Absence.NOT_APPLICABLE


def test_a_statement_by_absence_carries_its_implied_value() -> None:
    assert column_value(_column(), "unmeasured") == []
    assert read_column_field(_column(), "inferred.sensitivity").state is Absence.OMITTED


def test_an_unknown_path_raises_rather_than_reading_as_absent() -> None:
    with pytest.raises(KeyError):
        read_column_field(_column(), "cardinalty")


def test_a_catalog_only_file_has_no_row_count_to_read() -> None:
    got = read_table_block({"catalog_only": True, "columns": {}}, "row_count")

    assert got.state is Absence.NOT_APPLICABLE


def test_a_queried_view_reads_like_a_queried_table() -> None:
    view = {"type": "view", "row_count": 4, "row_count_method": "exact", "columns": {}}

    assert read_table_block(view, "row_count").state is Absence.PRESENT
    assert read_table_block(view, "catalog_only").state is Absence.OMITTED
    assert read_table_block(view, "physical_layout").state is Absence.OMITTED


def test_a_lost_table_block_is_unmeasured() -> None:
    got = read_table_block({"unmeasured": ["physical_layout"]}, "physical_layout")

    assert got.state is Absence.UNMEASURED


def test_a_views_absent_dependency_list_is_unmeasured_and_a_tables_is_not() -> None:
    assert read_table_block({"type": "view"}, "depends_on").state is Absence.UNMEASURED
    assert read_table_block({"type": "table"}, "depends_on").state is Absence.NOT_APPLICABLE


def test_a_nested_block_key_resolves_through_its_block() -> None:
    got = read_table_block({"scope": {"rows_scanned": 40}}, "scope.rows_scanned")

    assert (got.state, got.value) == (Absence.PRESENT, 40)


def test_numeric_string_is_withheld_only_on_a_numeric_type() -> None:
    assert verdict_withheld({"sql_type": "INTEGER"}, "looks_like", "numeric_string")
    assert not verdict_withheld({"sql_type": "VARCHAR"}, "looks_like", "numeric_string")
    assert not verdict_withheld({"sql_type": "INTEGER"}, "looks_like", "email")


_COLUMN_READINGS = [
    (_column(cardinality=5), "cardinality", ("present", 5, "emitted", "§2.2.3")),
    (
        _column(unmeasured=["values"]),
        "values",
        ("unmeasured", None, "the read failed this run", "§2.2.4"),
    ),
    (
        _column(classification="boolean"),
        "percentiles",
        ("not_applicable", None, "not carried by boolean", "§2.2.3"),
    ),
    (
        _column(classification="temporal"),
        "inferred.epoch_unit",
        ("not_applicable", None, "not carried by temporal", "§2.2.3"),
    ),
    (
        _column(redacted="hash"),
        "sketch",
        ("withheld", None, "withheld by the redacted marker", "§2.2.9"),
    ),
    (
        _column(classification="numeric", redacted="drop"),
        "percentiles",
        ("withheld", None, "withheld by the redacted marker", "§2.2.9"),
    ),
    (
        _column(cardinality_ratio=0.2),
        "inferred.candidate_key",
        ("omitted", False, "below the candidate-key threshold", "§4.2"),
    ),
    (
        _column(),
        "inferred.candidate_key",
        ("not_applicable", None, "no cardinality_ratio measured", "§4.2"),
    ),
    (
        _column(cardinality=1),
        "inferred.looks_like",
        ("omitted", None, "no pattern reached the threshold", "§4.1.2"),
    ),
    (
        _column(cardinality=0),
        "inferred.looks_like",
        ("not_applicable", None, "detection does not run here", "§4.1.5"),
    ),
    (
        _column(),
        "inferred.sensitivity",
        ("omitted", None, "nothing was detected - never that it is safe", "§4.4.2"),
    ),
    (
        _column(),
        "unmeasured",
        ("omitted", [], "every field the column should carry was measured", "§2.2.4"),
    ),
    (
        _column(),
        "cardinality_method",
        ("not_applicable", None, "not emitted for this column", "§7.2"),
    ),
    (_column(range={"min": {"at": 1}}), "range.min.at", ("present", 1, "emitted", "§2.2.3")),
    (
        _column(cardinality=3, inferred={"looks_like": "email"}),
        "inferred.looks_like.pattern",
        ("omitted", None, "no pattern reached the threshold", "§4.1.2"),
    ),
    (
        _column(classification="unrecognised"),
        "range",
        ("not_applicable", None, "not emitted for this column", "§7.2"),
    ),
]


@pytest.mark.parametrize(("column", "path", "expected"), _COLUMN_READINGS)
def test_a_column_reading_names_its_state_value_cause_and_section(
    column: dict[str, object],
    path: str,
    expected: tuple[str, object, str, str],
) -> None:
    got = read_column_field(column, path)

    assert (got.state.value, got.value, got.cause, got.spec_ref) == expected


_TABLE_READINGS = [
    ({"row_count": 9}, "row_count", ("present", 9, "emitted", "§2.2.1")),
    (
        {"unmeasured": ["grain"]},
        "grain",
        ("unmeasured", None, "the read failed this run", "§2.2.1"),
    ),
    (
        {"unmeasured": ["null_patterns.coverage_method"]},
        "null_patterns.coverage_method",
        ("unmeasured", None, "the read failed this run", "§2.2.1"),
    ),
    (
        {"catalog_only": True},
        "row_count",
        ("not_applicable", None, "nothing was queried", "§2.2.15"),
    ),
    ({"catalog_only": True}, "scope", ("omitted", None, "every row was read", "§2.2.8")),
    ({}, "row_count", ("not_applicable", None, "not emitted for this file", "§7.3")),
    ({}, "catalog_only", ("omitted", False, "the object was queried", "§2.2.15")),
    ({}, "scope", ("omitted", None, "every row was read", "§2.2.8")),
    ({}, "null_patterns", ("omitted", None, "no column carries a null", "§2.2.10")),
    ({}, "physical_layout", ("omitted", None, "the table declares no layout", "§2.2.11")),
    ({}, "unmeasured", ("omitted", [], "every block the file should carry was measured", "§2.2.1")),
    (
        {},
        "timeline",
        (
            "omitted",
            None,
            "no eligible anchor, a scoped or empty table, or disabled - not distinguishable",
            "§2.2.16",
        ),
    ),
    (
        {"type": "matview"},
        "depends_on",
        ("unmeasured", None, "the dependency read did not happen", "§2.2.17"),
    ),
    (
        {"type": "table"},
        "depends_on",
        ("not_applicable", None, "a table has no dependencies", "§2.2.17"),
    ),
    (
        {"physical_layout": {"keys": {"sort": ["a"]}}},
        "physical_layout.keys.sort",
        ("present", ["a"], "emitted", "§2.2.1"),
    ),
]


@pytest.mark.parametrize(("statistics", "name", "expected"), _TABLE_READINGS)
def test_a_table_reading_names_its_state_value_cause_and_section(
    statistics: dict[str, object],
    name: str,
    expected: tuple[str, object, str, str],
) -> None:
    got = read_table_block(statistics, name)

    assert (got.state.value, got.value, got.cause, got.spec_ref) == expected


def test_an_unknown_path_is_named_in_the_error() -> None:
    with pytest.raises(KeyError, match="no_such_field"):
        read_column_field(_column(), "no_such_field")

    with pytest.raises(KeyError, match="no_such_block"):
        read_table_block({}, "no_such_block")


def test_numeric_string_is_withheld_under_its_inferred_path_too() -> None:
    column = {"sql_type": "INTEGER", "classification": "numeric"}

    assert verdict_withheld(column, "inferred.looks_like", "numeric_string")


def test_emits_answers_whether_the_column_carries_the_key() -> None:
    column = _column(inferred={"looks_like": "email"})

    assert emits(column, "inferred.looks_like")
    assert not emits(column, "inferred.sensitivity")
    assert not emits(column, "range.min")


@pytest.mark.parametrize(
    ("classification", "expected"),
    [
        ("text", {"inferred.looks_like", "inferred.epoch_unit"}),
        ("binary", {"inferred.looks_like"}),
        ("numeric", set()),
    ],
)
def test_a_failed_draw_owes_only_the_verdicts_its_classification_allows(
    classification: str,
    expected: set[str],
) -> None:
    assert sample_verdicts(classification) == expected
