"""Mock-adapter tables built over a minimal valid table, plus the reference print's own shapes.

A builder fills every field a test leaves unstated, so a table-shape change lands in one place.
"""

from __future__ import annotations

from typing import Any

from dbprint.adapters import ColumnMeta, ColumnStats, CommentsMeta, Inferred, MockTable, ValueCount


def columns(*spec: tuple[str, str] | tuple[str, str, bool]) -> tuple[ColumnMeta, ...]:
    """One column per `(name, sql_type[, nullable])`, NOT NULL unless stated, ordinals from 1."""

    return tuple(
        ColumnMeta(name=name, sql_type=sql_type, nullable=any(rest), default=None, ordinal=i)
        for i, (name, sql_type, *rest) in enumerate(spec, start=1)
    )


# Spelled as the reference print declares them, or a run diffed against a committed print drifts.
SHAPE_PROBE_COLUMNS = columns(
    ("probe_id", "integer"),
    ("logger_ipv4", "character varying(45)"),
    ("json_text", "text"),
    ("payload_bytes", "bytea", True),
    ("tag_list", "text[]"),
)
VAULT_COLUMNS = columns(
    ("vault_id", "integer"),
    ("shelf_code", "character varying(8)"),
    ("site_name", "character varying(80)"),
    ("target_temperature_c", "numeric(4,1)"),
    ("opens_at", "time without time zone"),
    ("closes_at", "time without time zone"),
)


def mock_table(
    fqn: str,
    table_columns: tuple[ColumnMeta, ...],
    stats: dict[str, ColumnStats],
    *,
    primary_key: tuple[str, ...] = (),
    **overrides: Any,
) -> MockTable:
    """A table at `fqn` with no relationships, indexes, comments or samples unless overridden.

    The DDL is spelled from `table_columns` as `pg_dump` does, plus any `primary_key` constraint.
    """

    schema, name = fqn.split(".")
    body = ",\n".join(
        f"    {c.name} {c.sql_type}{'' if c.nullable else ' NOT NULL'}" for c in table_columns
    )
    ddl = f"CREATE TABLE {fqn} (\n{body}\n);\n"

    if primary_key:
        ddl += (
            f"\nALTER TABLE ONLY {fqn}\n"
            f"    ADD CONSTRAINT {name}_pkey PRIMARY KEY ({', '.join(primary_key)});\n"
        )

    fields: dict[str, Any] = {
        "type": "table",
        "namespace_path": (schema, name),
        "ddl": ddl,
        "columns": list(table_columns),
        "relationships": [],
        "indexes": [],
        "comments": CommentsMeta(table=None, columns={}),
        "stats": stats,
        "samples": {},
    }

    return MockTable(**{**fields, **overrides})


def exact_stats(
    sql_type: str,
    cardinality: int,
    cardinality_ratio: float,
    *,
    nullable: bool = False,
    null_count: int = 0,
    null_rate: float = 0.0,
    **fields: Any,
) -> ColumnStats:
    """A column measured exactly, with no nulls unless stated."""

    return ColumnStats(
        sql_type=sql_type,
        nullable=nullable,
        null_count=null_count,
        null_rate=null_rate,
        cardinality=cardinality,
        cardinality_ratio=cardinality_ratio,
        cardinality_method="exact",
        **fields,
    )


def unmeasured_stats(sql_type: str, *, nullable: bool = False) -> ColumnStats:
    """A column with no nulls whose cardinality was never measured."""

    return ColumnStats(
        sql_type=sql_type,
        nullable=nullable,
        null_count=0,
        null_rate=0.0,
        cardinality=None,
        cardinality_ratio=None,
        cardinality_method=None,
    )


def uuid_id_table(fqn: str) -> MockTable:
    """A ten-row table whose one column is a unique uuid `id`."""

    return mock_table(
        fqn,
        columns(("id", "uuid")),
        {"id": exact_stats("uuid", 10, 1.0, inferred=Inferred(candidate_key=True))},
        ddl=f"CREATE TABLE {fqn} (id uuid PRIMARY KEY);\n",
        samples={"id": [f"00000000-0000-7000-8000-{i:012d}" for i in range(10)]},
        row_count=10,
    )


def quarter_scanned_table(**overrides: Any) -> MockTable:
    """`public.t`: a thousand-row table whose statistics were measured over a quarter of it."""

    fields: dict[str, Any] = {
        "ddl": "CREATE TABLE public.t (bucket integer);\n",
        "row_count": 1000,
        "rows_scanned": 250,
        **overrides,
    }

    return mock_table(
        "public.t",
        columns(("bucket", "integer")),
        {
            "bucket": exact_stats(
                "integer",
                10,
                0.04,
                values=tuple(ValueCount(value=str(i), count=25) for i in range(10)),
                values_coverage=1.0,
                distribution="uniform",
            ),
        },
        **fields,
    )
