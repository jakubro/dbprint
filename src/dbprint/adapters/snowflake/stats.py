"""Two-phase batched per-table statistics computation. See ARCHITECTURE.md 2.

Phase A pre-classifies internally to steer Phase B; the engine re-applies SPEC 3.2
independently, and both MUST converge. Snowflake's ordered-set aggregates resolve against
a fixed-point numeric, so PERCENTILE_DISC is unreachable there and this adapter runs
a ranked scan for temporal percentiles. Driver-native scalars are normalized wherever
they enter the artifact: a temporal column at or below the enumeration threshold reaches
the `values` map as a key, and SPEC 2.2.4 restricts those to strings, numbers, booleans.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
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
from . import introspect
from .connection import Cursor, exec_query
from .identity import Identity
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


# Threshold above which cardinality is estimated rather than counted. SPEC 2.2.2.
APPROXIMATE_THRESHOLD = 1_000_000

# Vendor limit: Snowflake seeds are 0-2147483647, not a chosen value.
SEED_MODULUS = 2_147_483_648

# Ratio at/above which a column is re-counted exactly. Well below the 0.9999
# candidate-key threshold (SPEC 4.2): HLL error reaches double digits on small tables.
_EXACT_PROBE_RATIO = 0.85

_NUMERIC_TYPES = (
    "smallint",
    "integer",
    "bigint",
    "decimal",
    "numeric",
    "real",
    "double precision",
    "double",
    "float",
    "money",
    "number",
    "int",
    "tinyint",
    "mediumint",
    "hugeint",
    "ubigint",
    "uinteger",
    "usmallint",
    "utinyint",
)

_TEMPORAL_TYPES = (
    "date",
    "time",
    "timestamp",
    "timestamp with time zone",
    "timestamp without time zone",
    "time with time zone",
    "time without time zone",
    "timestamp_ntz",
    "timestamp_ltz",
    "timestamp_tz",
)

# Time-of-day only: date arithmetic is undefined, so span and age are derived in-process.
_TIME_ONLY_TYPES = ("time", "time with time zone", "time without time zone")

_BOOLEAN_TYPES = ("boolean", "bool")
_JSON_TYPES = ("json", "jsonb", "variant", "object")
_UNSUPPORTED_TYPES = (
    "bytea",
    "blob",
    "binary",
    "varbinary",
    "image",
    "record",
    "struct",
    "array",
    "geography",
    "geometry",
    "vector",
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

    source = _table_source(identity, scope)
    narrows = scope is not None and scope.narrows

    estimate = introspect.row_count_estimate(cursor, identity)
    # The catalog estimate describes the table, not the slice, so a narrowed read counts exactly.
    approximate = estimate > APPROXIMATE_THRESHOLD and not narrows

    rows_scanned, base_stats = _phase_a(cursor, identity, source, columns, approximate)
    row_count, row_count_method = _table_row_count(cursor, identity, rows_scanned, estimate, scope)

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
            # A narrowed read drew nothing; publishing an exhaustive shape for columns
            # nobody read would overclaim (SPEC 2.2.7).
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
            identity,
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
    quoted = [identity.quoted_column(col.name) for col in columns]
    cap = config.top_n_null_patterns
    rows = exec_query(
        cursor,
        f"""
        SELECT {null_flags(quoted, concat=False)} AS dbprint_nulls, COUNT(*) AS cnt
        FROM {source}
        GROUP BY 1
        ORDER BY cnt DESC, dbprint_nulls ASC
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
    """One batched statement testing every candidate pair. See SPEC 2.2.12.

    Snowflake spells multi-column DISTINCT as a plain argument list, `COUNT(DISTINCT a, b)`,
    one expression per candidate, aliased by position so the row maps back to `candidates`.
    """

    if not candidates:
        return ()

    source = _table_source(identity, scope)
    exprs = [
        f"COUNT(DISTINCT {identity.quoted_column(a)}, {identity.quoted_column(b)})"
        f" AS dbprint_grain_{i}"
        for i, (a, b) in enumerate(candidates)
    ]
    rows = exec_query(cursor, f"SELECT {', '.join(exprs)} FROM {source}").fetchall()

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
    cn = identity.quoted_column(column)
    bucket_expr = f"DATE_TRUNC('{unit}', {cn})"

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
    anchor_cn = identity.quoted_column(anchor_column)

    agg_exprs = []
    outer_exprs = []

    for i, subject in enumerate(subject_columns):
        subject_cn = identity.quoted_column(subject)
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
    identity: Identity,
    columns: list[ColumnMeta],
    counts: TableCounts,
    base: dict[str, BaseStats],
    candidates: tuple[tuple[str, str], ...],
    scope: TableScope | None = None,
) -> dict[tuple[str, str], float]:
    """One batched statement measuring every candidate pair's joint cardinality. See SPEC 2.2.13.

    Phase A already measured `cardinality(determinant)`, so only the joint
    `COUNT(DISTINCT a, b)` needs a statement - same spelling and aliasing as `probe_grain`.
    """

    del columns, counts

    if not candidates:
        return {}

    source = _table_source(identity, scope)
    exprs = [
        f"COUNT(DISTINCT {identity.quoted_column(a)}, {identity.quoted_column(b)})"
        f" AS dbprint_dep_{i}"
        for i, (a, b) in enumerate(candidates)
    ]
    rows = exec_query(cursor, f"SELECT {', '.join(exprs)} FROM {source}").fetchall()

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
    """Copy the drawn fraction into a session-lifetime table and name it on the scope.

    The only construct giving Snowflake a stable draw: SYSTEM/BLOCK accept a seed, but two
    evaluations of one seeded expression are not documented to read the same rows. The copy
    sits in the table's own schema, so it needs no session default.
    """

    name = materialized_name(identity.dotted().lower())
    drawn = _source(identity, scope, seed_from_fqn(identity.dotted().lower(), SEED_MODULUS))
    exec_query(cursor, f"CREATE TEMPORARY TABLE {identity.sibling(name)} AS SELECT * FROM {drawn}")

    return replace(scope, materialized=name)


def release(cursor: Cursor, identity: Identity, scope: TableScope) -> None:
    """Drop the copied sample; the session would drop it anyway, this frees it sooner."""

    if scope.materialized is None:
        return

    exec_query(cursor, f"DROP TABLE IF EXISTS {identity.sibling(scope.materialized)}")


def _table_source(identity: Identity, scope: TableScope | None) -> str:
    """The FROM expression every phase reads.

    Rebuilt per phase, not threaded: the seed is re-derived from the table's own name, so
    every call produces the same text - which on this engine is not the same rows.
    """

    return _source(identity, scope, seed_from_fqn(identity.dotted().lower(), SEED_MODULUS))


def _source(identity: Identity, scope: TableScope | None, seed: int | None = None) -> str:
    """Table reference every statistics query selects FROM. See ARCHITECTURE.md 2.

    A materialized scope is already the drawn rows, so it reads as a plain name and no
    sampler runs again. A sampled scope keeps SAMPLE on the base table - Snowflake refuses
    a seed on a subquery - and names SYSTEM/BLOCK, the only methods that accept one; BLOCK
    can bias small tables. Only a filter scope gets the subquery wrapper.
    """

    base = identity.quoted()

    if scope is None or not scope.narrows:
        return base
    elif scope.materialized is not None:
        return identity.sibling(scope.materialized)
    elif scope.sample is not None:
        seeded = "" if seed is None else f" SEED ({seed})"

        return f"{base} SAMPLE SYSTEM ({scope.sample * 100}){seeded}"
    else:
        return f"(SELECT * FROM {base} WHERE {scope.filter})"


def _table_row_count(
    cursor: Cursor,
    identity: Identity,
    rows_scanned: int,
    estimate: int,
    scope: TableScope | None,
) -> tuple[int, RowCountMethod]:
    """Rows in the table and how they were obtained, per SPEC 2.2.1.

    A narrowed read takes the catalog estimate; with none it counts exactly, since the
    scanned figure would report a filter matching nothing as an empty table (SPEC 2.2.7).
    An estimate below the scanned count still stands (SPEC 2.2.8).
    """

    if scope is None or not scope.narrows:
        return rows_scanned, "exact"

    if estimate >= 0:
        return estimate, "approximate"

    row = exec_query(cursor, f"SELECT COUNT(*) FROM {identity.quoted()}").fetchone()

    return (int(row[0]) if row and row[0] is not None else rows_scanned), "exact"


def _phase_a(
    cursor: Cursor,
    identity: Identity,
    source: str,
    columns: list[ColumnMeta],
    approximate: bool,
) -> tuple[int, dict[str, BaseStats]]:
    """One query yielding row_count + per-column null_count + cardinality."""

    method: CardinalityMethod = "approximate" if approximate else "exact"
    select_parts: list[str] = ["COUNT(*) AS row_count"]

    for col in columns:
        cn = identity.quoted_column(col.name)
        # COALESCE guards COUNT_IF's NULL-on-empty-table result; HLL already returns 0.
        select_parts.append(f"COALESCE(COUNT_IF({cn} IS NULL), 0) AS null_{_alias(col.name)}")

        if _matches(col.sql_type, _NUMERIC_TYPES):
            select_parts.append(f"COALESCE(COUNT_IF({cn} = 0), 0) AS zero_{_alias(col.name)}")
            select_parts.append(f"COALESCE(COUNT_IF({cn} < 0), 0) AS neg_{_alias(col.name)}")
            select_parts.append(
                f"COALESCE(COUNT_IF({cn} = TRUNC({cn})), 0) AS quant_{_alias(col.name)}",
            )
        elif _is_string_like(col.sql_type):
            select_parts.append(
                f"COALESCE(COUNT_IF(TO_VARCHAR({cn}) = ''), 0) AS empty_{_alias(col.name)}",
            )
            length_expr = f"LENGTH(TO_VARCHAR({cn}))"
            select_parts.append(f"MIN({length_expr}) AS lenmin_{_alias(col.name)}")
            select_parts.append(f"MAX({length_expr}) AS lenmax_{_alias(col.name)}")
            select_parts.append(f"AVG({length_expr}) AS lenavg_{_alias(col.name)}")
            select_parts.append(
                f"PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY {length_expr}) "
                f"AS lenp95_{_alias(col.name)}",
            )

        select_parts.append(f"{_distinct_expr(cn, approximate)} AS card_{_alias(col.name)}")

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

        # HLL errs both ways; `cardinality` is non-null-only (SPEC 2.2.2), so clamp to non_null.
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
            length_p95=length_p95,
        )

    if approximate:
        _settle_near_unique(cursor, identity, source, columns, out, row_count)

    return row_count, out


def _settle_near_unique(
    cursor: Cursor,
    identity: Identity,
    source: str,
    columns: list[ColumnMeta],
    base: dict[str, BaseStats],
    row_count: int,
) -> None:
    """Re-count, exactly, the columns an estimate could misclassify.

    An HLL estimate a fraction low drops a key under SPEC 4.2's 0.9999 candidate-key
    threshold, costing it `candidate_key`; only near-unique columns are re-counted.
    """

    near_unique = [
        col
        for col in columns
        if row_count and base[col.name].cardinality / row_count >= _EXACT_PROBE_RATIO
    ]

    if not near_unique:
        return

    select_parts = [
        f"COUNT(DISTINCT {identity.quoted_column(col.name)}) AS card_{_alias(col.name)}"
        for col in near_unique
    ]
    sql = f"SELECT {', '.join(select_parts)} FROM {source}"
    row = exec_query(cursor, sql).fetchone()

    if row is None:
        return

    for col, value in zip(near_unique, row):
        # A separate statement may read a different snapshot than phase A, so clamp to non_null.
        base[col.name] = replace(
            base[col.name],
            cardinality=min(row_count - base[col.name].null_count, int(value)),
            cardinality_method="exact",
        )


def _distinct_expr(quoted_column: str, approximate: bool) -> str:
    """Distinct-count aggregate for one column: HLL estimate or exact count."""

    if approximate:
        return f"APPROX_COUNT_DISTINCT({quoted_column})"

    return f"COUNT(DISTINCT {quoted_column})"


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
    identity: Identity,
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
        values, coverage, _ = _fetch_value_list(cursor, identity, source, col, non_null, config)

        return _replace(stats, values=values, values_coverage=coverage)

    if pre in ("categorical", "foreign_key_candidate"):
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

        return _replace(
            stats,
            values=values,
            values_coverage=coverage,
            distribution=distribution,
        )

    if pre == "numeric":
        rng, percentiles, distribution, frequencies, values, mean, total = _fetch_numeric_block(
            cursor,
            identity,
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
                _fetch_temporal_block(cursor, identity, source, col, non_null, config)
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

    values, coverage, exhaustive = _fetch_value_list(
        cursor,
        identity,
        source,
        col,
        non_null,
        config,
    )
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

    Uniqueness plays no part: the ratio lives on the `inferred` axis (SPEC 4.2).
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
    identity: Identity,
    source: str,
    col: ColumnMeta,
    non_null: int,
    config: StatisticsConfig,
) -> tuple[tuple[ValueCount, ...], float, bool]:
    """Ordered value list, its coverage, and whether it enumerates the column.

    Fetches one row beyond the cap so truncation is observed rather than predicted from
    an estimated cardinality (SPEC 2.2.4); the limit is interpolated because server-side
    LIMIT binding is unproven here. A tz-bearing timestamp renders through the same
    UTC-pinning path as `range`/`percentiles`, never the session's zone.
    """

    cn = identity.quoted_column(col.name)
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
        ORDER BY cnt DESC, CAST({select_expr} AS VARCHAR) ASC
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
    identity: Identity,
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
    cn = identity.quoted_column(col.name)
    percentile_keys = config.percentiles
    # PERCENTILE_CONT multiplies against the ordering column - NUMBER(20,6) yields FIXED(23,9),
    # overflowing at the range top - hence the DOUBLE cast; duckdb reads FLOAT as 32-bit.
    pct_select = ", ".join(
        f"PERCENTILE_CONT({p / 100.0}) WITHIN GROUP (ORDER BY CAST({cn} AS DOUBLE)) AS p_{p:02d}"
        for p in percentile_keys
    )
    row = exec_query(
        cursor,
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
        cursor,
        source,
        cn,
        cn,
        non_null,
        config,
        _round_numeric,
    )

    return rng, percentiles, distribution, frequencies, values, mean, total


# TZ variants convert through CONVERT_TIMEZONE before rendering, immune to the session
# TIMEZONE param unlike TO_VARCHAR's offset token; LTZ and TZ both carry an instant.
_TZ_TYPES = ("timestamp with time zone", "timestamp_ltz", "timestamp_tz")
_DATE_ONLY_TYPES = ("date",)


def _fetch_temporal_block(
    cursor: Cursor,
    identity: Identity,
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
        return _fetch_clock_temporal_block(cursor, identity, source, col, non_null, config)

    return _fetch_calendar_temporal_block(cursor, identity, source, col, non_null, config)


def _fetch_clock_temporal_block(
    cursor: Cursor,
    identity: Identity,
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

    cn = identity.quoted_column(col.name)
    percentile_keys = config.percentiles
    pct_select = ", ".join(
        f"MIN(CASE WHEN dbprint_rn >= CEIL({p / 100.0} * dbprint_n) THEN {cn} END) AS p_{p:02d}"
        for p in percentile_keys
    )
    row = exec_query(
        cursor,
        f"""
        SELECT MIN({cn}) AS mn, MAX({cn}) AS mx, {pct_select}
        FROM (
            SELECT
                {cn},
                ROW_NUMBER() OVER (ORDER BY {cn}) AS dbprint_rn,
                COUNT(*) OVER () AS dbprint_n
            FROM {source}
            WHERE {cn} IS NOT NULL
        ) ranked
        """,
    ).fetchone()

    if row is None:
        empty_range = Range(min=None, max=None, span_days=0)

        return empty_range, {}, "uniform", (), summarize_frequencies([]), (), None

    rng = Range(min=_iso_or_value(row[0]), max=_iso_or_value(row[1]), span_days=0)
    percentiles = {f"p{p:02d}": _iso_or_value(v) for p, v in zip(percentile_keys, row[2:])}
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
    identity: Identity,
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

    cn = identity.quoted_column(col.name)
    percentile_keys = config.percentiles
    # A DATE value is always its own day-truncation (SPEC 2.2.3): the count would be a
    # truism, so `quantized_count` is omitted entirely rather than published as a constant.
    day_aligned = not _matches(col.sql_type, _DATE_ONLY_TYPES)

    # `CEIL(p * n)` is the PERCENTILE_DISC rank; the computed columns are prefixed so a bare
    # `n`/`rn` cannot collide with a real one.
    pct_select = ", ".join(
        f"MIN(CASE WHEN dbprint_rn >= CEIL({p / 100.0} * dbprint_n) THEN {cn} END) AS p_{p:02d}"
        for p in percentile_keys
    )
    agg_select = [f"MIN({cn}) AS mn", f"MAX({cn}) AS mx", pct_select]

    if day_aligned:
        agg_select.append(f"COUNT_IF({cn} = DATE_TRUNC('day', {cn})) AS quant")

    percentile_renders = [
        (f"p{p:02d}", _render_calendar_bound(f"p_{p:02d}", col.sql_type)) for p in percentile_keys
    ]

    outer_select = [
        f"{_render_calendar_bound('mn', col.sql_type)} AS mn_text",
        f"{_render_calendar_bound('mx', col.sql_type)} AS mx_text",
        "FLOOR(DATEDIFF('second', mn, mx) / 86400) AS span_days",
        *(f"{expr} AS {key}_text" for key, expr in percentile_renders),
        *(["quant"] if day_aligned else []),
    ]

    row = exec_query(
        cursor,
        f"""
        SELECT {", ".join(outer_select)}
        FROM (
            SELECT {", ".join(agg_select)}
            FROM (
                SELECT
                    {cn},
                    ROW_NUMBER() OVER (ORDER BY {cn}) AS dbprint_rn,
                    COUNT(*) OVER () AS dbprint_n
                FROM {source}
                WHERE {cn} IS NOT NULL
            ) ranked
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

    # Floored in SQL per SPEC 2.2.4; this only narrows the type.
    span_days = int(span_raw) if span_raw is not None else 0
    rng = Range(min=row[0], max=row[1], span_days=span_days)
    percentiles = {key: text for (key, _), text in zip(percentile_renders, percentile_texts)}

    # Rendered the same way the bounds above already were - Snowflake carries no infinity
    # sentinel, but agreement with `range`/`percentiles` still requires one rendering rule.
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

    An explicit `TO_VARCHAR` picture ignores `TIMESTAMP_OUTPUT_FORMAT`/`DATE_OUTPUT_FORMAT`,
    which govern only an implicit cast; `FF6` renders six digits, stripped to `isoformat()`'s
    all-or-nothing form. Snowflake has no `infinity` sentinel or BC era, so no CASE guard.
    """

    is_tz = _matches(sql_type, _TZ_TYPES)
    is_date_only = _matches(sql_type, _DATE_ONLY_TYPES)
    picture = "YYYY-MM-DD" if is_date_only else 'YYYY-MM-DD"T"HH24:MI:SS.FF6'
    source_expr = f"CONVERT_TIMEZONE('UTC', {expr})" if is_tz else expr
    body = f"TO_VARCHAR({source_expr}, '{picture}')"

    if not is_date_only:
        body = f"REGEXP_REPLACE({body}, '\\.000000$', '')"

    if is_tz:
        body = f"{body} || 'Z'"

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
        ORDER BY cnt DESC, CAST({select_expr} AS VARCHAR) ASC
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


def _alias(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name)


def _replace(stats: ColumnStats, **kwargs: Any) -> ColumnStats:

    return replace(stats, **kwargs)
