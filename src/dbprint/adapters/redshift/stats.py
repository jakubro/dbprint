"""Two-phase batched per-table statistics computation for Redshift - Phase B pre-classifies
internally and both MUST converge; `cardinality_method` is always `exact`, nothing being stored.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from dbprint.config import StatisticsConfig
from dbprint.spec.classification import base_type, compute_cardinality_ratio, compute_null_rate
from dbprint.spec.coverage import coverage_share
from dbprint.spec.distribution import classify as classify_distribution
from dbprint.spec.distribution import summarize as summarize_frequencies
from dbprint.spec.temporal_range import is_representable
from .connection import exec_query
from .identity import Identity
from .introspect import table_rows_estimate
from ..base import (
    BaseStats,
    ColumnMeta,
    ColumnProgress,
    ColumnStats,
    Distribution,
    Frequencies,
    Length,
    NullPatterns,
    Range,
    RowCountMethod,
    TableCounts,
    TableScope,
    ValueCount,
    has_measurable_nulls,
    materialized_name,
    null_flags,
    null_patterns_from_rows,
    seed_from_fqn,
)


if TYPE_CHECKING:
    from .connection import Cursor


# Reduction range for the sampling seed - a safe width, not a documented Redshift limit.
SEED_MODULUS = 2**31

_NUMERIC_TYPES = (
    "smallint",
    "integer",
    "int",
    "bigint",
    "decimal",
    "numeric",
    "real",
    "double precision",
)

_TEMPORAL_TYPES = (
    "date",
    "time",
    "time without time zone",
    "time with time zone",
    "timestamp",
    "timestamp without time zone",
    "timestamp with time zone",
)

_TIME_ONLY_TYPES = ("time", "time without time zone", "time with time zone")
_DATE_ONLY_TYPES = ("date",)
_TZ_TYPES = ("timestamp with time zone", "time with time zone")

_BOOLEAN_TYPES = ("boolean",)
_JSON_TYPES = ("super",)
_UNSUPPORTED_TYPES = ("varbyte", "geometry", "geography", "hllsketch")


def compute_base(
    cursor: Cursor,
    identity: Identity,
    columns: list[ColumnMeta],
    scope: TableScope | None = None,
) -> tuple[TableCounts, dict[str, BaseStats]]:
    """Phase A: the table's counts plus per-column null_count and cardinality."""

    if not columns:
        return TableCounts(row_count=0, rows_scanned=0), {}

    source = _table_source(identity, scope)
    rows_scanned, base_stats = _phase_a(cursor, source, columns)
    row_count, row_count_method = _table_row_count(cursor, identity, rows_scanned, scope)

    return TableCounts(row_count, rows_scanned, row_count_method), base_stats


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
) -> dict[str, ColumnStats]:
    """Phase B: the classification-specific statistics, keyed by column name."""

    if not columns:
        return {}

    if counts.rows_scanned == 0:
        if scope is not None and scope.narrows:
            return {}

        return {c.name: _empty_stats(c) for c in columns}

    source = _table_source(identity, scope)
    enriched: dict[str, ColumnStats] = {}
    total = len(columns)

    for index, col in enumerate(columns, start=1):
        if on_column is not None:
            on_column(index, total, col.name)

        pre = _pre_classify(
            col,
            base[col.name].cardinality,
            config,
            col.name in fk_source_columns,
        )
        enriched[col.name] = _phase_b(
            cursor,
            source,
            col,
            base[col.name],
            counts.rows_scanned,
            pre,
            config,
            suppressed=col.name in suppress_values,
        )

    return enriched


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

    if not has_measurable_nulls(counts, base):
        return None

    source = _table_source(identity, scope)
    quoted = [_quote_ident(col.physical_name or col.name) for col in columns]
    cap = config.top_n_null_patterns
    rows = exec_query(
        cursor,
        f"""
        SELECT {null_flags(quoted, concat=False)} AS dbprint_nulls, COUNT(*) AS cnt
        FROM {source}
        GROUP BY 1
        ORDER BY cnt DESC, dbprint_nulls ASC
        LIMIT %s
        """,
        (cap + 1,),
    ).fetchall()

    return null_patterns_from_rows(rows, columns, counts.rows_scanned, cap)


def probe_grain(
    cursor: Cursor,
    identity: Identity,
    columns: list[ColumnMeta],
    counts: TableCounts,
    candidates: tuple[tuple[str, str], ...],
    scope: TableScope | None = None,
) -> tuple[tuple[str, str], ...]:
    """One batched statement testing every candidate pair. See SPEC 2.2.12."""

    if not candidates:
        return ()

    source = _table_source(identity, scope)
    physical = {col.name: col.physical_name or col.name for col in columns}
    exprs = [
        f"COUNT(DISTINCT {_composite_distinct_expr(physical[a], physical[b])}) AS dbprint_grain_{i}"
        for i, (a, b) in enumerate(candidates)
    ]
    row = exec_query(cursor, f"SELECT {', '.join(exprs)} FROM {source}").fetchone()

    if row is None:
        return ()

    return tuple(pair for i, pair in enumerate(candidates) if row[i] == counts.rows_scanned)


def probe_timeline(
    cursor: Cursor,
    identity: Identity,
    columns: list[ColumnMeta],
    counts: TableCounts,
    column: str,
    unit: Literal["day", "week", "month"],
    scope: TableScope | None = None,
) -> tuple[tuple[str, int], ...]:
    """One grouped statement bucketing `column` at `unit` grain (SPEC 2.2.16) - truncation sits in
    a CTE, so ordering sorts the temporal value, and the render alone applies the TZ conversion.
    """

    del counts

    source = _table_source(identity, scope)
    by_name = {col.name: col for col in columns}
    col = by_name[column]
    cn = _quote_ident(col.physical_name or col.name)
    bucket_expr = _timeline_bucket_expr(cn, col.sql_type, unit)

    rows = exec_query(
        cursor,
        f"""
        SELECT {_render_calendar_bound("bucket_start", col.sql_type)} AS bucket_text, cnt
        FROM (
            SELECT {bucket_expr} AS bucket_start, COUNT(*) AS cnt
            FROM {source}
            WHERE {cn} IS NOT NULL
            GROUP BY 1
        ) buckets
        ORDER BY bucket_start
        """,
    ).fetchall()

    return tuple((row[0], int(row[1])) for row in rows)


def _timeline_bucket_expr(cn: str, sql_type: str, unit: str) -> str:
    """Truncation expression for `probe_timeline`'s GROUP BY key (SPEC 2.2.16) - `DATE_TRUNC` is
    documented for timestamps only, so a DATE column casts explicitly.
    """

    if _matches(sql_type, _DATE_ONLY_TYPES):
        return f"DATE_TRUNC('{unit}', {cn}::timestamp)"

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
    """One statement, two conditional aggregates per subject column. See SPEC 2.2.4."""

    del counts

    if not subject_columns:
        return {}

    source = _table_source(identity, scope)
    by_name = {col.name: col for col in columns}
    anchor = by_name[anchor_column]
    anchor_cn = _quote_ident(anchor.physical_name or anchor.name)

    agg_exprs = []
    outer_exprs = []

    for i, subject in enumerate(subject_columns):
        subject_cn = _quote_ident(by_name[subject].physical_name or by_name[subject].name)
        agg_exprs.append(
            f"MIN(CASE WHEN {subject_cn} IS NOT NULL THEN {anchor_cn} END) AS from_{i}",
        )
        agg_exprs.append(f"MAX(CASE WHEN {subject_cn} IS NOT NULL THEN {anchor_cn} END) AS to_{i}")
        outer_exprs.append(
            f"{_render_calendar_bound(f'from_{i}', anchor.sql_type)} AS from_{i}_text",
        )
        outer_exprs.append(f"{_render_calendar_bound(f'to_{i}', anchor.sql_type)} AS to_{i}_text")

    row = exec_query(
        cursor,
        f"""
        SELECT {", ".join(outer_exprs)}
        FROM (SELECT {", ".join(agg_exprs)} FROM {source}) agg
        """,
    ).fetchone()

    if row is None:
        return {}

    windows: dict[str, tuple[str, str]] = {}

    for i, subject in enumerate(subject_columns):
        from_text, to_text = row[2 * i], row[2 * i + 1]

        if from_text is not None and to_text is not None:
            windows[subject] = (from_text, to_text)

    return windows


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

    if not candidates:
        return {}

    source = _table_source(identity, scope)
    physical = {col.name: col.physical_name or col.name for col in columns}
    exprs = [
        f"COUNT(DISTINCT {_composite_distinct_expr(physical[a], physical[b])}) AS dbprint_dep_{i}"
        for i, (a, b) in enumerate(candidates)
    ]
    row = exec_query(cursor, f"SELECT {', '.join(exprs)} FROM {source}").fetchone()

    if row is None:
        return {}

    out: dict[tuple[str, str], float] = {}

    for i, (a, b) in enumerate(candidates):
        joint = row[i]

        if joint:
            out[(a, b)] = min(1.0, base[a].cardinality / joint)

    return out


def materialize(cursor: Cursor, identity: Identity, scope: TableScope) -> TableScope:
    """Copy the drawn fraction into a session-lifetime temp table and name it on the scope -
    `RANDOM()` is evaluated exactly once here.
    """

    name = materialized_name(identity.dotted().lower())
    drawn = _sample_expr(identity, scope)
    exec_query(cursor, f"CREATE TEMPORARY TABLE {_quote_ident(name)} AS SELECT * FROM {drawn}")

    return replace(scope, materialized=name)


def release(cursor: Cursor, scope: TableScope) -> None:
    """Drop the copied sample; the session would drop it anyway, this frees it sooner."""

    if scope.materialized is None:
        return

    exec_query(cursor, f"DROP TABLE IF EXISTS {_quote_ident(scope.materialized)}")


def _sample_expr(identity: Identity, scope: TableScope) -> str:
    """The `RANDOM()`-bearing source `materialize_scope` reads once to build its copy - a derived
    table in a `FROM` clause needs an alias under Postgres-family grammar.
    """

    quoted = identity.quoted()

    if scope.filter is not None:
        return f"(SELECT * FROM {quoted} WHERE ({scope.filter})) AS dbprint_scoped"

    return f"(SELECT * FROM {quoted} WHERE RANDOM() < {scope.sample}) AS dbprint_scoped"


def _table_source(identity: Identity, scope: TableScope | None) -> str:
    """The FROM expression every phase reads, addressing the catalog's own spelling."""

    return _source(identity.quoted(), scope)


def _seed(identity: Identity) -> int:
    """The table's draw seed, hashed from the FOLDED path - the artifact's own name for it."""

    return seed_from_fqn(identity.dotted().lower(), SEED_MODULUS)


def _source(quoted_fqn: str, scope: TableScope | None, seed: int | None = None) -> str:
    """Table reference every statistics query selects FROM - a `sample` scope with no materialized
    copy never reaches here, `orchestrator._materialize_scope` having refused the table first.
    """

    del seed

    if scope is None or not scope.narrows:
        return quoted_fqn
    elif scope.materialized is not None:
        return _quote_ident(scope.materialized)
    else:
        return f"(SELECT * FROM {quoted_fqn} WHERE ({scope.filter})) AS dbprint_scoped"


def _table_row_count(
    cursor: Cursor,
    identity: Identity,
    rows_scanned: int,
    scope: TableScope | None,
) -> tuple[int, RowCountMethod]:
    """Rows in the table and how they were obtained (SPEC 2.2.1) - a narrowed read takes the
    catalog estimate where one is available, and counts exactly where none is.
    """

    if scope is None or not scope.narrows:
        return rows_scanned, "exact"

    estimate = table_rows_estimate(cursor, identity)

    if estimate >= 0:
        return estimate, "approximate"

    row = exec_query(cursor, f"SELECT COUNT(*) FROM {identity.quoted()}").fetchone()

    return (int(row[0]) if row and row[0] is not None else rows_scanned), "exact"


def _phase_a(
    cursor: Cursor,
    source: str,
    columns: list[ColumnMeta],
) -> tuple[int, dict[str, BaseStats]]:
    """One query yielding row_count + per-column null_count + cardinality."""

    select_parts: list[str] = ["COUNT(*) AS row_count"]

    for col in columns:
        cn = _quote_ident(col.physical_name or col.name)
        a = _alias(col.name)
        select_parts.append(f"COUNT(*) - COUNT({cn}) AS null_{a}")

        if _matches(col.sql_type, _NUMERIC_TYPES):
            select_parts.append(
                f"COALESCE(SUM(CASE WHEN {cn} = 0 THEN 1 ELSE 0 END), 0) AS zero_{a}",
            )
            select_parts.append(
                f"COALESCE(SUM(CASE WHEN {cn} < 0 THEN 1 ELSE 0 END), 0) AS neg_{a}",
            )
            select_parts.append(
                f"COALESCE(SUM(CASE WHEN {cn} = TRUNC({cn}) THEN 1 ELSE 0 END), 0) AS quant_{a}",
            )
        elif _is_string_like(col.sql_type):
            select_parts.append(
                f"COALESCE(SUM(CASE WHEN {cn}::varchar = '' THEN 1 ELSE 0 END), 0) AS empty_{a}",
            )
            length_expr = f"LENGTH({cn}::varchar)"
            select_parts.append(f"MIN({length_expr}) AS lenmin_{a}")
            select_parts.append(f"MAX({length_expr}) AS lenmax_{a}")
            select_parts.append(f"AVG({length_expr}::float8) AS lenavg_{a}")

        select_parts.append(f"COUNT(DISTINCT {cn}) AS card_{a}")

    row = exec_query(cursor, f"SELECT {', '.join(select_parts)} FROM {source}").fetchone()

    if row is None:
        return 0, {c.name: _empty_base(c) for c in columns}

    row_count = int(row[0])
    out: dict[str, BaseStats] = {}
    idx = 1

    for col in columns:
        null_count = int(row[idx])
        idx += 1
        zero_count = negative_count = empty_count = quantized_count = None
        length_min = length_max = length_avg = None

        if _matches(col.sql_type, _NUMERIC_TYPES):
            zero_count, negative_count, quantized_count = (
                int(row[idx]),
                int(row[idx + 1]),
                int(row[idx + 2]),
            )
            idx += 3
        elif _is_string_like(col.sql_type):
            empty_count = int(row[idx])
            length_min, length_max, length_avg = row[idx + 1], row[idx + 2], row[idx + 3]
            idx += 4

        cardinality = int(row[idx])
        idx += 1
        out[col.name] = BaseStats(
            null_count=null_count,
            cardinality=cardinality,
            cardinality_method="exact",
            supported=not _is_unsupported(col.sql_type),
            zero_count=zero_count,
            negative_count=negative_count,
            empty_count=empty_count,
            quantized_count=quantized_count,
            length_min=length_min,
            length_max=length_max,
            length_avg=length_avg,
        )

    return row_count, out


def _empty_stats(col: ColumnMeta) -> ColumnStats:
    if _is_unsupported(col.sql_type):
        return ColumnStats(
            sql_type=col.sql_type,
            nullable=col.nullable,
            null_count=0,
            null_rate=0.0,
            cardinality=None,
            cardinality_ratio=None,
            cardinality_method=None,
        )

    return ColumnStats(
        sql_type=col.sql_type,
        nullable=col.nullable,
        null_count=0,
        null_rate=0.0,
        cardinality=0,
        cardinality_ratio=0.0,
        cardinality_method="exact",
    )


def _phase_b(
    cursor: Cursor,
    source: str,
    col: ColumnMeta,
    base: BaseStats,
    rows_scanned: int,
    pre: str,
    config: StatisticsConfig,
    *,
    suppressed: bool = False,
) -> ColumnStats:
    null_count = base.null_count
    null_rate = compute_null_rate(null_count, rows_scanned)

    if pre == "unsupported":
        return ColumnStats(
            sql_type=col.sql_type,
            nullable=col.nullable,
            null_count=null_count,
            null_rate=null_rate,
            cardinality=None,
            cardinality_ratio=None,
            cardinality_method=None,
        )

    cardinality = int(base.cardinality)
    cardinality_ratio = compute_cardinality_ratio(cardinality, rows_scanned)
    non_null = rows_scanned - null_count
    length_min, length_max = base.length_min, base.length_max

    if length_min is not None and length_max is not None:
        length_p95 = _fetch_length_p95(cursor, source, col)
        length = (
            Length(
                min=length_min,
                max=length_max,
                avg=_round_numeric(base.length_avg),
                p95=length_p95,
            )
            if length_p95 is not None
            else None
        )
    else:
        length = None

    stats = ColumnStats(
        sql_type=col.sql_type,
        nullable=col.nullable,
        null_count=null_count,
        null_rate=null_rate,
        cardinality=cardinality,
        cardinality_ratio=cardinality_ratio,
        cardinality_method="exact",
        # Phase A gates these on raw sql_type, not on `pre` - a numeric type that classifies
        # categorical would otherwise carry a field its own classification forbids.
        zero_count=base.zero_count if pre == "numeric" else None,
        negative_count=base.negative_count if pre == "numeric" else None,
        empty_count=base.empty_count if pre == "text" else None,
        quantized_count=base.quantized_count if pre == "numeric" else None,
        length=length,
    )

    if pre == "json":
        return stats

    if pre == "boolean":
        values, coverage, _ = _fetch_value_list(cursor, source, col, non_null, config)

        return replace(stats, values=values, values_coverage=coverage)

    if pre == "categorical":
        values, coverage, exhaustive = _fetch_value_list(cursor, source, col, non_null, config)
        distribution = classify_distribution(
            [v.count for v in values],
            non_null,
            exhaustive=exhaustive,
        )

        return replace(stats, values=values, values_coverage=coverage, distribution=distribution)

    if pre == "numeric":
        rng, percentiles, distribution, frequencies, values, mean, total = _fetch_numeric_block(
            cursor,
            source,
            col,
            non_null,
            config,
        )

        return replace(
            stats,
            range=rng,
            percentiles=percentiles,
            distribution=distribution,
            frequencies=frequencies,
            values=values,
            mean=mean,
            sum=total,
        )

    if pre == "temporal":
        rng, percentiles, quantized = _fetch_temporal_scalars(cursor, source, col, config)
        unrepresentable = _unrepresentable_fields(rng, percentiles)
        cn = _quote_ident(col.physical_name or col.name)

        try:
            distribution, frequencies, values = _approximate_distribution_via_top_n(
                cursor,
                source,
                _render_calendar_bound(cn, col.sql_type),
                cn,
                non_null,
                config,
                lambda v: v,
            )
        except Exception:  # noqa: BLE001 - only the top-N statement is guarded, so scalars survive
            # `distribution`/`frequencies` are REQUIRED (SPEC 2.2.3); an empty count list would
            # classify `uniform` over nothing.
            return replace(
                stats,
                range=rng,
                percentiles=percentiles,
                unrepresentable=unrepresentable or None,
                quantized_count=quantized,
                unmeasured=("distribution", "frequencies", "values"),
            )

        return replace(
            stats,
            range=rng,
            percentiles=percentiles,
            distribution=distribution,
            frequencies=frequencies,
            unrepresentable=unrepresentable or None,
            values=values,
            quantized_count=quantized,
        )

    if suppressed and pre == "text":
        return stats

    values, coverage, exhaustive = _fetch_value_list(cursor, source, col, non_null, config)
    distribution = classify_distribution([v.count for v in values], non_null, exhaustive=exhaustive)

    return replace(stats, values=values, values_coverage=coverage, distribution=distribution)


def _pre_classify(
    col: ColumnMeta,
    cardinality: int,
    config: StatisticsConfig,
    has_declared_fk: bool,
) -> str:
    if _is_unsupported(col.sql_type):
        return "unsupported"
    elif _matches(col.sql_type, _BOOLEAN_TYPES):
        return "boolean"
    elif _matches(col.sql_type, _JSON_TYPES):
        return "json"
    elif has_declared_fk:
        return "foreign_key_candidate"
    elif cardinality <= config.enumeration_threshold:
        return "categorical"
    elif _matches(col.sql_type, _TEMPORAL_TYPES):
        return "temporal"
    elif _matches(col.sql_type, _NUMERIC_TYPES):
        return "numeric"
    else:
        return "text"


def _fetch_value_list(
    cursor: Cursor,
    source: str,
    col: ColumnMeta,
    non_null: int,
    config: StatisticsConfig,
) -> tuple[tuple[ValueCount, ...], float, bool]:
    cn = _quote_ident(col.physical_name or col.name)
    n = config.top_n_values
    select_expr = (
        _render_calendar_bound(cn, col.sql_type) if _matches(col.sql_type, _TZ_TYPES) else cn
    )
    rows = exec_query(
        cursor,
        f"""
        SELECT {select_expr} AS rendered, COUNT(*) AS cnt
        FROM {source}
        WHERE {cn} IS NOT NULL
        GROUP BY {cn}
        ORDER BY cnt DESC, {select_expr}::varchar ASC
        LIMIT %s
        """,
        (n + 1,),
    ).fetchall()
    exhaustive = len(rows) <= n
    entries = sorted(
        (ValueCount(value=_iso_or_value(value), count=int(cnt)) for value, cnt in rows[:n]),
        key=lambda v: (-v.count, str(v.value)),
    )
    values = tuple(entries)
    total = sum(v.count for v in values)

    return values, coverage_share(total, non_null, exhaustive=exhaustive), exhaustive


def _fetch_numeric_block(
    cursor: Cursor,
    source: str,
    col: ColumnMeta,
    non_null: int,
    config: StatisticsConfig,
) -> tuple[
    Range,
    dict[str, Any],
    Distribution,
    Frequencies,
    tuple[ValueCount, ...],
    float | None,
    float | None,
]:
    cn = _quote_ident(col.physical_name or col.name)
    keys = config.percentiles
    sql = (
        f"SELECT MIN({cn}) AS mn, MAX({cn}) AS mx, AVG({cn}::float8) AS avg_val, SUM({cn}) AS sum_val"
        f"{_percentile_select(cn, keys)} FROM {source}"
    )
    row = exec_query(cursor, sql).fetchone()

    if row is None:
        return Range(min=None, max=None), {}, "uniform", summarize_frequencies([]), (), None, None

    rng = Range(
        min=_round_numeric(row[0], exact_int=True),
        max=_round_numeric(row[1], exact_int=True),
    )
    mean = _round_numeric(row[2])
    total = _round_numeric(row[3], exact_int=True)
    percentiles = {f"p{p:02d}": _round_numeric(v) for p, v in zip(keys, row[4:])}
    distribution, frequencies, values = _approximate_distribution_via_top_n(
        cursor,
        source,
        cn,
        cn,
        non_null,
        config,
        _round_numeric,
    )

    return rng, percentiles, distribution, frequencies, values, mean, total


def _fetch_temporal_scalars(
    cursor: Cursor,
    source: str,
    col: ColumnMeta,
    config: StatisticsConfig,
) -> tuple[Range, dict[str, Any], int | None]:
    """MIN/MAX/percentiles/span/quantized_count for a temporal column - the caller fetches the
    value list separately, so a failure there never costs these already-measured scalars.
    """

    cn = _quote_ident(col.physical_name or col.name)
    keys = config.percentiles
    date_only = _matches(col.sql_type, _DATE_ONLY_TYPES)
    time_only = _matches(col.sql_type, _TIME_ONLY_TYPES)
    day_aligned = not (date_only or time_only)

    agg_select = [f"MIN({cn}) AS mn", f"MAX({cn}) AS mx"]

    if date_only:
        # `EXTRACT(EPOCH FROM ...)` accepts no DATE difference, and no `-` operator is documented
        # for two DATEs either, so DATEDIFF is the documented day-count function here.
        agg_select.append(f"DATEDIFF('day', MIN({cn}), MAX({cn})) AS span_days")
    elif not time_only:
        agg_select.append(
            f"FLOOR(EXTRACT(EPOCH FROM (MAX({cn}) - MIN({cn}))) / 86400) AS span_days",
        )

    if day_aligned:
        agg_select.append(
            f"SUM(CASE WHEN {cn} = DATE_TRUNC('day', {cn}) THEN 1 ELSE 0 END) AS quant",
        )

    percentile_renders = [
        (f"p{p:02d}", _render_calendar_bound(f"p_{p:02d}", col.sql_type)) for p in keys
    ]
    outer_select = [
        f"{_render_calendar_bound('mn', col.sql_type)} AS mn_text",
        f"{_render_calendar_bound('mx', col.sql_type)} AS mx_text",
        *(["span_days"] if not time_only else ["0 AS span_days"]),
        *(f"{expr} AS {key}_text" for key, expr in percentile_renders),
        *(["quant"] if day_aligned else []),
    ]

    sql = (
        f"SELECT {', '.join(outer_select)} FROM ("
        f"SELECT {', '.join(agg_select)}{_percentile_select_disc(cn, keys)} FROM {source}"
        f") agg"
    )
    row = exec_query(cursor, sql).fetchone()

    if row is None:
        return Range(min=None, max=None, span_days=0), {}, None

    span_raw = row[2]
    n_pct = len(keys)
    percentile_texts = row[3 : 3 + n_pct]
    quantized_count = int(row[3 + n_pct]) if day_aligned else None

    span_days = int(span_raw) if span_raw is not None else 0
    rng = Range(min=row[0], max=row[1], span_days=span_days)
    percentiles = {key: text for (key, _), text in zip(percentile_renders, percentile_texts)}

    return rng, percentiles, quantized_count


def _render_calendar_bound(expr: str, sql_type: str) -> str:
    """SQL text rendering `expr` per SPEC 2.2.4's domain-rendering rule - a tz-aware type
    normalizes to UTC first, and `TO_CHAR`'s always-six-digit `US` field is stripped.
    """

    is_tz = _matches(sql_type, _TZ_TYPES)
    is_date_only = _matches(sql_type, _DATE_ONLY_TYPES)
    is_time_only = _matches(sql_type, _TIME_ONLY_TYPES)
    source_expr = f"({expr} AT TIME ZONE 'UTC')" if is_tz else expr

    if is_date_only:
        return f"TO_CHAR({source_expr}, 'YYYY-MM-DD')"

    # TIME carries no date field, so the picture omits YYYY-MM-DD entirely - TO_CHAR would
    # otherwise render today's date rather than raise, silently fabricating a calendar day.
    picture = "HH24:MI:SS.US" if is_time_only else 'YYYY-MM-DD"T"HH24:MI:SS.US'
    body = f"TO_CHAR({source_expr}, '{picture}')"
    body = f"REGEXP_REPLACE({body}, '\\.000000$', '')"

    # NULL propagates through `||` on its own; no CASE guard is needed to keep a NULL bound NULL.
    return f"({body} || 'Z')" if is_tz else body


def _unrepresentable_fields(rng: Range, percentiles: dict[str, Any]) -> tuple[str, ...]:
    names = []

    if rng.min is not None and not is_representable(rng.min):
        names.append("min")

    if rng.max is not None and not is_representable(rng.max):
        names.append("max")

    for key in sorted(percentiles):
        value = percentiles[key]

        if value is not None and not is_representable(value):
            names.append(key)

    return tuple(names)


def _percentile_select(quoted_col: str, keys: Any) -> str:
    """Comma-prefixed `PERCENTILE_CONT` projections, one `WITHIN GROUP` per requested key - every
    clause orders by the same column, which is what Redshift's one-shape restriction allows.
    """

    if not keys:
        return ""

    parts = [
        f"PERCENTILE_CONT({p / 100.0}) WITHIN GROUP (ORDER BY {quoted_col}) AS p_{p:02d}"
        for p in keys
    ]

    return ", " + ", ".join(parts)


def _percentile_select_disc(quoted_col: str, keys: Any) -> str:
    """Comma-prefixed `APPROXIMATE PERCENTILE_DISC` projections, one per requested key - the
    only nearest-rank aggregate here, the plain form not existing on Redshift at all.
    """

    if not keys:
        return ""

    parts = [
        f"APPROXIMATE PERCENTILE_DISC({p / 100.0}) WITHIN GROUP (ORDER BY {quoted_col}) AS p_{p:02d}"
        for p in keys
    ]

    return ", " + ", ".join(parts)


def _fetch_length_p95(cursor: Cursor, source: str, col: ColumnMeta) -> float | None:
    cn = _quote_ident(col.physical_name or col.name)
    length_expr = f"LENGTH({cn}::varchar)"
    sql = (
        f"SELECT PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY {length_expr}) "
        f"FROM {source} WHERE {cn} IS NOT NULL"
    )
    row = exec_query(cursor, sql).fetchone()

    return _round_numeric(row[0]) if row is not None else None


def _approximate_distribution_via_top_n(
    cursor: Cursor,
    source: str,
    select_expr: str,
    group_expr: str,
    non_null: int,
    config: StatisticsConfig,
    value_transform: Any,
) -> tuple[Distribution, Frequencies, tuple[ValueCount, ...]]:
    n = config.top_n_values
    rows = exec_query(
        cursor,
        f"""
        SELECT {select_expr} AS rendered, COUNT(*) AS cnt
        FROM {source}
        WHERE {group_expr} IS NOT NULL
        GROUP BY {group_expr}
        ORDER BY cnt DESC, {select_expr}::varchar ASC
        LIMIT %s
        """,
        (n + 1,),
    ).fetchall()
    exhaustive = len(rows) <= n
    entries = sorted(
        (ValueCount(value=value_transform(value), count=int(cnt)) for value, cnt in rows[:n]),
        key=lambda v: (-v.count, str(v.value)),
    )
    values = tuple(entries)
    kept_counts = [v.count for v in values]

    return (
        classify_distribution(kept_counts, non_null, exhaustive=exhaustive),
        summarize_frequencies(kept_counts),
        values,
    )


def _empty_base(col: ColumnMeta) -> BaseStats:
    return BaseStats(
        null_count=0,
        cardinality=0,
        cardinality_method="exact",
        supported=not _is_unsupported(col.sql_type),
    )


def _is_unsupported(sql_type: str) -> bool:
    return base_type(sql_type) in _UNSUPPORTED_TYPES


def _matches(sql_type: str, types: tuple[str, ...]) -> bool:
    return base_type(sql_type) in types


def _is_string_like(sql_type: str) -> bool:
    return not (
        _is_unsupported(sql_type)
        or _matches(sql_type, _JSON_TYPES)
        or _matches(sql_type, _TEMPORAL_TYPES)
        or _matches(sql_type, _NUMERIC_TYPES)
        or _matches(sql_type, _BOOLEAN_TYPES)
    )


def _round_numeric(v: Any, *, exact_int: bool = False) -> Any:
    """`exact_int` is set only for count-like fields (`sum`, `range.min`/`max`) - an average or a
    percentile stays rate-valued, so it stays fractional even when one instance is whole (SPEC 2.2.6).
    """

    if v is None:
        return None

    if isinstance(v, int):
        return v

    # A Decimal integral to the last digit publishes exact - float64 loses precision above
    # 2**53, which a total over a bigint column reaches (SPEC 2.2.6 rounds only what is not).
    if exact_int and isinstance(v, Decimal) and v.is_finite() and v == int(v):
        return int(v)

    try:
        return round(float(v), 6)
    except (TypeError, ValueError):
        return v


def _iso_or_value(v: Any) -> Any:
    if v is None:
        return None

    iso = getattr(v, "isoformat", None)

    if callable(iso):
        s = iso()

        return s.replace("+00:00", "Z") if s.endswith("+00:00") else s

    return v


def _composite_distinct_expr(a: str, b: str) -> str:
    """A composite key `COUNT(DISTINCT ...)` can take - Redshift has no row constructor, so `a`/`b`
    are length-prefixed and concatenated, which no embedded delimiter can collide.
    """

    qa, qb = _quote_ident(a), _quote_ident(b)

    return (
        f"LENGTH({qa}::varchar)::varchar || ':' || {qa}::varchar || "
        f"'|' || LENGTH({qb}::varchar)::varchar || ':' || {qb}::varchar"
    )


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _alias(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name)
