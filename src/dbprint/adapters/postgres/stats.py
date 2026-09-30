"""Two-phase batched per-table statistics computation. See ARCHITECTURE.md 2.

Phase B pre-classifies each column to batch its queries by classification group, keeping the
per-table query count small; the engine re-applies SPEC 3.2 independently, and the adapter
NEVER stamps `classification`. Approximate methods activate above `APPROXIMATE_THRESHOLD`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Any, Literal

from dbprint.config import StatisticsConfig
from dbprint.spec.classification import (
    base_type,
    classify,
    compute_cardinality_ratio,
    compute_null_rate,
    has_day_resolution,
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
from .connection import DIALECT, exec_query
from .introspect import composite_columns, reltuples_estimate
from .rendering import render_domain, render_operand, render_text, temporal_shape
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
from ..identifiers import SOURCE_ALIAS, Identity, quote, source_column
from ..sql_layout import derived, indented, listed, select_from


if TYPE_CHECKING:
    import psycopg


APPROXIMATE_THRESHOLD = 1_000_000


# Range the sampling seed reduces into; Postgres bounds the seed nowhere, so dbprint sets it.
SEED_MODULUS = 2**31


_UNSUPPORTED_TYPES = (
    "bytea",
    "point",
    "line",
    "lseg",
    "box",
    "path",
    "polygon",
    "circle",
    "xml",
    "aclitem",
    "cid",
    "xid",
    "gtsvector",
    "jsonpath",
    "pg_snapshot",
    "txid_snapshot",
    "refcursor",
    "geometry",
    "geography",
    "vector",
)

# Vendor spellings profiled as text by representability (SPEC 3.1), declared so none is guessed.
_TEXT_TYPES: tuple[str, ...] = (
    "inet",
    "cidr",
    "macaddr",
    "macaddr8",
    "interval",
    "bit",
    "bit varying",
    "tsvector",
    "tsquery",
    "pg_lsn",
    "oid",
    "name",
    "citext",
    "anyenum",
    "anyrange",
    "anymultirange",
    '"char"',
    "tid",
    "xid8",
    "int2vector",
    "oidvector",
    "regclass",
    "regcollation",
    "regconfig",
    "regdictionary",
    "regnamespace",
    "regoper",
    "regoperator",
    "regproc",
    "regprocedure",
    "regrole",
    "regtype",
)

KNOWN_TYPES = (*_UNSUPPORTED_TYPES, *_TEXT_TYPES)


def compute_base(
    conn: psycopg.Connection,
    identity: Identity,
    columns: list[ColumnMeta],
    scope: TableScope | None = None,
) -> tuple[TableCounts, PhaseA]:
    """Phase A: the table's counts plus per-column null_count and cardinality."""

    if not columns:
        return TableCounts(row_count=0, rows_scanned=0), PhaseA({})

    source = _table_source(identity, scope)
    narrows = scope is not None and scope.narrows

    reltuples = reltuples_estimate(conn, identity)
    # The planner's n_distinct describes the whole table, so a narrowed read counts instead.
    approximate = reltuples > APPROXIMATE_THRESHOLD and not narrows
    composite = composite_columns(conn, identity)

    rows_scanned, phase_a = run_phase_a(
        columns,
        _phase_a_cost,
        partial(
            _phase_a_statement,
            conn,
            identity,
            source,
            approximate=approximate,
            composite=composite,
        ),
        partial(_null_counts, conn, source),
        partial(_recount, conn, source) if approximate else None,
        declines=lambda col: _is_unsupported(col.classified_type) or col.name in composite,
    )
    row_count, row_count_method = _table_row_count(
        conn,
        identity.quoted(),
        rows_scanned,
        reltuples,
        scope,
    )

    return TableCounts(row_count, rows_scanned, row_count_method), phase_a


def compute_columns(
    conn: psycopg.Connection,
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

        return PhaseB({c.name: _empty_stats(c, base[c.name].supported) for c in columns})

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
            supported=base[col.name].supported,
        )
        return _phase_b(
            conn,
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
    conn: psycopg.Connection,
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
        conn,
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
        LIMIT %s
        """,
        (cap + 1,),
    ).fetchall()

    return null_patterns_from_rows(rows, columns, counts.rows_scanned, cap)


def probe_grain(
    conn: psycopg.Connection,
    identity: Identity,
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

    source = _table_source(identity, scope)
    physical = {
        col.name: render_operand(source_column(col, DIALECT), col.classified_type)
        for col in columns
    }
    exprs = [
        f"COUNT(DISTINCT ({physical[a]}, {physical[b]})) AS {_alias(f'dbprint_grain_{i}')}"
        for i, (a, b) in enumerate(candidates)
    ]
    row = exec_query(conn, select_from(exprs, source)).fetchone()

    if row is None:
        return ()

    return tuple(pair for i, pair in enumerate(candidates) if row[i] == counts.rows_scanned)


def probe_timeline(
    conn: psycopg.Connection,
    identity: Identity,
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

    source = _table_source(identity, scope)
    by_name = {col.name: col for col in columns}
    col = by_name[column]
    cn = render_operand(source_column(col, DIALECT), col.classified_type)
    bucket_expr = _timeline_bucket_expr(cn, col.classified_type, unit)

    rows = exec_query(
        conn,
        f"""
        WITH
          buckets AS (

            SELECT
              {bucket_expr} AS bucket_start,
              COUNT(1) AS cnt
            FROM
              {indented(source, 14)}
            WHERE
              {cn} IS NOT NULL
            GROUP BY
              1

          )
        SELECT
          {indented(render_domain("bucket_start", col.classified_type), 10)} AS bucket_text,
          cnt
        FROM
          buckets
        ORDER BY
          bucket_start
        """,
    ).fetchall()

    return tuple((measured_text(row[0], "timeline"), int(row[1])) for row in rows)


def _timeline_bucket_expr(cn: str, sql_type: str, unit: str) -> str:
    """Truncation expression for `probe_timeline`'s GROUP BY key (SPEC 2.2.16) - `date_trunc`
    has no `date` overload before Postgres 14, so a DATE column casts to timestamp first.
    """

    if temporal_shape(sql_type) == "date":
        return f"DATE_TRUNC('{unit}', {cn}::TIMESTAMP)"

    return f"DATE_TRUNC('{unit}', {cn})"


def compute_populated_windows(
    conn: psycopg.Connection,
    identity: Identity,
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
            f"{render_domain(f'from_{i}', anchor.classified_type)} AS from_{i}_text",
        )
        outer_exprs.append(
            f"{render_domain(f'to_{i}', anchor.classified_type)} AS to_{i}_text",
        )

    row = exec_query(
        conn,
        f"""
        WITH
          agg AS (

            SELECT
              {listed(agg_exprs, 14)}
            FROM
              {indented(source, 14)}

          )
        SELECT
          {listed(outer_exprs, 10)}
        FROM
          agg
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
    conn: psycopg.Connection,
    identity: Identity,
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

    source = _table_source(identity, scope)
    physical = {
        col.name: render_operand(source_column(col, DIALECT), col.classified_type)
        for col in columns
    }
    exprs = [
        f"COUNT(DISTINCT ({physical[a]}, {physical[b]})) AS {_alias(f'dbprint_dep_{i}')}"
        for i, (a, b) in enumerate(candidates)
    ]
    row = exec_query(conn, select_from(exprs, source)).fetchone()

    if row is None:
        return {}

    out: dict[tuple[str, str], float] = {}

    for i, (a, b) in enumerate(candidates):
        joint = row[i]

        if joint:
            out[(a, b)] = min(1.0, base[a].cardinality / joint)

    return out


def materialize(conn: psycopg.Connection, identity: Identity, scope: TableScope) -> TableScope:
    """Copy the drawn fraction into a session-lifetime temp table and name it on the scope.

    Autocommit makes the CREATE outlive its own statement. The name stays unqualified: a
    temp table lives in `pg_temp`, and qualifying it addresses a different schema.
    """

    name = materialized_name(identity.fqn)
    drawn = _source(identity.quoted(), scope, _seed(identity))
    exec_query(
        conn,
        f"CREATE TEMPORARY TABLE {quote(name, DIALECT)} AS SELECT * FROM {drawn}",
    )

    return replace(scope, materialized=name)


def release(conn: psycopg.Connection, scope: TableScope) -> None:
    """Drop the copied sample; the session would drop it anyway, this frees it sooner."""

    if scope.materialized is None:
        return

    exec_query(conn, f"DROP TABLE IF EXISTS {quote(scope.materialized, DIALECT)}")


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

    A materialized scope is already the drawn rows and reads as a plain name. TABLESAMPLE binds
    to a base table and needs no wrapper; a predicate gets one. `REPEATABLE` makes every
    statement read the same rows, and BERNOULLI hashes (block, offset, seed), so a smaller rate
    under the same seed yields a subset of the larger draw.
    """

    if scope is None or not scope.narrows:
        return f"{quoted_fqn} {SOURCE_ALIAS}"
    elif scope.materialized is not None:
        return f"{quote(scope.materialized, DIALECT)} {SOURCE_ALIAS}"
    elif scope.sample is not None:
        repeatable = "" if seed is None else f" REPEATABLE ({seed})"
        rate = scope.sample * 100

        return f"{quoted_fqn} {SOURCE_ALIAS} TABLESAMPLE BERNOULLI({rate}){repeatable}"
    else:
        return derived(f"SELECT * FROM {quoted_fqn} WHERE {scope.filter}", SOURCE_ALIAS)


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

    row = exec_query(conn, f"SELECT COUNT(1) FROM {quoted_fqn} {SOURCE_ALIAS}").fetchone()

    return (int(row[0]) if row and row[0] is not None else rows_scanned), "exact"


def _phase_a_cost(column: ColumnMeta) -> int:
    if is_numeric_type(column.classified_type):
        return 5

    if _is_string_like(column.classified_type):
        return 7

    return 2


def _null_counts(
    conn: psycopg.Connection,
    source: str,
    columns: list[ColumnMeta],
) -> tuple[int, dict[str, int]]:
    counts = [
        f"COUNT({render_operand(source_column(col, DIALECT), col.classified_type)})"
        for col in columns
    ]
    row = exec_query(conn, select_from(["COUNT(1)", *counts], source)).fetchone()
    rows, *non_null = (int(value) for value in row) if row else (0, *(0 for _ in columns))

    return rows, {col.name: rows - n for col, n in zip(columns, non_null, strict=True)}


def _recount(
    conn: psycopg.Connection,
    source: str,
    columns: list[ColumnMeta],
) -> Sequence[Any] | None:
    select_parts = [
        f"{_distinct_count(col, render_operand(source_column(col, DIALECT), col.classified_type))} "
        f"AS {_alias(f'card_{col.name}')}"
        for col in columns
    ]

    return exec_query(conn, select_from(select_parts, source)).fetchone()


def _phase_a_statement(
    conn: psycopg.Connection,
    identity: Identity,
    source: str,
    columns: list[ColumnMeta],
    approximate: bool,
    composite: frozenset[str],
) -> tuple[int, dict[str, BaseStats]]:
    """One query yielding row_count + per-column null_count + cardinality."""

    select_parts: list[str] = ["COUNT(1) AS row_count"]

    for col in columns:
        cn = render_operand(source_column(col, DIALECT), col.classified_type)
        select_parts.append(f"COUNT(1) FILTER (WHERE {cn} IS NULL) AS null_{_alias(col.name)}")

        if is_numeric_type(col.classified_type):
            select_parts.append(f"COUNT(1) FILTER (WHERE {cn} = 0) AS zero_{_alias(col.name)}")
            select_parts.append(f"COUNT(1) FILTER (WHERE {cn} < 0) AS neg_{_alias(col.name)}")
            select_parts.append(
                f"COUNT(1) FILTER (WHERE {cn} = TRUNC({cn})) AS quant_{_alias(col.name)}",
            )

        # SPEC 2.2.3 conditions `length` on the published `sql_type`, which a domain renames.
        if _is_string_like(col.sql_type):
            select_parts.append(
                f"COUNT(1) FILTER (WHERE CAST({cn} AS TEXT) = '') AS empty_{_alias(col.name)}",
            )
            length_expr = f"LENGTH(CAST({cn} AS TEXT))"
            select_parts.append(f"MIN({length_expr}) AS lenmin_{_alias(col.name)}")
            select_parts.append(f"MAX({length_expr}) AS lenmax_{_alias(col.name)}")
            select_parts.append(f"AVG({length_expr}) AS lenavg_{_alias(col.name)}")
            select_parts.append(
                f"PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY {length_expr}) "
                f"AS lenp95_{_alias(col.name)}",
            )

        if not approximate:
            select_parts.append(f"{_distinct_count(col, cn)} AS card_{_alias(col.name)}")

    row = exec_query(conn, select_from(select_parts, source)).fetchone()

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

        if is_numeric_type(col.classified_type):
            zero_count = int(row[idx])
            idx += 1
            negative_count = int(row[idx])
            idx += 1
            quantized_count = int(row[idx])
            idx += 1

        if _is_string_like(col.sql_type):
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

        if approximate and _is_unsupported(col.classified_type):
            cardinality = 0
        elif approximate:
            estimate = _approximate_cardinality(
                conn,
                identity,
                col.physical_name or col.name,
                row_count,
                null_count,
            )

            if estimate is None:
                cardinality = _exact_cardinality(conn, source, col)
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
            supported=not _is_unsupported(col.classified_type) and col.name not in composite,
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


def _exact_cardinality(conn: psycopg.Connection, source: str, col: ColumnMeta) -> int:
    """Count distinct values for one column, when the planner has no estimate."""

    cn = render_operand(source_column(col, DIALECT), col.classified_type)
    row = exec_query(conn, select_from([f"COUNT(DISTINCT {cn})"], source)).fetchone()

    return int(row[0]) if row and row[0] is not None else 0


def _approximate_cardinality(
    conn: psycopg.Connection,
    identity: Identity,
    col_name: str,
    row_count: int,
    null_count: int,
) -> int | None:
    """Distinct-count estimate from the planner, or None when it has none.

    None is the absence of an estimate, not a zero count; the caller then counts exactly.
    """

    row = exec_query(
        conn,
        """
        SELECT
          sts.n_distinct
        FROM
          pg_stats sts
        WHERE
          sts.schemaname = %s
          AND sts.tablename = %s
          AND sts.attname = %s
        """,
        (*identity.addressed, col_name),
    ).fetchone()

    if not row or row[0] is None:
        return None

    n_distinct = float(row[0])
    non_null = row_count - null_count

    # Negative n_distinct is the negated row fraction, nulls included per pg_stats;
    # `cardinality` is non-null-only (SPEC 2.2.2), so clamp to non_null.
    if n_distinct < 0:
        return min(non_null, round(-n_distinct * row_count))

    # `stadistinct` 0 is "unknown", ANALYZE's answer for a type without an equality operator.
    if n_distinct == 0 and non_null > 0:
        return None

    return min(non_null, int(n_distinct))


def _empty_stats(col: ColumnMeta, supported: bool = True) -> ColumnStats:
    """SPEC 2.2.7 edge case: a table read in full and found empty -> minimal column stats.

    A narrowed read that drew nothing is a different condition and never reaches here.
    `supported` is Phase A's verdict, the same signal `_pre_classify` defers to.
    """

    if not supported or _is_unsupported(col.classified_type):
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
        length=length if pre in _LENGTH_CLASSIFICATIONS else None,
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
    """Declined when this adapter cannot profile the column; the shared SPEC 3.2 decision otherwise.

    `supported` comes from Phase A, since a composite column's type has no name to match.
    """

    if not supported or _is_unsupported(col.classified_type):
        return "unsupported"

    return classify(
        col.classified_type,
        cardinality,
        has_declared_fk,
        config.enumeration_threshold,
    )


def _fetch_value_list(
    conn: psycopg.Connection,
    source: str,
    col: ColumnMeta,
    non_null: int,
    config: StatisticsConfig,
) -> tuple[tuple[ValueCount, ...], float, bool]:
    """Ordered value list, its coverage, and whether it enumerates the column.

    Fetches one row beyond the bound so truncation is observed, not predicted from a cardinality
    that may be an estimate (SPEC 2.2.4). A tz-bearing timestamp routes through the same
    UTC-pinning renderer as `range`, so its literals never carry the session zone.
    """

    cn = render_operand(source_column(col, DIALECT), col.classified_type)
    limit = enumeration_limit(config.enumeration_threshold, config.top_n_values)
    select_expr = cn

    if temporal_shape(col.classified_type) == "timestamp_tz":
        select_expr = render_domain(cn, col.classified_type)
    elif _is_string_like(col.classified_type):
        select_expr = render_text(cn, col.classified_type)

    rows = exec_query(
        conn,
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
          cnt DESC, CAST({indented(select_expr, 10)} AS TEXT) ASC
        LIMIT %s
        """,
        (limit + 1,),
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
    cn = render_operand(source_column(col, DIALECT), col.classified_type)
    percentile_keys = config.percentiles
    pct_select = [
        f"PERCENTILE_CONT({p / 100.0}::DOUBLE PRECISION) WITHIN GROUP (ORDER BY {cn}) AS p_{p:02d}"
        for p in percentile_keys
    ]
    row = exec_query(
        conn,
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

    rng = Range(
        min=round_statistic(row[0], exact_int=True),
        max=round_statistic(row[1], exact_int=True),
    )
    mean = round_statistic(row[2])
    total = round_statistic(row[3], exact_int=True)
    percentiles = coherent_percentiles(
        {f"p{p:02d}": round_statistic(v) for p, v in zip(percentile_keys, row[4:], strict=True)},
        rng.min,
        rng.max,
    )
    distribution, frequencies, values = _approximate_distribution_via_top_n(
        conn,
        source,
        cn,
        cn,
        non_null,
        config,
        measured_value,
    )

    return rng, percentiles, distribution, frequencies, values, mean, total


_LENGTH_CLASSIFICATIONS = ("text", "categorical", "foreign_key_candidate")

# UTC bounds a calendar value is clamped to before date arithmetic; inside this window,
# subtracting an infinite timestamp cannot raise `DatetimeFieldOverflow` server-side.
_EPOCH_FLOOR = "0001-01-01T00:00:00Z"
_EPOCH_CEIL = "9999-12-31T23:59:59.999999Z"


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
    if temporal_shape(col.classified_type) in ("time", "time_tz"):
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

    cn = render_operand(source_column(col, DIALECT), col.classified_type)
    percentile_keys = config.percentiles
    # percentile_disc takes any sortable type; percentile_cont only double precision/interval.
    pct_select = [
        f"PERCENTILE_DISC({p / 100.0}::DOUBLE PRECISION) WITHIN GROUP (ORDER BY {cn}) AS p_{p:02d}"
        for p in percentile_keys
    ]
    row = exec_query(
        conn,
        f"""
        SELECT
          MIN({cn}) AS mn,
          MAX({cn}) AS mx,
          {listed(pct_select, 10)}
        FROM
          {indented(source, 10)}
        WHERE
          {cn} IS NOT NULL
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
    distribution, frequencies, values = _approximate_distribution_via_top_n(
        conn,
        source,
        cn,
        cn,
        non_null,
        config,
        measured_value,
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

    cn = render_operand(source_column(col, DIALECT), col.classified_type)
    percentile_keys = config.percentiles
    date_only = temporal_shape(col.classified_type) == "date"
    cast_type = (
        "TIMESTAMPTZ"
        if temporal_shape(col.classified_type) == "timestamp_tz"
        else "DATE"
        if date_only
        else "TIMESTAMP"
    )
    # A DATE value is always its own day-truncation (SPEC 2.2.3): the count would be a truism,
    # so `quantized_count` is omitted - judged, as the validator does, on the published type.
    day_aligned = has_day_resolution(col.sql_type)

    agg_select = (
        [f"MIN({cn}) AS mn", f"MAX({cn}) AS mx"]
        + (
            [f"COUNT(1) FILTER (WHERE {cn} = DATE_TRUNC('day', {cn})) AS quant"]
            if day_aligned
            else []
        )
        + [
            f"PERCENTILE_DISC({p / 100.0}::DOUBLE PRECISION) WITHIN GROUP (ORDER BY {cn}) AS p_{p:02d}"
            for p in percentile_keys
        ]
    )
    percentile_renders = [
        (f"p{p:02d}", render_domain(f"p_{p:02d}", col.classified_type)) for p in percentile_keys
    ]

    lo, hi = f"'{_EPOCH_FLOOR}'::{cast_type}", f"'{_EPOCH_CEIL}'::{cast_type}"
    clamped_mn = f"LEAST(GREATEST(mn, {lo}), {hi})::TIMESTAMPTZ"
    clamped_mx = f"LEAST(GREATEST(mx, {lo}), {hi})::TIMESTAMPTZ"
    # GREATEST/LEAST ignore a NULL argument rather than propagating it, so an empty column
    # would otherwise clamp to a real bound and report a span for data that does not exist.
    span_days = f"""
        CASE
          WHEN mn IS NULL THEN NULL
          ELSE EXTRACT(DAY FROM ({clamped_mx} - {clamped_mn}))
        END
        """

    outer_select = [
        f"{render_domain('mn', col.classified_type)} AS mn_text",
        f"{render_domain('mx', col.classified_type)} AS mx_text",
        *(f"{expr} AS {key}_text" for key, expr in percentile_renders),
        f"{span_days} AS span_days",
        *(["quant"] if day_aligned else []),
    ]

    row = exec_query(
        conn,
        f"""
        WITH
          agg AS (

            SELECT
              {listed(agg_select, 14)}
            FROM
              {indented(source, 14)}
            WHERE
              {cn} IS NOT NULL

          )
        SELECT
          {listed(outer_select, 10)}
        FROM
          agg
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
    rng = Range(
        min=measured_value(row[0], "range.min"),
        max=measured_value(row[1], "range.max"),
        span_days=span_days_val,
    )
    percentiles = {
        key: measured_value(text, f"percentiles.{key}")
        for (key, _), text in zip(percentile_renders, pct_texts, strict=True)
    }

    # Rendered in SQL like the bounds: a raw fetch can hand psycopg infinity or a year outside
    # 0001-9999, which it refuses to build.
    distribution, frequencies, values = _approximate_distribution_via_top_n(
        conn,
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

    limit = enumeration_limit(config.enumeration_threshold, config.top_n_values)
    rows = exec_query(
        conn,
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
          cnt DESC, CAST({indented(select_expr, 10)} AS TEXT) ASC
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


def _empty_base(col: ColumnMeta, composite: frozenset[str]) -> BaseStats:
    """Phase A's answer for a table whose batched query yielded no row.

    `supported` is per column, a property of the type rather than of the empty result.
    """

    return BaseStats(
        null_count=0,
        cardinality=0,
        cardinality_method="exact",
        supported=not _is_unsupported(col.classified_type) and col.name not in composite,
    )


def _distinct_count(col: ColumnMeta, cn: str) -> str:
    # A declined type may have no equality operator at all (`point`, `xml`), so nothing counts it.
    return "0" if _is_unsupported(col.classified_type) else f"COUNT(DISTINCT {cn})"


def _is_unsupported(sql_type: str) -> bool:
    base = base_type(sql_type)

    return base in _UNSUPPORTED_TYPES or is_array_type(sql_type)


def _matches(sql_type: str, types: tuple[str, ...]) -> bool:
    return base_type(sql_type) in types


def _is_string_like(sql_type: str) -> bool:
    return not _is_unsupported(sql_type) and is_string_like_type(sql_type)


def _alias(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name)


def _replace(stats: ColumnStats, **kwargs: Any) -> ColumnStats:

    return replace(stats, **kwargs)
