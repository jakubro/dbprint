"""Two-phase batched per-table statistics for BigQuery - Phase B pre-classifies internally and
both MUST converge; `cardinality_method` is `approximate` bar the exact re-count (SPEC 2.2.2).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Any, Literal

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
from .connection import exec_query
from .introspect import row_count_hint
from .rendering import render_operand, render_text, temporal_shape
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
)
from ..identifiers import SOURCE_ALIAS, Identity
from ..sql_layout import derived, indented, select_from


if TYPE_CHECKING:
    from .connection import Cursor


# Reduction range for the sampling seed - a safe width, not a documented BigQuery limit.
SEED_MODULUS = 2**31


# `materialize()`'s scratch copy self-expires this far out - generous for any single run,
# bounded enough that a killed process leaves nothing to find and drop by hand.
_SCRATCH_TABLE_EXPIRATION_HOURS = 6


# Engine units as BigQuery date parts (SPEC 2.2.16); its `WEEK` opens on Sunday, so `ISOWEEK`.
_TIMELINE_DATE_PARTS = {"day": "DAY", "week": "ISOWEEK", "month": "MONTH"}

_UNSUPPORTED_TYPES = (
    "bytes",
    "geography",
    "array",
    "struct",
    "record",
)

# Vendor spellings profiled as text by representability (SPEC 3.1), declared so none is guessed.
_TEXT_TYPES: tuple[str, ...] = (
    "interval",
    "range",
)

KNOWN_TYPES = (*_UNSUPPORTED_TYPES, *_TEXT_TYPES)


def compute_base(
    cursor: Cursor,
    project: str,
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
        partial(_phase_a_statement, cursor, identity, source),
        partial(_null_counts, cursor, identity, source),
        partial(_recount, cursor, identity, source),
        declines=lambda col: _is_unsupported(col.classified_type),
    )
    row_count, row_count_method = _table_row_count(
        cursor,
        project,
        identity,
        rows_scanned,
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
    """Phase B: the classification-specific statistics, keyed by column name. Scalar aggregates
    fuse into one statement; the value list stays per-column, since `APPROX_TOP_COUNT` ties.
    """

    if not columns:
        return PhaseB({})

    if counts.rows_scanned == 0:
        if scope is not None and scope.narrows:
            return PhaseB({})

        return PhaseB({c.name: _empty_stats(c) for c in columns})

    source = _table_source(identity, scope)
    pre_by_col = {
        col.name: _pre_classify(
            col,
            base[col.name].cardinality,
            config,
            col.name in fk_source_columns,
        )
        for col in columns
    }
    total = len(columns)
    position = {col.name: index for index, col in enumerate(columns, start=1)}

    def batch(cols: list[ColumnMeta]) -> dict[str, ColumnStats]:
        blocks = _fetch_phase_b_batch(cursor, identity, source, cols, base, pre_by_col)

        return {col.name: assemble(col, blocks.get(col.name, {})) for col in cols}

    def measure(col: ColumnMeta) -> ColumnStats:
        blocks = _fetch_phase_b_batch(cursor, identity, source, [col], base, pre_by_col)

        return assemble(col, blocks.get(col.name, {}))

    def assemble(col: ColumnMeta, block: dict[str, Any]) -> ColumnStats:
        if on_column is not None:
            on_column(position[col.name], total, col.name)

        return _assemble_column_stats(
            cursor,
            identity,
            source,
            col,
            base[col.name],
            counts.rows_scanned,
            pre_by_col[col.name],
            block,
            config,
            suppressed=col.name in suppress_values,
        )

    return run_phase_b(columns, measure, batch)


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
    quoted = [identity.source_column(col.name) for col in columns]
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
          dbprint_nulls
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
    """One batched statement testing every candidate pair (SPEC 2.2.12) - `COUNT(DISTINCT)` takes
    one expression here, so the pair is encoded as `TO_JSON_STRING(STRUCT(a, b))` (measured).
    """

    del columns

    if not candidates:
        return ()

    source = _table_source(identity, scope)
    exprs = [
        f"COUNT(DISTINCT TO_JSON_STRING(STRUCT("
        f"{identity.source_column(a)}, {identity.source_column(b)}))) AS dbprint_grain_{i}"
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
    col = {c.name: c for c in columns}[column]
    cn = identity.source_column(column)
    bq_unit = _TIMELINE_DATE_PARTS[unit]
    truncate = _timeline_truncation(col.classified_type)

    rows = exec_query(
        cursor,
        f"""
        SELECT
          bkt.bucket_start,
          bkt.cnt
        FROM
          (
            SELECT
              {truncate}({cn}, {bq_unit}) AS bucket_start,
              COUNT(1) AS cnt
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


def _timeline_truncation(sql_type: str) -> str:
    """BigQuery's truncation function for the anchor's own type (SPEC 2.2.16).

    Each returns its argument's type; casting to DATE first would discard a time nothing recovers.
    """

    if temporal_shape(sql_type) == "date":
        return "DATE_TRUNC"

    return "TIMESTAMP_TRUNC" if temporal_shape(sql_type) == "timestamp_tz" else "DATETIME_TRUNC"


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

    del counts, columns

    if not subject_columns:
        return {}

    source = _table_source(identity, scope)
    anchor_cn = identity.source_column(anchor_column)

    agg_exprs = []

    for i, subject in enumerate(subject_columns):
        subject_cn = identity.source_column(subject)
        agg_exprs.append(
            f"MIN(CASE WHEN {subject_cn} IS NOT NULL THEN {anchor_cn} END) AS from_{i}",
        )
        agg_exprs.append(f"MAX(CASE WHEN {subject_cn} IS NOT NULL THEN {anchor_cn} END) AS to_{i}")

    row = exec_query(cursor, select_from(agg_exprs, source)).fetchone()

    if row is None:
        return {}

    windows: dict[str, tuple[str, str]] = {}

    for i, subject in enumerate(subject_columns):
        from_val, to_val = row[2 * i], row[2 * i + 1]

        if from_val is not None and to_val is not None:
            windows[subject] = (
                measured_text(from_val, "populated.from"),
                measured_text(to_val, "populated.to"),
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
        f"COUNT(DISTINCT TO_JSON_STRING(STRUCT("
        f"{identity.source_column(a)}, {identity.source_column(b)}))) AS dbprint_dep_{i}"
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
    """Copy the drawn fraction into a real table under a throwaway name; `release()` drops it.

    A session-scoped temp table needs continuity this cursor has no seam for, so the copy is
    permanent: expiration rides the create statement, and `CREATE OR REPLACE` clears a stale one.
    """

    name = materialized_name(identity.fqn)
    drawn = _sample_expr(identity, scope)
    exec_query(
        cursor,
        f"CREATE OR REPLACE TABLE {identity.sibling(name)} "
        f"OPTIONS(expiration_timestamp = TIMESTAMP_ADD("
        f"CURRENT_TIMESTAMP(), INTERVAL {_SCRATCH_TABLE_EXPIRATION_HOURS} HOUR)) "
        f"AS SELECT * FROM {drawn}",
    )

    return replace(scope, materialized=identity.sibling(name))


def release(cursor: Cursor, scope: TableScope) -> None:
    """Drop the copied sample."""

    if scope.materialized is None:
        return

    exec_query(cursor, f"DROP TABLE IF EXISTS {scope.materialized}")


def _sample_expr(identity: Identity, scope: TableScope) -> str:
    """The `TABLESAMPLE`-bearing source `materialize_scope` reads once to build its copy."""

    quoted = identity.quoted()

    if scope.filter is not None:
        return derived(f"SELECT * FROM {quoted} WHERE ({scope.filter})", SOURCE_ALIAS)

    assert scope.sample is not None  # TableScope guarantees exactly one of filter/sample

    return derived(
        f"""
        SELECT
          *
        FROM
          {quoted} TABLESAMPLE SYSTEM ({scope.sample * 100} PERCENT)
        """,
        SOURCE_ALIAS,
    )


def _table_source(identity: Identity, scope: TableScope | None) -> str:
    return _source(identity.quoted(), scope)


def _source(quoted_fqn: str, scope: TableScope | None, seed: int | None = None) -> str:
    """Table reference every statistics query selects FROM - a `sample` scope with no materialized
    copy never reaches here, `orchestrator._materialize_scope` having refused the table first.
    """

    del seed

    if scope is None or not scope.narrows:
        return f"{quoted_fqn} {SOURCE_ALIAS}"
    elif scope.materialized is not None:
        return f"{scope.materialized} {SOURCE_ALIAS}"
    else:
        return derived(f"SELECT * FROM {quoted_fqn} WHERE ({scope.filter})", SOURCE_ALIAS)


def _table_row_count(
    cursor: Cursor,
    project: str,
    identity: Identity,
    rows_scanned: int,
    scope: TableScope | None,
) -> tuple[int, RowCountMethod]:
    """Rows in the table and how they were obtained (SPEC 2.2.1) - a narrowed read takes the
    catalog estimate where one is available, and counts exactly where none is.
    """

    if scope is None or not scope.narrows:
        return rows_scanned, "exact"

    estimate = row_count_hint(cursor, project, identity)

    if estimate is not None:
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
    identity: Identity,
    source: str,
    columns: list[ColumnMeta],
) -> tuple[int, dict[str, int]]:
    counts = [f"COUNT({identity.source_column(col.name)})" for col in columns]
    row = exec_query(cursor, select_from(["COUNT(1)", *counts], source)).fetchone()
    rows, *non_null = (int(value) for value in row) if row else (0, *(0 for _ in columns))

    return rows, {col.name: rows - n for col, n in zip(columns, non_null, strict=True)}


def _recount(
    cursor: Cursor,
    identity: Identity,
    source: str,
    columns: list[ColumnMeta],
) -> Sequence[Any] | None:
    select_parts = [
        f"COUNT(DISTINCT {render_operand(identity.source_column(col.name), col.classified_type)}) "
        f"AS dbprint_exact_{_alias(col.name)}"
        for col in columns
    ]

    return exec_query(cursor, select_from(select_parts, source)).fetchone()


def _phase_a_statement(
    cursor: Cursor,
    identity: Identity,
    source: str,
    columns: list[ColumnMeta],
) -> tuple[int, dict[str, BaseStats]]:
    """One query yielding row_count + per-column null_count + cardinality - `nulls` is avoided
    as a column alias, the parser reserving it (measured).
    """

    select_parts: list[str] = ["COUNT(1) AS row_count"]

    for col in columns:
        cn = identity.source_column(col.name)
        a = _alias(col.name)
        select_parts.append(f"COUNTIF({cn} IS NULL) AS dbprint_null_{a}")

        if is_numeric_type(col.classified_type):
            select_parts.append(f"COUNTIF({cn} = 0) AS dbprint_zero_{a}")
            select_parts.append(f"COUNTIF({cn} < 0) AS dbprint_neg_{a}")
            select_parts.append(f"COUNTIF({cn} = CAST({cn} AS INT64)) AS dbprint_quant_{a}")
        elif _is_string_like(col.classified_type):
            select_parts.append(
                f"COUNTIF({render_text(cn, col.classified_type)} = '') AS dbprint_empty_{a}",
            )
            length_expr = f"LENGTH(CAST({cn} AS STRING))"
            select_parts.append(f"MIN({length_expr}) AS dbprint_lenmin_{a}")
            select_parts.append(f"MAX({length_expr}) AS dbprint_lenmax_{a}")
            select_parts.append(f"AVG({length_expr}) AS dbprint_lenavg_{a}")

        # `_is_unsupported` types are skipped outright - SPEC 3.3's `unsupported` carries no
        # cardinality. JSON still measures, through `_exact_count_expr`'s string encoding.
        if not _is_unsupported(col.classified_type):
            select_parts.append(
                f"APPROX_COUNT_DISTINCT({render_operand(cn, col.classified_type)}) AS dbprint_card_{a}",
            )

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

        if _is_unsupported(col.classified_type):
            # Never queried above - discarded downstream regardless (SPEC 3.3's `unsupported`
            # carries no cardinality), so 0 is a dead value, not a claimed measurement.
            cardinality = 0
        else:
            cardinality = int(row[idx])
            idx += 1

        out[col.name] = BaseStats(
            null_count=null_count,
            cardinality=cardinality,
            cardinality_method="approximate",
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
        cardinality_method="approximate",
    )


def _fetch_phase_b_batch(
    cursor: Cursor,
    identity: Identity,
    source: str,
    columns: list[ColumnMeta],
    base: dict[str, BaseStats],
    pre_by_col: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """One statement covering every column's Phase B scalar aggregates - a flat `select_parts`
    list plus a per-column `plan`, so the single returned row slices back apart positionally.
    """

    select_parts: list[str] = []
    plans: dict[str, dict[str, Any]] = {}

    for col in columns:
        pre = pre_by_col[col.name]

        if pre in ("unsupported", "json"):
            continue

        cn = identity.source_column(col.name)
        a = _alias(col.name)
        plan: dict[str, Any] = {}

        if base[col.name].length_min is not None:
            select_parts.append(
                f"APPROX_QUANTILES(LENGTH(CAST({cn} AS STRING)), 100)[OFFSET(95)] AS lenp95_{a}",
            )
            plan["length_p95"] = True

        if pre == "numeric":
            select_parts.extend(
                [
                    f"MIN({cn}) AS mn_{a}",
                    f"MAX({cn}) AS mx_{a}",
                    f"AVG({cn}) AS avg_{a}",
                    f"SUM({cn}) AS sum_{a}",
                    f"APPROX_QUANTILES({cn}, 100) AS qs_{a}",
                ],
            )
            plan["numeric"] = True
        elif pre == "temporal":
            shape = temporal_shape(col.classified_type)
            time_only = shape in ("time", "time_tz")
            date_only = shape == "date"
            is_tz = shape == "timestamp_tz"
            day_aligned = not (date_only or time_only)

            select_parts.append(f"MIN({cn}) AS mn_{a}")
            select_parts.append(f"MAX({cn}) AS mx_{a}")
            select_parts.append(f"APPROX_QUANTILES({cn}, 100) AS qs_{a}")

            if date_only:
                # A DATE has no time-of-day component, so a calendar-date difference already
                # is the elapsed-day count.
                select_parts.append(f"DATE_DIFF(MAX({cn}), MIN({cn}), DAY) AS span_{a}")
            elif not time_only:
                # `*_DIFF` counts unit boundaries, so only the value's own resolution counts
                # elapsed time; `DIV` keeps the day count an exact integer (SPEC 2.2.4).
                diff_fn = "TIMESTAMP_DIFF" if is_tz else "DATETIME_DIFF"
                select_parts.append(
                    f"DIV({diff_fn}(MAX({cn}), MIN({cn}), MICROSECOND), 86400000000) AS span_{a}",
                )

            if day_aligned:
                # TIMESTAMP and DATETIME share no common type under `=`, so the midnight
                # constructor is chosen by whether this column is tz-aware or naive.
                midnight = f"TIMESTAMP(DATE({cn}))" if is_tz else f"DATETIME(DATE({cn}))"
                select_parts.append(f"COUNTIF({cn} = {midnight}) AS quant_{a}")

            plan.update(
                temporal=True,
                time_only=time_only,
                date_only=date_only,
            )

        plans[col.name] = plan

    if not select_parts:
        return {}

    row = exec_query(cursor, select_from(select_parts, source)).fetchone()

    if row is None:
        return {}

    blocks: dict[str, dict[str, Any]] = {}
    idx = 0

    for col in columns:
        col_plan = plans.get(col.name)

        if col_plan is None:
            continue

        block: dict[str, Any] = {}

        if col_plan.get("length_p95"):
            block["length_p95"] = row[idx]
            idx += 1

        if col_plan.get("numeric"):
            block["mn"], block["mx"], block["avg"], block["sum"], block["qs"] = row[idx : idx + 5]
            idx += 5

        if col_plan.get("temporal"):
            block["mn"] = row[idx]
            block["mx"] = row[idx + 1]
            block["qs"] = row[idx + 2]
            idx += 3

            if col_plan["date_only"] or not col_plan["time_only"]:
                block["span"] = row[idx]
                idx += 1

            if not (col_plan["date_only"] or col_plan["time_only"]):
                block["quant"] = row[idx]
                idx += 1

        blocks[col.name] = block

    return blocks


def _fetch_value_list(
    cursor: Cursor,
    identity: Identity,
    source: str,
    col: ColumnMeta,
    non_null: int,
    config: StatisticsConfig,
) -> tuple[tuple[ValueCount, ...], float, bool]:
    """The exact top-N value list for one column - deterministic `GROUP BY`, not
    `APPROX_TOP_COUNT`. See `compute_columns`'s docstring for why this stays per-column.
    """

    cn = identity.source_column(col.name)
    limit = enumeration_limit(config.enumeration_threshold, config.top_n_values)
    select_expr = (
        render_text(cn, col.classified_type) if _is_string_like(col.classified_type) else cn
    )
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
          rendered
        ORDER BY
          cnt DESC, CAST(rendered AS STRING) ASC
        LIMIT %s
        """,
        (limit + 1,),
    ).fetchall()
    exhaustive = len(rows) <= limit
    kept = rows if exhaustive else rows[: config.top_n_values]
    entries = order_values(
        (
            ValueCount(value=measured_value(value, f"values[{i}]"), count=int(cnt))
            for i, (value, cnt) in enumerate(kept)
        ),
    )
    values = tuple(entries)
    total = sum(v.count for v in values)

    return values, coverage_share(total, non_null, exhaustive=exhaustive), exhaustive


def _approximate_distribution_via_top_n(
    cursor: Cursor,
    source: str,
    select_expr: str,
    group_expr: str,
    non_null: int,
    config: StatisticsConfig,
    value_transform: Any,
) -> tuple[Distribution, Frequencies, tuple[ValueCount, ...]]:
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
          rendered
        ORDER BY
          cnt DESC, CAST(rendered AS STRING) ASC
        LIMIT %s
        """,
        (limit + 1,),
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


def _assemble_column_stats(
    cursor: Cursor,
    identity: Identity,
    source: str,
    col: ColumnMeta,
    base: BaseStats,
    rows_scanned: int,
    pre: str,
    block: dict[str, Any],
    config: StatisticsConfig,
    *,
    suppressed: bool = False,
) -> ColumnStats:
    """Assembly from `base` (Phase A) and `block` (this table's one fused scalar row), plus one
    per-column statement for the value list where `pre` needs one.
    """

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

    if length_min is not None and length_max is not None:
        length_p95 = round_statistic(block.get("length_p95"))
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
        values, coverage, _ = _fetch_value_list(cursor, identity, source, col, non_null, config)

        return replace(stats, values=values, values_coverage=coverage)

    if pre == "numeric":
        cn = identity.source_column(col.name)
        distribution, frequencies, values = _approximate_distribution_via_top_n(
            cursor,
            source,
            cn,
            cn,
            non_null,
            config,
            measured_value,
        )
        keys = config.percentiles
        quantiles = block.get("qs") or []
        rng = Range(
            min=round_statistic(block.get("mn"), exact_int=True),
            max=round_statistic(block.get("mx"), exact_int=True),
        )
        percentiles = coherent_percentiles(
            {f"p{p:02d}": round_statistic(quantiles[p]) for p in keys if p < len(quantiles)},
            rng.min,
            rng.max,
        )

        return replace(
            stats,
            range=rng,
            percentiles=percentiles,
            distribution=distribution,
            frequencies=frequencies,
            values=values,
            mean=round_statistic(block.get("avg")),
            sum=round_statistic(block.get("sum"), exact_int=True),
        )

    if pre == "temporal":
        keys = config.percentiles
        quantiles = block.get("qs") or []
        span_days = int(block["span"]) if block.get("span") is not None else 0
        rng = Range(
            min=measured_value(block.get("mn"), "range.min"),
            max=measured_value(block.get("mx"), "range.max"),
            span_days=span_days,
        )
        percentiles = {
            f"p{p:02d}": measured_value(quantiles[p], f"percentiles.p{p:02d}")
            for p in keys
            if p < len(quantiles)
        }
        unrepresentable = _unrepresentable_fields(rng, percentiles)
        quant = block.get("quant")
        quantized_count = int(quant) if quant is not None else None

        try:
            cn = identity.source_column(col.name)
            distribution, frequencies, values = _approximate_distribution_via_top_n(
                cursor,
                source,
                cn,
                cn,
                non_null,
                config,
                measured_value,
            )
        except UnrepresentableValue:
            raise
        except Exception as exc:  # noqa: BLE001 - only top-N is guarded; bounds survive
            # `distribution`/`frequencies` are REQUIRED (SPEC 2.2.3); an empty count list would
            # classify `uniform` over nothing.
            return replace(
                stats,
                range=rng,
                percentiles=percentiles,
                unrepresentable=unrepresentable or None,
                quantized_count=quantized_count,
                unmeasured=("distribution", "frequencies", "values"),
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
            quantized_count=quantized_count,
        )

    if pre == "categorical":
        values, coverage, exhaustive = _fetch_value_list(
            cursor,
            identity,
            source,
            col,
            non_null,
            config,
        )
        distribution = classify_distribution(
            [v.count for v in values],
            non_null,
            exhaustive=exhaustive,
        )

        return replace(stats, values=values, values_coverage=coverage, distribution=distribution)

    if suppressed and pre == "text":
        return stats

    # text / foreign_key_candidate: value list and its implied shape only.
    values, coverage, exhaustive = _fetch_value_list(
        cursor,
        identity,
        source,
        col,
        non_null,
        config,
    )
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


def _empty_base(col: ColumnMeta) -> BaseStats:
    return BaseStats(
        null_count=0,
        cardinality=0,
        cardinality_method="approximate",
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
