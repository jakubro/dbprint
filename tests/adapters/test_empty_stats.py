"""A table read in full and found empty publishes the minimal column shape (SPEC 2.2.7).

Phase A's verdict decides a declined column, so one no type table names still empties unsupported.
"""

from __future__ import annotations

import pytest

from dbprint.adapters import ColumnMeta
from dbprint.adapters.base import BaseStats, PhaseB, TableCounts
from dbprint.adapters.identifiers import Identity
from dbprint.config import StatisticsConfig
from tests.adapters.test_dialect_guard import DIALECTS, STATS_MODULES


def _empty_table(vendor: str, *, supported: bool) -> PhaseB:
    column = ColumnMeta(name="c", sql_type="integer", nullable=True, default=None, ordinal=1)
    base = BaseStats(null_count=0, cardinality=0, cardinality_method="exact", supported=supported)

    return STATS_MODULES[vendor].compute_columns(
        None,
        Identity.of(("arboretum", "seedbank", "vault"), DIALECTS[vendor]),
        [column],
        StatisticsConfig(),
        TableCounts(row_count=0, rows_scanned=0),
        {"c": base},
        frozenset(),
    )


@pytest.mark.parametrize("vendor", sorted(STATS_MODULES))
def test_a_column_phase_a_declined_carries_no_cardinality(vendor: str) -> None:
    stats = _empty_table(vendor, supported=False)["c"]

    assert (stats.cardinality, stats.cardinality_ratio, stats.cardinality_method) == (
        None,
        None,
        None,
    )
    assert (stats.null_count, stats.null_rate) == (0, 0.0)


@pytest.mark.parametrize(
    ("vendor", "method"),
    [
        ("bigquery", "approximate"),
        ("clickhouse", "exact"),
        ("databricks", "exact"),
        ("duckdb", "exact"),
        ("mysql", "exact"),
        ("postgres", "exact"),
        ("redshift", "exact"),
        ("snowflake", "exact"),
    ],
)
def test_a_supported_column_carries_a_zero_cardinality(vendor: str, method: str) -> None:
    """BigQuery counts every column approximately, so even an empty table says so."""

    stats = _empty_table(vendor, supported=True)["c"]

    assert (stats.cardinality, stats.cardinality_ratio, stats.cardinality_method) == (
        0,
        0.0,
        method,
    )
