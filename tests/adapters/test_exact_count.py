"""A narrowed read told to count exactly ignores the catalog estimate (SPEC 2.2.1)."""

from __future__ import annotations

from functools import partial
from typing import Any

from dbprint.adapters import statements
from dbprint.adapters.base import TableScope
from dbprint.adapters.duckdb import DIALECT
from dbprint.adapters.duckdb.connection import exec_query


def test_an_exact_count_ignores_the_estimate(duckdb_native_connection: Any) -> None:
    con = duckdb_native_connection
    con.execute("CREATE TABLE seedbank_lot AS SELECT range AS id FROM range(12)")
    count = partial(
        statements.table_row_count,
        partial(exec_query, con),
        DIALECT,
        "seedbank_lot",
        3,
    )

    approximate = count(TableScope(filter="id < 3"), lambda: 5)
    exact = count(TableScope(filter="id < 3", count_exactly=True), lambda: 5)

    assert approximate == (5, "approximate")
    assert exact == (12, "exact")


def test_a_narrowed_read_with_no_estimate_counts_exactly(duckdb_native_connection: Any) -> None:
    con = duckdb_native_connection
    con.execute("CREATE TABLE seedbank_lot AS SELECT range AS id FROM range(12)")

    counted = statements.table_row_count(
        partial(exec_query, con),
        DIALECT,
        "seedbank_lot",
        0,
        TableScope(filter="id < 0"),
        lambda: None,
    )

    assert counted == (12, "exact")
