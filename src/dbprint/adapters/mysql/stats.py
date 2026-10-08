"""Two-phase batched statistics for MySQL (ARCHITECTURE.md 2); cardinality is always exact.

Nulls are `COUNT(1) - COUNT(col)`; percentiles rank by `CEIL(p * n)` (percentile_disc semantics).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from functools import partial
from typing import Any, Literal

from dbprint.config import StatisticsConfig
from dbprint.spec.classification import (
    base_type,
    is_binary_type,
    is_boolean_type,
    is_numeric_type,
)
from dbprint.spec.distribution import summarize as summarize_frequencies
from dbprint.spec.rounding import (
    measured_value,
    round_statistic,
)
from dbprint.spec.spatial import Extent, Geometry
from .connection import DIALECT, Cursor, exec_query
from .introspect import table_rows_estimate
from .rendering import render_binary, render_domain, render_text, temporal_shape
from .. import statements
from ..base import (
    BaseStats,
    ColumnMeta,
    ColumnProgress,
    ColumnReads,
    ColumnStats,
    Distribution,
    DocumentReads,
    ExtentNotMeasured,
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
    row_count_or_none,
    run_phase_a,
    unrepresentable_fields,
    whole_temporal_block,
)
from ..errors import QueryFailed
from ..identifiers import SOURCE_ALIAS, Identity, source_column
from ..sql_layout import call, derived, indented, select_from
from ..statements import column_alias


_ER_SP_DOES_NOT_EXIST = 1305
_ER_NOT_IMPLEMENTED_FOR_GEOGRAPHIC_SRS = 3618

_UNSUPPORTED_TYPES: tuple[str, ...] = ()

# Vendor spellings profiled as text by representability (SPEC 3.1), declared so none is guessed.
_TEXT_TYPES: tuple[str, ...] = (
    "enum",
    "set",
    "bit",
)

KNOWN_TYPES = (*_UNSUPPORTED_TYPES, *_TEXT_TYPES)

_RANKED_VALUE = "rnk.dbprint_value"


def compute_base(
    cursor: Cursor,
    identity: Identity,
    columns: list[ColumnMeta],
    scope: TableScope | None = None,
    *,
    exact_count: bool = False,
) -> tuple[TableCounts, PhaseA]:
    """Phase A: the table's counts plus per-column null_count and cardinality.

    `exact_count` counts a narrowed table exactly instead of taking the catalog estimate.
    """

    if not columns:
        return TableCounts(row_count=0, rows_scanned=0), PhaseA({})

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
        identity.quoted(),
        rows_scanned,
        scope,
        lambda: None if exact_count else row_count_or_none(table_rows_estimate(cursor, identity)),
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
    """Phase B: the classification-specific statistics, keyed by column name.

    `source` replaces the table's own, for a part read through its derived rows.
    """

    source = source or table_source(identity, scope)
    reads = ColumnReads(
        value_list=partial(_fetch_value_list, cursor, source),
        numeric_block=partial(_fetch_numeric_block, cursor, source),
        temporal=partial(whole_temporal_block, partial(_fetch_temporal_block, cursor, source)),
        length_p95=partial(_fetch_length_p95, cursor, source),
        spatial=partial(_fetch_spatial, cursor, source),
        vector=partial(_fetch_vector, cursor, source),
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
    """The FROM expression every phase reads: a materialized scope's one copied draw, else a
    sample whose seed re-derives from the table's own name, so every phase builds the same text.
    """

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


_DOCUMENT_READS = {
    "STRING": "JSON_UNQUOTE({})",
    "INTEGER": "CAST({} AS SIGNED)",
    "DECIMAL": "CAST({} AS DECIMAL(65, 30))",
    "DOUBLE": "CAST({} AS DOUBLE)",
    "BOOLEAN": "(JSON_UNQUOTE({}) = 'true')",
}


def _document_entries(node: PartSource) -> str:
    keys = f"JSON_TABLE(JSON_KEYS({node.operand}), '$[*]' COLUMNS (k VARCHAR(512) PATH '$')) jky"
    entries = select_from(
        ["jky.k AS k", f"JSON_EXTRACT({node.operand}, CONCAT('$.', JSON_QUOTE(jky.k))) AS v"],
        f"{node.source},\n{keys}",
    )

    return derived(entries, "ent")


def _document_elements(node: PartSource) -> str:
    held = f"CASE WHEN JSON_TYPE({node.operand}) = 'ARRAY' THEN {node.operand} END"
    elements = f"JSON_TABLE({held}, '$[*]' COLUMNS (v JSON PATH '$')) jel"

    return derived(select_from(["jel.v AS v"], f"{node.source},\n{elements}"), "ent")


DOCUMENTS = DocumentReads(
    type_of=lambda operand: f"JSON_TYPE({operand})",
    null_name="NULL",
    general="JSON",
    object_name="OBJECT",
    array_name="ARRAY",
    key_sql_type="VARCHAR",
    names=frozenset({"json", "object", "array"}),
    numeric=("INTEGER", "DECIMAL", "DOUBLE"),
    entries=_document_entries,
    elements=_document_elements,
    read=lambda value, sql_type: _DOCUMENT_READS.get(sql_type, "{}").format(value),
    size=lambda operand: (
        f"CASE WHEN JSON_TYPE({operand}) IN ('OBJECT', 'ARRAY') THEN JSON_LENGTH({operand}) END"
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
        {col.name: source_column(col, DIALECT) for col in columns},
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
    cn = source_column(col, DIALECT)

    return statements.timeline(
        partial(exec_query, cursor),
        DIALECT,
        table_source(identity, scope),
        cn,
        _timeline_bucket_expr(cn, col.classified_type, unit),
        render_domain("bkt.bucket_start", col.classified_type, already_utc=True),
    )


def _timeline_bucket_expr(cn: str, sql_type: str, unit: str) -> str:
    """Truncation expression for `probe_timeline`'s GROUP BY key (SPEC 2.2.16): MySQL has no
    `date_trunc`, a TIMESTAMP normalizes to UTC first, and the bucket keeps the anchor's domain.
    """

    is_timestamp = temporal_shape(sql_type) == "timestamp_tz"
    is_date_only = temporal_shape(sql_type) == "date"
    normalized = f"CONVERT_TZ({cn}, @@session.time_zone, '+00:00')" if is_timestamp else cn

    if unit == "day":
        truncated = f"CAST({normalized} AS DATE)"
    elif unit == "week":
        truncated = f"DATE_SUB(CAST({normalized} AS DATE), INTERVAL WEEKDAY({normalized}) DAY)"
    else:
        truncated = (
            f"DATE_SUB(CAST({normalized} AS DATE), INTERVAL (DAYOFMONTH({normalized}) - 1) DAY)"
        )

    return truncated if is_date_only else f"CAST({truncated} AS DATETIME)"


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
        {col.name: source_column(col, DIALECT) for col in columns},
    )


def materialize(cursor: Cursor, identity: Identity, scope: TableScope) -> TableScope:
    """Copy the drawn fraction into a session-lifetime temp table and name it on the scope.

    `RAND(seed)` reproduces a row set only within a single reference, so this is the only way
    MySQL reads one draw. A temp table may not be named twice in one statement.
    """

    drawn = _source(identity.quoted(), scope, statements.table_seed(identity))
    # Qualified by the table's own database: a session with none set has nowhere else to put it.
    copy = identity.sibling(materialized_name(identity.fqn))
    exec_query(cursor, f"CREATE TEMPORARY TABLE {copy} AS SELECT * FROM {drawn}")

    return replace(scope, materialized=copy)


def release(cursor: Cursor, scope: TableScope) -> None:
    """Drop the copied sample; `TEMPORARY` is spelled out so no base table can match."""

    if scope.materialized is None:
        return

    exec_query(cursor, f"DROP TEMPORARY TABLE IF EXISTS {scope.materialized}")


def _source(quoted_fqn: str, scope: TableScope | None, seed: int | None = None) -> str:
    """Table reference every statistics query selects FROM. See ARCHITECTURE.md 2.

    A materialized scope is already the drawn rows and reads as a plain name. MySQL has no
    TABLESAMPLE, so the other two scope shapes are a predicate in a wrapper, and the seed
    reproduces one row set only while this expression is named once per statement.
    """

    if scope is None or not scope.narrows:
        return f"{quoted_fqn} {SOURCE_ALIAS}"
    elif scope.materialized is not None:
        return f"{scope.materialized} {SOURCE_ALIAS}"
    elif scope.sample is not None:
        draw = "RAND()" if seed is None else f"RAND({seed})"

        return derived(f"SELECT * FROM {quoted_fqn} WHERE {draw} < {scope.sample}", SOURCE_ALIAS)
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
        lambda col: source_column(col, DIALECT),
    )


def _phase_a_statement(
    cursor: Cursor,
    source: str,
    columns: list[ColumnMeta],
) -> tuple[int, dict[str, BaseStats]]:
    """One query yielding row_count + per-column null_count + cardinality."""

    select_parts: list[str] = ["COUNT(1) AS row_count"]

    for col in columns:
        cn = source_column(col, DIALECT)
        select_parts.append(f"COUNT(1) - COUNT({cn}) AS null_{column_alias(col.name)}")

        if is_numeric_type(col.classified_type) and not is_boolean_type(col.classified_type):
            select_parts.extend(
                (
                    f"COALESCE(SUM({cn} = 0), 0) AS zero_{column_alias(col.name)}",
                    f"COALESCE(SUM({cn} < 0), 0) AS neg_{column_alias(col.name)}",
                    f"COALESCE(SUM({cn} = TRUNCATE({cn}, 0)), 0) AS quant_{column_alias(col.name)}",
                ),
            )
        elif measures_length(col.classified_type, _is_unsupported):
            empty_condition, length_expr = _length_exprs(cn, col.classified_type)
            select_parts.extend(
                (
                    f"COALESCE(SUM({empty_condition}), 0) AS empty_{column_alias(col.name)}",
                    f"MIN({length_expr}) AS lenmin_{column_alias(col.name)}",
                    f"MAX({length_expr}) AS lenmax_{column_alias(col.name)}",
                    f"AVG({length_expr}) AS lenavg_{column_alias(col.name)}",
                ),
            )

        select_parts.append(f"COUNT(DISTINCT {cn}) AS card_{column_alias(col.name)}")

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

        if is_numeric_type(col.classified_type) and not is_boolean_type(col.classified_type):
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
        return f"LENGTH({cn}) = 0", f"LENGTH({cn})"

    return f"CAST({cn} AS CHAR) = ''", f"CHAR_LENGTH(CAST({cn} AS CHAR))"


def _fetch_value_list(
    cursor: Cursor,
    source: str,
    col: ColumnMeta,
    non_null: int,
    config: StatisticsConfig,
) -> ValueList:
    """Ordered value list, its coverage, and whether it enumerates the column.

    A TIMESTAMP routes through the same UTC-pinning renderer as `range`.
    """

    cn = source_column(col, DIALECT)
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


def _fetch_numeric_block(
    cursor: Cursor,
    source: str,
    col: ColumnMeta,
    non_null: int,
    config: StatisticsConfig,
    *,
    values: bool = True,
) -> NumericBlock:
    cn = source_column(col, DIALECT)
    value = _RANKED_VALUE
    select_parts = [
        f"MIN({value}) AS mn",
        f"MAX({value}) AS mx",
        f"AVG({value}) AS avg_val",
        f"SUM({value}) AS sum_val",
        *_percentile_select(config.percentiles),
    ]
    row = exec_query(cursor, select_from(select_parts, _ranked(source, cn))).fetchone()

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
    if temporal_shape(col.classified_type) in ("time", "year"):
        return _fetch_native_temporal_block(cursor, source, col, non_null, config)

    return _fetch_calendar_temporal_block(cursor, source, col, non_null, config)


def _fetch_native_temporal_block(
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
    """TIME and YEAR fetch path: neither risks a driver conversion failure, and neither carries
    a date to truncate to (SPEC 2.2.4), so `quantized_count` is always absent.
    """

    cn = source_column(col, DIALECT)
    keys = config.percentiles
    time_only = temporal_shape(col.classified_type) == "time"
    earliest = _as_date(f"MIN({_RANKED_VALUE})", col.classified_type)
    latest = _as_date(f"MAX({_RANKED_VALUE})", col.classified_type)
    select_parts = [f"MIN({_RANKED_VALUE}) AS mn", f"MAX({_RANKED_VALUE}) AS mx"]

    if not time_only:
        span = f"TIMESTAMPDIFF(MICROSECOND, {earliest}, {latest}) DIV 86400000000"
        select_parts.append(f"{span} AS span_days")

    select_parts += _percentile_select(keys)
    row = exec_query(cursor, select_from(select_parts, _ranked(source, cn))).fetchone()

    if row is None:
        empty_range = Range(min=None, max=None, span_days=0)

        return empty_range, {}, "uniform", (), summarize_frequencies([]), (), None

    # Floored in SQL per SPEC 2.2.4; this only narrows the type.
    span_raw = 0 if time_only else row[2]
    percentile_values = row[2:] if time_only else row[3:]
    span_days = int(span_raw) if span_raw is not None else 0
    rng = Range(
        min=measured_value(row[0], "range.min"),
        max=measured_value(row[1], "range.max"),
        span_days=span_days,
    )
    percentiles = {
        f"p{p:02d}": measured_value(v, f"percentiles.p{p:02d}")
        for p, v in zip(keys, percentile_values, strict=True)
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


def _fetch_calendar_temporal_block(
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
    """DATE / DATETIME / TIMESTAMP: bounds and percentiles render to text in SQL.

    The connector silently fetches the zero-date sentinel `0000-00-00` as NULL, which reads
    as no data; rendering to text keeps it a value.
    """

    cn = source_column(col, DIALECT)
    keys = config.percentiles
    # A DATE value is always its own day-truncation (SPEC 2.2.3): the count would be a
    # truism, so `quantized_count` is omitted entirely rather than published as a constant.
    day_aligned = temporal_shape(col.classified_type) != "date"
    value = _RANKED_VALUE
    agg_select = [f"MIN({value}) AS mn", f"MAX({value}) AS mx"]
    # `/` yields a DECIMAL rounded to scale 4, so an integer microsecond count is divided with
    # `DIV` instead - exact elapsed days (SPEC 2.2.4).
    agg_select.append(
        f"TIMESTAMPDIFF(MICROSECOND, MIN({value}), MAX({value})) DIV 86400000000 AS span_days",
    )

    if day_aligned:
        agg_select.append(f"COALESCE(SUM({value} = CAST({value} AS DATE)), 0) AS quant")

    percentile_renders = [
        (f"p{p:02d}", render_domain(f"agg.p_{p:02d}", col.classified_type)) for p in keys
    ]
    outer_select = [
        f"{render_domain('agg.mn', col.classified_type)} AS mn_text",
        f"{render_domain('agg.mx', col.classified_type)} AS mx_text",
        "agg.span_days",
        *(f"{expr} AS {key}_text" for key, expr in percentile_renders),
        *(["agg.quant"] if day_aligned else []),
    ]

    aggregated = select_from([*agg_select, *_percentile_select(keys)], _ranked(source, cn))
    row = exec_query(cursor, select_from(outer_select, derived(aggregated, "agg"))).fetchone()

    if row is None:
        empty_range = Range(min=None, max=None, span_days=0)

        return empty_range, {}, "uniform", (), summarize_frequencies([]), (), None

    span_raw = row[2]
    n_pct = len(keys)
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

    # Rendered the same way the bounds above already were - a raw fetch of the zero-date
    # sentinel silently reads as NULL, which is what `render_domain` avoids.
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


def _as_date(aggregate: str, sql_type: str) -> str:
    """Wrap an aggregate so date arithmetic has a date to work with.

    Date functions return NULL for a YEAR operand, which NULL-to-0 guards later read as `live`.
    """

    return f"MAKEDATE({aggregate}, 1)" if temporal_shape(sql_type) == "year" else aggregate


def _ranked(source: str, quoted_col: str) -> str:
    """Derived table carrying each non-null value with its rank and the total.

    Names the source once, so every percentile reads the same row set; per-percentile, a
    sampled read would draw an independent set and land a percentile outside its own range.
    The computed columns are prefixed so a bare `n`/`rn` cannot collide with a real column.
    """

    statement = f"""
        SELECT
          {quoted_col} AS dbprint_value,
          ROW_NUMBER() OVER (
            ORDER BY {quoted_col}
          ) AS dbprint_rn,
          COUNT(1) OVER () AS dbprint_n
        FROM
          {indented(source, 10)}
        WHERE
          {quoted_col} IS NOT NULL
        """

    return derived(statement, "rnk")


def _percentile_select(keys: Sequence[int]) -> list[str]:
    """Percentile_disc projections over the ranked derived table - MySQL has no ordered-set
    aggregate, and `CEIL(p * n)` ranks to a value the column holds, from the range's own scan.
    """

    return [
        f"MIN(CASE WHEN rnk.dbprint_rn >= CEIL({p / 100.0} * rnk.dbprint_n) "
        f"THEN {_RANKED_VALUE} END) AS p_{p:02d}"
        for p in keys
    ]


def _fetch_vector(cursor: Cursor, source: str, col: ColumnMeta) -> VectorReading:
    cn = source_column(col, DIALECT)

    # Outside HeatWave no function takes a VECTOR's norm (DISTANCE is HeatWave-only).
    return statements.vector(
        partial(exec_query, cursor),
        source,
        cn,
        dimension=f"VECTOR_DIM({cn})",
        norm=None,
    )


def _fetch_spatial(cursor: Cursor, source: str, col: ColumnMeta) -> tuple[Geometry, Extent | None]:
    cn = source_column(col, DIALECT)
    execute = partial(exec_query, cursor)

    try:
        return _read_spatial(execute, source, cn, validity=True)
    except QueryFailed as exc:
        if getattr(exc.cause, "errno", None) != _ER_SP_DOES_NOT_EXIST:
            raise

    # MariaDB has no ST_IsValid, so it cannot say whether a value is valid.
    return _read_spatial(execute, source, cn, validity=False)


def _read_spatial(
    execute: statements.Execute,
    source: str,
    cn: str,
    *,
    validity: bool,
) -> tuple[Geometry, Extent | None]:
    enveloped = derived(
        select_from([f"{cn} AS dbprint_geo", f"ST_ENVELOPE({cn}) AS dbprint_envelope"], source),
        "spt",
    )
    geo = "spt.dbprint_geo"

    try:
        return statements.spatial(
            execute,
            enveloped,
            geo,
            _spatial_accessors(geo, _envelope_bounds(geo), validity=validity),
        )
    except QueryFailed as exc:
        if getattr(exc.cause, "errno", None) != _ER_NOT_IMPLEMENTED_FOR_GEOGRAPHIC_SRS:
            raise

        geometry, _ = statements.spatial(
            execute,
            source,
            cn,
            _spatial_accessors(cn, None, validity=validity),
        )

        raise ExtentNotMeasured(geometry, str(exc)) from exc


def _spatial_accessors(
    g: str,
    bounds: tuple[str, str, str, str] | None,
    *,
    validity: bool,
) -> statements.SpatialAccessors:
    return statements.SpatialAccessors(
        kind=f"ST_GEOMETRYTYPE({g})",
        srid=f"ST_SRID({g})",
        flag=0,
        empty=f"ST_ISEMPTY({g})",
        invalid=f"NOT ST_ISVALID({g})" if validity else None,
        bounds=bounds,
    )


def _envelope_bounds(g: str) -> tuple[str, str, str, str]:
    # The envelope is the box's own polygon, or a point or axis-parallel segment when degenerate;
    # either way its first and third vertices are opposite corners.
    envelope = "spt.dbprint_envelope"
    corners = [
        f"CASE\n"
        f"  WHEN ST_ISEMPTY({g}) THEN NULL\n"
        f"  WHEN ST_GEOMETRYTYPE({envelope}) = 'POINT' THEN {envelope}\n"
        f"  WHEN ST_GEOMETRYTYPE({envelope}) = 'LINESTRING' THEN {end}({envelope})\n"
        f"  ELSE ST_POINTN(ST_EXTERIORRING({envelope}), {vertex})\n"
        f"END"
        for end, vertex in (("ST_STARTPOINT", 1), ("ST_ENDPOINT", 3))
    ]
    xs = [call("ST_X", corner) for corner in corners]
    ys = [call("ST_Y", corner) for corner in corners]

    return call("LEAST", *xs), call("LEAST", *ys), call("GREATEST", *xs), call("GREATEST", *ys)


def _fetch_length_p95(cursor: Cursor, source: str, col: ColumnMeta) -> float | None:
    """P95 character length (SPEC 2.2.4), ranked the same way numeric percentiles are - but with
    an explicit alias, the shared helpers repeating an expression only a bare column resolves.
    """

    cn = source_column(col, DIALECT)
    _, length_expr = _length_exprs(cn, col.classified_type)
    ranked = f"""
        SELECT
          {length_expr} AS dbprint_len,
          ROW_NUMBER() OVER (
            ORDER BY {length_expr}
          ) AS dbprint_rn,
          COUNT(1) OVER () AS dbprint_n
        FROM
          {indented(source, 10)}
        WHERE
          {cn} IS NOT NULL
        """
    p95 = "MIN(CASE WHEN rnk.dbprint_rn >= CEIL(0.95 * rnk.dbprint_n) THEN rnk.dbprint_len END)"
    row = exec_query(cursor, select_from([p95], derived(ranked, "rnk"))).fetchone()

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
