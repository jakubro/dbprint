"""MCP resource channel (`resources.read`) against the shared claims register.

The resource channel serves raw file text (MCP.md 6.1), so every claim here reads the
committed YAML/markdown the same way a client would, not a re-render through the tool layer.
"""

from __future__ import annotations

import pytest
import yaml

from dbprint.mcp import ServedConnections
from dbprint.mcp import errors as mcp_errors
from dbprint.mcp import resources as mcp_resources
from tests.fixtures.adversarial import (
    APPROXIMATE_ROW_COUNT_TABLE,
    DECLARED_MISSING_KIND,
    DECLARED_MISSING_TABLE,
    EMPTY_COLUMNS_TABLE,
    FUTURE_DATED_COLUMN,
    INCOMPLETE_GRAIN_TABLE,
    NEVER_DECLARED_KIND,
    REDACTED_COLUMN,
    SCOPED_TABLE,
    TRUNCATED_FK_COLUMN,
    UNEVALUATED_TABLE,
    AdversarialPrint,
)


COVERS = frozenset(
    {
        "scoped_table",
        "redacted_column",
        "future_dated_temporal",
        "truncated_fk_values",
        "unevaluated_diff_table",
        "empty_columns_map",
        "approximate_row_count",
        "incomplete_grain_search",
        "catalog_only_table",
        "declared_missing_artifact",
    },
)


def _state(adversarial_print: AdversarialPrint) -> ServedConnections:
    return ServedConnections(
        served={adversarial_print.conn.name: adversarial_print.conn},
        default=adversarial_print.conn.name,
    )


def _statistics(adversarial_print: AdversarialPrint, table: str) -> dict:
    conn = adversarial_print.conn.name
    path = table.replace(".", "/")
    result = mcp_resources.read(_state(adversarial_print), f"dbprint://{conn}/{path}/statistics")

    return yaml.safe_load(result.content)


def _diff(adversarial_print: AdversarialPrint) -> dict:
    conn = adversarial_print.conn.name
    result = mcp_resources.read(_state(adversarial_print), f"dbprint://{conn}/diff")

    return yaml.safe_load(result.content)


def test_scoped_table_carries_the_population(adversarial_print: AdversarialPrint) -> None:
    statistics = _statistics(adversarial_print, SCOPED_TABLE)

    assert statistics["scope"]["rows_scanned"] == 250
    assert statistics["row_count"] == 1000


def test_redacted_column_carries_no_real_literal(adversarial_print: AdversarialPrint) -> None:
    statistics = _statistics(adversarial_print, SCOPED_TABLE)
    email = statistics["columns"][REDACTED_COLUMN]

    assert email["redacted"] == "mask"
    assert all(entry["value"] != "a@example.com" for entry in email["values"])


def test_future_dated_temporal_freshness_reads_live(adversarial_print: AdversarialPrint) -> None:
    statistics = _statistics(adversarial_print, SCOPED_TABLE)
    shipped_at = statistics["columns"][FUTURE_DATED_COLUMN]

    assert shipped_at["freshness"]["classification"] == "live"


def test_truncated_fk_values_carry_their_own_coverage(adversarial_print: AdversarialPrint) -> None:
    statistics = _statistics(adversarial_print, SCOPED_TABLE)
    region_id = statistics["columns"][TRUNCATED_FK_COLUMN]

    assert region_id["values"]
    assert region_id["values_coverage"] < 1.0


def test_unevaluated_diff_table_is_never_folded_into_unchanged(
    adversarial_print: AdversarialPrint,
) -> None:
    diff = _diff(adversarial_print)

    assert diff["summary"]["unevaluated_tables"] > 0
    assert diff["summary"]["unchanged_tables"] == 0


def test_empty_columns_map_carries_the_zero_scan_marker(
    adversarial_print: AdversarialPrint,
) -> None:
    statistics = _statistics(adversarial_print, EMPTY_COLUMNS_TABLE)

    assert statistics["columns"] == {}
    assert statistics["scope"]["rows_scanned"] == 0


def test_approximate_row_count_carries_its_own_method(adversarial_print: AdversarialPrint) -> None:
    statistics = _statistics(adversarial_print, APPROXIMATE_ROW_COUNT_TABLE)

    assert statistics["row_count_method"] == "approximate"

    row_count_changes = [
        c for c in _diff(adversarial_print)["changes"] if c.get("kind") == "table_row_count_changed"
    ]

    for change in row_count_changes:
        assert "before_method" in change
        assert "after_method" in change


def test_incomplete_grain_search_carries_exhausted_false(
    adversarial_print: AdversarialPrint,
) -> None:
    statistics = _statistics(adversarial_print, INCOMPLETE_GRAIN_TABLE)

    assert statistics["grain"]["search"]["exhausted"] is False


def test_catalog_only_table_carries_no_dependency_or_layout_key(
    adversarial_print: AdversarialPrint,
) -> None:
    """SPEC 2.2.15 forbids both under the marker; absence is licensed, not a producer gap."""

    statistics = _statistics(adversarial_print, UNEVALUATED_TABLE)

    assert statistics["catalog_only"] is True
    assert "physical_layout" not in statistics
    assert "dependencies" not in statistics


def test_declared_missing_artifact_reads_a_different_error_than_never_declared(
    adversarial_print: AdversarialPrint,
) -> None:
    conn = adversarial_print.conn.name
    path = DECLARED_MISSING_TABLE.replace(".", "/")
    state = _state(adversarial_print)

    with pytest.raises(mcp_errors.McpError) as declared_missing:
        mcp_resources.read(state, f"dbprint://{conn}/{path}/{DECLARED_MISSING_KIND}")

    with pytest.raises(mcp_errors.McpError) as never_declared:
        mcp_resources.read(state, f"dbprint://{conn}/{path}/{NEVER_DECLARED_KIND}")

    assert declared_missing.value.code != never_declared.value.code


def test_declared_missing_optional_kind_reads_a_different_error_than_never_declared(
    adversarial_print: AdversarialPrint,
) -> None:
    """The same distinction as above, for a human-authored (optional) kind.

    `adversarial_print` is session-scoped, so the manifest mutation is undone before returning.
    """

    conn = adversarial_print.conn.name
    path = DECLARED_MISSING_TABLE.replace(".", "/")
    state = _state(adversarial_print)
    manifest_path = adversarial_print.print_root / "manifest.yaml"
    original = manifest_path.read_text()

    try:
        manifest = yaml.safe_load(original)
        manifest["tables"][DECLARED_MISSING_TABLE]["artifacts"]["description"] = "description.md"
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))

        with pytest.raises(mcp_errors.McpError) as declared_missing:
            mcp_resources.read(state, f"dbprint://{conn}/{path}/description")

        with pytest.raises(mcp_errors.McpError) as never_declared:
            mcp_resources.read(state, f"dbprint://{conn}/{path}/statistics_annotations")

        assert declared_missing.value.code != never_declared.value.code
    finally:
        manifest_path.write_text(original)
