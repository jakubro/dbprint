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
    is_binary_type,
    is_numeric_type,
    is_spatial_type,
)
from dbprint.spec.percentiles import coherent_percentiles
from dbprint.spec.rounding import (
    measured_value,
    round_statistic,
)
from dbprint.spec.spatial import Extent, Geometry
from .connection import DIALECT, exec_query
from .introspect import row_count_hint
from .rendering import render_binary, render_operand, render_text, temporal_shape
from .. import statements
from ..base import (
    ArrayReads,
    BaseStats,
    ColumnMeta,
    ColumnProgress,
    ColumnReads,
    ColumnStats,
    DocumentReads,
    NullPatterns,
    NumericBlock,
    PartSource,
    PhaseA,
    PhaseB,
    Range,
    RowCountMethod,
    TableCounts,
    TableScope,
    TopN,
    ValueList,
    assemble_column_stats,
    dotted_records,
    empty_base_stats,
    empty_column_stats,
    is_string_like,
    key_literal,
    materialized_name,
    measures_length,
    phase_a_cost,
    pre_classify,
    profile_over,
    run_phase_a,
    run_phase_b,
    spatial_column_stats,
    temporal_with_top_n,
)
from ..identifiers import SOURCE_ALIAS, Identity
from ..sql_layout import derived, select_from
from ..statements import column_alias


if TYPE_CHECKING:
    from .connection import Cursor


# `materialize()`'s scratch copy self-expires this far out - generous for any single run,
# bounded enough that a killed process leaves nothing to find and drop by hand.
_SCRATCH_TABLE_EXPIRATION_HOURS = 6


# Engine units as BigQuery date parts (SPEC 2.2.16); its `WEEK` opens on Sunday, so `ISOWEEK`.
_TIMELINE_DATE_PARTS = {"day": "DAY", "week": "ISOWEEK", "month": "MONTH"}

_UNSUPPORTED_TYPES = (
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
        phase_a_cost,
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
    *,
    source: str | None = None,
) -> PhaseB:
    """Phase B: the classification-specific statistics, keyed by column name. Scalar aggregates
    fuse into one statement; the value list stays per-column, since `APPROX_TOP_COUNT` ties.
    """

    if not columns:
        return PhaseB({})

    if counts.rows_scanned == 0:
        if scope is not None and scope.narrows:
            return PhaseB({})

        return PhaseB(
            {
                c.name: spatial_column_stats(
                    c,
                    base[c.name].null_count,
                    0.0,
                    partial(_fetch_spatial, cursor, identity, _table_source(identity, scope)),
                )
                if is_spatial_type(c.classified_type)
                else empty_column_stats(
                    c,
                    supported=base[c.name].supported,
                    method="approximate",
                )
                for c in columns
            },
        )

    source = source or _table_source(identity, scope)
    pre_by_col = {
        col.name: pre_classify(
            col,
            base[col.name].cardinality,
            config,
            col.name in fk_source_columns,
            supported=base[col.name].supported,
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

        reads = ColumnReads(
            value_list=partial(_fetch_value_list, cursor, identity, source),
            numeric_block=partial(_numeric_block, cursor, identity, source, block),
            temporal=partial(_temporal, cursor, identity, source, block),
            length_p95=lambda _col: round_statistic(block.get("length_p95")),
            spatial=partial(_fetch_spatial, cursor, identity, source),
        )

        return assemble_column_stats(
            col,
            base[col.name],
            counts.rows_scanned,
            pre_by_col[col.name],
            config,
            reads,
            suppressed=col.name in suppress_values,
        )

    return run_phase_b(columns, measure, batch)


def table_source(identity: Identity, scope: TableScope | None) -> str:
    """The FROM expression a table's statistics read, which a descent derives its parts from."""

    return _table_source(identity, scope)


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
            partial(_phase_a_statement, cursor, identity, source),
            partial(_null_counts, cursor, identity, source),
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
        select_from(["elm AS v"], f"{node.source},\nUNNEST({node.operand}) AS elm"),
        SOURCE_ALIAS,
    ),
    size=lambda operand: f"ARRAY_LENGTH({operand})",
    distinct=lambda operand: f"COUNT(DISTINCT TO_JSON_STRING({operand}))",
    norm=lambda operand: f"(SELECT SQRT(SUM(elm * elm)) FROM UNNEST({operand}) AS elm)",
)


RECORDS = dotted_records(DIALECT)


_DOCUMENT_READS = {
    "string": "JSON_VALUE({})",
    "number": "SAFE_CAST(JSON_VALUE({}) AS BIGNUMERIC)",
    "boolean": "BOOL({})",
}


def _document_entries(node: PartSource) -> str:
    # `JSON_KEYS` double-quotes a key holding a special character; the path step wants it bare.
    key = "IF(STARTS_WITH(jky, '\"'), JSON_VALUE(PARSE_JSON(jky)), jky)"
    keyed = select_from(
        [f"{key} AS k", f"{node.operand} AS doc"],
        f"{node.source},\nUNNEST(JSON_KEYS({node.operand}, 1)) AS jky",
    )
    objects = derived(f"{keyed}\nWHERE\n  JSON_TYPE({node.operand}) = 'object'", "obj")

    return derived(select_from(["obj.k AS k", "obj.doc[obj.k] AS v"], objects), "ent")


DOCUMENTS = DocumentReads(
    type_of=lambda operand: f"JSON_TYPE({operand})",
    null_name="null",
    general="JSON",
    object_name="object",
    array_name="array",
    key_sql_type="STRING",
    names=frozenset({"json", "object", "array"}),
    numeric=("number",),
    entries=_document_entries,
    elements=lambda node: derived(
        select_from(
            ["elm AS v"],
            f"{node.source},\nUNNEST(JSON_QUERY_ARRAY({node.operand})) AS elm",
        ),
        "ent",
    ),
    read=lambda value, sql_type: _DOCUMENT_READS.get(sql_type, "{}").format(value),
    size=lambda operand: (
        f"CASE JSON_TYPE({operand})"
        f"\n  WHEN 'object' THEN ARRAY_LENGTH(JSON_KEYS({operand}, 1))"
        f"\n  WHEN 'array' THEN ARRAY_LENGTH(JSON_QUERY_ARRAY({operand}))\nEND"
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
        _table_source(identity, scope),
        columns,
        [identity.source_column(col.name) for col in columns],
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
        _table_source(identity, scope),
        counts,
        candidates,
        {col.name: identity.source_column(col.name) for col in columns},
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
    cn = identity.source_column(column)
    truncate = _timeline_truncation(col.classified_type)

    return statements.timeline(
        partial(exec_query, cursor),
        DIALECT,
        _table_source(identity, scope),
        cn,
        f"{truncate}({cn}, {_TIMELINE_DATE_PARTS[unit]})",
        "bkt.bucket_start",
    )


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
        _table_source(identity, scope),
        base,
        candidates,
        {col.name: identity.source_column(col.name) for col in columns},
    )


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

    estimate = None if scope.count_exactly else row_count_hint(cursor, project, identity)

    if estimate is not None:
        return estimate, "approximate"

    row = exec_query(cursor, f"SELECT COUNT(1) FROM {identity.quoted()} {SOURCE_ALIAS}").fetchone()

    return (int(row[0]) if row and row[0] is not None else rows_scanned), "exact"


def _null_counts(
    cursor: Cursor,
    identity: Identity,
    source: str,
    columns: list[ColumnMeta],
) -> tuple[int, dict[str, int]]:
    return statements.null_counts(
        partial(exec_query, cursor),
        DIALECT,
        source,
        columns,
        lambda col: identity.source_column(col.name),
    )


def _recount(
    cursor: Cursor,
    identity: Identity,
    source: str,
    columns: list[ColumnMeta],
) -> Sequence[Any] | None:
    return statements.recount(
        partial(exec_query, cursor),
        DIALECT,
        source,
        columns,
        lambda col: render_operand(identity.source_column(col.name), col.classified_type),
        prefix="dbprint_exact_",
    )


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
        a = column_alias(col.name)
        select_parts.append(f"COUNTIF({cn} IS NULL) AS dbprint_null_{a}")

        if is_numeric_type(col.classified_type):
            select_parts.append(f"COUNTIF({cn} = 0) AS dbprint_zero_{a}")
            select_parts.append(f"COUNTIF({cn} < 0) AS dbprint_neg_{a}")
            select_parts.append(f"COUNTIF({cn} = CAST({cn} AS INT64)) AS dbprint_quant_{a}")
        elif measures_length(col.classified_type, _is_unsupported):
            empty_condition, length_expr = _length_exprs(cn, col.classified_type)
            select_parts.append(f"COUNTIF({empty_condition}) AS dbprint_empty_{a}")
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
        return 0, {
            c.name: empty_base_stats(
                supported=not _is_unsupported(c.classified_type),
                method="approximate",
            )
            for c in columns
        }

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
        elif measures_length(col.classified_type, _is_unsupported):
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

        if pre in ("unsupported", "json", "spatial"):
            continue

        cn = identity.source_column(col.name)
        a = column_alias(col.name)
        plan: dict[str, Any] = {}

        if base[col.name].length_min is not None:
            _, length_expr = _length_exprs(cn, col.classified_type)
            select_parts.append(f"APPROX_QUANTILES({length_expr}, 100)[OFFSET(95)] AS lenp95_{a}")
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


def _length_exprs(cn: str, sql_type: str) -> tuple[str, str]:
    if is_binary_type(sql_type):
        return f"LENGTH({cn}) = 0", f"LENGTH({cn})"

    return f"{render_text(cn, sql_type)} = ''", f"LENGTH(CAST({cn} AS STRING))"


def _fetch_spatial(
    cursor: Cursor,
    identity: Identity,
    source: str,
    col: ColumnMeta,
) -> tuple[Geometry, Extent | None]:
    cn = identity.source_column(col.name)

    # A GEOGRAPHY is always 2D on reference system 4326 and cannot hold an invalid value.
    return statements.spatial(
        partial(exec_query, cursor),
        source,
        cn,
        statements.SpatialAccessors(
            kind=f"ST_GEOMETRYTYPE({cn})",
            srid=4326,
            flag=0,
            empty=f"ST_ISEMPTY({cn})",
            invalid=None,
            bounds=(
                f"ST_BOUNDINGBOX({cn}).xmin",
                f"ST_BOUNDINGBOX({cn}).ymin",
                f"ST_BOUNDINGBOX({cn}).xmax",
                f"ST_BOUNDINGBOX({cn}).ymax",
            ),
        ),
    )


def _fetch_value_list(
    cursor: Cursor,
    identity: Identity,
    source: str,
    col: ColumnMeta,
    non_null: int,
    config: StatisticsConfig,
) -> ValueList:
    """The exact top-N value list for one column - deterministic `GROUP BY`, not
    `APPROX_TOP_COUNT`. See `compute_columns`'s docstring for why this stays per-column.
    """

    cn = identity.source_column(col.name)
    select_expr = (
        render_binary(cn)
        if is_binary_type(col.classified_type)
        else render_text(cn, col.classified_type)
        if is_string_like(col.classified_type, _is_unsupported)
        else cn
    )

    return statements.value_list(
        partial(exec_query, cursor),
        DIALECT,
        source,
        select_expr,
        non_null,
        config,
        column=cn,
        group="rendered",
    )


def _approximate_distribution_via_top_n(
    cursor: Cursor,
    source: str,
    select_expr: str,
    group_expr: str,
    non_null: int,
    config: StatisticsConfig,
    value_transform: Any,
) -> TopN:
    return statements.top_n(
        partial(exec_query, cursor),
        DIALECT,
        source,
        select_expr,
        non_null,
        config,
        value_transform,
        column=group_expr,
        group="rendered",
    )


def _numeric_block(
    cursor: Cursor,
    identity: Identity,
    source: str,
    block: dict[str, Any],
    col: ColumnMeta,
    non_null: int,
    config: StatisticsConfig,
    *,
    values: bool = True,
) -> NumericBlock:
    cn = identity.source_column(col.name)
    distribution, frequencies, listed = (
        _approximate_distribution_via_top_n(
            cursor,
            source,
            cn,
            cn,
            non_null,
            config,
            measured_value,
        )
        if values
        else (None, None, None)
    )
    quantiles = block.get("qs") or []
    rng = Range(
        min=round_statistic(block.get("mn"), exact_int=True),
        max=round_statistic(block.get("mx"), exact_int=True),
    )
    percentiles = coherent_percentiles(
        {
            f"p{p:02d}": round_statistic(quantiles[p])
            for p in config.percentiles
            if p < len(quantiles)
        },
        rng.min,
        rng.max,
    )

    return (
        rng,
        percentiles,
        distribution,
        frequencies,
        listed,
        round_statistic(block.get("avg")),
        round_statistic(block.get("sum"), exact_int=True),
    )


def _temporal(
    cursor: Cursor,
    identity: Identity,
    source: str,
    block: dict[str, Any],
    stats: ColumnStats,
    col: ColumnMeta,
    non_null: int,
    config: StatisticsConfig,
) -> ColumnStats:
    quantiles = block.get("qs") or []
    rng = Range(
        min=measured_value(block.get("mn"), "range.min"),
        max=measured_value(block.get("mx"), "range.max"),
        span_days=int(block["span"]) if block.get("span") is not None else 0,
    )
    percentiles = {
        f"p{p:02d}": measured_value(quantiles[p], f"percentiles.p{p:02d}")
        for p in config.percentiles
        if p < len(quantiles)
    }
    quant = block.get("quant")
    cn = identity.source_column(col.name)

    return temporal_with_top_n(
        stats,
        rng,
        percentiles,
        int(quant) if quant is not None else None,
        lambda: _approximate_distribution_via_top_n(
            cursor,
            source,
            cn,
            cn,
            non_null,
            config,
            measured_value,
        ),
    )


def _is_unsupported(sql_type: str) -> bool:
    return base_type(sql_type) in _UNSUPPORTED_TYPES
