"""Two-phase batched per-table statistics computation for ClickHouse - Phase B pre-classifies
internally and both MUST converge; native one-pass aggregates keep each phase one statement.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace
from functools import partial
from typing import Any, Literal

from dbprint.config import StatisticsConfig
from dbprint.spec.classification import (
    base_type,
    element_type,
    is_nullable_type,
    is_numeric_type,
    stored_type,
)
from dbprint.spec.distribution import summarize as summarize_frequencies
from dbprint.spec.rounding import (
    measured_value,
    round_statistic,
)
from dbprint.spec.spatial import Extent, Geometry
from .connection import DIALECT, Cursor, exec_query
from .introspect import estimate_row_count
from .rendering import render_domain, render_text, stores_below_microsecond, temporal_shape
from .. import statements
from ..base import (
    ArrayReads,
    BaseStats,
    CardinalityMethod,
    ColumnMeta,
    ColumnProgress,
    ColumnReads,
    ColumnStats,
    Distribution,
    DocumentReads,
    Frequencies,
    Member,
    NullPatterns,
    NumericBlock,
    PartSource,
    PhaseA,
    PhaseB,
    Range,
    RecordReads,
    TableCounts,
    TableScope,
    TopN,
    ValueCount,
    ValueList,
    VectorReading,
    counts_approximately,
    declared_maps,
    declared_members,
    empty_base_stats,
    is_string_like,
    key_literal,
    materialized_name,
    measure_columns,
    numeric_block_from_row,
    phase_a_cost,
    profile_over,
    row_count_or_none,
    run_phase_a,
    unrepresentable_fields,
    value_list_from_rows,
    whole_temporal_block,
)
from ..identifiers import SOURCE_ALIAS, Identity, quote, source_column, string_literal
from ..sql_layout import call, derived, indented, select_from
from ..statements import column_alias


_UNSUPPORTED_TYPES = (
    "array",
    "map",
    "tuple",
    "nested",
    "aggregatefunction",
    "simpleaggregatefunction",
    "dynamic",
    "nothing",
    "union",
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

    source = table_source(identity, scope)
    narrows = scope is not None and scope.narrows
    # The catalog estimate describes the whole table, so a narrowed read counts instead.
    estimate = estimate_row_count(cursor, identity)
    approximate = counts_approximately(estimate, narrows)
    rows_scanned, phase_a = run_phase_a(
        columns,
        phase_a_cost,
        partial(_phase_a_statement, cursor, source, approximate=approximate),
        partial(_null_counts, cursor, source),
        partial(_recount, cursor, source) if approximate else None,
        declines=lambda col: _is_unsupported(col.classified_type),
    )
    row_count, row_count_method = statements.table_row_count(
        partial(exec_query, cursor),
        DIALECT,
        identity.quoted(),
        rows_scanned,
        scope,
        lambda: row_count_or_none(estimate_row_count(cursor, identity)),
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
    *,
    source: str | None = None,
) -> PhaseB:
    """Phase B: the classification-specific statistics, keyed by column name."""

    source = source or table_source(identity, scope)
    reads = ColumnReads(
        value_list=partial(_fetch_value_list, cursor, source),
        numeric_block=partial(_fetch_numeric_block, cursor, source),
        temporal=partial(whole_temporal_block, partial(_fetch_temporal_block, cursor, source)),
        length_p95=partial(_fetch_length_p95, cursor, source),
        spatial=partial(_fetch_spatial, cursor, source),
        vector=partial(_fetch_vector, cursor, source),
    )

    return measure_columns(
        columns,
        config,
        counts,
        base,
        fk_source_columns,
        reads,
        suppress_values=suppress_values,
        on_column=on_column,
        scope=scope,
    )


def table_source(identity: Identity, scope: TableScope | None) -> str:
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


def profile_part(
    cursor: Cursor,
    identity: Identity,
    source: str,
    column: ColumnMeta,
    config: StatisticsConfig,
    *,
    suppress_values: frozenset[str] = frozenset(),
) -> ColumnStats:
    """A part's statistics over its derived `source` (SPEC 2.2.18), as a column's are read."""

    return profile_over(
        column,
        lambda columns: run_phase_a(
            columns,
            phase_a_cost,
            partial(_phase_a_statement, cursor, source, approximate=False),
            partial(_null_counts, cursor, source),
            declines=lambda col: _is_unsupported(col.classified_type),
        ),
        lambda columns, counts, base: compute_columns(
            cursor,
            identity,
            columns,
            config,
            counts,
            base,
            frozenset(),
            suppress_values,
            source=source,
        ),
    )


ARRAYS = ArrayReads(
    elements=lambda node, _element: derived(
        select_from([f"arrayJoin({node.operand}) AS v"], node.source),
        SOURCE_ALIAS,
    ),
    size=lambda operand: f"length({operand})",
    distinct=lambda operand: f"uniqExact({operand})",
    norm=lambda operand: f"L2Norm({operand})",
)


def root_type(column: ColumnMeta) -> str:
    """The type a descent reads a column as: its declared spelling, which names a union's types,
    or the type a `SimpleAggregateFunction` stores.
    """

    return stored_type(column.sql_type)


def element_type_of(_cursor: Cursor, node: PartSource) -> str | None:
    """An array node's element type; a `Nested` column's elements are tuples of its members."""

    if base_type(node.sql_type) == "nested":
        return "Tuple" + node.sql_type.strip()[len("Nested") :]

    return element_type(node.sql_type)


def _clickhouse_members(cursor: Cursor, node: PartSource) -> list[Member]:
    if base_type(node.sql_type) != "dynamic":
        return declared_members(node.sql_type)

    # `Dynamic` declares no members: the types it holds are read off its values.
    held = exec_query(
        cursor,
        select_from([f"DISTINCT dynamicType({node.operand})"], node.source)
        + f"\nWHERE\n  {node.operand} IS NOT NULL",
    ).fetchall()

    return [Member(name=str(t), sql_type=str(t), union=True) for (t,) in sorted(held)]


def _member_source(node: PartSource, member: Member) -> str:
    dynamic = base_type(node.sql_type) == "dynamic"
    literal = string_literal(member.name)

    if member.union:
        tag = "dynamicType" if dynamic else "variantType"
        element = "dynamicElement" if dynamic else "variantElement"
        accessor = f"{element}({node.operand}, {literal})"
        presence = f"{tag}({node.operand}) = {literal}"
    else:
        position = member.name.isdigit() and member.name == str(member.position)
        accessor = (
            f"tupleElement({node.operand}, {member.position})"
            if position
            else f"tupleElement({node.operand}, {literal})"
        )
        presence = f"{node.operand} IS NOT NULL" if is_nullable_type(node.sql_type) else ""

    statement = select_from([f"{accessor} AS v"], node.source)

    return derived(f"{statement}\nWHERE\n  {presence}" if presence else statement, SOURCE_ALIAS)


RECORDS = RecordReads(members=_clickhouse_members, source=_member_source)


# A map may hold one key twice: each instance counts it once, and `m[k]` reads its first value.
MAPS = declared_maps(
    lambda node, _key, _value: (
        [f"arrayJoin(arrayDistinct(mapKeys({node.operand}))) AS k", f"{node.operand}[k] AS v"],
        node.source,
    ),
    size=lambda operand: f"length(mapKeys({operand}))",
    backslash_escapes=True,
)


_DOCUMENT_READS = {
    "String": "JSONExtractString({})",
    "Int64": "JSONExtractInt({})",
    "UInt64": "JSONExtractUInt({})",
    "Double": "JSONExtractFloat({})",
    "Bool": "JSONExtractBool({})",
}


def _json_text(operand: str) -> str:
    # A `JSON` value serializes to its text and a part's raw JSON is text already, nullable or not.
    return f"assumeNotNull(toString({operand}))"


DOCUMENTS = DocumentReads(
    type_of=lambda operand: f"toString(JSONType({_json_text(operand)}))",
    null_name="Null",
    general="JSON",
    object_name="Object",
    array_name="Array",
    key_sql_type="String",
    names=frozenset({"json", "object", "array"}),
    numeric=("UInt64", "Int64", "Double"),
    entries=lambda node: derived(
        select_from(
            [
                f"arrayJoin(arrayDistinct(JSONExtractKeys({_json_text(node.operand)}))) AS k",
                f"JSONExtractRaw({_json_text(node.operand)}, k) AS v",
            ],
            node.source,
        ),
        "ent",
    ),
    elements=lambda node: derived(
        select_from(
            [f"arrayJoin(JSONExtractArrayRaw({_json_text(node.operand)})) AS v"],
            node.source,
        ),
        "ent",
    ),
    read=lambda value, sql_type: _DOCUMENT_READS.get(sql_type, "{}").format(value),
    size=lambda operand: (
        f"CASE JSONType({_json_text(operand)})"
        f"\n  WHEN 'Object' THEN length(arrayDistinct(JSONExtractKeys({_json_text(operand)})))"
        f"\n  WHEN 'Array' THEN JSONLength({_json_text(operand)})\nEND"
    ),
    literal=partial(key_literal, backslash_escapes=True),
)


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

    return statements.null_patterns(
        partial(exec_query, cursor),
        DIALECT,
        table_source(identity, scope),
        columns,
        [source_column(col, DIALECT) for col in columns],
        config,
        counts,
        base,
    )


def probe_grain(
    cursor: Cursor,
    identity: Identity,
    columns: list[ColumnMeta],
    counts: TableCounts,
    candidates: tuple[tuple[str, str], ...],
    scope: TableScope | None = None,
) -> tuple[tuple[str, str], ...]:
    """One batched statement testing every candidate pair. See SPEC 2.2.12."""

    return statements.grain_pairs(
        partial(exec_query, cursor),
        DIALECT,
        table_source(identity, scope),
        counts,
        candidates,
        {col.name: source_column(col, DIALECT) for col in columns},
    )


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

    col = {c.name: c for c in columns}[column]
    cn = source_column(col, DIALECT)
    date_only = temporal_shape(col.classified_type) == "date"

    return statements.timeline(
        partial(exec_query, cursor),
        DIALECT,
        table_source(identity, scope),
        cn,
        _timeline_bucket_expr(cn, unit, date_only=date_only),
        render_domain("bkt.bucket_start", col.classified_type),
    )


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

    source = table_source(identity, scope)
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
        exprs.extend(
            (
                f"{rendered_from} AS from_{column_alias(subject)}",
                f"{rendered_to} AS to_{column_alias(subject)}",
            ),
        )

    row = exec_query(cursor, select_from(exprs, source)).fetchone()

    return statements.windows_from_row(row, subject_columns)


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

    del counts

    return statements.dependency_strengths(
        partial(exec_query, cursor),
        DIALECT,
        table_source(identity, scope),
        base,
        candidates,
        {col.name: source_column(col, DIALECT) for col in columns},
    )


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


def _null_counts(
    cursor: Cursor,
    source: str,
    columns: list[ColumnMeta],
) -> tuple[int, dict[str, int]]:
    return statements.null_counts(
        partial(exec_query, cursor),
        DIALECT,
        source,
        columns,
        lambda col: source_column(col, DIALECT),
    )


def _recount(
    cursor: Cursor,
    source: str,
    columns: list[ColumnMeta],
) -> Sequence[Any] | None:
    return statements.recount(
        partial(exec_query, cursor),
        DIALECT,
        source,
        columns,
        lambda col: source_column(col, DIALECT),
    )


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
        a = column_alias(col.name)
        select_parts.append(f"countIf({cn} IS NULL) AS null_{a}")

        if is_numeric_type(col.classified_type):
            select_parts.extend(
                (
                    f"countIf({cn} = 0) AS zero_{a}",
                    f"countIf({cn} < 0) AS neg_{a}",
                    f"countIf({cn} = trunc({cn})) AS quant_{a}",
                ),
            )
        elif is_string_like(col.classified_type, _is_unsupported):
            # A non-String type (UUID, Enum) has no native `= ''`/`length()`; casting first
            # is what every "string-like" type here actually shares (Postgres does the same).
            rendered = f"toString({cn})"
            select_parts.extend(
                (
                    f"countIf({rendered} = '') AS empty_{a}",
                    # ClickHouse `length()` counts bytes, `lengthUTF8()` characters (SPEC 2.2.4).
                    f"min(lengthUTF8({rendered})) AS lenmin_{a}",
                    f"max(lengthUTF8({rendered})) AS lenmax_{a}",
                    f"avg(lengthUTF8({rendered})) AS lenavg_{a}",
                ),
            )

        select_parts.append(f"{card_fn}({cn}) AS card_{a}")

    row = exec_query(cursor, select_from(select_parts, source)).fetchone()

    if row is None:
        return 0, {
            c.name: empty_base_stats(supported=not _is_unsupported(c.classified_type))
            for c in columns
        }

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
        elif is_string_like(col.classified_type, _is_unsupported):
            empty_count = int(row[idx])
            length_min, length_max = (None if v is None else int(v) for v in row[idx + 1 : idx + 3])
            length_avg = row[idx + 3]
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


def _fetch_value_list(
    cursor: Cursor,
    source: str,
    col: ColumnMeta,
    non_null: int,
    config: StatisticsConfig,
) -> ValueList:
    cn = source_column(col, DIALECT)
    # `toString` stays the ordering key, since it decides the cutoff; a number publishes natively.
    native = is_numeric_type(col.classified_type)
    rows = statements.most_frequent(
        partial(exec_query, cursor),
        DIALECT,
        source,
        render_text(cn, col.classified_type),
        config,
        non_null=cn,
        group=cn,
        extra=(f"any({cn}) AS native",),
    )

    return value_list_from_rows(
        [(value if native else text, cnt) for text, cnt, value in rows],
        non_null,
        config,
    )


def _fetch_vector(cursor: Cursor, source: str, col: ColumnMeta) -> VectorReading:
    cn = source_column(col, DIALECT)
    # `QBit(element, dimension)`: the type fixes the element count; the Array cast is lossless.
    declared = re.search(r"qbit\(\s*\w+\s*,\s*(\d+)", col.classified_type, re.IGNORECASE)

    return statements.vector(
        partial(exec_query, cursor),
        source,
        cn,
        dimension=int(declared.group(1)) if declared else f"length(CAST({cn} AS Array(Float64)))",
        norm=f"L2Norm(CAST({cn} AS Array(Float64)))",
    )


def _fetch_spatial(cursor: Cursor, source: str, col: ColumnMeta) -> tuple[Geometry, Extent | None]:
    cn = source_column(col, DIALECT)
    geo_type = base_type(col.classified_type)
    variants = _geometry_variants(cursor) if geo_type == "geometry" else ()
    points = _points(cn, geo_type, variants)
    # Every vertex as one flat Array(Point), computed once beside the value it came from.
    flattened = derived(
        select_from([f"{cn} AS dbprint_geo", f"{points} AS dbprint_points"], source),
        "spt",
    )
    min_x, min_y, max_x, max_y = (
        call(
            "if",
            "empty(spt.dbprint_points)",
            "NULL",
            f"{fn}(arrayMap(p -> p.{axis}, spt.dbprint_points))",
        )
        for fn, axis in (("arrayMin", 1), ("arrayMin", 2), ("arrayMax", 1), ("arrayMax", 2))
    )

    # The geo types carry no reference system and no validity, and are planar 2D.
    return statements.spatial(
        partial(exec_query, cursor),
        flattened,
        "spt.dbprint_geo",
        statements.SpatialAccessors(
            kind=(
                "variantType(spt.dbprint_geo)"
                if geo_type == "geometry"
                else "toTypeName(spt.dbprint_geo)"
            ),
            srid=None,
            flag=0,
            empty="empty(spt.dbprint_points)",
            invalid=None,
            bounds=(min_x, min_y, max_x, max_y),
        ),
    )


_GEO_VARIANTS = (
    ("Point", "point"),
    ("Ring", "ring"),
    ("LineString", "linestring"),
    ("MultiLineString", "multilinestring"),
    ("Polygon", "polygon"),
    ("MultiPolygon", "multipolygon"),
    ("MultiPoint", "multipoint"),
)


def _geometry_variants(cursor: Cursor) -> tuple[tuple[str, str], ...]:
    # Naming a variant the server's `Geometry` lacks fails the statement, even in an untaken branch.
    row = exec_query(cursor, "SELECT toTypeName(variantType(CAST(NULL AS Geometry)))").fetchone()
    declared = set(re.findall(r"'(\w+)' = ", row[0]))

    return tuple(variant for variant in _GEO_VARIANTS if variant[0] in declared)


def _points(cn: str, geo_type: str, variants: tuple[tuple[str, str], ...] = ()) -> str:
    if geo_type == "point":
        return f"[{cn}]"

    if geo_type in ("ring", "linestring"):
        return cn

    if geo_type != "geometry":
        return call("arrayFlatten", cn)

    # A `Geometry` value is a Variant of the types the server declares, each its own element.
    *branches, (_, last) = (
        (variant, _points(f"variantElement({cn}, '{variant}')", geo_type))
        for variant, geo_type in variants
    )
    tests = [
        arg for variant, points in branches for arg in (f"variantType({cn}) = '{variant}'", points)
    ]

    return call("multiIf", *tests, last)


def _fetch_numeric_block(
    cursor: Cursor,
    source: str,
    col: ColumnMeta,
    non_null: int,
    config: StatisticsConfig,
    *,
    values: bool = True,
) -> NumericBlock:
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

    return numeric_block_from_row(
        row,
        row[4 : 4 + len(keys)] if row else (),
        config,
        None
        if not values
        else lambda: _approximate_distribution_via_top_n(
            cursor,
            source,
            cn,
            non_null,
            config,
            measured_value,
        ),
    )


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
    unrepresentable = unrepresentable_fields(rng, percentiles)

    return rng, percentiles, distribution, unrepresentable, frequencies, values, quantized_count


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
) -> TopN:
    return statements.top_n(
        partial(exec_query, cursor),
        DIALECT,
        source,
        select_expr,
        non_null,
        config,
        value_transform,
        column=group_expr or select_expr,
    )


def _is_unsupported(sql_type: str) -> bool:
    return base_type(sql_type) in _UNSUPPORTED_TYPES
