"""Two-phase batched per-table statistics computation. See ARCHITECTURE.md 2.

Phase B pre-classifies each column to batch its queries by classification group, keeping the
per-table query count small; the engine re-applies SPEC 3.2 independently, and the adapter
NEVER stamps `classification`. Approximate methods activate above `APPROXIMATE_THRESHOLD`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Any, Literal

from dbprint.config import StatisticsConfig
from dbprint.spec.classification import (
    base_type,
    has_day_resolution,
    is_array_type,
    is_binary_type,
    is_numeric_type,
)
from dbprint.spec.distribution import summarize as summarize_frequencies
from dbprint.spec.rounding import (
    measured_value,
)
from dbprint.spec.spatial import Extent, Geometry
from .connection import DIALECT, exec_query
from .introspect import composite_columns, is_hstore, reltuples_estimate, vector_schema
from .rendering import render_binary, render_domain, render_operand, render_text, temporal_shape
from .. import statements
from ..base import (
    ArrayReads,
    BaseStats,
    CardinalityMethod,
    ColumnMeta,
    ColumnProgress,
    ColumnReads,
    ColumnStats,
    Distribution,
    DocumentReads,
    Frequencies,
    MapEntries,
    MapReads,
    Member,
    NullPatterns,
    NumericBlock,
    PartSource,
    PhaseA,
    PhaseB,
    Range,
    RecordReads,
    RowCountMethod,
    TableCounts,
    TableScope,
    TopN,
    ValueCount,
    ValueList,
    VectorReading,
    empty_base_stats,
    is_string_like,
    key_literal,
    materialized_name,
    measure_columns,
    measures_length,
    numeric_block_from_row,
    phase_a_cost,
    profile_over,
    run_phase_a,
    unrepresentable_fields,
    whole_temporal_block,
)
from ..identifiers import SOURCE_ALIAS, Identity, quote, source_column
from ..sql_layout import call, derived, indented, listed, select_from
from ..statements import column_alias


if TYPE_CHECKING:
    import psycopg


APPROXIMATE_THRESHOLD = 1_000_000


_UNSUPPORTED_TYPES = (
    "point",
    "line",
    "lseg",
    "box",
    "path",
    "polygon",
    "circle",
    "xml",
    "aclitem",
    "cid",
    "xid",
    "gtsvector",
    "pg_snapshot",
    "txid_snapshot",
    "refcursor",
    "geometric",
    "hstore",
)

# Vendor spellings profiled as text by representability (SPEC 3.1), declared so none is guessed.
_TEXT_TYPES: tuple[str, ...] = (
    "jsonpath",
    "inet",
    "cidr",
    "macaddr",
    "macaddr8",
    "interval",
    "bit",
    "bit varying",
    "tsvector",
    "tsquery",
    "pg_lsn",
    "oid",
    "name",
    "citext",
    "anyenum",
    "anyrange",
    "anymultirange",
    '"char"',
    "tid",
    "xid8",
    "int2vector",
    "oidvector",
    "regclass",
    "regcollation",
    "regconfig",
    "regdictionary",
    "regnamespace",
    "regoper",
    "regoperator",
    "regproc",
    "regprocedure",
    "regrole",
    "regtype",
)

KNOWN_TYPES = (*_UNSUPPORTED_TYPES, *_TEXT_TYPES)


def compute_base(
    conn: psycopg.Connection,
    identity: Identity,
    columns: list[ColumnMeta],
    scope: TableScope | None = None,
) -> tuple[TableCounts, PhaseA]:
    """Phase A: the table's counts plus per-column null_count and cardinality."""

    if not columns:
        return TableCounts(row_count=0, rows_scanned=0), PhaseA({})

    source = _table_source(identity, scope)
    narrows = scope is not None and scope.narrows

    reltuples = reltuples_estimate(conn, identity)
    # The planner's n_distinct describes the whole table, so a narrowed read counts instead.
    approximate = reltuples > APPROXIMATE_THRESHOLD and not narrows
    composite = composite_columns(conn, identity)

    rows_scanned, phase_a = run_phase_a(
        columns,
        phase_a_cost,
        partial(
            _phase_a_statement,
            conn,
            identity,
            source,
            approximate=approximate,
            composite=composite,
        ),
        partial(_null_counts, conn, source),
        partial(_recount, conn, source) if approximate else None,
        declines=lambda col: _is_unsupported(col.classified_type) or col.name in composite,
    )
    row_count, row_count_method = _table_row_count(
        conn,
        identity.quoted(),
        rows_scanned,
        reltuples,
        scope,
    )

    return TableCounts(row_count, rows_scanned, row_count_method), phase_a


def compute_columns(
    conn: psycopg.Connection,
    identity: Identity,
    columns: list[ColumnMeta],
    config: StatisticsConfig,
    counts: TableCounts,
    base: dict[str, BaseStats],
    fk_source_columns: frozenset[str],
    suppress_values: frozenset[str] = frozenset(),
    on_column: ColumnProgress | None = None,
    scope: TableScope | None = None,
    *,
    source: str | None = None,
) -> PhaseB:
    """Phase B: the classification-specific statistics, keyed by column name."""

    source = source or _table_source(identity, scope)
    reads = ColumnReads(
        value_list=partial(_fetch_value_list, conn, source),
        numeric_block=partial(_fetch_numeric_block, conn, source),
        temporal=partial(whole_temporal_block, partial(_fetch_temporal_block, conn, source)),
        spatial=partial(_fetch_spatial, conn, source),
        vector=partial(_fetch_vector, conn, source),
    )

    return measure_columns(
        columns,
        config,
        counts,
        base,
        fk_source_columns,
        reads,
        suppress_values=suppress_values,
        on_column=on_column,
        scope=scope,
    )


def table_source(identity: Identity, scope: TableScope | None) -> str:
    """The FROM expression a table's statistics read, which a descent derives its parts from."""

    return _table_source(identity, scope)


def profile_part(
    conn: psycopg.Connection,
    identity: Identity,
    source: str,
    column: ColumnMeta,
    config: StatisticsConfig,
    *,
    suppress_values: frozenset[str] = frozenset(),
) -> ColumnStats:
    """A part's statistics over its derived `source` (SPEC 2.2.18), as a column's are read."""

    return profile_over(
        column,
        lambda columns: run_phase_a(
            columns,
            phase_a_cost,
            partial(
                _phase_a_statement,
                conn,
                identity,
                source,
                approximate=False,
                composite=frozenset(),
            ),
            partial(_null_counts, conn, source),
            declines=lambda col: _is_unsupported(col.classified_type),
        ),
        lambda columns, counts, base: compute_columns(
            conn,
            identity,
            columns,
            config,
            counts,
            base,
            frozenset(),
            suppress_values,
            source=source,
        ),
    )


ARRAYS = ArrayReads(
    elements=lambda node, _element: derived(
        select_from([f"UNNEST({node.operand}) AS v"], node.source),
        SOURCE_ALIAS,
    ),
    size=lambda operand: f"CARDINALITY({operand})",
    distinct=lambda operand: f"COUNT(DISTINCT {operand})",
    norm=lambda operand: f"(SELECT SQRT(SUM(elm.e * elm.e)) FROM UNNEST({operand}) AS elm (e))",
)


def root_type(column: ColumnMeta) -> str:
    """The type a descent reads a column as: a composite by its own name, not `record`."""

    return column.sql_type if column.classify_as == "record" else column.classified_type


RECORDS = RecordReads(
    members=lambda conn, node: _composite_members(conn, node.sql_type),
    source=lambda node, member: derived(
        select_from([f"({node.operand}).{quote(member.name, DIALECT)} AS v"], node.source)
        + f"\nWHERE\n  {node.operand} IS DISTINCT FROM NULL",
        SOURCE_ALIAS,
    ),
    # A row whose fields are all NULL is still a row: `IS NULL` would call it absent.
    present=lambda operand: f"{operand} IS DISTINCT FROM NULL",
)


def _hstore_entries(conn: psycopg.Connection, node: PartSource) -> MapEntries | None:
    if not is_hstore(node.sql_type):
        return None

    # `EACH` and `AKEYS` live in the extension's schema, which need not be on the search path.
    [(name,)] = exec_query(
        conn,
        """
        SELECT
          nsp.nspname
        FROM
          pg_type typ
          JOIN pg_namespace nsp ON nsp.oid = typ.typnamespace
        WHERE
          typ.typname = 'hstore'
        """,
    ).fetchall()
    schema = quote(str(name), DIALECT)
    entries = select_from(
        ["kvs.key AS k", "kvs.value AS v"],
        f"{node.source}\nCROSS JOIN LATERAL {schema}.EACH({node.operand}) kvs",
    )

    return MapEntries(
        "text",
        "text",
        derived(entries, "ent"),
        f"CARDINALITY({schema}.AKEYS({node.operand}))",
    )


MAPS = MapReads(entries=_hstore_entries, literal=key_literal)


def _document(operand: str) -> str:
    return f"{operand}::JSONB"


def _document_read(value: str, sql_type: str) -> str:
    text = f"({_document(value)} #>> '{{}}')"

    return {
        "string": text,
        "number": f"{text}::NUMERIC",
        "boolean": f"{text}::BOOLEAN",
    }.get(sql_type, _document(value))


def _documents_of(node: PartSource, kind: str, empty: str, expand: str, items: list[str]) -> str:
    # The set-returning functions raise on a value of another kind, so it reaches them as `empty`.
    document = _document(node.operand)
    held = (
        f"CASE WHEN JSONB_TYPEOF({document}) = '{kind}' THEN {document} ELSE '{empty}'::JSONB END"
    )
    expanded = f"{node.source}\nCROSS JOIN LATERAL {call(expand, held)} kvs"

    return derived(select_from(items, expanded), "ent")


DOCUMENTS = DocumentReads(
    type_of=lambda operand: f"JSONB_TYPEOF({_document(operand)})",
    null_name="null",
    general="jsonb",
    object_name="object",
    array_name="array",
    key_sql_type="text",
    names=frozenset({"json", "jsonb", "object", "array"}),
    numeric=("number",),
    entries=lambda node: _documents_of(
        node,
        "object",
        "{}",
        "JSONB_EACH",
        ["kvs.key AS k", "kvs.value AS v"],
    ),
    elements=lambda node: _documents_of(
        node,
        "array",
        "[]",
        "JSONB_ARRAY_ELEMENTS",
        ["kvs.value AS v"],
    ),
    read=_document_read,
    size=lambda operand: (
        f"CASE JSONB_TYPEOF({_document(operand)})"
        f"\n  WHEN 'object' THEN JSONB_ARRAY_LENGTH("
        f"JSONB_PATH_QUERY_ARRAY({_document(operand)}, '$.keyvalue()'))"
        f"\n  WHEN 'array' THEN JSONB_ARRAY_LENGTH({_document(operand)})\nEND"
    ),
    literal=key_literal,
)


def _composite_members(conn: psycopg.Connection, sql_type: str) -> list[Member]:
    rows = exec_query(
        conn,
        """
        SELECT
          att.attname,
          pg_catalog.FORMAT_TYPE(att.atttypid, att.atttypmod)
        FROM
          pg_type typ
          JOIN pg_type bas ON bas.oid = CASE WHEN typ.typtype = 'd' THEN typ.typbasetype ELSE typ.oid END
          JOIN pg_attribute att ON att.attrelid = bas.typrelid
        WHERE
          typ.oid = TO_REGTYPE(%s)
          AND bas.typtype = 'c'
          AND att.attnum > 0
          AND NOT att.attisdropped
        ORDER BY
          att.attnum
        """,
        (sql_type,),
    ).fetchall()

    return [
        Member(name=str(name), sql_type=str(member_type), position=position)
        for position, (name, member_type) in enumerate(rows, start=1)
    ]


def compute_null_patterns(
    conn: psycopg.Connection,
    identity: Identity,
    columns: list[ColumnMeta],
    config: StatisticsConfig,
    counts: TableCounts,
    base: dict[str, BaseStats],
    scope: TableScope | None = None,
) -> NullPatterns | None:
    """Which columns are null together, in one grouped scan. See SPEC 2.2.10."""

    return statements.null_patterns(
        partial(exec_query, conn),
        DIALECT,
        _table_source(identity, scope),
        columns,
        [_null_tested(col) for col in columns],
        config,
        counts,
        base,
    )


def probe_grain(
    conn: psycopg.Connection,
    identity: Identity,
    columns: list[ColumnMeta],
    counts: TableCounts,
    candidates: tuple[tuple[str, str], ...],
    scope: TableScope | None = None,
) -> tuple[tuple[str, str], ...]:
    """One batched statement testing every candidate pair. See SPEC 2.2.12."""

    return statements.grain_pairs(
        partial(exec_query, conn),
        DIALECT,
        _table_source(identity, scope),
        counts,
        candidates,
        {col.name: _operand(col) for col in columns},
    )


def probe_timeline(
    conn: psycopg.Connection,
    identity: Identity,
    columns: list[ColumnMeta],
    counts: TableCounts,
    column: str,
    unit: Literal["day", "week", "month"],
    scope: TableScope | None = None,
) -> tuple[tuple[str, int], ...]:
    """One grouped statement bucketing `column` at `unit` grain (SPEC 2.2.16)."""

    del counts

    col = {c.name: c for c in columns}[column]
    cn = _operand(col)

    return statements.timeline(
        partial(exec_query, conn),
        DIALECT,
        _table_source(identity, scope),
        cn,
        _timeline_bucket_expr(cn, col.classified_type, unit),
        render_domain("bkt.bucket_start", col.classified_type),
    )


def _timeline_bucket_expr(cn: str, sql_type: str, unit: str) -> str:
    """Truncation expression for `probe_timeline`'s GROUP BY key (SPEC 2.2.16) - `date_trunc`
    has no `date` overload before Postgres 14, so a DATE column casts to timestamp first.
    """

    if temporal_shape(sql_type) == "date":
        return f"DATE_TRUNC('{unit}', {cn}::TIMESTAMP)"

    return f"DATE_TRUNC('{unit}', {cn})"


def compute_populated_windows(
    conn: psycopg.Connection,
    identity: Identity,
    columns: list[ColumnMeta],
    counts: TableCounts,
    anchor_column: str,
    subject_columns: tuple[str, ...],
    scope: TableScope | None = None,
) -> dict[str, tuple[str, str]]:
    """One statement, two conditional aggregates per subject column (SPEC 2.2.4)."""

    del counts

    by_name = {col.name: col for col in columns}
    anchor = by_name[anchor_column]

    return statements.populated_windows(
        partial(exec_query, conn),
        _table_source(identity, scope),
        source_column(anchor, DIALECT),
        {subject: source_column(by_name[subject], DIALECT) for subject in subject_columns},
        lambda expr: render_domain(expr, anchor.classified_type),
    )


def probe_dependencies(
    conn: psycopg.Connection,
    identity: Identity,
    columns: list[ColumnMeta],
    counts: TableCounts,
    base: dict[str, BaseStats],
    candidates: tuple[tuple[str, str], ...],
    scope: TableScope | None = None,
) -> dict[tuple[str, str], float]:
    """One batched statement measuring every candidate pair's joint cardinality. See SPEC 2.2.13."""

    del counts

    return statements.dependency_strengths(
        partial(exec_query, conn),
        DIALECT,
        _table_source(identity, scope),
        base,
        candidates,
        {col.name: _operand(col) for col in columns},
    )


def materialize(conn: psycopg.Connection, identity: Identity, scope: TableScope) -> TableScope:
    """Copy the drawn fraction into a session-lifetime temp table and name it on the scope.

    Autocommit makes the CREATE outlive its own statement. The name stays unqualified: a
    temp table lives in `pg_temp`, and qualifying it addresses a different schema.
    """

    name = materialized_name(identity.fqn)
    drawn = _source(identity.quoted(), scope, statements.table_seed(identity))
    exec_query(
        conn,
        f"CREATE TEMPORARY TABLE {quote(name, DIALECT)} AS SELECT * FROM {drawn}",
    )

    return replace(scope, materialized=name)


def release(conn: psycopg.Connection, scope: TableScope) -> None:
    """Drop the copied sample; the session would drop it anyway, this frees it sooner."""

    if scope.materialized is None:
        return

    exec_query(conn, f"DROP TABLE IF EXISTS {quote(scope.materialized, DIALECT)}")


def _table_source(identity: Identity, scope: TableScope | None) -> str:
    """The FROM expression every phase reads.

    A materialized scope names one copied draw; unmaterialized, the seed re-derives from
    the table's own name, so every phase builds the same text.
    """

    return _source(identity.quoted(), scope, statements.table_seed(identity))


def _source(quoted_fqn: str, scope: TableScope | None, seed: int | None = None) -> str:
    """Table reference every statistics query selects FROM. See ARCHITECTURE.md 2.

    A materialized scope is already the drawn rows and reads as a plain name. TABLESAMPLE binds
    to a base table and needs no wrapper; a predicate gets one. `REPEATABLE` makes every
    statement read the same rows, and BERNOULLI hashes (block, offset, seed), so a smaller rate
    under the same seed yields a subset of the larger draw.
    """

    if scope is None or not scope.narrows:
        return f"{quoted_fqn} {SOURCE_ALIAS}"
    elif scope.materialized is not None:
        return f"{quote(scope.materialized, DIALECT)} {SOURCE_ALIAS}"
    elif scope.sample is not None:
        repeatable = "" if seed is None else f" REPEATABLE ({seed})"
        rate = scope.sample * 100

        return f"{quoted_fqn} {SOURCE_ALIAS} TABLESAMPLE BERNOULLI({rate}){repeatable}"
    else:
        return derived(f"SELECT * FROM {quoted_fqn} WHERE {scope.filter}", SOURCE_ALIAS)


def _table_row_count(
    conn: psycopg.Connection,
    quoted_fqn: str,
    rows_scanned: int,
    reltuples: float,
    scope: TableScope | None,
) -> tuple[int, RowCountMethod]:
    """Rows in the table and how they were obtained, per SPEC 2.2.1.

    A narrowed read takes the planner estimate; a never-analyzed table has none and counts
    exactly, since the scanned figure would report a filter matching nothing as an empty
    table (SPEC 2.2.7). An estimate below the scanned count still stands (SPEC 2.2.8).
    """

    if scope is None or not scope.narrows:
        return rows_scanned, "exact"

    if reltuples >= 0 and not scope.count_exactly:
        return int(reltuples), "approximate"

    row = exec_query(conn, f"SELECT COUNT(1) FROM {quoted_fqn} {SOURCE_ALIAS}").fetchone()

    return (int(row[0]) if row and row[0] is not None else rows_scanned), "exact"


def _null_counts(
    conn: psycopg.Connection,
    source: str,
    columns: list[ColumnMeta],
) -> tuple[int, dict[str, int]]:
    return statements.null_counts(partial(exec_query, conn), DIALECT, source, columns, _operand)


def _recount(
    conn: psycopg.Connection,
    source: str,
    columns: list[ColumnMeta],
) -> Sequence[Any] | None:
    return statements.recount(partial(exec_query, conn), DIALECT, source, columns, _operand)


def _phase_a_statement(
    conn: psycopg.Connection,
    identity: Identity,
    source: str,
    columns: list[ColumnMeta],
    approximate: bool,
    composite: frozenset[str],
) -> tuple[int, dict[str, BaseStats]]:
    """One query yielding row_count + per-column null_count + cardinality."""

    select_parts: list[str] = ["COUNT(1) AS row_count"]

    for col in columns:
        cn = render_operand(source_column(col, DIALECT), col.classified_type)
        select_parts.append(
            f"COUNT(1) FILTER (WHERE {cn} IS NULL) AS null_{column_alias(col.name)}",
        )

        if is_numeric_type(col.classified_type):
            select_parts.append(
                f"COUNT(1) FILTER (WHERE {cn} = 0) AS zero_{column_alias(col.name)}",
            )
            select_parts.append(f"COUNT(1) FILTER (WHERE {cn} < 0) AS neg_{column_alias(col.name)}")
            select_parts.append(
                f"COUNT(1) FILTER (WHERE {cn} = TRUNC({cn})) AS quant_{column_alias(col.name)}",
            )

        # SPEC 2.2.3 conditions `length` on the published `sql_type`, which a domain renames.
        if measures_length(col.sql_type, _is_unsupported):
            empty_condition, length_expr = _length_exprs(cn, col.sql_type)
            select_parts.append(
                f"COUNT(1) FILTER (WHERE {empty_condition}) AS empty_{column_alias(col.name)}",
            )
            select_parts.append(f"MIN({length_expr}) AS lenmin_{column_alias(col.name)}")
            select_parts.append(f"MAX({length_expr}) AS lenmax_{column_alias(col.name)}")
            select_parts.append(f"AVG({length_expr}) AS lenavg_{column_alias(col.name)}")
            select_parts.append(
                f"PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY {length_expr}) "
                f"AS lenp95_{column_alias(col.name)}",
            )

        if not approximate:
            select_parts.append(f"{_distinct_count(col, cn)} AS card_{column_alias(col.name)}")

    row = exec_query(conn, select_from(select_parts, source)).fetchone()

    if row is None:
        return 0, {
            c.name: empty_base_stats(
                supported=not _is_unsupported(c.classified_type) and c.name not in composite,
            )
            for c in columns
        }

    row_count = int(row[0])
    out: dict[str, BaseStats] = {}
    idx = 1

    for col in columns:
        null_count = int(row[idx])
        idx += 1
        zero_count = negative_count = empty_count = quantized_count = None
        length_min = length_max = length_avg = length_p95 = None

        if is_numeric_type(col.classified_type):
            zero_count = int(row[idx])
            idx += 1
            negative_count = int(row[idx])
            idx += 1
            quantized_count = int(row[idx])
            idx += 1

        if measures_length(col.sql_type, _is_unsupported):
            empty_count = int(row[idx])
            idx += 1
            length_min = row[idx]
            idx += 1
            length_max = row[idx]
            idx += 1
            length_avg = row[idx]
            idx += 1
            length_p95 = row[idx]
            idx += 1

        method: CardinalityMethod = "exact"

        if approximate and _is_unsupported(col.classified_type):
            cardinality = 0
        elif approximate:
            estimate = _approximate_cardinality(
                conn,
                identity,
                col.physical_name or col.name,
                row_count,
                null_count,
            )

            if estimate is None:
                cardinality = _exact_cardinality(conn, source, col)
            else:
                cardinality = estimate
                method = "approximate"
        else:
            cardinality = int(row[idx])
            idx += 1

        out[col.name] = BaseStats(
            null_count=null_count,
            cardinality=cardinality,
            cardinality_method=method,
            supported=not _is_unsupported(col.classified_type) and col.name not in composite,
            zero_count=zero_count,
            negative_count=negative_count,
            empty_count=empty_count,
            quantized_count=quantized_count,
            length_min=length_min,
            length_max=length_max,
            length_avg=length_avg,
            length_p95=length_p95,
        )

    return row_count, out


def _exact_cardinality(conn: psycopg.Connection, source: str, col: ColumnMeta) -> int:
    """Count distinct values for one column, when the planner has no estimate."""

    cn = render_operand(source_column(col, DIALECT), col.classified_type)
    row = exec_query(conn, select_from([f"COUNT(DISTINCT {cn})"], source)).fetchone()

    return int(row[0]) if row and row[0] is not None else 0


def _approximate_cardinality(
    conn: psycopg.Connection,
    identity: Identity,
    col_name: str,
    row_count: int,
    null_count: int,
) -> int | None:
    """Distinct-count estimate from the planner, or None when it has none.

    None is the absence of an estimate, not a zero count; the caller then counts exactly.
    """

    row = exec_query(
        conn,
        """
        SELECT
          sts.n_distinct
        FROM
          pg_stats sts
        WHERE
          sts.schemaname = %s
          AND sts.tablename = %s
          AND sts.attname = %s
        """,
        (*identity.addressed, col_name),
    ).fetchone()

    if not row or row[0] is None:
        return None

    n_distinct = float(row[0])
    non_null = row_count - null_count

    # Negative n_distinct is the negated row fraction, nulls included per pg_stats;
    # `cardinality` is non-null-only (SPEC 2.2.2), so clamp to non_null.
    if n_distinct < 0:
        return min(non_null, round(-n_distinct * row_count))

    # `stadistinct` 0 is "unknown", ANALYZE's answer for a type without an equality operator.
    if n_distinct == 0 and non_null > 0:
        return None

    return min(non_null, int(n_distinct))


def _length_exprs(cn: str, sql_type: str) -> tuple[str, str]:
    if is_binary_type(sql_type):
        return f"OCTET_LENGTH({cn}) = 0", f"OCTET_LENGTH({cn})"

    return f"CAST({cn} AS TEXT) = ''", f"LENGTH(CAST({cn} AS TEXT))"


def _fetch_value_list(
    conn: psycopg.Connection,
    source: str,
    col: ColumnMeta,
    non_null: int,
    config: StatisticsConfig,
) -> ValueList:
    """Ordered value list, its coverage, and whether it enumerates the column.

    A tz-bearing timestamp renders through `range`'s UTC-pinning path, never the session zone.
    """

    cn = _operand(col)
    select_expr = cn

    if is_binary_type(col.classified_type):
        select_expr = render_binary(cn)
    elif temporal_shape(col.classified_type) == "timestamp_tz":
        select_expr = render_domain(cn, col.classified_type)
    elif is_string_like(col.classified_type, _is_unsupported):
        select_expr = render_text(cn, col.classified_type)

    return statements.value_list(
        partial(exec_query, conn),
        DIALECT,
        source,
        select_expr,
        non_null,
        config,
        column=cn,
    )


def _fetch_vector(conn: psycopg.Connection, source: str, col: ColumnMeta) -> VectorReading:
    cn = source_column(col, DIALECT)
    kind = base_type(col.classified_type)
    # pgvector's functions live in its own schema, which need not be on the search path.
    schema = vector_schema(col.sql_type)
    norm = f"{schema}VECTOR_NORM" if kind == "vector" else f"{schema}L2_NORM"
    # `VECTOR_DIMS` takes `vector` and `halfvec`; a sparse value spells its dimension after `/`.
    dimension = (
        f"SPLIT_PART({cn}::TEXT, '/', 2)::INTEGER"
        if kind == "sparsevec"
        else f"{schema}VECTOR_DIMS({cn})"
    )

    return statements.vector(
        partial(exec_query, conn),
        source,
        cn,
        dimension=dimension,
        norm=f"{norm}({cn})",
    )


def _fetch_spatial(
    conn: psycopg.Connection,
    source: str,
    col: ColumnMeta,
) -> tuple[Geometry, Extent | None]:
    cn = source_column(col, DIALECT)
    # PostGIS's accessors take `geometry`; a `geography` reads through the cast in its own SRID.
    g = f"{cn}::GEOMETRY"

    return statements.spatial(
        partial(exec_query, conn),
        source,
        cn,
        statements.SpatialAccessors(
            kind=f"GEOMETRYTYPE({g})",
            srid=f"ST_SRID({g})",
            flag=f"ST_ZMFLAG({g})",
            empty=f"ST_ISEMPTY({g})",
            invalid=f"NOT ST_ISVALID({g})",
            bounds=(f"ST_XMIN({g})", f"ST_YMIN({g})", f"ST_XMAX({g})", f"ST_YMAX({g})"),
        ),
    )


def _fetch_numeric_block(
    conn: psycopg.Connection,
    source: str,
    col: ColumnMeta,
    non_null: int,
    config: StatisticsConfig,
    *,
    values: bool = True,
) -> NumericBlock:
    cn = _operand(col)
    pct_select = [
        f"PERCENTILE_CONT({p / 100.0}::DOUBLE PRECISION) WITHIN GROUP (ORDER BY {cn}) AS p_{p:02d}"
        for p in config.percentiles
    ]
    row = exec_query(
        conn,
        f"""
        SELECT
          MIN({cn}) AS mn,
          MAX({cn}) AS mx,
          AVG({cn}) AS avg_val,
          SUM({cn}) AS sum_val,
          {listed(pct_select, 10)}
        FROM
          {indented(source, 10)}
        WHERE
          {cn} IS NOT NULL
        """,
    ).fetchone()

    return numeric_block_from_row(
        row,
        row[4:] if row else (),
        config,
        None
        if not values
        else lambda: _approximate_distribution_via_top_n(
            conn,
            source,
            cn,
            cn,
            non_null,
            config,
            measured_value,
        ),
    )


# UTC bounds a calendar value is clamped to before date arithmetic; inside this window,
# subtracting an infinite timestamp cannot raise `DatetimeFieldOverflow` server-side.
_EPOCH_FLOOR = "0001-01-01T00:00:00Z"
_EPOCH_CEIL = "9999-12-31T23:59:59.999999Z"


def _fetch_temporal_block(
    conn: psycopg.Connection,
    source: str,
    col: ColumnMeta,
    non_null: int,
    config: StatisticsConfig,
) -> tuple[
    Range,
    dict[str, Any],
    Distribution,
    tuple[str, ...],
    Frequencies,
    tuple[ValueCount, ...],
    int | None,
]:
    if temporal_shape(col.classified_type) in ("time", "time_tz"):
        return _fetch_clock_temporal_block(conn, source, col, non_null, config)

    return _fetch_calendar_temporal_block(conn, source, col, non_null, config)


def _fetch_clock_temporal_block(
    conn: psycopg.Connection,
    source: str,
    col: ColumnMeta,
    non_null: int,
    config: StatisticsConfig,
) -> tuple[
    Range,
    dict[str, Any],
    Distribution,
    tuple[str, ...],
    Frequencies,
    tuple[ValueCount, ...],
    int | None,
]:
    """TIME / TIME WITH TIME ZONE fetch path - neither carries a year, an `infinity` sentinel or
    a date to truncate to (SPEC 2.2.4), so span is 0 and `quantized_count` always absent.
    """

    cn = render_operand(source_column(col, DIALECT), col.classified_type)
    percentile_keys = config.percentiles
    # percentile_disc takes any sortable type; percentile_cont only double precision/interval.
    pct_select = [
        f"PERCENTILE_DISC({p / 100.0}::DOUBLE PRECISION) WITHIN GROUP (ORDER BY {cn}) AS p_{p:02d}"
        for p in percentile_keys
    ]
    row = exec_query(
        conn,
        f"""
        SELECT
          MIN({cn}) AS mn,
          MAX({cn}) AS mx,
          {listed(pct_select, 10)}
        FROM
          {indented(source, 10)}
        WHERE
          {cn} IS NOT NULL
        """,
    ).fetchone()

    if row is None:
        empty_range = Range(min=None, max=None, span_days=0)

        return empty_range, {}, "uniform", (), summarize_frequencies([]), (), None

    rng = Range(
        min=measured_value(row[0], "range.min"),
        max=measured_value(row[1], "range.max"),
        span_days=0,
    )
    percentiles = {
        f"p{p:02d}": measured_value(v, f"percentiles.p{p:02d}")
        for p, v in zip(percentile_keys, row[2:], strict=True)
    }
    distribution, frequencies, values = _approximate_distribution_via_top_n(
        conn,
        source,
        cn,
        cn,
        non_null,
        config,
        measured_value,
    )

    return rng, percentiles, distribution, (), frequencies, values, None


def _fetch_calendar_temporal_block(
    conn: psycopg.Connection,
    source: str,
    col: ColumnMeta,
    non_null: int,
    config: StatisticsConfig,
) -> tuple[
    Range,
    dict[str, Any],
    Distribution,
    tuple[str, ...],
    Frequencies,
    tuple[ValueCount, ...],
    int | None,
]:
    """DATE / TIMESTAMP[TZ]: bounds and percentiles render to text in SQL.

    The CTE aggregates real values first, since rendered text does not sort like a temporal
    value; the outer query renders once, so no raw value reaches a fetch.
    """

    cn = render_operand(source_column(col, DIALECT), col.classified_type)
    percentile_keys = config.percentiles
    date_only = temporal_shape(col.classified_type) == "date"
    cast_type = (
        "TIMESTAMPTZ"
        if temporal_shape(col.classified_type) == "timestamp_tz"
        else "DATE"
        if date_only
        else "TIMESTAMP"
    )
    # A DATE value is always its own day-truncation (SPEC 2.2.3): the count would be a truism,
    # so `quantized_count` is omitted - judged, as the validator does, on the published type.
    day_aligned = has_day_resolution(col.sql_type)

    agg_select = (
        [f"MIN({cn}) AS mn", f"MAX({cn}) AS mx"]
        + (
            [f"COUNT(1) FILTER (WHERE {cn} = DATE_TRUNC('day', {cn})) AS quant"]
            if day_aligned
            else []
        )
        + [
            f"PERCENTILE_DISC({p / 100.0}::DOUBLE PRECISION) WITHIN GROUP (ORDER BY {cn}) AS p_{p:02d}"
            for p in percentile_keys
        ]
    )
    percentile_renders = [
        (f"p{p:02d}", render_domain(f"p_{p:02d}", col.classified_type)) for p in percentile_keys
    ]

    lo, hi = f"'{_EPOCH_FLOOR}'::{cast_type}", f"'{_EPOCH_CEIL}'::{cast_type}"
    clamped_mn = f"LEAST(GREATEST(mn, {lo}), {hi})::TIMESTAMPTZ"
    clamped_mx = f"LEAST(GREATEST(mx, {lo}), {hi})::TIMESTAMPTZ"
    # GREATEST/LEAST ignore a NULL argument rather than propagating it, so an empty column
    # would otherwise clamp to a real bound and report a span for data that does not exist.
    span_days = f"""
        CASE
          WHEN mn IS NULL THEN NULL
          ELSE EXTRACT(DAY FROM ({clamped_mx} - {clamped_mn}))
        END
        """

    outer_select = [
        f"{render_domain('mn', col.classified_type)} AS mn_text",
        f"{render_domain('mx', col.classified_type)} AS mx_text",
        *(f"{expr} AS {key}_text" for key, expr in percentile_renders),
        f"{span_days} AS span_days",
        *(["quant"] if day_aligned else []),
    ]

    row = exec_query(
        conn,
        f"""
        WITH
          agg AS (

            SELECT
              {listed(agg_select, 14)}
            FROM
              {indented(source, 14)}
            WHERE
              {cn} IS NOT NULL

          )
        SELECT
          {listed(outer_select, 10)}
        FROM
          agg
        """,
    ).fetchone()

    if row is None:
        empty_range = Range(min=None, max=None, span_days=0)

        return empty_range, {}, "uniform", (), summarize_frequencies([]), (), None

    n_pct = len(percentile_keys)
    pct_texts = row[2 : 2 + n_pct]
    span_raw = row[2 + n_pct]
    quantized_count = int(row[2 + n_pct + 1]) if day_aligned else None

    # Floored in SQL per SPEC 2.2.4; this only narrows the type.
    span_days_val = int(span_raw) if span_raw is not None else 0
    rng = Range(
        min=measured_value(row[0], "range.min"),
        max=measured_value(row[1], "range.max"),
        span_days=span_days_val,
    )
    percentiles = {
        key: measured_value(text, f"percentiles.{key}")
        for (key, _), text in zip(percentile_renders, pct_texts, strict=True)
    }

    # Rendered in SQL like the bounds: a raw fetch can hand psycopg infinity or a year outside
    # 0001-9999, which it refuses to build.
    distribution, frequencies, values = _approximate_distribution_via_top_n(
        conn,
        source,
        render_domain(cn, col.classified_type),
        cn,
        non_null,
        config,
        lambda v: v,
    )
    unrepresentable = unrepresentable_fields(rng, percentiles)

    return rng, percentiles, distribution, unrepresentable, frequencies, values, quantized_count


def _approximate_distribution_via_top_n(
    conn: psycopg.Connection,
    source: str,
    select_expr: str,
    group_expr: str,
    non_null: int,
    config: StatisticsConfig,
    value_transform: Callable[[Any], Any],
) -> TopN:
    return statements.top_n(
        partial(exec_query, conn),
        DIALECT,
        source,
        select_expr,
        non_null,
        config,
        value_transform,
        column=group_expr,
    )


def _distinct_count(col: ColumnMeta, cn: str) -> str:
    # A declined type may have no equality operator at all (`point`, `xml`), so nothing counts it.
    return "0" if _is_unsupported(col.classified_type) else f"COUNT(DISTINCT {cn})"


def _is_unsupported(sql_type: str) -> bool:
    base = base_type(sql_type)

    return base in _UNSUPPORTED_TYPES or is_array_type(sql_type)


def _null_tested(col: ColumnMeta) -> str:
    operand = source_column(col, DIALECT)

    # `ROW(NULL, NULL) IS NULL` holds, yet the null count (`COUNT(col)`) counts that row as a value.
    if col.classify_as == "record":
        return f"NULLIF({operand} IS DISTINCT FROM NULL, FALSE)"

    return operand


def _operand(col: ColumnMeta) -> str:
    return render_operand(source_column(col, DIALECT), col.classified_type)
