"""MCP tool channel (`dispatch`) against the shared claims register.

In-process dispatch: this checks the tool layer does not corrupt or drop a claim on its way
out, not wire framing, which tests/mcp/test_server.py owns.
"""

from __future__ import annotations

from dbprint.mcp import ServedConnections, dispatch
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


def _dict_result(adversarial_print: AdversarialPrint, name: str, arguments: dict) -> dict:
    """Dict-returning `dispatch`; md `get_table_context` is the only string (MCP.md 4.1)."""

    result = dispatch(_state(adversarial_print), name, arguments)
    assert isinstance(result, dict)

    return result


def _statistics(adversarial_print: AdversarialPrint, table: str) -> dict:
    result = _dict_result(
        adversarial_print,
        "get_table_context",
        {"table": table, "format": "json"},
    )

    return result["statistics"]


def _diff(adversarial_print: AdversarialPrint) -> dict:
    return _dict_result(adversarial_print, "get_diff", {})


def _md(adversarial_print: AdversarialPrint, table: str) -> str:
    result = dispatch(
        _state(adversarial_print),
        "get_table_context",
        {"table": table, "format": "md"},
    )

    assert isinstance(result, str)

    return result


def test_scoped_table_carries_the_population(adversarial_print: AdversarialPrint) -> None:
    statistics = _statistics(adversarial_print, SCOPED_TABLE)

    assert statistics["scope"]["rows_scanned"] == 250
    assert "Scanned: 250 of 1,000 rows (25.0%)" in _md(adversarial_print, SCOPED_TABLE)


def test_redacted_column_carries_no_real_literal(adversarial_print: AdversarialPrint) -> None:
    statistics = _statistics(adversarial_print, SCOPED_TABLE)
    email = statistics["columns"][REDACTED_COLUMN]

    assert all(entry["value"] != "a@example.com" for entry in email["values"])
    assert "a@example.com" not in _md(adversarial_print, SCOPED_TABLE)


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
    assert "Scanned: 0 of 500 rows (0.0%)" in _md(adversarial_print, EMPTY_COLUMNS_TABLE)
    assert "no columns" not in _md(adversarial_print, EMPTY_COLUMNS_TABLE).lower()


def test_approximate_row_count_carries_its_own_method(adversarial_print: AdversarialPrint) -> None:
    statistics = _statistics(adversarial_print, APPROXIMATE_ROW_COUNT_TABLE)

    assert statistics["row_count_method"] == "approximate"

    diff = _diff(adversarial_print)
    row_count_changes = [c for c in diff["changes"] if c.get("kind") == "table_row_count_changed"]

    # No delta exists in this fixture to mislabel; the property that would catch a
    # regression is that a delta, whenever one appears, always names both sides' method.
    for change in row_count_changes:
        assert "before_method" in change
        assert "after_method" in change


def test_incomplete_grain_search_carries_exhausted_false(
    adversarial_print: AdversarialPrint,
) -> None:
    statistics = _statistics(adversarial_print, INCOMPLETE_GRAIN_TABLE)

    assert statistics["grain"]["search"]["exhausted"] is False
    assert "Grain: search bounded, none found within the cap" in _md(
        adversarial_print,
        INCOMPLETE_GRAIN_TABLE,
    )


def test_catalog_only_table_carries_no_dependency_or_layout_claim(
    adversarial_print: AdversarialPrint,
) -> None:
    """SPEC 2.2.15 forbids both under the marker; absence is licensed, not a producer gap."""

    statistics = _statistics(adversarial_print, UNEVALUATED_TABLE)

    assert statistics["catalog_only"] is True
    assert "physical_layout" not in statistics
    assert "dependencies" not in statistics
    assert "Clustered by" not in _md(adversarial_print, UNEVALUATED_TABLE)
    assert "Partitioned by" not in _md(adversarial_print, UNEVALUATED_TABLE)


def test_declared_missing_artifact_is_named_not_conflated_with_never_declared(
    adversarial_print: AdversarialPrint,
) -> None:
    result = _dict_result(
        adversarial_print,
        "get_table_context",
        {"table": DECLARED_MISSING_TABLE, "format": "json"},
    )

    assert result.get("_missing") == [DECLARED_MISSING_KIND]
    assert DECLARED_MISSING_KIND not in result.get("_corrupted", {})

    md = _md(adversarial_print, DECLARED_MISSING_TABLE)

    assert f"Missing: {DECLARED_MISSING_KIND}" in md
    assert NEVER_DECLARED_KIND not in md
