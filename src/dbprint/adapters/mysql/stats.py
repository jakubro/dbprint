"""Two-phase batched per-table statistics computation for MySQL. See ARCHITECTURE.md 2.

Phase B pre-classifies each column internally, mirroring the engine's SPEC 3.2 order; both
MUST converge, and the adapter NEVER stamps `classification`.

MySQL has no `COUNT(*) FILTER` or `WITHIN GROUP`: nulls use `COUNT(*) - COUNT(col)`, and
percentiles use a ranked derived table with `CEIL(p * n)` (percentile_disc semantics).
Cardinality is always `COUNT(DISTINCT col)`, so `cardinality_method` always publishes `exact`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import Any, Literal

from dbprint.config import StatisticsConfig
from dbprint.spec.classification import (
    base_type,
    compute_cardinality_ratio,
    compute_null_rate,
)
from dbprint.spec.coverage import coverage_share
from dbprint.spec.distribution import classify as classify_distribution
from dbprint.spec.distribution import summarize as summarize_frequencies
from dbprint.spec.temporal_range import is_representable
from .connection import Cursor, exec_query
from .introspect import table_rows_estimate
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
    seed_from_fqn,
    temporal_block_unmeasured,
)


# Reduction range for the sampling seed - a safe width, not a documented MySQL limit.
SEED_MODULUS = 2**31


_NUMERIC_TYPES = (
    "tinyint",
    "smallint",
    "mediumint",
    "int",
    "integer",
    "bigint",
    "decimal",
    "dec",
    "numeric",
    "fixed",
    "float",
    "double",
    "real",
)

_TEMPORAL_TYPES = (
    "date",
    "datetime",
    "timestamp",
    "time",
    "year",
)

# YEAR yields NULL in CAST-to-DATE, DATEDIFF and TIMESTAMPDIFF; `MAKEDATE(y, 1)` converts it.
_YEAR_TYPES = ("year",)

# Time of day, no date: TIME comes back as a `timedelta` offset, so date arithmetic is undefined.
_TIME_ONLY_TYPES = ("time",)

_DATE_ONLY_TYPES = ("date",)

# TIMESTAMP is stored UTC and converted to the session `time_zone` on read; DATETIME is naive.
_TZ_TYPES = ("timestamp",)

_JSON_TYPES = ("json",)

# MySQL has no native BOOLEAN - `tinyint(1)` is the width-preserving spelling for one, which
# `_is_boolean` reads raw. Named singular so the plural-keyed registry sweep skips it.
_BOOLEAN_TYPE = "tinyint(1)"

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
)


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
            # A narrowed read drew nothing; an exact, exhaustive shape for columns
            # nobody read would overclaim (SPEC 2.2.7).
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
        LIMIT %s
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
    """One batched statement testing every candidate pair. See SPEC 2.2.12.

    MySQL spells multi-column DISTINCT as a plain argument list, no row-constructor parens;
    one expression per candidate, aliased by position so the row maps back to `candidates`.
    """

    if not candidates:
        return ()

    source = _table_source(fqn, _quote_qualified(fqn), scope)
    exprs = [
        f"COUNT(DISTINCT {_quote_ident(a)}, {_quote_ident(b)}) AS dbprint_grain_{i}"
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
    """One grouped statement bucketing `column` at `unit` grain (SPEC 2.2.16) - grouping is on
    the truncated value, which is always a bare DATE, so the outer render is `DATE_FORMAT`.
    """

    del counts

    source = _table_source(fqn, _quote_qualified(fqn), scope)
    by_name = {col.name: col for col in columns}
    col = by_name[column]
    cn = _quote_ident(col.name)
    bucket_expr = _timeline_bucket_expr(cn, col.sql_type, unit)

    rows = exec_query(
        cursor,
        f"""
        SELECT DATE_FORMAT(bucket_start, '%Y-%m-%d') AS bucket_text, cnt
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
    """Truncation expression for `probe_timeline`'s GROUP BY key (SPEC 2.2.16) - MySQL has no
    `date_trunc`, and a TIMESTAMP normalizes to UTC first; the result is always a bare DATE.
    """

    is_timestamp = _matches(sql_type, _TZ_TYPES)
    normalized = f"CONVERT_TZ({cn}, @@session.time_zone, '+00:00')" if is_timestamp else cn

    if unit == "day":
        return f"CAST({normalized} AS DATE)"

    if unit == "week":
        return f"DATE_SUB(CAST({normalized} AS DATE), INTERVAL WEEKDAY({normalized}) DAY)"

    return f"DATE_SUB(CAST({normalized} AS DATE), INTERVAL (DAYOFMONTH({normalized}) - 1) DAY)"


def compute_populated_windows(
    cursor: Cursor,
    fqn: str,
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
        agg_exprs.append(
            f"MAX(CASE WHEN {subject_cn} IS NOT NULL THEN {anchor_cn} END) AS to_{i}",
        )
        outer_exprs.append(
            f"{_render_calendar_bound(f'from_{i}', anchor.sql_type)} AS from_{i}_text",
        )
        outer_exprs.append(f"{_render_calendar_bound(f'to_{i}', anchor.sql_type)} AS to_{i}_text")

    row = exec_query(
        cursor,
        f"""
        SELECT {", ".join(outer_exprs)}
        FROM (
            SELECT {", ".join(agg_exprs)} FROM {source}
        ) agg
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
    """One batched statement measuring every candidate pair's joint cardinality. See SPEC 2.2.13.

    Phase A already measured `base[determinant].cardinality`, so only the joint
    `COUNT(DISTINCT a, b)` needs a fresh statement here.
    """

    del columns, counts

    if not candidates:
        return {}

    source = _table_source(fqn, _quote_qualified(fqn), scope)
    exprs = [
        f"COUNT(DISTINCT {_quote_ident(a)}, {_quote_ident(b)}) AS dbprint_dep_{i}"
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
    """Copy the drawn fraction into a session-lifetime temp table and name it on the scope.

    `RAND(seed)` reproduces a row set only within a single reference, so this is the only way
    MySQL reads one draw. A temp table may not be named twice in one statement.
    """

    name = materialized_name(fqn)
    drawn = _source(_quote_qualified(fqn), scope, seed_from_fqn(fqn, SEED_MODULUS))
    exec_query(cursor, f"CREATE TEMPORARY TABLE {_quote_ident(name)} AS SELECT * FROM {drawn}")

    return replace(scope, materialized=name)


def release(cursor: Cursor, scope: TableScope) -> None:
    """Drop the copied sample; `TEMPORARY` is spelled out so no base table can match."""

    if scope.materialized is None:
        return

    exec_query(cursor, f"DROP TEMPORARY TABLE IF EXISTS {_quote_ident(scope.materialized)}")


def _table_source(fqn: str, quoted: str, scope: TableScope | None) -> str:
    """The FROM expression every phase reads.

    A materialized scope names one copied draw; unmaterialized, the seed re-derives from
    the table's own name, so every phase builds the same text.
    """

    return _source(quoted, scope, seed_from_fqn(fqn, SEED_MODULUS))


def _source(quoted_fqn: str, scope: TableScope | None, seed: int | None = None) -> str:
    """Table reference every statistics query selects FROM. See ARCHITECTURE.md 2.

    A materialized scope is already the drawn rows and reads as a plain name. MySQL has no
    TABLESAMPLE, so the other two scope shapes are a predicate in a wrapper, and the seed
    reproduces one row set only while this expression is named once per statement.
    """

    if scope is None or not scope.narrows:
        return quoted_fqn
    elif scope.materialized is not None:
        return _quote_ident(scope.materialized)
    elif scope.sample is not None:
        draw = "RAND()" if seed is None else f"RAND({seed})"

        return f"(SELECT * FROM {quoted_fqn} WHERE {draw} < {scope.sample}) AS dbprint_scoped"
    else:
        return f"(SELECT * FROM {quoted_fqn} WHERE ({scope.filter})) AS dbprint_scoped"


def _table_row_count(
    cursor: Cursor,
    fqn: str,
    quoted_fqn: str,
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

    estimate = table_rows_estimate(cursor, fqn)

    if estimate >= 0:
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
        select_parts.append(f"COUNT(*) - COUNT({cn}) AS null_{_alias(col.name)}")

        if _matches(col.sql_type, _NUMERIC_TYPES) and not _is_boolean(col.sql_type):
            select_parts.append(f"COALESCE(SUM({cn} = 0), 0) AS zero_{_alias(col.name)}")
            select_parts.append(f"COALESCE(SUM({cn} < 0), 0) AS neg_{_alias(col.name)}")
            select_parts.append(
                f"COALESCE(SUM({cn} = TRUNCATE({cn}, 0)), 0) AS quant_{_alias(col.name)}",
            )
        elif _is_string_like(col.sql_type):
            select_parts.append(
                f"COALESCE(SUM(CAST({cn} AS CHAR) = ''), 0) AS empty_{_alias(col.name)}",
            )
            length_expr = f"CHAR_LENGTH(CAST({cn} AS CHAR))"
            select_parts.append(f"MIN({length_expr}) AS lenmin_{_alias(col.name)}")
            select_parts.append(f"MAX({length_expr}) AS lenmax_{_alias(col.name)}")
            select_parts.append(f"AVG({length_expr}) AS lenavg_{_alias(col.name)}")

        select_parts.append(f"COUNT(DISTINCT {cn}) AS card_{_alias(col.name)}")

    sql = f"SELECT {', '.join(select_parts)} FROM {source}"
    row = exec_query(cursor, sql).fetchone()

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

        if _matches(col.sql_type, _NUMERIC_TYPES) and not _is_boolean(col.sql_type):
            zero_count = int(row[idx])
            idx += 1
            negative_count = int(row[idx])
            idx += 1
            quantized_count = int(row[idx])
            idx += 1
        elif _is_string_like(col.sql_type):
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
    """SPEC 2.2.7 edge case: a table read in full and found empty -> minimal column stats.

    A narrowed read that drew nothing is a different condition and never reaches here.
    """

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
    method: CardinalityMethod = "exact"
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
        except Exception:  # noqa: BLE001 - the temporal block degrades as a whole, and its
            # fields are REQUIRED here (SPEC 2.2.3); the column names what the read cost it
            # rather than leaving an absence a reader would read as a structural cause.
            return _replace(stats, unmeasured=temporal_block_unmeasured(col.sql_type))

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
    """Adapter-internal classification mirroring the engine's SPEC 3.2 logic.

    Uniqueness plays no part: the cardinality ratio sits on the `inferred` axis (SPEC 4.2).
    """

    if _is_unsupported(col.sql_type):
        return "unsupported"
    elif _is_boolean(col.sql_type):
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
    """Ordered value list, its coverage, and whether it enumerates the column.

    Fetches one row beyond the cap so truncation is observed, not predicted from a cardinality
    that may be an estimate (SPEC 2.2.4). A TIMESTAMP routes through the same UTC-pinning
    renderer as `range`. The limit is interpolated, not bound: `DATE_FORMAT`'s literal `%`
    sequences would collide with the connector's `%s` substitution.
    """

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
        ORDER BY cnt DESC, CAST({select_expr} AS CHAR) ASC
        LIMIT {int(n) + 1}
        """,
    ).fetchall()
    exhaustive = len(rows) <= n
    # SPEC 2.2.4 ties break on the string form: the cast fixes the cutoff, this sort the order.
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
    sql = (
        f"SELECT MIN({cn}) AS mn, MAX({cn}) AS mx, AVG({cn}) AS avg_val, SUM({cn}) AS sum_val"
        f"{_percentile_select(cn, keys)} "
        f"FROM {_ranked(source, cn)} ranked"
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
    if _matches(col.sql_type, _TIME_ONLY_TYPES) or _matches(col.sql_type, _YEAR_TYPES):
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

    cn = _quote_ident(col.name)
    keys = config.percentiles
    time_only = _matches(col.sql_type, _TIME_ONLY_TYPES)
    earliest = _as_date(f"MIN({cn})", col.sql_type)
    latest = _as_date(f"MAX({cn})", col.sql_type)
    select_parts = [f"MIN({cn}) AS mn", f"MAX({cn}) AS mx"]

    if not time_only:
        span = f"FLOOR(TIMESTAMPDIFF(SECOND, {earliest}, {latest}) / 86400)"
        select_parts.append(f"{span} AS span_days")

    sql = (
        f"SELECT {', '.join(select_parts)}{_percentile_select(cn, keys)} "
        f"FROM {_ranked(source, cn)} ranked"
    )
    row = exec_query(cursor, sql).fetchone()

    if row is None:
        empty_range = Range(min=None, max=None, span_days=0)

        return empty_range, {}, "uniform", (), summarize_frequencies([]), (), None

    # Floored in SQL per SPEC 2.2.4; this only narrows the type.
    span_raw = 0 if time_only else row[2]
    percentile_values = row[2:] if time_only else row[3:]
    span_days = int(span_raw) if span_raw is not None else 0
    rng = Range(min=_iso_or_value(row[0]), max=_iso_or_value(row[1]), span_days=span_days)
    percentiles = {f"p{p:02d}": _iso_or_value(v) for p, v in zip(keys, percentile_values)}

    distribution, frequencies, values = _approximate_distribution_via_top_n(
        cursor,
        source,
        cn,
        cn,
        non_null,
        config,
        _iso_or_value,
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

    cn = _quote_ident(col.name)
    keys = config.percentiles
    # A DATE value is always its own day-truncation (SPEC 2.2.3): the count would be a
    # truism, so `quantized_count` is omitted entirely rather than published as a constant.
    day_aligned = not _matches(col.sql_type, _DATE_ONLY_TYPES)
    agg_select = [f"MIN({cn}) AS mn", f"MAX({cn}) AS mx"]
    agg_select.append(f"FLOOR(TIMESTAMPDIFF(SECOND, MIN({cn}), MAX({cn})) / 86400) AS span_days")

    if day_aligned:
        agg_select.append(f"COALESCE(SUM({cn} = CAST({cn} AS DATE)), 0) AS quant")

    percentile_renders = [
        (f"p{p:02d}", _render_calendar_bound(f"p_{p:02d}", col.sql_type)) for p in keys
    ]
    outer_select = [
        f"{_render_calendar_bound('mn', col.sql_type)} AS mn_text",
        f"{_render_calendar_bound('mx', col.sql_type)} AS mx_text",
        "span_days",
        *(f"{expr} AS {key}_text" for key, expr in percentile_renders),
        *(["quant"] if day_aligned else []),
    ]

    sql = (
        f"SELECT {', '.join(outer_select)} FROM ("
        f"SELECT {', '.join(agg_select)}{_percentile_select(cn, keys)} "
        f"FROM {_ranked(source, cn)} ranked"
        f") agg"
    )
    row = exec_query(cursor, sql).fetchone()

    if row is None:
        empty_range = Range(min=None, max=None, span_days=0)

        return empty_range, {}, "uniform", (), summarize_frequencies([]), (), None

    span_raw = row[2]
    n_pct = len(keys)
    percentile_texts = row[3 : 3 + n_pct]
    quantized_count = int(row[3 + n_pct]) if day_aligned else None

    # Floored in SQL per SPEC 2.2.4; this only narrows the type.
    span_days = int(span_raw) if span_raw is not None else 0
    rng = Range(min=row[0], max=row[1], span_days=span_days)
    percentiles = {key: text for (key, _), text in zip(percentile_renders, percentile_texts)}

    # Rendered the same way the bounds above already were - a raw fetch of the zero-date
    # sentinel silently reads as NULL, which is what `_render_calendar_bound` avoids.
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


def _render_calendar_bound(expr: str, sql_type: str) -> str:
    """SQL text rendering `expr` per SPEC 2.2.4's domain-rendering rule.

    TIMESTAMP is stored UTC and converted to the session `time_zone` on the way out, so it
    is converted back to a fixed UTC offset first; DATE/DATETIME are naive. `%f` always
    renders six digits, stripped to match `isoformat()`'s all-or-nothing form.
    """

    is_timestamp = _matches(sql_type, _TZ_TYPES)
    is_date_only = _matches(sql_type, _DATE_ONLY_TYPES)
    picture = "%Y-%m-%d" if is_date_only else "%Y-%m-%dT%H:%i:%s.%f"
    source_expr = f"CONVERT_TZ({expr}, @@session.time_zone, '+00:00')" if is_timestamp else expr
    body = f"DATE_FORMAT({source_expr}, '{picture}')"

    if not is_date_only:
        body = f"REGEXP_REPLACE({body}, '\\\\.000000$', '')"

    return body


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

    return f"MAKEDATE({aggregate}, 1)" if _matches(sql_type, _YEAR_TYPES) else aggregate


def _ranked(source: str, quoted_col: str) -> str:
    """Derived table carrying each non-null value with its rank and the total.

    Names the source once, so every percentile reads the same row set; per-percentile, a
    sampled read would draw an independent set and land a percentile outside its own range.
    The computed columns are prefixed so a bare `n`/`rn` cannot collide with a real column.
    """

    return (
        f"(SELECT {quoted_col}, "
        f"ROW_NUMBER() OVER (ORDER BY {quoted_col}) AS dbprint_rn, "
        f"COUNT(*) OVER () AS dbprint_n "
        f"FROM {source} WHERE {quoted_col} IS NOT NULL)"
    )


def _percentile_select(quoted_col: str, keys: Sequence[int]) -> str:
    """Comma-prefixed percentile_disc projections over the ranked derived table.

    `CEIL(p * n)` is the percentile_disc rank, so the result is always a value the column
    holds, from the same scan as the range beside it. MySQL has no ordered-set aggregate,
    so this reproduces what Postgres/Snowflake get from `PERCENTILE_DISC`.
    """

    if not keys:
        return ""

    parts = [
        f"MIN(CASE WHEN dbprint_rn >= CEIL({p / 100.0} * dbprint_n) "
        f"THEN {quoted_col} END) AS p_{p:02d}"
        for p in keys
    ]

    return ", " + ", ".join(parts)


def _fetch_length_p95(cursor: Cursor, source: str, col: ColumnMeta) -> float | None:
    """P95 character length (SPEC 2.2.4), ranked the same way numeric percentiles are - but with
    an explicit alias, the shared helpers repeating an expression only a bare column resolves.
    """

    cn = _quote_ident(col.name)
    length_expr = f"CHAR_LENGTH(CAST({cn} AS CHAR))"
    sql = (
        "SELECT MIN(CASE WHEN dbprint_rn >= CEIL(0.95 * dbprint_n) THEN dbprint_len END) "
        f"FROM (SELECT {length_expr} AS dbprint_len, "
        f"ROW_NUMBER() OVER (ORDER BY {length_expr}) AS dbprint_rn, "
        f"COUNT(*) OVER () AS dbprint_n "
        f"FROM {source} WHERE {cn} IS NOT NULL) ranked"
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
    value_transform: Callable[[Any], Any],
) -> tuple[Distribution, Frequencies, tuple[ValueCount, ...]]:
    """Distribution, frequencies, and the same top-N rows `values` publishes (SPEC 2.2.3) -
    grouping stays on the raw column, so a rendered expression cannot split one value in two.
    """

    n = config.top_n_values
    rows = exec_query(
        cursor,
        f"""
        SELECT {select_expr} AS rendered, COUNT(*) AS cnt
        FROM {source}
        WHERE {group_expr} IS NOT NULL
        GROUP BY {group_expr}
        ORDER BY cnt DESC, CAST({select_expr} AS CHAR) ASC
        LIMIT {int(n) + 1}
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
    """Phase A's answer for a table whose batched query yielded no row.

    `supported` is per column, a property of the type rather than of the empty result.
    """

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


def _is_boolean(sql_type: str) -> bool:
    """`col.sql_type` is `column_type` here, keeping the display width `base_type()` strips -
    `tinyint(1)` is a declared BOOLEAN's spelling, so this compares the raw string.
    """

    return sql_type.strip().lower() == _BOOLEAN_TYPE


def _is_string_like(sql_type: str) -> bool:
    """The SQL-type half of the test `_pre_classify` falls through to `text` on - a `tinyint(1)`
    boolean's `base_type()` is already in `_NUMERIC_TYPES`, so this returns `False` for it.
    """

    return not (
        _is_unsupported(sql_type)
        or _matches(sql_type, _JSON_TYPES)
        or _matches(sql_type, _TEMPORAL_TYPES)
        or _matches(sql_type, _NUMERIC_TYPES)
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
    """Render a driver value in the column's own domain, per SPEC 2.2.4."""

    if v is None:
        return None

    if isinstance(v, timedelta):
        # TIME is an offset from midnight reaching +/-838:59:59, so hours stay unwrapped.
        microseconds = (v.days * 86400 + v.seconds) * 1_000_000 + v.microseconds
        sign = "-" if microseconds < 0 else ""
        seconds, fraction = divmod(abs(microseconds), 1_000_000)
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        clock = f"{sign}{hours:02d}:{minutes:02d}:{seconds:02d}"

        return f"{clock}.{fraction:06d}" if fraction else clock

    iso = getattr(v, "isoformat", None)

    if callable(iso):
        s = iso()

        return s.replace("+00:00", "Z") if s.endswith("+00:00") else s

    return v


def _quote_ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _quote_qualified(fqn: str) -> str:
    database, _, table = fqn.partition(".")

    return f"{_quote_ident(database.strip('`'))}.{_quote_ident(table.strip('`'))}"


def _alias(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name)


def _replace(stats: ColumnStats, **kwargs: Any) -> ColumnStats:

    return replace(stats, **kwargs)
