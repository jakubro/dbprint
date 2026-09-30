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


class TestCompletenessGuard:
    """Every producer top-level field is identity, addressable, or deferred-with-reason."""

    def test_an_unaccounted_field_fails_the_check(self) -> None:
        """Proves the guard actually fires - not just that today's schemas happen to pass."""

        fake_schema = {"properties": {"format_version": {}, "a_new_field": {}}}

        with pytest.raises(ValueError, match="a_new_field"):
            gen._check_complete(fake_schema, frozenset({"format_version"}), frozenset(), {})

    def test_deferred_reasons_are_never_empty(self) -> None:
        for reason in {**gen.STATISTICS_DEFERRED, **gen.RELATIONSHIPS_DEFERRED}.values():
            assert reason.strip()
