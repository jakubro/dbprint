"""JSON Schema violations mapped onto the SPEC 6.3 catalog, per SPEC 2.2-2.7 and 6.3."""

from __future__ import annotations

from typing import Any

import pytest

from dbprint.conformance import schema_validation as sv
from dbprint.conformance.issue import Issue


_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "format_version": {"const": 1},
        "label": {"const": "fixed"},
        "shade": {"enum": ["red", "blue"]},
        "code": {"type": "string", "pattern": "^[a-z]+$"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "classification": {"enum": ["numeric"]},
                    "kind": {"enum": ["column_added"]},
                    "looks_like": {"enum": ["email"]},
                    "sensitivity": {"enum": ["contact"]},
                    "epoch_unit": {"enum": ["seconds"]},
                    "percentiles": {
                        "type": "object",
                        "patternProperties": {"^p[0-9]{2}$": {"type": "number"}},
                        "additionalProperties": False,
                    },
                    "tags": {"type": "object", "propertyNames": {"pattern": "^t_"}},
                },
            },
        },
    },
    "required": ["label"],
}


def _issues(data: Any) -> list[Issue]:
    return sv._check(_SCHEMA, data, "p.yaml", "§9.9")


class TestEachViolationMapsToItsCatalogCode:
    @pytest.mark.parametrize(
        ("field", "code", "spec_ref"),
        [
            ("classification", "schema.unknown-classification", "§3"),
            ("kind", "schema.unknown-change-kind", "§2.6.6"),
            ("looks_like", "schema.unknown-looks-like", "§4.1.6"),
            ("sensitivity", "schema.unknown-sensitivity", "§4.4"),
            ("epoch_unit", "schema.unknown-epoch-unit", "§4.5"),
        ],
    )
    def test_an_unknown_value_of_a_growing_vocabulary_warns(
        self,
        field: str,
        code: str,
        spec_ref: str,
    ) -> None:
        assert _issues({"label": "fixed", "items": [{}, {field: "novel"}]}) == [
            Issue(f"p.yaml::items[1].{field}", code, "warning", _not_one_of(field), spec_ref),
        ]

    def test_an_unknown_value_of_a_closed_vocabulary_is_an_error(self) -> None:
        assert _issues({"label": "fixed", "shade": "green"}) == [
            Issue(
                "p.yaml::shade",
                "schema.type-mismatch",
                "error",
                "'green' is not one of ['red', 'blue']",
                "§9.9",
            ),
        ]

    def test_a_missing_required_field(self) -> None:
        assert _issues({}) == [
            Issue(
                "p.yaml",
                "schema.missing-required-field",
                "error",
                "'label' is a required property",
                "§9.9",
            ),
        ]

    def test_a_property_name_pattern_outside_percentiles(self) -> None:
        assert _issues({"label": "fixed", "items": [{"tags": {"x": 1}}]}) == [
            Issue(
                "p.yaml::items[0].tags",
                "schema.type-mismatch",
                "error",
                "'x' does not match '^t_'",
                "§9.9",
            ),
        ]

    def test_a_pattern_violation_under_percentiles_names_the_percentile_rule(self) -> None:
        schema = {
            "type": "object",
            "properties": {"percentiles": {"type": "object", "propertyNames": {"pattern": "^p"}}},
        }

        assert sv._check(schema, {"percentiles": {"median": 1}}, "p.yaml", "§9.9") == [
            Issue(
                "p.yaml::percentiles",
                "schema.invalid-percentile-key",
                "error",
                "'median' does not match '^p'",
                "§2.2.4",
            ),
        ]

    def test_a_pattern_violation_elsewhere_is_a_type_mismatch(self) -> None:
        assert _issues({"label": "fixed", "code": "ABC"}) == [
            Issue(
                "p.yaml::code",
                "schema.type-mismatch",
                "error",
                "'ABC' does not match '^[a-z]+$'",
                "§9.9",
            ),
        ]

    def test_an_unknown_format_version_is_reported_at_the_file(self) -> None:
        assert _issues({"label": "fixed", "format_version": 2}) == [
            Issue("p.yaml", "version.unknown-format-version", "error", "1 was expected", "§5"),
        ]

    def test_any_other_constant_violation_is_a_type_mismatch(self) -> None:
        assert _issues({"label": "other"}) == [
            Issue("p.yaml::label", "schema.type-mismatch", "error", "'fixed' was expected", "§9.9"),
        ]

    def test_a_wrong_type_is_a_type_mismatch(self) -> None:
        assert _issues({"label": "fixed", "code": 5}) == [
            Issue(
                "p.yaml::code",
                "schema.type-mismatch",
                "error",
                "5 is not of type 'string'",
                "§9.9",
            ),
        ]

    def test_every_violation_in_the_document_is_reported(self) -> None:
        assert sorted(i.path for i in _issues({"shade": "green", "code": 5})) == [
            "p.yaml",
            "p.yaml::code",
            "p.yaml::shade",
        ]


class TestEachArtifactCitesItsOwnSection:
    @pytest.mark.parametrize(
        ("check", "spec_ref"),
        [
            (sv.check_statistics, "§2.2"),
            (sv.check_relationships, "§2.3"),
            (sv.check_manifest, "§2.5"),
            (sv.check_diff, "§2.6"),
            (sv.check_statistics_annotations, "§2.7.1"),
            (sv.check_relationships_annotations, "§2.7.2"),
            (sv.check_manifest_annotations, "§2.7.3"),
        ],
    )
    def test_a_body_that_is_not_a_mapping(self, check: Any, spec_ref: str) -> None:
        assert check(5, "p.yaml") == [
            Issue("p.yaml", "schema.type-mismatch", "error", "5 is not of type 'object'", spec_ref),
        ]

    def test_the_relationships_schema_is_the_packaged_one(self) -> None:
        assert sv.relationships_schema()["title"] == "dbprint relationships.yaml v1"


def _not_one_of(field: str) -> str:
    return f"'novel' is not one of {_SCHEMA['properties']['items']['items']['properties'][field]['enum']}"


def test_an_enum_violation_at_the_document_root_is_addressed_at_the_file() -> None:
    assert sv._check({"enum": ["a"]}, "b", "p.yaml", "§9.9") == [
        Issue("p.yaml", "schema.type-mismatch", "error", "'b' is not one of ['a']", "§9.9"),
    ]
