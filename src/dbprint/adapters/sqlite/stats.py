"""Two-phase batched per-table statistics computation - Phase A pre-classifies internally to
steer Phase B, the engine re-applies SPEC 3.2 independently, and both MUST converge.

SQLite has no percentile aggregate in every build and no seeded sample clause, so percentiles
read a ranked derived table and a draw is a registered hash of the table's seed and the row.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal

from dbprint.config import StatisticsConfig
from dbprint.spec.classification import (
    has_day_resolution,
    is_binary_type,
    is_boolean_type,
    is_numeric_type,
    is_string_like_type,
)
from dbprint.spec.distribution import summarize as summarize_frequencies
from dbprint.spec.rounding import measured_value, round_statistic
from . import introspect
from .connection import DIALECT, Cursor, exec_query
from .rendering import finite_value, instant, render_text, render_value
from .. import statements
from ..base import (
    BaseStats,
    ColumnMeta,
    ColumnProgress,
    ColumnReads,
    ColumnStats,
    Distribution,
    DocumentReads,
    Frequencies,
    NullPatterns,
    NumericBlock,
    PartSource,
    PhaseA,
    PhaseB,
    Range,
    TableCounts,
    TableScope,
    TopN,
    ValueCount,
    ValueList,
    empty_base_stats,
    key_literal,
    materialized_name,
    measure_columns,
    measures_length,
    numeric_block_from_row,
    numeric_row,
    phase_a_cost,
    pre_classify,
    profile_over,
    run_phase_a,
    unrepresentable_fields,
    value_list_from_rows,
    whole_temporal_block,
)
from ..identifiers import SOURCE_ALIAS, Identity, quote, quote_path, source_column
from ..sql_layout import derived, indented, listed, select_from
from ..statements import column_alias


KNOWN_TYPES: tuple[str, ...] = ()

_RANKED = "rnk"
_BUCKETS = {
    "day": "DATE({})",
    "week": "DATE({}, '-6 days', 'weekday 1')",
    "month": "DATE({}, 'start of month')",
}
_COLLATIONS = {"nocase": "NOCASE", "rtrim": "RTRIM"}
# `PRINTF('%017.9f', ...)` of a Julian day number, which sorts as the instant it names.
_INSTANT_KEY_WIDTH = 17


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
    rows_scanned, phase_a = run_phase_a(
        columns,
        phase_a_cost,
        partial(_phase_a_statement, cursor, source),
        partial(_null_counts, cursor, source),
        declines=lambda col: _is_unsupported(col.classified_type),
    )
    row_count, row_count_method = statements.table_row_count(
        partial(exec_query, cursor),
        DIALECT,
        identity.quoted(),
        rows_scanned,
        scope,
        lambda: None,
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
    """Phase B: the classification-specific statistics, keyed by column name.

    `source` replaces the table's own, for a part read through its derived rows.
    """

    source = source or table_source(identity, scope)
    reads = ColumnReads(
        value_list=partial(_fetch_value_list, cursor, source),
        numeric_block=partial(_fetch_numeric_block, cursor, source),
        temporal=partial(whole_temporal_block, partial(_fetch_temporal_block, cursor, source)),
        length_p95=partial(_fetch_length_p95, cursor, source),
    )

    return measure_columns(
        columns,
        config,
        counts,
        _published_lengths(columns, config, base, fk_source_columns),
        fk_source_columns,
        reads,
        suppress_values=suppress_values,
        on_column=on_column,
        scope=scope,
    )


def table_source(identity: Identity, scope: TableScope | None) -> str:
    """The FROM expression every phase reads - rebuilt per phase, the seed re-derived from the
    table's name, so every call produces the same text and the same rows.
    """

    return _source(identity, scope, statements.table_seed(identity))


def row_key(identity: Identity, alias: str | None = None) -> str:
    """Every column's value spelled with its storage class, joined - what a row's draw hashes."""

    prefix = f"{alias}." if alias else ""
    quoted = [prefix + quote(name, DIALECT) for name in identity.columns.values()]

    return " || ',' || ".join(f"QUOTE({column})" for column in quoted) or "NULL"


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
            partial(_phase_a_statement, cursor, source),
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


def _document_entries(node: PartSource) -> str:
    objects = derived(
        f"""
        SELECT
          {node.operand} AS doc
        FROM
          {indented(node.source, 10)}
        WHERE
          JSON_TYPE({node.operand}) = 'object'
        """,
        "obj",
    )

    return derived(
        f"""
        SELECT
          jse.key AS k,
          obj.doc -> jse.fullkey AS v
        FROM
          {indented(objects, 10)}
          JOIN JSON_EACH(obj.doc) jse ON 1
        """,
        "ent",
    )


def _document_elements(node: PartSource) -> str:
    arrays = derived(
        f"""
        SELECT
          {node.operand} AS doc
        FROM
          {indented(node.source, 10)}
        WHERE
          JSON_TYPE({node.operand}) = 'array'
        """,
        "arr",
    )

    return derived(
        f"""
        SELECT
          arr.doc -> jse.fullkey AS v
        FROM
          {indented(arrays, 10)}
          JOIN JSON_EACH(arr.doc) jse ON 1
        """,
        "ent",
    )


DOCUMENTS = DocumentReads(
    type_of=lambda operand: f"JSON_TYPE({operand})",
    null_name="null",
    general="JSON",
    object_name="object",
    array_name="array",
    key_sql_type="TEXT",
    names=frozenset({"json", "jsonb", "object", "array"}),
    numeric=("integer", "real"),
    entries=_document_entries,
    elements=_document_elements,
    read=lambda value, sql_type: (
        f"({value} ->> '$')" if sql_type in ("text", "integer", "real", "boolean") else value
    ),
    size=lambda operand: (
        f"CASE\n  WHEN JSON_TYPE({operand}) = 'object' THEN ("
        f"\n    SELECT\n      COUNT(DISTINCT jsk.key)\n    FROM\n      JSON_EACH({operand}) jsk\n  )"
        f"\n  WHEN JSON_TYPE({operand}) = 'array' THEN JSON_ARRAY_LENGTH({operand})\nEND"
    ),
    literal=key_literal,
    aliases={"true": "boolean", "false": "boolean"},
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
        {col.name: _comparable(col) for col in columns},
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
    """One grouped statement bucketing `column` at `unit` grain, each bucket an ISO date - the
    one temporal value SQLite writes that no row stores (SPEC 2.2.4, 2.2.16).
    """

    del counts

    cn = source_column({c.name: c for c in columns}[column], DIALECT)

    return statements.timeline(
        partial(exec_query, cursor),
        DIALECT,
        table_source(identity, scope),
        cn,
        _BUCKETS[unit].format(cn),
        "bkt.bucket_start",
    )


def compute_populated_windows(
    cursor: Cursor,
    identity: Identity,
    columns: list[ColumnMeta],
    counts: TableCounts,
    anchor_column: str,
    subject_columns: tuple[str, ...],
    scope: TableScope | None = None,
) -> dict[str, tuple[str, str]]:
    """One statement, two conditional aggregates per subject column (SPEC 2.2.4) - each bound
    the anchor's stored text, chosen in instant order through a sortable key.
    """

    del counts

    by_name = {col.name: col for col in columns}
    anchor = source_column(by_name[anchor_column], DIALECT)
    key = f"PRINTF('%0{_INSTANT_KEY_WIDTH}.9f', {instant(anchor)}) || {anchor}"

    return statements.populated_windows(
        partial(exec_query, cursor),
        table_source(identity, scope),
        key,
        {subject: source_column(by_name[subject], DIALECT) for subject in subject_columns},
        lambda expr: f"SUBSTR({expr}, {_INSTANT_KEY_WIDTH + 1})",
    )


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
        {col.name: _comparable(col) for col in columns},
    )


def materialize(cursor: Cursor, identity: Identity, scope: TableScope) -> TableScope:
    """Copy the drawn fraction into a session-lifetime table in `temp` - created with each
    column's declared type and collation, which `CREATE TABLE ... AS SELECT` would drop, and
    `STRICT` where the table is, so no affinity converts a value on the way.
    """

    name = materialized_name(identity.fqn)
    target = quote_path(("temp", name), DIALECT)
    definitions = [
        " ".join(
            part
            for part in (
                quote(col.name, DIALECT),
                col.sql_type,
                f"COLLATE {_COLLATIONS[col.collation]}" if col.collation else "",
            )
            if part
        )
        for col in introspect.declared_columns(cursor, identity)
    ]
    strict = " STRICT" if introspect.is_strict(cursor, identity) else ""
    exec_query(cursor, f"CREATE TABLE {target} ({', '.join(definitions)}){strict}")
    exec_query(
        cursor,
        f"INSERT INTO {target} SELECT {SOURCE_ALIAS}.* FROM {identity.quoted()} {SOURCE_ALIAS} "
        f"WHERE DBPRINT_DRAW({statements.table_seed(identity)}, "
        f"{row_key(identity, SOURCE_ALIAS)}) < {scope.sample}",
    )

    return replace(scope, materialized=name)


def release(cursor: Cursor, scope: TableScope) -> None:
    """Drop the copied sample; closing the session would drop it anyway, this frees it sooner."""

    if scope.materialized is None:
        return

    exec_query(cursor, f"DROP TABLE IF EXISTS {quote_path(('temp', scope.materialized), DIALECT)}")


def _source(identity: Identity, scope: TableScope | None, seed: int) -> str:
    base = identity.quoted()

    if scope is None or not scope.narrows:
        return f"{base} {SOURCE_ALIAS}"
    elif scope.materialized is not None:
        return f"{quote_path(('temp', scope.materialized), DIALECT)} {SOURCE_ALIAS}"
    elif scope.sample is not None:
        drawn = f"DBPRINT_DRAW({seed}, {row_key(identity)}) < {scope.sample}"

        return derived(f"SELECT * FROM {base} WHERE {drawn}", SOURCE_ALIAS)
    else:
        return derived(f"SELECT * FROM {base} WHERE {scope.filter}", SOURCE_ALIAS)


def _published_lengths(
    columns: list[ColumnMeta],
    config: StatisticsConfig,
    base: dict[str, BaseStats],
    fk_source_columns: frozenset[str],
) -> dict[str, BaseStats]:
    """Phase A's counts, less `length` where a list-classified column's published `sql_type`
    carries no string - the type SPEC 2.2.3's cell reads, which SQLite's values may not follow.
    """

    out = dict(base)

    for col in columns:
        stats = base.get(col.name)

        if stats is None or _string_valued(col.sql_type):
            continue

        pre = pre_classify(
            col,
            stats.cardinality,
            config,
            col.name in fk_source_columns,
            supported=stats.supported,
        )

        if pre in ("categorical", "foreign_key_candidate"):
            out[col.name] = replace(
                stats,
                length_min=None,
                length_max=None,
                length_avg=None,
                length_p95=None,
            )

    return out


def _string_valued(sql_type: str) -> bool:
    return is_string_like_type(sql_type) or is_binary_type(sql_type)


def _measures_length(col: ColumnMeta) -> bool:
    return measures_length(col.classified_type, _is_unsupported) or _string_valued(col.sql_type)


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


def _phase_a_statement(
    cursor: Cursor,
    source: str,
    columns: list[ColumnMeta],
) -> tuple[int, dict[str, BaseStats]]:
    select_parts: list[str] = ["COUNT(1) AS row_count"]

    for col in columns:
        cn = source_column(col, DIALECT)
        alias = column_alias(col.name)
        select_parts.append(f"COUNT(1) - COUNT({cn}) AS null_{alias}")

        if is_numeric_type(col.classified_type):
            fv = finite_value(cn, col.classified_type)
            select_parts.extend(
                (
                    f"COUNT(CASE WHEN {cn} = 0 THEN 1 END) AS zero_{alias}",
                    f"COUNT(CASE WHEN {fv} < 0 THEN 1 END) AS neg_{alias}",
                    (
                        f"COUNT(CASE WHEN TYPEOF({fv}) = 'integer' OR {fv} = ROUND({fv}) THEN 1 END) "
                        f"AS quant_{alias}"
                    ),
                ),
            )

        if _measures_length(col):
            empty_condition, length_expr = _length_exprs(cn, col.classified_type)
            select_parts.extend(
                (
                    f"COUNT(CASE WHEN {empty_condition} THEN 1 END) AS empty_{alias}",
                    f"MIN({length_expr}) AS lenmin_{alias}",
                    f"MAX({length_expr}) AS lenmax_{alias}",
                    f"AVG({length_expr}) AS lenavg_{alias}",
                ),
            )

        select_parts.append(f"COUNT(DISTINCT {cn}) AS card_{alias}")

    row = exec_query(cursor, select_from(select_parts, source)).fetchone()

    if row is None:
        return 0, {c.name: empty_base_stats(supported=True) for c in columns}

    row_count = int(row[0])
    out: dict[str, BaseStats] = {}
    idx = 1

    for col in columns:
        null_count = int(row[idx])
        idx += 1
        zero_count = negative_count = empty_count = quantized_count = None
        length_min = length_max = length_avg = None

        if is_numeric_type(col.classified_type):
            zero_count, negative_count, quantized_count = (int(v) for v in row[idx : idx + 3])
            idx += 3

        if _measures_length(col):
            empty_count = int(row[idx])
            length_min, length_max, length_avg = row[idx + 1 : idx + 4]
            idx += 4

        cardinality = int(row[idx])
        idx += 1
        out[col.name] = BaseStats(
            null_count=null_count,
            cardinality=cardinality,
            cardinality_method="exact",
            zero_count=zero_count,
            negative_count=negative_count,
            empty_count=empty_count,
            quantized_count=quantized_count,
            length_min=None if length_min is None else int(length_min),
            length_max=None if length_max is None else int(length_max),
            length_avg=length_avg,
            length_p95=None,
        )

    return row_count, out


def _length_exprs(cn: str, sql_type: str) -> tuple[str, str]:
    if is_binary_type(sql_type):
        return f"LENGTH({cn}) = 0", f"LENGTH({cn})"

    return f"{render_text(cn, sql_type)} = ''", f"LENGTH({render_text(cn, sql_type)})"


def _fetch_length_p95(cursor: Cursor, source: str, col: ColumnMeta) -> float | None:
    cn = source_column(col, DIALECT)
    _, length_expr = _length_exprs(cn, col.classified_type)
    row = exec_query(
        cursor,
        f"""
        SELECT
          {indented(_interpolated(0.95), 10)} AS p95
        FROM
          {indented(_ranked(source, length_expr, f"{cn} IS NOT NULL"), 10)}
        """,
    ).fetchone()

    return None if row is None or row[0] is None else round_statistic(row[0])


def _fetch_value_list(
    cursor: Cursor,
    source: str,
    col: ColumnMeta,
    non_null: int,
    config: StatisticsConfig,
) -> ValueList:
    cn = source_column(col, DIALECT)
    rows = statements.most_frequent(
        partial(exec_query, cursor),
        DIALECT,
        source,
        render_value(cn, col.classified_type),
        config,
        non_null=cn,
        group=cn,
    )

    if is_boolean_type(col.classified_type):
        rows = [(bool(value), *rest) for value, *rest in rows]

    return value_list_from_rows(rows, non_null, config)


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
    fv = finite_value(cn, col.classified_type)
    numbers = f"TYPEOF({cn}) IN ('integer', 'real')"
    counted = (
        [
            f"(SELECT COUNT({fv}) FROM {source} WHERE {numbers}) AS fin_val",
            f"(SELECT COUNT({cn}) FROM {source} WHERE {numbers}) AS nn_val",
        ]
        if fv != cn
        else []
    )
    ranked = _ranked(source, fv, f"{numbers}\n  AND {fv} IS NOT NULL")
    pct_select = [f"{_interpolated(p / 100.0)} AS p_{p:02d}" for p in config.percentiles]

    def read(aggregates: bool) -> Any:
        mean = f"AVG({_RANKED}.v)" if aggregates else "NULL"
        total = f"SUM({_RANKED}.v)" if aggregates else "NULL"

        return exec_query(
            cursor,
            f"""
            SELECT
              MIN({_RANKED}.v) AS mn,
              MAX({_RANKED}.v) AS mx,
              {mean} AS avg_val,
              {total} AS sum_val,
              {listed([*counted, *pct_select], 14)}
            FROM
              {indented(ranked, 14)}
            """,
        ).fetchone()

    row, failure = numeric_row(read)
    first_percentile = 4 + len(counted)

    return numeric_block_from_row(
        row,
        row[first_percentile:] if row else (),
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
        finite=row[4] if row and counted else None,
        non_null=row[5] if row and counted else None,
        failure=failure,
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
    """Bounds, percentiles and listed values as the stored text, chosen in instant order (SPEC
    2.2.4) - each percentile the value at rank `CEIL(p * n)`.
    """

    cn = source_column(col, DIALECT)
    day_aligned = has_day_resolution(col.classified_type)
    readable = f"TYPEOF({cn}) = 'text'\n  AND {instant(cn)} IS NOT NULL"
    ranked = _ranked(source, cn, readable, order=f"{instant(cn)}, {cn} COLLATE BINARY")
    selected = [
        f"MAX(CASE WHEN {_RANKED}.rn = 1 THEN {_RANKED}.v END) AS mn",
        f"MAX(CASE WHEN {_RANKED}.rn = {_RANKED}.n THEN {_RANKED}.v END) AS mx",
        *(
            f"MAX(CASE WHEN {_RANKED}.rn = {_ceiling(f'{p / 100.0} * {_RANKED}.n')} "
            f"THEN {_RANKED}.v END) AS p_{p:02d}"
            for p in config.percentiles
        ),
    ]

    if day_aligned:
        selected.append(f"COUNT(CASE WHEN {_at_midnight(f'{_RANKED}.v')} THEN 1 END) AS quant")

    row = exec_query(
        cursor,
        f"""
        SELECT
          {listed(selected, 10)}
        FROM
          {indented(ranked, 10)}
        """,
    ).fetchone()

    if row is None or row[0] is None:
        empty_range = Range(min=None, max=None, span_days=0)

        return empty_range, {}, "uniform", (), summarize_frequencies([]), (), None

    keys = config.percentiles
    rng = Range(
        min=measured_value(row[0], "range.min"),
        max=measured_value(row[1], "range.max"),
        span_days=_span_days(row[0], row[1]),
    )
    percentiles = {
        f"p{p:02d}": measured_value(text, f"percentiles.p{p:02d}")
        for p, text in zip(keys, row[2 : 2 + len(keys)], strict=True)
    }
    quantized_count = int(row[2 + len(keys)]) if day_aligned else None
    distribution, frequencies, values = _approximate_distribution_via_top_n(
        cursor,
        source,
        cn,
        non_null,
        config,
        lambda v: v,
    )

    return (
        rng,
        percentiles,
        distribution,
        unrepresentable_fields(rng, percentiles),
        frequencies,
        values,
        quantized_count,
    )


def _approximate_distribution_via_top_n(
    cursor: Cursor,
    source: str,
    column: str,
    non_null: int,
    config: StatisticsConfig,
    value_transform: Callable[[Any], Any],
) -> TopN:
    return statements.top_n(
        partial(exec_query, cursor),
        DIALECT,
        source,
        column,
        non_null,
        config,
        value_transform,
        column=column,
    )


def _ranked(source: str, value: str, condition: str, *, order: str | None = None) -> str:
    return derived(
        f"""
        SELECT
          {value} AS v,
          ROW_NUMBER() OVER (
            ORDER BY
              {order or value}
          ) AS rn,
          COUNT(1) OVER () AS n
        FROM
          {indented(source, 10)}
        WHERE
          {indented(condition, 10)}
        """,
        _RANKED,
    )


def _interpolated(fraction: float) -> str:
    """The `fraction` percentile of a ranked table, interpolated between the two ranks around it
    as `PERCENTILE_CONT` interpolates.
    """

    position = f"{fraction} * ({_RANKED}.n - 1)"
    lower = f"MAX(CASE WHEN {_RANKED}.rn = CAST({position} AS INTEGER) + 1 THEN {_RANKED}.v END)"
    upper = f"MAX(CASE WHEN {_RANKED}.rn = CAST({position} AS INTEGER) + 2 THEN {_RANKED}.v END)"
    whole = f"{fraction} * (MAX({_RANKED}.n) - 1)"

    return (
        f"{lower} + ({whole} - CAST({whole} AS INTEGER)) * (COALESCE({upper}, {lower}) - {lower})"
    )


def _ceiling(expr: str) -> str:
    return f"(CAST({expr} AS INTEGER) + ({expr} > CAST({expr} AS INTEGER)))"


def _at_midnight(value: str) -> str:
    """Whether a stored date-time text names midnight, in the frame its own text gives
    (SPEC 2.2.4): nothing but zeros after the date, then nothing, `Z` or an offset.
    """

    rest = f"LTRIM(SUBSTR({value}, 12), '0:. ')"

    return f"({rest} IN ('', 'Z', 'z') OR {rest} GLOB '[+-]*')"


def _comparable(col: ColumnMeta) -> str:
    """`col` as its collation compares it - a pair is counted through `QUOTE`, which is BINARY."""

    cn = source_column(col, DIALECT)

    if col.collation == "nocase":
        return f"CASE WHEN TYPEOF({cn}) = 'text' THEN LOWER({cn}) ELSE {cn} END"
    elif col.collation == "rtrim":
        return f"CASE WHEN TYPEOF({cn}) = 'text' THEN RTRIM({cn}, ' ') ELSE {cn} END"
    else:
        return cn


def _span_days(low: str, high: str) -> int:
    """Whole days elapsed, read in Python since `JULIANDAY` rounds to the millisecond."""

    try:
        return max(0, (_instant(high) - _instant(low)).days)
    except ValueError:
        return 0


def _instant(text: str) -> datetime:
    parsed = datetime.fromisoformat(text.strip())

    return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else parsed


def _is_unsupported(sql_type: str) -> bool:
    del sql_type

    return False
