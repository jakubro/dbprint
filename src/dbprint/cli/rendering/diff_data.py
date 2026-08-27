"""Renderers for `dbprint diff` output.

`render_human_text` builds the plain-text five-section layout, wrapped in a Rich Panel by
`diff_tty.render_human` for TTY. `render_data` emits SPEC 2.6-shaped diff dicts as
json/yaml, always unfiltered (SPEC 2.6.9): threshold filtering is human output only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from io import StringIO
from typing import Any, TextIO

import yaml

from dbprint.engine.yaml_dumper import ArtifactDumper


_FOOTER = "Run `dbprint generate` to refresh — the new diff.yaml will capture this."

_STAT_PATH_TO_THRESHOLD_KEY = {
    "cardinality_ratio": "cardinality_ratio",
    "values_coverage": "values_coverage",
}

# SPEC 2.6.4's fixed counter order - matches `engine.diff._summarize`'s own dict order.
_SUMMARY_ORDER = (
    "tables_added",
    "tables_removed",
    "tables_modified",
    "columns_added",
    "columns_removed",
    "columns_type_changed",
    "columns_nullable_changed",
    "columns_default_changed",
    "statistics_drifted",
    "relationships_changed",
    "indexes_changed",
    "comments_changed",
    "unchanged_tables",
    "unevaluated_tables",
)


@dataclass(frozen=True)
class DiffRenderOptions:
    """Per-run threshold knobs for the human renderer."""

    thresholds: dict[str, float]
    threshold_override: float | None = None


def render_human_text(diff_dict: dict[str, Any], options: DiffRenderOptions) -> str:
    """Return the plain-text rendering of one connection's diff."""

    buf = StringIO()
    conn_name = diff_dict.get("connection") or "(unknown)"
    buf.write(f"Connection: {conn_name}\n")
    buf.write("Comparing committed prints against live DB...\n\n")
    buf.write(_baseline_line(diff_dict.get("baseline") or {}) + "\n")
    buf.write(_target_line(diff_dict.get("target") or {}) + "\n")
    buf.write(_summary_line(diff_dict.get("summary") or {}) + "\n\n")

    changes = diff_dict.get("changes") or []

    _emit_section(buf, "Modified (DDL)", _ddl_lines_by_table(changes))
    _emit_section(buf, "Modified (row count)", _row_count_lines_by_table(changes))
    _emit_section(buf, "Modified (grain)", _grain_lines_by_table(changes))
    _emit_section(buf, "Modified (physical layout)", _physical_layout_lines_by_table(changes))
    statistics_by_table, elided = _statistics_lines_by_table(changes, options)
    _emit_section(buf, "Modified (statistics)", statistics_by_table)

    if elided:
        plural = "" if elided == 1 else "s"
        buf.write(f"({elided} change{plural} elided below threshold)\n\n")

    _emit_section(buf, "Modified (relationships)", _relationship_lines_by_table(changes))
    _emit_section(buf, "Modified (indexes)", _index_lines_by_table(changes))
    _emit_simple_section(buf, "Added", _added_lines(changes))
    _emit_simple_section(buf, "Removed", _removed_lines(changes))

    buf.write(_FOOTER + "\n")

    return buf.getvalue()


def render_data(diff_dicts: list[dict[str, Any]], fmt: str, stream: TextIO) -> None:
    """Emit one or more diff dicts as `fmt` (json array or yaml multi-document)."""

    if fmt == "yaml":
        # A live diff carries adapter values, so this needs the print's representer set.
        yaml.dump_all(
            diff_dicts,
            stream,
            Dumper=ArtifactDumper,
            sort_keys=False,
            default_flow_style=False,
        )
    else:
        json.dump(diff_dicts, stream, indent=2, default=str, sort_keys=False)
        stream.write("\n")


# Headline helpers.


def _baseline_line(baseline: dict[str, Any]) -> str:
    """What the live database was compared against (SPEC 2.6.4)."""

    path = baseline.get("path") or "(unknown)"
    bits = [f"Baseline: {path}"]
    generated_at = baseline.get("generated_at")

    if generated_at is not None:
        bits.append(f"generated {generated_at}")

    version = baseline.get("dbprint_version")

    if version is not None:
        bits.append(f"dbprint {version}")

    return ", ".join(bits)


def _target_line(target: dict[str, Any]) -> str:
    """What was scanned and under which selectors (SPEC 2.6.4).

    `selectors` renders only when it actually narrows.
    """

    bits = ["Target: live database"]
    scanned_at = target.get("scanned_at")

    if scanned_at is not None:
        bits.append(f"scanned {scanned_at}")

    tables_scanned = target.get("tables_scanned")

    if tables_scanned is not None:
        plural = "" if tables_scanned == 1 else "s"
        bits.append(f"{tables_scanned} table{plural} scanned")

    selectors = target.get("selectors") or {}
    include, exclude = selectors.get("include") or [], selectors.get("exclude") or []

    if include or exclude:
        clause = "selectors"

        if include:
            clause += f" include={include}"

        if exclude:
            clause += f" exclude={exclude}"

        bits.append(clause)

    return ", ".join(bits)


def _summary_line(summary: dict[str, Any]) -> str:
    """Non-zero counters only, in the artifact's own fixed order (SPEC 2.6.4).

    Read from the artifact, never recomputed from the rendered sections - those are
    threshold-filtered, so the headline would agree with the body and disagree with the data.
    """

    parts = [f"{key}={summary[key]}" for key in _SUMMARY_ORDER if summary.get(key)]

    return "Summary: " + (", ".join(parts) if parts else "no changes")


# Section helpers.


def _emit_section(buf: StringIO, title: str, lines_by_table: dict[str, list[str]]) -> None:
    buf.write(f"{title}:\n")

    if not lines_by_table:
        buf.write("  (none)\n\n")

        return

    for fqn in sorted(lines_by_table):
        buf.write(f"  {fqn}\n")

        buf.writelines(f"    {line}\n" for line in lines_by_table[fqn])

    buf.write("\n")


def _emit_simple_section(buf: StringIO, title: str, lines: list[str]) -> None:
    buf.write(f"{title}:\n")

    if not lines:
        buf.write("  (none)\n\n")

        return

    buf.writelines(f"  {line}\n" for line in sorted(lines))

    buf.write("\n")


# Per-section content builders.


_DDL_KINDS = {
    "column_added",
    "column_removed",
    "column_type_changed",
    "column_nullable_changed",
    "column_default_changed",
    "comment_changed",
}


def _ddl_lines_by_table(changes: list[dict[str, Any]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}

    for ev in changes:
        kind = ev.get("kind")

        if kind not in _DDL_KINDS:
            continue

        fqn = ev.get("table") or ""
        out.setdefault(fqn, []).append(_format_ddl_event(ev))

    return out


def _format_ddl_event(ev: dict[str, Any]) -> str:
    kind = ev["kind"]
    column = ev.get("column", "")

    if kind == "column_added":
        sql_type = ev.get("sql_type", "")
        nullable = ev.get("nullable")
        suffix = "" if nullable is None else f" (nullable={nullable})"

        return f"+ {column}   {sql_type}{suffix}"
    elif kind == "column_removed":
        return f"- {column}"
    elif kind == "column_type_changed":
        return f"~ {column}: type {ev.get('before')!r} -> {ev.get('after')!r}"
    elif kind == "column_nullable_changed":
        return f"~ {column}: nullable {ev.get('before')} -> {ev.get('after')}"
    elif kind == "column_default_changed":
        return f"~ {column}: default {ev.get('before')!r} -> {ev.get('after')!r}"
    elif kind == "comment_changed":
        target = ev.get("target", "table")
        before = ev.get("before")
        after = ev.get("after")

        if target == "column":
            return f"~ comment on column {ev.get('column')}: {before!r} -> {after!r}"

        return f"~ comment on table: {before!r} -> {after!r}"
    else:
        return f"~ {kind}"


def _row_count_lines_by_table(changes: list[dict[str, Any]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}

    for ev in changes:
        if ev.get("kind") != "table_row_count_changed":
            continue

        fqn = ev.get("table") or ""
        out.setdefault(fqn, []).append(_format_row_count_event(ev))

    return out


def _format_row_count_event(ev: dict[str, Any]) -> str:
    before, after, delta = ev.get("before"), ev.get("after"), ev.get("delta")
    sign = "+" if isinstance(delta, (int, float)) and delta >= 0 else ""
    approximate = "approximate" in (ev.get("before_method"), ev.get("after_method"))
    suffix = " (approximate)" if approximate else ""

    return f"~ row_count: {before} -> {after} ({sign}{delta}){suffix}"


def _grain_lines_by_table(changes: list[dict[str, Any]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}

    for ev in changes:
        if ev.get("kind") != "grain_changed":
            continue

        fqn = ev.get("table") or ""
        out.setdefault(fqn, []).append(_format_grain_event(ev))

    return out


def _format_grain_event(ev: dict[str, Any]) -> str:
    before = _format_grain_side(ev.get("before") or {})
    after = _format_grain_side(ev.get("after") or {})

    return f"~ grain: {before} -> {after}"


def _format_grain_side(block: dict[str, Any]) -> str:
    keys = block.get("keys") or []
    parts = [f"({', '.join(k.get('columns') or [])}) {k.get('detection')}" for k in keys]
    search = block.get("search")
    exhausted = search.get("exhausted") if isinstance(search, dict) else None
    suffix = "" if exhausted is None else f" [search exhausted={exhausted}]"

    return (", ".join(parts) or "none") + suffix


def _physical_layout_lines_by_table(changes: list[dict[str, Any]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}

    for ev in changes:
        if ev.get("kind") != "physical_layout_changed":
            continue

        fqn = ev.get("table") or ""
        out.setdefault(fqn, []).append(_format_physical_layout_event(ev))

    return out


def _format_physical_layout_event(ev: dict[str, Any]) -> str:
    before = _format_physical_layout_side(ev.get("before"))
    after = _format_physical_layout_side(ev.get("after"))

    return f"~ physical_layout: {before} -> {after}"


def _format_physical_layout_side(block: dict[str, Any] | None) -> str:
    if block is None:
        return "none"

    mechanism = block.get("mechanism", "")
    exprs = ", ".join(k.get("expression", "") for k in block.get("keys") or [])

    return f"{mechanism} ({exprs})"


def _statistics_lines_by_table(
    changes: list[dict[str, Any]],
    options: DiffRenderOptions,
) -> tuple[dict[str, list[str]], int]:
    """Rendered lines per table, plus how many `statistic_changed` events fell below threshold.

    `--format json`/`yaml` stay unfiltered; the count is what was missing, so a table whose
    only change is sub-threshold does not silently vanish.
    """

    out: dict[str, list[str]] = {}
    elided = 0

    for ev in changes:
        if ev.get("kind") != "statistic_changed":
            continue

        if not _statistic_passes_threshold(ev, options):
            elided += 1

            continue

        fqn = ev.get("table") or ""
        out.setdefault(fqn, []).append(_format_statistic_event(ev))

    for values in out.values():
        values.sort()

    return out, elided


def _statistic_passes_threshold(ev: dict[str, Any], options: DiffRenderOptions) -> bool:
    delta_pct = ev.get("delta_pct")

    if delta_pct is None:
        return True

    threshold = _resolve_threshold(ev.get("stat", ""), options)

    return abs(delta_pct) >= threshold


def _resolve_threshold(stat_path: str, options: DiffRenderOptions) -> float:
    if options.threshold_override is not None:
        return options.threshold_override

    thresholds = options.thresholds

    if stat_path.startswith("percentiles."):
        return float(thresholds.get("percentile_pct", thresholds.get("default", 0.01)))

    key = _STAT_PATH_TO_THRESHOLD_KEY.get(stat_path)

    if key is not None:
        return float(thresholds.get(key, thresholds.get("default", 0.01)))

    return float(thresholds.get("default", 0.01))


def _format_statistic_event(ev: dict[str, Any]) -> str:
    column = ev.get("column", "")
    stat = ev.get("stat", "")
    before = ev.get("before")
    after = ev.get("after")
    delta_pct = ev.get("delta_pct")
    suffix = ""

    if isinstance(delta_pct, (int, float)):
        suffix = f" ({delta_pct * 100:+.1f}%)"

    return f"{column} {stat}: {before} -> {after}{suffix}"


_REL_KINDS = {"relationship_added", "relationship_removed", "relationship_modified"}


def _relationship_lines_by_table(changes: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Render each relationship event under both source and target tables.

    `->` marks the outgoing side and `<-` the incoming one, so a line identifies its end.
    """

    out: dict[str, list[str]] = {}

    for ev in changes:
        kind = ev.get("kind")

        if kind not in _REL_KINDS:
            continue

        source = ev.get("source_table") or ""
        target = ev.get("target_table") or ""
        source_col = _format_columns(ev.get("source_column"))
        target_col = _format_columns(ev.get("target_column"))
        marker = _rel_marker(kind)
        details = _rel_details(ev)

        out.setdefault(source, []).append(
            f"{marker} -> {target}.{target_col}  (via {source_col}){details}",
        )
        out.setdefault(target, []).append(
            f"{marker} <- {source}.{source_col}  (via {target_col}){details}",
        )

    for values in out.values():
        values.sort()

    return out


def _rel_marker(kind: str) -> str:
    return {
        "relationship_added": "+",
        "relationship_removed": "-",
        "relationship_modified": "~",
    }.get(kind, "~")


def _rel_details(ev: dict[str, Any]) -> str:
    if ev.get("kind") != "relationship_modified":
        return ""

    parts: list[str] = []
    on_delete = ev.get("on_delete")
    on_update = ev.get("on_update")

    if isinstance(on_delete, dict):
        parts.append(f"on_delete {on_delete.get('before')} -> {on_delete.get('after')}")

    if isinstance(on_update, dict):
        parts.append(f"on_update {on_update.get('before')} -> {on_update.get('after')}")

    return f"   {', '.join(parts)}" if parts else ""


def _format_columns(cols: Any) -> str:
    if not cols:
        return ""
    elif isinstance(cols, (list, tuple)):
        return ",".join(str(c) for c in cols)
    else:
        return str(cols)


_INDEX_KINDS = {"index_added", "index_removed", "index_modified"}


def _index_lines_by_table(changes: list[dict[str, Any]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}

    for ev in changes:
        kind = ev.get("kind")

        if kind not in _INDEX_KINDS:
            continue

        fqn = ev.get("table") or ""
        out.setdefault(fqn, []).append(_format_index_event(ev))

    for values in out.values():
        values.sort()

    return out


def _format_index_event(ev: dict[str, Any]) -> str:
    kind = ev["kind"]
    name = ev.get("index_name", "")

    if kind == "index_added":
        cols = _format_columns(ev.get("columns"))
        unique = ev.get("unique")
        idx_type = ev.get("type")

        return f"+ {name}: ({cols}) unique={unique} type={idx_type}"
    elif kind == "index_removed":
        return f"- {name}"
    elif kind == "index_modified":
        before = ev.get("before") or {}
        after = ev.get("after") or {}
        b_cols = _format_columns(before.get("columns"))
        a_cols = _format_columns(after.get("columns"))
        line = f"~ {name}: ({b_cols}) -> ({a_cols})"
        details = []

        if before.get("unique") != after.get("unique"):
            details.append(f"unique {before.get('unique')} -> {after.get('unique')}")

        if before.get("type") != after.get("type"):
            details.append(f"type {before.get('type')} -> {after.get('type')}")

        return f"{line}   {', '.join(details)}" if details else line
    else:
        return f"~ {name}"


def _added_lines(changes: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []

    for ev in changes:
        if ev.get("kind") != "table_added":
            continue

        fqn = ev.get("table") or ""
        out.append(f"{fqn}   ({ev.get('type', 'table')})")

    return out


def _removed_lines(changes: list[dict[str, Any]]) -> list[str]:
    return [ev.get("table") or "" for ev in changes if ev.get("kind") == "table_removed"]
