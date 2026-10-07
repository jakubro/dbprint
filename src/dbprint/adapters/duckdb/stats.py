"""Two-phase batched per-table statistics computation - Phase A pre-classifies internally to
steer Phase B, the engine re-applies SPEC 3.2 independently, and both MUST converge.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from functools import partial
from typing import Any, Literal

from dbprint.config import StatisticsConfig
from dbprint.spec.classification import (
    base_type,
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
from .rendering import (
    render_binary,
    render_domain,
    render_text,
    stores_below_microsecond,
    temporal_shape,
)
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
    Member,
    NullPatterns,
    NumericBlock,
    PartSource,
    PhaseA,
    PhaseB,
    Range,
    RecordReads,
    TableCounts,
    TableScope,
    TopN,
    ValueCount,
    ValueList,
    counts_approximately,
    declared_maps,
    declared_members,
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
from ..identifiers import SOURCE_ALIAS, Identity, quote, source_column, string_literal
from ..sql_layout import derived, indented, listed, select_from
from ..statements import column_alias


# Threshold above which cardinality is estimated rather than counted. SPEC 2.2.2.

_UNSUPPORTED_TYPES = (
    "record",
    "struct",
    "array",
    "list",
    "map",
    "union",
)

# Vendor spellings profiled as text by representability (SPEC 3.1), declared so none is guessed.
_TEXT_TYPES: tuple[str, ...] = (
    "interval",
    "bit",
    "enum",
)

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

    source = table_source(identity, scope)
    narrows = scope is not None and scope.narrows

    estimate = introspect.row_count_estimate(cursor, identity)
    # The catalog estimate describes the table, not the slice, so a narrowed read counts exactly.
    approximate = counts_approximately(estimate, narrows)

    rows_scanned, phase_a = run_phase_a(
        columns,
        phase_a_cost,
        partial(_phase_a_statement, cursor, source, approximate=approximate),
        partial(_null_counts, cursor, source),
        partial(_recount, cursor, source) if approximate else None,
        declines=lambda col: _is_unsupported(col.classified_type),
    )
    row_count, row_count_method = statements.table_row_count(
        partial(exec_query, cursor),
        DIALECT,
        identity.quoted(),
        rows_scanned,
        scope,
        lambda: row_count_or_none(estimate),
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
        spatial=partial(_fetch_spatial, cursor, source),
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
    """The FROM expression every phase reads - rebuilt per phase, not threaded, the seed being
    re-derived from the table's name so every call produces the same text and the same rows.
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
            partial(_phase_a_statement, cursor, source, approximate=False),
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
        select_from([f"UNNEST({node.operand}) AS v"], node.source),
        SOURCE_ALIAS,
    ),
    size=lambda operand: f"LEN({operand})",
    distinct=lambda operand: f"COUNT(DISTINCT {operand})",
    norm=lambda operand: f"SQRT(LIST_DOT_PRODUCT({operand}, {operand}))",
)


def _member_source(node: PartSource, member: Member) -> str:
    name = string_literal(member.name)
    function = "UNION_EXTRACT" if member.union else "STRUCT_EXTRACT"
    presence = (
        f"UNION_TAG({node.operand}) = {name}" if member.union else f"{node.operand} IS NOT NULL"
    )
    statement = select_from([f"{function}({node.operand}, {name}) AS v"], node.source)

    return derived(f"{statement}\nWHERE\n  {presence}", SOURCE_ALIAS)


RECORDS = RecordReads(
    members=lambda _cursor, node: declared_members(node.sql_type),
    source=_member_source,
)


_DOCUMENT_READS = {
    "VARCHAR": "JSON_EXTRACT_STRING({}, '$')",
    "UBIGINT": "CAST({} AS HUGEINT)",
    "BIGINT": "CAST({} AS HUGEINT)",
    "DOUBLE": "CAST({} AS DOUBLE)",
    "BOOLEAN": "CAST({} AS BOOLEAN)",
}


def _document_entries(node: PartSource) -> str:
    keyed = select_from(
        [f"UNNEST(LIST_DISTINCT(JSON_KEYS({node.operand}))) AS k", f"{node.operand} AS doc"],
        node.source,
    )
    objects = derived(f"{keyed}\nWHERE\n  JSON_TYPE({node.operand}) = 'OBJECT'", "obj")
    entries = select_from(
        ["obj.k AS k", "JSON_EXTRACT(obj.doc, '$.' || TO_JSON(obj.k)) AS v"],
        objects,
    )

    return derived(entries, "ent")


def _document_elements(node: PartSource) -> str:
    elements = select_from([f"UNNEST(JSON_EXTRACT({node.operand}, '$[*]')) AS v"], node.source)

    return derived(f"{elements}\nWHERE\n  JSON_TYPE({node.operand}) = 'ARRAY'", "ent")


DOCUMENTS = DocumentReads(
    type_of=lambda operand: f"JSON_TYPE({operand})",
    null_name="NULL",
    general="JSON",
    object_name="OBJECT",
    array_name="ARRAY",
    key_sql_type="VARCHAR",
    names=frozenset({"json", "object", "array"}),
    numeric=("UBIGINT", "BIGINT", "DOUBLE"),
    entries=_document_entries,
    elements=_document_elements,
    read=lambda value, sql_type: _DOCUMENT_READS.get(sql_type, "{}").format(value),
    size=lambda operand: (
        f"CASE\n  WHEN JSON_TYPE({operand}) = 'OBJECT' THEN LEN(LIST_DISTINCT(JSON_KEYS({operand})))"
        f"\n  WHEN JSON_TYPE({operand}) = 'ARRAY' THEN JSON_ARRAY_LENGTH({operand})\nEND"
    ),
    literal=key_literal,
)


MAPS = declared_maps(
    lambda node, _key, _value: (
        [f"UNNEST(MAP_KEYS({node.operand})) AS k", f"UNNEST(MAP_VALUES({node.operand})) AS v"],
        node.source,
    ),
    size=lambda operand: f"CARDINALITY({operand})",
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
    """Copy the drawn fraction into a session-lifetime table and name it on the scope - duckdb
    allows a temp table only in its own `temp` catalog, so later reads address it bare.
    """

    name = materialized_name(identity.fqn)
    seed = statements.table_seed(identity)
    drawn = _source(identity.quoted(), scope, seed)
    exec_query(cursor, f"CREATE TEMPORARY TABLE {quote(name, DIALECT)} AS SELECT * FROM {drawn}")

    return replace(scope, materialized=name)


def release(cursor: Cursor, scope: TableScope) -> None:
    """Drop the copied sample; the session would drop it anyway, this frees it sooner."""

    if scope.materialized is None:
        return

    exec_query(cursor, f"DROP TABLE IF EXISTS {quote(scope.materialized, DIALECT)}")


def _source(base: str, scope: TableScope | None, seed: int | None = None) -> str:
    """Table reference every statistics query selects FROM - `TABLESAMPLE` binds to the single
    reference before it, so a filter scope wraps in a subquery and a materialized one does not.
    """

    if scope is None or not scope.narrows:
        return f"{base} {SOURCE_ALIAS}"
    elif scope.materialized is not None:
        return f"{quote(scope.materialized, DIALECT)} {SOURCE_ALIAS}"
    elif scope.sample is not None:
        seeded = "" if seed is None else f" REPEATABLE ({seed})"

        return f"{base} {SOURCE_ALIAS} TABLESAMPLE BERNOULLI({scope.sample * 100} PERCENT){seeded}"
    else:
        return derived(f"SELECT * FROM {base} WHERE {scope.filter}", SOURCE_ALIAS)


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


def _recount(
    cursor: Cursor,
    source: str,
    columns: list[ColumnMeta],
) -> Sequence[Any] | None:
    return statements.recount(
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
    approximate: bool,
) -> tuple[int, dict[str, BaseStats]]:
    """One query yielding row_count + per-column null_count + cardinality."""

    method: CardinalityMethod = "approximate" if approximate else "exact"
    select_parts: list[str] = ["COUNT(1) AS row_count"]

    for col in columns:
        cn = source_column(col, DIALECT)
        # COALESCE guards COUNT_IF's NULL-on-empty-table result; an approximate count
        # already returns 0 for an empty group.
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

        # An approximate count errs both ways; clamp to non-null (SPEC 2.2.2 is non-null-only).
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
    """Distinct-count aggregate for one column: an approximate estimate or an exact count."""

    if approximate:
        return f"APPROX_COUNT_DISTINCT({quoted_column})"

    return f"COUNT(DISTINCT {quoted_column})"


def _length_exprs(cn: str, sql_type: str) -> tuple[str, str]:
    if is_binary_type(sql_type):
        return f"OCTET_LENGTH({cn}) = 0", f"OCTET_LENGTH({cn})"

    return f"CAST({cn} AS VARCHAR) = ''", f"LENGTH(CAST({cn} AS VARCHAR))"


def _fetch_value_list(
    cursor: Cursor,
    source: str,
    col: ColumnMeta,
    non_null: int,
    config: StatisticsConfig,
) -> ValueList:
    cn = source_column(col, DIALECT)
    select_expr, group_expr = _value_select_and_group(cn, col.classified_type)

    return statements.value_list(
        partial(exec_query, cursor),
        DIALECT,
        source,
        select_expr,
        non_null,
        config,
        column=cn,
        group=group_expr,
    )


def _fetch_spatial(
    cursor: Cursor,
    source: str,
    col: ColumnMeta,
) -> tuple[Geometry, Extent | None]:
    # The accessors live in the `spatial` extension, which autoload does not reach; never INSTALL.
    try:
        exec_query(cursor, "LOAD spatial")
    except Exception as exc:
        raise RuntimeError(f"the duckdb spatial extension could not be loaded: {exc}") from exc

    cn = source_column(col, DIALECT)

    return statements.spatial(
        partial(exec_query, cursor),
        source,
        cn,
        statements.SpatialAccessors(
            # GROUP BY on the bare GEOMETRY_TYPE enum fails; its VARCHAR spelling groups.
            kind=f"CAST(ST_GEOMETRYTYPE({cn}) AS VARCHAR)",
            srid=f"ST_CRS({cn})",
            flag=f"ST_ZMFLAG({cn})",
            empty=f"ST_ISEMPTY({cn})",
            invalid=f"NOT ST_ISVALID({cn})",
            bounds=(f"ST_XMIN({cn})", f"ST_YMIN({cn})", f"ST_XMAX({cn})", f"ST_YMAX({cn})"),
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
    cn = source_column(col, DIALECT)
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
    exact = _exact_integer if _matches(col.classified_type, _DECIMAL_STRING_TYPES) else _as_is

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
            lambda v: measured_value(exact(v)),
        ),
        exact,
    )


# duckdb hands these back as decimal strings, exact past 2**53.
_DECIMAL_STRING_TYPES = ("bignum",)


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
    if temporal_shape(col.classified_type) in ("time", "time_tz"):
        return _fetch_clock_temporal_block(cursor, source, col, non_null, config)

    return _fetch_calendar_temporal_block(cursor, source, col, non_null, config)


def _fetch_clock_temporal_block(
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
    """Time-of-day fetch path: no year to misrender, no unrepresentable fields, span 0 - and no
    date to truncate to either (SPEC 2.2.4), so `quantized_count` is always absent.
    """

    cn = source_column(col, DIALECT)
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
    select_expr, group_expr = _value_select_and_group(cn, col.classified_type)
    distribution, frequencies, values = _approximate_distribution_via_top_n(
        cursor,
        source,
        select_expr,
        group_expr,
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
    """DATE / TIMESTAMP variants: bounds and percentiles render to text in SQL."""

    cn = source_column(col, DIALECT)
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
        # Second boundaries corrected by the sub-second fractions, then integer division: exact
        # elapsed days with no INT64 overflow at the calendar's ends (SPEC 2.2.4).
        """
        (
          DATEDIFF('second', agg.mn, agg.mx)
          - CASE
            WHEN DATE_PART('microseconds', agg.mx) % 1000000
              < DATE_PART('microseconds', agg.mn) % 1000000 THEN 1
            ELSE 0
          END
        ) // 86400 AS span_days
        """,
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

    # Rendered the same way the bounds above already were, for agreement across the file
    # even though duckdb has no overflow sentinel of its own to guard against.
    rendered = render_domain(cn, col.classified_type)
    distribution, frequencies, values = _approximate_distribution_via_top_n(
        cursor,
        source,
        rendered,
        rendered if stores_below_microsecond(col.classified_type) else cn,
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


def _value_select_and_group(cn: str, sql_type: str) -> tuple[str, str]:
    if is_binary_type(sql_type):
        return render_binary(cn), cn

    if temporal_shape(sql_type) == "timestamp_tz":
        return render_domain(cn, sql_type), cn

    if is_string_like(sql_type, _is_unsupported):
        return render_text(cn, sql_type), cn

    if not stores_below_microsecond(sql_type):
        return cn, cn

    if temporal_shape(sql_type) == "time":
        return f"CAST({cn} AS TIME)", f"CAST({cn} AS TIME)"

    rendered = render_domain(cn, sql_type)

    return rendered, rendered


def _exact_integer(v: Any) -> Any:
    return v if v is None else int(v)


def _as_is(v: Any) -> Any:
    return v


def _matches(sql_type: str, types: tuple[str, ...]) -> bool:
    return base_type(sql_type) in types


def _ranked_percentiles(keys: Sequence[int]) -> list[str]:
    return [
        f"MIN(CASE WHEN rnk.dbprint_rn >= CEIL({p / 100.0} * rnk.dbprint_n) "
        f"THEN {_RANKED_VALUE} END) AS p_{p:02d}"
        for p in keys
    ]
