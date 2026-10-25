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

from dbprint.spec import authored
from dbprint.spec.absence import block_value, column_value, disabled_of
from dbprint.spec.artifacts import (
    DESCRIPTION_FILENAME,
    MANIFEST_ANNOTATIONS_FILENAME,
    RELATIONSHIPS_ANNOTATIONS_FILENAME,
    STATISTICS_ANNOTATIONS_FILENAME,
    declared_artifacts,
    walkable_tables,
)
from dbprint.spec.classification import compute_null_rate
from dbprint.spec.edges import edge_detection, edge_key, rejected_edges
from dbprint.spec.parts import display, member, parent, parse
from dbprint.spec.scope import (
    ScanScope,
    coverage_statement,
    qualify,
    reply_scope,
    rows_scanned,
    scope_line,
    scope_of,
)
from dbprint.spec.value_text import spell_number, spell_percent
from . import context_terms, notes_synthesis, table_readings
from .baseline import (
    ArtifactReader,
    contained,
    disabled_feature_message,
    failed_tables,
    missing_artifacts,
    read_artifact,
    table_directory,
    unmeasured_block_message,
    unprofiled_message,
)
from .context_terms import TERMS
from .notes_synthesis import Rendered
from .relationship_graph import incoming_key, with_human_edges, withhold_rejected
from .table_readings import grain_reading
from .token_budget import Section, make_section, select, tokens_of, truncation_marker
from .value_list import grouped_values, value_key, value_notes
from .yaml_dumper import spell_inline, spell_literal, spell_value


HEADER_TOKEN_OVERHEAD = 8  # conservative reserve for the multi-table document header
NULL_PATTERN_DISPLAY_LIMIT = 8  # combinations rendered; the footer covers exactly these

Purpose = Literal["profile", "query"]

QUERY_SAMPLE_LIMIT = 5  # most frequent values of a sampled column the query purpose shows
QUERY_FREQUENT_SHARE = 0.01  # least share of non-null rows a numeric/temporal value needs to show

_DETECTION_RANK = {"declared": 0, "inferred": 1, "measured": 2, "annotated": 3}
_NOT_A_JOIN_TARGET = "not a join target, no declared-unique column"
_MEANINGS_UNDOCUMENTED = "meanings: not documented here"
_CODED_CLASSIFICATIONS = frozenset({"categorical", "text"})
_FREQUENCY_CLASSIFICATIONS = frozenset({"numeric", "temporal"})
_FREQUENT_STATEMENT = "frequent values, not a domain"

# Budget-keep order under `query`, not render order; the header is pinned rather than ranked.
_QUERY_SECTION_PRIORITY = ("legend", "ddl", "connection_notes", "values", "joins", "dictionary")

_BLOCK_LABEL = {
    "physical_layout": "clustering or partitioning",
    "null_patterns": "columns null on the same rows",
}
_DISABLED_LABEL = {
    "looks_like": "value shapes (looks like)",
    "null_patterns": "columns null on the same rows",
    "parts": "parts of nested columns",
    "relationships.infer": "inferred relationships",
    "sketches": "value sketches and measured joins",
    "timeline": "timeline",
}


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
    profile_override: dict[str, Any] | None
    last_run_failed: bool = False
    # Each referencer column's distinct count, keyed (referencer_table, column), for SPEC 2.3.10.
    incoming_cardinalities: dict[tuple[Any, str], int] = field(default_factory=dict)
    # SPEC 2.7.3's notes, set on a single-table render only; a multi-table one carries them once.
    connection_notes: str | None = None
    # SPEC 2.7.1's `derived_from`: per column, each source's label and its note and value notes.
    inherited: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


@dataclass
class AssemblyResult:
    """Full rendered output of one assembly run."""

    text: str
    tables_included: int
    truncated: tuple[str, ...] = field(default_factory=tuple)
    terms: frozenset[str] = frozenset()


@dataclass(frozen=True)
class PayloadResult:
    """One connection's structured payloads, before a caller renders or wraps them."""

    payloads: list[dict[str, Any]]
    tables_included: int
    truncated: tuple[str, ...]
    # The connection's `connection_notes` and `_corrupted`, which ride the wrapper, not a table.
    header: dict[str, Any] = field(default_factory=dict)


OpenQuestionKind = Literal["description", "codes", "frequent_value", "edge"]


@dataclass(frozen=True)
class OpenQuestion:
    """One thing the print measured but cannot explain, and the file that would answer it."""

    kind: OpenQuestionKind
    column: str | None
    values: tuple[Any, ...]
    edge: str | None
    file: str
    key: str | None


def assemble(
    manifest: dict[str, Any],
    print_root: Path,
    tables: list[str],
    options: AssemblyOptions,
    connection_name: str | None = None,
    multi_connection: bool = False,
    read: ArtifactReader = read_artifact,
) -> AssemblyResult:
    """Assemble the requested table fragments; `tables` is the caller's resolved FQN order."""

    if not tables:
        return AssemblyResult(text="", tables_included=0)

    loaded = [_load_table_artifacts(manifest, print_root, fqn, read) for fqn in tables]

    if len(loaded) == 1:
        _attach_connection_notes(loaded[0], print_root, read)

    if options.format in ("json", "yaml"):
        return _assemble_structured(loaded, options, connection_name, print_root, read)
    else:
        return _assemble_markdown(
            loaded,
            options,
            connection_name,
            print_root,
            manifest,
            multi_connection,
            read,
        )


def assemble_payloads(
    manifest: dict[str, Any],
    print_root: Path,
    tables: list[str],
    options: AssemblyOptions,
) -> PayloadResult:
    """The per-table structured payloads the json and yaml formats carry, before rendering.

    A multi-connection caller wraps these, so the connection name and `header` ride the wrapper.
    """

    if not tables:
        return PayloadResult(payloads=[], tables_included=0, truncated=())

    loaded = [_load_table_artifacts(manifest, print_root, fqn, read_artifact) for fqn in tables]
    header = _connection_keys(print_root, options, read_artifact)
    payloads, included, truncated = _budgeted_structured_payloads(loaded, options, header)

    return PayloadResult(
        payloads=payloads,
        tables_included=included,
        truncated=truncated,
        header=header,
    )


def ranked_sections(
    manifest: dict[str, Any],
    print_root: Path,
    table: str,
    options: AssemblyOptions,
    read: ArtifactReader = read_artifact,
) -> list[tuple[str, str, tuple[frozenset[str], ...]]]:
    """One table's Markdown sections, none dropped, in the order a budget would keep them.

    Each carries its lines' term keys; the caller builds the legend (`context_terms.legend`).
    """

    a = _load_table_artifacts(manifest, print_root, table, read)
    _attach_connection_notes(a, print_root, read)
    adapter = manifest.get("adapter")
    adapter = adapter if isinstance(adapter, str) and adapter else None
    params = table_readings.connection_profile(manifest)

    return [(s.name, s.text, s.line_terms) for s in _sections(a, options, params, adapter)]


def structured_sections(
    manifest: dict[str, Any],
    print_root: Path,
    table: str,
    options: AssemblyOptions,
    read: ArtifactReader = read_artifact,
) -> tuple[dict[str, Any], list[tuple[str, Any]]]:
    """One table's structured identity fields and its sections, none dropped, in budget order."""

    a = _load_table_artifacts(manifest, print_root, table, read)
    _attach_connection_notes(a, print_root, read)

    return _structured_parts(a, options)


def open_questions(
    manifest: dict[str, Any],
    print_root: Path,
    table: str,
    read: ArtifactReader = read_artifact,
) -> list[OpenQuestion]:
    """What `table`'s print cannot answer and the print-root-relative file each is answered in."""

    a = _load_table_artifacts(manifest, print_root, table, read)
    entry = walkable_tables(manifest).get(table) or {}
    directory = table_directory(print_root, table, entry).relative_to(print_root).as_posix()
    columns_file = f"{directory}/{STATISTICS_ANNOTATIONS_FILENAME}"
    out: list[OpenQuestion] = []

    if not (a.description and a.description.strip()):
        out.append(
            OpenQuestion(
                "description",
                None,
                (),
                None,
                f"{directory}/{DESCRIPTION_FILENAME}",
                None,
            ),
        )

    columns = (a.statistics or {}).get("columns")
    columns = columns if isinstance(columns, dict) else {}
    scope = scope_of(a.statistics)
    row_count = block_value(a.statistics or {}, "row_count")

    for name in _ordered_column_names(columns):
        col = columns[name]
        annotation = (a.annotations or {}).get(name)
        key = f"columns.{name}.values"

        if not isinstance(col, dict) or column_value(col, "redacted"):
            continue

        listed = _covered_values(col)

        if listed is not None:
            if _codes_undocumented(col, listed[1], annotation, a.inherited.get(name)):
                codes = tuple(e.get("value") for e in listed[0])
                out.append(OpenQuestion("codes", name, codes, None, columns_file, key))

            continue

        noted = value_notes(annotation)

        for frequent in (_frequent_values(col, scope, row_count) or ([], 0.0))[0]:
            if value_key(frequent.get("value")) not in noted:
                value = (frequent.get("value"),)
                out.append(OpenQuestion("frequent_value", name, value, None, columns_file, key))

    for edge in _edges(a)[0]:
        detection = edge_detection(edge)

        if detection not in {"measured", "inferred"} or str(edge.get("note") or "").strip():
            continue

        source = ", ".join(str(c) for c in edge.get("column") or [])
        target = ", ".join(str(c) for c in edge.get("target_column") or [])
        described = f"{source} -> {edge.get('target_table')}.{target} ({detection})"
        rel_file = f"{directory}/{RELATIONSHIPS_ANNOTATIONS_FILENAME}"
        out.append(OpenQuestion("edge", None, (), described, rel_file, "refers_to"))

    return out


def fk_target_map(
    relationships: dict[str, Any] | None,
    relationship_annotations: list[Any] | None = None,
) -> dict[str, list[str]]:
    """Every `refers_to` edge per source column, surest first, less rejected ones (SPEC 2.7.2).

    A composite or path-valued edge is listed under each member column, its own endpoint in front.
    """

    rejected = rejected_edges(relationship_annotations, _by_edge_key(relationships))
    out: dict[str, list[str]] = {}

    for entry in _ranked((relationships or {}).get("refers_to")):
        if edge_key(entry) in rejected:
            continue

        cols = [str(c) for c in entry.get("column") or []]
        label = f"{_target(entry)} ({edge_detection(entry)})"

        if len(cols) > 1 or entry.get("path"):
            label = f"{_endpoint(entry, 'column', 'path')} -> {label}"

        for column in cols:
            out.setdefault(column, []).append(label)

    return out


def _assemble_markdown(
    artifacts: list[TableArtifacts],
    options: AssemblyOptions,
    connection_name: str | None,
    print_root: Path,
    manifest: dict[str, Any],
    multi_connection: bool,
    read: ArtifactReader,
) -> AssemblyResult:
    """Header provenance rides a multi-table or multi-connection render (SPEC 2.5); connection
    notes ride that header over several tables and their own section over one.
    """

    multi_table = len(artifacts) > 1
    header = ""
    header_tokens = 0

    if (multi_table or multi_connection) and connection_name:
        table_word = "table" if len(artifacts) == 1 else "tables"
        header = f"# Context for connection {connection_name} ({len(artifacts)} {table_word})"

        if multi_table:
            notes, notes_reason = load_connection_notes(print_root, read)

            if notes and options.include_annotations:
                header += "\n\n" + notes

            if notes_reason is not None:
                header += "\n\n" + _corrupted_summary({"manifest_annotations": notes_reason})

        provenance = _provenance_block(manifest, connection_name)

        if provenance:
            header += "\n\n" + provenance

        header_tokens = max(HEADER_TOKEN_OVERHEAD, len(header) // 4)

    connection_profile = table_readings.connection_profile(manifest)
    adapter = manifest.get("adapter")
    adapter = adapter if isinstance(adapter, str) and adapter else None
    shared_legend = multi_table or multi_connection
    legend_tokens = 0

    # One legend serves the whole document, so its room comes off the top like the header's.
    if shared_legend and options.budget is not None:
        offered = frozenset().union(
            *(
                s.terms
                for a in artifacts
                for s in _sections(a, options, connection_profile, adapter)
            ),
        )
        legend_tokens = tokens_of(context_terms.legend(offered))

    per_table_budget: int | None = None

    if options.budget is not None:
        remaining = max(0, options.budget - header_tokens - legend_tokens)
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
    terms: set[str] = set()
    included = 0

    for a in artifacts:
        rendered = _render_table_markdown(
            a,
            options,
            per_table_budget,
            connection_profile,
            adapter,
            with_legend=not shared_legend,
        )

        if rendered.text:
            fragments.append(rendered.text)

        terms |= rendered.terms

        # A budget too tight for even the header leaves `fragment` as the bare truncation
        # marker - real text, but not a table this run actually included.
        if rendered.has_content:
            included += 1

            if rendered.truncated:
                truncated.append(a.fqn)
        else:
            truncated.append(a.fqn)

    body = "\n\n---\n\n".join(fragments)
    legend = context_terms.legend(terms) if multi_table and not multi_connection else ""
    text = "".join(f"{part}\n\n" for part in (header, legend) if part) + body

    return AssemblyResult(
        text=text,
        tables_included=included,
        truncated=tuple(truncated),
        terms=frozenset(terms),
    )


@dataclass(frozen=True)
class _TableRender:
    text: str
    truncated: bool
    has_content: bool
    terms: frozenset[str]


def _render_table_markdown(
    a: TableArtifacts,
    options: AssemblyOptions,
    budget: int | None,
    connection_profile: dict[str, Any],
    adapter: str | None,
    *,
    with_legend: bool,
) -> _TableRender:
    """Render one table's sections in reading order, its legend second when `with_legend`.

    The legend is budgeted at every offered term, then shrunk to the terms of the kept sections.
    """

    offered = _sections(a, options, connection_profile, adapter)

    if with_legend and (
        full := context_terms.legend(frozenset().union(*(s.terms for s in offered)))
    ):
        offered = [offered[0], make_section("legend", full), *offered[1:]]

    selection = select(offered, budget)
    kept = {s.name for s in selection.included}
    terms = frozenset().union(*(s.terms for s in selection.included))
    legend = context_terms.legend(terms)
    shown = [
        s
        for s in _in_reading_order(offered, options.purpose)
        if s.name in kept and (s.name != "legend" or legend)
    ]
    text = "\n\n".join(legend if s.name == "legend" else s.text for s in shown)
    marker = truncation_marker(selection)

    # A budget too tight for even the header omits every section, so the marker is the whole
    # return, never blank - a caller must see why, not a silent empty success.
    if marker:
        text = f"{text}\n\n{marker}" if text else marker

    return _TableRender(text, selection.truncated, bool(selection.included), terms)


def _sections(
    a: TableArtifacts,
    options: AssemblyOptions,
    connection_profile: dict[str, Any],
    adapter: str | None,
) -> list[Section]:
    if options.purpose == "query":
        return _query_sections(a, options, adapter)

    return _profile_sections(a, options, connection_profile, adapter)


def _section(name: str, lines: list[Rendered], *, pinned: bool = False) -> Section:
    return make_section(
        name,
        "\n".join(line.text for line in lines),
        pinned=pinned,
        line_terms=[line.terms for line in lines],
    )


def _profile_sections(
    a: TableArtifacts,
    options: AssemblyOptions,
    connection_profile: dict[str, Any],
    adapter: str | None,
) -> list[Section]:
    include_qualifiers = options.include_stats and bool(a.statistics)
    sections: list[Section] = []
    sections.append(
        _section(
            "header",
            _markdown_header(a, include_qualifiers, connection_profile, adapter),
            pinned=True,
        ),
    )

    if options.include_annotations and a.connection_notes:
        sections.append(make_section("connection_notes", _markdown_connection_notes(a)))

    if options.include_ddl:
        sections.append(make_section("ddl", _markdown_ddl(a)))

    if options.include_description and a.description:
        sections.append(make_section("description", _markdown_description(a)))

    if options.include_annotations and a.annotations and _has_rendered_annotations(a.annotations):
        sections.append(make_section("annotations", _markdown_annotations(a)))

    if options.include_stats and a.statistics:
        if block_value(a.statistics, "catalog_only") is True:
            # SPEC 2.2.15: nothing was queried, so there is no cardinality to table - list
            # the columns a catalog read already named, not a table of fabricated cells.
            sections.append(make_section("columns", _markdown_catalog_only_columns(a)))
        else:
            if lost := _markdown_unmeasured(a):
                sections.append(make_section("unmeasured", lost))

            if off := _markdown_disabled(a):
                sections.append(make_section("disabled", off))

            if block_value(a.statistics, "physical_layout"):
                sections.append(make_section("physical_layout", _markdown_physical_layout(a)))

            profile = table_readings.effective_profile(
                connection_profile,
                a.profile_override,
            )
            sections.append(
                _section("cardinality", _markdown_cardinality_table(a, profile)),
            )

            if block_value(a.statistics, "null_patterns"):
                sections.append(_section("null_patterns", _markdown_null_patterns(a)))

    if options.include_relationships and a.relationships:
        sections.append(_section("relationships", _markdown_relationships(a)))

    # Offered last, so a wide document's parts are what a tight budget drops first.
    if options.include_stats and a.statistics and (parts := _markdown_parts(a, options)):
        sections.append(_section("parts", parts))

    return sections


def _query_sections(
    a: TableArtifacts,
    options: AssemblyOptions,
    adapter: str | None,
) -> list[Section]:
    """The `query` sections in the order a budget keeps them: the header, then by priority."""

    dictionary = _markdown_data_dictionary(a, options)
    joined = _referencing_columns(a) if options.include_relationships else set()
    rendered = (
        ("header", _query_markdown_header(a, adapter)),
        ("ddl", [Rendered(_markdown_ddl(a))] if options.include_ddl else []),
        (
            "connection_notes",
            [Rendered(_markdown_connection_notes(a))]
            if options.include_annotations and a.connection_notes
            else [],
        ),
        ("joins", _markdown_joins(a) if options.include_relationships else []),
        ("dictionary", [Rendered(dictionary)] if dictionary else []),
        ("values", _markdown_column_values(a, joined)),
    )
    sections = {
        name: _section(name, lines, pinned=name == "header") for name, lines in rendered if lines
    }

    return [sections[n] for n in ("header", *_QUERY_SECTION_PRIORITY) if n in sections]


def _in_reading_order(sections: list[Section], purpose: Purpose) -> list[Section]:
    """Query sections in reading order; profile sections already are, the legend second."""

    if purpose != "query":
        return sections

    order = ("header", "legend", "ddl", "connection_notes", "joins", "dictionary", "values")

    return sorted(sections, key=lambda s: order.index(s.name))


def _query_markdown_header(a: TableArtifacts, adapter: str | None) -> list[Rendered]:
    """Identity plus the scope marker - what the value lists below cover, and no other measure."""

    lines = _identity_lines(a, adapter, "query")

    if scoped := _scope_summary(a.statistics or {}):
        lines.append(scoped)

    if external := external_line(a.statistics or {}):
        lines.append(Rendered(external))

    if merging := _merging_summary(a.statistics or {}):
        lines.append(Rendered(merging))

    if (off := _disabled_summary(a.statistics or {})).text:
        lines.append(off)

    return lines


def _markdown_joins(a: TableArtifacts) -> list[Rendered]:
    """Every edge relationships.yaml carries but a human-rejected one, with its detection.

    An inferred or measured edge is here and nowhere else a query writer reads.
    """

    if a.relationships is None:
        return []

    refers_to, referenced_by = _edges(a)
    lines = [Rendered("## Joins", frozenset({"joins"}))]

    if not refers_to and not referenced_by:
        ineligible = a.relationships.get("eligible_target") is False

        return [
            *lines,
            Rendered(f"none found ({_NOT_A_JOIN_TARGET})" if ineligible else "none found"),
        ]
    shares = _null_shares(a)

    for entry in refers_to:
        line = Rendered(
            f"- {_endpoint(entry, 'column', 'path')} -> {_target(entry)} ({edge_detection(entry)})",
            _detection_terms(entry),
        )
        facts = [line]

        if (share := _referencing_null_share(a, entry, shares)) is not None:
            facts.append(_nulls_fact(share[0]))

        lines.append(notes_synthesis.join_facts([*facts, *_note_fact(entry)]))

    for entry in referenced_by:
        line = Rendered(
            f"- {_endpoint(entry, 'column', 'path')} <- {_referencer(entry)} "
            f"({edge_detection(entry)})",
            _detection_terms(entry),
        )
        lines.append(notes_synthesis.join_facts([line, *_note_fact(entry)]))

    return lines


def _target(entry: dict[str, Any]) -> str:
    return f"{entry.get('target_table', '?')}.{_endpoint(entry, 'target_column', 'target_path')}"


def _referencer(entry: dict[str, Any]) -> str:
    referencer = _endpoint(entry, "referencer_column", "referencer_path")

    return f"{entry.get('referencer_table', '?')}.{referencer}"


def _endpoint(entry: dict[str, Any], columns: str, path: str) -> str:
    """The columns, and the key path inside the one column a path-valued edge names (SPEC 2.3.9)."""

    keys = entry.get(path)
    joined = _join_columns(entry.get(columns))

    return (
        f"{joined} (path {'.'.join(map(str, keys))})" if isinstance(keys, list) and keys else joined
    )


def _note_fact(entry: dict[str, Any]) -> list[Rendered]:
    note = entry.get("note")

    return [Rendered(f"note: {' '.join(note.split())}")] if isinstance(note, str) else []


def _referencing_columns(a: TableArtifacts) -> set[str]:
    refers_to, _ = _edges(a)

    return {
        column
        for entry in refers_to
        if not entry.get("path") and (column := _single_column(entry)) is not None
    }


def _referencing_null_share(
    a: TableArtifacts,
    entry: dict[str, Any],
    shares: dict[str, tuple[str, float]],
) -> tuple[str, float] | None:
    """The referencing endpoint's null share, spelled and raw; a path key's from its part (SPEC
    2.2.18): NULL where the column is null, the key absent or a JSON `null`; no part, no figure.
    """

    column = _single_column(entry)
    keys = entry.get("path")

    if not keys:
        return shares.get(column) if column is not None else None

    statistics = a.statistics or {}
    col = (statistics.get("columns") or {}).get(column)
    parts = column_value(col, "parts") if isinstance(col, dict) else None
    key_part = parts.get("".join(member(str(k)) for k in keys)) if isinstance(parts, dict) else None

    if not isinstance(col, dict) or not isinstance(key_part, dict):
        return None

    scope = scope_of(statistics)
    population = rows_scanned(col, scope)
    population = population if population is not None else block_value(statistics, "row_count")
    occurrences = column_value(key_part, "occurrences")
    key_nulls = column_value(key_part, "null_count")

    if not isinstance(occurrences, int) or not isinstance(population, int) or not population:
        return None

    null_rows = population - occurrences + (key_nulls if isinstance(key_nulls, int) else 0)
    rate = compute_null_rate(null_rows, population)

    return (spell_percent(rate) if null_rows else qualify("none", scope)), rate


def _single_column(entry: dict[str, Any]) -> str | None:
    columns = entry.get("column")

    return str(columns[0]) if isinstance(columns, list) and len(columns) == 1 else None


def _null_shares(a: TableArtifacts) -> dict[str, tuple[str, float]]:
    columns = (a.statistics or {}).get("columns")

    if not isinstance(columns, dict):
        return {}

    scope = scope_of(a.statistics)
    out = {}

    for name in _ordered_column_names(columns):
        col = columns[name]

        if isinstance(col, dict) and (share := notes_synthesis.null_share(col, scope)) is not None:
            out[name] = (share, column_value(col, "null_rate"))

    return out


def _nulls_fact(share: str) -> Rendered:
    keys = {"nulls", "over_the_rows_scanned"} if share.startswith("none ") else {"nulls"}

    return Rendered(f"nulls: {share}", frozenset(keys))


def _detection_terms(entry: dict[str, Any]) -> frozenset[str]:
    if edge_detection(entry) == "annotated":
        return frozenset({"annotated"})

    key = f"detection:{edge_detection(entry)}"

    return frozenset({key}) if key in TERMS else frozenset()


def _edges(a: TableArtifacts) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The table's `refers_to` and `referenced_by` edges, the surest first.

    Declared, then inferred, then measured (SPEC 2.3); the artifact's own order within a rank.
    """

    relationships = a.relationships or {}

    return _ranked(relationships.get("refers_to")), _ranked(relationships.get("referenced_by"))


def _ranked(edges: Any) -> list[dict[str, Any]]:
    listed = [e for e in edges or [] if isinstance(e, dict)]

    return sorted(
        listed,
        key=lambda e: _DETECTION_RANK.get(edge_detection(e), len(_DETECTION_RANK)),
    )


def _join_columns(columns: Any) -> str:
    """One column bare, a composite key in parentheses, so `a, b -> t.c, d` cannot be misread."""

    names = [str(c) for c in columns] if isinstance(columns, list) else []

    if not names:
        return "?"

    return names[0] if len(names) == 1 else "(" + ", ".join(names) + ")"


def _markdown_data_dictionary(a: TableArtifacts, options: AssemblyOptions) -> str:
    """The table's description and every column a human has defined (SPEC 2.7.1)."""

    description = a.description if options.include_description else None
    annotations = a.annotations or {}
    columns = (a.statistics or {}).get("columns")
    # A column whose lineage is producer-parsed carries no annotation entry of its own.
    parsed_only = [
        name
        for name in (_ordered_column_names(columns) if isinstance(columns, dict) else [])
        if name in a.inherited and name not in annotations
    ]
    notes = (
        [
            line
            for name in [*annotations, *parsed_only]
            for line in _dictionary_lines(
                name,
                annotations.get(name, {}),
                a.inherited.get(name, []),
            )
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


def _dictionary_lines(
    name: str,
    entry: dict[str, Any],
    inherited: list[dict[str, Any]],
) -> list[str]:
    lines = []
    note = entry.get("note")

    if isinstance(note, str) and note.strip():
        lines.append(f"- {name}: {' '.join(note.split())}")

    for source in inherited:
        facts = [" ".join(source["note"].split())] if source.get("note") else []
        facts += [
            f"{spell_literal(v['value'])} = {' '.join(v['note'].split())}"
            for v in source.get("values", [])
        ]
        lines.append(f"- {name} (from {source['source']}): {'; '.join(facts)}")

    return lines


def _markdown_column_values(a: TableArtifacts, joined: set[str]) -> list[Rendered]:
    """Every column whose list a predicate can rely on, then each nullable column's null share.

    A column on a row states its share in the Nulls cell; one in `joined`, on its Joins line.
    """

    columns = (a.statistics or {}).get("columns")

    if not isinstance(columns, dict):
        return []

    scope = scope_of(a.statistics)
    row_count = block_value(a.statistics or {}, "row_count")
    annotations = a.annotations or {}
    shares = _null_shares(a)
    rows: list[Rendered] = []

    for name in _ordered_column_names(columns):
        col = columns[name]
        listed = _covered_values(col)
        frequent = _frequent_values(col, scope, row_count) if listed is None else None

        if listed is not None:
            entries, coverage = listed
            shown, share = _shown_values(entries, coverage)
            statement = coverage_statement(coverage, scope)
            cue = _codes_undocumented(col, coverage, annotations.get(name), a.inherited.get(name))
        elif frequent is not None:
            shown, share = frequent
            statement, cue = _FREQUENT_STATEMENT, False
        else:
            continue

        values_cell = _values_cell(col, shown, annotations.get(name))
        nulls = shares.pop(name, ("",))[0]
        rows.append(
            Rendered(
                f"| {_escape_cell(name)} | {values_cell.text} | {spell_percent(share)} - {statement}"
                f"{f'; {_MEANINGS_UNDOCUMENTED}' if cue else ''} | {nulls} |",
                values_cell.terms
                | _scope_terms(statement)
                | _scope_terms(nulls)
                | ({"meanings"} if cue else set())
                | ({"frequent_values"} if frequent is not None else set()),
            ),
        )

    rest = [f"{name} ({share})" for name, (share, _) in shares.items() if name not in joined]

    if not rows and not rest:
        return []

    lines = [Rendered("## Column values")]

    if rows:
        head = "| Column | Values (count) | Coverage | Nulls |"
        lines += [
            Rendered(""),
            Rendered(head, frozenset({"nulls_column"})),
            Rendered("|---|---|---|---|"),
            *rows,
        ]

    if rest:
        line = f"Nulls: {notes_synthesis.LIST_SEPARATOR.join(rest)}"
        terms = {"nulls_column"} | _scope_terms(line)
        lines += [Rendered(""), Rendered(line, frozenset(terms))]

    return lines


def _scope_terms(text: str) -> frozenset[str]:
    return frozenset({"over_the_rows_scanned"}) if "over the rows scanned" in text else frozenset()


def _covered_values(col: Any) -> tuple[list[dict[str, Any]], float] | None:
    """The value entries and the coverage describing them; None without both (SPEC 2.2.3).

    A `numeric`/`temporal` list carries no coverage: a frequency sample, never a domain.
    """

    if not isinstance(col, dict):
        return None

    entries = column_value(col, "values")
    coverage = column_value(col, "values_coverage")

    if not isinstance(entries, list) or not entries or not _is_number(coverage):
        return None

    return [e for e in entries if isinstance(e, dict)], float(coverage)


def _frequent_values(
    col: Any,
    scope: ScanScope | None,
    row_count: Any,
) -> tuple[list[dict[str, Any]], float] | None:
    """Values holding `QUERY_FREQUENT_SHARE` of non-null rows and twice an even spread's share,
    at most `QUERY_SAMPLE_LIMIT`, with their joint share; a uniform column shows none.
    """

    if (
        not isinstance(col, dict)
        or column_value(col, "classification") not in _FREQUENCY_CLASSIFICATIONS
    ):
        return None

    entries = column_value(col, "values")
    cardinality = column_value(col, "cardinality")
    population = notes_synthesis.non_null_rows(
        col,
        row_count if isinstance(row_count, int) else None,
        scope,
    )

    if not isinstance(entries, list) or not population or not _is_number(cardinality):
        return None

    floor = max(QUERY_FREQUENT_SHARE, 2 / cardinality) if cardinality else QUERY_FREQUENT_SHARE
    shown = [e for e in entries if isinstance(e, dict) and _count_of(e) / population >= floor]
    shown = shown[:QUERY_SAMPLE_LIMIT]

    return (shown, sum(_count_of(e) for e in shown) / population) if shown else None


def _shown_values(
    entries: list[dict[str, Any]],
    coverage: float,
) -> tuple[list[dict[str, Any]], float]:
    """The entries `query` shows and the share of the column they cover.

    A sampled list is cut to `QUERY_SAMPLE_LIMIT` categories, a spelling group counting as one.
    """

    if coverage == 1.0:  # noqa: RUF069 - coverage 1.0 is the exhaustive sentinel
        return entries, coverage

    groups = grouped_values(entries)[:QUERY_SAMPLE_LIMIT]
    shown = [entry for canonical, members in groups for entry in (canonical, *members)]
    listed = sum(_count_of(e) for e in entries)
    kept = sum(_count_of(e) for e in shown)

    return shown, coverage * kept / listed if listed else coverage


def _count_of(entry: dict[str, Any]) -> int:
    """An entry's count; 0 where the artifact carries no integer."""

    count = entry.get("count")

    return count if isinstance(count, int) and not isinstance(count, bool) else 0


def _is_number(value: Any) -> bool:
    """A real number - `bool` is an `int` to Python and never a coverage."""

    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _codes_undocumented(
    col: Any,
    coverage: float,
    annotation: dict[str, Any] | None,
    inherited: list[dict[str, Any]] | None,
) -> bool:
    return (
        coverage == 1.0  # noqa: RUF069 - coverage 1.0 is the exhaustive sentinel
        and column_value(col, "classification") in _CODED_CLASSIFICATIONS
        and not column_value(col, "redacted")
        and not _has_meaning(annotation)
        and not inherited
    )


def _meanings_documented(a: TableArtifacts, purpose: Purpose) -> bool:
    if (a.description and a.description.strip()) or (purpose == "query" and a.inherited):
        return True

    return any(_has_meaning(entry) for entry in (a.annotations or {}).values())


def _has_meaning(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False

    note = entry.get("note")

    return (isinstance(note, str) and bool(note.strip())) or bool(value_notes(entry))


def _values_cell(
    col: dict[str, Any],
    entries: list[Any],
    annotation: dict[str, Any] | None,
) -> Rendered:
    """One column's listed values; a redacted column publishes its counts and no literal."""

    redaction = column_value(col, "redacted")
    counts = [entry.get("count") for entry in entries if isinstance(entry, dict)]

    if isinstance(redaction, str) and redaction:
        withheld = ", ".join(f"withheld ({c})" for c in counts)
        keys = {"redacted", "values", "withheld", f"redaction:{redaction}"}

        return Rendered(f"redacted: {redaction}; values: {withheld}", frozenset(keys & set(TERMS)))

    notes = value_notes(annotation)
    cells = []

    for entry, members in grouped_values(entries):
        value = entry.get("value")
        counted = [entry, *members]
        total = sum(e.get("count") or 0 for e in counted)
        cell = f"{_escape_cell(spell_literal(value))} ({total})"

        if members:
            spellings = ", ".join(
                f"{_escape_cell(spell_literal(e.get('value')))} ({e.get('count')})" for e in counted
            )
            cell += f" {{{spellings}}}"

        note = notes.get(value_key(value))

        if note:
            cell += f" = {_escape_cell(spell_literal(note))}"

        cells.append(cell)

    return Rendered(", ".join(cells))


def _escape_cell(text: str) -> str:
    """Make `text` safe to interpolate into one Markdown table cell.

    Order is load-bearing: escaping the pipe first leaves a live delimiter behind a backslash.
    """

    escaped = text.replace("\\", "\\\\").replace("|", "\\|")

    return escaped.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def _identity_lines(a: TableArtifacts, adapter: str | None, purpose: Purpose) -> list[Rendered]:
    """What every header opens with: the table, its dialect, what is missing, what is unexplained.

    The adapter and missing-artifact lines are unconditional (SPEC 2.5).
    """

    parts = []

    if a.row_count is not None:
        parts.append(f"{spell_number(a.row_count)} rows")
    parts.append(f"{a.column_count} columns")

    lines = [f"# Table: {a.fqn}  ({', '.join(parts)})"]

    if a.last_run_failed:
        lines.append(f"Unprofiled: {unprofiled_message(a.fqn)} - this print is an earlier run's")

    if adapter:
        lines.append(f"Adapter: {adapter}")

    if a.missing:
        lines.append(_missing_summary(a.missing))

    if a.corrupted:
        lines.append(_corrupted_summary(a.corrupted))

    rendered = [Rendered(line) for line in lines]

    if not _meanings_documented(a, purpose):
        rendered.append(
            Rendered(
                "Meanings: not documented here - read codes, units and defaults from the code",
                frozenset({"meanings_table"}),
            ),
        )

    return rendered


def _markdown_header(
    a: TableArtifacts,
    include_qualifiers: bool,
    connection_profile: dict[str, Any],
    adapter: str | None,
) -> list[Rendered]:
    """The identity lines, then one line per table-level qualifier that applies."""

    lines = _identity_lines(a, adapter, "profile")

    if not include_qualifiers:
        return lines

    statistics = a.statistics or {}
    qualifiers = (
        _scope_summary(statistics),
        Rendered(external_line(statistics)),
        Rendered(_merging_summary(statistics)),
        _grain_summary(statistics, a.annotated_grain),
        _timeline_summary(statistics),
        _disabled_summary(statistics),
        Rendered(_depends_on_summary(statistics)),
        Rendered(
            _profile_override_summary(
                connection_profile,
                a.profile_override,
            ),
        ),
    )
    lines.extend(line for line in qualifiers if line.text)

    return lines


def external_line(statistics: dict[str, Any]) -> str:
    """The line an object whose rows live in another system earns (SPEC 2.2.20)."""

    if block_value(statistics, "external") is not True:
        return ""

    read = (
        "not queried by dbprint"
        if block_value(statistics, "catalog_only") is True
        else "statistics were read through it"
    )

    return (
        f"External: rows live outside this database - {read}; "
        "every query against it reads the other system"
    )


def _merging_summary(statistics: dict[str, Any]) -> str:
    """The one line a merging engine earns (SPEC 2.2.19): which rows the counts are, and FINAL."""

    block = block_value(statistics, "merging")

    if not isinstance(block, dict):
        return ""

    key = ", ".join(k.get("expression", "") for k in block.get("key") or [])
    head = f"Merging: {block.get('engine')} on ({key})"

    if block.get("rows") == "merged":
        return f"{head} - statistics were read with FINAL; queries without FINAL see more rows"

    if block.get("one_row_per_key"):
        grouped = f", or GROUP BY {key}," if key else ""

        return (
            f"{head} - rows counted here may repeat a key until merged; "
            f"query with FINAL{grouped} for one row per key"
        )

    return (
        f"{head} - rows counted here include state and cancel rows not yet collapsed; "
        "query with FINAL for the collapsed rows"
    )


def _profile_override_summary(
    connection_profile: dict[str, Any],
    table_override: dict[str, Any] | None,
) -> str:
    """Stated so a table-level override is never applied silently."""

    if not table_override:
        return ""

    own = _profile_leaves(table_readings.effective_profile(connection_profile, table_override))
    base = _profile_leaves(connection_profile)
    differing = {path: value for path, value in own.items() if base.get(path) != value}

    if not differing:
        return ""

    rendered = ", ".join(
        f"{path}={_spell_setting(value)}" for path, value in sorted(differing.items())
    )

    return f"Profile override: {rendered}"


def _profile_leaves(tree: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}

    for key, value in tree.items():
        if isinstance(value, dict):
            out.update(_profile_leaves(value, f"{prefix}{key}."))
        else:
            out[f"{prefix}{key}"] = value

    return out


def _spell_setting(value: Any) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def _missing_summary(missing: tuple[str, ...]) -> str:
    """One line naming every declared kind whose file is absent from disk (SPEC 2.5)."""

    return f"Missing: {', '.join(missing)} (declared but missing from disk)"


def _corrupted_summary(corrupted: dict[str, str]) -> str:
    """One line naming every declared kind present on disk but unreadable (SPEC 2.5).

    `corrupted` maps kind to why: the structured payload carries the reason, this prose line
    only the kinds.
    """

    return f"Unreadable: {', '.join(corrupted)} (present on disk, malformed)"


def _scope_summary(statistics: dict[str, Any]) -> Rendered:
    scope = scope_of(statistics)

    if scope is None:
        return Rendered("")

    narrowed = (
        {"sampled"}
        if scope.sample is not None
        else {"filtered_by"}
        if scope.filter is not None and scope.filter.strip()
        else set()
    )

    return Rendered(scope_line(scope), frozenset({"scanned", *narrowed}))


def _grain_summary(
    statistics: dict[str, Any],
    annotated_grain: dict[str, Any] | None,
) -> Rendered:
    """What identifies a row, one line - a table-level fact, not a per-column cell (SPEC 2.2.12).

    A human-authored key (SPEC 2.7.1) rides the same list tagged `annotated`; it adds a fact,
    never replaces the producer's measurement.
    """

    reading = grain_reading(statistics, annotated_grain)

    grain = frozenset({"grain"})

    if reading.state == "exhausted":
        return Rendered("Grain: searched, none found", grain)
    elif reading.state == "bounded":
        return Rendered("Grain: search bounded, none found within the cap", grain)
    elif reading.state == "not_determined":
        return Rendered("Grain: not determined", grain)

    rendered = "; ".join(
        f"({', '.join(key.get('columns') or [])}) {key.get('detection')}"
        + (f" - {spell_literal(key['note'])}" if key.get("note") else "")
        for key in reading.keys
    )
    detections = {
        "annotated" if key.get("detection") == "annotated" else f"detection:{key.get('detection')}"
        for key in reading.keys
    }

    return Rendered(f"Grain: {rendered}", grain | {d for d in detections if d in TERMS})


def _timeline_summary(statistics: dict[str, Any]) -> Rendered:
    """The anchor column's bucketed activity, one line (SPEC 2.2.16) - anchor, unit, bucket
    count and span, enough to judge recency and gaps without the full column list.
    """

    block = block_value(statistics, "timeline")

    if not block:
        return Rendered("")

    column, unit = block.get("column"), block.get("unit")
    buckets = [b for b in (block.get("buckets") or []) if isinstance(b, dict)]
    head = f"Timeline: {column} ({unit}); buckets: {len(buckets)}"

    if not buckets:
        return Rendered(head, frozenset({"timeline", "buckets"}))

    first, last = spell_literal(buckets[0].get("start")), spell_literal(buckets[-1].get("start"))
    share = table_readings.scanned_row_share(block.get("coverage"))
    covered = f"; covers: {share}" if share else ""
    keys = {"timeline", "buckets", "bucket_starts", *(["covers"] if share else [])}

    return Rendered(f"{head}; bucket starts: {first} -> {last}{covered}", frozenset(keys))


def _depends_on_summary(statistics: dict[str, Any]) -> str:
    """What a view/matview reads, one line (SPEC 2.2.17) - absent when the producer could not
    ask, so this renders nothing rather than guess; a plain table never carries the field.
    """

    block = block_value(statistics, "depends_on")

    if not isinstance(block, list):
        return ""

    names = [t for t in block if isinstance(t, str)]

    if not names:
        return "Depends on: nothing else in this print"

    return f"Depends on: {', '.join(names)}"


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

        lines.append(f"- Selectors applied to this print: {'; '.join(parts)}")

    redaction_count = manifest.get("redaction_rules_configured")

    if (
        isinstance(redaction_count, int)
        and not isinstance(redaction_count, bool)
        and redaction_count
    ):
        rule_word = "rule" if redaction_count == 1 else "rules"
        lines.append(f"- Redaction configured: {redaction_count} {rule_word}")

    percentiles = (table_readings.connection_profile(manifest).get("statistics") or {}).get(
        "percentiles",
    )

    if isinstance(percentiles, list) and percentiles:
        rendered = ", ".join(f"P{p}" for p in percentiles)
        lines.append(f"- Percentiles configured: {rendered}")

    return "## Provenance\n\n" + "\n".join(lines) if lines else ""


def _markdown_connection_notes(a: TableArtifacts) -> str:
    return f"## Connection notes\n\n{a.connection_notes}"


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
                    spelled = spell_value(value)
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


def _markdown_cardinality_table(
    a: TableArtifacts,
    profile: dict[str, Any],
) -> list[Rendered]:
    assert a.statistics is not None
    columns = a.statistics.get("columns") or {}
    row_count = block_value(a.statistics, "row_count")
    scope = scope_of(a.statistics)
    fk_targets = fk_target_map(a.relationships)

    lines = [
        Rendered("## Cardinality & key columns"),
        Rendered(""),
        Rendered("| Column | Cardinality | Notes |"),
        Rendered("|---|---|---|"),
    ]

    ordered = _ordered_column_names(columns)

    for name in ordered:
        col = columns[name]
        cardinality = _format_cardinality_cell(col, row_count, scope)
        notes = notes_synthesis.synthesize(
            col,
            fk_targets.get(name),
            profile=profile,
            scope=scope,
            row_count=row_count,
        )
        lines.append(
            Rendered(
                f"| {_escape_cell(name)} | {_escape_cell(cardinality)} | {_escape_cell(notes.text)} |",
                notes.terms,
            ),
        )

    return lines


def _markdown_parts(a: TableArtifacts, options: AssemblyOptions) -> list[Rendered]:
    """Each column's parts (SPEC 2.2.18), one row per part labelled `<column><path>`."""

    del options
    assert a.statistics is not None
    columns = a.statistics.get("columns") or {}
    row_count = block_value(a.statistics, "row_count")
    scope = scope_of(a.statistics)
    rows: list[Rendered] = []

    for name in _ordered_column_names(columns):
        col = columns[name]
        parts = column_value(col, "parts")

        if not isinstance(parts, dict):
            continue

        counted = rows_scanned(col, scope)
        scanned = counted if counted is not None else row_count

        for path, block in parts.items():
            if not isinstance(block, dict):
                continue

            notes = notes_synthesis.synthesize(block, None, profile={}, scope=scope)

            if present := _presence(name, path, block, parts, scanned, scoped=counted is not None):
                notes = notes_synthesis.join_facts(
                    [notes, Rendered(present, frozenset({"present"}))],
                )

            cardinality = _format_cardinality_cell(block, None)
            rows.append(
                Rendered(
                    f"| {_escape_cell(display(name, path))} | {_escape_cell(cardinality)} "
                    f"| {_escape_cell(notes.text)} |",
                    notes.terms,
                ),
            )

    if not rows:
        return []

    head = ["## Column parts", "", "| Part | Cardinality | Notes |", "|---|---|---|"]

    return [*(Rendered(line) for line in head), *rows]


def _presence(
    column: str,
    path: str,
    block: dict[str, Any],
    parts: dict[str, Any],
    scanned: int | None,
    *,
    scoped: bool,
) -> str | None:
    """A member's presence over its population: rows for a top-level member, else parent instances.

    The row share sits beside it under no array or map step, named `rows scanned` on a scoped read.
    """

    rows = "rows scanned" if scoped else "rows"

    steps = parse(path)
    occurrences = column_value(block, "occurrences")

    if steps[-1].kind != "member" or not isinstance(occurrences, int):
        return None

    up = parent(path)

    if up is None:
        return f"present: {spell_percent(occurrences / scanned)} of {rows}" if scanned else None

    holder = parts.get(up)

    if not isinstance(holder, dict):
        return None

    population = (column_value(holder, "occurrences") or 0) - (
        column_value(holder, "null_count") or 0
    )

    if not population:
        return None

    text = f"present: {spell_percent(occurrences / population)} of {display(column, up)}"

    if scanned and all(step.kind == "member" for step in steps):
        text += f" ({spell_percent(occurrences / scanned)} of {rows})"

    return text


def _markdown_unmeasured(a: TableArtifacts) -> str:
    """Name each lost table-level block (SPEC 2.2.1): the reading guide reads an absent one as none."""

    assert a.statistics is not None
    named = block_value(a.statistics, "unmeasured") or []
    lines = [
        f"- `{name}`{f' ({_BLOCK_LABEL[name]})' if name in _BLOCK_LABEL else ''}: "
        f"{unmeasured_block_message(name)}"
        for name in named
        if isinstance(name, str)
    ]

    return "\n".join(["## Blocks in the file's `unmeasured` list", "", *lines]) if lines else ""


def _markdown_disabled(a: TableArtifacts) -> str:
    """Name each switched-off measurement (SPEC 2.2.21), so its absence never reads as none."""

    assert a.statistics is not None
    lines = [
        f"- `{name}` ({_DISABLED_LABEL[name]}): {disabled_feature_message(name)}"
        for name in sorted(disabled_of(a.statistics))
    ]

    return "\n".join(["## Measurements in the file's `disabled` list", "", *lines]) if lines else ""


def _disabled_summary(statistics: dict[str, Any]) -> Rendered:
    if not (named := disabled_of(statistics)):
        return Rendered("")

    labels = ", ".join(_DISABLED_LABEL[name] for name in sorted(named))

    return Rendered(f"Not measured: switched off - {labels}", frozenset({"switched_off"}))


def _markdown_physical_layout(a: TableArtifacts) -> str:
    """The declared clustering/partitioning key - a schema fact, never a claim about pruning."""

    assert a.statistics is not None
    layout = table_readings.physical_layout(a.statistics) or table_readings.LayoutReading(None, [])
    labels = {"cluster": "Clustered by", "partition": "Partitioned by", "sort": "Sorted by"}
    label = labels.get(layout.mechanism, "Partitioned by")
    expressions = ", ".join(k.get("expression", "") for k in layout.keys)

    return f"## Physical layout\n\n{label}: {expressions}"


def _markdown_null_patterns(a: TableArtifacts) -> list[Rendered]:
    """Which columns are null on the same rows, as shares of the scanned rows.

    Worded as an observation (SPEC 2.2.10); the footer covers exactly the combinations shown.
    """

    assert a.statistics is not None
    block = block_value(a.statistics, "null_patterns") or {}
    patterns = [p for p in (block.get("patterns") or []) if isinstance(p, dict)]
    shown = patterns[:NULL_PATTERN_DISPLAY_LIMIT]
    scanned = _null_pattern_population(a.statistics, block, patterns)
    lines = [
        "## Columns null on the same rows",
        "",
        "| Rows | Null together |",
        "|---|---|",
    ]

    for entry in shown:
        names = ", ".join(entry.get("columns") or []) or "(none - fully populated)"
        count = int(entry.get("count") or 0)
        rows = spell_percent(count / scanned) if scanned else spell_number(count)
        lines.append(f"| {rows} | {_escape_cell(names)} |")

    if not scanned:
        return [Rendered(line) for line in lines]

    every = len(shown) == len(patterns) and block.get("coverage") == 1.0  # noqa: RUF069 - coverage 1.0 is the exhaustive sentinel
    held = sum(int(entry.get("count") or 0) for entry in shown) / scanned
    covered = "every scanned row" if every else table_readings.scanned_row_share(held)
    # Silent on `measured` - matches the per-column coverage hedge (notes_synthesis.py).
    hedge = " (bounded)" if block.get("coverage_method") == "bounded" else ""
    footer = Rendered(
        f"Shown combinations cover {covered}{hedge}.",
        frozenset({"shown_combinations"}),
    )

    return [*(Rendered(line) for line in [*lines, ""]), footer]


def _null_pattern_population(
    statistics: dict[str, Any],
    block: dict[str, Any],
    patterns: list[dict[str, Any]],
) -> int | None:
    """The scanned rows the shares are of: the scope's, the table's, else derived from coverage."""

    scope = scope_of(statistics)
    scanned = scope.rows_scanned if scope is not None else block_value(statistics, "row_count")

    if isinstance(scanned, int) and not isinstance(scanned, bool) and scanned > 0:
        return scanned

    coverage = block.get("coverage")
    listed = sum(int(entry.get("count") or 0) for entry in patterns)

    if isinstance(coverage, int | float) and not isinstance(coverage, bool) and coverage > 0:
        return round(listed / coverage) or None

    return None


def _markdown_relationships(a: TableArtifacts) -> list[Rendered]:
    assert a.relationships is not None
    lines = [Rendered("## Relationships")]
    refers_to, referenced_by = _edges(a)

    if not refers_to and not referenced_by:
        # `eligible_target: false` (SPEC 2.3.8) says nothing COULD reference this object, not
        # merely that nothing does - a bare "(none)" collapses that into the weaker claim.
        if a.relationships.get("eligible_target") is False:
            lines.append(Rendered(f"- (none - {_NOT_A_JOIN_TARGET})"))
        else:
            lines.append(Rendered("- (none)"))

        return lines

    for entry in refers_to:
        via = _endpoint(entry, "column", "path")
        lines.append(
            Rendered(
                f"- -> {_target(entry)} ({edge_detection(entry)}); via: {via}{_on_delete(entry)}",
                _detection_terms(entry) | {"via"} | _on_delete_terms(entry),
            ),
        )
        lines.extend(_note_line(entry))
        lines.extend(_observed_lines(entry, _own_cardinality(a, entry.get("column"))))

    for entry in referenced_by:
        lines.append(
            Rendered(
                f"- <- {_referencer(entry)} ({edge_detection(entry)}){_on_delete(entry)}",
                _detection_terms(entry) | _on_delete_terms(entry),
            ),
        )
        lines.extend(_note_line(entry))
        referencer = (entry.get("referencer_table"), _join_columns(entry.get("referencer_column")))
        lines.extend(_observed_lines(entry, a.incoming_cardinalities.get(referencer)))

    return lines


def _note_line(entry: dict[str, Any]) -> list[Rendered]:
    note = entry.get("note")

    return [Rendered(f"  note: {' '.join(note.split())}")] if isinstance(note, str) else []


def _own_cardinality(a: TableArtifacts, columns: Any) -> int | None:
    names = columns if isinstance(columns, list) else []
    column = (a.statistics or {}).get("columns", {}).get(names[0]) if len(names) == 1 else None
    cardinality = column_value(column, "cardinality") if isinstance(column, dict) else None

    return cardinality if isinstance(cardinality, int) else None


def _on_delete(entry: dict[str, Any]) -> str:
    """Absent on an inferred edge (SPEC 2.3.8) - never invent a referential action."""

    on_delete = entry.get("on_delete")

    return f"; on delete: {on_delete}" if on_delete is not None else ""


def _on_delete_terms(entry: dict[str, Any]) -> frozenset[str]:
    return frozenset({"on_delete"}) if entry.get("on_delete") is not None else frozenset()


def _observed_lines(entry: dict[str, Any], child_distinct: int | None) -> list[Rendered]:
    """SPEC 2.3.10: what joining across this edge costs, beside its declared shape.

    `child_distinct` is the referencing column's own distinct count: comparing all of it is exact.
    """

    observed = entry.get("observed")

    if not isinstance(observed, dict):
        return []

    if observed.get("scope_compatible") is False:
        return [Rendered("  not measured (one side was read in part)", frozenset({"not_measured"}))]

    fanout_avg = observed.get("fanout_avg")
    target_coverage = observed.get("target_coverage")

    if fanout_avg is None or target_coverage is None:
        return []

    facts = [f"fanout avg: {spell_number(fanout_avg)}"]
    keys = {"fanout_avg", "covers"}
    fanout_max = observed.get("fanout_max")

    if fanout_max is not None:
        facts.append(f"fanout max: {spell_number(fanout_max)}")
        keys.add("fanout_max")

    facts.append(f"covers: {spell_percent(target_coverage)} of target values")
    containment = observed.get("containment")

    if containment is not None:
        contained = f"contained: {spell_percent(containment)} of referencing values"
        compared = observed.get("answerable_count")

        if isinstance(compared, int) and not isinstance(compared, bool) and compared > 0:
            if compared == child_distinct:
                contained += " (exact)"
            else:
                margin = spell_percent(1 / math.sqrt(compared))
                contained += f" (\u00b1{margin}, {spell_number(compared)} compared)"

        facts.append(contained)
        keys.add("contained")

    if observed.get("coherent") is False:
        facts.append("incoherent (more distinct referencing values than target values)")
        keys.add("incoherent")

    return [Rendered("  " + "; ".join(facts), frozenset(keys))]


def _format_cardinality_cell(
    col: dict[str, Any],
    row_count: int | None,
    scope: ScanScope | None = None,
) -> str:
    """The distinct count, whether it saturates the set it was measured over, and how counted.

    A scoped column counts distinct over `rows_scanned` (SPEC 2.2.8), so the cue names which
    population it compared. Neither fires at zero: a read that matched no rows saturates
    nothing. `cardinality_method: approximate` marks an estimate; exact is unmarked.
    """

    cardinality = column_value(col, "cardinality")

    if cardinality is None:
        return "n/a"

    text = spell_number(cardinality)
    saturated = table_readings.saturation(col, row_count, scope)

    if saturated is not None:
        text += " (= scanned rows)" if saturated == "scanned_rows" else " (= row count)"

    if column_value(col, "cardinality_method") == "approximate":
        text += " (approx)"

    normalized = column_value(col, "normalized_cardinality")

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
    "binary": 6,
    "composite": 7,
    "spatial": 8,
    "vector": 9,
    "json": 10,
    "unsupported": 11,
}


def _ordered_column_names(columns: dict[str, Any]) -> list[str]:
    """Stable ordering for the cardinality table: `_COLUMN_ORDER_PRIORITY`, then YAML order."""

    def key(name_col: tuple[str, dict[str, Any]]) -> tuple[int, int]:
        name, col = name_col
        classification = col.get("classification", "unsupported")
        priority = _COLUMN_ORDER_PRIORITY.get(classification, len(_COLUMN_ORDER_PRIORITY))

        return priority, list(columns.keys()).index(name)

    return [n for n, _ in sorted(columns.items(), key=key)]


def _load_artifact(
    path: Path,
    read: ArtifactReader,
) -> tuple[dict[str, Any] | None, str | None]:
    """`(None, None)` covers both "never declared" and "declared but missing"; a non-`None` reason
    is a declared file that exists and failed to parse, naming why.
    """

    if not path.is_file():
        return None, None

    try:
        data = read(path)
    except yaml.YAMLError as exc:
        return None, str(exc)

    if isinstance(data, dict):
        return data, None

    return None, "parses, but is not a mapping"


def load_annotations(
    path: Path,
    kind: str,
    read: ArtifactReader,
) -> tuple[dict[str, Any] | None, str | None]:
    """`_load_artifact`, withholding a body that fails admission (SPEC 2.7) with its reason."""

    data, reason = _load_artifact(path, read)

    if data is not None and (problem := authored.malformation(kind, data)) is not None:
        return None, f"does not match its schema (SPEC 2.7): {problem}"

    return authored.admitted(kind, data) if data is not None else None, reason


def load_connection_notes(
    print_root: Path,
    read: ArtifactReader,
) -> tuple[str | None, str | None]:
    """`manifest.annotations.yaml`'s `notes` field (SPEC 2.7.3) and why it is corrupt, if it is -
    `(None, None)` covers absent and empty alike; a non-`None` reason is present but unreadable.
    """

    path = print_root / MANIFEST_ANNOTATIONS_FILENAME

    if path.is_file() and not contained(print_root, path):
        return None, None

    mapping, reason = load_annotations(path, "manifest_annotations", read)

    if mapping is None:
        return None, reason

    notes = mapping.get("notes")

    return (notes.strip() if isinstance(notes, str) and notes.strip() else None), None


def _connection_keys(
    print_root: Path,
    options: AssemblyOptions,
    read: ArtifactReader,
) -> dict[str, Any]:
    """The structured counterpart of the Markdown header's notes block (SPEC 2.7.3)."""

    notes, reason = load_connection_notes(print_root, read)
    keys: dict[str, Any] = {}

    if notes and options.include_annotations:
        keys["connection_notes"] = notes

    if reason is not None:
        keys["_corrupted"] = {"manifest_annotations": reason}

    return keys


def _attach_connection_notes(a: TableArtifacts, print_root: Path, read: ArtifactReader) -> None:
    notes, reason = load_connection_notes(print_root, read)
    a.connection_notes = notes

    if reason is not None:
        a.corrupted["manifest_annotations"] = reason


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


def _load_table_artifacts(
    manifest: dict[str, Any],
    print_root: Path,
    fqn: str,
    read: ArtifactReader,
) -> TableArtifacts:
    """Read every available per-table artifact off disk; tolerate missing optional pieces."""

    entry = walkable_tables(manifest).get(fqn) or {}
    table_path = table_directory(print_root, fqn, entry)
    artifacts = declared_artifacts(entry)

    ddl_path = table_path / artifacts["ddl"] if "ddl" in artifacts else None
    ddl = ddl_path.read_text(encoding="utf-8") if ddl_path and ddl_path.is_file() else ""

    statistics = None
    corrupted: dict[str, str] = {}

    if "statistics" in artifacts:
        statistics, stats_reason = _load_artifact(table_path / artifacts["statistics"], read)

        if stats_reason is not None:
            corrupted["statistics"] = stats_reason

    relationships = None

    if "relationships" in artifacts:
        relationships, rel_reason = _load_artifact(table_path / artifacts["relationships"], read)

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
        stats_ann, stats_ann_reason = load_annotations(
            table_path / artifacts["statistics_annotations"],
            "statistics_annotations",
            read,
        )

        if stats_ann_reason is not None:
            corrupted["statistics_annotations"] = stats_ann_reason

        annotations = table_readings.live_annotations(_annotation_columns(stats_ann), statistics)
        annotated_grain = _annotated_grain(stats_ann)

    relationship_annotations = None

    if "relationships_annotations" in artifacts:
        rel_ann, rel_ann_reason = load_annotations(
            table_path / artifacts["relationships_annotations"],
            "relationships_annotations",
            read,
        )

        if rel_ann_reason is not None:
            corrupted["relationships_annotations"] = rel_ann_reason

        relationship_annotations = _relationship_annotation_entries(rel_ann)

    table_profile = entry.get("profile")
    own_rejections = rejected_edges(relationship_annotations, _by_edge_key(relationships))
    incoming, _ = incoming_rejections(manifest, print_root, fqn, relationships, read)
    authored, _ = authored_relationship_annotations(manifest, print_root, read)
    shown = with_human_edges(
        withhold_rejected(relationships, own_rejections, incoming, fqn),
        authored,
        fqn,
    )

    if shown is not relationships and relationship_annotations is not None:
        kept = {edge_key(e) for e in (shown or {}).get("refers_to") or [] if isinstance(e, dict)}
        withheld = {
            edge_key(e)
            for e in (relationships or {}).get("refers_to") or []
            if isinstance(e, dict) and edge_key(e) not in kept
        }
        relationship_annotations = [
            e for e in relationship_annotations if edge_key(e) not in withheld
        ]

    return TableArtifacts(
        fqn=fqn,
        table_type=entry.get("type", "table"),
        row_count=entry.get("row_count"),
        column_count=int(entry.get("columns") or 0),
        ddl=ddl,
        statistics=statistics,
        relationships=shown,
        description=description,
        annotations=annotations,
        annotated_grain=annotated_grain,
        relationship_annotations=relationship_annotations,
        missing=missing_artifacts(table_path, artifacts),
        corrupted=corrupted,
        profile_override=table_profile if isinstance(table_profile, dict) else None,
        last_run_failed=fqn in failed_tables(manifest),
        incoming_cardinalities=_incoming_cardinalities(manifest, print_root, relationships, read),
        inherited=_inherited_notes(manifest, print_root, annotations, statistics, read),
    )


def _inherited_notes(
    manifest: dict[str, Any],
    print_root: Path,
    annotations: dict[str, dict[str, Any]] | None,
    statistics: dict[str, Any] | None,
    read: ArtifactReader,
) -> dict[str, list[dict[str, Any]]]:
    """Each column's `derived_from` sources that carry a note or value notes, one step deep.

    An annotation's `derived_from` replaces the producer's parsed list for its column, even empty.
    """

    entries = walkable_tables(manifest)
    parsed: dict[str, dict[str, dict[str, Any]]] = {}
    out: dict[str, list[dict[str, Any]]] = {}
    columns = (statistics or {}).get("columns")
    lineage = {
        name: column_value(col, "derived_from")
        for name, col in (columns.items() if isinstance(columns, dict) else [])
        if isinstance(col, dict) and column_value(col, "derived_from") is not None
    }
    lineage |= {
        name: entry["derived_from"]
        for name, entry in (annotations or {}).items()
        if "derived_from" in entry
    }

    for name, sources in lineage.items():
        for source in sources if isinstance(sources, list) else []:
            table = source.get("table") if isinstance(source, dict) else None
            column = source.get("column") if isinstance(source, dict) else None

            if not isinstance(table, str) or not isinstance(column, str) or table not in entries:
                continue

            if table not in parsed:
                parsed[table] = _live_source_annotations(print_root, table, entries[table], read)

            noted = parsed[table].get(column)

            if not isinstance(noted, dict) or not _has_meaning(noted):
                continue

            note = noted.get("note")
            values = [
                {"value": v.get("value"), "note": v["note"]}
                for v in noted.get("values") or []
                if isinstance(v, dict) and isinstance(v.get("note"), str) and v["note"].strip()
            ]
            out.setdefault(name, []).append(
                {
                    "source": f"{table}.{column}",
                    **({"note": note} if isinstance(note, str) and note.strip() else {}),
                    **({"values": values} if values else {}),
                },
            )

    return out


def _live_source_annotations(
    print_root: Path,
    table: str,
    entry: dict[str, Any],
    read: ArtifactReader,
) -> dict[str, dict[str, Any]]:
    artifacts = declared_artifacts(entry)
    directory = table_directory(print_root, table, entry)

    if "statistics_annotations" not in artifacts:
        return {}

    annotated = _annotation_columns(
        load_annotations(
            directory / artifacts["statistics_annotations"],
            "statistics_annotations",
            read,
        )[0],
    )
    statistics = (
        _load_artifact(directory / artifacts["statistics"], read)[0]
        if "statistics" in artifacts
        else None
    )

    return table_readings.live_annotations(annotated, statistics)


def _incoming_cardinalities(
    manifest: dict[str, Any],
    print_root: Path,
    relationships: dict[str, Any] | None,
    read: ArtifactReader,
) -> dict[tuple[Any, str], int]:
    """The referencing column's distinct count behind each compared incoming edge.

    A referencer missing or unreadable leaves its edges out.
    """

    entries = walkable_tables(manifest)
    out: dict[tuple[Any, str], int] = {}
    parsed: dict[str, dict[str, Any] | None] = {}

    for edge in (relationships or {}).get("referenced_by") or []:
        observed = edge.get("observed") if isinstance(edge, dict) else None
        referencer = edge.get("referencer_table") if isinstance(edge, dict) else None
        columns = edge.get("referencer_column") if isinstance(edge, dict) else None

        if not isinstance(observed, dict) or "answerable_count" not in observed:
            continue

        if referencer not in entries or not isinstance(columns, list) or len(columns) != 1:
            continue

        if referencer not in parsed:
            name = declared_artifacts(entries[referencer]).get("statistics")
            path = table_directory(print_root, referencer, entries[referencer]) / (name or "")
            parsed[referencer] = _load_artifact(path, read)[0] if name else None

        column = ((parsed[referencer] or {}).get("columns") or {}).get(columns[0])
        cardinality = column_value(column, "cardinality") if isinstance(column, dict) else None

        if isinstance(cardinality, int):
            out[(referencer, _join_columns(columns))] = cardinality

    return out


def incoming_rejections(
    manifest: dict[str, Any],
    print_root: Path,
    fqn: str,
    relationships: dict[str, Any] | None,
    read: ArtifactReader,
) -> tuple[dict[tuple[Any, ...], dict[str, Any]], tuple[Path, ...]]:
    """Each referencer's rejections of an edge into `fqn`, keyed `(referencer, *incoming_key)`.

    Also the annotation files read - only the referencer authors a rejection (SPEC 2.7.2).
    """

    entries = walkable_tables(manifest)
    referencers = {
        edge.get("referencer_table")
        for edge in (relationships or {}).get("referenced_by") or []
        if isinstance(edge, dict)
    }
    out: dict[tuple[Any, ...], dict[str, Any]] = {}
    consulted: list[Path] = []

    for referencer in sorted(r for r in referencers if isinstance(r, str) and r in entries):
        entry = entries[referencer]
        name = declared_artifacts(entry).get("relationships_annotations")

        if name is None:
            continue

        path = table_directory(print_root, referencer, entry) / name
        consulted.append(path)
        annotations, _ = load_annotations(path, "relationships_annotations", read)

        rows = {
            incoming_key(edge, fqn): edge
            for edge in (relationships or {}).get("referenced_by") or []
            if isinstance(edge, dict) and edge.get("referencer_table") == referencer
        }
        annotated = _relationship_annotation_entries(annotations)

        for key, rejection in rejected_edges(annotated, rows).items():
            if key[1] == fqn:
                out[(referencer, *key)] = rejection

    return out, tuple(consulted)


def authored_relationship_annotations(
    manifest: dict[str, Any],
    print_root: Path,
    read: ArtifactReader,
) -> tuple[dict[str, list[dict[str, Any]] | None], tuple[Path, ...]]:
    """Every table's `relationships.annotations.yaml` `refers_to` by FQN, and the files read.

    Every table's: a human-authored edge leaves no `referenced_by` row naming whose file to read.
    """

    out: dict[str, list[dict[str, Any]] | None] = {}
    consulted: list[Path] = []

    for table, entry in sorted(walkable_tables(manifest).items()):
        name = declared_artifacts(entry).get("relationships_annotations")

        if name is None:
            continue

        path = table_directory(print_root, table, entry) / name
        consulted.append(path)
        out[table] = _relationship_annotation_entries(
            load_annotations(path, "relationships_annotations", read)[0],
        )

    return out, tuple(consulted)


def assemble_structured(
    manifest: dict[str, Any],
    print_root: Path,
    table: str,
    options: AssemblyOptions,
    read: ArtifactReader = read_artifact,
) -> dict[str, Any]:
    """The single-table structured object `format: json` / `format: yaml` describe.

    For MCP's `get_table_context`, built by the same builder the CLI's json/yaml paths use,
    so `budget_tokens` and `--budget` mean the same thing regardless of caller.
    """

    a = _load_table_artifacts(manifest, print_root, table, read)
    _attach_connection_notes(a, print_root, read)

    return _budgeted_structured_payload(a, options, options.budget)


def _budgeted_structured_payload(
    a: TableArtifacts,
    options: AssemblyOptions,
    budget: int | None,
) -> dict[str, Any]:
    """One table's structured payload under a budget; the identity fields never drop."""

    header, candidates = _structured_parts(a, options)

    return _payload_from(header, candidates, budget)


def _structured_parts(
    a: TableArtifacts,
    options: AssemblyOptions,
) -> tuple[dict[str, Any], list[tuple[str, Any]]]:
    header: dict[str, Any] = {"table": a.fqn, "type": a.table_type, "columns_count": a.column_count}

    if a.row_count is not None:
        header["row_count"] = a.row_count

    if a.last_run_failed:
        header["_unprofiled"] = unprofiled_message(a.fqn)

    if a.missing:
        header["_missing"] = list(a.missing)

    if a.corrupted:
        header["_corrupted"] = dict(a.corrupted)

    if not _meanings_documented(a, options.purpose):
        header["meanings_documented"] = False

    candidates: list[tuple[str, Any]] = []

    # The counts below describe the scanned set; the header survives any budget drop (SPEC 2.2.8).
    header.update(reply_scope(scope_of(a.statistics)))

    if options.purpose == "query":
        return header, _query_candidates(a, options)

    if options.include_ddl:
        candidates.append(("ddl", a.ddl))

    if options.include_annotations and a.connection_notes:
        candidates.append(("connection_notes", a.connection_notes))

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

    return header, candidates


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

    if options.include_annotations and a.connection_notes:
        candidates.append(("connection_notes", a.connection_notes))

    values = _structured_values(a)

    if values:
        candidates.append(("values", values))

    if options.include_relationships and a.relationships is not None:
        candidates.append(("joins", _structured_joins(a)))

    joined = _referencing_columns(a) if options.include_relationships else set()
    nulls = {
        name: rate
        for name, (_, rate) in _null_shares(a).items()
        if name not in values and name not in joined
    }

    if nulls:
        candidates.append(("nulls", nulls))

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

    if options.include_annotations and a.inherited:
        candidates.append(("inherited_notes", a.inherited))

    if options.include_description and a.description is not None:
        candidates.append(("description", a.description))

    return candidates


def _structured_joins(a: TableArtifacts) -> dict[str, Any]:
    """The Joins list as data: each edge's columns and detection, a lone referencer's nulls."""

    refers_to, referenced_by = _edges(a)
    shares = _null_shares(a)

    return {
        "refers_to": [
            _edge_fields(
                e,
                ("column", "path", "target_table", "target_column", "target_path", "note"),
            )
            | (
                {"null_rate": share[1]}
                if (share := _referencing_null_share(a, e, shares)) is not None
                else {}
            )
            for e in refers_to
        ],
        "referenced_by": [
            _edge_fields(
                e,
                (
                    "column",
                    "path",
                    "referencer_table",
                    "referencer_column",
                    "referencer_path",
                    "note",
                ),
            )
            for e in referenced_by
        ],
    }


def _edge_fields(entry: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """The named keys of an edge as the artifact spells them, plus its detection."""

    edge = {key: entry[key] for key in keys if key in entry}
    edge["detection"] = edge_detection(entry)

    return edge


def _structured_values(a: TableArtifacts) -> dict[str, Any]:
    """Per column: the entries the `query` purpose shows, their notes, and what they cover."""

    columns = (a.statistics or {}).get("columns")

    if not isinstance(columns, dict):
        return {}

    scope = scope_of(a.statistics)
    row_count = block_value(a.statistics or {}, "row_count")
    annotations = a.annotations or {}
    shares = _null_shares(a)
    out: dict[str, Any] = {}

    for name in _ordered_column_names(columns):
        col = columns[name]
        listed = _covered_values(col)
        frequent = _frequent_values(col, scope, row_count) if listed is None else None
        block: dict[str, Any]

        if listed is not None:
            entries, coverage = listed
            shown, share = _shown_values(entries, coverage)
            block = {
                "coverage": coverage,
                "coverage_statement": coverage_statement(coverage, scope),
            }

            if coverage != 1.0:  # noqa: RUF069 - coverage 1.0 is the exhaustive sentinel
                block["shown_coverage"] = share
        elif frequent is not None:
            shown, share = frequent
            block = {"coverage_statement": _FREQUENT_STATEMENT, "shown_coverage": share}
        else:
            continue

        redaction = column_value(col, "redacted")

        if isinstance(redaction, str) and redaction:
            block["redacted"] = redaction
            block["counts"] = [e.get("count") for e in shown]
        else:
            block["entries"] = _structured_entries(shown, annotations.get(name))

        if name in shares:
            block["null_rate"] = shares[name][1]

        if listed is not None and _codes_undocumented(
            col,
            listed[1],
            annotations.get(name),
            a.inherited.get(name),
        ):
            block["meanings_documented"] = False

        out[name] = block

    return out


def _structured_entries(entries: list[Any], annotation: dict[str, Any] | None) -> list[Any]:
    notes = value_notes(annotation)
    rendered = []

    for entry in entries:
        if not isinstance(entry, dict):
            continue

        item = {"value": entry.get("value"), "count": entry.get("count")}
        note = notes.get(value_key(entry.get("value")))

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


def _assemble_structured(
    artifacts: list[TableArtifacts],
    options: AssemblyOptions,
    connection_name: str | None,
    print_root: Path,
    read: ArtifactReader,
) -> AssemblyResult:
    """One document: a table's object bare, several wrapped in `connection`, notes and `tables`."""

    if len(artifacts) == 1:
        payloads, included, truncated = _budgeted_structured_payloads(artifacts, options)
        body: dict[str, Any] = payloads[0]
    else:
        header = _connection_keys(print_root, options, read)
        payloads, included, truncated = _budgeted_structured_payloads(artifacts, options, header)
        body = {"connection": connection_name, **header, "tables": payloads}

    if options.format == "json":
        text = json.dumps(body, indent=2, default=str, sort_keys=False) + "\n"
    else:
        text = yaml.safe_dump_all([body], sort_keys=False, default_flow_style=False)

    return AssemblyResult(text=text, tables_included=included, truncated=truncated)


def _budgeted_structured_payloads(
    artifacts: list[TableArtifacts],
    options: AssemblyOptions,
    header: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], int, tuple[str, ...]]:
    """Per-table payloads under an even split of the budget left after the `header` keys; a split
    that floors to zero excludes every table from `tables_included`.
    """

    if options.budget is None:
        payloads = [_budgeted_structured_payload(a, options, None) for a in artifacts]

        return payloads, len(artifacts), ()

    header_tokens = tokens_of(_measure_for_budget(header)) if header else 0
    per_table_budget = max(options.budget - header_tokens, 0) // len(artifacts)
    payloads = []
    truncated: list[str] = []

    for a in artifacts:
        payload = _budgeted_structured_payload(a, options, per_table_budget)
        payloads.append(payload)

        if "_truncated" in payload:
            truncated.append(a.fqn)

    included = len(artifacts) if per_table_budget > 0 else 0

    return payloads, included, tuple(truncated)


def _by_edge_key(relationships: dict[str, Any] | None) -> dict[tuple[Any, ...], Any]:
    return {
        edge_key(e): e for e in (relationships or {}).get("refers_to") or [] if isinstance(e, dict)
    }
