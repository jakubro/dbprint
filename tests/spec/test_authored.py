"""`spec.authored`: an annotation file is admitted only whole, by its schema and string keys."""

from __future__ import annotations

import copy
import datetime
from typing import Any

import pytest

from dbprint.spec.authored import admitted, malformation, non_string_keys


_STATISTICS = {
    "format_version": 1,
    "columns": {
        "status": {
            "note": "lifecycle stage",
            "claims": {"cardinality": 3},
            "values": [{"value": "seed", "note": "planted"}, {"value": 2, "note": "two"}],
            "derived_from": [{"table": "g.garden.beds", "column": "name"}],
            "rejected": ["candidate_key", "sensitivity"],
        },
    },
    "grain": {"keys": [{"columns": ["id"], "note": "one row per plant"}]},
}
_RELATIONSHIPS = {
    "format_version": 1,
    "refers_to": [
        {
            "column": ["bed_id"],
            "target_table": "g.garden.beds",
            "target_column": ["id"],
            "note": "each plant sits in one bed",
        },
    ],
}
_MANIFEST = {"format_version": 1, "notes": "Beds and plants."}


@pytest.mark.parametrize(
    ("kind", "data"),
    [
        ("statistics_annotations", _STATISTICS),
        ("relationships_annotations", _RELATIONSHIPS),
        ("manifest_annotations", _MANIFEST),
    ],
)
def test_a_documented_shape_is_admitted(kind: str, data: dict[str, Any]) -> None:
    assert malformation(kind, data) is None
    assert admitted(kind, data) == data


def _with(base: dict[str, Any], path: tuple[Any, ...], value: Any) -> dict[str, Any]:
    out = copy.deepcopy(base)
    node: Any = out

    for step in path[:-1]:
        node = node[step]

    node[path[-1]] = value

    return out


@pytest.mark.parametrize(
    ("kind", "data", "address"),
    [
        ("statistics_annotations", _with(_STATISTICS, ("columns",), ["status"]), "columns"),
        ("statistics_annotations", _with(_STATISTICS, ("columns",), 7), "columns"),
        (
            "statistics_annotations",
            _with(_STATISTICS, ("grain", "keys", 0, "columns"), [{"id": 1}]),
            "grain.keys[0].columns[0]",
        ),
        (
            "statistics_annotations",
            _with(_STATISTICS, ("columns", "status", "values", 0, "value"), ["seed"]),
            "columns.status.values[0].value",
        ),
        (
            "statistics_annotations",
            _with(_STATISTICS, ("columns", "status", "note"), 5),
            "columns.status.note",
        ),
        (
            "statistics_annotations",
            _with(_STATISTICS, ("columns", "status", "rejected"), ["cardinality"]),
            "columns.status.rejected[0]",
        ),
        (
            "statistics_annotations",
            _with(_STATISTICS, ("columns", "status", "rejected"), ["looks_like", "looks_like"]),
            "columns.status.rejected",
        ),
        (
            "relationships_annotations",
            _with(_RELATIONSHIPS, ("refers_to", 0, "target_table"), ["g.garden.beds"]),
            "refers_to[0].target_table",
        ),
        ("relationships_annotations", _with(_RELATIONSHIPS, ("refers_to",), 7), "refers_to"),
        ("manifest_annotations", _with(_MANIFEST, ("notes",), ["x"]), "notes"),
    ],
)
def test_a_malformed_shape_names_its_address(kind: str, data: Any, address: str) -> None:
    found = malformation(kind, data)

    assert found is not None
    assert found.startswith(f"{address}: ")
    assert admitted(kind, data) is None


@pytest.mark.parametrize("key", [1, True, 1.5, None])
def test_a_non_string_key_is_refused_naming_the_key_and_its_type(key: Any) -> None:
    data = _with(_STATISTICS, ("columns", "status", "claims", key), 3)

    assert non_string_keys(data) == [("columns", "status", "claims", key)]
    assert malformation("statistics_annotations", data) == (
        f"columns.status.claims.{key}: mapping key {key!r} ({type(key).__name__}) is not a "
        "string; quote it"
    )


def test_a_root_that_is_not_a_mapping_is_refused() -> None:
    assert malformation("manifest_annotations", ["notes"]) == "root: a list, not a mapping"


def test_the_first_problem_in_document_order_is_named() -> None:
    data = _with(_STATISTICS, ("columns", "status", "note"), 5)
    data = _with(data, ("grain", "keys", 0, "columns"), "id")

    assert (malformation("statistics_annotations", data) or "").startswith("columns.status.note")


def test_a_root_level_problem_is_addressed_at_the_root() -> None:
    assert malformation("manifest_annotations", {"notes": "x"}) == (
        "root: 'format_version' is a required property"
    )


def test_a_non_string_key_is_named_ahead_of_the_schema_error_at_its_mapping() -> None:
    assert (malformation("manifest_annotations", {"format_version": 1, 1: "x"}) or "").startswith(
        "1: mapping key 1 (int)",
    )


def test_an_unquoted_date_is_read_as_its_iso_text() -> None:
    data = _with(_STATISTICS, ("columns", "status", "note"), datetime.date(2024, 1, 1))

    assert malformation("statistics_annotations", data) is None
    assert (admitted("statistics_annotations", data) or {})["columns"]["status"]["note"] == (
        "2024-01-01"
    )


def test_a_key_s_type_is_named_by_its_builtin_kind() -> None:
    class Sourced(int):
        pass

    data = _with(_STATISTICS, ("columns", "status", "claims", Sourced(1)), 3)

    assert "(int)" in (malformation("statistics_annotations", data) or "")
