"""Derive the array-entry annotation schemas from the producer schemas they layer over.

Run via `just docs`; golden-tested, so a producer identity field and its annotation-schema
mirror cannot silently drift. Only the array-entry shape (`grain`, `refers_to`) is derived,
by copying each identity field's sub-schema from the producer schema; `columns` addresses by
map key and stays hand-shaped. `_check_complete()` fails on an unbucketed producer property.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_DIR = REPO_ROOT / "src" / "dbprint" / "spec" / "v1"
STATISTICS_SCHEMA_PATH = SPEC_DIR / "statistics.schema.json"
RELATIONSHIPS_SCHEMA_PATH = SPEC_DIR / "relationships.schema.json"
STATISTICS_ANNOTATIONS_PATH = SPEC_DIR / "statistics_annotations.schema.json"
RELATIONSHIPS_ANNOTATIONS_PATH = SPEC_DIR / "relationships_annotations.schema.json"

# Metadata/identity fields naming the FILE, not an addressable entry within it.
STATISTICS_IDENTITY_FIELDS = frozenset(
    {"format_version", "table", "type", "profiled_at", "catalog_only"},
)
RELATIONSHIPS_IDENTITY_FIELDS = frozenset({"format_version", "table", "profiled_at"})

# Fields with measured demand behind them (SPEC 2.7.1/2.7.2) - derived below.
STATISTICS_ADDRESSABLE = frozenset({"grain"})
RELATIONSHIPS_ADDRESSABLE = frozenset({"refers_to"})

# Not yet addressable; each reason is why it needs its own decision rather than falling out
# of this same transform for free.
STATISTICS_DEFERRED = {
    "row_count": "a scalar, not a keyed collection - nothing to address per-entry",
    "row_count_method": "a scalar, not a keyed collection",
    "scope": "a scalar/flag block, not addressable per-entry",
    "null_patterns": "identity is an unordered column SET, unlike grain's ordered array",
    "dependencies": "identity is a determinant/dependent pair - no measured demand yet",
    "timeline": "identity is a single chosen anchor column, not a keyed collection - no "
    "measured demand yet",
    "physical_layout": "identity is a clustering/partition expression, not a column name",
    "depends_on": "a bare list of FQN strings, not a keyed collection - no measured demand yet",
    "columns": "address-by-map-key (keyed-map shape), not address-by-identity-tuple",
    "unmeasured": "a producer's record of what its own run could not obtain - a human has "
    "nothing to correct about it, and annotating a field as measured would not make it so",
}
RELATIONSHIPS_DEFERRED = {
    "eligible_target": "a table-level scalar, not addressable",
    "referenced_by": "mirrors refers_to on the OTHER table; the correction belongs on the "
    "source side (SPEC 2.7.2's own edge case) - never generate a channel here",
}

_HEADER_KEYS = ("$schema", "$id", "title", "description")


def build_statistics_annotations() -> dict[str, Any]:
    """statistics.annotations.yaml's schema: hand-shaped `columns` + derived `grain`."""

    stats_schema = _load(STATISTICS_SCHEMA_PATH)
    _check_complete(
        stats_schema,
        STATISTICS_IDENTITY_FIELDS,
        STATISTICS_ADDRESSABLE,
        STATISTICS_DEFERRED,
    )

    grain_key = stats_schema["$defs"]["GrainKey"]
    columns_field = grain_key["properties"]["columns"]

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://jakubro.github.io/dbprint/spec/v1/statistics_annotations.schema.json",
        "title": "dbprint statistics.annotations.yaml v1",
        "description": "Per-table user-authored column notes. See SPEC.md section 2.7.1.",
        "type": "object",
        "required": ["format_version", "columns"],
        "additionalProperties": False,
        "properties": {
            "format_version": {"const": 1},
            "columns": _COLUMNS_PROPERTY,
            "grain": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "keys": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["columns"],
                            "properties": {
                                "columns": columns_field,
                                "note": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
    }


def build_relationships_annotations() -> dict[str, Any]:
    """relationships.annotations.yaml's schema: derived `refers_to` identity + fixed extras."""

    rel_schema = _load(RELATIONSHIPS_SCHEMA_PATH)
    _check_complete(
        rel_schema,
        RELATIONSHIPS_IDENTITY_FIELDS,
        RELATIONSHIPS_ADDRESSABLE,
        RELATIONSHIPS_DEFERRED,
    )

    refers_to = rel_schema["$defs"]["RefersTo"]
    # The addressing quintuple (SPEC 2.3.9's path-valued endpoint included). Every other
    # RefersTo property is a producer measurement, never something a human restates.
    identity_props = ("column", "path", "target_table", "target_column", "target_path")

    annotated_refers_to = {
        "type": "object",
        "required": ["column", "target_table", "target_column"],
        "additionalProperties": False,
        "properties": {
            **{name: refers_to["properties"][name] for name in identity_props},
            "note": {"type": "string"},
            "verdict": {"enum": ["rejected"]},
            "claims": {"type": "object", "additionalProperties": True},
        },
    }

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://jakubro.github.io/dbprint/spec/v1/relationships_annotations.schema.json",
        "title": "dbprint relationships.annotations.yaml v1",
        "description": "Per-table user-authored edge notes and rejections. See SPEC.md section 2.7.2.",
        "type": "object",
        "required": ["format_version", "refers_to"],
        "additionalProperties": False,
        "properties": {
            "format_version": {"const": 1},
            "refers_to": {
                "type": "array",
                "items": {"$ref": "#/$defs/AnnotatedRefersTo"},
            },
        },
        "$defs": {
            "ColumnArray": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
            },
            "AnnotatedRefersTo": annotated_refers_to,
        },
    }


# `columns` addresses by map key (SPEC 2.7.1), a different shape than grain/refers_to's
# identity-tuple addressing (see the module docstring) - hand-shaped, not derived.
_COLUMNS_PROPERTY: dict[str, Any] = {
    "type": "object",
    "additionalProperties": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "note": {"type": "string"},
            "claims": {"type": "object", "additionalProperties": True},
            "values": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["value", "note"],
                    "additionalProperties": False,
                    "properties": {
                        "value": {},
                        "note": {"type": "string"},
                    },
                },
            },
        },
    },
}


def write_schemas() -> None:
    """Regenerate both annotation schemas in place."""

    _write(STATISTICS_ANNOTATIONS_PATH, build_statistics_annotations())
    _write(RELATIONSHIPS_ANNOTATIONS_PATH, build_relationships_annotations())


def _check_complete(
    producer_schema: dict[str, Any],
    identity_fields: frozenset[str],
    addressable: frozenset[str],
    deferred: dict[str, str],
) -> None:
    """Every top-level producer property is identity, addressable, or deferred-with-reason.

    A field in none of the three is a channel-less field this check exists to catch - fails
    loudly here (and in the golden test) rather than shipping unaddressed.
    """

    accounted = identity_fields | addressable | set(deferred)
    top_level = set(producer_schema["properties"])
    missing = top_level - accounted

    if missing:
        raise ValueError(
            f"producer field(s) {sorted(missing)} are neither identity, addressable, nor "
            "deferred - add to one of the three buckets in gen_annotation_schemas.py",
        )


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, schema: dict[str, Any]) -> None:
    ordered = {k: schema[k] for k in _HEADER_KEYS} | {
        k: v for k, v in schema.items() if k not in _HEADER_KEYS
    }
    path.write_text(json.dumps(ordered, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    write_schemas()
    print(f"wrote {STATISTICS_ANNOTATIONS_PATH}")
    print(f"wrote {RELATIONSHIPS_ANNOTATIONS_PATH}")
