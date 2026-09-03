"""Per-table context-fragment builder for `dbprint context`.

Reads the on-disk artifacts, assembles a per-table fragment in the requested format
(md/json/yaml), applies the token-budget algorithm, and joins fragments across tables.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import notes_synthesis
from .baseline import declared_artifacts, missing_artifacts, table_directory, walkable_tables
from .token_budget import Section, make_section, select, truncation_marker
from .yaml_dumper import spell_inline


HEADER_TOKEN_OVERHEAD = 8  # conservative reserve for the multi-table document header
NULL_PATTERN_DISPLAY_LIMIT = 8  # combinations rendered before the rest are summarised


@dataclass(frozen=True)
class AssemblyOptions:
    """Per-invocation flags from the `dbprint context` command."""

    format: str = "md"
    include_ddl: bool = True
    include_description: bool = True
    include_annotations: bool = True
    include_stats: bool = True
    include_relationships: bool = True
    budget: int | None = None  # total tokens; None = unbounded


@dataclass
class TableArtifacts:
    """Bundle of on-disk artifacts for one table, post-parse."""

    fqn: str
    table_type: str
    row_count: int | None
    column_count: int
    ddl: str
    statistics: dict[str, Any] | None
    relationships: dict[str, Any] | None
    description: str | None
    annotations: dict[str, dict[str, Any]] | None
    annotated_grain: dict[str, Any] | None
    relationship_annotations: list[dict[str, Any]] | None
    missing: tuple[str, ...]
    corrupted: dict[str, str]
    statistics_params_override: dict[str, Any] | None


@dataclass
class AssemblyResult:
    """Full rendered output of one assembly run."""

    text: str
    tables_included: int
    truncated: tuple[str, ...] = field(default_factory=tuple)


def assemble(
    manifest: dict[str, Any],
    print_root: Path,
    tables: list[str],
    options: AssemblyOptions,
    connection_name: str | None = None,
) -> AssemblyResult:
    """Assemble the requested table fragments; `tables` is the caller's resolved FQN order."""

    if not tables:
        return AssemblyResult(text="", tables_included=0)

    loaded = [_load_table_artifacts(manifest, print_root, fqn) for fqn in tables]

    if options.format == "json":
        return _assemble_json(loaded, options)
    elif options.format == "yaml":
        return _assemble_yaml(loaded, options)
    else:
        return _assemble_markdown(loaded, options, connection_name, print_root, manifest)


def _assemble_markdown(
    artifacts: list[TableArtifacts],
    options: AssemblyOptions,
    connection_name: str | None,
    print_root: Path,
    manifest: dict[str, Any],
) -> AssemblyResult:
    """Connection notes (SPEC 2.7.3) and provenance ride the document header, which only a
    multi-table render has; a single-table fragment states its own dialect instead (SPEC 2.5).
    """

    multi = len(artifacts) > 1
    header = ""
    header_tokens = 0

    if multi and connection_name:
        header = f"# Context for connection {connection_name} ({len(artifacts)} tables)"
        notes, notes_reason = _load_connection_notes(print_root)

        if notes:
            header += "\n\n" + notes

        if notes_reason is not None:
            header += "\n\n" + _corrupted_summary({"manifest_annotations": notes_reason})

        provenance = _provenance_block(manifest, connection_name)

        if provenance:
            header += "\n\n" + provenance

        header_tokens = max(HEADER_TOKEN_OVERHEAD, len(header) // 4)

    per_table_budget: int | None = None

    if options.budget is not None:
        remaining = max(0, options.budget - header_tokens)
        per_table_budget = remaining // len(artifacts)

        if per_table_budget == 0:
            # Budget cannot cover any table; emit the header (when present) only.
            text = header + "\n" if header else ""

            return AssemblyResult(
                text=text,
                tables_included=0,
                truncated=tuple(a.fqn for a in artifacts),
            )

    fragments: list[str] = []
    truncated: list[str] = []
    included = 0

    connection_statistics_params = manifest.get("statistics_params") or {}
    adapter = manifest.get("adapter")
    adapter = adapter if isinstance(adapter, str) and adapter else None

    for a in artifacts:
        fragment, was_truncated, has_content = _render_table_markdown(
            a,
            options,
            per_table_budget,
            connection_statistics_params,
            adapter,
        )

        if fragment:
            fragments.append(fragment)

        # A budget too tight for even the header leaves `fragment` as the bare truncation
        # marker - real text, but not a table this run actually included.
        if has_content:
            included += 1

            if was_truncated:
                truncated.append(a.fqn)
        else:
            truncated.append(a.fqn)

    body = "\n\n---\n\n".join(fragments)
    text = (header + "\n\n" if header else "") + body

    return AssemblyResult(
        text=text,
        tables_included=included,
        truncated=tuple(truncated),
    )


def _render_table_markdown(
    a: TableArtifacts,
    options: AssemblyOptions,
    budget: int | None,
    connection_statistics_params: dict[str, Any],
    adapter: str | None,
) -> tuple[str, bool, bool]:
    """Render one table; return (markdown text, was_truncated, has_content). `has_content` is
    false when the budget missed even the header - `text` is then the bare truncation marker.
    """

    include_qualifiers = options.include_stats and bool(a.statistics)
    sections: list[Section] = []
    sections.append(
        make_section(
            "header",
            _markdown_header(a, include_qualifiers, connection_statistics_params, adapter),
        ),
    )

    if options.include_ddl:
        sections.append(make_section("ddl", _markdown_ddl(a)))

    if options.include_description and a.description:
        sections.append(make_section("description", _markdown_description(a)))

    if options.include_annotations and a.annotations and _has_rendered_annotations(a.annotations):
        sections.append(make_section("annotations", _markdown_annotations(a)))

    if options.include_stats and a.statistics:
        if a.statistics.get("catalog_only") is True:
            # SPEC 2.2.15: nothing was queried, so there is no cardinality to table - list
            # the columns a catalog read already named, not a table of fabricated cells.
            sections.append(make_section("columns", _markdown_catalog_only_columns(a)))
        else:
            if a.statistics.get("physical_layout"):
                sections.append(make_section("physical_layout", _markdown_physical_layout(a)))

            effective_params = {
                **connection_statistics_params,
                **(a.statistics_params_override or {}),
            }
            sections.append(
                make_section("cardinality", _markdown_cardinality_table(a, effective_params)),
            )

            if a.statistics.get("null_patterns"):
                sections.append(make_section("null_patterns", _markdown_null_patterns(a)))

    if options.include_relationships and a.relationships:
        sections.append(make_section("relationships", _markdown_relationships(a)))

    selection = select(sections, budget)
    text = "\n\n".join(s.text for s in selection.included)
    marker = truncation_marker(selection)

    # A budget too tight for even the header omits every section, so the marker is the whole
    # return, never blank - a caller must see why, not a silent empty success.
    if marker:
        text = f"{text}\n\n{marker}" if text else marker

    return text, selection.truncated, bool(selection.included)


def _markdown_header(
    a: TableArtifacts,
    include_qualifiers: bool,
    connection_statistics_params: dict[str, Any],
    adapter: str | None,
) -> str:
    """The identity line, then one line per table-level qualifier that applies.

    Missing-artifact and adapter lines are ungated by `include_qualifiers` - every fragment
    states its own SQL dialect (SPEC 2.5).
    """

    parts = []

    if a.row_count is not None:
        parts.append(f"{a.row_count:,} rows")
    parts.append(f"{a.column_count} columns")

    lines = [f"# Table: {a.fqn}  ({', '.join(parts)})"]

    if adapter:
        lines.append(f"Adapter: {adapter}")

    if a.missing:
        lines.append(_missing_summary(a.missing))

    if a.corrupted:
        lines.append(_corrupted_summary(a.corrupted))

    if not include_qualifiers:
        return "\n".join(lines)

    statistics = a.statistics or {}
    qualifiers = (
        _scope_summary(statistics),
        _grain_summary(statistics, a.annotated_grain),
        _timeline_summary(statistics),
        _depends_on_summary(statistics),
        _statistics_params_override_summary(
            connection_statistics_params,
            a.statistics_params_override,
        ),
    )
    lines.extend(line for line in qualifiers if line)

    return "\n".join(lines)


def _statistics_params_override_summary(
    connection_defaults: dict[str, Any],
    table_override: dict[str, Any] | None,
) -> str:
    """Stated so a table-level override is never applied silently."""

    if not table_override:
        return ""

    differing = {
        key: value for key, value in table_override.items() if value != connection_defaults.get(key)
    }

    if not differing:
        return ""

    rendered = ", ".join(f"{key}={value}" for key, value in sorted(differing.items()))

    return f"Statistics params override: {rendered}"


def _missing_summary(missing: tuple[str, ...]) -> str:
    """One line naming every declared kind whose file is absent from disk (SPEC 2.5)."""

    return f"Missing: {', '.join(missing)} (declared but missing from disk)"


def _corrupted_summary(corrupted: dict[str, str]) -> str:
    """One line naming every declared kind present on disk but unreadable (SPEC 2.5).

    `corrupted` maps kind to why: the structured payload carries the reason, this prose line
    only the kinds.
    """

    return f"Unreadable: {', '.join(corrupted)} (present on disk, failed to parse)"


def _scope_summary(statistics: dict[str, Any]) -> str:
    """Which rows the statistics were computed over, when the read was narrowed (SPEC 2.2.8).

    Absence of the block asserts the whole table was read, so it renders nothing. The share
    is taken against `row_count`, since `sample` records what was asked for, not what came.
    """

    block = statistics.get("scope")

    if not isinstance(block, dict):
        return ""

    rows_scanned = block.get("rows_scanned")

    if not isinstance(rows_scanned, int):
        return ""

    row_count = statistics.get("row_count")
    scanned = f"{rows_scanned:,}"

    if isinstance(row_count, int) and row_count > 0:
        share = round(100 * rows_scanned / row_count, 1)
        scanned = f"{scanned} of {row_count:,} rows ({share}%)"
    else:
        scanned = f"{scanned} rows"

    return f"Scanned: {scanned}{_narrowing_suffix(block)}"


def _narrowing_suffix(scope: dict[str, Any]) -> str:
    """How the read was narrowed, from the one of `sample`/`filter` present (SPEC 2.2.8)."""

    sample = scope.get("sample")

    if isinstance(sample, (int, float)) and not isinstance(sample, bool):
        return f", sample {_significant_digits(sample, 4)}"

    row_filter = scope.get("filter")

    if isinstance(row_filter, str) and row_filter.strip():
        return f", filter `{row_filter}`"

    return ""


def _significant_digits(value: float, digits: int) -> str:
    """Trailing zeros and the trailing point are stripped."""

    if value == 0:
        return "0"

    exponent = math.floor(math.log10(abs(value)))
    decimals = max(digits - 1 - exponent, 0)
    text = f"{value:.{decimals}f}"

    return text.rstrip("0").rstrip(".") if "." in text else text


def _grain_summary(statistics: dict[str, Any], annotated_grain: dict[str, Any] | None) -> str:
    """What identifies a row, one line - a table-level fact, not a per-column cell (SPEC 2.2.12).

    A human-authored key (SPEC 2.7.1) rides the same list tagged `annotated`; it adds a fact,
    never replaces the producer's measurement.
    """

    block = statistics.get("grain")
    keys = [k for k in (block.get("keys") or []) if isinstance(k, dict)] if block else []
    keys = keys + _annotated_grain_keys(annotated_grain)

    if not keys:
        search = block.get("search") if block else None
        exhausted = search.get("exhausted") if isinstance(search, dict) else None

        if exhausted is True:
            return "Grain: searched, none found"

        if exhausted is False:
            return "Grain: search bounded, none found within the cap"

        return "Grain: not determined"

    rendered = "; ".join(
        f"({', '.join(key.get('columns') or [])}) {key.get('detection')}"
        + (f' - "{key["note"]}"' if key.get("note") else "")
        for key in keys
    )

    return f"Grain: {rendered}"


def _timeline_summary(statistics: dict[str, Any]) -> str:
    """The anchor column's bucketed activity, one line (SPEC 2.2.16) - anchor, unit, bucket
    count and span, enough to judge recency and gaps without the full column list.
    """

    block = statistics.get("timeline")

    if not block:
        return ""

    column, unit = block.get("column"), block.get("unit")
    buckets = [b for b in (block.get("buckets") or []) if isinstance(b, dict)]

    if not buckets:
        return f"Timeline: {column} ({unit}), no non-null values bucketed"

    span = f"{buckets[0].get('start')} to {buckets[-1].get('start')}"
    coverage = block.get("coverage")
    is_real_number = isinstance(coverage, (int, float)) and not isinstance(coverage, bool)
    covered = f", {_coverage_share_words(coverage)}" if is_real_number else ""

    return f"Timeline: {column} ({unit}), {len(buckets)} bucket(s), {span}{covered}"


def _coverage_share_words(coverage: float) -> str:
    """A `<1.0` coverage never rounds up to a false "every scanned row" (SPEC 2.2.16) - a null
    anchor counts toward `rows_scanned` but no bucket, so `1.0` alone means every value landed.

    The percentage floors rather than rounds: `0.9995` must not print as the `100.0%` those
    words are withheld for saying.
    """

    if coverage >= 1:
        return "every scanned row"

    return f"{math.floor(coverage * 1000) / 10}% of scanned rows"


def _depends_on_summary(statistics: dict[str, Any]) -> str:
    """What a view/matview reads, one line (SPEC 2.2.17) - absent when the producer could not
    ask, so this renders nothing rather than guess; a plain table never carries the field.
    """

    block = statistics.get("depends_on")

    if not isinstance(block, list):
        return ""

    names = [t for t in block if isinstance(t, str)]

    if not names:
        return "Depends on: nothing else in this print"

    return f"Depends on: {', '.join(names)}"


def _annotated_grain_keys(annotated_grain: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Human-authored grain keys as `{columns, detection, note}`, `note` omitted when absent."""

    if not annotated_grain:
        return []

    keys = annotated_grain.get("keys")

    if not isinstance(keys, list):
        return []

    result = []

    for key in keys:
        if not isinstance(key, dict):
            continue

        entry = {"columns": key.get("columns") or [], "detection": "annotated"}
        note = key.get("note")

        if isinstance(note, str) and note.strip():
            entry["note"] = note

        result.append(entry)

    return result


def _provenance_block(manifest: dict[str, Any], connection_name: str) -> str:
    """The manifest-level parameters that decided what was measured (SPEC 2.5).

    A field the manifest never populated renders nothing.
    """

    lines: list[str] = []
    adapter = manifest.get("adapter")

    if isinstance(adapter, str) and adapter:
        lines.append(f"- Adapter: {adapter}")

    dbprint_version = manifest.get("dbprint_version")

    if isinstance(dbprint_version, str) and dbprint_version:
        lines.append(f"- dbprint version: {dbprint_version}")

    generated_at = manifest.get("generated_at")

    if isinstance(generated_at, str) and generated_at:
        lines.append(f"- Generated: {generated_at}")

    manifest_connection = manifest.get("connection")

    if (
        isinstance(manifest_connection, str)
        and manifest_connection
        and manifest_connection != connection_name
    ):
        lines.append(
            f"- Connection name mismatch: manifest declares {manifest_connection!r}, "
            f"resolved as {connection_name!r}",
        )

    collation = manifest.get("default_collation")

    if isinstance(collation, str) and collation:
        lines.append(f"- Default collation: {collation}")

    selectors = manifest.get("selectors") or {}
    include = [s for s in (selectors.get("include") or []) if isinstance(s, str)]
    exclude = [s for s in (selectors.get("exclude") or []) if isinstance(s, str)]

    if include or exclude:
        parts = []

        if include:
            parts.append(f"include {', '.join(include)}")

        if exclude:
            parts.append(f"exclude {', '.join(exclude)}")

        lines.append(f"- Selectors narrow this print: {'; '.join(parts)}")

    redaction_count = manifest.get("redaction_rules_configured")

    if (
        isinstance(redaction_count, int)
        and not isinstance(redaction_count, bool)
        and redaction_count
    ):
        rule_word = "rule" if redaction_count == 1 else "rules"
        lines.append(f"- Redaction configured: {redaction_count} {rule_word}")

    percentiles = (manifest.get("statistics_params") or {}).get("percentiles")

    if isinstance(percentiles, list) and percentiles:
        rendered = ", ".join(f"p{p}" for p in percentiles)
        lines.append(f"- Percentiles configured: {rendered}")

    return "## Provenance\n\n" + "\n".join(lines) if lines else ""


def _markdown_ddl(a: TableArtifacts) -> str:
    return "## DDL\n\n```sql\n" + a.ddl.rstrip() + "\n```"


def _markdown_description(a: TableArtifacts) -> str:
    assert a.description is not None

    return "## Description\n\n" + a.description.rstrip()


def _markdown_annotations(a: TableArtifacts) -> str:
    assert a.annotations is not None
    lines = ["## Annotations"]

    for name, entry in a.annotations.items():
        if not _annotation_entry_has_content(entry):
            continue

        note = entry.get("note")

        # A colon promises text that is not coming - a note-less header names the column and stops.
        if isinstance(note, str) and note.strip():
            lines.append(f"- **{name}**: {note.strip()}")
        else:
            lines.append(f"- **{name}**")

        claims = entry.get("claims")

        if isinstance(claims, dict) and claims:
            # The assertion grammar's own YAML (ASSERTIONS.md 2.1), not Python's repr.
            rendered = ", ".join(
                f"{stat}: {spell_inline(predicate)}" for stat, predicate in claims.items()
            )
            lines.append(f"  - claims: {rendered}")

        values = entry.get("values")

        if isinstance(values, list):
            for value_entry in values:
                if not isinstance(value_entry, dict):
                    continue

                value_note = value_entry.get("note")

                if isinstance(value_note, str) and value_note.strip():
                    value = value_entry.get("value")
                    spelled = "NULL" if value is None else spell_inline(value)
                    lines.append(f"  - {spelled}: {value_note.strip()}")

    return "\n".join(lines)


def _annotation_entry_has_content(entry: dict[str, Any]) -> bool:
    """Whether an entry (SPEC 2.7.1) renders anything at all - a note, a claim, or a value note.

    Mirrors `_markdown_annotations` exactly, so the gate and the body it wraps cannot disagree.
    """

    note = entry.get("note")

    if isinstance(note, str) and note.strip():
        return True

    claims = entry.get("claims")

    if isinstance(claims, dict) and claims:
        return True

    values = entry.get("values")

    if isinstance(values, list):
        for value_entry in values:
            if isinstance(value_entry, dict):
                value_note = value_entry.get("note")

                if isinstance(value_note, str) and value_note.strip():
                    return True

    return False


def _has_rendered_annotations(annotations: dict[str, dict[str, Any]]) -> bool:
    """Gates the whole `## Annotations` section."""

    return any(_annotation_entry_has_content(entry) for entry in annotations.values())


def _markdown_catalog_only_columns(a: TableArtifacts) -> str:
    """The column list for an object nothing was queried for (SPEC 2.2.15).

    No cardinality cell to fill and no Notes column to synthesize - `sql_type` and
    `classification` are the whole of what a catalog read supplies.
    """

    assert a.statistics is not None
    columns = a.statistics.get("columns") or {}

    lines = [
        "## Columns (not queried)",
        "",
        "| Column | Type | Classification |",
        "|---|---|---|",
    ]

    for name in _ordered_column_names(columns):
        col = columns[name]
        lines.append(f"| {name} | {col.get('sql_type', '?')} | {col.get('classification', '?')} |")

    return "\n".join(lines)


def _markdown_cardinality_table(a: TableArtifacts, statistics_params: dict[str, Any]) -> str:
    assert a.statistics is not None
    columns = a.statistics.get("columns") or {}
    row_count = a.statistics.get("row_count")
    fk_targets = _build_fk_target_map(a.relationships or {})

    lines = [
        "## Cardinality & key columns",
        "",
        "| Column | Cardinality | Notes |",
        "|---|---|---|",
    ]

    ordered = _ordered_column_names(columns)

    for name in ordered:
        col = columns[name]
        cardinality = _format_cardinality_cell(col, row_count)
        notes = notes_synthesis.synthesize(
            col,
            fk_targets.get(name),
            statistics_params=statistics_params,
        )
        lines.append(f"| {name} | {cardinality} | {notes} |")

    return "\n".join(lines)


def _markdown_physical_layout(a: TableArtifacts) -> str:
    """The declared clustering/partitioning key - a schema fact, never a claim about pruning."""

    assert a.statistics is not None
    block = a.statistics.get("physical_layout") or {}
    keys = [k for k in (block.get("keys") or []) if isinstance(k, dict)]
    labels = {"cluster": "Clustered by", "partition": "Partitioned by", "sort": "Sorted by"}
    label = labels.get(block.get("mechanism"), "Partitioned by")
    expressions = ", ".join(k.get("expression", "") for k in keys)

    return f"## Physical layout\n\n{label}: {expressions}"


def _markdown_null_patterns(a: TableArtifacts) -> str:
    """Which columns are null on the same rows, as an ordered table.

    Worded as an observation throughout: SPEC 2.2.10 makes a pattern a measurement over
    the rows read, not a constraint a reader can write a query against.
    """

    assert a.statistics is not None
    block = a.statistics.get("null_patterns") or {}
    patterns = [p for p in (block.get("patterns") or []) if isinstance(p, dict)]
    lines = [
        "## Columns null on the same rows",
        "",
        "| Rows | Null together |",
        "|---|---|",
    ]

    for entry in patterns[:NULL_PATTERN_DISPLAY_LIMIT]:
        names = ", ".join(entry.get("columns") or []) or "(none - fully populated)"
        lines.append(f"| {int(entry.get('count') or 0):,} | {names} |")

    remainder = len(patterns) - NULL_PATTERN_DISPLAY_LIMIT

    if remainder > 0:
        lines.append(f"| ... | {remainder} further combinations |")

    coverage = block.get("coverage")

    if isinstance(coverage, (int, float)) and not isinstance(coverage, bool):
        share = _coverage_share_words(coverage)
        # Silent on `measured` - matches the per-column coverage hedge (notes_synthesis.py).
        hedge = " (bounded)" if block.get("coverage_method") == "bounded" else ""
        lines.append("")
        lines.append(f"Observed over {share}{hedge}.")

    return "\n".join(lines)


def _markdown_relationships(a: TableArtifacts) -> str:
    assert a.relationships is not None
    lines = ["## Relationships"]
    refers_to = a.relationships.get("refers_to") or []
    referenced_by = a.relationships.get("referenced_by") or []
    rejected = _rejected_edges(a.relationship_annotations)

    if not refers_to and not referenced_by:
        # `eligible_target: false` (SPEC 2.3.8) says nothing COULD reference this object, not
        # merely that nothing does - a bare "(none)" collapses that into the weaker claim.
        if a.relationships.get("eligible_target") is False:
            lines.append("- (none - not a join target, no declared-unique column)")
        else:
            lines.append("- (none)")

        return "\n".join(lines)

    for entry in refers_to:
        cols = ", ".join(entry.get("column", []))
        tgt_table = entry.get("target_table", "?")
        tgt_cols = ", ".join(entry.get("target_column", []))
        detection = _edge_detection(entry)
        # Absent on an inferred edge (SPEC 2.3.8) - never invent a referential action.
        on_delete = entry.get("on_delete")
        suffix = f", on_delete={on_delete}" if on_delete is not None else ""
        lines.append(f"- -> {tgt_table}.{tgt_cols} (via {cols}, {detection}{suffix})")
        lines.extend(_rejection_line(rejected.get(_edge_key(entry))))
        lines.extend(_observed_lines(entry))

    for entry in referenced_by:
        ref_table = entry.get("referencer_table", "?")
        ref_cols = ", ".join(entry.get("referencer_column", []))
        detection = _edge_detection(entry)
        on_delete = entry.get("on_delete")
        suffix = f", on_delete={on_delete}" if on_delete is not None else ""
        lines.append(f"- <- {ref_table}.{ref_cols} ({detection}{suffix})")
        lines.extend(_observed_lines(entry))

    return "\n".join(lines)


def _observed_lines(entry: dict[str, Any]) -> list[str]:
    """SPEC 2.3.10: what joining across this edge costs, beside its declared shape.

    An absent block renders nothing; `scope_compatible: false` is itself a measurement and
    gets its own line rather than the same silence.
    """

    observed = entry.get("observed")

    if not isinstance(observed, dict):
        return []

    if observed.get("scope_compatible") is False:
        return ["  observed: scopes not comparable"]

    fanout_avg = observed.get("fanout_avg")
    target_coverage = observed.get("target_coverage")

    if fanout_avg is None or target_coverage is None:
        return []

    text = f"  observed: fanout avg {fanout_avg:,.1f}"
    fanout_max = observed.get("fanout_max")

    if fanout_max is not None:
        text += f" (max {fanout_max:,})"

    text += f", covers {target_coverage:.1%} of target"
    containment = observed.get("containment")

    if containment is not None:
        text += f", {containment:.1%} of the referencing values are contained"
        answerable = observed.get("answerable_count")

        # SPEC 2.3.10: a containment ratio needs the margin its denominator implies - the same
        # evidence-before-verdict idiom as `looks_like`'s sampled/matched pair.
        if isinstance(answerable, int) and not isinstance(answerable, bool):
            text += f" ({answerable:,} answerable)"

    lines = [text]

    if observed.get("coherent") is False:
        lines.append("  **[INCOHERENT: referencing cardinality exceeds the target's]**")

    return lines


def _rejected_edges(
    relationship_annotations: list[dict[str, Any]] | None,
) -> dict[tuple[Any, ...], dict[str, Any]]:
    """Rejected refers_to entries from relationships.annotations.yaml, keyed by address.

    Keyed by the same triplet the base artifact addresses an edge by (SPEC 2.7.2), so the
    renderer can pull the note beside the verdict.
    """

    if not relationship_annotations:
        return {}

    return {
        _edge_key(entry): entry
        for entry in relationship_annotations
        if entry.get("verdict") == "rejected"
    }


def _edge_key(entry: dict[str, Any]) -> tuple[Any, ...]:
    """The (column, target_table, target_column) triplet an edge is addressed by."""

    return (
        tuple(entry.get("column") or ()),
        entry.get("target_table"),
        tuple(entry.get("target_column") or ()),
    )


def _rejection_line(entry: dict[str, Any] | None) -> list[str]:
    """A one-line marker when a human rejected this edge, else nothing.

    The graph itself is unchanged (SPEC 2.7.2); this only reports the overrule.
    """

    if entry is None:
        return []

    note = entry.get("note")
    suffix = f": {note}" if isinstance(note, str) and note.strip() else ""

    return [f"  **[REJECTED by human annotation{suffix}]**"]


def _edge_detection(entry: dict[str, Any]) -> str:
    """The weaker reading of an absent `detection`, which SPEC 2.3.2 marks REQUIRED and gives no
    default: `inferred` never overstates the edge (SPEC 2.3 forbids reading a guess as declared).
    """

    return entry.get("detection") or "inferred"


def _format_cardinality_cell(col: dict[str, Any], row_count: int | None) -> str:
    """The distinct count, whether it saturates the set it was measured over, and how counted.

    A scoped column counts distinct over `rows_scanned` (SPEC 2.2.8), so the cue names which
    population it compared. Neither fires at zero: a read that matched no rows saturates
    nothing. `cardinality_method: approximate` marks an estimate; exact is unmarked.
    """

    cardinality = col.get("cardinality")

    if cardinality is None:
        return "n/a"

    text = f"{cardinality:,}"
    rows_scanned = col.get("rows_scanned")

    if isinstance(rows_scanned, int):
        text += " (= scanned rows)" if rows_scanned and cardinality == rows_scanned else ""
    elif row_count and cardinality == row_count:
        text += " (= row count)"

    if col.get("cardinality_method") == "approximate":
        text += " (approx)"

    normalized = col.get("normalized_cardinality")

    if isinstance(normalized, int) and normalized < cardinality:
        text += f" ({cardinality - normalized} merge case/whitespace-folded)"

    return text


_COLUMN_ORDER_PRIORITY = {
    "foreign_key_candidate": 0,
    "categorical": 1,
    "temporal": 2,
    "numeric": 3,
    "boolean": 4,
    "text": 5,
    "json": 6,
    "unsupported": 7,
}


def _ordered_column_names(columns: dict[str, Any]) -> list[str]:
    """Stable ordering for the cardinality table: `_COLUMN_ORDER_PRIORITY`, then YAML order."""

    def key(name_col: tuple[str, dict[str, Any]]) -> tuple[int, int]:
        name, col = name_col
        classification = col.get("classification", "unsupported")
        priority = _COLUMN_ORDER_PRIORITY.get(classification, 8)

        return priority, list(columns.keys()).index(name)

    return [n for n, _ in sorted(columns.items(), key=key)]


def _build_fk_target_map(relationships: dict[str, Any]) -> dict[str, str]:
    """Map source column -> '<target>.<column> (<detection>)' for every `refers_to` entry.

    The Notes cell renders this verbatim, so the detection qualifier is baked in here
    (SPEC 2.3: a consumer MUST NOT treat an inferred edge as a constraint).
    """

    out: dict[str, str] = {}

    for entry in relationships.get("refers_to") or []:
        cols = entry.get("column") or []
        tgt_cols = entry.get("target_column") or []
        tgt_table = entry.get("target_table") or ""
        detection = _edge_detection(entry)

        if len(cols) == 1 and len(tgt_cols) == 1:
            out[cols[0]] = f"{tgt_table}.{tgt_cols[0]} ({detection})"
        elif cols:
            joined_src = ",".join(cols)
            joined_tgt = ",".join(tgt_cols) if tgt_cols else "?"
            out[joined_src] = f"{tgt_table}.({joined_tgt}) ({detection})"

    return out


def _load_artifact(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """`(None, None)` covers both "never declared" and "declared but missing"; a non-`None` reason
    is a declared file that exists and failed to parse, naming why.
    """

    if not path.is_file():
        return None, None

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return None, str(exc)

    if isinstance(data, dict):
        return data, None

    return None, "parses, but is not a mapping"


def _load_connection_notes(print_root: Path) -> tuple[str | None, str | None]:
    """`manifest.annotations.yaml`'s `notes` field (SPEC 2.7.3) and why it is corrupt, if it is -
    `(None, None)` covers absent and empty alike; a non-`None` reason is present but unreadable.
    """

    mapping, reason = _load_artifact(print_root / "manifest.annotations.yaml")

    if mapping is None:
        return None, reason

    notes = mapping.get("notes")

    return (notes.strip() if isinstance(notes, str) and notes.strip() else None), None


def _annotation_columns(mapping: dict[str, Any] | None) -> dict[str, dict[str, Any]] | None:
    """The `columns` sub-mapping of a parsed `statistics.annotations.yaml`, or None.

    Each entry is `{note, claims, values}`, all optional (SPEC 2.7.1); an entry that is
    not a mapping is dropped rather than failing the table's whole annotation set.
    """

    if mapping is None:
        return None

    columns = mapping.get("columns")

    if not isinstance(columns, dict):
        return None

    return {name: entry for name, entry in columns.items() if isinstance(entry, dict)}


def _annotated_grain(mapping: dict[str, Any] | None) -> dict[str, Any] | None:
    """The `grain` block of a parsed `statistics.annotations.yaml`, or None (SPEC 2.7.1)."""

    if mapping is None:
        return None

    grain = mapping.get("grain")

    return grain if isinstance(grain, dict) else None


def _relationship_annotation_entries(
    mapping: dict[str, Any] | None,
) -> list[dict[str, Any]] | None:
    """`refers_to` from a parsed `relationships.annotations.yaml`, None if absent (SPEC 2.7.2)."""

    if mapping is None:
        return None

    entries = mapping.get("refers_to")

    if not isinstance(entries, list):
        return None

    return [entry for entry in entries if isinstance(entry, dict)]


def _load_table_artifacts(manifest: dict[str, Any], print_root: Path, fqn: str) -> TableArtifacts:
    """Read every available per-table artifact off disk; tolerate missing optional pieces."""

    entry = walkable_tables(manifest).get(fqn) or {}
    table_path = table_directory(print_root, fqn, entry)
    artifacts = declared_artifacts(entry)

    ddl_path = table_path / artifacts.get("ddl", "ddl.sql")
    ddl = ddl_path.read_text(encoding="utf-8") if ddl_path.is_file() else ""

    statistics = None
    corrupted: dict[str, str] = {}

    if "statistics" in artifacts:
        statistics, stats_reason = _load_artifact(table_path / artifacts["statistics"])

        if stats_reason is not None:
            corrupted["statistics"] = stats_reason

    relationships = None

    if "relationships" in artifacts:
        relationships, rel_reason = _load_artifact(table_path / artifacts["relationships"])

        if rel_reason is not None:
            corrupted["relationships"] = rel_reason

    description = None

    if "description" in artifacts:
        desc_path = table_path / artifacts["description"]

        if desc_path.is_file():
            description = desc_path.read_text(encoding="utf-8")

    annotations = None
    annotated_grain = None

    if "statistics_annotations" in artifacts:
        stats_ann, stats_ann_reason = _load_artifact(
            table_path / artifacts["statistics_annotations"],
        )

        if stats_ann_reason is not None:
            corrupted["statistics_annotations"] = stats_ann_reason

        annotations = _annotation_columns(stats_ann)
        annotated_grain = _annotated_grain(stats_ann)

        # A key naming a column no longer in the table is stale (SPEC 2.7.1). `statistics` is
        # None only for an artifact predating the columns map, where such keys stand as-is.
        if annotations and statistics is not None:
            known_columns = statistics.get("columns") or {}
            annotations = {
                name: entry for name, entry in annotations.items() if name in known_columns
            }

    relationship_annotations = None

    if "relationships_annotations" in artifacts:
        rel_ann, rel_ann_reason = _load_artifact(
            table_path / artifacts["relationships_annotations"],
        )

        if rel_ann_reason is not None:
            corrupted["relationships_annotations"] = rel_ann_reason

        relationship_annotations = _relationship_annotation_entries(rel_ann)

    table_params = entry.get("statistics_params")

    return TableArtifacts(
        fqn=fqn,
        table_type=entry.get("type", "table"),
        row_count=entry.get("row_count"),
        column_count=int(entry.get("columns") or 0),
        ddl=ddl,
        statistics=statistics,
        relationships=relationships,
        description=description,
        annotations=annotations,
        annotated_grain=annotated_grain,
        relationship_annotations=relationship_annotations,
        missing=missing_artifacts(table_path, artifacts),
        corrupted=corrupted,
        statistics_params_override=table_params if isinstance(table_params, dict) else None,
    )


def assemble_structured(
    manifest: dict[str, Any],
    print_root: Path,
    table: str,
    options: AssemblyOptions,
) -> dict[str, Any]:
    """The single-table structured object `format: json` / `format: yaml` describe.

    For MCP's `get_table_context`, built by the same builder the CLI's json/yaml paths use,
    so `budget_tokens` and `--budget` mean the same thing regardless of caller.
    """

    a = _load_table_artifacts(manifest, print_root, table)

    return _budgeted_structured_payload(a, options, options.budget)


def _budgeted_structured_payload(
    a: TableArtifacts,
    options: AssemblyOptions,
    budget: int | None,
) -> dict[str, Any]:
    """One table's structured payload under a budget; the identity fields never drop."""

    header: dict[str, Any] = {"table": a.fqn, "type": a.table_type, "columns_count": a.column_count}

    if a.row_count is not None:
        header["row_count"] = a.row_count

    if a.missing:
        header["_missing"] = list(a.missing)

    if a.corrupted:
        header["_corrupted"] = dict(a.corrupted)

    candidates: list[tuple[str, Any]] = []

    if options.include_ddl:
        candidates.append(("ddl", a.ddl))

    if options.include_description and a.description is not None:
        candidates.append(("description", a.description))

    if options.include_annotations and a.annotations:
        candidates.append(("annotations", a.annotations))

    if options.include_annotations and a.annotated_grain:
        candidates.append(("grain_annotations", a.annotated_grain))

    if options.include_stats and a.statistics is not None:
        candidates.append(("statistics", _stripped_statistics(a.statistics)))

    if options.include_relationships and a.relationships is not None:
        candidates.append(("relationships", a.relationships))

    if options.include_relationships and a.relationship_annotations:
        candidates.append(("relationship_annotations", a.relationship_annotations))

    sections = [make_section(name, _measure_for_budget(value)) for name, value in candidates]
    selection = select(sections, budget)
    included_names = {s.name for s in selection.included}

    payload = dict(header)

    for name, value in candidates:
        if name in included_names:
            payload[name] = value

    if selection.truncated:
        payload["_truncated"] = [name for name, _ in candidates if name not in included_names]

    return payload


def _stripped_statistics(statistics: dict[str, Any]) -> dict[str, Any]:
    """`statistics` with each column's `sketch` removed - a copy, never mutated in place.

    No surface on this path decodes a sketch; the resource endpoint still serves it verbatim.
    """

    columns = statistics.get("columns")

    if not isinstance(columns, dict):
        return statistics

    stripped_columns = {
        name: {k: v for k, v in col.items() if k != "sketch"} if isinstance(col, dict) else col
        for name, col in columns.items()
    }

    return {**statistics, "columns": stripped_columns}


def _measure_for_budget(value: Any) -> str:
    """Text whose length approximates `value`'s token cost - never emitted itself."""

    return value if isinstance(value, str) else json.dumps(value, default=str)


def _assemble_json(artifacts: list[TableArtifacts], options: AssemblyOptions) -> AssemblyResult:
    """JSON output: array of per-table objects (single object if exactly one)."""

    payloads, included, truncated = _budgeted_structured_payloads(artifacts, options)
    body: Any = payloads[0] if len(payloads) == 1 else payloads
    text = json.dumps(body, indent=2, default=str, sort_keys=False)

    return AssemblyResult(
        text=text + "\n",
        tables_included=included,
        truncated=truncated,
    )


def _assemble_yaml(artifacts: list[TableArtifacts], options: AssemblyOptions) -> AssemblyResult:
    """YAML output: multi-document stream, one document per table."""

    payloads, included, truncated = _budgeted_structured_payloads(artifacts, options)
    text = yaml.safe_dump_all(payloads, sort_keys=False, default_flow_style=False)

    return AssemblyResult(
        text=text,
        tables_included=included,
        truncated=truncated,
    )


def _budgeted_structured_payloads(
    artifacts: list[TableArtifacts],
    options: AssemblyOptions,
) -> tuple[list[dict[str, Any]], int, tuple[str, ...]]:
    """Per-table payloads under an even split of the total budget.

    These formats carry no document header, so the split is `budget // len(artifacts)`; a
    split that floors to zero excludes every table from `tables_included`.
    """

    if options.budget is None:
        payloads = [_budgeted_structured_payload(a, options, None) for a in artifacts]

        return payloads, len(artifacts), ()

    per_table_budget = options.budget // len(artifacts)
    payloads = []
    truncated: list[str] = []

    for a in artifacts:
        payload = _budgeted_structured_payload(a, options, per_table_budget)
        payloads.append(payload)

        if "_truncated" in payload:
            truncated.append(a.fqn)

    included = len(artifacts) if per_table_budget > 0 else 0

    return payloads, included, tuple(truncated)
