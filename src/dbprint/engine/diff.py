"""Diff computation per SPEC 2.6.

`compute()` covers every v1 change kind and fires events only for tables within
`target.selectors` scope (SPEC 2.6.8). `baseline` is the parsed manifest+per-table dict tree of
the prior on-disk state (None on the first-ever run); `current` is the same shape.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from dbprint.config.selectors import match as selector_match
from dbprint.spec.v1 import FORMAT_VERSION


# The kind reporting a column's own data moving, not the table's shape.
DATA_CHANGE_KIND = "statistic_changed"

ROW_COUNT_CHANGE_KIND = "table_row_count_changed"
GRAIN_CHANGE_KIND = "grain_changed"
PHYSICAL_LAYOUT_CHANGE_KIND = "physical_layout_changed"
DEPENDS_ON_CHANGE_KIND = "depends_on_changed"

# Kinds reporting data moving; every other kind means a committed print is no longer true.
# grain_changed, physical_layout_changed and depends_on_changed sit with the shape-moving kinds:
# each states what a constraint or a view's substrate declares, which churning data cannot move.
DATA_CHANGE_KINDS = frozenset({DATA_CHANGE_KIND, ROW_COUNT_CHANGE_KIND})


# Diff payload subkeys.


@dataclass(frozen=True)
class DiffSelectors:
    """Table filters for one diff run, already merged from config and CLI."""

    include: tuple[str, ...]
    exclude: tuple[str, ...]


@dataclass
class TableState:
    """Snapshot of one table - common shape for baseline and current sides.

    A field is None where the v1 print keeps it only in DDL (columns/indexes/comments), and a
    comparison skips any pair with a None side, so events need structured data from both.
    """

    fqn: str
    type: str
    columns: dict[str, ColumnState] | None = None
    relationships: list[FkState] | None = None
    indexes: dict[str, IndexState] | None = None
    table_comment: str | None = None
    column_comments: dict[str, str] | None = None
    statistics: dict[str, dict[str, Any]] | None = None
    table_comment_known: bool = False
    row_count: int | None = None
    row_count_method: str | None = None
    # This side's statistics.yaml carries a top-level `scope` block (SPEC 2.2.8): a
    # narrowed read, whose scan-scale absolute counts stop comparing.
    scoped: bool = False
    # This side's statistics.yaml carries `catalog_only` (SPEC 2.2.15): no query was
    # issued at all, so it has no measurement to compare against the other side's.
    catalog_only: bool = False
    grain: TableGrainState | None = None
    physical_layout: PhysicalLayoutState | None = None
    # The FQNs a view/matview reads (SPEC 2.2.17); None on a table or where the catalog could not
    # answer - absent on both sides reads as "nothing to compare", never as a removal.
    depends_on: tuple[str, ...] | None = None


@dataclass(frozen=True)
class GrainKeyState:
    """One column combination naming a table's grain (SPEC 2.2.12), either side."""

    columns: tuple[str, ...]
    detection: str


@dataclass(frozen=True)
class TableGrainState:
    """A table's `grain` block on either side of a diff.

    `search_exhausted` is None when the measured probe never ran, True/False once it did.
    """

    keys: tuple[GrainKeyState, ...]
    search_exhausted: bool | None = None


@dataclass(frozen=True)
class PhysicalLayoutKeyState:
    """One clustering/partitioning key component (SPEC 2.2.11), either side."""

    expression: str
    column: str | None = None


@dataclass(frozen=True)
class PhysicalLayoutState:
    """A table's `physical_layout` block on either side of a diff.

    `mechanism == ""` means "confirmed not clustered/partitioned", distinct from a None
    `TableState.physical_layout`, which means no data was contributed at all.
    """

    mechanism: str
    keys: tuple[PhysicalLayoutKeyState, ...] = ()


@dataclass(frozen=True)
class ColumnState:
    """One column's structural shape on either side of a diff."""

    name: str
    sql_type: str
    nullable: bool
    default: str | None
    # False when the default is unknown rather than absent, which None alone cannot say: a
    # baseline hydrated from statistics.yaml must not fire column_default_changed.
    default_known: bool = True


@dataclass(frozen=True)
class FkState:
    """One FK's shape on either side of a diff.

    (source_columns, target_table, target_columns) is the identity matched before/after;
    the rest is payload, diffed only once matched.
    """

    source_columns: tuple[str, ...]
    target_table: str
    target_columns: tuple[str, ...]
    # None on a baseline-hydrated edge that recorded neither (SPEC 2.3.8) - never defaulted to
    # a real action, which a live-extracted edge (this dataclass's other producer) always has.
    on_delete: str | None
    on_update: str | None
    detection: str = "declared"


@dataclass(frozen=True)
class IndexState:
    """One index's shape on either side of a diff."""

    name: str
    columns: tuple[str, ...]
    unique: bool
    type: str


# Top-level compute.


def compute(
    baseline: dict[str, TableState] | None,
    current: dict[str, TableState],
    *,
    connection_name: str,
    adapter_kind: str,
    baseline_path: str,
    baseline_generated_at: str | None,
    baseline_dbprint_version: str | None,
    scanned_at: str,
    selectors: DiffSelectors,
    generated_at: str,
    carried: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Return the diff dict ready for YAML serialization.

    `carried` names tables this run did not re-read: their current state is the
    baseline's own, so they compare equal and only the caller can name them (SPEC 2.6.4).
    """

    # Baseline scoped like current, so an out-of-scope table is not a removal (SPEC 2.6.8).
    include, exclude = list(selectors.include), list(selectors.exclude)
    baseline_map = {
        fqn: state
        for fqn, state in (baseline or {}).items()
        if selector_match(fqn, include, exclude)
    }
    changes: list[dict[str, Any]] = []

    added_fqns = sorted(set(current) - set(baseline_map))
    removed_fqns = sorted(set(baseline_map) - set(current))
    shared_fqns = sorted(set(current) & set(baseline_map))

    for fqn in added_fqns:
        changes.append({"kind": "table_added", "table": fqn, "type": current[fqn].type})

    for fqn in removed_fqns:
        changes.append({"kind": "table_removed", "table": fqn})

    modified_table_fqns: set[str] = set()
    unevaluated_table_fqns: set[str] = set()

    for fqn in shared_fqns:
        before, after = baseline_map[fqn], current[fqn]
        table_changes = _diff_table(fqn, before, after)

        if table_changes:
            modified_table_fqns.add(fqn)
            changes.extend(table_changes)
        elif (
            fqn in carried
            or not _statistics_comparable(before, after)
            or before.scoped
            or after.scoped
            or before.catalog_only
            or after.catalog_only
        ):
            # A scoped table that emitted nothing was never compared on absolute counts
            # (SPEC 2.2.8); a catalog_only one (SPEC 2.2.15) measured nothing. Both unevaluated.
            unevaluated_table_fqns.add(fqn)

    summary = _summarize(
        changes,
        unchanged_tables=len(shared_fqns) - len(modified_table_fqns) - len(unevaluated_table_fqns),
        unevaluated_tables=len(unevaluated_table_fqns),
    )

    return {
        "format_version": FORMAT_VERSION,
        "generated_at": generated_at,
        "connection": connection_name,
        "adapter": adapter_kind,
        "baseline": {
            "source": "committed_prints",
            "path": baseline_path,
            "generated_at": baseline_generated_at,
            # Whatever the committed print itself recorded, or None - never this process's own
            # version, which describes the run computing the diff, not the baseline being read.
            "dbprint_version": baseline_dbprint_version,
        },
        "target": {
            "source": "live_database",
            "scanned_at": scanned_at,
            "selectors": {
                "include": list(selectors.include),
                "exclude": list(selectors.exclude),
            },
            "tables_scanned": len(current),
        },
        "summary": summary,
        "changes": changes,
    }


def has_schema_changes(diff_dict: dict[str, Any]) -> bool:
    """True when the diff carries a change of shape, not only of data.

    The diff records both without distinction; this is the split an exit code answers, since
    shape moving means the print no longer describes the database it names.
    """

    return any(c.get("kind") not in DATA_CHANGE_KINDS for c in diff_dict.get("changes") or [])


# `sampled`/`matched` and the `looks_like_candidate` pair move whenever the sample is redrawn -
# drift about the measurement, not the column. `sketch` has no current side in a diff-only run.
# Membership here is a BLANKET PROJECTION, not a per-comparison skip: `comparable_columns` strips
# these off both sides, so a field the comparison must still READ belongs in `_MARKER_STATS`.
_UNCOMPARED_STATS = frozenset(
    {
        "freshness",
        "sql_type",
        "nullable",
        # The scanned-set marker itself (SPEC 2.2.8), echoed on every column of a scoped file -
        # it describes the read, not the data, and differs between any two sampled runs.
        "rows_scanned",
        "inferred.sampled",
        "inferred.matched",
        "inferred.looks_like_candidate",
        "inferred.looks_like_candidate_share",
        "sketch",
    },
)

# Present on one side only where the other never computed it - a `dbprint diff` run has no current
# side, so it is skipped rather than compared; a `generate` run compares it normally.
_PRESENCE_GATED_STATS = frozenset({"normalized_cardinality"})

# Kept by the projection and skipped per comparison: a marker, not a measurement (SPEC 2.2.4), but
# one `_diff_one_column_stats` must still read off both sides to know what not to compare.
_MARKER_STATS = frozenset({"unmeasured"})


def comparable_columns(columns: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Project a `statistics.yaml` columns map onto the fields drift is read from.

    Both sides pass through here, so what counts as a statistic is stated once: `freshness`
    drifts with the clock, and `sql_type`/`nullable` have their own change kinds.
    `cardinality`/`cardinality_ratio` drop per column in `_diff_one_column_stats`.
    """

    return {
        name: {key: value for key, value in payload.items() if key not in _UNCOMPARED_STATS}
        for name, payload in columns.items()
        if isinstance(payload, dict)
    }


def grain_from_block(data: Any) -> TableGrainState | None:
    """Parse a `grain` block (SPEC 2.2.12) into its diff-comparable state.

    None on anything but a well-formed block: a baseline predating the field is not
    an empty one, and the caller's guard must tell "nothing to compare" from "equal".
    """

    if not isinstance(data, dict):
        return None

    keys = data.get("keys")

    if not isinstance(keys, list):
        return None

    parsed_keys = tuple(
        GrainKeyState(columns=tuple(k["columns"]), detection=k["detection"])
        for k in keys
        if isinstance(k, dict)
        and isinstance(k.get("columns"), list)
        and isinstance(k.get("detection"), str)
    )
    search = data.get("search")
    exhausted = search.get("exhausted") if isinstance(search, dict) else None

    return TableGrainState(
        keys=parsed_keys,
        search_exhausted=exhausted if isinstance(exhausted, bool) else None,
    )


# The confirmed-unclustered sentinel: a real, comparable value, never Python None.
_NO_PHYSICAL_LAYOUT = PhysicalLayoutState(mechanism="", keys=())


def physical_layout_from_block(data: Any) -> PhysicalLayoutState:
    """Parse a `physical_layout` block (SPEC 2.2.11) into its diff-comparable state.

    Absence means "not clustered", never "not checked", so it parses to the same
    sentinel a genuinely unclustered table would rather than to None.
    """

    if not isinstance(data, dict):
        return _NO_PHYSICAL_LAYOUT

    mechanism = data.get("mechanism")
    keys = data.get("keys")

    if not isinstance(mechanism, str) or not mechanism or not isinstance(keys, list):
        return _NO_PHYSICAL_LAYOUT

    parsed_keys = tuple(
        PhysicalLayoutKeyState(expression=k["expression"], column=k.get("column"))
        for k in keys
        if isinstance(k, dict) and isinstance(k.get("expression"), str)
    )

    return PhysicalLayoutState(mechanism=mechanism, keys=parsed_keys)


def _statistics_comparable(before: TableState, after: TableState) -> bool:
    """Whether a statistics artifact was hydrated on both sides (SPEC 2.6.4).

    Every object type declares `statistics.yaml` in a conformant print (SPEC 2.2.15), so None
    means this run produced no artifact at all, not a difference in object type.
    """

    return before.statistics is not None and after.statistics is not None


def _diff_table(fqn: str, before: TableState, after: TableState) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    if before.columns is not None and after.columns is not None:
        out.extend(_diff_columns(fqn, before.columns, after.columns))

    if before.relationships is not None and after.relationships is not None:
        out.extend(_diff_relationships(fqn, before.relationships, after.relationships))

    if before.indexes is not None and after.indexes is not None:
        out.extend(_diff_indexes(fqn, before.indexes, after.indexes))

    if before.table_comment_known and after.table_comment_known:
        out.extend(_diff_comments(fqn, before, after))

    if before.row_count is not None and after.row_count is not None:
        row_count_change = _diff_row_count(
            fqn,
            before.row_count,
            after.row_count,
            before.row_count_method,
            after.row_count_method,
        )

        if row_count_change is not None:
            out.append(row_count_change)

    if before.grain is not None and after.grain is not None:
        grain_change = _diff_grain(fqn, before.grain, after.grain)

        if grain_change is not None:
            out.append(grain_change)

    if before.physical_layout is not None and after.physical_layout is not None:
        layout_change = _diff_physical_layout(fqn, before.physical_layout, after.physical_layout)

        if layout_change is not None:
            out.append(layout_change)

    if before.depends_on is not None and after.depends_on is not None:
        depends_on_change = _diff_depends_on(fqn, before.depends_on, after.depends_on)

        if depends_on_change is not None:
            out.append(depends_on_change)

    if (
        before.statistics is not None
        and after.statistics is not None
        and before.columns is not None
        and after.columns is not None
        and not before.catalog_only
        and not after.catalog_only
    ):
        out.extend(
            _diff_statistics(
                fqn,
                before.statistics,
                after.statistics,
                before.columns,
                after.columns,
                scoped=before.scoped or after.scoped,
            ),
        )

    return out


def _diff_columns(
    fqn: str,
    before: dict[str, ColumnState],
    after: dict[str, ColumnState],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    shared = sorted(set(before) & set(after))

    for name in added:
        c = after[name]
        out.append(
            {
                "kind": "column_added",
                "table": fqn,
                "column": name,
                "sql_type": c.sql_type,
                "nullable": c.nullable,
            },
        )

    for name in removed:
        out.append({"kind": "column_removed", "table": fqn, "column": name})

    for name in shared:
        b, a = before[name], after[name]

        if b.sql_type != a.sql_type:
            out.append(
                {
                    "kind": "column_type_changed",
                    "table": fqn,
                    "column": name,
                    "before": b.sql_type,
                    "after": a.sql_type,
                },
            )

        if b.nullable != a.nullable:
            out.append(
                {
                    "kind": "column_nullable_changed",
                    "table": fqn,
                    "column": name,
                    "before": b.nullable,
                    "after": a.nullable,
                },
            )

        if b.default_known and a.default_known and b.default != a.default:
            out.append(
                {
                    "kind": "column_default_changed",
                    "table": fqn,
                    "column": name,
                    "before": b.default,
                    "after": a.default,
                },
            )

    return out


def _diff_relationships(
    fqn: str,
    before: list[FkState],
    after: list[FkState],
) -> list[dict[str, Any]]:
    """SPEC 2.6.6: a `detection: measured` edge produces no event, on either side - `diff` never
    runs the sketch pass one depends on, so every such edge would report as removed.
    """

    out: list[dict[str, Any]] = []
    before = [fk for fk in before if fk.detection != "measured"]
    after = [fk for fk in after if fk.detection != "measured"]
    before_by_key = {_fk_key(fk): fk for fk in before}
    after_by_key = {_fk_key(fk): fk for fk in after}

    for key in sorted(set(after_by_key) - set(before_by_key)):
        fk = after_by_key[key]
        out.append(
            {
                "kind": "relationship_added",
                "source_table": fqn,
                "source_column": list(fk.source_columns),
                "target_table": fk.target_table,
                "target_column": list(fk.target_columns),
                "on_delete": fk.on_delete,
                "on_update": fk.on_update,
                "detection": fk.detection,
            },
        )

    for key in sorted(set(before_by_key) - set(after_by_key)):
        fk = before_by_key[key]
        out.append(
            {
                "kind": "relationship_removed",
                "source_table": fqn,
                "source_column": list(fk.source_columns),
                "target_table": fk.target_table,
                "target_column": list(fk.target_columns),
            },
        )

    for key in sorted(set(before_by_key) & set(after_by_key)):
        b, a = before_by_key[key], after_by_key[key]
        delta: dict[str, Any] = {}

        if b.on_delete != a.on_delete:
            delta["on_delete"] = {"before": b.on_delete, "after": a.on_delete}

        if b.on_update != a.on_update:
            delta["on_update"] = {"before": b.on_update, "after": a.on_update}

        if delta:
            event: dict[str, Any] = {
                "kind": "relationship_modified",
                "source_table": fqn,
                "source_column": list(a.source_columns),
                "target_table": a.target_table,
                "target_column": list(a.target_columns),
            }
            event.update(delta)
            out.append(event)

    return out


def _diff_indexes(
    fqn: str,
    before: dict[str, IndexState],
    after: dict[str, IndexState],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    shared = sorted(set(before) & set(after))

    for name in added:
        idx = after[name]
        out.append(
            {
                "kind": "index_added",
                "table": fqn,
                "index_name": name,
                "columns": list(idx.columns),
                "unique": idx.unique,
                "type": idx.type,
            },
        )

    for name in removed:
        out.append({"kind": "index_removed", "table": fqn, "index_name": name})

    for name in shared:
        b, a = before[name], after[name]

        if (b.columns, b.unique, b.type) != (a.columns, a.unique, a.type):
            out.append(
                {
                    "kind": "index_modified",
                    "table": fqn,
                    "index_name": name,
                    "before": {"columns": list(b.columns), "unique": b.unique, "type": b.type},
                    "after": {"columns": list(a.columns), "unique": a.unique, "type": a.type},
                },
            )

    return out


def _diff_comments(fqn: str, before: TableState, after: TableState) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    if before.table_comment != after.table_comment:
        out.append(
            {
                "kind": "comment_changed",
                "table": fqn,
                "target": "table",
                "before": before.table_comment,
                "after": after.table_comment,
            },
        )

    before_comments = before.column_comments or {}
    after_comments = after.column_comments or {}
    all_cols = sorted(set(before_comments) | set(after_comments))

    for col in all_cols:
        b = before_comments.get(col)
        a = after_comments.get(col)

        if b != a:
            out.append(
                {
                    "kind": "comment_changed",
                    "table": fqn,
                    "target": "column",
                    "column": col,
                    "before": b,
                    "after": a,
                },
            )

    return out


def _diff_row_count(
    fqn: str,
    before: int,
    after: int,
    before_method: str | None,
    after_method: str | None,
) -> dict[str, Any] | None:
    """A table-grain event - meaningful even under a sample-scale `scope` (SPEC 2.2.1)."""

    if before == after:
        return None

    return {
        "kind": ROW_COUNT_CHANGE_KIND,
        "table": fqn,
        "before": before,
        "after": after,
        "delta": after - before,
        "before_method": before_method,
        "after_method": after_method,
    }


def _diff_grain(
    fqn: str,
    before: TableGrainState,
    after: TableGrainState,
) -> dict[str, Any] | None:
    """A table-grain event carrying full before/after blocks, not a computed delta.

    A key whose `detection` changes (`declared` <-> `measured`) is neither added nor
    removed, and an add/remove delta would misreport it as a spurious pair.
    """

    if before == after:
        return None

    return {
        "kind": GRAIN_CHANGE_KIND,
        "table": fqn,
        "before": _grain_payload(before),
        "after": _grain_payload(after),
    }


def _grain_payload(state: TableGrainState) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "keys": [{"columns": list(k.columns), "detection": k.detection} for k in state.keys],
    }

    if state.search_exhausted is not None:
        payload["search"] = {"exhausted": state.search_exhausted}

    return payload


def _diff_physical_layout(
    fqn: str,
    before: PhysicalLayoutState,
    after: PhysicalLayoutState,
) -> dict[str, Any] | None:
    """A table-grain event; `null` on a side confirmed to carry no clustering key."""

    if before == after:
        return None

    return {
        "kind": PHYSICAL_LAYOUT_CHANGE_KIND,
        "table": fqn,
        "before": _physical_layout_payload(before),
        "after": _physical_layout_payload(after),
    }


def _physical_layout_payload(state: PhysicalLayoutState) -> dict[str, Any] | None:
    if not state.mechanism:
        return None

    return {
        "mechanism": state.mechanism,
        "keys": [
            (
                {"expression": k.expression, "column": k.column}
                if k.column is not None
                else {"expression": k.expression}
            )
            for k in state.keys
        ],
    }


def _diff_depends_on(
    fqn: str,
    before: tuple[str, ...],
    after: tuple[str, ...],
) -> dict[str, Any] | None:
    """A table-grain event: which objects a view/matview reads (SPEC 2.2.17) is a declared fact
    that moves only when someone edits the view, not when its rows churn.
    """

    if before == after:
        return None

    return {
        "kind": DEPENDS_ON_CHANGE_KIND,
        "table": fqn,
        "before": list(before),
        "after": list(after),
    }


def _diff_statistics(
    fqn: str,
    before_stats: dict[str, dict[str, Any]],
    after_stats: dict[str, dict[str, Any]],
    before_cols: dict[str, ColumnState],
    after_cols: dict[str, ColumnState],
    *,
    scoped: bool,
) -> list[dict[str, Any]]:
    """Per-column stat-by-stat comparison for columns present in both sides.

    `scoped` is table-grain - either side's population moved - not per-column.
    """

    out: list[dict[str, Any]] = []
    shared_cols = sorted(set(before_cols) & set(after_cols))

    for col in shared_cols:
        b = before_stats.get(col, {})
        a = after_stats.get(col, {})
        out.extend(_diff_one_column_stats(fqn, col, b, a, scoped=scoped))

    return out


# Dropped per column, not in the projection: only when a side counts approximately.
_APPROXIMATE_EXCLUDED_STATS = frozenset({"cardinality", "cardinality_ratio"})

# Scan-scale by construction: a side carrying `scope` suppresses them rather than reporting
# its population change as drift. Ratios are normalised to their own scan and keep comparing.
# The count fields scale one-for-one with `rows_scanned` as `null_count` does (SPEC 2.2.8);
# `mean` and `length.*` do not, and keep comparing under scope like `null_rate`.
_POPULATION_ABSOLUTE_STATS = frozenset(
    {
        "cardinality",
        "null_count",
        "values",
        "sum",
        "zero_count",
        "negative_count",
        "empty_count",
        "quantized_count",
        # A distinct-value count under folding (SPEC 2.2.4), so it scales with the scanned set
        # exactly as `cardinality` beside it does.
        "normalized_cardinality",
    },
)


def _unmeasured_of(col: dict[str, Any]) -> frozenset[str]:
    """The field names one side declares it could not measure (SPEC 2.2.4)."""

    value = col.get("unmeasured")

    if not isinstance(value, list):
        return frozenset()

    return frozenset(name for name in value if isinstance(name, str))


def _diff_one_column_stats(
    fqn: str,
    col: str,
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    scoped: bool,
) -> list[dict[str, Any]]:
    # A missing `cardinality_method` reads as exact, so a baseline predating the field keeps
    # comparing instead of silently stopping.
    approximate = "approximate" in (
        before.get("cardinality_method", "exact"),
        after.get("cardinality_method", "exact"),
    )
    out: list[dict[str, Any]] = []
    paths = _stat_paths(before) | _stat_paths(after)
    # SPEC 2.2.4: a field either side declares unmeasured has no reading to compare - and only the
    # artifact's own marker can tell a hydrated baseline's failed read from a value that moved.
    unmeasured = _unmeasured_of(before) | _unmeasured_of(after)

    for path in sorted(paths):
        if path in _UNCOMPARED_STATS or path in _MARKER_STATS:
            continue

        if path.split(".")[0] in unmeasured:
            continue

        if approximate and path in _APPROXIMATE_EXCLUDED_STATS:
            continue

        if scoped and path in _POPULATION_ABSOLUTE_STATS:
            continue

        if path in _PRESENCE_GATED_STATS and (path not in before or path not in after):
            continue

        b = _get_path(before, path)
        a = _get_path(after, path)

        if _same_reading(b, a):
            continue

        event: dict[str, Any] = {
            "kind": DATA_CHANGE_KIND,
            "table": fqn,
            "column": col,
            "stat": path,
            "before": b,
            "after": a,
        }

        if _is_numeric_stat(path) and isinstance(b, (int, float)) and isinstance(a, (int, float)):
            event["delta"] = a - b

            if b != 0:
                # Magnitude, not b itself, so a negative before cannot flip the sign of delta.
                event["delta_pct"] = (a - b) / abs(b)
        out.append(event)

    return out


def _same_reading(before: Any, after: Any) -> bool:
    """True when two readings of one stat say the same thing, NaN included.

    A NaN never equals itself, so an untreated NaN bound would report drift on every run.
    """

    if isinstance(before, float) and isinstance(after, float):
        return before == after or (math.isnan(before) and math.isnan(after))

    return before == after


def _stat_paths(stats: dict[str, Any]) -> set[str]:
    """Flatten one column's stats into dot-paths skipping nested maps/lists."""

    flat: set[str] = set()

    for k, v in stats.items():
        if isinstance(v, dict):
            for sub in v:
                flat.add(f"{k}.{sub}")
        else:
            flat.add(k)

    return flat


def _get_path(stats: dict[str, Any], path: str) -> Any:
    if "." in path:
        head, _, tail = path.partition(".")
        sub = stats.get(head)

        if isinstance(sub, dict):
            return sub.get(tail)

        return None

    return stats.get(path)


# `sql_type` and `freshness.*` are unreachable: `_UNCOMPARED_STATS` drops them before
# `_stat_paths` flattens a payload, so neither needs an entry.
_NON_NUMERIC_STATS = {
    "classification",
    "distribution",
    "values",
    "cardinality_method",
}


def _is_numeric_stat(path: str) -> bool:
    return path not in _NON_NUMERIC_STATS


def _fk_key(fk: FkState) -> tuple[tuple[str, ...], str, tuple[str, ...]]:
    return fk.source_columns, fk.target_table, fk.target_columns


def _summarize(
    changes: list[dict[str, Any]],
    *,
    unchanged_tables: int,
    unevaluated_tables: int,
) -> dict[str, int]:
    counts = {
        "tables_added": 0,
        "tables_removed": 0,
        "tables_modified": 0,
        "columns_added": 0,
        "columns_removed": 0,
        "columns_type_changed": 0,
        "columns_nullable_changed": 0,
        "columns_default_changed": 0,
        "statistics_drifted": 0,
        "relationships_changed": 0,
        "indexes_changed": 0,
        "comments_changed": 0,
        "unchanged_tables": unchanged_tables,
        "unevaluated_tables": unevaluated_tables,
    }

    modified_tables: set[str] = set()

    for c in changes:
        kind = c["kind"]

        if kind == "table_added":
            counts["tables_added"] += 1
        elif kind == "table_removed":
            counts["tables_removed"] += 1
        elif kind == "column_added":
            counts["columns_added"] += 1
            modified_tables.add(c["table"])
        elif kind == "column_removed":
            counts["columns_removed"] += 1
            modified_tables.add(c["table"])
        elif kind == "column_type_changed":
            counts["columns_type_changed"] += 1
            modified_tables.add(c["table"])
        elif kind == "column_nullable_changed":
            counts["columns_nullable_changed"] += 1
            modified_tables.add(c["table"])
        elif kind == "column_default_changed":
            counts["columns_default_changed"] += 1
            modified_tables.add(c["table"])
        elif kind == DATA_CHANGE_KIND:
            counts["statistics_drifted"] += 1
            modified_tables.add(c["table"])
        elif kind == ROW_COUNT_CHANGE_KIND:
            # No summary counter of its own (SPEC 2.6.4) - still counts the table as
            # modified, or a row-count-only change would read as unchanged.
            modified_tables.add(c["table"])
        elif kind in {GRAIN_CHANGE_KIND, PHYSICAL_LAYOUT_CHANGE_KIND, DEPENDS_ON_CHANGE_KIND}:
            # No summary counter of their own - still counts the table as modified.
            modified_tables.add(c["table"])
        elif kind in {"relationship_added", "relationship_removed", "relationship_modified"}:
            counts["relationships_changed"] += 1
            modified_tables.add(c.get("source_table", c.get("table", "")))
        elif kind in {"index_added", "index_removed", "index_modified"}:
            counts["indexes_changed"] += 1
            modified_tables.add(c["table"])
        elif kind == "comment_changed":
            counts["comments_changed"] += 1
            modified_tables.add(c["table"])

    counts["tables_modified"] = len(modified_tables)

    return counts
