"""Per-adapter SQL dialect declarations; see GUIDELINES.md's adapters section for the guard.

`VENDOR_SUPPORT` maps each discriminating syntax fragment to the vendors that accept it.
`duckdb` is not an adapter but the Snowflake adapter's test substrate (ARCHITECTURE.md 10),
so the sweep also catches syntax duckdb accepts and Snowflake rejects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Paramstyle = Literal["pyformat", "qmark"]
Vendor = Literal["postgres", "mysql", "snowflake", "duckdb"]

PLACEHOLDERS: dict[Paramstyle, str] = {"pyformat": "%s", "qmark": "?"}


@dataclass(frozen=True)
class Dialect:
    """The SQL dialect one adapter's emitted statements must conform to."""

    vendor: Vendor
    paramstyle: Paramstyle

    @property
    def placeholder(self) -> str:
        """The bind marker this adapter's driver expects."""

        return PLACEHOLDERS[self.paramstyle]


# Fragment -> vendors that accept it; matched case-insensitively against whitespace-collapsed text.
VENDOR_SUPPORT: dict[str, frozenset[Vendor]] = {
    # Catalog surfaces.
    "duckdb_tables(": frozenset({"duckdb"}),
    "duckdb_views(": frozenset({"duckdb"}),
    "duckdb_columns(": frozenset({"duckdb"}),
    "duckdb_indexes(": frozenset({"duckdb"}),
    "duckdb_constraints(": frozenset({"duckdb"}),
    "pg_catalog": frozenset({"postgres"}),
    "pg_class": frozenset({"postgres"}),
    "pg_namespace": frozenset({"postgres"}),
    "pg_attribute": frozenset({"postgres"}),
    "pg_index": frozenset({"postgres"}),
    "pg_stats": frozenset({"postgres"}),
    "reltuples": frozenset({"postgres"}),
    "pg_get_partkeydef(": frozenset({"postgres"}),
    "information_schema.statistics": frozenset({"mysql"}),
    "information_schema.key_column_usage": frozenset({"postgres", "mysql", "duckdb"}),
    "information_schema.index_columns": frozenset({"snowflake"}),
    "information_schema.partitions": frozenset({"mysql"}),
    # Metadata commands.
    "get_ddl(": frozenset({"snowflake"}),
    "show create table": frozenset({"mysql"}),
    "show imported keys": frozenset({"snowflake"}),
    "show tables": frozenset({"snowflake"}),
    # Aggregates and functions.
    "count_if(": frozenset({"snowflake", "duckdb"}),
    "filter (where": frozenset({"postgres", "duckdb"}),
    # Collapsing whitespace never inserts a space, so the no-space form needs its own entry.
    "filter(where": frozenset({"postgres", "duckdb"}),
    "extract(epoch": frozenset({"postgres", "duckdb"}),
    "date_part(": frozenset({"postgres", "snowflake", "duckdb"}),
    "datediff(": frozenset({"mysql", "snowflake", "duckdb"}),
    "timestampdiff(": frozenset({"mysql", "snowflake"}),
    "now()": frozenset({"postgres", "mysql", "duckdb"}),
    # Open paren rather than `rand()`, so a seeded `RAND(<n>)` is covered too.
    "rand(": frozenset({"mysql"}),
    "random()": frozenset({"postgres", "snowflake", "duckdb"}),
    # The hash-ordered distinct draw (SPEC 4.1.2); MD5 is native to all three.
    "md5(": frozenset({"postgres", "mysql", "snowflake", "duckdb"}),
    # MySQL lacks WITHIN GROUP and these functions (mysql/stats.py).
    "within group": frozenset({"postgres", "snowflake", "duckdb"}),
    "percentile_cont(": frozenset({"postgres", "snowflake", "duckdb"}),
    "percentile_disc(": frozenset({"postgres", "snowflake", "duckdb"}),
    # Sampling/seeding spellings differ per vendor and reach only some methods.
    "using sample": frozenset({"duckdb"}),
    "sample row (": frozenset({"snowflake"}),
    "sample system (": frozenset({"snowflake"}),
    "sample block (": frozenset({"snowflake"}),
    "tablesample": frozenset({"postgres", "snowflake", "duckdb"}),
    "repeatable (": frozenset({"postgres", "snowflake", "duckdb"}),
    "seed (": frozenset({"snowflake"}),
    # Quoting and casts.
    "`": frozenset({"mysql"}),
    "::": frozenset({"postgres", "snowflake", "duckdb"}),
}
