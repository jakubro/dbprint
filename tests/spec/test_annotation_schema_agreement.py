"""The two array-entry annotation schemas must match what scripts/gen_annotation_schemas.py
derives from the producer schemas they layer over (SPEC 2.7.1 grain, 2.7.2 refers_to).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _load_generator():
    """Import scripts/gen_annotation_schemas.py so the test shares the derivation path."""

    path = Path(__file__).resolve().parents[2] / "scripts" / "gen_annotation_schemas.py"
    spec = importlib.util.spec_from_file_location("gen_annotation_schemas", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module


gen = _load_generator()

# The producer schemas' own location on disk, resolved independently of gen's path constants
# and read with a plain json.load - so a bug shared by both reads cannot cancel out.
_SPEC_DIR = Path(__file__).resolve().parents[2] / "src" / "dbprint" / "spec" / "v1"
_STATISTICS_SCHEMA_PATH = _SPEC_DIR / "statistics.schema.json"
_RELATIONSHIPS_SCHEMA_PATH = _SPEC_DIR / "relationships.schema.json"


def _load_schema(path: Path) -> dict:
    """Parse a real producer schema file directly, independent of the generator."""

    return json.loads(path.read_text())


class TestGoldenReference:
    def test_statistics_annotations_schema_matches_derivation(self) -> None:
        committed = json.loads(gen.STATISTICS_ANNOTATIONS_PATH.read_text())
        assert committed == gen.build_statistics_annotations(), (
            "statistics_annotations.schema.json is out of date. Run `just docs` and commit."
        )

    def test_relationships_annotations_schema_matches_derivation(self) -> None:
        committed = json.loads(gen.RELATIONSHIPS_ANNOTATIONS_PATH.read_text())
        assert committed == gen.build_relationships_annotations(), (
            "relationships_annotations.schema.json is out of date. Run `just docs` and commit."
        )

    def test_golden_check_detects_drift_in_a_derived_identity_field(self) -> None:
        """A producer constraint change must show up as a real diff, not a vacuous pass."""

        committed = json.loads(gen.STATISTICS_ANNOTATIONS_PATH.read_text())
        rendered = gen.build_statistics_annotations()

        # Both sides parsed, so only the mutation below can separate them - comparing the
        # compact dump against the file's indented text would differ on whitespace alone.
        assert rendered == committed

        rendered["properties"]["grain"]["properties"]["keys"]["items"]["properties"]["columns"][
            "minItems"
        ] = 99

        assert rendered != committed


class TestIdentityFieldsMirrorTheProducer:
    """grain/refers_to's addressing constraints are copied, never retyped by hand."""

    def test_grain_columns_constraint_matches_the_producer_schema(self) -> None:
        stats_schema = _load_schema(_STATISTICS_SCHEMA_PATH)
        producer_columns = stats_schema["$defs"]["GrainKey"]["properties"]["columns"]

        derived = gen.build_statistics_annotations()
        annotated_columns = derived["properties"]["grain"]["properties"]["keys"]["items"][
            "properties"
        ]["columns"]

        assert annotated_columns == producer_columns

    def test_refers_to_identity_fields_match_the_producer_schema(self) -> None:
        rel_schema = _load_schema(_RELATIONSHIPS_SCHEMA_PATH)
        producer = rel_schema["$defs"]["RefersTo"]["properties"]

        derived = gen.build_relationships_annotations()
        annotated = derived["$defs"]["AnnotatedRefersTo"]["properties"]

        for name in ("column", "path", "target_table", "target_column", "target_path"):
            assert annotated[name] == producer[name]


class TestCompletenessGuard:
    """Every producer top-level field is identity, addressable, or deferred-with-reason."""

    def test_every_statistics_field_is_accounted_for(self) -> None:
        stats_schema = _load_schema(_STATISTICS_SCHEMA_PATH)

        gen._check_complete(
            stats_schema,
            gen.STATISTICS_IDENTITY_FIELDS,
            gen.STATISTICS_ADDRESSABLE,
            gen.STATISTICS_DEFERRED,
        )

    def test_every_relationships_field_is_accounted_for(self) -> None:
        rel_schema = gen._load(gen.RELATIONSHIPS_SCHEMA_PATH)

        gen._check_complete(
            rel_schema,
            gen.RELATIONSHIPS_IDENTITY_FIELDS,
            gen.RELATIONSHIPS_ADDRESSABLE,
            gen.RELATIONSHIPS_DEFERRED,
        )

    def test_an_unaccounted_field_fails_the_check(self) -> None:
        """Proves the guard actually fires - not just that today's schemas happen to pass."""

        fake_schema = {"properties": {"format_version": {}, "a_new_field": {}}}

        with pytest.raises(ValueError, match="a_new_field"):
            gen._check_complete(fake_schema, frozenset({"format_version"}), frozenset(), {})

    def test_deferred_reasons_are_never_empty(self) -> None:
        for reason in {**gen.STATISTICS_DEFERRED, **gen.RELATIONSHIPS_DEFERRED}.values():
            assert reason.strip()
