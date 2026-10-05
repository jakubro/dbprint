"""One partition decides what a `redacted` marker withholds, and every reader takes it from there.

A statistics field without a role, or a reader holding its own withheld set, fails here.
"""

from __future__ import annotations

import ast
import json
from importlib import resources
from pathlib import Path
from typing import Any

import dbprint
from dbprint.conformance import diff as conformance_diff
from dbprint.engine import diff as engine_diff
from dbprint.spec.redaction import (
    FIELD_ROLES,
    NOT_COMPARED_UNDER_REDACTION,
    WITHHELD_UNDER_REDACTION,
    apply_redaction_rule,
)


_PACKAGE = Path(dbprint.__file__).parent

_READS_THE_MARKER_FOR_ANOTHER_REASON: dict[tuple[str, str], str] = {
    (
        "conformance/statistics.py",
        "_check_span_days",
    ): "reads the primitive to skip substituted bounds",
    (
        "conformance/statistics.py",
        "_check_sketch",
    ): "keeps its own code, stats.sketch-on-redacted-column",
    ("engine/carried.py", "_columns"): "hydrates the committed marker and sketch verbatim",
    (
        "conformance/statistics.py",
        "_check_mean_containment",
    ): "skips a check substituted bounds cannot satisfy",
    (
        "conformance/statistics.py",
        "_check_max_age_days_mismatch",
    ): "skips a check substituted bounds cannot satisfy",
    (
        "engine/orchestrator.py",
        "_normalize_table",
    ): "skips a query whose result the partition withholds",
    (
        "conformance/statistics.py",
        "_check_unredacted_sensitive",
    ): "warns when a cell value is published with no marker",
}


def _schema() -> dict[str, Any]:
    text = resources.files("dbprint.spec.v1").joinpath("statistics.schema.json").read_text()

    return json.loads(text)


def test_every_column_property_has_exactly_one_role() -> None:
    column_properties = set(_schema()["$defs"]["Column"]["properties"])

    assert set(FIELD_ROLES) == column_properties


def test_both_diffs_exclude_what_the_partition_withholds_or_substitutes() -> None:
    assert engine_diff._REDACTED_EXCLUDED_STATS is NOT_COMPARED_UNDER_REDACTION
    assert conformance_diff._VALUE_BEARING_STATS is NOT_COMPARED_UNDER_REDACTION


def test_the_schema_forbids_the_withheld_set_under_a_marker() -> None:
    rule = _schema()["$defs"]["NothingWithheldWhenRedacted"]
    named = {entry["required"][0] for entry in rule["then"]["not"]["anyOf"]}

    assert rule["if"]["required"] == ["redacted"]
    assert named == WITHHELD_UNDER_REDACTION


def test_apply_redaction_rule_keeps_the_count_profile_and_drops_the_rest() -> None:
    column = {field: 1 for field in FIELD_ROLES} | {"redacted": "mask"}

    apply_redaction_rule(column)

    assert set(column) == {
        "cardinality",
        "cardinality_method",
        "cardinality_ratio",
        "classification",
        "collation",
        "distribution",
        "frequencies",
        "freshness",
        "geometry",
        "inferred",
        "null_count",
        "null_rate",
        "nullable",
        "occurrences",
        "parts",
        "parts_found",
        "percentiles",
        "physical_layout_key",
        "physical_name",
        "populated",
        "range",
        "redacted",
        "rows_scanned",
        "size",
        "sql_type",
        "types",
        "unmeasured",
        "values",
        "values_coverage",
        "values_coverage_method",
    }


def test_apply_redaction_rule_leaves_an_unmarked_column_alone() -> None:
    column = {field: 1 for field in FIELD_ROLES if field != "redacted"}

    apply_redaction_rule(column)

    assert set(column) == set(FIELD_ROLES) - {"redacted"}


def test_no_other_module_decides_what_a_marker_withholds() -> None:
    hits = sorted(
        (path.relative_to(_PACKAGE).as_posix(), function.name)
        for path in _PACKAGE.rglob("*.py")
        if path.relative_to(_PACKAGE).as_posix() != "spec/redaction.py"
        for function in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(function, ast.FunctionDef) and _gates_a_withheld_field(function)
    )

    assert hits == sorted(_READS_THE_MARKER_FOR_ANOTHER_REASON)


def test_the_sweep_flags_what_it_exists_to_catch() -> None:
    snippet = ast.parse(
        "def f(col):\n    if col.get('redacted') is not None:\n        col.pop('mean', None)\n"
        "def g(col):\n    if is_redacted(col):\n        col.pop('mean', None)\n",
    )

    assert all(
        _gates_a_withheld_field(function)
        for function in ast.walk(snippet)
        if isinstance(function, ast.FunctionDef)
    )


def _gates_a_withheld_field(function: ast.FunctionDef) -> bool:
    names = {
        node.value
        for node in ast.walk(function)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    return _reads_redacted(function) and bool(names & WITHHELD_UNDER_REDACTION)


def _reads_redacted(function: ast.FunctionDef) -> bool:
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name | ast.Attribute)
            and (node.func.id if isinstance(node.func, ast.Name) else node.func.attr)
            == "is_redacted"
        ):
            return True

        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and _is_redacted_key(node.args[0])
        ):
            return True

        if isinstance(node, ast.Subscript) and _is_redacted_key(node.slice):
            return True

        if isinstance(node, ast.Compare) and _is_redacted_key(node.left):
            return True

    return False


def _is_redacted_key(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value == "redacted"
