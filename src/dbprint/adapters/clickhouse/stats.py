"""Two-phase batched per-table statistics computation for ClickHouse - Phase B pre-classifies
internally and both MUST converge; native one-pass aggregates keep each phase one statement.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from functools import partial
from typing import Any, Literal

from dbprint.config import StatisticsConfig
from dbprint.spec.classification import (
    base_type,
    classify,
    compute_cardinality_ratio,
    compute_null_rate,
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
from .connection import DIALECT, Cursor, exec_query
from .introspect import estimate_row_count
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
    temporal_block_unmeasured,
)
from ..identifiers import SOURCE_ALIAS, Identity, quote, source_column
from ..sql_layout import derived, indented, select_from


APPROXIMATE_THRESHOLD = 1_000_000


_UNSUPPORTED_TYPES = (
    "array",
    "map",
    "tuple",
    "nested",
    "aggregatefunction",
    "simpleaggregatefunction",
    "dynamic",
    "point",
    "ring",
    "linestring",
    "multilinestring",
    "polygon",
    "multipolygon",
    "geometry",
    "qbit",
    "nothing",
)

# Vendor spellings profiled as text by representability (SPEC 3.1), declared so none is guessed.
_TEXT_TYPES: tuple[str, ...] = (
    "ipv4",
    "ipv6",
    "enum",
    "enum8",
    "enum16",
    "intervalnanosecond",
    "intervalmicrosecond",
    "intervalmillisecond",
    "intervalsecond",
    "intervalminute",
    "intervalhour",
    "intervalday",
    "intervalweek",
    "intervalmonth",
    "intervalquarter",
    "intervalyear",
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

    source = _source(identity, scope)
    narrows = scope is not None and scope.narrows
    # The catalog estimate describes the whole table, so a narrowed read counts instead.
    estimate = estimate_row_count(cursor, identity)
    approximate = estimate > APPROXIMATE_THRESHOLD and not narrows
    rows_scanned, phase_a = run_phase_a(
        columns,
        _phase_a_cost,
        partial(_phase_a_statement, cursor, source, approximate=approximate),
        partial(_null_counts, cursor, source),
        partial(_recount, cursor, source) if approximate else None,
        declines=lambda col: _is_unsupported(col.classified_type),
    )
    row_count, row_count_method = _table_row_count(cursor, identity, rows_scanned, scope)

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
            # A narrowed read drew nothing; an exact, exhaustive shape for columns
            # nobody read would overclaim (SPEC 2.2.7).
            return PhaseB({})

        return PhaseB({c.name: _empty_stats(c) for c in columns})

    source = _source(identity, scope)
    total = len(columns)
    position = {col.name: index for index, col in enumerate(columns, start=1)}

    def measure(col: ColumnMeta) -> ColumnStats:
        if on_column is not None:
            on_column(position[col.name], total, col.name)

        pre = _pre_classify(col, base[col.name].cardinality, config, col.name in fk_source_columns)
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

    source = _source(identity, scope)
    quoted = [source_column(col, DIALECT) for col in columns]
    cap = config.top_n_null_patterns
    rows = exec_query(
        cursor,
        f"""
        SELECT
          {indented(null_flags(quoted, concat=True), 10)} AS dbprint_nulls,
          count() AS cnt
        FROM
          {indented(source, 10)}
        GROUP BY
          dbprint_nulls
        ORDER BY
          cnt DESC, dbprint_nulls ASC
        LIMIT {cap + 1}
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
    """One batched statement testing every candidate pair (SPEC 2.2.12) - `uniqExact(tuple(a, b))`
    never the bare form, which silently drops any row where either argument is NULL (measured).
    """

    if not candidates:
        return ()

    source = _source(identity, scope)
    physical = {col.name: source_column(col, DIALECT) for col in columns}
    exprs = [
        f"uniqExact(tuple({physical[a]}, {physical[b]})) AS dbprint_grain_{i}"
        for i, (a, b) in enumerate(candidates)
    ]
    row = exec_query(cursor, select_from(exprs, source)).fetchone()

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
    """One grouped statement bucketing `column` at `unit` grain. See SPEC 2.2.16."""

    del counts

    source = _source(identity, scope)
    by_name = {col.name: col for col in columns}
    col = by_name[column]
    cn = source_column(col, DIALECT)
    date_only = temporal_shape(col.classified_type) == "date"
    bucket_expr = _timeline_bucket_expr(cn, unit, date_only=date_only)
    rendered = render_domain("bkt.bucket_start", col.classified_type)

    rows = exec_query(
        cursor,
        f"""
        SELECT
          {indented(rendered, 10)} AS bucket_text,
          bkt.cnt
        FROM
          (
            SELECT
              {bucket_expr} AS bucket_start,
              count() AS cnt
            FROM
              {indented(source, 14)}
            WHERE
              {cn} IS NOT NULL
            GROUP BY
              bucket_start
          ) bkt
        ORDER BY
          bkt.bucket_start
        """,
    ).fetchall()

    return tuple((measured_text(row[0], "timeline"), int(row[1])) for row in rows)


def _timeline_bucket_expr(cn: str, unit: str, *, date_only: bool) -> str:
    """Native truncation function for `probe_timeline`'s GROUP BY key. See SPEC 2.2.16.

    A `Date` anchor buckets as a date; widening to `DateTime` publishes a midnight it lacks.
    """

    if unit == "week":
        return f"toStartOfWeek({cn}, 1)"

    if unit == "month":
        return f"toStartOfMonth({cn})"

    return f"toDate({cn})" if date_only else f"toStartOfDay({cn})"


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

    source = _source(identity, scope)
    by_name = {col.name: col for col in columns}
    anchor = by_name[anchor_column]
    anchor_cn = source_column(anchor, DIALECT)
    exprs = []

    for subject in subject_columns:
        subject_cn = source_column(by_name[subject], DIALECT)
        from_expr = f"minIf({anchor_cn}, {subject_cn} IS NOT NULL)"
        to_expr = f"maxIf({anchor_cn}, {subject_cn} IS NOT NULL)"
        rendered_from = render_domain(from_expr, anchor.classified_type)
        rendered_to = render_domain(to_expr, anchor.classified_type)
        exprs.append(f"{rendered_from} AS from_{_alias(subject)}")
        exprs.append(f"{rendered_to} AS to_{_alias(subject)}")

    row = exec_query(cursor, select_from(exprs, source)).fetchone()

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
    """One batched statement measuring every candidate pair's joint cardinality (SPEC 2.2.13) -
    the null-safe `uniqExact(tuple(...))` form, or null-bearing rows inflate the ratio.
    """

    del counts

    if not candidates:
        return {}

    source = _source(identity, scope)
    physical = {col.name: source_column(col, DIALECT) for col in columns}
    exprs = [
        f"uniqExact(tuple({physical[a]}, {physical[b]})) AS dbprint_dep_{i}"
        for i, (a, b) in enumerate(candidates)
    ]
    row = exec_query(cursor, select_from(exprs, source)).fetchone()

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
    `ENGINE = MergeTree` keeps a large draw off RAM, and the name is never database-qualified.
    """

    name = materialized_name(identity.fqn)
    drawn = _sample_expr(identity, scope)
    exec_query(
        cursor,
        f"CREATE TEMPORARY TABLE {quote(name, DIALECT)} ENGINE = MergeTree ORDER BY tuple() "
        f"AS SELECT * FROM {drawn}",
    )

    return replace(scope, materialized=name)


def release(cursor: Cursor, scope: TableScope) -> None:
    """Drop the copied sample; `TEMPORARY` is spelled out so no base table can match."""

    if scope.materialized is None:
        return

    exec_query(cursor, f"DROP TEMPORARY TABLE IF EXISTS {quote(scope.materialized, DIALECT)}")


def _source(identity: Identity, scope: TableScope | None) -> str:
    """The FROM expression every phase reads - a `sample` scope with no materialized copy never
    reaches here, `orchestrator._materialize_scope` having refused the table first.
    """

    quoted = identity.quoted()

    if scope is None or not scope.narrows:
        return f"{quoted} {SOURCE_ALIAS}"
    elif scope.materialized is not None:
        return f"{quote(scope.materialized, DIALECT)} {SOURCE_ALIAS}"
    else:
        return derived(f"SELECT * FROM {quoted} WHERE ({scope.filter})", SOURCE_ALIAS)


def _sample_expr(identity: Identity, scope: TableScope) -> str:
    """The `SAMPLE`-bearing source `materialize_scope` reads once to build its copy."""

    quoted = identity.quoted()

    if scope.filter is not None:
        return derived(f"SELECT * FROM {quoted} WHERE ({scope.filter})", SOURCE_ALIAS)

    return derived(
        f"""
        SELECT
          *
        FROM
          {quoted} SAMPLE {scope.sample}
        """,
        SOURCE_ALIAS,
    )


def _table_row_count(
    cursor: Cursor,
    identity: Identity,
    rows_scanned: int,
    scope: TableScope | None,
) -> tuple[int, RowCountMethod]:
    """Rows in the table and how they were obtained (SPEC 2.2.1) - a narrowed read takes the
    catalog estimate, counting exactly where none exists so an empty match is not an empty table.
    """

    if scope is None or not scope.narrows:
        return rows_scanned, "exact"

    estimate = estimate_row_count(cursor, identity)

    if estimate >= 0:
        return int(estimate), "approximate"

    row = exec_query(cursor, f"SELECT count() FROM {identity.quoted()} {SOURCE_ALIAS}").fetchone()

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
    counts = [f"count({source_column(col, DIALECT)})" for col in columns]
    row = exec_query(cursor, select_from(["count()", *counts], source)).fetchone()
    rows, *non_null = (int(value) for value in row) if row else (0, *(0 for _ in columns))

    return rows, {col.name: rows - n for col, n in zip(columns, non_null, strict=True)}


def _recount(
    cursor: Cursor,
    source: str,
    columns: list[ColumnMeta],
) -> Sequence[Any] | None:
    select_parts = [
        f"uniqExact({source_column(col, DIALECT)}) AS {_alias(f'card_{col.name}')}"
        for col in columns
    ]

    return exec_query(cursor, select_from(select_parts, source)).fetchone()


def _phase_a_statement(
    cursor: Cursor,
    source: str,
    columns: list[ColumnMeta],
    approximate: bool,
) -> tuple[int, dict[str, BaseStats]]:
    """One query yielding row_count + per-column null_count + cardinality - `approximate` swaps
    in `uniqCombined64` without a second round trip, near-unique columns being re-probed after.
    """

    select_parts: list[str] = ["count() AS row_count"]
    card_fn = "uniqCombined64" if approximate else "uniqExact"

    for col in columns:
        cn = source_column(col, DIALECT)
        a = _alias(col.name)
        select_parts.append(f"countIf({cn} IS NULL) AS null_{a}")

        if is_numeric_type(col.classified_type):
            select_parts.append(f"countIf({cn} = 0) AS zero_{a}")
            select_parts.append(f"countIf({cn} < 0) AS neg_{a}")
            select_parts.append(f"countIf({cn} = trunc({cn})) AS quant_{a}")
        elif _is_string_like(col.classified_type):
            # A non-String type (UUID, Enum) has no native `= ''`/`length()`; casting first
            # is what every "string-like" type here actually shares (Postgres does the same).
            rendered = f"toString({cn})"
            select_parts.append(f"countIf({rendered} = '') AS empty_{a}")
            # `length()` is bytes on ClickHouse; `lengthUTF8()` is characters (SPEC 2.2.4).
            select_parts.append(f"min(lengthUTF8({rendered})) AS lenmin_{a}")
            select_parts.append(f"max(lengthUTF8({rendered})) AS lenmax_{a}")
            select_parts.append(f"avg(lengthUTF8({rendered})) AS lenavg_{a}")

        select_parts.append(f"{card_fn}({cn}) AS card_{a}")

    row = exec_query(cursor, select_from(select_parts, source)).fetchone()

    if row is None:
        return 0, {c.name: _empty_base(c) for c in columns}

    row_count = int(row[0])
    out: dict[str, BaseStats] = {}
    idx = 1
    method: CardinalityMethod = "approximate" if approximate else "exact"

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
        elif _is_string_like(col.classified_type):
            empty_count = int(row[idx])
            length_min, length_max, length_avg = row[idx + 1], row[idx + 2], row[idx + 3]
            idx += 4

        # uniqCombined64 errs both ways; cardinality is non-null-only (SPEC 2.2.2), so clamp.
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
        )

    return row_count, out


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
    length = None

    if base.length_min is not None and base.length_max is not None:
        length_p95 = _fetch_length_p95(cursor, source, col)
        length = (
            Length(
                min=int(base.length_min),
                max=int(base.length_max),
                avg=round_statistic(base.length_avg),
                p95=length_p95,
            )
            if length_p95 is not None
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
        except UnrepresentableValue:
            raise
        except Exception as exc:  # noqa: BLE001 - the temporal block degrades as a whole, and its
            # fields are REQUIRED here (SPEC 2.2.3); the column names what the read cost it
            # rather than leaving an absence a reader would read as a structural cause.
            return replace(
                stats,
                unmeasured=temporal_block_unmeasured(col.classified_type),
                unmeasured_cause=exc,
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
    # `toString` stays the ordering key, since it decides the cutoff; a number publishes natively.
    native = is_numeric_type(col.classified_type)
    rows = exec_query(
        cursor,
        f"""
        SELECT
          {render_text(cn, col.classified_type)} AS rendered,
          count() AS cnt,
          any({cn}) AS native
        FROM
          {indented(source, 10)}
        WHERE
          {cn} IS NOT NULL
        GROUP BY
          {cn}
        ORDER BY
          cnt DESC, rendered ASC
        LIMIT {limit + 1}
        """,
    ).fetchall()
    exhaustive = len(rows) <= limit
    kept = rows if exhaustive else rows[: config.top_n_values]
    entries = order_values(
        (
            ValueCount(
                value=measured_value(value if native else text, f"values[{i}]"),
                count=int(cnt),
            )
            for i, (text, cnt, value) in enumerate(kept)
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
    keys = config.percentiles
    select_parts = [
        f"min({cn})",
        f"max({cn})",
        f"avg({cn})",
        f"sum({cn})",
        *_percentile_exprs(cn, keys),
    ]
    row = exec_query(cursor, select_from(select_parts, source)).fetchone()

    if row is None:
        return Range(min=None, max=None), {}, "uniform", summarize_frequencies([]), (), None, None

    rng = Range(
        min=round_statistic(row[0], exact_int=True),
        max=round_statistic(row[1], exact_int=True),
    )
    mean = round_statistic(row[2])
    total = round_statistic(row[3], exact_int=True)
    percentile_values = row[4 : 4 + len(keys)]
    percentiles = coherent_percentiles(
        {f"p{p:02d}": round_statistic(v) for p, v in zip(keys, percentile_values, strict=True)},
        rng.min,
        rng.max,
    )
    distribution, frequencies, values = _approximate_distribution_via_top_n(
        cursor,
        source,
        cn,
        non_null,
        config,
        measured_value,
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
    """Bounds and percentiles render to text in SQL; `quantized_count` is omitted for a `Date`,
    already its own day, and a `Time`, which has none.
    """

    cn = source_column(col, DIALECT)
    keys = config.percentiles
    clock = temporal_shape(col.classified_type) in ("time", "time_tz")
    day_aligned = temporal_shape(col.classified_type) not in ("date", "time", "time_tz")
    # Nearest rank over the column's own sorted values, so a percentile keeps the type and zone
    # the bounds render from; `quantileExactLow` ranks floor(p * n) + 1, and a float rank overshoots.
    ranked = [
        f"""
        if(
          empty(arraySort(groupArray({cn})) AS dbprint_sorted),
          NULL,
          dbprint_sorted[greatest(intDiv({p} * length(dbprint_sorted) + 99, 100), 1)]
        )
        """
        for p in keys
    ]
    percentile_exprs = [render_domain(expr, col.classified_type) for expr in ranked]
    select_parts = [
        render_domain(f"min({cn})", col.classified_type),
        render_domain(f"max({cn})", col.classified_type),
        # Elapsed whole days at microsecond resolution: `dateDiff` counts unit boundaries, so a
        # coarser unit would count a crossed boundary as a whole one (SPEC 2.2.4).
        "0" if clock else f"intDiv(dateDiff('microsecond', min({cn}), max({cn})), 86400000000)",
        *percentile_exprs,
    ]

    if day_aligned:
        select_parts.append(
            f"countIf(toStartOfDay(toDateTime({cn})) = toDateTime({cn})) AS quant",
        )

    row = exec_query(cursor, select_from(select_parts, source)).fetchone()

    if row is None:
        empty_range = Range(min=None, max=None, span_days=0)

        return empty_range, {}, "uniform", (), summarize_frequencies([]), (), None

    span_days = int(row[2]) if row[2] is not None else 0
    rng = Range(
        min=measured_value(row[0], "range.min"),
        max=measured_value(row[1], "range.max"),
        span_days=span_days,
    )
    percentile_values = row[3 : 3 + len(keys)]
    percentiles = {
        f"p{p:02d}": measured_value(v, f"percentiles.p{p:02d}")
        for p, v in zip(keys, percentile_values, strict=True)
    }
    quantized_count = int(row[3 + len(keys)]) if day_aligned else None

    distribution, frequencies, values = _approximate_distribution_via_top_n(
        cursor,
        source,
        render_domain(cn, col.classified_type),
        non_null,
        config,
        lambda v: v,
        group_expr=None if stores_below_microsecond(col.classified_type) else cn,
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


def _percentile_exprs(cn: str, keys: Sequence[int]) -> list[str]:
    """Scalar `quantileExactInclusive(p)(col)` calls, one per requested percentile -
    the array form round-trips as a repr, and plain `quantileExact` is nearest-rank instead.
    """

    return [f"quantileExactInclusive({p / 100.0})(toFloat64({cn}))" for p in keys]


def _fetch_length_p95(cursor: Cursor, source: str, col: ColumnMeta) -> float | None:
    """P95 character length (SPEC 2.2.4), via `quantileExactInclusive` over `lengthUTF8`."""

    cn = source_column(col, DIALECT)
    row = exec_query(
        cursor,
        f"""
        SELECT
          quantileExactInclusive(0.95)(lengthUTF8(toString({cn})))
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
    non_null: int,
    config: StatisticsConfig,
    value_transform: Any,
    group_expr: str | None = None,
) -> tuple[Distribution, Frequencies, tuple[ValueCount, ...]]:
    """Distribution, frequencies, and the same top-N rows `values` publishes (SPEC 2.2.3)."""

    limit = enumeration_limit(config.enumeration_threshold, config.top_n_values)
    grouping = group_expr or select_expr
    rows = exec_query(
        cursor,
        f"""
        SELECT
          {indented(select_expr, 10)} AS rendered,
          count() AS cnt
        FROM
          {indented(source, 10)}
        WHERE
          {indented(grouping, 10)} IS NOT NULL
        GROUP BY
          {indented(grouping, 10)}
        ORDER BY
          cnt DESC, rendered ASC
        LIMIT {limit + 1}
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
    return base_type(sql_type) in _UNSUPPORTED_TYPES


def _matches(sql_type: str, types: tuple[str, ...]) -> bool:
    return base_type(sql_type) in types


def _is_string_like(sql_type: str) -> bool:
    return not _is_unsupported(sql_type) and is_string_like_type(sql_type)


def _alias(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name)
