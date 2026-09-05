"""Two-phase batched per-table statistics computation for ClickHouse - Phase B pre-classifies
internally and both MUST converge; native one-pass aggregates keep each phase one statement.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from typing import Any, Literal

from dbprint.config import StatisticsConfig
from dbprint.spec.classification import base_type, compute_cardinality_ratio, compute_null_rate
from dbprint.spec.coverage import coverage_share
from dbprint.spec.distribution import classify as classify_distribution
from dbprint.spec.distribution import summarize as summarize_frequencies
from dbprint.spec.temporal_range import is_representable
from .connection import Cursor, exec_query
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
    temporal_block_unmeasured,
)


APPROXIMATE_THRESHOLD = 1_000_000

# Ratio at/above which a column is re-counted exactly - the same constant Postgres/Snowflake
# use, since SPEC 4.2's candidate-key threshold (0.9999) needs a precise count near the top.
_EXACT_PROBE_RATIO = 0.85

_NUMERIC_TYPES = (
    "int8",
    "int16",
    "int32",
    "int64",
    "int128",
    "int256",
    "uint8",
    "uint16",
    "uint32",
    "uint64",
    "uint128",
    "uint256",
    "float32",
    "float64",
    "decimal",
    "decimal32",
    "decimal64",
    "decimal128",
    "decimal256",
)

_TEMPORAL_TYPES = ("date", "date32", "datetime", "datetime64")

_JSON_TYPES = ("json",)

_BOOLEAN_TYPES = ("bool",)

_UNSUPPORTED_TYPES = (
    "array",
    "map",
    "tuple",
    "nested",
    "aggregatefunction",
    "simpleaggregatefunction",
)


def compute_base(
    cursor: Cursor,
    identity: Identity,
    columns: list[ColumnMeta],
    scope: TableScope | None = None,
) -> tuple[TableCounts, dict[str, BaseStats]]:
    """Phase A: the table's counts plus per-column null_count and cardinality."""

    if not columns:
        return TableCounts(row_count=0, rows_scanned=0), {}

    source = _source(identity, scope)
    narrows = scope is not None and scope.narrows
    # The catalog estimate describes the whole table, so a narrowed read counts instead.
    estimate = estimate_row_count(cursor, identity)
    approximate = estimate > APPROXIMATE_THRESHOLD and not narrows
    rows_scanned, base_stats = _phase_a(cursor, source, columns, approximate)
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
            # A narrowed read drew nothing; an exact, exhaustive shape for columns
            # nobody read would overclaim (SPEC 2.2.7).
            return {}

        return {c.name: _empty_stats(c) for c in columns}

    source = _source(identity, scope)
    enriched: dict[str, ColumnStats] = {}
    total = len(columns)

    for index, col in enumerate(columns, start=1):
        if on_column is not None:
            on_column(index, total, col.name)

        pre = _pre_classify(col, base[col.name].cardinality, config, col.name in fk_source_columns)
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

    source = _source(identity, scope)
    quoted = [_quote_ident(col.physical_name or col.name) for col in columns]
    cap = config.top_n_null_patterns
    rows = exec_query(
        cursor,
        f"""
        SELECT {null_flags(quoted, concat=True)} AS dbprint_nulls, count() AS cnt
        FROM {source}
        GROUP BY dbprint_nulls
        ORDER BY cnt DESC, dbprint_nulls ASC
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
    physical = {col.name: col.physical_name or col.name for col in columns}
    exprs = [
        f"uniqExact(tuple({_quote_ident(physical[a])}, {_quote_ident(physical[b])}))"
        f" AS dbprint_grain_{i}"
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

    del counts

    source = _source(identity, scope)
    by_name = {col.name: col for col in columns}
    cn = _quote_ident(by_name[column].physical_name or by_name[column].name)
    bucket_expr = _timeline_bucket_expr(cn, unit)

    rows = exec_query(
        cursor,
        f"""
        SELECT toString(bucket_start) AS bucket_text, cnt
        FROM (
            SELECT {bucket_expr} AS bucket_start, count() AS cnt
            FROM {source}
            WHERE {cn} IS NOT NULL
            GROUP BY bucket_start
        ) buckets
        ORDER BY bucket_start
        """,
    ).fetchall()

    return tuple((row[0], int(row[1])) for row in rows)


def _render_temporal(expr: str) -> str:
    """SQL text rendering a DateTime expression per SPEC 2.2.4's ISO domain rule - `T` separator
    and no trailing `.000000`, so two adapters render one instant to the identical string.
    """

    rendered = f"formatDateTime(toDateTime64({expr}, 6), '%Y-%m-%dT%H:%i:%S.%f')"

    return f"replaceRegexpOne({rendered}, '\\\\.000000$', '')"


def _timeline_bucket_expr(cn: str, unit: str) -> str:
    """Native truncation function for `probe_timeline`'s GROUP BY key. See SPEC 2.2.16."""

    if unit == "day":
        return f"toStartOfDay(toDateTime({cn}))"

    if unit == "week":
        return f"toStartOfWeek(toDateTime({cn}), 1)"

    return f"toStartOfMonth(toDateTime({cn}))"


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
    anchor_cn = _quote_ident(anchor.physical_name or anchor.name)
    day_aligned = not _matches(anchor.sql_type, ("date", "date32"))
    exprs = []

    for subject in subject_columns:
        subject_cn = _quote_ident(by_name[subject].physical_name or by_name[subject].name)
        from_expr = f"minIf({anchor_cn}, {subject_cn} IS NOT NULL)"
        to_expr = f"maxIf({anchor_cn}, {subject_cn} IS NOT NULL)"
        rendered_from = _render_temporal(from_expr) if day_aligned else f"toString({from_expr})"
        rendered_to = _render_temporal(to_expr) if day_aligned else f"toString({to_expr})"
        exprs.append(f"{rendered_from} AS from_{_alias(subject)}")
        exprs.append(f"{rendered_to} AS to_{_alias(subject)}")

    row = exec_query(cursor, f"SELECT {', '.join(exprs)} FROM {source}").fetchone()

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
    """One batched statement measuring every candidate pair's joint cardinality (SPEC 2.2.13) -
    the null-safe `uniqExact(tuple(...))` form, or null-bearing rows inflate the ratio.
    """

    del counts

    if not candidates:
        return {}

    source = _source(identity, scope)
    physical = {col.name: col.physical_name or col.name for col in columns}
    exprs = [
        f"uniqExact(tuple({_quote_ident(physical[a])}, {_quote_ident(physical[b])}))"
        f" AS dbprint_dep_{i}"
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
    `ENGINE = MergeTree` keeps a large draw off RAM, and the name is never database-qualified.
    """

    name = materialized_name(identity.dotted().lower())
    drawn = _sample_expr(identity, scope)
    exec_query(
        cursor,
        f"CREATE TEMPORARY TABLE {_quote_ident(name)} ENGINE = MergeTree ORDER BY tuple() "
        f"AS SELECT * FROM {drawn}",
    )

    return replace(scope, materialized=name)


def release(cursor: Cursor, scope: TableScope) -> None:
    """Drop the copied sample; `TEMPORARY` is spelled out so no base table can match."""

    if scope.materialized is None:
        return

    exec_query(cursor, f"DROP TEMPORARY TABLE IF EXISTS {_quote_ident(scope.materialized)}")


def _source(identity: Identity, scope: TableScope | None) -> str:
    """The FROM expression every phase reads - a `sample` scope with no materialized copy never
    reaches here, `orchestrator._materialize_scope` having refused the table first.
    """

    quoted = identity.quoted()

    if scope is None or not scope.narrows:
        return quoted
    elif scope.materialized is not None:
        return _quote_ident(scope.materialized)
    else:
        return f"(SELECT * FROM {quoted} WHERE ({scope.filter})) AS dbprint_scoped"


def _sample_expr(identity: Identity, scope: TableScope) -> str:
    """The `SAMPLE`-bearing source `materialize_scope` reads once to build its copy."""

    quoted = identity.quoted()

    if scope.filter is not None:
        return f"(SELECT * FROM {quoted} WHERE ({scope.filter}))"

    return f"(SELECT * FROM {quoted} SAMPLE {scope.sample})"


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

    row = exec_query(cursor, f"SELECT count() FROM {identity.quoted()}").fetchone()

    return (int(row[0]) if row and row[0] is not None else rows_scanned), "exact"


def _phase_a(
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
        cn = _quote_ident(col.physical_name or col.name)
        a = _alias(col.name)
        select_parts.append(f"countIf({cn} IS NULL) AS null_{a}")

        if _matches(col.sql_type, _NUMERIC_TYPES):
            select_parts.append(f"countIf({cn} = 0) AS zero_{a}")
            select_parts.append(f"countIf({cn} < 0) AS neg_{a}")
            select_parts.append(f"countIf({cn} = trunc({cn})) AS quant_{a}")
        elif _is_string_like(col.sql_type):
            # A non-String type (UUID, Enum) has no native `= ''`/`length()`; casting first
            # is what every "string-like" type here actually shares (Postgres does the same).
            rendered = f"toString({cn})"
            select_parts.append(f"countIf({rendered} = '') AS empty_{a}")
            # `length()` is bytes on ClickHouse; `lengthUTF8()` is characters (SPEC 2.2.4).
            select_parts.append(f"min(lengthUTF8({rendered})) AS lenmin_{a}")
            select_parts.append(f"max(lengthUTF8({rendered})) AS lenmax_{a}")
            select_parts.append(f"avg(lengthUTF8({rendered})) AS lenavg_{a}")

        select_parts.append(f"{card_fn}({cn}) AS card_{a}")

    row = exec_query(cursor, f"SELECT {', '.join(select_parts)} FROM {source}").fetchone()

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

        # uniqCombined64 errs both ways; cardinality is non-null-only (SPEC 2.2.2), so clamp.
        cardinality = min(row_count - null_count, int(row[idx]))
        idx += 1
        out[col.name] = BaseStats(
            null_count=null_count,
            cardinality=cardinality,
            cardinality_method=method,
            supported=not _is_unsupported(col.sql_type),
            zero_count=zero_count,
            negative_count=negative_count,
            empty_count=empty_count,
            quantized_count=quantized_count,
            length_min=length_min,
            length_max=length_max,
            length_avg=length_avg,
        )

    if approximate:
        _settle_near_unique(cursor, source, columns, out, row_count)

    return row_count, out


def _settle_near_unique(
    cursor: Cursor,
    source: str,
    columns: list[ColumnMeta],
    base: dict[str, BaseStats],
    row_count: int,
) -> None:
    """Re-count, exactly, the columns an HLL estimate could misclassify - an estimate a fraction
    low costs a key its `candidate_key`, so near-unique columns take one batched follow-up.
    """

    near_unique = [
        col
        for col in columns
        if row_count and base[col.name].cardinality / row_count >= _EXACT_PROBE_RATIO
    ]

    if not near_unique:
        return

    select_parts = [
        f"uniqExact({_quote_ident(col.physical_name or col.name)}) AS {_alias(f'card_{col.name}')}"
        for col in near_unique
    ]
    row = exec_query(cursor, f"SELECT {', '.join(select_parts)} FROM {source}").fetchone()

    if row is None:
        return

    for col, value in zip(near_unique, row):
        # A separate statement may read a different snapshot than phase A; clamp to non_null.
        base[col.name] = replace(
            base[col.name],
            cardinality=min(row_count - base[col.name].null_count, int(value)),
            cardinality_method="exact",
        )


def _empty_stats(col: ColumnMeta) -> ColumnStats:
    """SPEC 2.2.7 edge case: a table read in full and found empty -> minimal column stats."""

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
                avg=_round_numeric(base.length_avg),
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
    """Adapter-internal classification mirroring the engine's SPEC 3.2 logic - uniqueness plays
    no part, the cardinality ratio sitting on the `inferred` axis (SPEC 4.2).
    """

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
    """Ordered value list, its coverage, and whether it enumerates the column - one row beyond
    the cap is fetched, so truncation is observed rather than predicted (SPEC 2.2.4).
    """

    cn = _quote_ident(col.physical_name or col.name)
    n = config.top_n_values
    rows = exec_query(
        cursor,
        f"""
        SELECT toString({cn}) AS rendered, count() AS cnt
        FROM {source}
        WHERE {cn} IS NOT NULL
        GROUP BY {cn}
        ORDER BY cnt DESC, rendered ASC
        LIMIT {n + 1}
        """,
    ).fetchall()
    exhaustive = len(rows) <= n
    entries = sorted(
        (ValueCount(value=value, count=int(cnt)) for value, cnt in rows[:n]),
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
        f"SELECT min({cn}), max({cn}), avg({cn}), sum({cn}), {_percentile_exprs(cn, keys)} "
        f"FROM {source}"
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
    percentile_values = row[4 : 4 + len(keys)]
    percentiles = {f"p{p:02d}": _round_numeric(v) for p, v in zip(keys, percentile_values)}
    distribution, frequencies, values = _approximate_distribution_via_top_n(
        cursor,
        source,
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
    """DATE/DATE32/DATETIME/DATETIME64: bounds and percentiles render to text in SQL, and
    `quantized_count` is omitted for `Date`, where every value already is its own day.
    """

    cn = _quote_ident(col.physical_name or col.name)
    keys = config.percentiles
    day_aligned = not _matches(col.sql_type, ("date", "date32"))
    # quantileExactInclusive rejects Date/DateTime directly (measured), so every temporal value
    # is reduced to a float first and rendered back through the column's own domain.
    if day_aligned:
        percentile_exprs = ", ".join(
            _render_temporal(
                f"toDateTime64(quantileExactInclusive({p / 100.0})(toFloat64({cn})), 6)",
            )
            for p in keys
        )
    else:
        percentile_exprs = ", ".join(
            f"toString(toDate32(toInt32(round("
            f"quantileExactInclusive({p / 100.0})(toFloat64({cn}))))))"
            for p in keys
        )
    select_parts = [
        _render_temporal(f"min({cn})") if day_aligned else f"toString(min({cn}))",
        _render_temporal(f"max({cn})") if day_aligned else f"toString(max({cn}))",
        # Elapsed whole days, not calendar-day boundaries crossed - `dateDiff('day', ...)`
        # would count a span crossing midnight as 1 even when under 24 hours elapsed.
        f"intDiv(dateDiff('second', min({cn}), max({cn})), 86400)",
        percentile_exprs,
    ]

    if day_aligned:
        select_parts.append(
            f"countIf(toStartOfDay(toDateTime({cn})) = toDateTime({cn})) AS quant",
        )

    row = exec_query(cursor, f"SELECT {', '.join(select_parts)} FROM {source}").fetchone()

    if row is None:
        empty_range = Range(min=None, max=None, span_days=0)

        return empty_range, {}, "uniform", (), summarize_frequencies([]), (), None

    span_days = int(row[2]) if row[2] is not None else 0
    rng = Range(min=row[0], max=row[1], span_days=span_days)
    percentile_values = row[3 : 3 + len(keys)]
    percentiles = {f"p{p:02d}": v for p, v in zip(keys, percentile_values)}
    quantized_count = int(row[3 + len(keys)]) if day_aligned else None

    distribution, frequencies, values = _approximate_distribution_via_top_n(
        cursor,
        source,
        _render_temporal(cn) if day_aligned else f"toString({cn})",
        non_null,
        config,
        lambda v: v,
        group_expr=cn,
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


def _percentile_exprs(cn: str, keys: Sequence[int]) -> str:
    """Comma-joined scalar `quantileExactInclusive(p)(col)` calls, one per requested percentile -
    the array form round-trips as a repr, and plain `quantileExact` is nearest-rank instead.
    """

    return ", ".join(f"quantileExactInclusive({p / 100.0})(toFloat64({cn}))" for p in keys)


def _fetch_length_p95(cursor: Cursor, source: str, col: ColumnMeta) -> float | None:
    """P95 character length (SPEC 2.2.4), via `quantileExactInclusive` over `lengthUTF8`."""

    cn = _quote_ident(col.physical_name or col.name)
    row = exec_query(
        cursor,
        f"SELECT quantileExactInclusive(0.95)(lengthUTF8(toString({cn}))) "
        f"FROM {source} WHERE {cn} IS NOT NULL",
    ).fetchone()

    return _round_numeric(row[0]) if row is not None else None


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

    n = config.top_n_values
    grouping = group_expr or select_expr
    rows = exec_query(
        cursor,
        f"""
        SELECT {select_expr} AS rendered, count() AS cnt
        FROM {source}
        WHERE {grouping} IS NOT NULL
        GROUP BY {grouping}
        ORDER BY cnt DESC, rendered ASC
        LIMIT {n + 1}
        """,
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
    """Phase A's answer for a table whose batched query yielded no row."""

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
    """Neither json, boolean, temporal, numeric nor unsupported."""

    base = base_type(sql_type)

    return base not in (
        *_UNSUPPORTED_TYPES,
        *_JSON_TYPES,
        *_BOOLEAN_TYPES,
        *_TEMPORAL_TYPES,
        *_NUMERIC_TYPES,
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


def _quote_ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _alias(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name)
