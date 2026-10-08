"""Two-phase batched per-table statistics computation for Databricks - Phase B pre-classifies
internally and both MUST converge; `cardinality_method` is always `exact`, nothing being stored.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Any, Literal

from dbprint.config import StatisticsConfig
from dbprint.spec.classification import (
    base_type,
    is_binary_type,
    is_numeric_type,
)
from dbprint.spec.distribution import summarize as summarize_frequencies
from dbprint.spec.rounding import (
    measured_value,
    round_statistic,
)
from dbprint.spec.spatial import Extent, Geometry
from .connection import DIALECT, exec_query
from .introspect import estimate_row_count
from .rendering import (
    render_binary,
    render_domain,
    render_operand,
    render_text,
    temporal_shape,
    utc_instant,
)
from .. import statements
from ..base import (
    ArrayReads,
    BaseStats,
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
    TableCounts,
    TableScope,
    TopN,
    ValueCount,
    ValueList,
    declared_maps,
    dotted_records,
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
from ..sql_layout import call, derived, indented, select_from
from ..statements import column_alias


if TYPE_CHECKING:
    from .connection import Cursor


_UNSUPPORTED_TYPES = (
    "array",
    "map",
    "struct",
    # Spark's legacy calendar interval: no ordering, and no stored column can hold one.
    "interval",
    "void",
)

# Vendor spellings profiled as text by representability (SPEC 3.1), declared so none is guessed.
_TEXT_TYPES = (
    "interval year",
    "interval year to month",
    "interval month",
    "interval day",
    "interval day to hour",
    "interval day to minute",
    "interval day to second",
    "interval hour",
    "interval hour to minute",
    "interval hour to second",
    "interval minute",
    "interval minute to second",
    "interval second",
)

KNOWN_TYPES = (*_UNSUPPORTED_TYPES, *_TEXT_TYPES)


def compute_base(
    cursor: Cursor,
    identity: Identity,
    columns: list[ColumnMeta],
    scope: TableScope | None = None,
) -> tuple[TableCounts, PhaseA]:
    """Phase A: the table's counts plus per-column null_count and cardinality."""

    if not columns:
        return TableCounts(row_count=0, rows_scanned=0), PhaseA({})

    quoted = identity.quoted()
    source = table_source(identity, scope)
    rows_scanned, phase_a = run_phase_a(
        columns,
        phase_a_cost,
        partial(_phase_a_statement, cursor, source),
        partial(_null_counts, cursor, source),
        declines=lambda col: _is_unsupported(col.classified_type),
    )
    row_count, row_count_method = statements.table_row_count(
        partial(exec_query, cursor),
        DIALECT,
        quoted,
        rows_scanned,
        scope,
        lambda: estimate_row_count(cursor, identity),
    )

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

    source = source or table_source(identity, scope)
    reads = ColumnReads(
        value_list=partial(_fetch_value_list, cursor, source),
        numeric_block=partial(_fetch_numeric_block, cursor, source),
        temporal=partial(whole_temporal_block, partial(_fetch_temporal_block, cursor, source)),
        spatial=partial(_fetch_spatial, cursor, source),
        length_p95=partial(_fetch_length_p95, cursor, source),
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
    return _source(identity.quoted(), scope, statements.table_seed(identity))


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
            partial(_phase_a_statement, cursor, source),
            partial(_null_counts, cursor, source),
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


ARRAYS = ArrayReads(
    elements=lambda node, _element: derived(
        select_from([f"EXPLODE({node.operand}) AS v"], node.source),
        SOURCE_ALIAS,
    ),
    size=lambda operand: f"SIZE({operand})",
    distinct=lambda operand: f"COUNT(DISTINCT {operand})",
    norm=lambda operand: f"SQRT(AGGREGATE({operand}, 0D, (acc, e) -> acc + e * e))",
)


RECORDS = dotted_records(DIALECT)


MAPS = declared_maps(
    lambda node, _key, _value: ([f"EXPLODE({node.operand}) AS (k, v)"], node.source),
    size=lambda operand: f"SIZE({operand})",
    backslash_escapes=True,
)


_DOCUMENT_READS = {
    "STRING": "TRY_VARIANT_GET({}, '$', 'string')",
    "TINYINT": "TRY_VARIANT_GET({}, '$', 'bigint')",
    "SMALLINT": "TRY_VARIANT_GET({}, '$', 'bigint')",
    "INT": "TRY_VARIANT_GET({}, '$', 'bigint')",
    "BIGINT": "TRY_VARIANT_GET({}, '$', 'bigint')",
    "DECIMAL": "TRY_VARIANT_GET({}, '$', 'decimal(38, 18)')",
    "FLOAT": "TRY_VARIANT_GET({}, '$', 'double')",
    "DOUBLE": "TRY_VARIANT_GET({}, '$', 'double')",
    "BOOLEAN": "TRY_VARIANT_GET({}, '$', 'boolean')",
    "DATE": "TRY_VARIANT_GET({}, '$', 'date')",
    "TIMESTAMP": "TRY_VARIANT_GET({}, '$', 'timestamp')",
}


def _document_kind(operand: str) -> str:
    return f"REGEXP_EXTRACT(SCHEMA_OF_VARIANT({operand}), '^[A-Z_]+', 0)"


def _exploded(node: PartSource, keyed: bool) -> str:
    items = ["vex.key AS k", "vex.value AS v"] if keyed else ["vex.value AS v"]
    exploded = select_from(items, f"{node.source},\nLATERAL VARIANT_EXPLODE({node.operand}) vex")

    return derived(f"{exploded}\nWHERE\n  vex.key IS {'NOT ' if keyed else ''}NULL", "ent")


DOCUMENTS = DocumentReads(
    type_of=_document_kind,
    null_name="VOID",
    general="VARIANT",
    object_name="OBJECT",
    array_name="ARRAY",
    key_sql_type="STRING",
    names=frozenset({"variant", "object", "array"}),
    numeric=("TINYINT", "SMALLINT", "INT", "BIGINT", "DECIMAL", "FLOAT", "DOUBLE"),
    entries=partial(_exploded, keyed=True),
    elements=partial(_exploded, keyed=False),
    read=lambda value, sql_type: _DOCUMENT_READS.get(sql_type, "{}").format(value),
    size=lambda operand: (
        f"CASE {_document_kind(operand)}"
        f"\n  WHEN 'OBJECT' THEN SIZE(TRY_VARIANT_GET({operand}, '$', 'map<string, variant>'))"
        f"\n  WHEN 'ARRAY' THEN SIZE(TRY_VARIANT_GET({operand}, '$', 'array<variant>'))\nEND"
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
        table_source(identity, scope),
        columns,
        [source_column(col, DIALECT) for col in columns],
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
        table_source(identity, scope),
        counts,
        candidates,
        {
            col.name: render_operand(source_column(col, DIALECT), col.classified_type)
            for col in columns
        },
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
    cn = render_operand(source_column(col, DIALECT), col.classified_type)
    spark_unit = {"day": "DAY", "week": "WEEK", "month": "MONTH"}[unit]

    return statements.timeline(
        partial(exec_query, cursor),
        DIALECT,
        table_source(identity, scope),
        cn,
        call("DATE_TRUNC", f"'{spark_unit}'", utc_instant(cn, col.classified_type)),
        render_domain("bkt.bucket_start", col.classified_type, already_utc=True),
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
        table_source(identity, scope),
        source_column(anchor, DIALECT),
        {subject: source_column(by_name[subject], DIALECT) for subject in subject_columns},
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
        table_source(identity, scope),
        base,
        candidates,
        {
            col.name: render_operand(source_column(col, DIALECT), col.classified_type)
            for col in columns
        },
    )


def materialize(cursor: Cursor, identity: Identity, scope: TableScope) -> TableScope:
    """Copy the drawn fraction into a session-lifetime temp table and name it on the scope -
    where `CREATE TEMPORARY TABLE` is unavailable this raises and the caller degrades safely.
    """

    name = materialized_name(identity.fqn)
    drawn = _sample_expr(identity, scope)
    exec_query(cursor, f"CREATE TEMPORARY TABLE {quote(name, DIALECT)} AS SELECT * FROM {drawn}")

    return replace(scope, materialized=name)


def release(cursor: Cursor, scope: TableScope) -> None:
    """Drop the copied sample; a no-op when materialization was never taken."""

    if scope.materialized is None:
        return

    exec_query(cursor, f"DROP TABLE IF EXISTS {quote(scope.materialized, DIALECT)}")


def _sample_expr(identity: Identity, scope: TableScope) -> str:
    """The `TABLESAMPLE`-bearing source `materialize_scope` reads once to build its copy."""

    quoted = identity.quoted()

    if scope.filter is not None:
        return f"(SELECT * FROM {quoted} WHERE ({scope.filter})) {SOURCE_ALIAS}"

    assert scope.sample is not None  # TableScope guarantees exactly one of filter/sample

    return f"(SELECT * FROM {quoted} TABLESAMPLE ({scope.sample * 100} PERCENT)) {SOURCE_ALIAS}"


def _source(quoted_fqn: str, scope: TableScope | None, seed: int | None = None) -> str:
    """Table reference every statistics query selects FROM - `TABLESAMPLE ... REPEATABLE` is
    coherent on this engine (measured), so an unmaterialized scope still reads stably.
    """

    if scope is None or not scope.narrows:
        return f"{quoted_fqn} {SOURCE_ALIAS}"
    elif scope.materialized is not None:
        return f"{quote(scope.materialized, DIALECT)} {SOURCE_ALIAS}"
    elif scope.sample is not None:
        repeatable = "" if seed is None else f" REPEATABLE ({seed})"

        # Spark's grammar places the alias after the sample clause, not before it.
        return f"{quoted_fqn} TABLESAMPLE ({scope.sample * 100} PERCENT){repeatable} {SOURCE_ALIAS}"
    else:
        return derived(f"SELECT * FROM {quoted_fqn} WHERE ({scope.filter})", SOURCE_ALIAS)


def _null_counts(
    cursor: Cursor,
    source: str,
    columns: list[ColumnMeta],
) -> tuple[int, dict[str, int]]:
    return statements.null_counts(
        partial(exec_query, cursor),
        DIALECT,
        source,
        columns,
        lambda col: render_operand(source_column(col, DIALECT), col.classified_type),
    )


def _phase_a_statement(
    cursor: Cursor,
    source: str,
    columns: list[ColumnMeta],
) -> tuple[int, dict[str, BaseStats]]:
    """One query yielding row_count + per-column null_count + cardinality."""

    select_parts: list[str] = ["COUNT(1) AS row_count"]

    for col in columns:
        cn = render_operand(source_column(col, DIALECT), col.classified_type)
        a = column_alias(col.name)
        select_parts.append(f"COUNT(1) - COUNT({cn}) AS null_{a}")

        if is_numeric_type(col.classified_type):
            select_parts.extend(
                (
                    f"COALESCE(SUM(CASE WHEN {cn} = 0 THEN 1 ELSE 0 END), 0) AS zero_{a}",
                    f"COALESCE(SUM(CASE WHEN {cn} < 0 THEN 1 ELSE 0 END), 0) AS neg_{a}",
                    f"COALESCE(SUM(CASE WHEN {cn} = FLOOR({cn}) THEN 1 ELSE 0 END), 0) AS quant_{a}",
                ),
            )
        elif measures_length(col.classified_type, _is_unsupported):
            empty_condition, length_expr = _length_exprs(cn, col.classified_type)
            select_parts.extend(
                (
                    f"COALESCE(SUM(CASE WHEN {empty_condition} THEN 1 ELSE 0 END), 0) AS empty_{a}",
                    f"MIN({length_expr}) AS lenmin_{a}",
                    f"MAX({length_expr}) AS lenmax_{a}",
                    f"AVG(CAST({length_expr} AS DOUBLE)) AS lenavg_{a}",
                ),
            )

        select_parts.append(f"COUNT(DISTINCT {cn}) AS card_{a}")

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
        length_min = length_max = length_avg = None

        if is_numeric_type(col.classified_type):
            zero_count, negative_count, quantized_count = (
                int(row[idx]),
                int(row[idx + 1]),
                int(row[idx + 2]),
            )
            idx += 3
        elif measures_length(col.classified_type, _is_unsupported):
            empty_count = int(row[idx])
            length_min, length_max, length_avg = row[idx + 1], row[idx + 2], row[idx + 3]
            idx += 4

        cardinality = int(row[idx])
        idx += 1
        out[col.name] = BaseStats(
            null_count=null_count,
            cardinality=cardinality,
            cardinality_method="exact",
            supported=not _is_unsupported(col.classified_type),
            zero_count=zero_count,
            negative_count=negative_count,
            empty_count=empty_count,
            quantized_count=quantized_count,
            length_min=length_min,
            length_max=length_max,
            length_avg=length_avg,
        )

    return row_count, out


def _length_exprs(cn: str, sql_type: str) -> tuple[str, str]:
    if is_binary_type(sql_type):
        return f"OCTET_LENGTH({cn}) = 0", f"OCTET_LENGTH({cn})"

    return f"CAST({cn} AS STRING) = ''", f"LENGTH(CAST({cn} AS STRING))"


def _fetch_value_list(
    cursor: Cursor,
    source: str,
    col: ColumnMeta,
    non_null: int,
    config: StatisticsConfig,
) -> ValueList:
    cn = render_operand(source_column(col, DIALECT), col.classified_type)
    select_expr = cn

    if is_binary_type(col.classified_type):
        select_expr = render_binary(cn)
    elif temporal_shape(col.classified_type) == "timestamp_tz":
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
    )


def _fetch_spatial(
    cursor: Any,
    source: str,
    col: ColumnMeta,
) -> tuple[Geometry, Extent | None]:
    cn = source_column(col, DIALECT)
    # The extent and validity accessors take GEOMETRY only, so a GEOGRAPHY reads through its WKB.
    g = cn if base_type(col.classified_type) == "geometry" else f"ST_GEOMFROMWKB(ST_ASBINARY({cn}))"

    return statements.spatial(
        partial(exec_query, cursor),
        source,
        cn,
        statements.SpatialAccessors(
            kind=f"ST_GEOMETRYTYPE({cn})",
            srid=f"ST_SRID({cn})",
            flag=(
                f"CASE ST_NDIMS({cn})\n"
                f"  WHEN 2 THEN 0\n"
                f"  WHEN 4 THEN 3\n"
                f"  WHEN 3 THEN CASE WHEN ST_ZMAX({g}) IS NULL THEN 1 ELSE 2 END\n"
                f"END"
            ),
            empty=f"ST_ISEMPTY({cn})",
            invalid=f"NOT ST_ISVALID({g})",
            bounds=(f"ST_XMIN({g})", f"ST_YMIN({g})", f"ST_XMAX({g})", f"ST_YMAX({g})"),
        ),
    )


def _fetch_numeric_block(
    cursor: Cursor,
    source: str,
    col: ColumnMeta,
    non_null: int,
    config: StatisticsConfig,
    *,
    values: bool = True,
) -> NumericBlock:
    cn = render_operand(source_column(col, DIALECT), col.classified_type)
    levels = ", ".join(str(p / 100.0) for p in config.percentiles)
    select_parts = [
        f"MIN({cn}) AS mn",
        f"MAX({cn}) AS mx",
        f"AVG(CAST({cn} AS DOUBLE)) AS avg_val",
        f"SUM({cn}) AS sum_val",
        f"PERCENTILE({cn}, ARRAY({levels})) AS pcts",
    ]
    row = exec_query(cursor, select_from(select_parts, source)).fetchone()

    return numeric_block_from_row(
        row,
        row[4] if row and row[4] is not None else (),
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
    cn = render_operand(source_column(col, DIALECT), col.classified_type)
    keys = config.percentiles
    day_aligned = temporal_shape(col.classified_type) != "date"
    # Rendered SQL-side, not via Python's `.isoformat()`: PySpark collects a naive local
    # datetime, so a Python render would carry no 'Z' and read as outside its own range.
    rendered_min = render_domain(f"MIN({cn})", col.classified_type)
    rendered_max = render_domain(f"MAX({cn})", col.classified_type)

    # Integer microseconds, not DOUBLE epochs: a float rounds away the last microseconds of a
    # century-wide span (SPEC 2.2.4).
    max_micros = f"UNIX_MICROS(CAST(MAX({cn}) AS TIMESTAMP))"
    min_micros = f"UNIX_MICROS(CAST(MIN({cn}) AS TIMESTAMP))"

    agg_select = [
        f"{rendered_min} AS mn",
        f"{rendered_max} AS mx",
        f"({max_micros} - {min_micros}) DIV 86400000000 AS span_days",
        # PERCENTILE and PERCENTILE_APPROX move an instant through a double and PERCENTILE_DISC
        # refuses a timestamp (measured), so the nearest rank is picked from the values instead.
        f"ARRAY_SORT(COLLECT_LIST({cn})) AS dbprint_sorted",
    ]

    if day_aligned:
        agg_select.append(
            f"COALESCE(SUM(CASE WHEN {cn} = DATE_TRUNC('DAY', {cn}) THEN 1 ELSE 0 END), 0) AS quant",
        )

    ranked = [
        render_domain(
            f"""
            CASE
              WHEN SIZE(blk.dbprint_sorted) = 0 THEN NULL
              ELSE ELEMENT_AT(
                blk.dbprint_sorted,
                CAST(
                  GREATEST(({p} * CAST(SIZE(blk.dbprint_sorted) AS BIGINT) + 99) DIV 100, 1) AS INT
                )
              )
            END
            """,
            col.classified_type,
        )
        for p in keys
    ]
    outer = [
        "blk.mn",
        "blk.mx",
        "blk.span_days",
        *ranked,
        *(["blk.quant"] if day_aligned else []),
    ]
    aggregated = select_from(agg_select, source)
    row = exec_query(cursor, select_from(outer, derived(aggregated, "blk"))).fetchone()

    if row is None:
        empty_range = Range(min=None, max=None, span_days=0)

        return empty_range, {}, "uniform", (), summarize_frequencies([]), (), None

    span_raw = row[2]
    quantized_count = int(row[3 + len(keys)]) if day_aligned else None

    span_days = int(span_raw) if span_raw is not None else 0
    rng = Range(
        min=measured_value(row[0], "range.min"),
        max=measured_value(row[1], "range.max"),
        span_days=span_days,
    )
    percentiles = {
        f"p{p:02d}": measured_value(value, f"percentiles.p{p:02d}")
        for p, value in zip(keys, row[3 : 3 + len(keys)], strict=True)
    }

    distribution, frequencies, values = _approximate_distribution_via_top_n(
        cursor,
        source,
        render_domain(cn, col.classified_type),
        cn,
        non_null,
        config,
        lambda v: v,
    )
    unrepresentable = unrepresentable_fields(rng, percentiles)

    return rng, percentiles, distribution, unrepresentable, frequencies, values, quantized_count


def _fetch_length_p95(cursor: Cursor, source: str, col: ColumnMeta) -> float | None:
    cn = render_operand(source_column(col, DIALECT), col.classified_type)
    _, length_expr = _length_exprs(cn, col.classified_type)
    row = exec_query(
        cursor,
        f"""
        SELECT
          PERCENTILE({length_expr}, 0.95)
        FROM
          {indented(source, 10)}
        WHERE
          {cn} IS NOT NULL
        """,
    ).fetchone()

    return round_statistic(row[0]) if row is not None else None


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
    return base_type(sql_type) in _UNSUPPORTED_TYPES
