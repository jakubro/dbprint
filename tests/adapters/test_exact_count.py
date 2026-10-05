"""A narrowed read told to count exactly ignores the catalog estimate (SPEC 2.2.1)."""

from __future__ import annotations

from typing import Any

from dbprint.adapters.base import TableScope
from dbprint.adapters.duckdb import stats


def test_an_exact_count_ignores_the_estimate(duckdb_native_connection: Any) -> None:
    con = duckdb_native_connection
    con.execute("CREATE TABLE seedbank_lot AS SELECT range AS id FROM range(12)")
    narrowed = TableScope(filter="id < 3")

    approximate = stats._table_row_count(con, "seedbank_lot", 3, 5, narrowed)
    exact = stats._table_row_count(
        con,
        "seedbank_lot",
        3,
        5,
        TableScope(filter="id < 3", count_exactly=True),
    )

    assert approximate == (5, "approximate")
    assert exact == (12, "exact")
