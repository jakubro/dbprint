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
    classify,
    compute_cardinality_ratio,
    compute_null_rate,
    is_boolean_type,
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
from .introspect import table_rows_estimate
from .rendering import render_domain, render_text, temporal_shape
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
from ..identifiers import SOURCE_ALIAS, Identity, qualified, quote
from ..sql_layout import derived, indented, listed, select_from


# Reduction range for the sampling seed - a safe width, not a documented MySQL limit.
SEED_MODULUS = 2**31


_UNSUPPORTED_TYPES = (
    "blob",
    "tinyblob",
    "mediumblob",
    "longblob",
    "binary",
    "varbinary",
    "geometry",
    "point",
    "linestring",
    "polygon",
    "geometrycollection",
    "multipoint",
    "multilinestring",
    "multipolygon",
    "vector",
)

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
) -> tuple[TableCounts, PhaseA]:
    """Phase A: the table's counts plus per-column null_count and cardinality."""

    if not columns:
        return TableCounts(row_count=0, rows_scanned=0), PhaseA({})

    source = _table_source(identity, scope)
    rows_scanned, phase_a = run_phase_a(
        columns,
        _phase_a_cost,
        partial(_phase_a_statement, cursor, source),
        partial(_null_counts, cursor, source),
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
    quoted = [_qualified(col.name) for col in columns]
    cap = config.top_n_null_patterns
    rows = exec_query(
        cursor,
        f"""
        SELECT
          {indented(null_flags(quoted, concat=True), 10)} AS dbprint_nulls,
          COUNT(1) AS cnt
        FROM
          {indented(source, 10)}
        GROUP BY
          1
        ORDER BY
          cnt DESC, dbprint_nulls ASC
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
    """One batched statement testing every candidate pair. See SPEC 2.2.12.

    MySQL spells multi-column DISTINCT as a plain argument list, no row-constructor parens;
    one expression per candidate, aliased by position so the row maps back to `candidates`.
    """

    if not candidates:
        return ()

    source = _table_source(identity, scope)
    exprs = [
        f"COUNT(DISTINCT {_qualified(a)}, {_qualified(b)}) AS dbprint_grain_{i}"
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

    source = _table_source(identity, scope)
    by_name = {col.name: col for col in columns}
    col = by_name[column]
    cn = _qualified(col.name)
    bucket_expr = _timeline_bucket_expr(cn, col.classified_type, unit)
    bucket_text = render_domain("bkt.bucket_start", col.classified_type, already_utc=True)

    rows = exec_query(
        cursor,
        f"""
        SELECT
          {indented(bucket_text, 10)} AS bucket_text,
          bkt.cnt
        FROM
          (
            SELECT
              {indented(bucket_expr, 14)} AS bucket_start,
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
    """One statement, two conditional aggregates per subject column (SPEC 2.2.4) - aggregated in
    a derived table so the outer query renders each bound through the anchor's domain rule.
    """

    del counts

    if not subject_columns:
        return {}

    source = _table_source(identity, scope)
    by_name = {col.name: col for col in columns}
    anchor = by_name[anchor_column]
    anchor_cn = _qualified(anchor.name)

    agg_exprs = []
    outer_exprs = []

    for i, subject in enumerate(subject_columns):
        subject_cn = _qualified(by_name[subject].name)
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
    """One batched statement measuring every candidate pair's joint cardinality. See SPEC 2.2.13.

    Phase A already measured `base[determinant].cardinality`, so only the joint
    `COUNT(DISTINCT a, b)` needs a fresh statement here.
    """

    del columns, counts

    if not candidates:
        return {}

    source = _table_source(identity, scope)
    exprs = [
        f"COUNT(DISTINCT {_qualified(a)}, {_qualified(b)}) AS dbprint_dep_{i}"
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
    """Copy the drawn fraction into a session-lifetime temp table and name it on the scope.

    `RAND(seed)` reproduces a row set only within a single reference, so this is the only way
    MySQL reads one draw. A temp table may not be named twice in one statement.
    """

    drawn = _source(identity.quoted(), scope, _seed(identity))
    # Qualified by the table's own database: a session with none set has nowhere else to put it.
    copy = identity.sibling(materialized_name(identity.fqn))
    exec_query(cursor, f"CREATE TEMPORARY TABLE {copy} AS SELECT * FROM {drawn}")

    return replace(scope, materialized=copy)


def release(cursor: Cursor, scope: TableScope) -> None:
    """Drop the copied sample; `TEMPORARY` is spelled out so no base table can match."""

    if scope.materialized is None:
        return

    exec_query(cursor, f"DROP TEMPORARY TABLE IF EXISTS {scope.materialized}")


def _table_source(identity: Identity, scope: TableScope | None) -> str:
    """The FROM expression every phase reads.

    A materialized scope names one copied draw; unmaterialized, the seed re-derives from
    the table's own name, so every phase builds the same text.
    """

    return _source(identity.quoted(), scope, _seed(identity))


def _seed(identity: Identity) -> int:
    """The table's draw seed, hashed from the FOLDED path - the artifact's own name for it."""

    return seed_from_fqn(identity.fqn, SEED_MODULUS)


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


def _table_row_count(
    cursor: Cursor,
    identity: Identity,
    rows_scanned: int,
    scope: TableScope | None,
) -> tuple[int, RowCountMethod]:
    """Rows in the table and how they were obtained, per SPEC 2.2.1.

    A narrowed read takes the catalog estimate; with none it counts exactly, since the
    scanned figure would report a filter matching nothing as an empty table (SPEC 2.2.7).
    InnoDB's sampled `table_rows` lags, so an estimate under the scan still stands (SPEC 2.2.8).
    """

    if scope is None or not scope.narrows:
        return rows_scanned, "exact"

    estimate = table_rows_estimate(cursor, identity)

    if estimate >= 0:
        return estimate, "approximate"

    row = exec_query(cursor, f"SELECT COUNT(1) FROM {identity.quoted()} {SOURCE_ALIAS}").fetchone()

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
    counts = [f"COUNT({_qualified(col.name)})" for col in columns]
    row = exec_query(cursor, select_from(["COUNT(1)", *counts], source)).fetchone()
    rows, *non_null = (int(value) for value in row) if row else (0, *(0 for _ in columns))

    return rows, {col.name: rows - n for col, n in zip(columns, non_null, strict=True)}


def _phase_a_statement(
    cursor: Cursor,
    source: str,
    columns: list[ColumnMeta],
) -> tuple[int, dict[str, BaseStats]]:
    """One query yielding row_count + per-column null_count + cardinality."""

    select_parts: list[str] = ["COUNT(1) AS row_count"]

    for col in columns:
        cn = _qualified(col.name)
        select_parts.append(f"COUNT(1) - COUNT({cn}) AS null_{_alias(col.name)}")

        if is_numeric_type(col.classified_type) and not is_boolean_type(col.classified_type):
            select_parts.append(f"COALESCE(SUM({cn} = 0), 0) AS zero_{_alias(col.name)}")
            select_parts.append(f"COALESCE(SUM({cn} < 0), 0) AS neg_{_alias(col.name)}")
            select_parts.append(
                f"COALESCE(SUM({cn} = TRUNCATE({cn}, 0)), 0) AS quant_{_alias(col.name)}",
            )
        elif _is_string_like(col.classified_type):
            select_parts.append(
                f"COALESCE(SUM(CAST({cn} AS CHAR) = ''), 0) AS empty_{_alias(col.name)}",
            )
            length_expr = f"CHAR_LENGTH(CAST({cn} AS CHAR))"
            select_parts.append(f"MIN({length_expr}) AS lenmin_{_alias(col.name)}")
            select_parts.append(f"MAX({length_expr}) AS lenmax_{_alias(col.name)}")
            select_parts.append(f"AVG({length_expr}) AS lenavg_{_alias(col.name)}")

        select_parts.append(f"COUNT(DISTINCT {cn}) AS card_{_alias(col.name)}")

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
        length_min = length_max = length_avg = None

        if is_numeric_type(col.classified_type) and not is_boolean_type(col.classified_type):
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


def _empty_stats(col: ColumnMeta) -> ColumnStats:
    """SPEC 2.2.7 edge case: a table read in full and found empty -> minimal column stats.

    A narrowed read that drew nothing is a different condition and never reaches here.
    """

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
    method: CardinalityMethod = "exact"
    non_null = rows_scanned - null_count
    length_min, length_max = base.length_min, base.length_max

    if length_min is not None and length_max is not None:
        length_p95 = _fetch_length_p95(cursor, source, col)
        length = (
            Length(
                min=length_min,
                max=length_max,
                avg=round_statistic(base.length_avg),
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

    if pre == "categorical":
        values, coverage, exhaustive = _fetch_value_list(cursor, source, col, non_null, config)
        distribution = classify_distribution(
            [v.count for v in values],
            non_null,
            exhaustive=exhaustive,
        )

        return _replace(
            stats,
            values=values,
            values_coverage=coverage,
            distribution=distribution,
        )

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

    # pre in {"text", "foreign_key_candidate"}: only `text` is suppressible.
    if suppressed and pre == "text":
        return stats

    values, coverage, exhaustive = _fetch_value_list(cursor, source, col, non_null, config)
    distribution = classify_distribution([v.count for v in values], non_null, exhaustive=exhaustive)

    return _replace(
        stats,
        values=values,
        values_coverage=coverage,
        distribution=distribution,
    )


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
    """Ordered value list, its coverage, and whether it enumerates the column.

    Fetches one row beyond the bound so truncation is observed, not predicted from a cardinality
    that may be an estimate (SPEC 2.2.4). A TIMESTAMP routes through the same UTC-pinning
    renderer as `range`. The limit is interpolated, not bound: `DATE_FORMAT`'s literal `%`
    sequences would collide with the connector's `%s` substitution.
    """

    cn = _qualified(col.name)
    limit = enumeration_limit(config.enumeration_threshold, config.top_n_values)
    select_expr = cn

    if temporal_shape(col.classified_type) == "timestamp_tz":
        select_expr = render_domain(cn, col.classified_type)
    elif _is_string_like(col.classified_type):
        select_expr = render_text(cn, col.classified_type)

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
          {cn}
        ORDER BY
          cnt DESC, CAST({indented(select_expr, 10)} AS CHAR) ASC
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
    cn = _qualified(col.name)
    keys = config.percentiles
    value = _RANKED_VALUE
    select_parts = [
        f"MIN({value}) AS mn",
        f"MAX({value}) AS mx",
        f"AVG({value}) AS avg_val",
        f"SUM({value}) AS sum_val",
        *_percentile_select(keys),
    ]
    row = exec_query(cursor, select_from(select_parts, _ranked(source, cn))).fetchone()

    if row is None:
        return Range(min=None, max=None), {}, "uniform", summarize_frequencies([]), (), None, None

    rng = Range(
        min=round_statistic(row[0], exact_int=True),
        max=round_statistic(row[1], exact_int=True),
    )
    mean = round_statistic(row[2])
    total = round_statistic(row[3], exact_int=True)
    percentiles = coherent_percentiles(
        {f"p{p:02d}": round_statistic(v) for p, v in zip(keys, row[4:], strict=True)},
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

    cn = _qualified(col.name)
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

    cn = _qualified(col.name)
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


def _fetch_length_p95(cursor: Cursor, source: str, col: ColumnMeta) -> float | None:
    """P95 character length (SPEC 2.2.4), ranked the same way numeric percentiles are - but with
    an explicit alias, the shared helpers repeating an expression only a bare column resolves.
    """

    cn = _qualified(col.name)
    length_expr = f"CHAR_LENGTH(CAST({cn} AS CHAR))"
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
) -> tuple[Distribution, Frequencies, tuple[ValueCount, ...]]:
    """Distribution, frequencies, and the same top-N rows `values` publishes (SPEC 2.2.3) -
    grouping stays on the raw column, so a rendered expression cannot split one value in two.
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
          {group_expr} IS NOT NULL
        GROUP BY
          {group_expr}
        ORDER BY
          cnt DESC, CAST({indented(select_expr, 10)} AS CHAR) ASC
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
    """Phase A's answer for a table whose batched query yielded no row.

    `supported` is per column, a property of the type rather than of the empty result.
    """

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


def _qualified(name: str) -> str:
    return qualified(quote(name, DIALECT))


def _alias(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name)


def _replace(stats: ColumnStats, **kwargs: Any) -> ColumnStats:

    return replace(stats, **kwargs)
