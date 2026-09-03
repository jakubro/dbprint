"""Two-phase batched per-table statistics for BigQuery - Phase B pre-classifies internally and
both MUST converge; `cardinality_method` is `approximate` bar the exact re-count (SPEC 2.2.2).
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
from .introspect import estimate_row_count
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
    Range,
    RowCountMethod,
    TableCounts,
    TableScope,
    ValueCount,
    has_measurable_nulls,
    materialized_name,
    null_flags,
    null_patterns_from_rows,
)


if TYPE_CHECKING:
    from .connection import Cursor


# Reduction range for the sampling seed - a safe width, not a documented BigQuery limit.
SEED_MODULUS = 2**31

# Ratio at/above which a column is re-counted exactly (SPEC 2.2.2) - the constant Postgres and
# Snowflake use, since APPROX_COUNT_DISTINCT can cost a real key its verdict at SPEC 4.2's 0.9999.
_EXACT_PROBE_RATIO = 0.85

# `materialize()`'s scratch copy self-expires this far out - generous for any single run,
# bounded enough that a killed process leaves nothing to find and drop by hand.
_SCRATCH_TABLE_EXPIRATION_HOURS = 6

_NUMERIC_TYPES = ("int64", "integer", "float64", "float", "numeric", "bignumeric", "decimal")
_TEMPORAL_TYPES = ("date", "time", "datetime", "timestamp")
_DATE_ONLY_TYPES = ("date",)
_TIME_ONLY_TYPES = ("time",)
_TZ_TYPES = ("timestamp",)  # the only instant-based temporal type; datetime/date/time are naive

_BOOLEAN_TYPES = ("bool", "boolean")
_JSON_TYPES = ("json",)
_UNSUPPORTED_TYPES = ("bytes", "geography", "array", "struct", "record")


def compute_base(
    cursor: Cursor,
    project: str,
    identity: Identity,
    columns: list[ColumnMeta],
    scope: TableScope | None = None,
) -> tuple[TableCounts, dict[str, BaseStats]]:
    """Phase A: the table's counts plus per-column null_count and cardinality."""

    if not columns:
        return TableCounts(row_count=0, rows_scanned=0), {}

    source = _table_source(identity, scope)
    rows_scanned, base_stats = _phase_a(cursor, identity, source, columns)
    _settle_near_unique(cursor, identity, source, columns, base_stats, rows_scanned)
    row_count, row_count_method = _table_row_count(
        cursor,
        project,
        identity,
        rows_scanned,
        scope,
    )

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
    """Phase B: the classification-specific statistics, keyed by column name. Scalar aggregates
    fuse into one statement; the value list stays per-column, since `APPROX_TOP_COUNT` ties.
    """

    if not columns:
        return {}

    if counts.rows_scanned == 0:
        if scope is not None and scope.narrows:
            return {}

        return {c.name: _empty_stats(c) for c in columns}

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
    blocks = _fetch_phase_b_batch(cursor, identity, source, columns, base, pre_by_col)

    enriched: dict[str, ColumnStats] = {}
    total = len(columns)

    for index, col in enumerate(columns, start=1):
        if on_column is not None:
            on_column(index, total, col.name)

        enriched[col.name] = _assemble_column_stats(
            cursor,
            identity,
            source,
            col,
            base[col.name],
            counts.rows_scanned,
            pre_by_col[col.name],
            blocks.get(col.name, {}),
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
    quoted = [identity.quoted_column(col.name) for col in columns]
    cap = config.top_n_null_patterns
    rows = exec_query(
        cursor,
        f"""
        SELECT {null_flags(quoted, concat=True)} AS dbprint_nulls, COUNT(*) AS cnt
        FROM {source}
        GROUP BY dbprint_nulls
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
    """One batched statement testing every candidate pair (SPEC 2.2.12) - `COUNT(DISTINCT)` takes
    one expression here, so the pair is encoded as `TO_JSON_STRING(STRUCT(a, b))` (measured).
    """

    del columns

    if not candidates:
        return ()

    source = _table_source(identity, scope)
    exprs = [
        f"COUNT(DISTINCT TO_JSON_STRING(STRUCT("
        f"{identity.quoted_column(a)}, {identity.quoted_column(b)}))) AS dbprint_grain_{i}"
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
    """One grouped statement bucketing `column` at `unit` grain. See SPEC 2.2.16."""

    del counts, columns

    source = _table_source(identity, scope)
    cn = identity.quoted_column(column)
    bq_unit = unit.upper()

    rows = exec_query(
        cursor,
        f"""
        SELECT bucket_start, cnt
        FROM (
            SELECT DATE_TRUNC(DATE({cn}), {bq_unit}) AS bucket_start, COUNT(*) AS cnt
            FROM {source}
            WHERE {cn} IS NOT NULL
            GROUP BY bucket_start
        ) buckets
        ORDER BY bucket_start
        """,
    ).fetchall()

    return tuple((_iso_or_value(row[0]), int(row[1])) for row in rows)


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
    anchor_cn = identity.quoted_column(anchor_column)

    agg_exprs = []

    for i, subject in enumerate(subject_columns):
        subject_cn = identity.quoted_column(subject)
        agg_exprs.append(
            f"MIN(CASE WHEN {subject_cn} IS NOT NULL THEN {anchor_cn} END) AS from_{i}",
        )
        agg_exprs.append(f"MAX(CASE WHEN {subject_cn} IS NOT NULL THEN {anchor_cn} END) AS to_{i}")

    row = exec_query(cursor, f"SELECT {', '.join(agg_exprs)} FROM {source}").fetchone()

    if row is None:
        return {}

    windows: dict[str, tuple[str, str]] = {}

    for i, subject in enumerate(subject_columns):
        from_val, to_val = row[2 * i], row[2 * i + 1]

        if from_val is not None and to_val is not None:
            windows[subject] = (_iso_or_value(from_val), _iso_or_value(to_val))

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
        f"{identity.quoted_column(a)}, {identity.quoted_column(b)}))) AS dbprint_dep_{i}"
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
    """Copy the drawn fraction into a real table under a throwaway name; `release()` drops it.

    A session-scoped temp table needs continuity this cursor has no seam for, so the copy is
    permanent: expiration rides the create statement, and `CREATE OR REPLACE` clears a stale one.
    """

    name = materialized_name(identity.dotted().lower())
    drawn = _sample_expr(identity, scope)
    exec_query(
        cursor,
        f"CREATE OR REPLACE TABLE {identity.sibling(name)} "
        f"OPTIONS(expiration_timestamp = TIMESTAMP_ADD("
        f"CURRENT_TIMESTAMP(), INTERVAL {_SCRATCH_TABLE_EXPIRATION_HOURS} HOUR)) "
        f"AS SELECT * FROM {drawn}",
    )

    return replace(scope, materialized=f"{identity.dataset}.{name}")


def release(cursor: Cursor, scope: TableScope) -> None:
    """Drop the copied sample."""

    if scope.materialized is None:
        return

    dataset, name = scope.materialized.split(".", 1)
    exec_query(cursor, f"DROP TABLE IF EXISTS `{dataset}`.`{name}`")


def _sample_expr(identity: Identity, scope: TableScope) -> str:
    """The `TABLESAMPLE`-bearing source `materialize_scope` reads once to build its copy."""

    quoted = identity.quoted()

    if scope.filter is not None:
        return f"(SELECT * FROM {quoted} WHERE ({scope.filter}))"

    assert scope.sample is not None  # TableScope guarantees exactly one of filter/sample

    return f"(SELECT * FROM {quoted} TABLESAMPLE SYSTEM ({scope.sample * 100} PERCENT))"


def _table_source(identity: Identity, scope: TableScope | None) -> str:
    return _source(identity.quoted(), scope)


def _source(quoted_fqn: str, scope: TableScope | None, seed: int | None = None) -> str:
    """Table reference every statistics query selects FROM - a `sample` scope with no materialized
    copy never reaches here, `orchestrator._materialize_scope` having refused the table first.
    """

    del seed

    if scope is None or not scope.narrows:
        return quoted_fqn
    elif scope.materialized is not None:
        dataset, name = scope.materialized.split(".", 1)

        return f"`{dataset}`.`{name}`"
    else:
        return f"(SELECT * FROM {quoted_fqn} WHERE ({scope.filter})) AS dbprint_scoped"


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

    estimate = estimate_row_count(cursor, project, identity)

    if estimate is not None:
        return estimate, "approximate"

    row = exec_query(cursor, f"SELECT COUNT(*) FROM {identity.quoted()}").fetchone()

    return (int(row[0]) if row and row[0] is not None else rows_scanned), "exact"


def _phase_a(
    cursor: Cursor,
    identity: Identity,
    source: str,
    columns: list[ColumnMeta],
) -> tuple[int, dict[str, BaseStats]]:
    """One query yielding row_count + per-column null_count + cardinality - `nulls` is avoided
    as a column alias, the parser reserving it (measured).
    """

    select_parts: list[str] = ["COUNT(*) AS row_count"]

    for col in columns:
        cn = identity.quoted_column(col.name)
        a = _alias(col.name)
        select_parts.append(f"COUNTIF({cn} IS NULL) AS dbprint_null_{a}")

        if _matches(col.sql_type, _NUMERIC_TYPES):
            select_parts.append(f"COUNTIF({cn} = 0) AS dbprint_zero_{a}")
            select_parts.append(f"COUNTIF({cn} < 0) AS dbprint_neg_{a}")
            select_parts.append(f"COUNTIF({cn} = CAST({cn} AS INT64)) AS dbprint_quant_{a}")
        elif _is_string_like(col.sql_type):
            select_parts.append(f"COUNTIF({cn} = '') AS dbprint_empty_{a}")
            length_expr = f"LENGTH(CAST({cn} AS STRING))"
            select_parts.append(f"MIN({length_expr}) AS dbprint_lenmin_{a}")
            select_parts.append(f"MAX({length_expr}) AS dbprint_lenmax_{a}")
            select_parts.append(f"AVG({length_expr}) AS dbprint_lenavg_{a}")

        # `_is_unsupported` types are skipped outright - SPEC 3.3's `unsupported` carries no
        # cardinality. JSON still measures, through `_exact_count_expr`'s string encoding.
        if not _is_unsupported(col.sql_type):
            select_parts.append(
                f"APPROX_COUNT_DISTINCT({_exact_count_expr(cn, col.sql_type)}) AS dbprint_card_{a}",
            )

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

        if _is_unsupported(col.sql_type):
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


def _settle_near_unique(
    cursor: Cursor,
    identity: Identity,
    source: str,
    columns: list[ColumnMeta],
    base: dict[str, BaseStats],
    row_count: int,
) -> None:
    """Re-count exactly the columns `APPROX_COUNT_DISTINCT` could misclassify (SPEC 2.2.2).

    Mutates `base` in place - one batched statement over the columns whose sketch sits near-unique.
    """

    near_unique = [
        col
        for col in columns
        if not _is_unsupported(col.sql_type)
        and row_count
        and base[col.name].cardinality / row_count >= _EXACT_PROBE_RATIO
    ]

    if not near_unique:
        return

    select_parts = [
        f"COUNT(DISTINCT {_exact_count_expr(identity.quoted_column(col.name), col.sql_type)}) "
        f"AS dbprint_exact_{_alias(col.name)}"
        for col in near_unique
    ]
    row = exec_query(cursor, f"SELECT {', '.join(select_parts)} FROM {source}").fetchone()

    if row is None:
        return

    for col, value in zip(near_unique, row):
        # A separate statement may read a different snapshot than Phase A - clamp to non_null.
        base[col.name] = replace(
            base[col.name],
            cardinality=min(row_count - base[col.name].null_count, int(value)),
            cardinality_method="exact",
        )


def _exact_count_expr(cn: str, sql_type: str) -> str:
    """`COUNT`/`APPROX_COUNT_DISTINCT`'s argument for one column - JSON needs `TO_JSON_STRING`
    first, not being directly groupable.
    """

    return f"TO_JSON_STRING({cn})" if _matches(sql_type, _JSON_TYPES) else cn


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

        cn = identity.quoted_column(col.name)
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
            time_only = _matches(col.sql_type, _TIME_ONLY_TYPES)
            date_only = _matches(col.sql_type, _DATE_ONLY_TYPES)
            is_tz = _matches(col.sql_type, _TZ_TYPES)
            day_aligned = not (date_only or time_only)

            select_parts.append(f"MIN({cn}) AS mn_{a}")
            select_parts.append(f"MAX({cn}) AS mx_{a}")
            select_parts.append(f"APPROX_QUANTILES({cn}, 100) AS qs_{a}")

            if date_only:
                # A DATE has no time-of-day component, so a calendar-date difference already
                # is the elapsed-day count.
                select_parts.append(f"DATE_DIFF(MAX({cn}), MIN({cn}), DAY) AS span_{a}")
            elif not time_only:
                # `DATE_DIFF` would count calendar dates crossed, not elapsed time - floor
                # division on elapsed seconds is what every other adapter's span_days means.
                diff_fn = "TIMESTAMP_DIFF" if is_tz else "DATETIME_DIFF"
                select_parts.append(
                    f"CAST(FLOOR({diff_fn}(MAX({cn}), MIN({cn}), SECOND) / 86400) AS INT64) "
                    f"AS span_{a}",
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

    row = exec_query(cursor, f"SELECT {', '.join(select_parts)} FROM {source}").fetchone()

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

    cn = identity.quoted_column(col.name)
    n = config.top_n_values
    rows = exec_query(
        cursor,
        f"""
        SELECT {cn} AS rendered, COUNT(*) AS cnt
        FROM {source}
        WHERE {cn} IS NOT NULL
        GROUP BY rendered
        ORDER BY cnt DESC, CAST(rendered AS STRING) ASC
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
        GROUP BY rendered
        ORDER BY cnt DESC, CAST(rendered AS STRING) ASC
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
        length_p95 = _round_numeric(block.get("length_p95"))
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
        cn = identity.quoted_column(col.name)
        distribution, frequencies, values = _approximate_distribution_via_top_n(
            cursor,
            source,
            cn,
            cn,
            non_null,
            config,
            _round_numeric,
        )
        keys = config.percentiles
        quantiles = block.get("qs") or []
        rng = Range(
            min=_round_numeric(block.get("mn"), exact_int=True),
            max=_round_numeric(block.get("mx"), exact_int=True),
        )
        percentiles = {
            f"p{p:02d}": _round_numeric(quantiles[p]) for p in keys if p < len(quantiles)
        }

        return replace(
            stats,
            range=rng,
            percentiles=percentiles,
            distribution=distribution,
            frequencies=frequencies,
            values=values,
            mean=_round_numeric(block.get("avg")),
            sum=_round_numeric(block.get("sum"), exact_int=True),
        )

    if pre == "temporal":
        keys = config.percentiles
        quantiles = block.get("qs") or []
        span_days = int(block["span"]) if block.get("span") is not None else 0
        rng = Range(
            min=_iso_or_value(block.get("mn")),
            max=_iso_or_value(block.get("mx")),
            span_days=span_days,
        )
        percentiles = {f"p{p:02d}": _iso_or_value(quantiles[p]) for p in keys if p < len(quantiles)}
        unrepresentable = _unrepresentable_fields(rng, percentiles)
        quant = block.get("quant")
        quantized_count = int(quant) if quant is not None else None

        try:
            cn = identity.quoted_column(col.name)
            distribution, frequencies, values = _approximate_distribution_via_top_n(
                cursor,
                source,
                cn,
                cn,
                non_null,
                config,
                _iso_or_value,
            )
        except Exception:  # noqa: BLE001 - only the top-N statement is guarded, so bounds survive
            # `distribution`/`frequencies` are REQUIRED (SPEC 2.2.3); an empty count list would
            # classify `uniform` over nothing.
            return replace(
                stats,
                range=rng,
                percentiles=percentiles,
                unrepresentable=unrepresentable or None,
                quantized_count=quantized_count,
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


def _render_calendar_bound(expr: str, sql_type: str) -> str:
    """SQL text rendering `expr` per SPEC 2.2.4's domain-rendering rule - used only where a
    canonical STRING form must be computed in SQL; elsewhere Python's `_iso_or_value` does it.
    """

    is_date_only = _matches(sql_type, _DATE_ONLY_TYPES)
    is_time_only = _matches(sql_type, _TIME_ONLY_TYPES)
    is_tz = _matches(sql_type, _TZ_TYPES)

    if is_date_only:
        return f"FORMAT_DATE('%Y-%m-%d', {expr})"

    if is_time_only:
        return f"REGEXP_REPLACE(FORMAT_TIME('%H:%M:%E6S', {expr}), r'\\.000000$', '')"

    formatter = "FORMAT_TIMESTAMP" if is_tz else "FORMAT_DATETIME"
    picture = "%Y-%m-%dT%H:%M:%E6S"
    body = f"{formatter}('{picture}', {expr})"
    body = f"REGEXP_REPLACE({body}, r'\\.000000$', '')"

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


def _empty_base(col: ColumnMeta) -> BaseStats:
    return BaseStats(
        null_count=0,
        cardinality=0,
        cardinality_method="approximate",
        supported=not _is_unsupported(col.sql_type),
    )


def _is_unsupported(sql_type: str) -> bool:
    """Exact membership misses BigQuery's parametrized spelling - `ARRAY<STRING>` and
    `STRUCT<a INT64>` never reduce to the bare `array`/`struct` entries.
    """

    base = base_type(sql_type)

    return base in _UNSUPPORTED_TYPES or base.startswith(("array<", "struct<"))


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


def _alias(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name)
