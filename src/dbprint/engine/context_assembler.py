"""Per-table context-fragment builder for `dbprint context`.

Reads the on-disk artifacts, assembles a per-table fragment in the requested format
(md/json/yaml), applies the token-budget algorithm, and joins fragments across tables.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from . import notes_synthesis
from .baseline import declared_artifacts, missing_artifacts, table_directory, walkable_tables
from .token_budget import Section, make_section, select, truncation_marker
from .yaml_dumper import spell_inline


HEADER_TOKEN_OVERHEAD = 8  # conservative reserve for the multi-table document header
NULL_PATTERN_DISPLAY_LIMIT = 8  # combinations rendered before the rest are summarised

Purpose = Literal["profile", "query"]

# Fixed sentences, so a consumer can key on them rather than parse a number it also gets.
WHOLE_DOMAIN_STATEMENT = "the list is the whole domain"
SCANNED_DOMAIN_STATEMENT = "the list is the whole domain over the rows scanned"
SAMPLED_STATEMENT = "a sample of the most frequent values"

QUERY_SAMPLE_LIMIT = 5  # most frequent values of a sampled column the query purpose shows

_DETECTION_RANK = {"declared": 0, "inferred": 1, "measured": 2}

# Budget-keep order under `query`, not render order; the header is pinned rather than ranked.
_QUERY_SECTION_PRIORITY = ("ddl", "values", "joins", "dictionary")


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
    purpose: Purpose = "profile"


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


@dataclass(frozen=True)
class PayloadResult:
    """One connection's structured payloads, before a caller renders or wraps them."""

    payloads: list[dict[str, Any]]
    tables_included: int
    truncated: tuple[str, ...]


def assemble(
    manifest: dict[str, Any],
    print_root: Path,
    tables: list[str],
    options: AssemblyOptions,
    connection_name: str | None = None,
    multi_connection: bool = False,
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
        return _assemble_markdown(
            loaded,
            options,
            connection_name,
            print_root,
            manifest,
            multi_connection,
        )


def assemble_payloads(
    manifest: dict[str, Any],
    print_root: Path,
    tables: list[str],
    options: AssemblyOptions,
) -> PayloadResult:
    """The per-table structured payloads the json and yaml formats carry, before rendering.

    A multi-connection caller wraps these, so the connection name rides the wrapper (MCP.md 4.1).
    """

    if not tables:
        return PayloadResult(payloads=[], tables_included=0, truncated=())

    loaded = [_load_table_artifacts(manifest, print_root, fqn) for fqn in tables]
    payloads, included, truncated = _budgeted_structured_payloads(loaded, options)

    return PayloadResult(payloads=payloads, tables_included=included, truncated=truncated)


def _assemble_markdown(
    artifacts: list[TableArtifacts],
    options: AssemblyOptions,
    connection_name: str | None,
    print_root: Path,
    manifest: dict[str, Any],
    multi_connection: bool = False,
) -> AssemblyResult:
    """Header provenance rides a multi-table or multi-connection render; a lone fragment states
    its own dialect instead (SPEC 2.5). Connection notes appear on the multi-table case alone.
    """

    multi_table = len(artifacts) > 1
    header = ""
    header_tokens = 0

    if (multi_table or multi_connection) and connection_name:
        table_word = "table" if len(artifacts) == 1 else "tables"
        header = f"# Context for connection {connection_name} ({len(artifacts)} {table_word})"

        if multi_table:
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

    if options.purpose == "query":
        return _render_query_markdown(a, options, budget, adapter)

    include_qualifiers = options.include_stats and bool(a.statistics)
    sections: list[Section] = []
    sections.append(
        make_section(
            "header",
            _markdown_header(a, include_qualifiers, connection_statistics_params, adapter),
            pinned=True,
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


def _render_query_markdown(
    a: TableArtifacts,
    options: AssemblyOptions,
    budget: int | None,
    adapter: str | None,
) -> tuple[str, bool, bool]:
    """The `query` purpose: what a query writer reads, and nothing measured about the data.

    Sections render in reading order and drop in `_QUERY_SECTION_PRIORITY` order.
    """

    rendered = (
        ("header", _query_markdown_header(a, adapter)),
        ("ddl", _markdown_ddl(a) if options.include_ddl else ""),
        ("joins", _markdown_joins(a) if options.include_relationships else ""),
        ("dictionary", _markdown_data_dictionary(a, options)),
        ("values", _markdown_column_values(a)),
    )
    sections = {
        name: make_section(name, text, pinned=name == "header") for name, text in rendered if text
    }
    offered = [sections[n] for n in ("header", *_QUERY_SECTION_PRIORITY) if n in sections]
    selection = select(offered, budget)
    included = {s.name for s in selection.included}
    text = "\n\n".join(sections[name].text for name, _ in rendered if name in included)
    marker = truncation_marker(selection)

    if marker:
        text = f"{text}\n\n{marker}" if text else marker

    return text, selection.truncated, bool(selection.included)


def _query_markdown_header(a: TableArtifacts, adapter: str | None) -> str:
    """Identity plus the scope marker - what the value lists below cover, and no other measure."""

    lines = _identity_lines(a, adapter)
    scope = _scope_summary(a.statistics or {})

    if scope:
        lines.append(scope)

    return "\n".join(lines)


def _markdown_joins(a: TableArtifacts) -> str:
    """Every edge the print knows with how it was found (SPEC 2.3): the join paths.

    An inferred or measured edge is here and nowhere else a query writer reads.
    """

    refers_to, referenced_by = _edges(a)

    if not refers_to and not referenced_by:
        return ""

    rejected = _rejected_edges(a.relationship_annotations)
    lines = ["## Joins"]

    for entry in refers_to:
        target = f"{entry.get('target_table', '?')}.{_join_columns(entry.get('target_column'))}"
        lines.append(
            f"- {_join_columns(entry.get('column'))} -> {target} ({_edge_detection(entry)})",
        )
        lines.extend(_rejection_line(rejected.get(_edge_key(entry))))

    for entry in referenced_by:
        source = (
            f"{entry.get('referencer_table', '?')}.{_join_columns(entry.get('referencer_column'))}"
        )
        lines.append(
            f"- {_join_columns(entry.get('column'))} <- {source} ({_edge_detection(entry)})",
        )

    return "\n".join(lines)


def _edges(a: TableArtifacts) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The table's `refers_to` and `referenced_by` edges, the surest first.

    Declared, then inferred, then measured (SPEC 2.3); the artifact's own order within a rank.
    """

    relationships = a.relationships or {}

    def ranked(edges: Any) -> list[dict[str, Any]]:
        listed = [e for e in edges or [] if isinstance(e, dict)]

        return sorted(
            listed,
            key=lambda e: _DETECTION_RANK.get(_edge_detection(e), len(_DETECTION_RANK)),
        )

    return ranked(relationships.get("refers_to")), ranked(relationships.get("referenced_by"))


def _join_columns(columns: Any) -> str:
    """One column bare, a composite key in parentheses, so `a, b -> t.c, d` cannot be misread."""

    names = [str(c) for c in columns] if isinstance(columns, list) else []

    if not names:
        return "?"

    return names[0] if len(names) == 1 else "(" + ", ".join(names) + ")"


def _markdown_data_dictionary(a: TableArtifacts, options: AssemblyOptions) -> str:
    """The table's description and every column a human has defined (SPEC 2.7.1)."""

    description = a.description if options.include_description else None
    notes = (
        [
            f"- {name}: {' '.join(entry['note'].split())}"
            for name, entry in (a.annotations or {}).items()
            if isinstance(entry.get("note"), str) and entry["note"].strip()
        ]
        if options.include_annotations
        else []
    )

    if not notes and not description:
        return ""

    lines = ["## Data dictionary"]

    if description:
        lines += ["", description.rstrip()]

    if notes:
        lines += ["", *notes]

    return "\n".join(lines)


def _markdown_column_values(a: TableArtifacts) -> str:
    """Every column whose list a predicate can rely on: the whole domain, or a stated share."""

    columns = (a.statistics or {}).get("columns")

    if not isinstance(columns, dict):
        return ""

    scoped = isinstance((a.statistics or {}).get("scope"), dict)
    annotations = a.annotations or {}
    rows = []

    for name in _ordered_column_names(columns):
        col = columns[name]
        listed = _covered_values(col)

        if listed is None:
            continue

        entries, coverage = listed
        shown, share = _shown_values(entries, coverage)
        values_cell = _values_cell(col, shown, annotations.get(name))
        statement = _coverage_statement(coverage, scoped=scoped)
        rows.append(f"| {_escape_cell(name)} | {values_cell} | {share} - {statement} |")

    if not rows:
        return ""

    return "\n".join(
        ["## Column values", "", "| Column | Values (count) | Coverage |", "|---|---|---|", *rows],
    )


def _covered_values(col: Any) -> tuple[list[dict[str, Any]], float] | None:
    """The value entries and the coverage describing them; None without both (SPEC 2.2.3).

    A `numeric`/`temporal` list carries no coverage: a frequency sample, never a domain.
    """

    if not isinstance(col, dict):
        return None

    entries = col.get("values")
    coverage = col.get("values_coverage")

    if not isinstance(entries, list) or not entries or not _is_number(coverage):
        return None

    return [e for e in entries if isinstance(e, dict)], float(coverage)


def _shown_values(
    entries: list[dict[str, Any]],
    coverage: float,
) -> tuple[list[dict[str, Any]], float]:
    """The entries `query` shows and the share of the column they cover.

    A sampled list is cut to `QUERY_SAMPLE_LIMIT` categories, a spelling group counting as one.
    """

    if coverage == 1.0:
        return entries, coverage

    groups = _spelling_groups(entries)[:QUERY_SAMPLE_LIMIT]
    shown = [entry for canonical, members in groups for entry in (canonical, *members)]
    listed = sum(_count_of(e) for e in entries)
    kept = sum(_count_of(e) for e in shown)

    return shown, round(coverage * kept / listed, 4) if listed else coverage


def _count_of(entry: dict[str, Any]) -> int:
    """An entry's count; 0 where the artifact carries no integer."""

    count = entry.get("count")

    return count if isinstance(count, int) and not isinstance(count, bool) else 0


def _is_number(value: Any) -> bool:
    """A real number - `bool` is an `int` to Python and never a coverage."""

    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _values_cell(
    col: dict[str, Any],
    entries: list[Any],
    annotation: dict[str, Any] | None,
) -> str:
    """One column's listed values; a redacted column publishes its counts and no literal."""

    redaction = col.get("redacted")
    counts = [entry.get("count") for entry in entries if isinstance(entry, dict)]

    if isinstance(redaction, str) and redaction:
        return f"values withheld ({redaction}), counts " + " / ".join(str(c) for c in counts)

    notes = _value_notes(annotation)
    cells = []

    for entry, members in _spelling_groups(entries):
        value = entry.get("value")
        spelled = "NULL" if value is None else spell_inline(value)
        counted = [entry, *members]
        total = sum(e.get("count") or 0 for e in counted)
        cell = f"{_escape_cell(spelled)} ({total})"

        if members:
            spellings = ", ".join(
                f"{_escape_cell(spell_inline(e.get('value')))} {e.get('count')}" for e in counted
            )
            cell += f" {{{spellings}}}"

        note = notes.get(_value_key(value))

        if note:
            cell += f" = {_escape_cell(note)}"

        cells.append(cell)

    return " / ".join(cells)


def _spelling_groups(entries: list[Any]) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Canonical entries in the list's own order, each with the lesser spellings of it.

    A member naming a value the list lacks stands alone: dropped, its literal would vanish.
    """

    listed = {
        _value_key(entry.get("value"))
        for entry in entries
        if isinstance(entry, dict) and entry.get("spelling_of") is None
    }
    members: dict[str, list[dict[str, Any]]] = {}

    for entry in entries:
        if isinstance(entry, dict) and entry.get("spelling_of") is not None:
            members.setdefault(_value_key(entry["spelling_of"]), []).append(entry)

    return [
        (entry, members.get(_value_key(entry.get("value")), []))
        for entry in entries
        if isinstance(entry, dict)
        and (entry.get("spelling_of") is None or _value_key(entry["spelling_of"]) not in listed)
    ]


def _coverage_statement(coverage: float, *, scoped: bool) -> str:
    """What the list is: the column's whole domain, or its most frequent values (SPEC 2.2.4).

    Under `scope` an exhaustive list is exhaustive over the rows scanned, never over the table.
    """

    if coverage == 1.0:
        return SCANNED_DOMAIN_STATEMENT if scoped else WHOLE_DOMAIN_STATEMENT

    return SAMPLED_STATEMENT


def _value_notes(annotation: dict[str, Any] | None) -> dict[str, str]:
    """A column's per-value notes (SPEC 2.7.1), keyed for lookup beside the value itself."""

    if not isinstance(annotation, dict):
        return {}

    entries = annotation.get("values")

    if not isinstance(entries, list):
        return {}

    return {
        _value_key(entry.get("value")): " ".join(entry["note"].split())
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("note"), str) and entry["note"].strip()
    }


def _value_key(value: Any) -> str:
    """YAML reads `1` and `'1'` as different scalars; the string form matches either spelling."""

    return str(value)


def _escape_cell(text: str) -> str:
    """Make `text` safe to interpolate into one Markdown table cell.

    Order is load-bearing: escaping the pipe first leaves a live delimiter behind a backslash.
    """

    escaped = text.replace("\\", "\\\\").replace("|", "\\|")

    return escaped.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def _identity_lines(a: TableArtifacts, adapter: str | None) -> list[str]:
    """What every header opens with: the table, its dialect, and what is missing from it.

    The adapter and missing-artifact lines are unconditional (SPEC 2.5).
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

    return lines


def _markdown_header(
    a: TableArtifacts,
    include_qualifiers: bool,
    connection_statistics_params: dict[str, Any],
    adapter: str | None,
) -> str:
    """The identity lines, then one line per table-level qualifier that applies."""

    lines = _identity_lines(a, adapter)

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
        lines.append(
            f"| {_escape_cell(name)} "
            f"| {_escape_cell(str(col.get('sql_type', '?')))} "
            f"| {_escape_cell(str(col.get('classification', '?')))} |",
        )

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
        lines.append(
            f"| {_escape_cell(name)} | {_escape_cell(cardinality)} | {_escape_cell(notes)} |",
        )

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
        lines.append(f"| {int(entry.get('count') or 0):,} | {_escape_cell(names)} |")

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

    if options.purpose == "query":
        scope = (a.statistics or {}).get("scope")

        if isinstance(scope, dict):
            # The counts below it describe the scanned set; `statistics` carried this on the
            # profile path, and `query` drops that object (SPEC 2.2.8).
            header["scope"] = scope

        return _payload_from(header, _query_candidates(a, options), budget)

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

    return _payload_from(header, candidates, budget)


def _payload_from(
    header: dict[str, Any],
    candidates: list[tuple[str, Any]],
    budget: int | None,
) -> dict[str, Any]:
    """Apply the budget to an ordered candidate list; the identity fields never drop."""

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


def _query_candidates(a: TableArtifacts, options: AssemblyOptions) -> list[tuple[str, Any]]:
    """The `query` selection as structured data, ordered as a budget should keep it."""

    candidates: list[tuple[str, Any]] = []

    if options.include_ddl:
        candidates.append(("ddl", a.ddl))

    values = _structured_values(a)

    if values:
        candidates.append(("values", values))

    joins = _structured_joins(a) if options.include_relationships else {}

    if joins:
        candidates.append(("joins", joins))

    dictionary = (
        {
            name: " ".join(entry["note"].split())
            for name, entry in (a.annotations or {}).items()
            if isinstance(entry.get("note"), str) and entry["note"].strip()
        }
        if options.include_annotations
        else {}
    )

    if dictionary:
        candidates.append(("dictionary", dictionary))

    if options.include_description and a.description is not None:
        candidates.append(("description", a.description))

    return candidates


def _structured_joins(a: TableArtifacts) -> dict[str, Any]:
    """The Joins list as data: each edge's columns and detection, and a human's rejection."""

    refers_to, referenced_by = _edges(a)

    if not refers_to and not referenced_by:
        return {}

    rejected = _rejected_edges(a.relationship_annotations)
    out: dict[str, Any] = {"refers_to": [], "referenced_by": []}

    for entry in refers_to:
        edge = _edge_fields(entry, ("column", "target_table", "target_column"))
        rejection = rejected.get(_edge_key(entry))

        if rejection is not None:
            note = rejection.get("note")
            edge["rejected"] = note if isinstance(note, str) and note.strip() else True

        out["refers_to"].append(edge)

    for entry in referenced_by:
        out["referenced_by"].append(
            _edge_fields(entry, ("column", "referencer_table", "referencer_column")),
        )

    return out


def _edge_fields(entry: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """The named keys of an edge as the artifact spells them, plus its detection."""

    edge = {key: entry[key] for key in keys if key in entry}
    edge["detection"] = _edge_detection(entry)

    return edge


def _structured_values(a: TableArtifacts) -> dict[str, Any]:
    """Per column: the entries the `query` purpose shows, their notes, and what they cover."""

    columns = (a.statistics or {}).get("columns")

    if not isinstance(columns, dict):
        return {}

    scoped = isinstance((a.statistics or {}).get("scope"), dict)
    annotations = a.annotations or {}
    out: dict[str, Any] = {}

    for name in _ordered_column_names(columns):
        col = columns[name]
        listed = _covered_values(col)

        if listed is None:
            continue

        entries, coverage = listed
        shown, share = _shown_values(entries, coverage)
        block: dict[str, Any] = {
            "coverage": coverage,
            "coverage_statement": _coverage_statement(coverage, scoped=scoped),
        }

        if coverage != 1.0:
            block["shown_coverage"] = share

        redaction = col.get("redacted")

        if isinstance(redaction, str) and redaction:
            block["redacted"] = redaction
            block["counts"] = [e.get("count") for e in shown]
        else:
            block["entries"] = _structured_entries(shown, annotations.get(name))

        out[name] = block

    return out


def _structured_entries(entries: list[Any], annotation: dict[str, Any] | None) -> list[Any]:
    notes = _value_notes(annotation)
    rendered = []

    for entry in entries:
        if not isinstance(entry, dict):
            continue

        item = {"value": entry.get("value"), "count": entry.get("count")}
        note = notes.get(_value_key(entry.get("value")))

        if note:
            item["note"] = note

        if entry.get("spelling_of") is not None:
            item["spelling_of"] = entry["spelling_of"]

        rendered.append(item)

    return rendered


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
