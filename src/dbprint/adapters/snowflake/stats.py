"""Two-phase batched per-table statistics computation. See ARCHITECTURE.md 2.

Phase A pre-classifies internally to steer Phase B; the engine re-applies SPEC 3.2
independently, and both MUST converge. Snowflake's ordered-set aggregates resolve against
a fixed-point numeric, so PERCENTILE_DISC is unreachable there and this adapter runs
a ranked scan for temporal percentiles. Driver-native scalars are normalized wherever
they enter the artifact: a temporal column at or below the enumeration threshold reaches
the `values` map as a key, and SPEC 2.2.4 restricts those to strings, numbers, booleans.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from functools import partial
from typing import Any, Literal

from dbprint.config import StatisticsConfig
from dbprint.spec.classification import (
    base_type,
    element_type,
    is_array_type,
    is_binary_type,
    is_numeric_type,
)
from dbprint.spec.distribution import summarize as summarize_frequencies
from dbprint.spec.rounding import (
    measured_value,
)
from dbprint.spec.spatial import Extent, Geometry
from . import introspect
from .connection import DIALECT, Cursor, exec_query
from .rendering import render_binary, render_domain, render_text, temporal_shape
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
    NullPatterns,
    NumericBlock,
    PartSource,
    PhaseA,
    PhaseB,
    Range,
    RowCountMethod,
    TableCounts,
    TableScope,
    TopN,
    ValueCount,
    ValueList,
    VectorReading,
    declared_maps,
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
from ..identifiers import SOURCE_ALIAS, Identity
from ..sql_layout import derived, indented, listed, select_from
from ..statements import column_alias


# Threshold above which cardinality is estimated rather than counted. SPEC 2.2.2.
APPROXIMATE_THRESHOLD = 1_000_000

_UNSUPPORTED_TYPES = (
    "record",
    "struct",
    "array",
    "map",
    "file",
    "unknown",
)

# Vendor spellings profiled as text by representability (SPEC 3.1), declared so none is guessed.
_TEXT_TYPES: tuple[str, ...] = ()

KNOWN_TYPES = (*_UNSUPPORTED_TYPES, *_TEXT_TYPES)

_RANKED_VALUE = "rnk.dbprint_value"


def compute_base(
    cursor: Cursor,
    identity: Identity,
    columns: list[ColumnMeta],
    scope: TableScope | None = None,
) -> tuple[TableCounts, PhaseA]:
    """Phase A: the table's counts plus per-column null_count and cardinality."""

    if not columns:
        return TableCounts(row_count=0, rows_scanned=0), PhaseA({})

    source = _table_source(identity, scope)
    narrows = scope is not None and scope.narrows

    estimate = introspect.row_count_estimate(cursor, identity)
    # The catalog estimate describes the table, not the slice, so a narrowed read counts exactly.
    approximate = estimate > APPROXIMATE_THRESHOLD and not narrows

    rows_scanned, phase_a = run_phase_a(
        columns,
        phase_a_cost,
        partial(_phase_a_statement, cursor, identity, source, approximate=approximate),
        partial(_null_counts, cursor, identity, source),
        partial(_recount, cursor, identity, source) if approximate else None,
        declines=lambda col: _is_unsupported(col.classified_type),
    )
    row_count, row_count_method = _table_row_count(cursor, identity, rows_scanned, estimate, scope)

    return TableCounts(row_count, rows_scanned, row_count_method), phase_a


def compute_columns(
    cursor: Cursor,
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
        value_list=partial(_fetch_value_list, cursor, identity, source),
        numeric_block=partial(_fetch_numeric_block, cursor, identity, source),
        temporal=partial(
            whole_temporal_block,
            partial(_fetch_temporal_block, cursor, identity, source),
        ),
        spatial=partial(_fetch_spatial, cursor, identity, source),
        vector=partial(_fetch_vector, cursor, identity, source),
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


# TYPEOF's reading of an element, as the scalar type it is cast to before profiling.
_ELEMENT_KINDS = {
    "VARCHAR": "VARCHAR",
    "BOOLEAN": "BOOLEAN",
    "INTEGER": "NUMBER",
    "DECIMAL": "NUMBER",
    "DOUBLE": "FLOAT",
    "ARRAY": "ARRAY",
}


def table_source(identity: Identity, scope: TableScope | None) -> str:
    """The FROM expression a table's statistics read, which a descent derives its parts from."""

    return _table_source(identity, scope)


def profile_part(
    cursor: Cursor,
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
            partial(_phase_a_statement, cursor, identity, source, approximate=False),
            partial(_null_counts, cursor, identity, source),
            declines=lambda col: _is_unsupported(col.classified_type),
        ),
        lambda columns, counts, base: compute_columns(
            cursor,
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


def element_type_of(cursor: Cursor, node: PartSource) -> str | None:
    """The type an array node's elements read as: the one scalar kind every non-null element has.

    `ARRAY` reports no element type, so its flattened elements are probed; mixed kinds or
    objects read as `VARIANT`, and an array of arrays recurses.
    """

    if base_type(node.sql_type) != "array":
        return element_type(node.sql_type)

    kinds = exec_query(
        cursor,
        f"""
        SELECT DISTINCT
          TYPEOF(flt.value)
        FROM
          {indented(node.source, 10)},
          LATERAL FLATTEN(INPUT => {node.operand}) flt
        WHERE
          NOT IS_NULL_VALUE(flt.value)
        """,
    ).fetchall()
    found = {_ELEMENT_KINDS.get(str(kind), "VARIANT") for (kind,) in kinds}

    return found.pop() if len(found) == 1 else "VARIANT"


ARRAYS = ArrayReads(
    elements=lambda node, element: derived(
        select_from(
            [f"{_flattened_value(element)} AS v"],
            f"{node.source},\nLATERAL FLATTEN(INPUT => {node.operand}) flt",
        ),
        SOURCE_ALIAS,
    ),
    size=lambda operand: f"ARRAY_SIZE({operand})",
    distinct=lambda operand: f"COUNT(DISTINCT {operand})",
    norm=lambda operand: f"SQRT(REDUCE({operand}, 0, (acc, e) -> acc + e::FLOAT * e::FLOAT))",
)


MAPS = declared_maps(
    lambda node, key, value: (
        [f"flt.key::{key} AS k", f"flt.value::{value} AS v"],
        f"{node.source},\nLATERAL FLATTEN(INPUT => {node.operand}) flt",
    ),
    size=lambda operand: f"MAP_SIZE({operand})",
    backslash_escapes=True,
)


_DOCUMENT_READS = {
    "VARCHAR": "{}::VARCHAR",
    "INTEGER": "{}::NUMBER(38, 0)",
    "DECIMAL": "{}::NUMBER(38, 12)",
    "DOUBLE": "{}::DOUBLE",
    "BOOLEAN": "{}::BOOLEAN",
}


# `TYPEOF` and `FLATTEN` refuse a structured type, so every document read goes through `VARIANT`.
DOCUMENTS = DocumentReads(
    type_of=lambda operand: f"TYPEOF({operand}::VARIANT)",
    null_name="NULL_VALUE",
    general="VARIANT",
    object_name="OBJECT",
    array_name="ARRAY",
    key_sql_type="VARCHAR",
    names=frozenset({"variant", "object"}),
    numeric=("INTEGER", "DECIMAL", "DOUBLE"),
    entries=lambda node: derived(
        select_from(
            ["fob.key AS k", "fob.value AS v"],
            f"{node.source},\nLATERAL FLATTEN(INPUT => {node.operand}::VARIANT, MODE => 'OBJECT') fob",
        ),
        "ent",
    ),
    elements=lambda node: derived(
        select_from(
            ["fel.value AS v"],
            f"{node.source},\nLATERAL FLATTEN(INPUT => {node.operand}::VARIANT, MODE => 'ARRAY') fel",
        ),
        "ent",
    ),
    read=lambda value, sql_type: _DOCUMENT_READS.get(sql_type, "{}").format(value),
    size=lambda operand: (
        f"CASE TYPEOF({operand}::VARIANT)"
        f"\n  WHEN 'OBJECT' THEN ARRAY_SIZE(OBJECT_KEYS({operand}::VARIANT))"
        f"\n  WHEN 'ARRAY' THEN ARRAY_SIZE({operand}::VARIANT)\nEND"
    ),
    literal=partial(key_literal, backslash_escapes=True),
)


def compute_null_patterns(
    cursor: Cursor,
    identity: Identity,
    columns: list[ColumnMeta],
    config: StatisticsConfig,
    counts: TableCounts,
    base: dict[str, BaseStats],
    scope: TableScope | None = None,
) -> NullPatterns | None:
    """Which columns are null together, in one grouped scan. See SPEC 2.2.10."""

    return statements.null_patterns(
        partial(exec_query, cursor),
        DIALECT,
        _table_source(identity, scope),
        columns,
        [identity.source_column(col.name) for col in columns],
        config,
        counts,
        base,
    )


def probe_grain(
    cursor: Cursor,
    identity: Identity,
    columns: list[ColumnMeta],
    counts: TableCounts,
    candidates: tuple[tuple[str, str], ...],
    scope: TableScope | None = None,
) -> tuple[tuple[str, str], ...]:
    """One batched statement testing every candidate pair. See SPEC 2.2.12."""

    return statements.grain_pairs(
        partial(exec_query, cursor),
        DIALECT,
        _table_source(identity, scope),
        counts,
        candidates,
        {col.name: identity.source_column(col.name) for col in columns},
    )


def probe_timeline(
    cursor: Cursor,
    identity: Identity,
    columns: list[ColumnMeta],
    counts: TableCounts,
    column: str,
    unit: Literal["day", "week", "month"],
    scope: TableScope | None = None,
) -> tuple[tuple[str, int], ...]:
    """One grouped statement bucketing `column` at `unit` grain. See SPEC 2.2.16."""

    del counts

    col = {c.name: c for c in columns}[column]
    cn = identity.source_column(column)

    return statements.timeline(
        partial(exec_query, cursor),
        DIALECT,
        _table_source(identity, scope),
        cn,
        f"DATE_TRUNC('{unit}', {cn})",
        render_domain("bkt.bucket_start", col.classified_type),
    )


def compute_populated_windows(
    cursor: Cursor,
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
        partial(exec_query, cursor),
        _table_source(identity, scope),
        identity.source_column(anchor.name),
        {subject: identity.source_column(by_name[subject].name) for subject in subject_columns},
        lambda expr: render_domain(expr, anchor.classified_type),
    )


def probe_dependencies(
    cursor: Cursor,
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
        partial(exec_query, cursor),
        DIALECT,
        _table_source(identity, scope),
        base,
        candidates,
        {col.name: identity.source_column(col.name) for col in columns},
    )


def materialize(cursor: Cursor, identity: Identity, scope: TableScope) -> TableScope:
    """Copy the drawn fraction into a session-lifetime table and name it on the scope.

    The only construct giving Snowflake a stable draw: SYSTEM/BLOCK accept a seed, but two
    evaluations of one seeded expression are not documented to read the same rows. The copy
    sits in the table's own schema, so it needs no session default.
    """

    name = materialized_name(identity.fqn)
    drawn = _source(identity, scope, statements.table_seed(identity))
    exec_query(cursor, f"CREATE TEMPORARY TABLE {identity.sibling(name)} AS SELECT * FROM {drawn}")

    return replace(scope, materialized=name)


def release(cursor: Cursor, identity: Identity, scope: TableScope) -> None:
    """Drop the copied sample; the session would drop it anyway, this frees it sooner."""

    if scope.materialized is None:
        return

    exec_query(cursor, f"DROP TABLE IF EXISTS {identity.sibling(scope.materialized)}")


def _table_source(identity: Identity, scope: TableScope | None) -> str:
    """The FROM expression every phase reads.

    Rebuilt per phase, not threaded: the seed is re-derived from the table's own name, so
    every call produces the same text - which on this engine is not the same rows.
    """

    return _source(identity, scope, statements.table_seed(identity))


def _source(identity: Identity, scope: TableScope | None, seed: int | None = None) -> str:
    """Table reference every statistics query selects FROM. See ARCHITECTURE.md 2.

    A materialized scope is already the drawn rows, so it reads as a plain name and no
    sampler runs again. A sampled scope keeps SAMPLE on the base table - Snowflake refuses
    a seed on a subquery - and names SYSTEM/BLOCK, the only methods that accept one; BLOCK
    can bias small tables. Only a filter scope gets the subquery wrapper.
    """

    base = identity.quoted()

    if scope is None or not scope.narrows:
        return f"{base} {SOURCE_ALIAS}"
    elif scope.materialized is not None:
        return f"{identity.sibling(scope.materialized)} {SOURCE_ALIAS}"
    elif scope.sample is not None:
        seeded = "" if seed is None else f" SEED ({seed})"

        return f"{base} {SOURCE_ALIAS} SAMPLE SYSTEM ({scope.sample * 100}){seeded}"
    else:
        return derived(f"SELECT * FROM {base} WHERE {scope.filter}", SOURCE_ALIAS)


def _table_row_count(
    cursor: Cursor,
    identity: Identity,
    rows_scanned: int,
    estimate: int,
    scope: TableScope | None,
) -> tuple[int, RowCountMethod]:
    """Rows in the table and how they were obtained, per SPEC 2.2.1.

    A narrowed read takes the catalog estimate; with none it counts exactly, since the
    scanned figure would report a filter matching nothing as an empty table (SPEC 2.2.7).
    An estimate below the scanned count still stands (SPEC 2.2.8).
    """

    if scope is None or not scope.narrows:
        return rows_scanned, "exact"

    if estimate >= 0 and not scope.count_exactly:
        return estimate, "approximate"

    row = exec_query(cursor, f"SELECT COUNT(1) FROM {identity.quoted()} {SOURCE_ALIAS}").fetchone()

    return (int(row[0]) if row and row[0] is not None else rows_scanned), "exact"


def _null_counts(
    cursor: Cursor,
    identity: Identity,
    source: str,
    columns: list[ColumnMeta],
) -> tuple[int, dict[str, int]]:
    return statements.null_counts(
        partial(exec_query, cursor),
        DIALECT,
        source,
        columns,
        lambda col: identity.source_column(col.name),
    )


def _recount(
    cursor: Cursor,
    identity: Identity,
    source: str,
    columns: list[ColumnMeta],
) -> Sequence[Any] | None:
    return statements.recount(
        partial(exec_query, cursor),
        DIALECT,
        source,
        columns,
        lambda col: identity.source_column(col.name),
    )


def _phase_a_statement(
    cursor: Cursor,
    identity: Identity,
    source: str,
    columns: list[ColumnMeta],
    approximate: bool,
) -> tuple[int, dict[str, BaseStats]]:
    """One query yielding row_count + per-column null_count + cardinality."""

    method: CardinalityMethod = "approximate" if approximate else "exact"
    select_parts: list[str] = ["COUNT(1) AS row_count"]

    for col in columns:
        cn = identity.source_column(col.name)
        # COALESCE guards COUNT_IF's NULL-on-empty-table result; HLL already returns 0.
        select_parts.append(f"COALESCE(COUNT_IF({cn} IS NULL), 0) AS null_{column_alias(col.name)}")

        if is_numeric_type(col.classified_type):
            select_parts.append(f"COALESCE(COUNT_IF({cn} = 0), 0) AS zero_{column_alias(col.name)}")
            select_parts.append(f"COALESCE(COUNT_IF({cn} < 0), 0) AS neg_{column_alias(col.name)}")
            select_parts.append(
                f"COALESCE(COUNT_IF({cn} = TRUNC({cn})), 0) AS quant_{column_alias(col.name)}",
            )
        elif measures_length(col.classified_type, _is_unsupported):
            empty_condition, length_expr = _length_exprs(cn, col.classified_type)
            select_parts.append(
                f"COALESCE(COUNT_IF({empty_condition}), 0) AS empty_{column_alias(col.name)}",
            )
            select_parts.append(f"MIN({length_expr}) AS lenmin_{column_alias(col.name)}")
            select_parts.append(f"MAX({length_expr}) AS lenmax_{column_alias(col.name)}")
            select_parts.append(f"AVG({length_expr}) AS lenavg_{column_alias(col.name)}")
            select_parts.append(
                f"PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY {length_expr}) "
                f"AS lenp95_{column_alias(col.name)}",
            )

        select_parts.append(f"{_distinct_expr(cn, approximate)} AS card_{column_alias(col.name)}")

    row = exec_query(cursor, select_from(select_parts, source)).fetchone()

    if row is None:
        return 0, {
            c.name: empty_base_stats(supported=not _is_unsupported(c.classified_type))
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
        elif measures_length(col.classified_type, _is_unsupported):
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

        # HLL errs both ways; `cardinality` is non-null-only (SPEC 2.2.2), so clamp to non_null.
        cardinality = min(row_count - null_count, int(row[idx]))
        idx += 1
        out[col.name] = BaseStats(
            null_count=null_count,
            cardinality=cardinality,
            cardinality_method=method,
            supported=not _is_unsupported(col.classified_type),
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


def _distinct_expr(quoted_column: str, approximate: bool) -> str:
    """Distinct-count aggregate for one column: HLL estimate or exact count."""

    if approximate:
        return f"APPROX_COUNT_DISTINCT({quoted_column})"

    return f"COUNT(DISTINCT {quoted_column})"


def _length_exprs(cn: str, sql_type: str) -> tuple[str, str]:
    if is_binary_type(sql_type):
        return f"OCTET_LENGTH({cn}) = 0", f"OCTET_LENGTH({cn})"

    return f"TO_VARCHAR({cn}) = ''", f"LENGTH(TO_VARCHAR({cn}))"


def _fetch_value_list(
    cursor: Cursor,
    identity: Identity,
    source: str,
    col: ColumnMeta,
    non_null: int,
    config: StatisticsConfig,
) -> ValueList:
    """Ordered value list, its coverage, and whether it enumerates the column.

    A tz-bearing timestamp renders through `range`'s UTC-pinning path, never the session zone.
    """

    cn = identity.source_column(col.name)
    # Snowflake's catalog omits a timestamp's precision, which defaults to nanoseconds, so every
    # timestamp groups on its microsecond rendering (SPEC 2.2.4).
    select_expr = cn

    if is_binary_type(col.classified_type):
        select_expr = render_binary(cn)
    elif temporal_shape(col.classified_type) in ("timestamp", "timestamp_tz"):
        select_expr = render_domain(cn, col.classified_type)
    elif is_string_like(col.classified_type, _is_unsupported):
        select_expr = render_text(cn, col.classified_type)

    return statements.value_list(
        partial(exec_query, cursor),
        DIALECT,
        source,
        select_expr,
        non_null,
        config,
        column=cn,
        group=select_expr,
    )


def _fetch_vector(
    cursor: Cursor,
    identity: Identity,
    source: str,
    col: ColumnMeta,
) -> VectorReading:
    cn = identity.source_column(col.name)
    # The type fixes the element count; a catalog spelling without it reads it per value.
    declared = re.search(r",\s*(\d+)\s*\)", col.classified_type)

    return statements.vector(
        partial(exec_query, cursor),
        source,
        cn,
        dimension=int(declared.group(1)) if declared else f"ARRAY_SIZE({cn}::ARRAY)",
        norm=f"SQRT(VECTOR_INNER_PRODUCT({cn}, {cn}))",
    )


def _fetch_spatial(
    cursor: Cursor,
    identity: Identity,
    source: str,
    col: ColumnMeta,
) -> tuple[Geometry, Extent | None]:
    cn = identity.source_column(col.name)

    # No geometry-type or is-empty accessor exists: GeoJSON names the kind, a point count of 0
    # is the empty value, and both types are 2D-only.
    return statements.spatial(
        partial(exec_query, cursor),
        source,
        cn,
        statements.SpatialAccessors(
            kind=f"ST_ASGEOJSON({cn}):type::STRING",
            srid=f"ST_SRID({cn})",
            flag=0,
            empty=f"ST_NPOINTS({cn}) = 0",
            invalid=f"NOT ST_ISVALID({cn})",
            bounds=(f"ST_XMIN({cn})", f"ST_YMIN({cn})", f"ST_XMAX({cn})", f"ST_YMAX({cn})"),
        ),
    )


def _fetch_numeric_block(
    cursor: Cursor,
    identity: Identity,
    source: str,
    col: ColumnMeta,
    non_null: int,
    config: StatisticsConfig,
    *,
    values: bool = True,
) -> NumericBlock:
    cn = identity.source_column(col.name)
    # PERCENTILE_CONT multiplies against the ordering column - NUMBER(20,6) yields FIXED(23,9),
    # overflowing at the range top - hence the DOUBLE cast; duckdb reads FLOAT as 32-bit.
    pct_select = [
        f"PERCENTILE_CONT({p / 100.0}) WITHIN GROUP (ORDER BY CAST({cn} AS DOUBLE)) AS p_{p:02d}"
        for p in config.percentiles
    ]
    row = exec_query(
        cursor,
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
            cursor,
            source,
            cn,
            cn,
            non_null,
            config,
            measured_value,
        ),
    )


def _fetch_temporal_block(
    cursor: Cursor,
    identity: Identity,
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
        return _fetch_clock_temporal_block(cursor, identity, source, col, non_null, config)

    return _fetch_calendar_temporal_block(cursor, identity, source, col, non_null, config)


def _fetch_clock_temporal_block(
    cursor: Cursor,
    identity: Identity,
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
    """Time-of-day fetch path: no year to misrender, no unrepresentable fields, span 0 - and no
    date to truncate to either (SPEC 2.2.4), so `quantized_count` is always absent.
    """

    cn = identity.source_column(col.name)
    percentile_keys = config.percentiles
    pct_select = _ranked_percentiles(percentile_keys)
    row = exec_query(
        cursor,
        f"""
        SELECT
          MIN({_RANKED_VALUE}) AS mn,
          MAX({_RANKED_VALUE}) AS mx,
          {listed(pct_select, 10)}
        FROM
          (
            SELECT
              {cn} AS dbprint_value,
              ROW_NUMBER() OVER (
                ORDER BY
                  {cn}
              ) AS dbprint_rn,
              COUNT(1) OVER () AS dbprint_n
            FROM
              {indented(source, 14)}
            WHERE
              {cn} IS NOT NULL
          ) rnk
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
        cursor,
        source,
        cn,
        cn,
        non_null,
        config,
        measured_value,
    )

    return rng, percentiles, distribution, (), frequencies, values, None


def _ranked_percentiles(keys: Sequence[int]) -> list[str]:
    # `CEIL(p * n)` is the PERCENTILE_DISC rank; the computed columns are prefixed so a bare
    # `n`/`rn` cannot collide with a real one.
    return [
        f"MIN(CASE WHEN rnk.dbprint_rn >= CEIL({p / 100.0} * rnk.dbprint_n) "
        f"THEN {_RANKED_VALUE} END) AS p_{p:02d}"
        for p in keys
    ]


def _span_days_sql(timestamp: bool) -> str:
    """Whole elapsed days between `mn` and `mx`, exact at microsecond resolution (SPEC 2.2.4).

    `DATEDIFF` counts unit boundaries and `/` rounds to scale 6; both are corrected for here.
    """

    if not timestamp:
        return "DATEDIFF('day', agg.mn, agg.mx)"

    micros = {
        side: f"(DATE_PART('nanosecond', {side}) - MOD(DATE_PART('nanosecond', {side}), 1000))"
        for side in ("agg.mn", "agg.mx")
    }
    seconds = f"""
        (
          DATEDIFF('second', agg.mn, agg.mx)
          - CASE
            WHEN {micros["agg.mx"]} < {micros["agg.mn"]} THEN 1
            ELSE 0
          END
        )
        """

    return f"""
        (
          (
            {indented(seconds, 12)}
            - MOD(
              {indented(seconds, 14)},
              86400
            )
          )
          / 86400
        )
        """


def _fetch_calendar_temporal_block(
    cursor: Cursor,
    identity: Identity,
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
    """DATE / TIMESTAMP variants: bounds and percentiles render to text in SQL."""

    cn = identity.source_column(col.name)
    percentile_keys = config.percentiles
    # A DATE value is always its own day-truncation (SPEC 2.2.3): the count would be a
    # truism, so `quantized_count` is omitted entirely rather than published as a constant.
    day_aligned = temporal_shape(col.classified_type) != "date"

    value = _RANKED_VALUE
    agg_select = [
        f"MIN({value}) AS mn",
        f"MAX({value}) AS mx",
        *_ranked_percentiles(percentile_keys),
    ]

    if day_aligned:
        agg_select.append(f"COUNT_IF({value} = DATE_TRUNC('day', {value})) AS quant")

    percentile_renders = [
        (f"p{p:02d}", render_domain(f"agg.p_{p:02d}", col.classified_type)) for p in percentile_keys
    ]

    outer_select = [
        f"{render_domain('agg.mn', col.classified_type)} AS mn_text",
        f"{render_domain('agg.mx', col.classified_type)} AS mx_text",
        f"{_span_days_sql(day_aligned)} AS span_days",
        *(f"{expr} AS {key}_text" for key, expr in percentile_renders),
        *(["agg.quant"] if day_aligned else []),
    ]

    row = exec_query(
        cursor,
        f"""
        SELECT
          {listed(outer_select, 10)}
        FROM
          (
            SELECT
              {listed(agg_select, 14)}
            FROM
              (
                SELECT
                  {cn} AS dbprint_value,
                  ROW_NUMBER() OVER (
                    ORDER BY
                      {cn}
                  ) AS dbprint_rn,
                  COUNT(1) OVER () AS dbprint_n
                FROM
                  {indented(source, 18)}
                WHERE
                  {cn} IS NOT NULL
              ) rnk
          ) agg
        """,
    ).fetchone()

    if row is None:
        empty_range = Range(min=None, max=None, span_days=0)

        return empty_range, {}, "uniform", (), summarize_frequencies([]), (), None

    span_raw = row[2]
    n_pct = len(percentile_keys)
    percentile_texts = row[3 : 3 + n_pct]
    quantized_count = int(row[3 + n_pct]) if day_aligned else None

    # Floored in SQL per SPEC 2.2.4; this only narrows the type.
    span_days = int(span_raw) if span_raw is not None else 0
    rng = Range(
        min=measured_value(row[0], "range.min"),
        max=measured_value(row[1], "range.max"),
        span_days=span_days,
    )
    percentiles = {
        key: measured_value(text, f"percentiles.{key}")
        for (key, _), text in zip(percentile_renders, percentile_texts, strict=True)
    }

    # Grouped on the rendering, as `_fetch_value_list` is: the precision may exceed it.
    rendered = render_domain(cn, col.classified_type)
    distribution, frequencies, values = _approximate_distribution_via_top_n(
        cursor,
        source,
        rendered,
        rendered,
        non_null,
        config,
        lambda v: v,
    )
    unrepresentable = unrepresentable_fields(rng, percentiles)

    return rng, percentiles, distribution, unrepresentable, frequencies, values, quantized_count


def _approximate_distribution_via_top_n(
    cursor: Cursor,
    source: str,
    select_expr: str,
    group_expr: str,
    non_null: int,
    config: StatisticsConfig,
    value_transform: Callable[[Any], Any],
) -> TopN:
    return statements.top_n(
        partial(exec_query, cursor),
        DIALECT,
        source,
        select_expr,
        non_null,
        config,
        value_transform,
        column=group_expr,
    )


def _is_unsupported(sql_type: str) -> bool:
    base = base_type(sql_type)

    return base in _UNSUPPORTED_TYPES or is_array_type(sql_type)


def _flattened_value(element: str) -> str:
    if element in ("VARIANT", "ARRAY"):
        return "flt.value"

    return f"flt.value::{element}"
