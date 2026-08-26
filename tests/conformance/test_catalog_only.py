"""SPEC 2.2.15 `catalog_only`: the marker forbids any per-column measurement.

Licenses row_count's absence too, and is checked at both layers - the JSON Schema and the
conformance invariants that read beyond it.
"""

from __future__ import annotations

from typing import Any

from dbprint.conformance import Issue, statistics
from dbprint.conformance.schema_validation import check_statistics


PATH = "public/t/statistics.yaml"
FQN = "public.t"


def _codes(issues: list[Issue]) -> set[str]:
    return {i.code for i in issues}


def _payload(*, catalog_only: bool, row_count: int | None = None) -> dict[str, Any]:
    """A minimal one-column print, `catalog_only` and `row_count` set as the case needs."""

    payload: dict[str, Any] = {
        "format_version": 1,
        "table": FQN,
        "type": "table",
        "profiled_at": "2026-01-01T00:00:00Z",
        "grain": {"keys": []},
    }

    if catalog_only:
        payload["catalog_only"] = True

    if row_count is not None:
        payload["row_count"] = row_count
        payload["row_count_method"] = "exact"

    return payload


class TestUnqueriedFile:
    """SPEC Behavior: a marked file with no row_count, columns carrying sql_type/classification."""

    def test_minimal_column_is_conformant(self) -> None:
        payload = _payload(catalog_only=True)
        payload["columns"] = {
            "c": {"sql_type": "number(38,0)", "nullable": True, "classification": "numeric"},
        }

        assert check_statistics(payload, PATH) == []
        assert statistics.check(payload, PATH, FQN) == []

    def test_catalog_derivable_optional_fields_are_allowed(self) -> None:
        """Catalog-derived fields survive the marker like `sql_type`/`nullable` do."""

        payload = _payload(catalog_only=True)
        payload["physical_layout"] = {
            "mechanism": "cluster",
            "keys": [{"expression": "c", "column": "c"}],
        }
        payload["columns"] = {
            "c": {
                "sql_type": "varchar(64)",
                "nullable": False,
                "classification": "text",
                "physical_name": "C",
                "collation": "en_US.UTF-8",
                "physical_layout_key": True,
            },
        }

        assert check_statistics(payload, PATH) == []
        assert statistics.check(payload, PATH, FQN) == []

    def test_a_declared_grain_key_is_unaffected(self) -> None:
        """Declared-key introspection is catalog metadata (SPEC 2.2.12), unaffected here."""

        payload = _payload(catalog_only=True)
        payload["grain"] = {"keys": [{"columns": ["c"], "detection": "declared"}]}
        payload["columns"] = {
            "c": {"sql_type": "int", "nullable": False, "classification": "numeric"},
        }

        assert check_statistics(payload, PATH) == []
        assert statistics.check(payload, PATH, FQN) == []

    def test_no_physical_layout_or_dependencies_is_licensed_not_silent(self) -> None:
        """SPEC 2.2.15 licenses both absences by name - a query is what either requires."""

        payload = _payload(catalog_only=True)
        payload["columns"] = {
            "c": {"sql_type": "int", "nullable": False, "classification": "numeric"},
        }

        assert "physical_layout" not in payload
        assert "dependencies" not in payload
        assert check_statistics(payload, PATH) == []
        assert statistics.check(payload, PATH, FQN) == []


class TestUnqueriedFileWithoutTheMarker:
    """SPEC Behavior: the same file with the marker removed loses row_count's exemption."""

    def test_missing_row_count_is_rejected(self) -> None:
        payload = _payload(catalog_only=False)
        payload["columns"] = {
            "c": {"sql_type": "int", "nullable": True, "classification": "numeric"},
        }

        assert "schema.missing-required-field" in _codes(check_statistics(payload, PATH))


class TestMarkerPlusAMeasuredStatistic:
    """SPEC Behavior: a file claiming nothing was read may not publish a measurement."""

    def test_a_column_carrying_cardinality_is_rejected(self) -> None:
        payload = _payload(catalog_only=True)
        payload["columns"] = {
            "c": {
                "sql_type": "int",
                "nullable": True,
                "classification": "numeric",
                "cardinality": 5,
            },
        }

        schema_issues = check_statistics(payload, PATH)
        semantic_issues = statistics.check(payload, PATH, FQN)

        assert schema_issues != []
        assert "stats.measurement-under-catalog-only" in _codes(semantic_issues)

    def test_row_count_alongside_the_marker_is_rejected(self) -> None:
        payload = _payload(catalog_only=True, row_count=10)
        payload["columns"] = {
            "c": {"sql_type": "int", "nullable": True, "classification": "numeric"},
        }

        assert check_statistics(payload, PATH) != []


class TestQueriedFilesAreUnaffected:
    """Regression: the marker narrows nothing about a file that never carries it."""

    def test_empty_table_is_unchanged(self) -> None:
        payload = _payload(catalog_only=False, row_count=0)
        payload["columns"] = {
            "c": {
                "sql_type": "int",
                "nullable": True,
                "null_count": 0,
                "null_rate": 0.0,
                "classification": "categorical",
                "cardinality": 0,
                "cardinality_ratio": 0.0,
                "cardinality_method": "exact",
                "values": [],
                "values_coverage": 1.0,
                "distribution": "uniform",
            },
        }

        assert check_statistics(payload, PATH) == []
        assert statistics.check(payload, PATH, FQN) == []

    def test_unsupported_column_is_unchanged(self) -> None:
        """SPEC 3.3's narrowed clause still classifies a queried, unmodelled type `unsupported`."""

        payload = _payload(catalog_only=False, row_count=10)
        payload["columns"] = {
            "c": {
                "sql_type": "bytea",
                "nullable": True,
                "null_count": 0,
                "null_rate": 0.0,
                "classification": "unsupported",
            },
        }

        assert check_statistics(payload, PATH) == []
        assert statistics.check(payload, PATH, FQN) == []
