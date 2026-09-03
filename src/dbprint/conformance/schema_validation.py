"""JSON Schema validation per SPEC 2.2-2.6.

jsonschema errors map to the SPEC 6.3 catalog; enum violations on forward-compat fields warn.
"""

from __future__ import annotations

import importlib.resources
import json
from collections.abc import Iterable
from typing import Any, Literal

from jsonschema import Draft202012Validator

from .issue import Issue


Severity = Literal["error", "warning"]


_FORWARD_COMPAT_ENUM_FIELDS: dict[str, tuple[str, str]] = {
    "classification": ("schema.unknown-classification", "§3"),
    "kind": ("schema.unknown-change-kind", "§2.6.6"),
    "looks_like": ("schema.unknown-looks-like", "§4.1.6"),
    "sensitivity": ("schema.unknown-sensitivity", "§4.4"),
    "epoch_unit": ("schema.unknown-epoch-unit", "§4.5"),
}


def _load_schema(name: str) -> dict[str, Any]:
    text = importlib.resources.files("dbprint.spec.v1").joinpath(name).read_text(encoding="utf-8")

    return json.loads(text)


_STATS = _load_schema("statistics.schema.json")
_REL = _load_schema("relationships.schema.json")
_MANIFEST = _load_schema("manifest.schema.json")
_DIFF = _load_schema("diff.schema.json")
_STATISTICS_ANNOTATIONS = _load_schema("statistics_annotations.schema.json")
_RELATIONSHIPS_ANNOTATIONS = _load_schema("relationships_annotations.schema.json")
_MANIFEST_ANNOTATIONS = _load_schema("manifest_annotations.schema.json")


def check_statistics(data: Any, path: str) -> list[Issue]:
    """Validate a statistics.yaml body against the packaged JSON Schema."""

    return _check(_STATS, data, path, "§2.2")


def check_relationships(data: Any, path: str) -> list[Issue]:
    """Validate a relationships.yaml body against the packaged JSON Schema."""

    return _check(_REL, data, path, "§2.3")


def check_manifest(data: Any, path: str) -> list[Issue]:
    """Validate a manifest.yaml body against the packaged JSON Schema."""

    return _check(_MANIFEST, data, path, "§2.5")


def check_diff(data: Any, path: str) -> list[Issue]:
    """Validate a diff.yaml body against the packaged JSON Schema."""

    return _check(_DIFF, data, path, "§2.6")


def check_statistics_annotations(data: Any, path: str) -> list[Issue]:
    """Validate a statistics.annotations.yaml body against the packaged JSON Schema."""

    return _check(_STATISTICS_ANNOTATIONS, data, path, "§2.7.1")


def check_relationships_annotations(data: Any, path: str) -> list[Issue]:
    """Validate a relationships.annotations.yaml body against the packaged JSON Schema."""

    return _check(_RELATIONSHIPS_ANNOTATIONS, data, path, "§2.7.2")


def check_manifest_annotations(data: Any, path: str) -> list[Issue]:
    """Validate a manifest.annotations.yaml body against the packaged JSON Schema."""

    return _check(_MANIFEST_ANNOTATIONS, data, path, "§2.7.3")


def _check(schema: dict[str, Any], data: Any, path: str, spec_ref: str) -> list[Issue]:
    validator = Draft202012Validator(schema)
    issues: list[Issue] = []

    for err in validator.iter_errors(data):
        code, severity, ref = _classify(err, spec_ref)
        pointer = () if code in _DOCUMENT_LEVEL_CODES else err.absolute_path
        issues.append(Issue(_address(path, pointer), code, severity, err.message, ref))

    return issues


# Codes not tied to one field: `format_version.py` reports at the file, not a key.
_DOCUMENT_LEVEL_CODES = frozenset({"version.unknown-format-version"})


def _address(path: str, pointer: Iterable[Any]) -> str:
    """Address an issue at the field it names, or the file when it has none.

    jsonschema errors carry no location, so segments are joined as `.name`/`[i]`, the same
    convention the semantic checks use. A document-wide violation keeps the file path alone.
    """

    rendered = ""

    for segment in pointer:
        if isinstance(segment, int):
            rendered += f"[{segment}]"
        else:
            rendered += f".{segment}" if rendered else str(segment)

    return f"{path}::{rendered}" if rendered else path


def _classify(err, default_ref: str) -> tuple[str, Severity, str]:
    if err.validator == "enum":
        field = list(err.absolute_path)[-1] if err.absolute_path else None

        if isinstance(field, str) and field in _FORWARD_COMPAT_ENUM_FIELDS:
            code, ref = _FORWARD_COMPAT_ENUM_FIELDS[field]

            return code, "warning", ref

        return "schema.type-mismatch", "error", default_ref
    elif err.validator == "required":
        return "schema.missing-required-field", "error", default_ref
    elif err.validator == "pattern":
        path_parts = list(err.absolute_path)

        if "percentiles" in path_parts:
            return "schema.invalid-percentile-key", "error", "§2.2.4"

        return "schema.type-mismatch", "error", default_ref
    elif err.validator == "const":
        # const violation on format_version produces version-specific code
        if list(err.absolute_path) == ["format_version"]:
            return "version.unknown-format-version", "error", "§5"

        return "schema.type-mismatch", "error", default_ref
    else:
        return "schema.type-mismatch", "error", default_ref
