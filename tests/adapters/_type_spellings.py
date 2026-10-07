"""Type spellings the declaration sweep reads per engine, and why some stay undeclared."""

from __future__ import annotations


DUCKDB_DECLARATIONS = {
    "ARRAY": "INTEGER[3]",
    "DECIMAL": "DECIMAL(18,3)",
    "ENUM": "ENUM('a', 'b')",
    "LIST": "INTEGER[]",
    "MAP": "MAP(VARCHAR, INTEGER)",
    "STRUCT": "STRUCT(a INTEGER)",
    "UNION": "UNION(n INTEGER, s VARCHAR)",
}


POSTGRES_SKIPPED = {
    "unknown": "a pseudo-type literal, never a column's declared type",
}
