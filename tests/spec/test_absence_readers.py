"""Every reader of `statistics.yaml` asks `spec.absence` what an absent field means.

A static sweep, agreement with SPEC 7.2/7.3 and the 2.2.3 matrix, and each surface's mapping.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

import dbprint
from dbprint.assertions import statistic
from dbprint.assertions.parser import AssertionSet, TablePredicates
from dbprint.conformance.column_annotations import _check_claim
from dbprint.docs.view import column_view
from dbprint.engine import notes_synthesis
from dbprint.engine.diff import _diff_one_column_stats
from dbprint.mcp.tools import _column_filters, _column_matches
from dbprint.spec.absence import (
    COLUMN_FIELDS,
    TABLE_BLOCKS,
    Absence,
    read_column_field,
)
from tests.spec._spec_markdown import matrix, section, table_rows
from tests.spec.test_absence_table import _absence_table_fields


_PACKAGE = Path(dbprint.__file__).parent

_SWEPT = ("assertions", "cli", "docs", "engine", "mcp")

_NOT_READERS = {
    "conformance/statistics.py": "the validator enforces emission; the resolver is checked against it",
    "engine/manifest_builder.py": "producer",
    "engine/orchestrator.py": "producer",
    "engine/writer.py": "producer",
    "spec/absence.py": "the resolver",
}

_READS_ANOTHER_DOCUMENT: dict[tuple[str, str, str], str] = {
    ("assertions/parser.py", "_parse_tables", "row_count"): ".dbprint.yaml predicate key",
    ("conformance/column_annotations.py", "check_grain_annotations", "grain"): "annotation file",
    ("conformance/column_annotations.py", "check_value_notes", "values"): "annotation file",
    **{
        ("engine/carried.py", "_columns", key): "hydrates a committed column verbatim"
        for key in (
            "candidate_key",
            "cardinality",
            "cardinality_method",
            "looks_like",
            "redacted",
            "sensitivity",
            "sketch",
        )
    },
    ("engine/carried.py", "_entry_from_payload", "row_count"): "manifest entry",
    **{
        ("engine/carried.py", "_hydrate_statistics", key): "copies the committed baseline verbatim"
        for key in (
            "catalog_only",
            "collation",
            "depends_on",
            "grain",
            "physical_layout",
            "physical_name",
            "row_count",
            "row_count_method",
            "scope",
        )
    },
    ("engine/carried.py", "_load_table", "scope"): "records the committed scope verbatim",
    ("docs/view.py", "_annotated_grain_keys", "grain"): "annotation file",
    ("docs/view.py", "_row_count", "row_count"): "manifest entry",
    ("docs/view.py", "column_view", "values"): "annotation value notes",
    ("docs/view.py", "row_count_view", "rows_scanned"): "scope_view's own rendered mapping",
    ("engine/context_assembler.py", "_annotated_grain", "grain"): "annotation file",
    ("engine/context_assembler.py", "_annotation_entry_has_content", "values"): "annotation",
    ("engine/context_assembler.py", "_load_table_artifacts", "row_count"): "manifest entry",
    ("engine/context_assembler.py", "_markdown_annotations", "values"): "annotation",
    ("engine/context_assembler.py", "_provenance_block", "percentiles"): "statistics_params",
    ("engine/context_assembler.py", "_value_notes", "values"): "annotation",
    ("mcp/tools.py", "_column_filters", "candidate_key"): "tool arguments",
    ("mcp/tools.py", "_column_filters", "looks_like"): "tool arguments",
    ("mcp/tools.py", "_column_filters", "redacted"): "tool arguments",
    ("mcp/tools.py", "_column_filters", "sensitivity"): "tool arguments",
    ("mcp/tools.py", "_column_value_notes", "values"): "annotation",
    ("mcp/tools.py", "_matching_value_notes", "values"): "annotation",
    ("mcp/tools.py", "_search_match", "row_count"): "manifest entry",
    ("mcp/tools.py", "_tool_list_tables", "row_count"): "manifest entry",
}


def test_no_consumer_reads_an_owned_field_by_key() -> None:
    assert set(_bypasses()) - set(_READS_ANOTHER_DOCUMENT) == set()


def test_every_allowlisted_read_still_exists() -> None:
    assert set(_READS_ANOTHER_DOCUMENT) - set(_bypasses()) == set()


def test_the_sweep_flags_what_it_exists_to_catch() -> None:
    snippet = "def f(col):\n    return col.get('candidate_key') or col['cardinality']\n"

    assert {field for _, field in _hits(ast.parse(snippet))} == {"candidate_key", "cardinality"}


def test_the_column_fields_are_spec_7_2() -> None:
    assert set(_absence_table_fields()) == COLUMN_FIELDS


def test_the_table_blocks_are_spec_7_3s_statistics_rows() -> None:
    rows = table_rows(section("### 7.3 Absent blocks and files", "### 7.4"))[1:]
    names = [name for cells in rows for name in _backticked(cells[0])]

    assert set(names[: names.index("eligible_target")]) == TABLE_BLOCKS


def _forbidden_cells() -> list[tuple[str, str]]:
    classifications = [c.strip("`") for c in table_rows(section("#### 2.2.3", "#### 2.2.4"))[0][1:]]

    return [
        (classification, field)
        for field, verdicts in matrix().items()
        for classification, verdict in zip(classifications, verdicts, strict=True)
        if verdict.startswith("—")
    ]


@pytest.mark.parametrize(("classification", "field"), _forbidden_cells())
def test_every_forbidden_cell_reads_not_applicable(classification: str, field: str) -> None:
    column = {"sql_type": "VARCHAR", "classification": classification}

    if field != "cardinality_ratio":
        column["cardinality_ratio"] = 0.5

    assert read_column_field(column, field).state is Absence.NOT_APPLICABLE


_BROKEN_KEY = {
    "sql_type": "INTEGER",
    "classification": "categorical",
    "cardinality": 10,
    "cardinality_ratio": 0.0167,
}
_PROSE_COLUMN = {
    "sql_type": "VARCHAR",
    "classification": "text",
    "cardinality": 5,
    "cardinality_ratio": 0.01,
}
_LOST_VALUES = {
    "sql_type": "BOOLEAN",
    "classification": "boolean",
    "cardinality": 2,
    "cardinality_ratio": 0.0033,
    "unmeasured": ["values", "values_coverage"],
}
_LOST_CARDINALITY = {
    "sql_type": "VARCHAR",
    "classification": "categorical",
    "unmeasured": ["cardinality", "cardinality_ratio"],
}
_CATALOG_ONLY = {"sql_type": "INTEGER", "classification": "numeric"}


def _evaluate(
    columns: dict[str, dict[str, Any]],
    table: dict[str, Any] | None = None,
    **predicates: Any,
) -> dict[str, tuple[str, str]]:
    preds = TablePredicates(
        fqn="db.main.t",
        row_count=predicates.pop("row_count", None),
        columns={"c": predicates},
    )
    stats = {"db.main.t": {"columns": columns, **(table or {})}}
    issues = statistic.evaluate(AssertionSet(tables={"db.main.t": preds}), "wh", stats)

    return {i.path.rsplit(".", 1)[-1]: (i.code, i.detail) for i in issues}


@pytest.mark.parametrize(
    ("column", "stat", "expected", "code"),
    [
        (_PROSE_COLUMN, "looks_like", "email", "assertion.looks-like-mismatch"),
        (
            {**_BROKEN_KEY, "classification": "categorical"},
            "looks_like",
            "numeric_string",
            "assertion.inapplicable-stat",
        ),
    ],
    ids=["omitted-pattern", "withheld-verdict"],
)
def test_an_assertion_maps_each_reading(
    column: dict[str, Any],
    stat: str,
    expected: Any,
    code: str,
) -> None:
    assert _evaluate({"c": column}, **{stat: expected})[stat][0] == code


def test_a_non_key_asserted_as_non_key_passes() -> None:
    assert _evaluate({"c": _BROKEN_KEY}, candidate_key=False) == {}


def test_an_unmeasured_stat_says_so() -> None:
    _, detail = _evaluate({"c": _LOST_VALUES}, accepted_values=[True])["accepted_values"]

    assert "unmeasured" in detail


def test_row_count_on_a_catalog_only_file_is_inapplicable() -> None:
    issues = _evaluate({"c": _CATALOG_ONLY}, {"catalog_only": True}, row_count={"max": 10})

    assert issues["row_count"][0] == "assertion.inapplicable-stat"


def test_a_stale_claim_contradicts_rather_than_going_unassertable() -> None:
    [issue] = _check_claim("t/statistics.annotations.yaml", "c", "candidate_key", True, _BROKEN_KEY)

    assert issue.code == "annotations.claim-contradicts-statistic"
    assert "cardinality_ratio" in issue.detail


@pytest.mark.parametrize(
    ("column", "forbidden"),
    [(_LOST_VALUES, "true / "), (_LOST_CARDINALITY, "distinct")],
    ids=["lost-values", "lost-cardinality"],
)
def test_notes_render_no_number_for_a_lost_measurement(
    column: dict[str, Any],
    forbidden: str,
) -> None:
    note = notes_synthesis.synthesize(column)

    assert forbidden not in note
    assert "unmeasured:" in note


def test_the_diff_reports_a_lost_key_as_false() -> None:
    before = {**_BROKEN_KEY, "cardinality_ratio": 1.0, "inferred": {"candidate_key": True}}
    [event] = [
        e
        for e in _diff_one_column_stats("db.main.t", "c", before, _BROKEN_KEY, scoped=False)
        if e["stat"] == "inferred.candidate_key"
    ]

    assert (event["before"], event["after"]) == (True, False)


_REDACTED_NUMBER = {
    "sql_type": "INTEGER",
    "classification": "numeric",
    "cardinality": 600,
    "cardinality_ratio": 1.0,
    "redacted": "mask",
}

# One probe per non-present state (SPEC 7): the column, and the field it lacks.
_STATES: dict[str, tuple[dict[str, Any], str]] = {
    "omitted": (_BROKEN_KEY, "inferred.candidate_key"),
    "not_applicable": (_CATALOG_ONLY, "inferred.candidate_key"),
    "unmeasured": (_LOST_VALUES, "values"),
    "withheld": (_REDACTED_NUMBER, "mean"),
}
_KEYED = {"cardinality": 600, "cardinality_ratio": 1.0, "inferred": {"candidate_key": True}}
# The same column with the field present - each row's control that the surface renders the claim.
_PRESENT: dict[str, dict[str, Any]] = {
    "omitted": {**_BROKEN_KEY, **_KEYED},
    "not_applicable": {**_CATALOG_ONLY, **_KEYED},
    "unmeasured": {
        **{k: v for k, v in _LOST_VALUES.items() if k != "unmeasured"},
        "values": [{"value": True, "count": 400}, {"value": False, "count": 200}],
        "values_coverage": 1.0,
    },
    "withheld": {
        **{k: v for k, v in _REDACTED_NUMBER.items() if k != "redacted"},
        "mean": 3.5,
        "range": {"min": 1, "max": 6},
    },
}
# What each surface says when the field is present; `hints_only` states keys, never values.
_CLAIMS: dict[tuple[str, str], str] = {
    ("notes", "omitted"): "candidate key",
    ("notes", "not_applicable"): "candidate key",
    ("notes", "unmeasured"): "true / ",
    ("notes", "withheld"): "mean=",
    ("hints", "omitted"): "candidate key",
    ("hints", "not_applicable"): "candidate key",
    ("docs", "unmeasured"): "covered",
    ("docs", "withheld"): "'bounds'",
}


def _rendered(surface: str, column: dict[str, Any]) -> str:
    if surface == "notes":
        return notes_synthesis.synthesize(column)

    if surface == "hints":
        return notes_synthesis.synthesize(column, hints_only=True)

    view = column_view("c", column, 600, None, {}, {})

    return " ".join(str(value) for value in view.values())


@pytest.mark.parametrize(("surface", "state"), sorted(_CLAIMS))
def test_no_rendering_claims_what_an_absent_field_would_have_said(surface: str, state: str) -> None:
    column, field = _STATES[state]
    claim = _CLAIMS[surface, state]

    assert read_column_field(column, field).state.value == state
    assert claim in _rendered(surface, _PRESENT[state])
    assert claim not in _rendered(surface, column)


def test_the_notes_and_the_docs_page_name_an_unmeasured_field() -> None:
    view = column_view("c", _LOST_VALUES, 600, None, {}, {})

    assert "unmeasured" in notes_synthesis.synthesize(_LOST_VALUES)
    assert "values" in (view["unmeasured"] or ())


@pytest.mark.parametrize(
    ("state", "filters", "matches"),
    [
        ("omitted", {"candidate_key": True}, False),
        ("omitted", {"candidate_key": False}, True),
        ("not_applicable", {"candidate_key": True}, False),
        ("not_applicable", {"candidate_key": False}, False),
        ("withheld", {"redacted": "mask"}, True),
    ],
)
def test_search_maps_each_state(state: str, filters: dict[str, Any], matches: bool) -> None:
    column, _ = _STATES[state]

    assert _column_matches(column, _column_filters(filters)) is matches


@pytest.mark.parametrize(
    ("state", "stat", "expected", "code"),
    [
        ("omitted", "candidate_key", True, "assertion.candidate-key-mismatch"),
        ("not_applicable", "candidate_key", True, "assertion.inapplicable-stat"),
        ("unmeasured", "accepted_values", [True, False], "assertion.inapplicable-stat"),
        ("withheld", "range.min", {"max": 1}, "assertion.redacted-stat"),
    ],
)
def test_the_assertion_payload_maps_each_state(
    state: str,
    stat: str,
    expected: Any,
    code: str,
) -> None:
    column, _ = _STATES[state]

    assert _evaluate({"c": column}, **{stat: expected})[stat.rsplit(".", 1)[-1]][0] == code


def _bypasses() -> list[tuple[str, str, str]]:
    modules = [p for d in _SWEPT for p in (_PACKAGE / d).rglob("*.py")]
    modules.append(_PACKAGE / "conformance/column_annotations.py")
    found = set()

    for path in modules:
        rel = path.relative_to(_PACKAGE).as_posix()

        if rel in _NOT_READERS:
            continue

        for function in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found |= {(rel, name, field) for name, field in _hits(function)}

    return sorted(found)


def _hits(tree: ast.AST) -> set[tuple[str, str]]:
    out = set()

    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        for node in ast.walk(function):
            key = _read_key(node)

            if isinstance(key, str) and (key in _OWNED or f"inferred.{key}" in _OWNED):
                out.add((function.name, key))

    return out


def _read_key(node: ast.AST) -> object:
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ):
        return node.args[0].value

    if (
        isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Constant)
        and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops)
    ):
        return node.left.value

    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.ctx, ast.Load)
        and isinstance(node.slice, ast.Constant)
    ):
        return node.slice.value

    return None


def _backticked(cell: str) -> list[str]:
    return [part.split("`")[0] for part in cell.split("`")[1::2]]


_OWNED = COLUMN_FIELDS | TABLE_BLOCKS
