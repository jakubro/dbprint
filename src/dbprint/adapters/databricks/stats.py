"""Two-phase batched per-table statistics computation for Databricks - Phase B pre-classifies
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
from .introspect import estimate_row_count
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
    temporal_block_unmeasured,
)


if TYPE_CHECKING:
    from .connection import Cursor


# Reduction range for the sampling seed - a safe width, not a documented Databricks limit.
SEED_MODULUS = 2**31

_NUMERIC_TYPES = (
    "tinyint",
    "smallint",
    "int",
    "bigint",
    "float",
    "double",
    "decimal",
)

_TEMPORAL_TYPES = ("date", "timestamp", "timestamp_ntz")
_TZ_TYPES = ("timestamp",)  # session-zone-converted on read; timestamp_ntz carries no zone
_DATE_ONLY_TYPES = ("date",)

_BOOLEAN_TYPES = ("boolean",)
_JSON_TYPES = ("variant",)
_UNSUPPORTED_TYPES = ("binary", "array", "map", "struct", "interval")


def compute_base(
    cursor: Cursor,
    fqn: str,
    columns: list[ColumnMeta],
    scope: TableScope | None = None,
) -> tuple[TableCounts, dict[str, BaseStats]]:
    """Phase A: the table's counts plus per-column null_count and cardinality."""

    if not columns:
        return TableCounts(row_count=0, rows_scanned=0), {}

    quoted = _quote_qualified(fqn)
    source = _table_source(fqn, quoted, scope)
    rows_scanned, base_stats = _phase_a(cursor, source, columns)
    row_count, row_count_method = _table_row_count(cursor, fqn, quoted, rows_scanned, scope)

    return TableCounts(row_count, rows_scanned, row_count_method), base_stats


def compute_columns(
    cursor: Cursor,
    fqn: str,
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

    source = _table_source(fqn, _quote_qualified(fqn), scope)
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
    fqn: str,
    columns: list[ColumnMeta],
    config: StatisticsConfig,
    counts: TableCounts,
    base: dict[str, BaseStats],
    scope: TableScope | None = None,
) -> NullPatterns | None:
    """Which columns are null together, in one grouped scan. See SPEC 2.2.10."""

    if not has_measurable_nulls(counts, base):
        return None

    source = _table_source(fqn, _quote_qualified(fqn), scope)
    quoted = [_quote_ident(col.name) for col in columns]
    cap = config.top_n_null_patterns
    rows = exec_query(
        cursor,
        f"""
        SELECT {null_flags(quoted, concat=True)} AS dbprint_nulls, COUNT(*) AS cnt
        FROM {source}
        GROUP BY 1
        ORDER BY cnt DESC, dbprint_nulls ASC
        LIMIT ?
        """,
        (cap + 1,),
    ).fetchall()

    return null_patterns_from_rows(rows, columns, counts.rows_scanned, cap)


def probe_grain(
    cursor: Cursor,
    fqn: str,
    columns: list[ColumnMeta],
    counts: TableCounts,
    candidates: tuple[tuple[str, str], ...],
    scope: TableScope | None = None,
) -> tuple[tuple[str, str], ...]:
    """One batched statement testing every candidate pair (SPEC 2.2.12) - `struct(a, b)`, the
    bare form silently dropping any row where either argument is null (documented).
    """

    del columns

    if not candidates:
        return ()

    source = _table_source(fqn, _quote_qualified(fqn), scope)
    exprs = [
        f"COUNT(DISTINCT struct({_quote_ident(a)}, {_quote_ident(b)})) AS dbprint_grain_{i}"
        for i, (a, b) in enumerate(candidates)
    ]
    row = exec_query(cursor, f"SELECT {', '.join(exprs)} FROM {source}").fetchone()

    if row is None:
        return ()

    return tuple(pair for i, pair in enumerate(candidates) if row[i] == counts.rows_scanned)


def probe_timeline(
    cursor: Cursor,
    fqn: str,
    columns: list[ColumnMeta],
    counts: TableCounts,
    column: str,
    unit: Literal["day", "week", "month"],
    scope: TableScope | None = None,
) -> tuple[tuple[str, int], ...]:
    """One grouped statement bucketing `column` at `unit` grain. See SPEC 2.2.16."""

    del counts

    source = _table_source(fqn, _quote_qualified(fqn), scope)
    by_name = {col.name: col for col in columns}
    col = by_name[column]
    cn = _quote_ident(col.name)
    normalized = _utc_expr(cn, col.sql_type)
    spark_unit = {"day": "DAY", "week": "WEEK", "month": "MONTH"}[unit]

    rows = exec_query(
        cursor,
        f"""
        SELECT DATE_FORMAT(bucket_start, 'yyyy-MM-dd') AS bucket_text, cnt
        FROM (
            SELECT DATE_TRUNC('{spark_unit}', {normalized}) AS bucket_start, COUNT(*) AS cnt
            FROM {source}
            WHERE {cn} IS NOT NULL
            GROUP BY 1
        ) buckets
        ORDER BY bucket_start
        """,
    ).fetchall()

    return tuple((row[0], int(row[1])) for row in rows)


def compute_populated_windows(
    cursor: Cursor,
    fqn: str,
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

    source = _table_source(fqn, _quote_qualified(fqn), scope)
    by_name = {col.name: col for col in columns}
    anchor = by_name[anchor_column]
    anchor_cn = _quote_ident(anchor.name)

    agg_exprs = []
    outer_exprs = []

    for i, subject in enumerate(subject_columns):
        subject_cn = _quote_ident(by_name[subject].name)
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
    fqn: str,
    columns: list[ColumnMeta],
    counts: TableCounts,
    base: dict[str, BaseStats],
    candidates: tuple[tuple[str, str], ...],
    scope: TableScope | None = None,
) -> dict[tuple[str, str], float]:
    """One batched statement measuring every candidate pair's joint cardinality. See SPEC 2.2.13."""

    del columns, counts

    if not candidates:
        return {}

    source = _table_source(fqn, _quote_qualified(fqn), scope)
    exprs = [
        f"COUNT(DISTINCT struct({_quote_ident(a)}, {_quote_ident(b)})) AS dbprint_dep_{i}"
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


def materialize(cursor: Cursor, fqn: str, scope: TableScope) -> TableScope:
    """Copy the drawn fraction into a session-lifetime temp table and name it on the scope -
    where `CREATE TEMPORARY TABLE` is unavailable this raises and the caller degrades safely.
    """

    name = materialized_name(fqn)
    drawn = _sample_expr(fqn, scope)
    exec_query(cursor, f"CREATE TEMPORARY TABLE {_quote_ident(name)} AS SELECT * FROM {drawn}")

    return replace(scope, materialized=name)


def release(cursor: Cursor, scope: TableScope) -> None:
    """Drop the copied sample; a no-op when materialization was never taken."""

    if scope.materialized is None:
        return

    exec_query(cursor, f"DROP TABLE IF EXISTS {_quote_ident(scope.materialized)}")


def _sample_expr(fqn: str, scope: TableScope) -> str:
    """The `TABLESAMPLE`-bearing source `materialize_scope` reads once to build its copy."""

    quoted = _quote_qualified(fqn)

    if scope.filter is not None:
        return f"(SELECT * FROM {quoted} WHERE ({scope.filter}))"

    assert scope.sample is not None  # TableScope guarantees exactly one of filter/sample

    return f"(SELECT * FROM {quoted} TABLESAMPLE ({scope.sample * 100} PERCENT))"


def _quote_qualified(fqn: str) -> str:
    schema, table = fqn.partition(".")[0], fqn.partition(".")[2]

    return f"`{schema}`.`{table}`"


def _table_source(fqn: str, quoted: str, scope: TableScope | None) -> str:
    return _source(fqn, quoted, scope, seed_from_fqn(fqn, SEED_MODULUS))


def _source(fqn: str, quoted_fqn: str, scope: TableScope | None, seed: int | None = None) -> str:
    """Table reference every statistics query selects FROM - `TABLESAMPLE ... REPEATABLE` is
    coherent on this engine (measured), so an unmaterialized scope still reads stably.
    """

    del fqn

    if scope is None or not scope.narrows:
        return quoted_fqn
    elif scope.materialized is not None:
        return _quote_ident(scope.materialized)
    elif scope.sample is not None:
        repeatable = "" if seed is None else f" REPEATABLE ({seed})"

        return f"{quoted_fqn} TABLESAMPLE ({scope.sample * 100} PERCENT){repeatable}"
    else:
        return f"(SELECT * FROM {quoted_fqn} WHERE ({scope.filter})) AS dbprint_scoped"


def _table_row_count(
    cursor: Cursor,
    fqn: str,
    quoted_fqn: str,
    rows_scanned: int,
    scope: TableScope | None,
) -> tuple[int, RowCountMethod]:
    """Rows in the table and how they were obtained (SPEC 2.2.1) - a narrowed read takes the
    catalog estimate, counting exactly where none exists so an empty match is not an empty table.
    """

    if scope is None or not scope.narrows:
        return rows_scanned, "exact"

    estimate = estimate_row_count(cursor, fqn)

    if estimate is not None:
        return estimate, "approximate"

    row = exec_query(cursor, f"SELECT COUNT(*) FROM {quoted_fqn}").fetchone()

    return (int(row[0]) if row and row[0] is not None else rows_scanned), "exact"


def _phase_a(
    cursor: Cursor,
    source: str,
    columns: list[ColumnMeta],
) -> tuple[int, dict[str, BaseStats]]:
    """One query yielding row_count + per-column null_count + cardinality."""

    select_parts: list[str] = ["COUNT(*) AS row_count"]

    for col in columns:
        cn = _quote_ident(col.name)
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
                f"COALESCE(SUM(CASE WHEN {cn} = FLOOR({cn}) THEN 1 ELSE 0 END), 0) AS quant_{a}",
            )
        elif _is_string_like(col.sql_type):
            select_parts.append(
                f"COALESCE(SUM(CASE WHEN CAST({cn} AS STRING) = '' THEN 1 ELSE 0 END), 0) AS empty_{a}",
            )
            length_expr = f"LENGTH(CAST({cn} AS STRING))"
            select_parts.append(f"MIN({length_expr}) AS lenmin_{a}")
            select_parts.append(f"MAX({length_expr}) AS lenmax_{a}")
            select_parts.append(f"AVG(CAST({length_expr} AS DOUBLE)) AS lenavg_{a}")

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
        try:
            rng, percentiles, distribution, unrepresentable, frequencies, values, quantized = (
                _fetch_temporal_block(cursor, source, col, non_null, config)
            )
        except Exception:  # noqa: BLE001 - the temporal block degrades as a whole, and its
            # fields are REQUIRED here (SPEC 2.2.3); the column names what the read cost it
            # rather than leaving an absence a reader would read as a structural cause.
            return replace(stats, unmeasured=temporal_block_unmeasured(col.sql_type))

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
    cn = _quote_ident(col.name)
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
        ORDER BY cnt DESC, CAST({select_expr} AS STRING) ASC
        LIMIT ?
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
    cn = _quote_ident(col.name)
    keys = config.percentiles
    levels = ", ".join(str(p / 100.0) for p in keys)
    sql = (
        f"SELECT MIN({cn}) AS mn, MAX({cn}) AS mx, AVG(CAST({cn} AS DOUBLE)) AS avg_val, "
        f"SUM({cn}) AS sum_val, PERCENTILE({cn}, ARRAY({levels})) AS pcts FROM {source}"
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
    pct_values = row[4] or []
    percentiles = {f"p{p:02d}": _round_numeric(v) for p, v in zip(keys, pct_values)}
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
    cn = _quote_ident(col.name)
    keys = config.percentiles
    day_aligned = not _matches(col.sql_type, _DATE_ONLY_TYPES)
    levels = ", ".join(str(p / 100.0) for p in keys)
    # TIMESTAMP_NTZ and DATE both reject a direct cast to DOUBLE, so every domain routes
    # through TIMESTAMP first and casts back through the same picture MIN/MAX render.
    epoch_expr = f"CAST(CAST({cn} AS TIMESTAMP) AS DOUBLE)"
    # Rendered SQL-side, not via Python's `.isoformat()`: PySpark collects a naive local
    # datetime, so a Python render would carry no 'Z' and read as outside its own range.
    rendered_min = _render_calendar_bound(f"MIN({cn})", col.sql_type)
    rendered_max = _render_calendar_bound(f"MAX({cn})", col.sql_type)

    max_epoch = f"CAST(CAST(MAX({cn}) AS TIMESTAMP) AS DOUBLE)"
    min_epoch = f"CAST(CAST(MIN({cn}) AS TIMESTAMP) AS DOUBLE)"

    agg_select = [
        f"{rendered_min} AS mn",
        f"{rendered_max} AS mx",
        f"FLOOR(({max_epoch} - {min_epoch}) / 86400) AS span_days",
        f"PERCENTILE({epoch_expr}, ARRAY({levels})) AS pcts",
    ]

    if day_aligned:
        agg_select.append(
            f"COALESCE(SUM(CASE WHEN {cn} = DATE_TRUNC('DAY', {cn}) THEN 1 ELSE 0 END), 0) AS quant",
        )

    row = exec_query(cursor, f"SELECT {', '.join(agg_select)} FROM {source}").fetchone()

    if row is None:
        empty_range = Range(min=None, max=None, span_days=0)

        return empty_range, {}, "uniform", (), summarize_frequencies([]), (), None

    span_raw = row[2]
    pct_epochs = row[3] or []
    quantized_count = int(row[4]) if day_aligned else None

    span_days = int(span_raw) if span_raw is not None else 0
    rng = Range(min=row[0], max=row[1], span_days=span_days)
    percentiles = {
        f"p{p:02d}": _epoch_to_rendered(cursor, epoch, col.sql_type)
        for p, epoch in zip(keys, pct_epochs)
    }

    distribution, frequencies, values = _approximate_distribution_via_top_n(
        cursor,
        source,
        _render_calendar_bound(cn, col.sql_type),
        cn,
        non_null,
        config,
        lambda v: v,
    )
    unrepresentable = _unrepresentable_fields(rng, percentiles)

    return rng, percentiles, distribution, unrepresentable, frequencies, values, quantized_count


def _epoch_to_rendered(cursor: Cursor, epoch: float, sql_type: str) -> str | None:
    """Render one epoch-seconds float back through the column's own domain rule - one scalar
    round trip per percentile, the cost of reusing one rendering path rather than two.
    """

    # Spark casts a numeric literal to TIMESTAMP only, so this routes through TIMESTAMP and lets
    # `_render_calendar_bound` narrow, making an epoch round trip render like the column itself.
    bound = _render_calendar_bound(f"CAST({epoch!r} AS TIMESTAMP)", sql_type)
    row = exec_query(cursor, f"SELECT {bound}").fetchone()

    return row[0] if row else None


def _render_calendar_bound(expr: str, sql_type: str) -> str:
    """SQL text rendering `expr` per SPEC 2.2.4's domain-rendering rule."""

    is_tz = _matches(sql_type, _TZ_TYPES)
    is_date_only = _matches(sql_type, _DATE_ONLY_TYPES)
    source_expr = _to_utc_ntz(expr) if is_tz else expr

    if is_date_only:
        return f"DATE_FORMAT({expr}, 'yyyy-MM-dd')"

    body = f"DATE_FORMAT({source_expr}, \"yyyy-MM-dd'T'HH:mm:ss.SSSSSS\")"
    body = f"REGEXP_REPLACE({body}, '\\\\.000000$', '')"

    return f"CONCAT({body}, 'Z')" if is_tz else body


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


def _utc_expr(cn: str, sql_type: str) -> str:
    return _to_utc_ntz(cn) if _matches(sql_type, _TZ_TYPES) else cn


def _to_utc_ntz(expr: str) -> str:
    """Convert a session-zone `TIMESTAMP` to the true UTC instant, then drop the zone.

    A bare `CAST(expr AS TIMESTAMP_NTZ)` reinterprets the session-zone wall clock as naive with no
    conversion, so every caller needing a UTC instant goes through `CONVERT_TIMEZONE` first.
    """

    return f"CAST(CONVERT_TIMEZONE(current_timezone(), 'UTC', {expr}) AS TIMESTAMP_NTZ)"


def _fetch_length_p95(cursor: Cursor, source: str, col: ColumnMeta) -> float | None:
    cn = _quote_ident(col.name)
    length_expr = f"LENGTH(CAST({cn} AS STRING))"
    row = exec_query(
        cursor,
        f"SELECT PERCENTILE({length_expr}, 0.95) FROM {source} WHERE {cn} IS NOT NULL",
    ).fetchone()

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
        ORDER BY cnt DESC, CAST({select_expr} AS STRING) ASC
        LIMIT ?
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


def _quote_ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _alias(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name)
