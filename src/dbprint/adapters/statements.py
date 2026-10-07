"""Statistics statements every SQL adapter emits, written once and spelled through its `Dialect`.

An adapter supplies the source, each column's operand and a rendering; the shape is written here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from dbprint.config import StatisticsConfig
from dbprint.spec.coverage import enumeration_limit
from dbprint.spec.rounding import measured_text, round_statistic
from dbprint.spec.spatial import Extent, Geometry, SpatialGroup, dimensions_of, fold, ogc_kind
from .base import (
    MIN_SAMPLE_DRAW,
    BaseStats,
    ColumnMeta,
    InstanceShape,
    Length,
    NullPatterns,
    PartSource,
    RowCountMethod,
    TableCounts,
    TableScope,
    TopN,
    ValueList,
    VectorReading,
    has_measurable_nulls,
    is_string_like,
    null_flags,
    null_patterns_from_rows,
    seed_from_fqn,
    top_n_summary,
    value_list_from_rows,
)
from .dialect import Dialect
from .identifiers import SOURCE_ALIAS, Identity
from .sql_layout import call, derived, indented, listed, select_from


# Below `n * SMALL_TABLE_FACTOR` estimated rows a sample reads the column directly; above, the
# engine draws `n * SAMPLE_RATE_MULTIPLIER` rows first, compensating for the DISTINCT filter.
SMALL_TABLE_FACTOR = 10
SAMPLE_RATE_MULTIPLIER = 10

# Snowflake's documented seed range, 0-2147483647; no other engine documents a narrower one.
SEED_MODULUS = 2**31


class Execute(Protocol):
    """Run one statement and return the cursor holding the result: `partial(exec_query, cursor)`."""

    def __call__(self, sql: str, params: Any = None, /) -> Any: ...


@dataclass(frozen=True)
class SpatialAccessors:
    """One engine's per-value spelling of each SPEC 2.2.4 spatial reading, over one column.

    An `int` is a constant the type fixes; `srid`/`invalid` None means the type carries no
    reference system or cannot hold an invalid value; `bounds` None reads no `extent`.
    """

    kind: str
    srid: str | int | None
    flag: str | int
    empty: str
    invalid: str | None
    bounds: tuple[str, str, str, str] | None


def column_alias(name: str) -> str:
    """`name` as a bare SQL alias: every character outside `[A-Za-z0-9]` becomes `_`."""

    return "".join(c if c.isalnum() else "_" for c in name)


def most_frequent(
    execute: Execute,
    dialect: Dialect,
    source: str,
    rendered: str,
    config: StatisticsConfig,
    *,
    non_null: str,
    group: str,
    extra: Sequence[str] = (),
) -> list[Any]:
    """`(rendered, cnt, *extra)` rows of the most frequent values, one past the limit (SPEC 2.2.4).

    Ties break on the value's text; the limit is inlined, since a literal `%` would break `%s`.
    """

    limit = enumeration_limit(config.enumeration_threshold, config.top_n_values)
    selected = [f"{rendered} AS rendered", f"{dialect.row_count} AS cnt", *extra]

    return execute(
        f"""
        SELECT
          {listed(selected, 10)}
        FROM
          {indented(source, 10)}
        WHERE
          {indented(non_null, 10)} IS NOT NULL
        GROUP BY
          {indented(group, 10)}
        ORDER BY
          cnt DESC, {indented(dialect.text_order(rendered), 10)} ASC
        LIMIT {int(limit) + 1}
        """,
    ).fetchall()


def value_list(
    execute: Execute,
    dialect: Dialect,
    source: str,
    rendered: str,
    non_null: int,
    config: StatisticsConfig,
    *,
    column: str,
    group: str | None = None,
) -> ValueList:
    """A column's value list, its coverage and whether it enumerates the column (SPEC 2.2.4)."""

    rows = most_frequent(
        execute,
        dialect,
        source,
        rendered,
        config,
        non_null=column,
        group=group or column,
    )

    return value_list_from_rows(rows, non_null, config)


def top_n(
    execute: Execute,
    dialect: Dialect,
    source: str,
    rendered: str,
    non_null: int,
    config: StatisticsConfig,
    value_transform: Callable[[Any], Any],
    *,
    column: str,
    group: str | None = None,
) -> TopN:
    """Distribution, frequencies and values from the top-N rows (SPEC 2.2.3).

    Grouping stays on the raw column, so a rendered expression cannot split one value in two.
    """

    rows = most_frequent(
        execute,
        dialect,
        source,
        rendered,
        config,
        non_null=column,
        group=group or column,
    )

    return top_n_summary(rows, non_null, config, value_transform)


def null_patterns(
    execute: Execute,
    dialect: Dialect,
    source: str,
    columns: list[ColumnMeta],
    operands: Sequence[str],
    config: StatisticsConfig,
    counts: TableCounts,
    base: dict[str, BaseStats],
) -> NullPatterns | None:
    """Which columns are null together, in one grouped scan. See SPEC 2.2.10."""

    if not has_measurable_nulls(counts, base):
        return None

    cap = config.top_n_null_patterns
    flags = null_flags(list(operands), concat=dialect.concat_null_flags)
    rows = execute(
        f"""
        SELECT
          {indented(flags, 10)} AS dbprint_nulls,
          {dialect.row_count} AS cnt
        FROM
          {indented(source, 10)}
        GROUP BY
          {"1" if dialect.group_by_ordinal else "dbprint_nulls"}
        ORDER BY
          cnt DESC, dbprint_nulls ASC
        LIMIT {int(cap) + 1}
        """,
    ).fetchall()

    return null_patterns_from_rows(rows, columns, counts.rows_scanned, cap)


def null_counts(
    execute: Execute,
    dialect: Dialect,
    source: str,
    columns: list[ColumnMeta],
    operand: Callable[[ColumnMeta], str],
) -> tuple[int, dict[str, int]]:
    """The rows scanned and each column's null count, read on their own."""

    counts = [f"{dialect.count_fn}({operand(col)})" for col in columns]
    row = execute(select_from([dialect.row_count, *counts], source)).fetchone()
    rows, *non_null = (int(value) for value in row) if row else (0, *(0 for _ in columns))

    return rows, {col.name: rows - n for col, n in zip(columns, non_null, strict=True)}


def recount(
    execute: Execute,
    dialect: Dialect,
    source: str,
    columns: list[ColumnMeta],
    operand: Callable[[ColumnMeta], str],
    *,
    prefix: str = "card_",
) -> Sequence[Any] | None:
    """One exact distinct count per column, for near-unique columns an estimate cannot settle."""

    select_parts = [
        f"{dialect.distinct_count.format(operand(col))} AS {prefix}{column_alias(col.name)}"
        for col in columns
    ]

    return execute(select_from(select_parts, source)).fetchone()


def pair_cardinalities(
    execute: Execute,
    dialect: Dialect,
    source: str,
    candidates: tuple[tuple[str, str], ...],
    operands: Mapping[str, str],
    prefix: str,
) -> Sequence[Any] | None:
    """One distinct count per candidate pair, in one statement aliased by position."""

    exprs = [
        f"{dialect.pair_distinct.format(a=operands[a], b=operands[b])} AS {prefix}_{i}"
        for i, (a, b) in enumerate(candidates)
    ]

    return execute(select_from(exprs, source)).fetchone()


def grain_pairs(
    execute: Execute,
    dialect: Dialect,
    source: str,
    counts: TableCounts,
    candidates: tuple[tuple[str, str], ...],
    operands: Mapping[str, str],
) -> tuple[tuple[str, str], ...]:
    """The candidate pairs whose distinct count equals the rows scanned (SPEC 2.2.12)."""

    if not candidates:
        return ()

    row = pair_cardinalities(execute, dialect, source, candidates, operands, "dbprint_grain")

    if row is None:
        return ()

    return tuple(pair for i, pair in enumerate(candidates) if row[i] == counts.rows_scanned)


def dependency_strengths(
    execute: Execute,
    dialect: Dialect,
    source: str,
    base: dict[str, BaseStats],
    candidates: tuple[tuple[str, str], ...],
    operands: Mapping[str, str],
) -> dict[tuple[str, str], float]:
    """Each candidate's `cardinality(determinant) / cardinality(pair)` (SPEC 2.2.13).

    Phase A already measured the determinant, so only the joint count needs a statement.
    """

    if not candidates:
        return {}

    row = pair_cardinalities(execute, dialect, source, candidates, operands, "dbprint_dep")

    if row is None:
        return {}

    return {
        (a, b): min(1.0, base[a].cardinality / row[i])
        for i, (a, b) in enumerate(candidates)
        if row[i]
    }


def timeline(
    execute: Execute,
    dialect: Dialect,
    source: str,
    column: str,
    bucket: str,
    bucket_text: str,
) -> tuple[tuple[str, int], ...]:
    """`column`'s non-null rows counted per `bucket`, ascending (SPEC 2.2.16).

    `bucket_text` renders `bkt.bucket_start`; truncating in a derived table sorts the real value.
    """

    rows = execute(
        f"""
        SELECT
          {indented(bucket_text, 10)} AS bucket_text,
          bkt.cnt
        FROM
          (
            SELECT
              {indented(bucket, 14)} AS bucket_start,
              {dialect.row_count} AS cnt
            FROM
              {indented(source, 14)}
            WHERE
              {column} IS NOT NULL
            GROUP BY
              {"1" if dialect.group_by_ordinal else "bucket_start"}
          ) bkt
        ORDER BY
          bkt.bucket_start
        """,
    ).fetchall()

    return tuple((measured_text(row[0], "timeline"), int(row[1])) for row in rows)


def populated_windows(
    execute: Execute,
    source: str,
    anchor: str,
    subjects: Mapping[str, str],
    render: Callable[[str], str],
) -> dict[str, tuple[str, str]]:
    """Each subject's [from, to] window over the anchor, one statement (SPEC 2.2.4).

    `subjects` maps each name to its operand; bounds render through the anchor's own domain rule.
    """

    if not subjects:
        return {}

    agg_exprs = []
    outer_exprs = []

    for i, operand in enumerate(subjects.values()):
        agg_exprs.append(f"MIN(CASE WHEN {operand} IS NOT NULL THEN {anchor} END) AS from_{i}")
        agg_exprs.append(f"MAX(CASE WHEN {operand} IS NOT NULL THEN {anchor} END) AS to_{i}")
        outer_exprs.append(f"{render(f'agg.from_{i}')} AS from_{i}_text")
        outer_exprs.append(f"{render(f'agg.to_{i}')} AS to_{i}_text")

    row = execute(
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

    return windows_from_row(row, tuple(subjects))


def windows_from_row(
    row: Sequence[Any] | None,
    subjects: tuple[str, ...],
) -> dict[str, tuple[str, str]]:
    """A windows statement's `(from, to)` pair per subject, in order; an empty one is absent."""

    if row is None:
        return {}

    windows: dict[str, tuple[str, str]] = {}

    for i, subject in enumerate(subjects):
        from_text, to_text = row[2 * i], row[2 * i + 1]

        if from_text is not None and to_text is not None:
            windows[subject] = (
                measured_text(from_text, "populated.from"),
                measured_text(to_text, "populated.to"),
            )

    return windows


def sample_distinct(
    scope: TableScope | None,
    n: int,
    estimate: float,
    *,
    direct: Callable[[], list[Any]],
    draw: Callable[[], list[Any] | None],
) -> list[Any]:
    """Up to n distinct values for `looks_like`, read directly from a small or unestimated table.

    Else the engine's sub-draw; a None draw, or one starved below `MIN_SAMPLE_DRAW`, reads directly.
    """

    if estimate <= 0 or estimate < n * SMALL_TABLE_FACTOR:
        return direct()

    values = draw()

    if values is None or _starved(scope, values, n):
        return direct()

    return values


def scoped_estimate(estimate: float | None, scope: TableScope | None) -> float:
    """Rows the scoped read covers: a fraction scales the catalog estimate, a predicate cannot.

    No estimate is -1, which routes to the direct read.
    """

    rows = -1.0 if estimate is None else float(estimate)

    if scope is None or scope.sample is None:
        return rows

    return rows * scope.sample


def table_seed(identity: Identity) -> int:
    """The table's draw seed, hashed from the folded FQN - the artifact's own name for it."""

    return seed_from_fqn(identity.fqn, SEED_MODULUS)


def distinct_values(
    execute: Execute,
    dialect: Dialect,
    source: str,
    column: str,
    selected: str,
    n: int,
    seed: int,
    *,
    conjunct: str = "",
) -> list[Any]:
    """Up to n distinct non-null values of `column` read from `source` as `selected`.

    Ordered by a hash under the table's seed (SPEC 4.1.2), independent of frequency and storage.
    """

    rows = execute(
        f"""
        SELECT
          drw.v
        FROM
          (
            SELECT DISTINCT
              {indented(selected, 14)} AS v
            FROM
              {indented(source, 14)}
            WHERE
              {column} IS NOT NULL{conjunct}
          ) drw
        ORDER BY
          {dialect.seed_hash.format(seed=dialect.placeholder, value="drw.v")}
        LIMIT {int(n)}
        """,
        (str(seed),),
    ).fetchall()

    return [r[0] for r in rows]


def typed_distinct_values(
    execute: Execute,
    source: str,
    column: str,
    n: int,
    seed: int,
    sql_type: str | None,
    *,
    dialect: Dialect,
    render_text: Callable[[str, str], str],
    render_operand: Callable[[str, str], str],
    unsupported: Callable[[str], bool],
) -> list[Any]:
    """`distinct_values` reading a string-like column as its engine text, any other typed column
    through its comparable operand, and an untyped one as written.
    """

    if sql_type is None:
        selected = column
    elif is_string_like(sql_type, unsupported):
        selected = render_text(column, sql_type)
    else:
        selected = render_operand(column, sql_type)

    return distinct_values(execute, dialect, source, column, selected, n, seed)


def _starved(scope: TableScope | None, values: list[Any], n: int) -> bool:
    # Only a predicate can starve a draw; a fraction sizes it to the rate it asked for.
    if scope is None or not scope.filter:
        return False

    return len(values) < min(n, MIN_SAMPLE_DRAW)


def key_sketch(
    execute: Execute,
    table: str,
    column: str,
    canonical: str,
    low64: str,
    k: int,
    *,
    order: str = "h",
) -> list[int]:
    """The k smallest `low64` hashes of `column`'s distinct canonical values (SPEC 2.2.14).

    `low64` reads `dst.v`; `order` sorts it unsigned. Never scoped; `k` inlined (MySQL `%`).
    """

    rows = execute(
        f"""
        SELECT
          {indented(low64, 10)} AS h
        FROM
          (
            SELECT DISTINCT
              {indented(canonical, 14)} AS v
            FROM
              {table} {SOURCE_ALIAS}
            WHERE
              {column} IS NOT NULL
          ) dst
        ORDER BY
          {order}
        LIMIT {int(k)}
        """,
    ).fetchall()

    return [int(r[0]) for r in rows]


def spatial(
    execute: Execute,
    source: str,
    column: str,
    accessors: SpatialAccessors,
) -> tuple[Geometry, Extent | None]:
    """A spatial column's `geometry` and `extent`, from one statement grouped by kind, SRID and
    coordinate flag (SPEC 2.2.4).
    """

    keys = [key for key in (accessors.kind, accessors.srid, accessors.flag) if isinstance(key, str)]
    invalid = "0" if accessors.invalid is None else _counted(accessors.invalid)
    bounds = (
        ["NULL"] * 4
        if accessors.bounds is None
        else [
            call(fn, b)
            for fn, b in zip(("MIN", "MIN", "MAX", "MAX"), accessors.bounds, strict=True)
        ]
    )
    rows = execute(
        f"""
        SELECT
          {listed([*keys, "COUNT(1)", _counted(accessors.empty), invalid, *bounds], 10)}
        FROM
          {indented(source, 10)}
        WHERE
          {column} IS NOT NULL
        GROUP BY
          {listed(keys, 10)}
        """,
    ).fetchall()

    return fold(
        [_spatial_group(accessors, row) for row in rows],
        with_srids=accessors.srid is not None,
        with_validity=accessors.invalid is not None,
    )


def vector(
    execute: Execute,
    source: str,
    column: str,
    *,
    dimension: str | int,
    norm: str | None,
) -> VectorReading:
    """A vector column's `dimension`, `norm` and `zero_count` in one pass (SPEC 2.2.4).

    An `int` dimension is one the type fixes; `norm` None names `norm` and `zero_count` unmeasured.
    """

    norm_expr = "NULL" if norm is None else norm
    measured = derived(
        select_from(
            [f"{dimension} AS dbprint_dimension", f"{norm_expr} AS dbprint_norm"],
            source,
        )
        + f"\nWHERE\n  {column} IS NOT NULL",
        "vec",
    )
    row = execute(
        select_from(
            [
                "COUNT(1)",
                "MIN(vec.dbprint_dimension)",
                "MAX(vec.dbprint_dimension)",
                "MIN(CASE WHEN vec.dbprint_norm > 0 THEN vec.dbprint_norm END)",
                "MAX(CASE WHEN vec.dbprint_norm > 0 THEN vec.dbprint_norm END)",
                _counted("vec.dbprint_norm = 0"),
            ],
            measured,
        ),
    ).fetchone()
    non_null, min_dim, max_dim, min_norm, max_norm, zeros = row

    return VectorReading(
        dimension=(int(min_dim), int(max_dim)) if non_null else None,
        norm=(
            (round_statistic(min_norm), round_statistic(max_norm)) if min_norm is not None else None
        ),
        zero_count=None if norm is None else int(zeros or 0),
        unmeasured=("norm", "zero_count") if norm is None else (),
    )


def table_row_count(
    execute: Execute,
    dialect: Dialect,
    quoted: str,
    rows_scanned: int,
    scope: TableScope | None,
    estimate: Callable[[], int | None],
) -> tuple[int, RowCountMethod]:
    """Rows in the table and how they were obtained (SPEC 2.2.1); a narrowed read takes `estimate`.

    With none, or under `count_exactly`, it counts, so a filter matching nothing never reads as empty.
    """

    if scope is None or not scope.narrows:
        return rows_scanned, "exact"

    found = None if scope.count_exactly else estimate()

    if found is not None:
        return found, "approximate"

    row = execute(f"SELECT {dialect.row_count} FROM {quoted} {SOURCE_ALIAS}").fetchone()

    return (int(row[0]) if row and row[0] is not None else rows_scanned), "exact"


def normalized_cardinality(
    execute: Execute,
    dialect: Dialect,
    source: str,
    column: str,
    text: str,
) -> int:
    """The distinct count of `column` read as `text`, trimmed and case-folded (SPEC 2.2.4)."""

    row = execute(
        f"""
        SELECT
          {dialect.distinct_count.format(dialect.trim_fold.format(text))} AS n
        FROM
          {indented(source, 10)}
        WHERE
          {column} IS NOT NULL
        """,
    ).fetchone()

    return int(row[0]) if row and row[0] is not None else 0


def row_count_of(execute: Execute, source: str) -> int:
    """The rows `source` holds - a part's occurrences, read off its derived source."""

    row = execute(select_from(["COUNT(1)"], source)).fetchone()

    return int(row[0]) if row and row[0] is not None else 0


def instance_shape(
    execute: Execute,
    node: PartSource,
    size_of: Callable[[str], str],
    *,
    distinct: Callable[[str], str] | None = None,
    norm: Callable[[str], str] | None = None,
) -> InstanceShape:
    """An array's, a map's or a document's non-null instances with a size, as wholes (SPEC
    2.2.18): sizes, empties, and the distinct instances where `distinct` counts them.

    `norm` asks for the norm bounds and zero count of a floating-point array of one length.
    """

    size = size_of(node.operand)
    normed = norm is not None
    instances = derived(
        select_from(
            [
                f"{size} AS sz",
                f"{node.operand} AS whole",
                *([f"CASE WHEN {size} > 0 THEN {norm(node.operand)} END AS nrm"] if normed else []),
                f"ROW_NUMBER() OVER (ORDER BY {size}) AS rn",
                "COUNT(1) OVER () AS n",
            ],
            node.source,
        )
        + f"\nWHERE\n  {node.operand} IS NOT NULL\n  AND {size} IS NOT NULL",
        "shp",
    )
    counted = distinct("shp.whole") if distinct is not None else "NULL"
    row = execute(
        select_from(
            [
                "COUNT(1)",
                "MIN(shp.sz)",
                "MAX(shp.sz)",
                "AVG(shp.sz)",
                "MIN(CASE WHEN shp.rn >= CEIL(0.95 * shp.n) THEN shp.sz END)",
                _counted("shp.sz = 0"),
                counted,
                "MIN(CASE WHEN shp.sz > 0 THEN shp.sz END)",
                *(
                    [
                        "MIN(CASE WHEN shp.nrm > 0 THEN shp.nrm END)",
                        "MAX(CASE WHEN shp.nrm > 0 THEN shp.nrm END)",
                        _counted("shp.nrm = 0"),
                    ]
                    if normed
                    else ["NULL", "NULL", "NULL"]
                ),
            ],
            instances,
        ),
    ).fetchone()
    count, lo, hi, avg, p95, empty, cardinality, shortest, low, high, zeros = row

    if not count:
        return InstanceShape(size=None, empty_count=0, cardinality=0 if distinct else None)

    fixed = normed and shortest is not None and shortest == hi

    return InstanceShape(
        size=Length(
            min=int(lo),
            max=int(hi),
            avg=round_statistic(avg),
            p95=round_statistic(p95),
        ),
        empty_count=int(empty or 0),
        cardinality=None if cardinality is None else int(cardinality),
        norm=(round_statistic(low), round_statistic(high)) if fixed and low is not None else None,
        zero_count=int(zeros or 0) if fixed else None,
    )


def map_keys(
    execute: Execute,
    dialect: Dialect,
    entries: str,
    limit: int,
) -> tuple[list[tuple[Any, int]], int]:
    """A map's `limit` most frequent keys with the instances holding each, and its distinct keys.

    `entries` is a `MapEntries` source; ties at the cut fall in key order (SPEC 2.2.18).
    """

    rows = execute(
        f"""
        SELECT
          ent.k,
          {dialect.row_count} AS cnt,
          COUNT(1) OVER () AS n_keys
        FROM
          {indented(entries, 10)}
        GROUP BY
          ent.k
        ORDER BY
          cnt DESC, ent.k ASC
        LIMIT {int(limit)}
        """,
    ).fetchall()

    return [(key, int(count)) for key, count, _ in rows], int(rows[0][2]) if rows else 0


def _counted(condition: str) -> str:
    return call("SUM", f"CASE WHEN {condition} THEN 1 ELSE 0 END")


def _spatial_group(accessors: SpatialAccessors, row: Sequence[Any]) -> SpatialGroup:
    values = iter(row)
    kind = next(values)
    srid = next(values) if isinstance(accessors.srid, str) else accessors.srid
    flag = next(values) if isinstance(accessors.flag, str) else accessors.flag
    count, empty, invalid, min_x, min_y, max_x, max_y = values

    return SpatialGroup(
        kind=ogc_kind(str(kind)),
        srid=0 if srid is None else _srid(srid),
        dimensions=dimensions_of(int(flag)),
        count=int(count),
        empty=int(empty or 0),
        invalid=int(invalid or 0),
        min_x=min_x,
        min_y=min_y,
        max_x=max_x,
        max_y=max_y,
    )


def _srid(value: Any) -> int | str:
    return value if isinstance(value, str) else int(value)
