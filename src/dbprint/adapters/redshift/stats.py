"""Two-phase batched per-table statistics computation for Redshift - Phase B pre-classifies
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
from dbprint.spec.rounding import (
    measured_value,
    round_statistic,
)
from dbprint.spec.spatial import Extent, Geometry
from .connection import DIALECT, exec_query
from .introspect import table_rows_estimate
from .rendering import render_binary, render_domain, render_text, temporal_shape
from .. import statements
from ..base import (
    BaseStats,
    ColumnMeta,
    ColumnProgress,
    ColumnReads,
    ColumnStats,
    DocumentReads,
    NullPatterns,
    NumericBlock,
    PhaseA,
    PhaseB,
    Range,
    TableCounts,
    TableScope,
    TopN,
    ValueList,
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
    temporal_with_top_n,
)
from ..identifiers import SOURCE_ALIAS, Identity, quote, source_column
from ..sql_layout import derived, indented, select_from
from ..statements import column_alias


if TYPE_CHECKING:
    from .connection import Cursor


_UNSUPPORTED_TYPES = ("hllsketch",)

# Vendor spellings profiled as text by representability (SPEC 3.1), declared so none is guessed.
_TEXT_TYPES: tuple[str, ...] = ("bpchar",)

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
        lambda: row_count_or_none(table_rows_estimate(cursor, identity)),
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
        temporal=partial(_temporal, cursor, source),
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
    """The FROM expression every phase reads, addressing the catalog's own spelling."""

    return _source(identity.quoted(), scope)


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
    "string": "{}::VARCHAR",
    "number": "{}::FLOAT8",
    "boolean": "{}::BOOLEAN",
}


DOCUMENTS = DocumentReads(
    type_of=lambda operand: f"JSON_TYPEOF({operand})",
    null_name="null",
    general="SUPER",
    object_name="object",
    array_name="array",
    key_sql_type="VARCHAR",
    names=frozenset({"super", "object", "array"}),
    numeric=("number",),
    entries=lambda node: derived(
        select_from(
            ["unp_k AS k", "unp_v AS v"],
            f"{node.source},\nUNPIVOT {node.operand} AS unp_v AT unp_k",
        ),
        "ent",
    ),
    elements=lambda node: derived(
        select_from(["unp_e AS v"], f"{node.source},\n{node.operand} AS unp_e"),
        "ent",
    ),
    read=lambda value, sql_type: _DOCUMENT_READS.get(sql_type, "{}").format(value),
    size=lambda operand: (
        f"CASE JSON_TYPEOF({operand})"
        f"\n  WHEN 'object' THEN GET_NUMBER_ATTRIBUTES({operand})"
        f"\n  WHEN 'array' THEN GET_ARRAY_LENGTH({operand})\nEND"
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
    """One grouped statement bucketing `column` at `unit` grain (SPEC 2.2.16) - the render alone
    applies the TZ conversion.
    """

    del counts

    col = {c.name: c for c in columns}[column]
    cn = source_column(col, DIALECT)

    return statements.timeline(
        partial(exec_query, cursor),
        DIALECT,
        table_source(identity, scope),
        cn,
        _timeline_bucket_expr(cn, col.classified_type, unit),
        render_domain("bkt.bucket_start", col.classified_type),
    )


def _timeline_bucket_expr(cn: str, sql_type: str, unit: str) -> str:
    """Truncation expression for `probe_timeline`'s GROUP BY key (SPEC 2.2.16) - `DATE_TRUNC` is
    documented for timestamps only, so a DATE column casts explicitly.
    """

    if temporal_shape(sql_type) == "date":
        return f"DATE_TRUNC('{unit}', {cn}::TIMESTAMP)"

    return f"DATE_TRUNC('{unit}', {cn})"


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
    """Copy the drawn fraction into a session-lifetime temp table and name it on the scope -
    `RANDOM()` is evaluated exactly once here.
    """

    name = materialized_name(identity.fqn)
    drawn = _sample_expr(identity, scope)
    exec_query(cursor, f"CREATE TEMPORARY TABLE {quote(name, DIALECT)} AS SELECT * FROM {drawn}")

    return replace(scope, materialized=name)


def release(cursor: Cursor, scope: TableScope) -> None:
    """Drop the copied sample; the session would drop it anyway, this frees it sooner."""

    if scope.materialized is None:
        return

    exec_query(cursor, f"DROP TABLE IF EXISTS {quote(scope.materialized, DIALECT)}")


def _sample_expr(identity: Identity, scope: TableScope) -> str:
    """The `RANDOM()`-bearing source `materialize_scope` reads once to build its copy - a derived
    table in a `FROM` clause needs an alias under Postgres-family grammar.
    """

    quoted = identity.quoted()

    if scope.filter is not None:
        return f"(SELECT * FROM {quoted} WHERE ({scope.filter})) {SOURCE_ALIAS}"

    return f"(SELECT * FROM {quoted} WHERE RANDOM() < {scope.sample}) {SOURCE_ALIAS}"


def _source(quoted_fqn: str, scope: TableScope | None, seed: int | None = None) -> str:
    """Table reference every statistics query selects FROM - a `sample` scope with no materialized
    copy never reaches here, `orchestrator._materialize_scope` having refused the table first.
    """

    del seed

    if scope is None or not scope.narrows:
        return f"{quoted_fqn} {SOURCE_ALIAS}"
    elif scope.materialized is not None:
        return f"{quote(scope.materialized, DIALECT)} {SOURCE_ALIAS}"
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
        a = column_alias(col.name)
        select_parts.append(f"COUNT(1) - COUNT({cn}) AS null_{a}")

        if is_numeric_type(col.classified_type):
            select_parts.extend(
                (
                    f"COALESCE(SUM(CASE WHEN {cn} = 0 THEN 1 ELSE 0 END), 0) AS zero_{a}",
                    f"COALESCE(SUM(CASE WHEN {cn} < 0 THEN 1 ELSE 0 END), 0) AS neg_{a}",
                    f"COALESCE(SUM(CASE WHEN {cn} = TRUNC({cn}) THEN 1 ELSE 0 END), 0) AS quant_{a}",
                ),
            )
        elif measures_length(col.classified_type, _is_unsupported):
            empty_condition, length_expr = _length_exprs(cn, col.classified_type)
            select_parts.extend(
                (
                    f"COALESCE(SUM(CASE WHEN {empty_condition} THEN 1 ELSE 0 END), 0) AS empty_{a}",
                    f"MIN({length_expr}) AS lenmin_{a}",
                    f"MAX({length_expr}) AS lenmax_{a}",
                    f"AVG({length_expr}::FLOAT8) AS lenavg_{a}",
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

    return f"{cn}::VARCHAR = ''", f"LENGTH({cn}::VARCHAR)"


def _fetch_value_list(
    cursor: Cursor,
    source: str,
    col: ColumnMeta,
    non_null: int,
    config: StatisticsConfig,
) -> ValueList:
    cn = source_column(col, DIALECT)
    select_expr = cn

    if is_binary_type(col.classified_type):
        select_expr = render_binary(cn)
    elif temporal_shape(col.classified_type) in ("timestamp_tz", "time_tz"):
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
    # Few accessors take a GEOGRAPHY, so every reading goes through the cast to GEOMETRY.
    g = f"{cn}::GEOMETRY"

    return statements.spatial(
        partial(exec_query, cursor),
        source,
        cn,
        statements.SpatialAccessors(
            kind=f"GEOMETRYTYPE({g})",
            srid=f"ST_SRID({g})",
            flag=(
                f"CASE\n"
                f"  WHEN ST_ZMAX({g}) IS NULL AND ST_MMAX({g}) IS NULL THEN 0\n"
                f"  WHEN ST_ZMAX({g}) IS NULL THEN 1\n"
                f"  WHEN ST_MMAX({g}) IS NULL THEN 2\n"
                f"  ELSE 3\n"
                f"END"
            ),
            empty=f"ST_ISEMPTY({g})",
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
    cn = source_column(col, DIALECT)
    select_parts = [
        f"MIN({cn}) AS mn",
        f"MAX({cn}) AS mx",
        f"AVG({cn}::FLOAT8) AS avg_val",
        f"SUM({cn}) AS sum_val",
        *_percentile_select(cn, config.percentiles),
    ]
    row = exec_query(cursor, select_from(select_parts, source)).fetchone()

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


def _temporal(
    cursor: Cursor,
    source: str,
    stats: ColumnStats,
    col: ColumnMeta,
    non_null: int,
    config: StatisticsConfig,
) -> ColumnStats:
    rng, percentiles, quantized = _fetch_temporal_scalars(cursor, source, col, config)
    cn = source_column(col, DIALECT)

    return temporal_with_top_n(
        stats,
        rng,
        percentiles,
        quantized,
        lambda: _approximate_distribution_via_top_n(
            cursor,
            source,
            render_domain(cn, col.classified_type),
            cn,
            non_null,
            config,
            lambda v: v,
        ),
    )


def _fetch_temporal_scalars(
    cursor: Cursor,
    source: str,
    col: ColumnMeta,
    config: StatisticsConfig,
) -> tuple[Range, dict[str, Any], int | None]:
    """MIN/MAX/percentiles/span/quantized_count for a temporal column - the caller fetches the
    value list separately, so a failure there never costs these already-measured scalars.
    """

    cn = source_column(col, DIALECT)
    keys = config.percentiles
    date_only = temporal_shape(col.classified_type) == "date"
    time_only = temporal_shape(col.classified_type) in ("time", "time_tz")
    day_aligned = not (date_only or time_only)

    agg_select = [f"MIN({cn}) AS mn", f"MAX({cn}) AS mx"]

    if date_only:
        # `EXTRACT(EPOCH FROM ...)` accepts no DATE difference, and no `-` operator is documented
        # for two DATEs either, so DATEDIFF is the documented day-count function here.
        agg_select.append(f"DATEDIFF('day', MIN({cn}), MAX({cn})) AS span_days")
    elif not time_only:
        # An integer microsecond count, divided only once it is an exact multiple of a day:
        # no float epoch and no rounded division (SPEC 2.2.4).
        micros = f"DATEDIFF('microsecond', MIN({cn}), MAX({cn}))"
        agg_select.append(
            f"({micros} - MOD({micros}, 86400000000)) / 86400000000 AS span_days",
        )

    if day_aligned:
        agg_select.append(
            f"SUM(CASE WHEN {cn} = DATE_TRUNC('day', {cn}) THEN 1 ELSE 0 END) AS quant",
        )

    percentile_renders = [
        (f"p{p:02d}", render_domain(f"agg.p_{p:02d}", col.classified_type)) for p in keys
    ]
    outer_select = [
        f"{render_domain('agg.mn', col.classified_type)} AS mn_text",
        f"{render_domain('agg.mx', col.classified_type)} AS mx_text",
        *(["agg.span_days"] if not time_only else ["0 AS span_days"]),
        *(f"{expr} AS {key}_text" for key, expr in percentile_renders),
        *(["agg.quant"] if day_aligned else []),
    ]

    aggregated = select_from([*agg_select, *_percentile_select_disc(cn, keys)], source)
    row = exec_query(cursor, select_from(outer_select, derived(aggregated, "agg"))).fetchone()

    if row is None:
        return Range(min=None, max=None, span_days=0), {}, None

    span_raw = row[2]
    n_pct = len(keys)
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

    return rng, percentiles, quantized_count


def _percentile_select(quoted_col: str, keys: Any) -> list[str]:
    """`PERCENTILE_CONT` projections, one `WITHIN GROUP` per requested key - every clause
    orders by the same column, which is what Redshift's one-shape restriction allows.
    """

    return [
        f"PERCENTILE_CONT({p / 100.0}) WITHIN GROUP (ORDER BY {quoted_col}) AS p_{p:02d}"
        for p in keys
    ]


def _percentile_select_disc(quoted_col: str, keys: Any) -> list[str]:
    """`APPROXIMATE PERCENTILE_DISC` projections, one per requested key - the only nearest-rank
    aggregate here, the plain form not existing on Redshift at all.
    """

    return [
        f"APPROXIMATE PERCENTILE_DISC({p / 100.0}) WITHIN GROUP (ORDER BY {quoted_col}) AS p_{p:02d}"
        for p in keys
    ]


def _fetch_length_p95(cursor: Cursor, source: str, col: ColumnMeta) -> float | None:
    cn = source_column(col, DIALECT)
    _, length_expr = _length_exprs(cn, col.classified_type)
    row = exec_query(
        cursor,
        f"""
        SELECT
          PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY {length_expr})
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
