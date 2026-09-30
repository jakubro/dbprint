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
    classify,
    compute_cardinality_ratio,
    compute_null_rate,
    is_array_type,
    is_numeric_type,
    is_string_like_type,
)
from dbprint.spec.coverage import coverage_share, enumeration_limit
from dbprint.spec.distribution import classify as classify_distribution
from dbprint.spec.distribution import summarize as summarize_frequencies
from dbprint.spec.percentiles import coherent_percentiles
from dbprint.spec.rounding import (
    UnrepresentableValue,
    measured_text,
    measured_value,
    round_statistic,
)
from dbprint.spec.temporal_range import is_representable
from . import introspect
from .connection import DIALECT, Cursor, exec_query
from .rendering import render_domain, render_text, stores_below_microsecond, temporal_shape
from ..base import (
    BaseStats,
    CardinalityMethod,
    ColumnMeta,
    ColumnProgress,
    ColumnStats,
    Distribution,
    Frequencies,
    Length,
    NullPatterns,
    PhaseA,
    PhaseB,
    Range,
    RowCountMethod,
    TableCounts,
    TableScope,
    ValueCount,
    has_measurable_nulls,
    materialized_name,
    null_flags,
    null_patterns_from_rows,
    order_values,
    run_phase_a,
    run_phase_b,
    seed_from_fqn,
    temporal_block_unmeasured,
)
from ..identifiers import SOURCE_ALIAS, Identity, qualified, quote, source_column
from ..sql_layout import derived, indented, listed, select_from


# Threshold above which cardinality is estimated rather than counted. SPEC 2.2.2.
APPROXIMATE_THRESHOLD = 1_000_000

# duckdb's REPEATABLE seed carries no documented narrower range than a signed 32-bit int.
SEED_MODULUS = 2**31


_UNSUPPORTED_TYPES = (
    "bytea",
    "blob",
    "binary",
    "varbinary",
    "image",
    "record",
    "struct",
    "array",
    "list",
    "map",
    "geography",
    "geometry",
    "vector",
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

    source = _table_source(identity, scope)
    narrows = scope is not None and scope.narrows

    estimate = introspect.row_count_estimate(cursor, identity)
    # The catalog estimate describes the table, not the slice, so a narrowed read counts exactly.
    approximate = estimate > APPROXIMATE_THRESHOLD and not narrows

    rows_scanned, phase_a = run_phase_a(
        columns,
        _phase_a_cost,
        partial(_phase_a_statement, cursor, source, approximate=approximate),
        partial(_null_counts, cursor, source),
        partial(_recount, cursor, source) if approximate else None,
        declines=lambda col: _is_unsupported(col.classified_type),
    )
    row_count, row_count_method = _table_row_count(
        cursor,
        identity.quoted(),
        rows_scanned,
        estimate,
        scope,
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
) -> PhaseB:
    """Phase B: the classification-specific statistics, keyed by column name."""

    if not columns:
        return PhaseB({})

    if counts.rows_scanned == 0:
        if scope is not None and scope.narrows:
            # A narrowed read drew nothing; publishing an exhaustive shape for columns
            # nobody read would overclaim (SPEC 2.2.7).
            return PhaseB({})

        return PhaseB({c.name: _empty_stats(c) for c in columns})

    source = _table_source(identity, scope)
    total = len(columns)
    position = {col.name: index for index, col in enumerate(columns, start=1)}

    def measure(col: ColumnMeta) -> ColumnStats:
        if on_column is not None:
            on_column(position[col.name], total, col.name)

        pre = _pre_classify(
            col,
            base[col.name].cardinality,
            config,
            col.name in fk_source_columns,
        )
        return _phase_b(
            cursor,
            source,
            col,
            base[col.name],
            counts.rows_scanned,
            pre,
            config,
            suppressed=col.name in suppress_values,
        )

    return run_phase_b(columns, measure)


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
    quoted = [source_column(col, DIALECT) for col in columns]
    cap = config.top_n_null_patterns
    rows = exec_query(
        cursor,
        f"""
        SELECT
          {indented(null_flags(quoted, concat=False), 10)} AS dbprint_nulls,
          COUNT(1) AS cnt
        FROM
          {indented(source, 10)}
        GROUP BY
          1
        ORDER BY
          cnt DESC, dbprint_nulls ASC
        LIMIT {int(cap) + 1}
        """,
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
    """One batched statement testing every candidate pair (SPEC 2.2.12) - `COUNT(DISTINCT (a, b))`
    needs the extra parens, a bare list reading as two positional arguments.
    """

    if not candidates:
        return ()

    source = _table_source(identity, scope)
    exprs = [
        f"COUNT(DISTINCT ({_qualified(a)}, {_qualified(b)})) AS dbprint_grain_{i}"
        for i, (a, b) in enumerate(candidates)
    ]
    rows = exec_query(cursor, select_from(exprs, source)).fetchall()

    if not rows:
        return ()

    row = rows[0]

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
    """One grouped statement bucketing `column` at `unit` grain (SPEC 2.2.16) - grouping is on
    the truncated value in a derived table, so ordering sorts the value, not its text.
    """

    del counts

    source = _table_source(identity, scope)
    by_name = {col.name: col for col in columns}
    col = by_name[column]
    cn = source_column(col, DIALECT)
    bucket_expr = f"DATE_TRUNC('{unit}', {cn})"

    rows = exec_query(
        cursor,
        f"""
        SELECT
          {indented(render_domain("bkt.bucket_start", col.classified_type), 10)} AS bucket_text,
          bkt.cnt
        FROM
          (
            SELECT
              {bucket_expr} AS bucket_start,
              COUNT(1) AS cnt
            FROM
              {indented(source, 14)}
            WHERE
              {cn} IS NOT NULL
            GROUP BY
              1
          ) bkt
        ORDER BY
          bkt.bucket_start
        """,
    ).fetchall()

    return tuple((measured_text(row[0], "timeline"), int(row[1])) for row in rows)


def compute_populated_windows(
    cursor: Cursor,
    identity: Identity,
    columns: list[ColumnMeta],
    counts: TableCounts,
    anchor_column: str,
    subject_columns: tuple[str, ...],
    scope: TableScope | None = None,
) -> dict[str, tuple[str, str]]:
    """One statement, two conditional aggregates per subject column (SPEC 2.2.4) - aggregated in
    a derived table so the outer query renders each bound through the anchor's domain rule.
    """

    del counts

    if not subject_columns:
        return {}

    source = _table_source(identity, scope)
    by_name = {col.name: col for col in columns}
    anchor = by_name[anchor_column]
    anchor_cn = source_column(anchor, DIALECT)

    agg_exprs = []
    outer_exprs = []

    for i, subject in enumerate(subject_columns):
        subject_cn = source_column(by_name[subject], DIALECT)
        agg_exprs.append(
            f"MIN(CASE WHEN {subject_cn} IS NOT NULL THEN {anchor_cn} END) AS from_{i}",
        )
        agg_exprs.append(
            f"MAX(CASE WHEN {subject_cn} IS NOT NULL THEN {anchor_cn} END) AS to_{i}",
        )
        outer_exprs.append(
            f"{render_domain(f'agg.from_{i}', anchor.classified_type)} AS from_{i}_text",
        )
        outer_exprs.append(
            f"{render_domain(f'agg.to_{i}', anchor.classified_type)} AS to_{i}_text",
        )

    row = exec_query(
        cursor,
        f"""
        SELECT
          {listed(outer_exprs, 10)}
        FROM
          (
            SELECT
              {listed(agg_exprs, 14)}
            FROM
              {indented(source, 14)}
          ) agg
        """,
    ).fetchone()

    if row is None:
        return {}

    windows: dict[str, tuple[str, str]] = {}

    for i, subject in enumerate(subject_columns):
        from_text, to_text = row[2 * i], row[2 * i + 1]

        if from_text is not None and to_text is not None:
            windows[subject] = (
                measured_text(from_text, "populated.from"),
                measured_text(to_text, "populated.to"),
            )

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

    del columns, counts

    if not candidates:
        return {}

    source = _table_source(identity, scope)
    exprs = [
        f"COUNT(DISTINCT ({_qualified(a)}, {_qualified(b)})) AS dbprint_dep_{i}"
        for i, (a, b) in enumerate(candidates)
    ]
    rows = exec_query(cursor, select_from(exprs, source)).fetchall()

    if not rows:
        return {}

    row = rows[0]
    out: dict[tuple[str, str], float] = {}

    for i, (a, b) in enumerate(candidates):
        joint = row[i]

        if joint:
            out[(a, b)] = min(1.0, base[a].cardinality / joint)

    return out


def materialize(cursor: Cursor, identity: Identity, scope: TableScope) -> TableScope:
    """Copy the drawn fraction into a session-lifetime table and name it on the scope - duckdb
    allows a temp table only in its own `temp` catalog, so later reads address it bare.
    """

    name = materialized_name(identity.fqn)
    seed = seed_from_fqn(identity.fqn, SEED_MODULUS)
    drawn = _source(identity.quoted(), scope, seed)
    exec_query(cursor, f"CREATE TEMPORARY TABLE {quote(name, DIALECT)} AS SELECT * FROM {drawn}")

    return replace(scope, materialized=name)


def release(cursor: Cursor, scope: TableScope) -> None:
    """Drop the copied sample; the session would drop it anyway, this frees it sooner."""

    if scope.materialized is None:
        return

    exec_query(cursor, f"DROP TABLE IF EXISTS {quote(scope.materialized, DIALECT)}")


def _table_source(identity: Identity, scope: TableScope | None) -> str:
    """The FROM expression every phase reads - rebuilt per phase, not threaded, the seed being
    re-derived from the table's name so every call produces the same text and the same rows.
    """

    return _source(identity.quoted(), scope, seed_from_fqn(identity.fqn, SEED_MODULUS))


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


def _table_row_count(
    cursor: Cursor,
    quoted: str,
    rows_scanned: int,
    estimate: int,
    scope: TableScope | None,
) -> tuple[int, RowCountMethod]:
    """Rows in the table and how they were obtained (SPEC 2.2.1) - a narrowed read takes the
    catalog estimate, counting exactly where none exists so an empty match is not an empty table.
    """

    if scope is None or not scope.narrows:
        return rows_scanned, "exact"

    if estimate >= 0:
        return estimate, "approximate"

    row = exec_query(cursor, f"SELECT COUNT(1) FROM {quoted} {SOURCE_ALIAS}").fetchone()

    return (int(row[0]) if row and row[0] is not None else rows_scanned), "exact"


def _phase_a_cost(column: ColumnMeta) -> int:
    if is_numeric_type(column.classified_type):
        return 5

    if _is_string_like(column.classified_type):
        return 7

    return 2


def _null_counts(
    cursor: Cursor,
    source: str,
    columns: list[ColumnMeta],
) -> tuple[int, dict[str, int]]:
    counts = [f"COUNT({source_column(col, DIALECT)})" for col in columns]
    row = exec_query(cursor, select_from(["COUNT(1)", *counts], source)).fetchone()
    rows, *non_null = (int(value) for value in row) if row else (0, *(0 for _ in columns))

    return rows, {col.name: rows - n for col, n in zip(columns, non_null, strict=True)}


def _recount(
    cursor: Cursor,
    source: str,
    columns: list[ColumnMeta],
) -> Sequence[Any] | None:
    select_parts = [
        f"COUNT(DISTINCT {source_column(col, DIALECT)}) AS card_{_alias(col.name)}"
        for col in columns
    ]

    return exec_query(cursor, select_from(select_parts, source)).fetchone()


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
        select_parts.append(f"COALESCE(COUNT_IF({cn} IS NULL), 0) AS null_{_alias(col.name)}")

        if is_numeric_type(col.classified_type):
            select_parts.append(f"COALESCE(COUNT_IF({cn} = 0), 0) AS zero_{_alias(col.name)}")
            select_parts.append(f"COALESCE(COUNT_IF({cn} < 0), 0) AS neg_{_alias(col.name)}")
            select_parts.append(
                f"COALESCE(COUNT_IF({cn} = TRUNC({cn})), 0) AS quant_{_alias(col.name)}",
            )
        elif _is_string_like(col.classified_type):
            select_parts.append(
                f"COALESCE(COUNT_IF(CAST({cn} AS VARCHAR) = ''), 0) AS empty_{_alias(col.name)}",
            )
            length_expr = f"LENGTH(CAST({cn} AS VARCHAR))"
            select_parts.append(f"MIN({length_expr}) AS lenmin_{_alias(col.name)}")
            select_parts.append(f"MAX({length_expr}) AS lenmax_{_alias(col.name)}")
            select_parts.append(f"AVG({length_expr}) AS lenavg_{_alias(col.name)}")
            select_parts.append(
                f"PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY {length_expr}) "
                f"AS lenp95_{_alias(col.name)}",
            )

        select_parts.append(f"{_distinct_expr(cn, approximate)} AS card_{_alias(col.name)}")

    row = exec_query(cursor, select_from(select_parts, source)).fetchone()

    if row is None:
        return 0, {c.name: _empty_base(c) for c in columns}

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
        elif _is_string_like(col.classified_type):
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


def _empty_stats(col: ColumnMeta) -> ColumnStats:
    """SPEC 2.2.7 edge case: a table read in full and found empty -> minimal column stats."""

    if _is_unsupported(col.classified_type):
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
    """Build the final ColumnStats for one column based on the pre-classification."""

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
    method: CardinalityMethod = base.cardinality_method
    non_null = rows_scanned - null_count
    length_min, length_max = base.length_min, base.length_max
    length = (
        Length(
            min=length_min,
            max=length_max,
            avg=round_statistic(base.length_avg),
            p95=round_statistic(base.length_p95),
        )
        if length_min is not None and length_max is not None
        else None
    )

    stats = ColumnStats(
        sql_type=col.sql_type,
        nullable=col.nullable,
        null_count=null_count,
        null_rate=null_rate,
        cardinality=cardinality,
        cardinality_ratio=cardinality_ratio,
        cardinality_method=method,
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

        return _replace(stats, values=values, values_coverage=coverage)

    if pre in ("categorical", "foreign_key_candidate"):
        values, coverage, exhaustive = _fetch_value_list(cursor, source, col, non_null, config)
        distribution = classify_distribution(
            [v.count for v in values],
            non_null,
            exhaustive=exhaustive,
        )

        return _replace(stats, values=values, values_coverage=coverage, distribution=distribution)

    if pre == "numeric":
        rng, percentiles, distribution, frequencies, values, mean, total = _fetch_numeric_block(
            cursor,
            source,
            col,
            non_null,
            config,
        )

        return _replace(
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
        except UnrepresentableValue:
            raise
        except Exception as exc:  # noqa: BLE001 - the temporal block degrades as a whole, and its
            # fields are REQUIRED here (SPEC 2.2.3); the column names what the read cost it
            # rather than leaving an absence a reader would read as a structural cause.
            return _replace(
                stats,
                unmeasured=temporal_block_unmeasured(col.classified_type),
                unmeasured_cause=exc,
            )

        return _replace(
            stats,
            range=rng,
            percentiles=percentiles,
            distribution=distribution,
            frequencies=frequencies,
            unrepresentable=unrepresentable or None,
            values=values,
            quantized_count=quantized,
        )

    # pre == "text": the only suppressible classification; `distribution` goes with the list.
    if suppressed:
        return stats

    values, coverage, exhaustive = _fetch_value_list(cursor, source, col, non_null, config)
    distribution = classify_distribution([v.count for v in values], non_null, exhaustive=exhaustive)

    return _replace(stats, values=values, values_coverage=coverage, distribution=distribution)


def _pre_classify(
    col: ColumnMeta,
    cardinality: int,
    config: StatisticsConfig,
    has_declared_fk: bool,
) -> str:
    if _is_unsupported(col.classified_type):
        return "unsupported"

    return classify(
        col.classified_type,
        cardinality,
        has_declared_fk,
        config.enumeration_threshold,
    )


def _fetch_value_list(
    cursor: Cursor,
    source: str,
    col: ColumnMeta,
    non_null: int,
    config: StatisticsConfig,
) -> tuple[tuple[ValueCount, ...], float, bool]:
    """Ordered value list, its coverage, and whether it enumerates the column - one row beyond
    the bound is fetched, so truncation is observed rather than predicted (SPEC 2.2.4).
    """

    cn = source_column(col, DIALECT)
    limit = enumeration_limit(config.enumeration_threshold, config.top_n_values)
    select_expr, group_expr = _value_select_and_group(cn, col.classified_type)
    rows = exec_query(
        cursor,
        f"""
        SELECT
          {indented(select_expr, 10)} AS rendered,
          COUNT(1) AS cnt
        FROM
          {indented(source, 10)}
        WHERE
          {cn} IS NOT NULL
        GROUP BY
          {indented(group_expr, 10)}
        ORDER BY
          cnt DESC, CAST({indented(select_expr, 10)} AS VARCHAR) ASC
        LIMIT {int(limit) + 1}
        """,
    ).fetchall()
    exhaustive = len(rows) <= limit
    kept = rows if exhaustive else rows[: config.top_n_values]
    # SPEC 2.2.4: the cast fixes which tied entries survive the cutoff.
    entries = order_values(
        (
            ValueCount(value=measured_value(value, f"values[{i}]"), count=int(cnt))
            for i, (value, cnt) in enumerate(kept)
        ),
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
    cn = source_column(col, DIALECT)
    percentile_keys = config.percentiles
    pct_select = [
        f"PERCENTILE_CONT({p / 100.0}) WITHIN GROUP (ORDER BY CAST({cn} AS DOUBLE)) AS p_{p:02d}"
        for p in percentile_keys
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

    if row is None:
        empty_range = Range(min=None, max=None)

        return empty_range, {}, "uniform", summarize_frequencies([]), (), None, None

    exact = _exact_integer if _matches(col.classified_type, _DECIMAL_STRING_TYPES) else _as_is
    rng = Range(
        min=round_statistic(exact(row[0]), exact_int=True),
        max=round_statistic(exact(row[1]), exact_int=True),
    )
    mean = round_statistic(row[2])
    total = round_statistic(exact(row[3]), exact_int=True)
    percentiles = coherent_percentiles(
        {f"p{p:02d}": round_statistic(v) for p, v in zip(percentile_keys, row[4:], strict=True)},
        rng.min,
        rng.max,
    )
    distribution, frequencies, values = _approximate_distribution_via_top_n(
        cursor,
        source,
        cn,
        cn,
        non_null,
        config,
        lambda v: measured_value(exact(v)),
    )

    return rng, percentiles, distribution, frequencies, values, mean, total


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
    unrepresentable = _unrepresentable_fields(rng, percentiles)

    return rng, percentiles, distribution, unrepresentable, frequencies, values, quantized_count


def _unrepresentable_fields(rng: Range, percentiles: dict[str, Any]) -> tuple[str, ...]:
    """Field names per SPEC 2.2.4 whose rendered text names a year outside 0001-9999."""

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


def _approximate_distribution_via_top_n(
    cursor: Cursor,
    source: str,
    select_expr: str,
    group_expr: str,
    non_null: int,
    config: StatisticsConfig,
    value_transform: Callable[[Any], Any],
) -> tuple[Distribution, Frequencies, tuple[ValueCount, ...]]:
    """Distribution, frequencies, and the same top-N rows `values` publishes (SPEC 2.2.3) -
    `group_expr` is the raw column unless the rendering is coarser than what is stored.
    """

    limit = enumeration_limit(config.enumeration_threshold, config.top_n_values)
    rows = exec_query(
        cursor,
        f"""
        SELECT
          {indented(select_expr, 10)} AS rendered,
          COUNT(1) AS cnt
        FROM
          {indented(source, 10)}
        WHERE
          {indented(group_expr, 10)} IS NOT NULL
        GROUP BY
          {indented(group_expr, 10)}
        ORDER BY
          cnt DESC, CAST({indented(select_expr, 10)} AS VARCHAR) ASC
        LIMIT {int(limit) + 1}
        """,
    ).fetchall()
    exhaustive = len(rows) <= limit
    kept = rows if exhaustive else rows[: config.top_n_values]
    entries = order_values(
        (ValueCount(value=value_transform(value), count=int(cnt)) for value, cnt in kept),
    )
    values = tuple(entries)
    kept_counts = [v.count for v in values]

    return (
        classify_distribution(kept_counts, non_null, exhaustive=exhaustive),
        summarize_frequencies(kept_counts),
        values,
    )


def _empty_base(col: ColumnMeta) -> BaseStats:
    """Phase A's answer for a table whose batched query yielded no row."""

    return BaseStats(
        null_count=0,
        cardinality=0,
        cardinality_method="exact",
        supported=not _is_unsupported(col.classified_type),
    )


def _is_unsupported(sql_type: str) -> bool:
    base = base_type(sql_type)

    return base in _UNSUPPORTED_TYPES or is_array_type(sql_type)


def _value_select_and_group(cn: str, sql_type: str) -> tuple[str, str]:
    if temporal_shape(sql_type) == "timestamp_tz":
        return render_domain(cn, sql_type), cn

    if _is_string_like(sql_type):
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


def _is_string_like(sql_type: str) -> bool:
    return not _is_unsupported(sql_type) and is_string_like_type(sql_type)


def _qualified(name: str) -> str:
    return qualified(quote(name, DIALECT))


def _ranked_percentiles(keys: Sequence[int]) -> list[str]:
    return [
        f"MIN(CASE WHEN rnk.dbprint_rn >= CEIL({p / 100.0} * rnk.dbprint_n) "
        f"THEN {_RANKED_VALUE} END) AS p_{p:02d}"
        for p in keys
    ]


def _alias(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name)


def _replace(stats: ColumnStats, **kwargs: Any) -> ColumnStats:

    return replace(stats, **kwargs)
