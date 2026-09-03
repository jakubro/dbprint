"""Two-phase batched per-table statistics computation. See ARCHITECTURE.md 2.

Phase B pre-classifies each column to batch its queries by classification group, keeping the
per-table query count small; the engine re-applies SPEC 3.2 independently, and the adapter
NEVER stamps `classification`. Approximate methods activate above `APPROXIMATE_THRESHOLD`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

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
from .connection import exec_query
from .introspect import composite_columns, reltuples_estimate
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


if TYPE_CHECKING:
    import psycopg


APPROXIMATE_THRESHOLD = 1_000_000

# Ratio at/above which a column is re-counted exactly; the same constant Snowflake uses, since
# pg_stats.n_distinct is a stored planner statistic of unbounded staleness, not a live sketch.
_EXACT_PROBE_RATIO = 0.85

# Range the sampling seed reduces into; Postgres bounds the seed nowhere, so dbprint sets it.
SEED_MODULUS = 2**31

_NUMERIC_TYPES = (
    "smallint",
    "integer",
    "bigint",
    "decimal",
    "numeric",
    "real",
    "double precision",
    "money",
)

# format_type() always renders the qualified spelling - never bare "time"/"timestamp".
_TEMPORAL_TYPES = (
    "date",
    "timestamp with time zone",
    "timestamp without time zone",
    "time with time zone",
    "time without time zone",
)

# Time-of-day types carry no date: span is always 0 and `_render_calendar_bound` never fires.
_TIME_ONLY_TYPES = ("time", "time with time zone", "time without time zone")

_BOOLEAN_TYPES = ("boolean",)
_JSON_TYPES = ("json", "jsonb")
_UNSUPPORTED_TYPES = ("bytea",)


def compute_base(
    conn: psycopg.Connection,
    fqn: str,
    columns: list[ColumnMeta],
    scope: TableScope | None = None,
) -> tuple[TableCounts, dict[str, BaseStats]]:
    """Phase A: the table's counts plus per-column null_count and cardinality."""

    if not columns:
        return TableCounts(row_count=0, rows_scanned=0), {}

    quoted = _quoted_fqn(fqn)
    source = _table_source(fqn, quoted, scope)
    narrows = scope is not None and scope.narrows

    reltuples = reltuples_estimate(conn, fqn)
    # The planner's n_distinct describes the whole table, so a narrowed read counts instead.
    approximate = reltuples > APPROXIMATE_THRESHOLD and not narrows
    composite = composite_columns(conn, fqn)

    rows_scanned, base_stats = _phase_a(conn, quoted, source, columns, approximate, composite)
    row_count, row_count_method = _table_row_count(conn, quoted, rows_scanned, reltuples, scope)

    return TableCounts(row_count, rows_scanned, row_count_method), base_stats


def compute_columns(
    conn: psycopg.Connection,
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

        return {c.name: _empty_stats(c, base[c.name].supported) for c in columns}

    source = _table_source(fqn, _quoted_fqn(fqn), scope)
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
            supported=base[col.name].supported,
        )
        enriched[col.name] = _phase_b(
            conn,
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
    conn: psycopg.Connection,
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

    source = _table_source(fqn, _quoted_fqn(fqn), scope)
    quoted = [_quote_ident(col.physical_name or col.name) for col in columns]
    cap = config.top_n_null_patterns
    rows = exec_query(
        conn,
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
    conn: psycopg.Connection,
    fqn: str,
    columns: list[ColumnMeta],
    counts: TableCounts,
    candidates: tuple[tuple[str, str], ...],
    scope: TableScope | None = None,
) -> tuple[tuple[str, str], ...]:
    """One batched statement testing every candidate pair. See SPEC 2.2.12.

    Postgres spells multi-column DISTINCT as a row constructor, `COUNT(DISTINCT (a, b))`;
    one expression per candidate, aliased by position so the row maps back to `candidates`.
    """

    if not candidates:
        return ()

    source = _table_source(fqn, _quoted_fqn(fqn), scope)
    physical = {col.name: col.physical_name or col.name for col in columns}
    exprs = [
        f"COUNT(DISTINCT ({_quote_ident(physical[a])}, {_quote_ident(physical[b])}))"
        f" AS {_alias(f'dbprint_grain_{i}')}"
        for i, (a, b) in enumerate(candidates)
    ]
    row = exec_query(conn, f"SELECT {', '.join(exprs)} FROM {source}").fetchone()

    if row is None:
        return ()

    return tuple(pair for i, pair in enumerate(candidates) if row[i] == counts.rows_scanned)


def probe_timeline(
    conn: psycopg.Connection,
    fqn: str,
    columns: list[ColumnMeta],
    counts: TableCounts,
    column: str,
    unit: Literal["day", "week", "month"],
    scope: TableScope | None = None,
) -> tuple[tuple[str, int], ...]:
    """One grouped statement bucketing `column` at `unit` grain (SPEC 2.2.16) - truncation is in
    a CTE, so ordering sorts the real temporal value rather than its rendered text.
    """

    del counts

    source = _table_source(fqn, _quoted_fqn(fqn), scope)
    by_name = {col.name: col for col in columns}
    col = by_name[column]
    cn = _quote_ident(col.physical_name or col.name)
    bucket_expr = _timeline_bucket_expr(cn, col.sql_type, unit)

    rows = exec_query(
        conn,
        f"""
        WITH buckets AS (
            SELECT {bucket_expr} AS bucket_start, COUNT(*) AS cnt
            FROM {source}
            WHERE {cn} IS NOT NULL
            GROUP BY 1
        )
        SELECT {_render_calendar_bound("bucket_start", col.sql_type)} AS bucket_text, cnt
        FROM buckets
        ORDER BY bucket_start
        """,
    ).fetchall()

    return tuple((row[0], int(row[1])) for row in rows)


def _timeline_bucket_expr(cn: str, sql_type: str, unit: str) -> str:
    """Truncation expression for `probe_timeline`'s GROUP BY key (SPEC 2.2.16) - `date_trunc`
    has no `date` overload before Postgres 14, so a DATE column casts to timestamp first.
    """

    if _matches(sql_type, _DATE_ONLY_TYPES):
        return f"date_trunc('{unit}', {cn}::timestamp)"

    return f"date_trunc('{unit}', {cn})"


def compute_populated_windows(
    conn: psycopg.Connection,
    fqn: str,
    columns: list[ColumnMeta],
    counts: TableCounts,
    anchor_column: str,
    subject_columns: tuple[str, ...],
    scope: TableScope | None = None,
) -> dict[str, tuple[str, str]]:
    """One statement, two conditional aggregates per subject column (SPEC 2.2.4) - aggregated in
    a CTE so the outer query renders each bound through the anchor's own domain rule.
    """

    del counts

    if not subject_columns:
        return {}

    source = _table_source(fqn, _quoted_fqn(fqn), scope)
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
        agg_exprs.append(
            f"MAX(CASE WHEN {subject_cn} IS NOT NULL THEN {anchor_cn} END) AS to_{i}",
        )
        outer_exprs.append(
            f"{_render_calendar_bound(f'from_{i}', anchor.sql_type)} AS from_{i}_text",
        )
        outer_exprs.append(f"{_render_calendar_bound(f'to_{i}', anchor.sql_type)} AS to_{i}_text")

    row = exec_query(
        conn,
        f"""
        WITH agg AS (
            SELECT {", ".join(agg_exprs)} FROM {source}
        )
        SELECT {", ".join(outer_exprs)} FROM agg
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
    conn: psycopg.Connection,
    fqn: str,
    columns: list[ColumnMeta],
    counts: TableCounts,
    base: dict[str, BaseStats],
    candidates: tuple[tuple[str, str], ...],
    scope: TableScope | None = None,
) -> dict[tuple[str, str], float]:
    """One batched statement measuring every candidate pair's joint cardinality. See SPEC 2.2.13.

    Phase A already measured `base[determinant].cardinality`, so only the joint
    `COUNT(DISTINCT (a, b))` needs a fresh statement here.
    """

    del counts

    if not candidates:
        return {}

    source = _table_source(fqn, _quoted_fqn(fqn), scope)
    physical = {col.name: col.physical_name or col.name for col in columns}
    exprs = [
        f"COUNT(DISTINCT ({_quote_ident(physical[a])}, {_quote_ident(physical[b])}))"
        f" AS {_alias(f'dbprint_dep_{i}')}"
        for i, (a, b) in enumerate(candidates)
    ]
    row = exec_query(conn, f"SELECT {', '.join(exprs)} FROM {source}").fetchone()

    if row is None:
        return {}

    out: dict[tuple[str, str], float] = {}

    for i, (a, b) in enumerate(candidates):
        joint = row[i]

        if joint:
            out[(a, b)] = min(1.0, base[a].cardinality / joint)

    return out


def materialize(conn: psycopg.Connection, fqn: str, scope: TableScope) -> TableScope:
    """Copy the drawn fraction into a session-lifetime temp table and name it on the scope.

    Autocommit makes the CREATE outlive its own statement. The name stays unqualified: a
    temp table lives in `pg_temp`, and qualifying it addresses a different schema.
    """

    name = materialized_name(fqn)
    quoted = _quoted_fqn(fqn)
    drawn = _source(quoted, scope, seed_from_fqn(fqn, SEED_MODULUS))
    exec_query(
        conn,
        f"CREATE TEMPORARY TABLE {_quote_ident(name)} AS SELECT * FROM {drawn}",
    )

    return replace(scope, materialized=name)


def release(conn: psycopg.Connection, scope: TableScope) -> None:
    """Drop the copied sample; the session would drop it anyway, this frees it sooner."""

    if scope.materialized is None:
        return

    exec_query(conn, f"DROP TABLE IF EXISTS {_quote_ident(scope.materialized)}")


def _quoted_fqn(fqn: str) -> str:
    schema, _, table = fqn.partition(".")

    return _quote_qualified(schema, table)


def _table_source(fqn: str, quoted: str, scope: TableScope | None) -> str:
    """The FROM expression every phase reads.

    A materialized scope names one copied draw; unmaterialized, the seed re-derives from
    the table's own name, so every phase builds the same text.
    """

    return _source(quoted, scope, seed_from_fqn(fqn, SEED_MODULUS))


def _source(quoted_fqn: str, scope: TableScope | None, seed: int | None = None) -> str:
    """Table reference every statistics query selects FROM. See ARCHITECTURE.md 2.

    A materialized scope is already the drawn rows and reads as a plain name. TABLESAMPLE binds
    to a base table and needs no wrapper; a predicate gets one. `REPEATABLE` makes every
    statement read the same rows, and BERNOULLI hashes (block, offset, seed), so a smaller rate
    under the same seed yields a subset of the larger draw.
    """

    if scope is None or not scope.narrows:
        return quoted_fqn
    elif scope.materialized is not None:
        return _quote_ident(scope.materialized)
    elif scope.sample is not None:
        repeatable = "" if seed is None else f" REPEATABLE ({seed})"

        return f"{quoted_fqn} TABLESAMPLE BERNOULLI({scope.sample * 100}){repeatable}"
    else:
        return f"(SELECT * FROM {quoted_fqn} WHERE {scope.filter}) AS dbprint_scoped"


def _table_row_count(
    conn: psycopg.Connection,
    quoted_fqn: str,
    rows_scanned: int,
    reltuples: float,
    scope: TableScope | None,
) -> tuple[int, RowCountMethod]:
    """Rows in the table and how they were obtained, per SPEC 2.2.1.

    A narrowed read takes the planner estimate; a never-analyzed table has none and counts
    exactly, since the scanned figure would report a filter matching nothing as an empty
    table (SPEC 2.2.7). An estimate below the scanned count still stands (SPEC 2.2.8).
    """

    if scope is None or not scope.narrows:
        return rows_scanned, "exact"

    if reltuples >= 0:
        return int(reltuples), "approximate"

    row = exec_query(conn, f"SELECT COUNT(*) FROM {quoted_fqn}").fetchone()

    return (int(row[0]) if row and row[0] is not None else rows_scanned), "exact"


def _phase_a(
    conn: psycopg.Connection,
    quoted_fqn: str,
    source: str,
    columns: list[ColumnMeta],
    approximate: bool,
    composite: frozenset[str],
) -> tuple[int, dict[str, BaseStats]]:
    """One query yielding row_count + per-column null_count + cardinality."""

    select_parts: list[str] = ["COUNT(*) AS row_count"]

    for col in columns:
        cn = _quote_ident(col.physical_name or col.name)
        select_parts.append(f"COUNT(*) FILTER (WHERE {cn} IS NULL) AS null_{_alias(col.name)}")

        if _matches(col.sql_type, _NUMERIC_TYPES):
            select_parts.append(f"COUNT(*) FILTER (WHERE {cn} = 0) AS zero_{_alias(col.name)}")
            select_parts.append(f"COUNT(*) FILTER (WHERE {cn} < 0) AS neg_{_alias(col.name)}")
            select_parts.append(
                f"COUNT(*) FILTER (WHERE {cn} = TRUNC({cn})) AS quant_{_alias(col.name)}",
            )
        elif _is_string_like(col.sql_type):
            select_parts.append(
                f"COUNT(*) FILTER (WHERE CAST({cn} AS text) = '') AS empty_{_alias(col.name)}",
            )
            length_expr = f"LENGTH(CAST({cn} AS text))"
            select_parts.append(f"MIN({length_expr}) AS lenmin_{_alias(col.name)}")
            select_parts.append(f"MAX({length_expr}) AS lenmax_{_alias(col.name)}")
            select_parts.append(f"AVG({length_expr}) AS lenavg_{_alias(col.name)}")
            select_parts.append(
                f"PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY {length_expr}) "
                f"AS lenp95_{_alias(col.name)}",
            )

        if not approximate:
            select_parts.append(f"COUNT(DISTINCT {cn}) AS card_{_alias(col.name)}")

    sql = f"SELECT {', '.join(select_parts)} FROM {source}"
    row = exec_query(conn, sql).fetchone()

    if row is None:
        return 0, {c.name: _empty_base(c, composite) for c in columns}

    row_count = int(row[0])
    out: dict[str, BaseStats] = {}
    idx = 1

    for col in columns:
        null_count = int(row[idx])
        idx += 1
        zero_count = negative_count = empty_count = quantized_count = None
        length_min = length_max = length_avg = length_p95 = None

        if _matches(col.sql_type, _NUMERIC_TYPES):
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
            length_p95 = row[idx]
            idx += 1

        method: CardinalityMethod = "exact"

        if approximate:
            estimate = _approximate_cardinality(
                conn,
                quoted_fqn,
                col.physical_name or col.name,
                row_count,
                null_count,
            )

            if estimate is None:
                cardinality = _exact_cardinality(conn, source, col.physical_name or col.name)
            else:
                cardinality = estimate
                method = "approximate"
        else:
            cardinality = int(row[idx])
            idx += 1

        out[col.name] = BaseStats(
            null_count=null_count,
            cardinality=cardinality,
            cardinality_method=method,
            supported=not _is_unsupported(col.sql_type) and col.name not in composite,
            zero_count=zero_count,
            negative_count=negative_count,
            empty_count=empty_count,
            quantized_count=quantized_count,
            length_min=length_min,
            length_max=length_max,
            length_avg=length_avg,
            length_p95=length_p95,
        )

    if approximate:
        _settle_near_unique(conn, source, columns, out, row_count)

    return row_count, out


def _exact_cardinality(conn: psycopg.Connection, source: str, col_name: str) -> int:
    """Count distinct values for one column, when the planner has no estimate."""

    cn = _quote_ident(col_name)
    row = exec_query(conn, f"SELECT COUNT(DISTINCT {cn}) FROM {source}").fetchone()

    return int(row[0]) if row and row[0] is not None else 0


def _settle_near_unique(
    conn: psycopg.Connection,
    source: str,
    columns: list[ColumnMeta],
    base: dict[str, BaseStats],
    row_count: int,
) -> None:
    """Re-count, exactly, the columns a stale planner estimate could misclassify.

    SPEC 4.2's candidate-key threshold sits at ratio 0.9999, and `pg_stats.n_distinct` is a
    stored statistic of unbounded staleness, so an estimate a fraction low costs a primary
    key its `candidate_key`. Only near-unique columns are re-counted.
    """

    near_unique = [
        col
        for col in columns
        if row_count and base[col.name].cardinality / row_count >= _EXACT_PROBE_RATIO
    ]

    if not near_unique:
        return

    select_parts = [
        f"COUNT(DISTINCT {_quote_ident(col.physical_name or col.name)}) AS {_alias(f'card_{col.name}')}"
        for col in near_unique
    ]
    sql = f"SELECT {', '.join(select_parts)} FROM {source}"
    row = exec_query(conn, sql).fetchone()

    if row is None:
        return

    for col, value in zip(near_unique, row):
        # A separate statement may read a different snapshot than phase A, so clamp to non_null.
        base[col.name] = replace(
            base[col.name],
            cardinality=min(row_count - base[col.name].null_count, int(value)),
            cardinality_method="exact",
        )


def _approximate_cardinality(
    conn: psycopg.Connection,
    quoted_fqn: str,
    col_name: str,
    row_count: int,
    null_count: int,
) -> int | None:
    """Distinct-count estimate from the planner, or None when it has none.

    None is the absence of an estimate, not a zero count; the caller then counts exactly.
    """

    schema, table = quoted_fqn.replace('"', "").split(".", 1)
    row = exec_query(
        conn,
        """
        SELECT n_distinct
        FROM pg_stats
        WHERE schemaname = %s AND tablename = %s AND attname = %s
        """,
        (schema, table, col_name),
    ).fetchone()

    if not row or row[0] is None:
        return None

    n_distinct = float(row[0])
    non_null = row_count - null_count

    # Negative n_distinct is the negated row fraction, nulls included per pg_stats;
    # `cardinality` is non-null-only (SPEC 2.2.2), so clamp to non_null.
    if n_distinct < 0:
        return min(non_null, round(-n_distinct * row_count))

    return min(non_null, int(n_distinct))


def _empty_stats(col: ColumnMeta, supported: bool = True) -> ColumnStats:
    """SPEC 2.2.7 edge case: a table read in full and found empty -> minimal column stats.

    A narrowed read that drew nothing is a different condition and never reaches here.
    `supported` is Phase A's verdict, the same signal `_pre_classify` defers to.
    """

    if not supported or _is_unsupported(col.sql_type):
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
    conn: psycopg.Connection,
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
            avg=_round_numeric(base.length_avg),
            p95=_round_numeric(base.length_p95),
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
        values, coverage, _ = _fetch_value_list(conn, source, col, non_null, config)

        return _replace(stats, values=values, values_coverage=coverage)

    if pre in ("categorical", "foreign_key_candidate"):
        values, coverage, exhaustive = _fetch_value_list(conn, source, col, non_null, config)
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
            conn,
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
                _fetch_temporal_block(conn, source, col, non_null, config)
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

    # pre == "text": the only suppressible classification; `distribution` goes with the list.
    if suppressed:
        return stats

    values, coverage, exhaustive = _fetch_value_list(conn, source, col, non_null, config)
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
    *,
    supported: bool = True,
) -> str:
    """Adapter-internal classification mirroring the engine's SPEC 3.2 logic.

    Steers Phase B query selection only; the engine re-classifies independently and both MUST
    agree. Uniqueness plays no part (SPEC 4.2). `supported` comes from Phase A rather than the
    type name, since a catalog fact like a composite column's type has no name to match.
    """

    if not supported or _is_unsupported(col.sql_type):
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
    conn: psycopg.Connection,
    source: str,
    col: ColumnMeta,
    non_null: int,
    config: StatisticsConfig,
) -> tuple[tuple[ValueCount, ...], float, bool]:
    """Ordered value list, its coverage, and whether it enumerates the column.

    Fetches one row beyond the cap so truncation is observed, not predicted from a cardinality
    that may be an estimate (SPEC 2.2.4). A tz-bearing timestamp routes through the same
    UTC-pinning renderer as `range`, so its literals never carry the session zone.
    """

    cn = _quote_ident(col.physical_name or col.name)
    n = config.top_n_values
    select_expr = (
        _render_calendar_bound(cn, col.sql_type) if _matches(col.sql_type, _TZ_TYPES) else cn
    )
    rows = exec_query(
        conn,
        f"""
        SELECT {select_expr} AS rendered, COUNT(*) AS cnt
        FROM {source}
        WHERE {cn} IS NOT NULL
        GROUP BY {cn}
        ORDER BY cnt DESC, CAST({select_expr} AS text) ASC
        LIMIT %s
        """,
        (n + 1,),
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
    conn: psycopg.Connection,
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
    percentile_keys = config.percentiles
    pct_select = ", ".join(
        f"percentile_cont({p / 100.0}::double precision) WITHIN GROUP (ORDER BY {cn}) AS p_{p:02d}"
        for p in percentile_keys
    )
    row = exec_query(
        conn,
        f"SELECT MIN({cn}) AS mn, MAX({cn}) AS mx, AVG({cn}) AS avg_val, SUM({cn}) AS sum_val, "
        f"{pct_select} FROM {source} WHERE {cn} IS NOT NULL",
    ).fetchone()

    if row is None:
        empty_range = Range(min=None, max=None)

        return empty_range, {}, "uniform", summarize_frequencies([]), (), None, None

    rng = Range(
        min=_round_numeric(row[0], exact_int=True),
        max=_round_numeric(row[1], exact_int=True),
    )
    mean = _round_numeric(row[2])
    total = _round_numeric(row[3], exact_int=True)
    percentiles = {f"p{p:02d}": _round_numeric(v) for p, v in zip(percentile_keys, row[4:])}
    distribution, frequencies, values = _approximate_distribution_via_top_n(
        conn,
        source,
        cn,
        cn,
        non_null,
        config,
        _round_numeric,
    )

    return rng, percentiles, distribution, frequencies, values, mean, total


# A raw fetch of a calendar type can hand psycopg a value it refuses to build: infinity, or a
# year outside 0001-9999. TIME and TIME WITH TIME ZONE carry neither and fetch unchanged.
_TZ_TYPES = ("timestamp with time zone",)
_DATE_ONLY_TYPES = ("date",)

# UTC bounds a calendar value is clamped to before date arithmetic; inside this window,
# subtracting an infinite timestamp cannot raise `DatetimeFieldOverflow` server-side.
_EPOCH_FLOOR = "0001-01-01T00:00:00Z"
_EPOCH_CEIL = "9999-12-31T23:59:59Z"


def _fetch_temporal_block(
    conn: psycopg.Connection,
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
    if _matches(col.sql_type, _TIME_ONLY_TYPES):
        return _fetch_clock_temporal_block(conn, source, col, non_null, config)

    return _fetch_calendar_temporal_block(conn, source, col, non_null, config)


def _fetch_clock_temporal_block(
    conn: psycopg.Connection,
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
    """TIME / TIME WITH TIME ZONE fetch path - neither carries a year, an `infinity` sentinel or
    a date to truncate to (SPEC 2.2.4), so span is 0 and `quantized_count` always absent.
    """

    cn = _quote_ident(col.physical_name or col.name)
    percentile_keys = config.percentiles
    # percentile_disc takes any sortable type; percentile_cont only double precision/interval.
    pct_select = ", ".join(
        f"percentile_disc({p / 100.0}::double precision) WITHIN GROUP (ORDER BY {cn}) AS p_{p:02d}"
        for p in percentile_keys
    )
    row = exec_query(
        conn,
        f"""
        SELECT MIN({cn}) AS mn, MAX({cn}) AS mx, {pct_select}
        FROM {source}
        WHERE {cn} IS NOT NULL
        """,
    ).fetchone()

    if row is None:
        empty_range = Range(min=None, max=None, span_days=0)

        return empty_range, {}, "uniform", (), summarize_frequencies([]), (), None

    rng = Range(min=_iso_or_value(row[0]), max=_iso_or_value(row[1]), span_days=0)
    percentiles = {f"p{p:02d}": _iso_or_value(v) for p, v in zip(percentile_keys, row[2:])}
    distribution, frequencies, values = _approximate_distribution_via_top_n(
        conn,
        source,
        cn,
        cn,
        non_null,
        config,
        _iso_or_value,
    )

    return rng, percentiles, distribution, (), frequencies, values, None


def _fetch_calendar_temporal_block(
    conn: psycopg.Connection,
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
    """DATE / TIMESTAMP[TZ]: bounds and percentiles render to text in SQL.

    The CTE aggregates real values first, since rendered text does not sort like a temporal
    value; the outer query renders once, so no raw value reaches a fetch.
    """

    cn = _quote_ident(col.physical_name or col.name)
    percentile_keys = config.percentiles
    date_only = _matches(col.sql_type, _DATE_ONLY_TYPES)
    cast_type = (
        "timestamptz" if _matches(col.sql_type, _TZ_TYPES) else "date" if date_only else "timestamp"
    )
    # A DATE value is always its own day-truncation (SPEC 2.2.3): the count would be a
    # truism, so `quantized_count` is omitted entirely rather than published as a constant.
    day_aligned = not date_only

    agg_select = ", ".join(
        [f"MIN({cn}) AS mn", f"MAX({cn}) AS mx"]
        + (
            [f"COUNT(*) FILTER (WHERE {cn} = date_trunc('day', {cn})) AS quant"]
            if day_aligned
            else []
        )
        + [
            f"percentile_disc({p / 100.0}::double precision) WITHIN GROUP (ORDER BY {cn}) AS p_{p:02d}"
            for p in percentile_keys
        ],
    )
    percentile_renders = [
        (f"p{p:02d}", _render_calendar_bound(f"p_{p:02d}", col.sql_type)) for p in percentile_keys
    ]

    lo, hi = f"'{_EPOCH_FLOOR}'::{cast_type}", f"'{_EPOCH_CEIL}'::{cast_type}"
    clamped_mn = f"LEAST(GREATEST(mn, {lo}), {hi})::timestamptz"
    clamped_mx = f"LEAST(GREATEST(mx, {lo}), {hi})::timestamptz"
    # GREATEST/LEAST ignore a NULL argument rather than propagating it, so an empty column
    # would otherwise clamp to a real bound and report a span for data that does not exist.
    span_days = (
        f"CASE WHEN mn IS NULL THEN NULL ELSE "
        f"FLOOR(EXTRACT(EPOCH FROM ({clamped_mx} - {clamped_mn})) / 86400) END"
    )

    outer_select = [
        f"{_render_calendar_bound('mn', col.sql_type)} AS mn_text",
        f"{_render_calendar_bound('mx', col.sql_type)} AS mx_text",
        *(f"{expr} AS {key}_text" for key, expr in percentile_renders),
        f"{span_days} AS span_days",
        *(["quant"] if day_aligned else []),
    ]

    row = exec_query(
        conn,
        f"""
        WITH agg AS (
            SELECT {agg_select} FROM {source} WHERE {cn} IS NOT NULL
        )
        SELECT {", ".join(outer_select)} FROM agg
        """,
    ).fetchone()

    if row is None:
        empty_range = Range(min=None, max=None, span_days=0)

        return empty_range, {}, "uniform", (), summarize_frequencies([]), (), None

    n_pct = len(percentile_keys)
    pct_texts = row[2 : 2 + n_pct]
    span_raw = row[2 + n_pct]
    quantized_count = int(row[2 + n_pct + 1]) if day_aligned else None

    # Floored in SQL per SPEC 2.2.4; this only narrows the type.
    span_days_val = int(span_raw) if span_raw is not None else 0
    rng = Range(min=row[0], max=row[1], span_days=span_days_val)
    percentiles = {key: text for (key, _), text in zip(percentile_renders, pct_texts)}

    # Same rendering the bounds above already went through - a raw fetch of the group key
    # risks the driver overflow `_render_calendar_bound` exists to avoid (see module note).
    distribution, frequencies, values = _approximate_distribution_via_top_n(
        conn,
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

    A tz column converts via `AT TIME ZONE 'UTC'`, not `to_char`'s TZH/TZM tokens, which
    shift with the session TimeZone GUC. `to_char` returns NULL for `infinity` and always
    appends an AD/BC era token, so both are handled outside the picture string.
    """

    is_tz = _matches(sql_type, _TZ_TYPES)
    is_date_only = _matches(sql_type, _DATE_ONLY_TYPES)
    cast_type = "timestamptz" if is_tz else "date" if is_date_only else "timestamp"
    picture = "YYYY-MM-DD" if is_date_only else 'YYYY-MM-DD"T"HH24:MI:SS.US'
    source_expr = f"{expr} AT TIME ZONE 'UTC'" if is_tz else expr

    body = f"to_char({source_expr}, '{picture}')"

    if not is_date_only:
        body = f"regexp_replace({body}, '\\.000000$', '')"

    if is_tz:
        body = f"{body} || 'Z'"

    era = f"CASE WHEN to_char({expr}, 'BC') = 'BC' THEN ' BC' ELSE '' END"

    return (
        f"CASE "
        f"WHEN {expr} = 'infinity'::{cast_type} THEN 'infinity' "
        f"WHEN {expr} = '-infinity'::{cast_type} THEN '-infinity' "
        f"ELSE {body} || {era} "
        f"END"
    )


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
    conn: psycopg.Connection,
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
        conn,
        f"""
        SELECT {select_expr} AS rendered, COUNT(*) AS cnt
        FROM {source}
        WHERE {group_expr} IS NOT NULL
        GROUP BY {group_expr}
        ORDER BY cnt DESC, CAST({select_expr} AS text) ASC
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


def _empty_base(col: ColumnMeta, composite: frozenset[str]) -> BaseStats:
    """Phase A's answer for a table whose batched query yielded no row.

    `supported` is per column, a property of the type rather than of the empty result.
    """

    return BaseStats(
        null_count=0,
        cardinality=0,
        cardinality_method="exact",
        supported=not _is_unsupported(col.sql_type) and col.name not in composite,
    )


def _is_unsupported(sql_type: str) -> bool:
    base = base_type(sql_type)

    return base in _UNSUPPORTED_TYPES or base.endswith("[]")


def _matches(sql_type: str, types: tuple[str, ...]) -> bool:
    return base_type(sql_type) in types


def _is_string_like(sql_type: str) -> bool:
    """The same type test `_pre_classify` falls through to `text` on, run ahead of cardinality -
    the SQL-type half only; the cardinality-and-FK half is added by that branch itself.
    """

    return not (
        _is_unsupported(sql_type)
        or _matches(sql_type, _BOOLEAN_TYPES)
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
    if v is None:
        return None

    iso = getattr(v, "isoformat", None)

    if callable(iso):
        s = iso()

        return s.replace("+00:00", "Z") if s.endswith("+00:00") else s

    return v


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _quote_qualified(schema: str, table: str) -> str:
    return f"{_quote_ident(schema)}.{_quote_ident(table)}"


def _alias(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name)


def _replace(stats: ColumnStats, **kwargs: Any) -> ColumnStats:

    return replace(stats, **kwargs)
