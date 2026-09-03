"""Per-adapter SQL dialect declarations; see GUIDELINES.md's adapters section for the guard.

`VENDOR_SUPPORT` maps each discriminating syntax fragment to the vendors that accept it.
`duckdb` is not an adapter but the Snowflake adapter's test substrate (ARCHITECTURE.md 10),
so the sweep also catches syntax duckdb accepts and Snowflake rejects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Paramstyle = Literal["pyformat", "qmark"]
Vendor = Literal[
    "postgres",
    "mysql",
    "snowflake",
    "duckdb",
    "clickhouse",
    "redshift",
    "databricks",
    "bigquery",
]

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
    "pg_class": frozenset({"postgres", "redshift"}),
    "pg_namespace": frozenset({"postgres", "redshift"}),
    "pg_attribute": frozenset({"postgres", "redshift"}),
    "pg_constraint": frozenset({"postgres", "redshift"}),
    "pg_index": frozenset({"postgres"}),
    "pg_stats": frozenset({"postgres"}),
    "reltuples": frozenset({"postgres"}),
    "pg_get_partkeydef(": frozenset({"postgres"}),
    "information_schema.statistics": frozenset({"mysql"}),
    "information_schema.key_column_usage": frozenset(
        {"postgres", "mysql", "duckdb", "databricks", "bigquery"},
    ),
    "information_schema.index_columns": frozenset({"snowflake"}),
    "information_schema.partitions": frozenset({"mysql"}),
    # Redshift's `SVV_*`/`STV_*` system views - no INFORMATION_SCHEMA equivalent for these fields.
    "svv_": frozenset({"redshift"}),
    "stv_": frozenset({"redshift"}),
    # Metadata commands.
    "get_ddl(": frozenset({"snowflake"}),
    "show create table": frozenset({"mysql", "databricks"}),
    "show imported keys": frozenset({"snowflake"}),
    "show tables": frozenset({"snowflake", "databricks"}),
    "show table ": frozenset({"redshift"}),
    "show view ": frozenset({"redshift"}),
    "db_collation(": frozenset({"redshift"}),
    # Databricks' DESCRIBE-based fallback path (no INFORMATION_SCHEMA equivalent outside
    # Unity Catalog); `describe detail` also runs on the Unity Catalog path.
    "describe table": frozenset({"databricks"}),
    "describe detail": frozenset({"databricks"}),
    # Aggregates and functions.
    "count_if(": frozenset({"snowflake", "duckdb"}),
    "filter (where": frozenset({"postgres", "duckdb"}),
    # Collapsing whitespace never inserts a space, so the no-space form needs its own entry.
    "filter(where": frozenset({"postgres", "duckdb"}),
    "extract(epoch": frozenset({"postgres", "duckdb", "redshift"}),
    "date_part(": frozenset({"postgres", "snowflake", "duckdb"}),
    "datediff(": frozenset({"mysql", "snowflake", "duckdb", "clickhouse", "redshift"}),
    "timestampdiff(": frozenset({"mysql", "snowflake"}),
    "now()": frozenset({"postgres", "mysql", "duckdb"}),
    # `COUNT(DISTINCT (a, b))`'s row constructor, which Redshift does not support. The space
    # before the paren is deliberate: postgres/duckdb's composite-key expression renders it so.
    "distinct (": frozenset({"postgres", "duckdb"}),
    # Open paren rather than `rand()`, so a seeded `RAND(<n>)` is covered too; BigQuery's own
    # `RAND()` takes no argument at all, and still matches this substring.
    "rand(": frozenset({"mysql", "databricks", "bigquery"}),
    "random()": frozenset({"postgres", "snowflake", "duckdb", "redshift"}),
    # The hash-ordered distinct draw (SPEC 4.1.2); MD5 is native to all eight - `halfMD5(`
    # and `MD5(` both contain this substring, so one entry covers both ClickHouse spellings.
    "md5(": frozenset(
        {
            "postgres",
            "mysql",
            "snowflake",
            "duckdb",
            "clickhouse",
            "redshift",
            "databricks",
            "bigquery",
        },
    ),
    # Databricks' array-valued percentile, one call per column rather than one per key.
    "percentile(": frozenset({"databricks"}),
    # Databricks' hex-to-decimal conversion for the sketch's low-64-bit recombination.
    "conv(": frozenset({"databricks"}),
    # BigQuery's approximate cardinality and percentile-array functions.
    "approx_count_distinct(": frozenset({"bigquery"}),
    "approx_quantiles(": frozenset({"bigquery"}),
    # BigQuery's array-index syntax, used to pull one percentile out of an APPROX_QUANTILES array.
    "offset(": frozenset({"bigquery"}),
    # BigQuery's per-type calendar formatters, used only inside the key sketch's canonical form.
    "format_timestamp(": frozenset({"bigquery"}),
    "format_date(": frozenset({"bigquery"}),
    "format_datetime(": frozenset({"bigquery"}),
    "format_time(": frozenset({"bigquery"}),
    "date_diff(": frozenset({"bigquery"}),
    # `COUNT(DISTINCT)` takes exactly one expression on BigQuery, so a composite key hashes
    # through `STRUCT(...)` cast to JSON text - the vendor-admitted encoding (measured).
    "to_json_string(": frozenset({"bigquery"}),
    "struct(": frozenset({"bigquery"}),
    # BigQuery's hex-digest rendering for the key sketch, paired with `md5(` above.
    "to_hex(": frozenset({"bigquery"}),
    # MySQL lacks WITHIN GROUP and these functions (mysql/stats.py).
    "within group": frozenset({"postgres", "snowflake", "duckdb", "redshift"}),
    "percentile_cont(": frozenset({"postgres", "snowflake", "duckdb", "redshift"}),
    # Redshift's aggregate PERCENTILE_DISC exists only as `APPROXIMATE PERCENTILE_DISC` - the
    # substring still matches, so this membership covers both spellings.
    "percentile_disc(": frozenset({"postgres", "snowflake", "duckdb", "redshift"}),
    # Sampling/seeding spellings differ per vendor and reach only some methods.
    "using sample": frozenset({"duckdb"}),
    "sample row (": frozenset({"snowflake"}),
    "sample system (": frozenset({"snowflake"}),
    "sample block (": frozenset({"snowflake"}),
    "tablesample": frozenset({"postgres", "snowflake", "duckdb", "databricks", "bigquery"}),
    "repeatable (": frozenset({"postgres", "snowflake", "duckdb", "databricks"}),
    "seed (": frozenset({"snowflake"}),
    # Redshift's `STRTOL`-based sketch - no other adapter here spells this.
    "strtol(": frozenset({"redshift"}),
    # ClickHouse's `system.*` catalog - no INFORMATION_SCHEMA equivalent for these columns.
    "system.tables": frozenset({"clickhouse"}),
    "system.columns": frozenset({"clickhouse"}),
    "system.data_skipping_indices": frozenset({"clickhouse"}),
    # ClickHouse's native aggregate/date vocabulary - no other adapter here spells these.
    "uniqexact(": frozenset({"clickhouse"}),
    "uniqcombined64(": frozenset({"clickhouse"}),
    "quantilesexact(": frozenset({"clickhouse"}),
    "quantileexact(": frozenset({"clickhouse"}),
    "countif(": frozenset({"clickhouse", "bigquery"}),
    "minif(": frozenset({"clickhouse"}),
    "maxif(": frozenset({"clickhouse"}),
    "tostartofday(": frozenset({"clickhouse"}),
    "tostartofweek(": frozenset({"clickhouse"}),
    "tostartofmonth(": frozenset({"clickhouse"}),
    "reinterpretasuint64(": frozenset({"clickhouse"}),
    "lowerutf8(": frozenset({"clickhouse"}),
    "trimboth(": frozenset({"clickhouse"}),
    # Quoting and casts.
    "`": frozenset({"mysql", "clickhouse", "databricks", "bigquery"}),
    "::": frozenset({"postgres", "snowflake", "duckdb", "redshift"}),
}
