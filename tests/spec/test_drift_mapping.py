"""Every field a print records has exactly one drift rule, and every diff kind has a family.

Fields come from walking the packaged JSON schemas and the manifest's artifact kinds, never a list.
"""

from __future__ import annotations

import importlib.resources
import json
from typing import Any, cast

import pytest

from dbprint.spec import drift
from dbprint.spec.drift import Data, Marker, Shape, Uncompared


_ARTIFACT_SCHEMAS = {
    "manifest": "manifest.schema.json",
    "statistics": "statistics.schema.json",
    "relationships": "relationships.schema.json",
}
_FILE_ARTIFACTS = {
    "ddl": "ddl",
    "description": "description",
    "statistics_annotations": "statistics_annotations",
    "relationships_annotations": "relationships_annotations",
}


def _schema(name: str) -> dict[str, Any]:
    text = importlib.resources.files("dbprint.spec.v1").joinpath(name).read_text(encoding="utf-8")

    return json.loads(text)


def _paths(schema: dict[str, Any]) -> set[str]:
    out: set[str] = set()

    def walk(node: Any, prefix: str, seen: tuple[str, ...]) -> None:
        if not isinstance(node, dict):
            return

        ref = node.get("$ref")

        if isinstance(ref, str) and ref not in seen:
            walk(schema["$defs"][ref.split("/")[-1]], prefix, (*seen, ref))

        for key in ("allOf", "anyOf", "oneOf"):
            for sub in node.get(key, []):
                walk(sub, prefix, seen)

        for key in ("if", "then", "else"):
            walk(node.get(key), prefix, seen)

        for name, sub in (node.get("properties") or {}).items():
            path = f"{prefix}.{name}" if prefix else name
            out.add(path)
            walk(sub, path, seen)

        star = f"{prefix}.*" if prefix else "*"
        walk(node.get("additionalProperties"), star, seen)

        for sub in (node.get("patternProperties") or {}).values():
            walk(sub, star, seen)

        walk(node.get("items"), star, seen)

    walk(schema, "", ())

    return out


def _universe() -> set[tuple[str, str]]:
    fields = {
        (artifact, path)
        for artifact, name in _ARTIFACT_SCHEMAS.items()
        for path in _paths(_schema(name))
    }
    artifacts = _schema("manifest.schema.json")["$defs"]["Artifacts"]["properties"]

    return fields | {(_FILE_ARTIFACTS[key], "") for key in artifacts if key in _FILE_ARTIFACTS}


def _unruled(universe: set[tuple[str, str]]) -> list:
    missing = []

    for artifact, path in sorted(universe):
        try:
            rule = drift.rule_for(cast("drift.Artifact", artifact), path)
        except KeyError:
            missing.append((artifact, path))
            continue

        if _is_membership(rule) and (artifact, path) not in drift.FIELD_RULES:
            missing.append((artifact, path))

    return missing


def _is_membership(rule: drift.Rule) -> bool:
    """A container rule reporting only additions and removals says nothing about its entries."""

    return isinstance(rule, Shape) and sorted(k.rsplit("_", 1)[-1] for k in rule.kinds) == [
        "added",
        "removed",
    ]


def test_every_recorded_field_has_a_rule() -> None:
    assert _unruled(_universe()) == []


def test_every_manifest_artifact_kind_is_ruled() -> None:
    declared = set(_schema("manifest.schema.json")["$defs"]["Artifacts"]["properties"])

    assert declared - {"statistics", "relationships"} == set(_FILE_ARTIFACTS)


def test_no_rule_names_a_field_that_does_not_exist() -> None:
    universe = _universe() | {(artifact, "") for artifact in _FILE_ARTIFACTS.values()}

    assert [key for key in drift.FIELD_RULES if key not in universe] == []


def test_every_diff_kind_has_exactly_one_family() -> None:
    kinds = set(_schema("diff.schema.json")["$defs"]["Kind"]["enum"])

    assert kinds == drift.SHAPE_CHANGE_KINDS | drift.DATA_CHANGE_KINDS
    assert drift.SHAPE_CHANGE_KINDS & drift.DATA_CHANGE_KINDS == frozenset()


@pytest.mark.parametrize(
    "planted",
    [("statistics", "a_future_block"), ("statistics", "columns.*.a_future_stat")],
    ids=["table", "column"],
)
def test_a_planted_unmapped_field_is_caught(planted: tuple[str, str]) -> None:
    universe = _universe() | {planted}

    assert _unruled(universe) == [planted]


@pytest.mark.parametrize(
    ("path", "family"),
    [
        ("physical_name", Shape),
        ("collation", Shape),
        ("physical_layout_key", Uncompared),
        ("range.min", Data),
        ("inferred.sampled", Uncompared),
        ("inferred.looks_like", Data),
        ("unmeasured", Marker),
    ],
)
def test_a_column_path_resolves_through_its_nearest_rule(path: str, family: type) -> None:
    assert isinstance(drift.column_field_rule(path), family)


def test_change_kinds_map_to_their_drift_family() -> None:
    assert drift.family_of("statistic_changed") == "data"
    assert drift.family_of("column_added") == "shape"

    with pytest.raises(KeyError, match="no_such_kind"):
        drift.family_of("no_such_kind")


def test_an_unmapped_path_names_itself_in_the_error() -> None:
    with pytest.raises(KeyError, match="no_such_field"):
        drift.rule_for("manifest", "no_such_field")
