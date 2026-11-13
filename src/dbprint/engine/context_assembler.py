"""Per-table context-fragment builder for `dbprint context`.

Reads the on-disk artifacts, assembles a per-table fragment in the requested format
(md/json/yaml), applies the token-budget algorithm, and joins fragments across tables.
"""

from __future__ import annotations

import copy
import json
import math
import multiprocessing
import re
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Literal

import yaml

from dbprint.spec import authored
from dbprint.spec import fqn as fqn_syntax
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
from dbprint.spec.rejection import rejected_fields, without_rejected
from dbprint.spec.scope import (
    ScanScope,
    coverage_statement,
    list_is_complete,
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
    PrintReader,
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
from .value_list import count_of, grouped_values, noted_values, value_key, value_notes
from .yaml_dumper import spell_inline, spell_literal, spell_value


HEADER_TOKEN_OVERHEAD = 8  # conservative reserve for the multi-table document header
NULL_PATTERN_DISPLAY_LIMIT = 8  # combinations rendered; the footer covers exactly these

Purpose = Literal["profile", "query"]

QUERY_SAMPLE_LIMIT = 5  # most frequent values of a sampled column the query purpose shows
QUERY_FREQUENT_SHARE = 0.01  # least share of non-null rows a numeric/temporal value needs to show
NAME_ONLY_LISTED = 5  # name-only incoming joins listed one by one before they fold into counts

NO_MATCH_MIN_COMPARED = 10  # values a measured-empty join compares before it reads `no match`
# Below this many tables a worker process's start costs more than the tables it would render.
_PARALLEL_MIN_TABLES = 100
_TABLES_PER_PROCESS = 64

SAMPLE_RESCALING = (
    "a row count - nulls, a value's count, a true/false split - MAY be multiplied by "
    "row_count / rows_scanned, and a distinct count, ratio, bound, percentile, sum or mean never is"
)
FIELD_NOTES = {
    "sample": (
        "the statistics describe rows_scanned of row_count rows, a random sample: "
        f"{SAMPLE_RESCALING}"
    ),
    "filter": (
        "the statistics describe rows_scanned of row_count rows, which a predicate chose: nothing "
        "rescales to the table"
    ),
    "scanned": (
        "the statistics describe rows_scanned of row_count rows, not the table: nothing rescales "
        "to it"
    ),
    "unmeasured": (
        "each field an unmeasured list names was tried and not measured: its value is unknown, "
        "never zero or none"
    ),
    "disabled": (
        "configuration switched off each measurement the disabled list names, so every field it "
        "produces is unknown, not none"
    ),
    "sensitivity": (
        "a column carrying no inferred.sensitivity was not detected, which is not a finding that "
        "it is safe to publish"
    ),
}

_DETECTION_RANK = {"declared": 0, "inferred": 1, "measured": 2, "annotated": 3}
_NOT_A_JOIN_TARGET = "not a join target, no declared-unique column"
_MEANINGS_UNDOCUMENTED = "meanings: not documented here"
_CODED_CLASSIFICATIONS = frozenset({"categorical", "text"})
_FREQUENCY_CLASSIFICATIONS = frozenset({"numeric", "temporal"})
_FREQUENT_STATEMENT = "frequent values, not a domain"
_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
_LINE_BREAK_RE = re.compile(r"\r\n|[\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")
_QUOTE_PREFIX_RE = re.compile(r"^(?: {0,3}>[ \t]?)*")
_QUOTE_MARKER_RE = re.compile(r"^ {0,3}>[ \t]?")
_CONTAINER_CHARACTERS = " \t>*+-0123456789.)"
_LIST_MARKER_RE = re.compile(
    r"^ {0,3}(?:[-*+]|(\d{1,9})[.)])(?:[ \t]{1,4}(?![ \t])|[ \t](?=[ \t]{4})|[ \t]*$)",
)
_ATX_HEADING_RE = re.compile(r"^( {0,3})(#{1,6})(?:[ \t]+(.*))?$")
_ATX_CLOSING_RE = re.compile(r"(?:^|[ \t]+)#+[ \t]*$")
_SETEXT_UNDERLINE_RE = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_THEMATIC_BREAK_RE = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,})[^`]*$|^ {0,3}(~{3,}).*$")
_FENCE_CLOSE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
_INDENTED_CODE_RE = re.compile(r"^(?: {4}| {0,3}\t)")
_LINK_REFERENCE_RE = re.compile(r"^\[(?:[^\]\\]|\\.)+\]:")
_LINK_DEFINITION_RE = re.compile(
    r"""^ {0,3}\[(?:[^\]\\]|\\.)+\]:[ \t]*\S+(?:[ \t]+(?:"[^"]*"|'[^']*'|\([^)]*\)))?[ \t]*$""",
)
_BLANK_RE = re.compile(r"^[ \t]*$")
_HTML_BLOCK_TAGS = (
    "address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|dd|details|"
    "dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|frame|frameset|h[1-6]|head|"
    "header|hr|html|iframe|legend|li|link|main|menu|menuitem|nav|noframes|ol|optgroup|option|p|"
    "param|search|section|summary|table|tbody|td|tfoot|th|thead|title|tr|track|ul"
)
_HTML_ATTRIBUTE = (
    r"(?:[ \t]+[A-Za-z_:][A-Za-z0-9_.:-]*"
    r"""(?:[ \t]*=[ \t]*(?:[^ \t"'=<>`]+|'[^']*'|"[^"]*"))?)"""
)
# CommonMark's seven HTML block kinds: start, end, and whether the start interrupts a paragraph.
_HTML_BLOCKS = (
    (
        re.compile(r"^ {0,3}<(?:script|pre|style|textarea)(?:[ \t>]|$)", re.IGNORECASE),
        re.compile(r"</(?:script|pre|style|textarea)>", re.IGNORECASE),
        True,
    ),
    (re.compile(r"^ {0,3}<!--"), re.compile(r"-->"), True),
    (re.compile(r"^ {0,3}<\?"), re.compile(r"\?>"), True),
    (re.compile(r"^ {0,3}<![A-Za-z]"), re.compile(r">"), True),
    (re.compile(r"^ {0,3}<!\[CDATA\["), re.compile(r"\]\]>"), True),
    (
        re.compile(rf"^ {{0,3}}</?(?:{_HTML_BLOCK_TAGS})(?:[ \t]|/?>|$)", re.IGNORECASE),
        _BLANK_RE,
        True,
    ),
    (
        re.compile(
            rf"^ {{0,3}}(?:<[A-Za-z][A-Za-z0-9-]*{_HTML_ATTRIBUTE}*[ \t]*/?>"
            r"|</[A-Za-z][A-Za-z0-9-]*[ \t]*>)[ \t]*$",
        ),
        _BLANK_RE,
        False,
    ),
)
_AUTHORED_HEADING_LEVEL = 3

# Under `query`, the render order and the order a budget keeps sections in; the header is pinned.
_QUERY_SECTION_ORDER = ("legend", "ddl", "connection_notes", "values", "dictionary", "joins")

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
    profiled_at: str | None = None
    # Each referencer column's distinct count, keyed (referencer_table, column), for SPEC 2.3.10.
    incoming_cardinalities: dict[tuple[Any, str], int] = field(default_factory=dict)
    # SPEC 2.7.3's notes, set on a single-table render only; a multi-table one carries them once.
    connection_notes: str | None = None
    # The same notes on every render, read as documentation by the meanings cues.
    shared_notes: str | None = None
    prose_spans: tuple[str, ...] | None = None
    # SPEC 2.7.1's `derived_from`: per column, each source's label and its note and value notes.
    inherited: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # Per table, each column whose `physical_name` differs from its key; another table's if named.
    spellings: dict[str, dict[str, str]] = field(default_factory=dict)
    # Each `inherited` source label with its column as the source table spells it.
    source_labels: dict[str, str] = field(default_factory=dict)
    # Each noted `derived_from` source as (table, column), the ones `inherited` labels.
    sources: set[tuple[str, str]] = field(default_factory=set)
    spelling_files: tuple[Path, ...] = ()


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
    workers: int = 1,
) -> AssemblyResult:
    """Assemble the requested table fragments; `tables` is the caller's resolved FQN order.

    With `workers` above 1, a Markdown selection of many tables under no budget renders in up to
    that many processes, each reading the print from disk itself.
    """

    if not tables:
        return AssemblyResult(text="", tables_included=0)

    if (
        workers > 1
        and options.format == "md"
        and options.budget is None
        and len(tables) >= _PARALLEL_MIN_TABLES
    ):
        return _assemble_markdown_in_processes(
            manifest,
            print_root,
            tables,
            options,
            connection_name,
            multi_connection,
            workers,
        )

    read = _reader(read)
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

    read = PrintReader()
    loaded = [_load_table_artifacts(manifest, print_root, fqn, read) for fqn in tables]
    header = _connection_keys(print_root, options, read)
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

    read = _reader(read)
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

    read = _reader(read)
    a = _load_table_artifacts(manifest, print_root, table, read)
    _attach_connection_notes(a, print_root, read)

    return _structured_parts(a, options)


def spelling_files(
    manifest: dict[str, Any],
    print_root: Path,
    table: str,
    read: ArtifactReader = read_artifact,
) -> tuple[Path, ...]:
    """The other tables' statistics files `table`'s context reads for its column spellings."""

    return _load_table_artifacts(manifest, print_root, table, _reader(read)).spelling_files


def open_questions(
    manifest: dict[str, Any],
    print_root: Path,
    table: str,
    read: ArtifactReader = read_artifact,
) -> list[OpenQuestion]:
    """What `table`'s print cannot answer and the print-root-relative file each is answered in."""

    read = _reader(read)
    a = _load_table_artifacts(manifest, print_root, table, read)
    entry = _entries(manifest, read).get(table) or {}
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
            # The brief asks for the value notes resolve_value reads; prose naming a column is not.
            if _codes_undocumented(a, name, col, listed[1], prose=False):
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
        label = f"{_target(entry)} ({_how_found(entry)})"

        if len(cols) > 1 or entry.get("path"):
            label = f"{_endpoint(entry, 'column', 'path')} -> {label}"

        for column in cols:
            out.setdefault(column, []).append(label)

    return out


def neighbour_weight(print_root: Path, fqn: str, entry: dict[str, Any]) -> int:
    """Sort key for loading `fqn`: its relationships file size; the file names the tables read."""

    name = declared_artifacts(entry).get("relationships")

    try:
        return (table_directory(print_root, fqn, entry) / name).stat().st_size if name else 0
    except OSError:
        return 0


def no_match(entry: dict[str, Any]) -> bool:
    """Whether the edge measured empty: `containment` 0 over `NO_MATCH_MIN_COMPARED`+ values."""

    observed = entry.get("observed")

    if not isinstance(observed, dict):
        return False

    compared = observed.get("answerable_count")

    return (
        _is_number(observed.get("containment"))
        and observed["containment"] == 0
        and isinstance(compared, int)
        and not isinstance(compared, bool)
        and compared >= NO_MATCH_MIN_COMPARED
    )


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

    frame = _markdown_frame(
        len(artifacts),
        options,
        connection_name,
        print_root,
        manifest,
        multi_connection,
        read,
    )
    header_tokens = max(HEADER_TOKEN_OVERHEAD, len(frame.header) // 4) if frame.header else 0
    legend_tokens = 0

    # One legend serves the whole document, so its room comes off the top like the header's.
    if frame.shared_legend and options.budget is not None:
        offered = frozenset(frame.header_terms).union(
            *(
                s.terms
                for a in artifacts
                for s in _sections(a, options, frame.connection_profile, frame.adapter)
            ),
        )
        legend_tokens = tokens_of(context_terms.legend(offered))

    per_table_budget: int | None = None

    if options.budget is not None:
        remaining = max(0, options.budget - header_tokens - legend_tokens)
        per_table_budget = remaining // len(artifacts)

        if per_table_budget == 0:
            # Budget cannot cover any table; emit the header (when present) only.
            text = frame.header + "\n" if frame.header else ""

            return AssemblyResult(
                text=text,
                tables_included=0,
                truncated=tuple(a.fqn for a in artifacts),
            )

    renders = [
        (
            a.fqn,
            _render_table_markdown(
                a,
                options,
                per_table_budget,
                frame.connection_profile,
                frame.adapter,
                with_legend=not frame.shared_legend,
            ),
        )
        for a in artifacts
    ]

    return _joined_markdown(frame, renders, multi_connection)


def _assemble_markdown_in_processes(
    manifest: dict[str, Any],
    print_root: Path,
    tables: list[str],
    options: AssemblyOptions,
    connection_name: str | None,
    multi_connection: bool,
    workers: int,
) -> AssemblyResult:
    """`_assemble_markdown` with no budget, each table loaded and rendered in a worker process.

    Workers read the print from disk through readers of their own.
    """

    read = PrintReader()
    frame = _markdown_frame(
        len(tables),
        options,
        connection_name,
        print_root,
        manifest,
        multi_connection,
        read,
    )
    settings = (options, frame.connection_profile, frame.adapter, not frame.shared_legend)
    processes = min(workers, math.ceil(len(tables) / _TABLES_PER_PROCESS))

    with ProcessPoolExecutor(
        processes,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_start_render_worker,
        initargs=(manifest, print_root, settings),
    ) as pool:
        # Heaviest tables first, so none left to the end of the queue stretches the run's tail.
        entries = _entries(manifest, read)
        heaviest_first = sorted(
            tables,
            key=lambda fqn: -neighbour_weight(print_root, fqn, entries.get(fqn) or {}),
        )
        rendered = {fqn: pool.submit(_render_in_worker, fqn) for fqn in heaviest_first}
        renders = [(fqn, rendered[fqn].result()) for fqn in tables]

    return _joined_markdown(frame, renders, multi_connection)


@dataclass(frozen=True)
class _MarkdownFrame:
    """What a Markdown reply carries around its tables: the header and their render settings."""

    header: str
    header_terms: frozenset[str]
    connection_profile: dict[str, Any]
    adapter: str | None
    multi_table: bool
    shared_legend: bool


def _markdown_frame(
    count: int,
    options: AssemblyOptions,
    connection_name: str | None,
    print_root: Path,
    manifest: dict[str, Any],
    multi_connection: bool,
    read: ArtifactReader,
) -> _MarkdownFrame:
    multi_table = count > 1
    header = ""
    header_terms: set[str] = set()

    if (multi_table or multi_connection) and connection_name:
        table_word = "table" if count == 1 else "tables"
        header = f"# Context for connection {connection_name} ({count} {table_word})"

        if multi_table:
            notes, notes_reason = load_connection_notes(print_root, read)

            if notes and options.include_annotations:
                header += "\n\n## Connection notes\n\n" + _quoted_block(_nested_headings(notes))
                header_terms.add("quoted")

            if notes_reason is not None:
                header += "\n\n" + _corrupted_summary({"manifest_annotations": notes_reason})

        provenance = _provenance_block(manifest, connection_name)

        if provenance:
            header += "\n\n" + provenance

    adapter = manifest.get("adapter")

    return _MarkdownFrame(
        header=header,
        header_terms=frozenset(header_terms),
        connection_profile=table_readings.connection_profile(manifest),
        adapter=adapter if isinstance(adapter, str) and adapter else None,
        multi_table=multi_table,
        shared_legend=multi_table or multi_connection,
    )


def _joined_markdown(
    frame: _MarkdownFrame,
    renders: list[tuple[str, _TableRender]],
    multi_connection: bool,
) -> AssemblyResult:
    fragments: list[str] = []
    truncated: list[str] = []
    terms = set(frame.header_terms)
    included = 0

    for fqn, rendered in renders:
        if rendered.text:
            fragments.append(rendered.text)

        terms |= rendered.terms

        # A budget too tight for even the header leaves `fragment` as the bare truncation
        # marker - real text, but not a table this run actually included.
        if rendered.has_content:
            included += 1

            if rendered.truncated:
                truncated.append(fqn)
        else:
            truncated.append(fqn)

    body = "\n\n---\n\n".join(fragments)
    legend = context_terms.legend(terms) if frame.multi_table and not multi_connection else ""
    text = "".join(f"{part}\n\n" for part in (frame.header, legend) if part) + body

    return AssemblyResult(
        text=text,
        tables_included=included,
        truncated=tuple(truncated),
        terms=frozenset(terms),
    )


# A worker process's print and render settings, set once by `_start_render_worker`.
_RENDER_WORKER: dict[str, Any] = {}


def _start_render_worker(
    manifest: dict[str, Any],
    print_root: Path,
    settings: tuple[AssemblyOptions, dict[str, Any], str | None, bool],
) -> None:
    _RENDER_WORKER.update(manifest=manifest, print_root=print_root, settings=settings)
    _RENDER_WORKER["read"] = PrintReader()


def _render_in_worker(fqn: str) -> _TableRender:
    options, connection_profile, adapter, with_legend = _RENDER_WORKER["settings"]
    a = _load_table_artifacts(
        _RENDER_WORKER["manifest"],
        _RENDER_WORKER["print_root"],
        fqn,
        _RENDER_WORKER["read"],
    )

    return _render_table_markdown(
        a,
        options,
        None,
        connection_profile,
        adapter,
        with_legend=with_legend,
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
    shown = [s for s in offered if s.name in kept and (s.name != "legend" or legend)]
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
        sections.append(_section("connection_notes", _markdown_connection_notes(a)))

    if options.include_ddl:
        sections.append(make_section("ddl", _markdown_ddl(a)))

    if options.include_description and a.description:
        sections.append(_section("description", _markdown_description(a)))

    if options.include_annotations and a.annotations and _has_rendered_annotations(a.annotations):
        sections.append(_section("annotations", _markdown_annotations(a)))

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
    joined = _referencing_columns(a) if options.include_relationships else set()
    rendered = (
        ("header", _query_markdown_header(a, adapter)),
        ("ddl", [Rendered(_markdown_ddl(a))] if options.include_ddl else []),
        (
            "connection_notes",
            _markdown_connection_notes(a)
            if options.include_annotations and a.connection_notes
            else [],
        ),
        ("joins", _markdown_joins(a) if options.include_relationships else []),
        ("dictionary", _markdown_data_dictionary(a, options)),
        ("values", _markdown_column_values(a, joined, notes=options.include_annotations)),
    )
    sections = {
        name: _section(name, lines, pinned=name == "header") for name, lines in rendered if lines
    }

    return [sections[n] for n in ("header", *_QUERY_SECTION_ORDER) if n in sections]


def _query_markdown_header(a: TableArtifacts, adapter: str | None) -> list[Rendered]:
    """Identity plus the scope marker - what the value lists below cover, and no other measure."""

    lines = _identity_lines(a, adapter, "query")

    if (scoped := _scope_summary(a.statistics or {})).text:
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
    own = partial(_spelling, a, a.fqn)

    for entry in refers_to:
        target = _target(entry, partial(_spelling, a, entry.get("target_table")))
        line = Rendered(
            f"- {_endpoint(entry, 'column', 'path', own)} -> {target} ({_how_found(entry)})",
            _detection_terms(entry),
        )
        facts = [line]

        if (share := _referencing_null_share(a, entry, shares)) is not None:
            facts.append(_nulls_fact(share[0]))

        lines.append(notes_synthesis.join_facts([*facts, *_note_fact(entry)]))

    listed, grouped = _name_only_groups(referenced_by)

    for entry in listed:
        referencer = _referencer(entry, partial(_spelling, a, entry.get("referencer_table")))
        line = Rendered(
            f"- {_endpoint(entry, 'column', 'path', own)} <- {referencer} ({_how_found(entry)})",
            _detection_terms(entry),
        )
        lines.append(notes_synthesis.join_facts([line, *_note_fact(entry)]))

    for group in grouped:
        first = group[0]
        spelled = _spelling(a, first.get("referencer_table"), first["referencer_column"][0])
        lines.append(
            Rendered(
                f"- {own(first['column'][0])} <- {_name_only_text(group, spelled)}",
                frozenset({"detection:inferred"}),
            ),
        )

    return lines


def _name_only_groups(
    edges: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]:
    """Past `NAME_ONLY_LISTED` name-only edges, a column pair two or more share folds to a count."""

    name_only = [e for e in edges if _name_only(e)]

    if len(name_only) <= NAME_ONLY_LISTED:
        return edges, []

    pairs: dict[tuple[str, str], list[dict[str, Any]]] = {}

    for edge in name_only:
        pairs.setdefault((edge["column"][0], edge["referencer_column"][0]), []).append(edge)

    folded = {id(e) for group in pairs.values() if len(group) > 1 for e in group}
    grouped = sorted((g for g in pairs.values() if len(g) > 1), key=lambda g: -len(g))

    return [e for e in edges if id(e) not in folded], grouped


def _name_only(edge: dict[str, Any]) -> bool:
    observed = edge.get("observed")
    measured = (
        isinstance(observed, dict)
        and observed.get("fanout_avg") is not None
        and observed.get("target_coverage") is not None
    )

    return (
        edge_detection(edge) == "inferred"
        and not edge.get("note")
        and not edge.get("path")
        and not edge.get("referencer_path")
        and _single_column(edge) is not None
        and len(edge.get("referencer_column") or []) == 1
        and not measured
        and not no_match(edge)
    )


def _name_only_text(group: list[dict[str, Any]], spelled: str) -> str:
    key = group[0]["referencer_column"][0]
    unmeasured = "; not measured" if all(_unmeasured_terms(e) for e in group) else ""

    return (
        f"{spelled} in {len(group)} tables (inferred){unmeasured}; search_columns pattern "
        f"'{key}' lists every column of that name"
    )


def _target(entry: dict[str, Any], spell: Callable[[str], str] = str) -> str:
    target = _endpoint(entry, "target_column", "target_path", spell)

    return f"{entry.get('target_table', '?')}.{target}"


def _referencer(entry: dict[str, Any], spell: Callable[[str], str] = str) -> str:
    referencer = _endpoint(entry, "referencer_column", "referencer_path", spell)

    return f"{entry.get('referencer_table', '?')}.{referencer}"


def _endpoint(
    entry: dict[str, Any],
    columns: str,
    path: str,
    spell: Callable[[str], str] = str,
) -> str:
    """The columns, and the key path inside the one column a path-valued edge names (SPEC 2.3.9)."""

    keys = entry.get(path)
    joined = _join_columns(entry.get(columns), spell)

    return (
        f"{joined} (path {'.'.join(map(str, keys))})" if isinstance(keys, list) and keys else joined
    )


def _note_fact(entry: dict[str, Any]) -> list[Rendered]:
    note = entry.get("note")

    return [_quoted(f"note: {_spelled_note(note)}")] if isinstance(note, str) else []


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
    matched = frozenset({"no_match"}) if no_match(entry) else frozenset()

    if edge_detection(entry) == "annotated":
        return frozenset({"annotated"}) | matched

    key = f"detection:{edge_detection(entry)}"

    return (frozenset({key}) if key in TERMS else frozenset()) | matched


def _how_found(entry: dict[str, Any]) -> str:
    suffix = notes_synthesis.NO_MATCH_SUFFIX if no_match(entry) else ""

    return f"{edge_detection(entry)}{suffix}"


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


def _join_columns(columns: Any, spell: Callable[[str], str] = str) -> str:
    """One column bare, a composite key in parentheses, so `a, b -> t.c, d` cannot be misread."""

    names = [spell(str(c)) for c in columns] if isinstance(columns, list) else []

    if not names:
        return "?"

    return names[0] if len(names) == 1 else "(" + ", ".join(names) + ")"


def _markdown_data_dictionary(a: TableArtifacts, options: AssemblyOptions) -> list[Rendered]:
    """The description and every column a human has defined, unshown value notes included."""

    description = a.description if options.include_description else None
    annotations = a.annotations or {}
    unshown = _unshown_value_notes(a)
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
                _spelling(a, a.fqn, name),
                annotations.get(name, {}),
                unshown.get(name, []),
                [
                    {**source, "source": a.source_labels.get(source["source"], source["source"])}
                    for source in a.inherited.get(name, [])
                ],
            )
        ]
        if options.include_annotations
        else []
    )

    if not notes and not description:
        return []

    lines = [Rendered("## Data dictionary")]

    if description:
        lines += [Rendered(""), *_quoted_lines(_nested_headings(description.rstrip()))]

    if notes:
        lines += [Rendered(""), *notes]

    return lines


def _dictionary_lines(
    name: str,
    entry: dict[str, Any],
    unshown: list[_ValueNote],
    inherited: list[dict[str, Any]],
) -> list[Rendered]:
    lines = []
    note = entry.get("note")
    own = [_quoted(_spelled_note(note))] if isinstance(note, str) and note.strip() else []
    own += [_value_note_fact(noted) for noted in unshown]

    if own:
        facts = notes_synthesis.join_facts(own)
        lines.append(Rendered(f"- {name}: {facts.text}", facts.terms))

    for source in inherited:
        facts = [_spelled_note(source["note"])] if source.get("note") else []
        facts += [
            f"{spell_literal(v['value'])} = {_spelled_note(v['note'])}"
            for v in source.get("values", [])
        ]
        lines.append(_quoted(f"- {name} (from {source['source']}): {'; '.join(facts)}"))

    return lines


@dataclass(frozen=True)
class _ValueNote:
    """A value note no shown value list entry carries; `absent` when a complete list lacks it."""

    value: Any
    note: str
    absent: bool


def _unshown_value_notes(a: TableArtifacts) -> dict[str, list[_ValueNote]]:
    """Per column, in file order, the value notes no shown entry carries; a redacted column's are
    withheld, each naming a literal (SPEC 2.2.9).
    """

    columns = (a.statistics or {}).get("columns")
    columns = columns if isinstance(columns, dict) else {}
    scope = scope_of(a.statistics)
    row_count = block_value(a.statistics or {}, "row_count")
    out: dict[str, list[_ValueNote]] = {}

    for name, annotation in (a.annotations or {}).items():
        col = columns.get(name)

        if _redacted(col):
            continue

        shown = {value_key(e.get("value")) for e in _shown_entries(col, scope, row_count)}
        listed = [
            e.get("value")
            for e in (column_value(col, "values") if isinstance(col, dict) else None) or []
            if isinstance(e, dict)
        ]
        stored = {value_key(v): v for v in listed}
        complete = isinstance(col, dict) and list_is_complete(col)
        unshown = [
            _ValueNote(
                value if _listed_as_is(value, listed) else stored.get(key, value),
                note,
                absent=complete and value not in listed and key not in stored,
            )
            for key, (value, note) in noted_values(annotation).items()
            if key not in shown
        ]

        if unshown:
            out[name] = unshown

    return out


def _redacted(col: Any) -> bool:
    redaction = column_value(col, "redacted") if isinstance(col, dict) else None

    return isinstance(redaction, str) and bool(redaction)


def _listed_as_is(value: Any, listed: list[Any]) -> bool:
    return any(type(v) is type(value) and v == value for v in listed)


def _shown_entries(col: Any, scope: ScanScope | None, row_count: Any) -> list[dict[str, Any]]:
    if (listed := _covered_values(col)) is not None:
        return _shown_values(*listed)[0]

    frequent = _frequent_values(col, scope, row_count)

    return frequent[0] if frequent is not None else []


def _value_note_fact(noted: _ValueNote) -> Rendered:
    if noted.absent:
        return Rendered(
            f"{spell_literal(noted.value)} (not in the rows read) = {spell_literal(noted.note)}",
            frozenset({"not_in_rows_read", "quoted"}),
        )

    return _quoted(f"{spell_literal(noted.value)} = {spell_literal(noted.note)}")


def _markdown_column_values(
    a: TableArtifacts,
    joined: set[str],
    *,
    notes: bool = True,
) -> list[Rendered]:
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
            cue = _codes_undocumented(a, name, col, coverage)
        elif frequent is not None:
            shown, share = frequent
            statement, cue = _FREQUENT_STATEMENT, False
        else:
            continue

        values_cell = _values_cell(col, shown, annotations.get(name) if notes else None)
        nulls = shares.pop(name, ("",))[0]
        rows.append(
            Rendered(
                f"| {_escape_cell(_spelling(a, a.fqn, name))} | {values_cell.text} | "
                f"{spell_percent(share)} - {statement}"
                f"{f'; {_MEANINGS_UNDOCUMENTED}' if cue else ''} | {nulls} |",
                values_cell.terms
                | _scope_terms(statement)
                | _scope_terms(nulls)
                | ({"meanings"} if cue else set())
                | ({"frequent_values"} if frequent is not None else set()),
            ),
        )

    rest = [
        f"{_spelling(a, a.fqn, name)} ({share})"
        for name, (share, _) in shares.items()
        if name not in joined
    ]

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
    """The value entries and the coverage describing them; None without both (SPEC 2.2.3), or
    for a sampled list whose values each hold one row, which names rows rather than codes.

    A `numeric`/`temporal` list carries no coverage: a frequency sample, never a domain.
    """

    if not isinstance(col, dict):
        return None

    entries = column_value(col, "values")
    coverage = column_value(col, "values_coverage")

    if not isinstance(entries, list) or not entries or not _is_number(coverage):
        return None

    listed = [e for e in entries if isinstance(e, dict)]

    totals = [sum(count_of(e) for e in (c, *m)) for c, m in grouped_values(listed)]

    if coverage < 1.0 and all(total <= 1 for total in totals):
        return None

    return listed, float(coverage)


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
    shown = [e for e in entries if isinstance(e, dict) and count_of(e) / population >= floor]
    shown = shown[:QUERY_SAMPLE_LIMIT]

    return (shown, sum(count_of(e) for e in shown) / population) if shown else None


def _shown_values(
    entries: list[dict[str, Any]],
    coverage: float,
) -> tuple[list[dict[str, Any]], float]:
    """The entries `query` shows and the share of the column they cover.

    A sampled list is cut to `QUERY_SAMPLE_LIMIT` categories, a spelling group counting as one.
    """

    if coverage == 1.0:  # noqa: RUF069 - coverage 1.0 is the exhaustive sentinel
        return [
            entry
            for canonical, members in grouped_values(entries)
            for entry in (canonical, *members)
        ], coverage

    groups = grouped_values(entries)[:QUERY_SAMPLE_LIMIT]
    shown = [entry for canonical, members in groups for entry in (canonical, *members)]
    listed = sum(count_of(e) for e in entries)
    kept = sum(count_of(e) for e in shown)

    return shown, coverage * kept / listed if listed else coverage


def _is_number(value: Any) -> bool:
    """A real number - `bool` is an `int` to Python and never a coverage."""

    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _codes_undocumented(
    a: TableArtifacts,
    name: str,
    col: Any,
    coverage: float,
    *,
    prose: bool = True,
) -> bool:
    return (
        coverage == 1.0  # noqa: RUF069 - coverage 1.0 is the exhaustive sentinel
        and column_value(col, "classification") in _CODED_CLASSIFICATIONS
        and not column_value(col, "redacted")
        and not _has_meaning((a.annotations or {}).get(name))
        and not a.inherited.get(name)
        and not (prose and _a_code_span_names(a, name))
    )


def _a_code_span_names(a: TableArtifacts, name: str, *, shared_only: bool = False) -> bool:
    names = {name.lower(), _spelling(a, a.fqn, name).lower()}
    word = re.compile("|".join(rf"(?<![\w.]){re.escape(n)}(?!\w)" for n in sorted(names)))
    spans = _code_spans(a.shared_notes) if shared_only else _prose_spans(a)

    return any(word.search(span) for span in spans)


def _prose_spans(a: TableArtifacts) -> tuple[str, ...]:
    if a.prose_spans is None:
        texts = [a.description, a.shared_notes]

        for entry in (a.annotations or {}).values():
            if isinstance(entry, dict):
                texts += [entry.get("note"), *value_notes(entry).values()]

        a.prose_spans = tuple(span for text in texts for span in _code_spans(text))

    return a.prose_spans


def _code_spans(text: Any) -> tuple[str, ...]:
    return tuple(s.lower() for s in _CODE_SPAN_RE.findall(text)) if isinstance(text, str) else ()


def _meanings_documented(a: TableArtifacts, purpose: Purpose) -> bool:
    if (a.description and a.description.strip()) or (purpose == "query" and a.inherited):
        return True

    if any(_has_meaning(entry) for entry in (a.annotations or {}).values()):
        return True

    named = [*((a.statistics or {}).get("columns") or {}), fqn_syntax.split(a.fqn)[-1]]

    return any(_a_code_span_names(a, str(n), shared_only=True) for n in named)


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
    counts = [count_of(entry) for entry in entries if isinstance(entry, dict)]

    if isinstance(redaction, str) and redaction:
        withheld = ", ".join(f"withheld ({c})" for c in counts)
        keys = {"redacted", "values", "withheld", f"redaction:{redaction}"}

        return Rendered(f"redacted: {redaction}; values: {withheld}", frozenset(keys & set(TERMS)))

    notes = value_notes(annotation)
    cells = []

    for entry, members in grouped_values(entries):
        value = entry.get("value")
        counted = [entry, *members]
        total = sum(count_of(e) for e in counted)
        cell = f"{_escape_cell(spell_literal(value))} ({total})"

        if members:
            spellings = ", ".join(
                f"{_escape_cell(spell_literal(e.get('value')))} ({count_of(e)})"
                + _inline_note(notes.get(value_key(e.get("value"))) if e is not entry else None)
                for e in counted
            )
            cell += f" {{{spellings}}}"

        cells.append(cell + _inline_note(notes.get(value_key(value))))

    noted = any(value_key(e.get("value")) in notes for e in entries if isinstance(e, dict))

    return Rendered(", ".join(cells), frozenset({"quoted"}) if noted else frozenset())


def _inline_note(note: str | None) -> str:
    return f" = {_escape_cell(spell_literal(note))}" if note else ""


def _quoted(text: str) -> Rendered:
    return Rendered(text, frozenset({"quoted"}))


def _quoted_lines(text: str) -> list[Rendered]:
    return [
        _quoted(f"> {_expanded_lead(line)}" if line else ">") for line in text.splitlines() or [""]
    ]


def _quoted_block(text: str) -> str:
    return "\n".join(line.text for line in _quoted_lines(text))


def _spelled_note(note: str) -> str:
    return spell_literal(" ".join(note.split()))


def _nested_headings(text: str) -> str:
    lines = _LINE_BREAK_RE.split(text)
    headings = _heading_lines(lines)

    if not headings:
        return text

    shift = _AUTHORED_HEADING_LEVEL - min(h.level for h in headings.values())
    out: list[str] = []
    index = 0

    while index < len(lines):
        if (h := headings.get(index)) is not None:
            # A title ending in a `#` run would lose it as the ATX closing sequence.
            title = f"{h.title} #" if _ATX_CLOSING_RE.search(h.title) else h.title
            out.append(f"{h.prefix}{'#' * min(6, h.level + shift)} {title}".rstrip())
            index = h.last + 1
        else:
            out.append(lines[index])
            index += 1

    return "\n".join(out)


@dataclass(frozen=True)
class _Heading:
    level: int
    title: str
    prefix: str
    last: int


@dataclass
class _Paragraph:
    first: int
    level: tuple[int, int, bool]
    prefix: str
    parts: list[str]


def _heading_lines(lines: list[str]) -> dict[int, _Heading]:
    out: dict[int, _Heading] = {}
    paragraph: _Paragraph | None = None
    items: list[int] = []
    fence: str | None = None
    html: re.Pattern[str] | None = None
    block = (0, 0, 0)
    empty_item = False
    definition: tuple[int, int, bool] | None = None

    for index, raw in enumerate(lines):
        line = _expanded_lead(raw)
        raw_blank = not line.strip()

        if raw_blank and empty_item:
            items.pop()

        empty_item = False
        indent = len(line) - len(line.lstrip(" "))
        nested = len(items) if raw_blank else sum(1 for column in items if column <= indent)
        after_items = line[items[nested - 1] if nested else 0 :]
        after_quote = _QUOTE_PREFIX_RE.sub("", after_items, count=1)
        depth = after_items[: len(after_items) - len(after_quote)].count(">")
        lead, content = line[: len(line) - len(after_quote)], after_quote

        if fence is not None or html is not None:
            held = (
                None
                if nested < block[0]
                else _quote_stripped(line[items[block[0] - 1] if block[0] else 0 :], block[1])
            )

            if held is not None and block[2] and held.strip():
                held = held[block[2] :] if not held[: block[2]].strip() else None

            if held is None:
                fence = html = None
            elif fence is not None:
                closed = _FENCE_CLOSE_RE.match(held)

                if closed and closed[1][0] == fence[0] and len(closed[1]) >= len(fence):
                    fence = None

                continue
            else:
                if html is not None and html.search(held):
                    html = None

                continue

        blank = not content.strip()
        marker = None if blank else _LIST_MARKER_RE.match(content)
        body = content[marker.end() :] if marker else content
        prefix = lead + (content[: marker.end()] if marker else "")
        plain = (nested, depth, False)
        level = (
            (nested + 1, depth, False) if marker and not depth else (nested, depth, bool(marker))
        )
        thematic = bool(_THEMATIC_BREAK_RE.match(content) or _THEMATIC_BREAK_RE.match(body))
        continued = False
        after_definition, definition = definition, None

        if (
            paragraph is not None
            and paragraph.level == plain
            and (underline := _SETEXT_UNDERLINE_RE.match(content))
            and not _LINK_REFERENCE_RE.match(paragraph.parts[0])
        ):
            title = " ".join(paragraph.parts)
            level_of = 1 if underline[1][0] == "=" else 2
            out[paragraph.first] = _Heading(level_of, title, paragraph.prefix, index)
            paragraph, marker = None, None
        elif blank:
            paragraph = None
        elif (
            marker is not None
            and plain in {paragraph.level if paragraph else None, after_definition}
            and ((marker[1] is not None and int(marker[1]) != 1) or not body.strip())
        ):
            if paragraph is None:
                definition = plain
            else:
                paragraph.parts.append(content.strip())

            continued = True
        elif (found := _html_block(body, interrupting=paragraph is not None)) is not None:
            html = None if found is not _BLANK_RE and found.search(body) else found
            block, paragraph = (level[0], depth, marker.end() if marker and depth else 0), None
        elif opened := _FENCE_OPEN_RE.match(body):
            fence = opened[1] or opened[2]
            block, paragraph = (level[0], depth, marker.end() if marker and depth else 0), None
        elif atx := _ATX_HEADING_RE.match(body):
            title = _ATX_CLOSING_RE.sub("", atx[3] or "").strip()
            out[index] = _Heading(len(atx[2]), title, prefix + atx[1], index)
            paragraph = None
        elif thematic:
            paragraph = None
        elif paragraph is not None and marker is None and depth <= paragraph.level[1]:
            paragraph.parts.append(body.strip())
            continued = True
        elif _INDENTED_CODE_RE.match(body):
            paragraph = None
        elif _LINK_DEFINITION_RE.match(body):
            paragraph, definition = None, plain
        else:
            paragraph = _Paragraph(index, level, prefix, [body.strip()]) if body.strip() else None

        if not raw_blank and not continued:
            del items[nested:]

            if marker is not None and not depth and not thematic:
                empty_item = not body.strip()
                items.append(len(prefix.rstrip()) + 1 if empty_item else len(prefix))

    return out


def _expanded_lead(line: str) -> str:
    # A tab's width depends on its column, which a `> ` prefix or a rewritten heading would move.
    lead_end = len(line) - len(line.lstrip(_CONTAINER_CHARACTERS))

    return line[:lead_end].expandtabs(4) + line[lead_end:]


def _quote_stripped(text: str, depth: int) -> str | None:
    for _ in range(depth):
        if (stripped := _QUOTE_MARKER_RE.sub("", text, count=1)) == text:
            return None

        text = stripped

    return text


def _html_block(body: str, *, interrupting: bool) -> re.Pattern[str] | None:
    for start, end, interrupts in _HTML_BLOCKS:
        if start.match(body) and (interrupts or not interrupting):
            return end

    return None


def _escape_cell(text: str) -> str:
    """Make `text` safe to interpolate into one Markdown table cell.

    Order is load-bearing: escaping the pipe first leaves a live delimiter behind a backslash.
    """

    escaped = text.replace("\\", "\\\\").replace("|", "\\|")

    return escaped.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def _spelling(a: TableArtifacts, table: Any, column: str) -> str:
    return a.spellings.get(table, {}).get(column, column) if isinstance(table, str) else column


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

    if a.profiled_at is not None:
        lines.append(f"Profiled: {a.profiled_at}")

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

    quoted = {"quoted"} if any(key.get("note") for key in reading.keys) else set()

    return Rendered(f"Grain: {rendered}", grain | quoted | {d for d in detections if d in TERMS})


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


def _markdown_connection_notes(a: TableArtifacts) -> list[Rendered]:
    assert a.connection_notes is not None

    notes = _nested_headings(a.connection_notes)

    return [Rendered("## Connection notes"), Rendered(""), *_quoted_lines(notes)]


def _markdown_ddl(a: TableArtifacts) -> str:
    return "## DDL\n\n```sql\n" + a.ddl.rstrip() + "\n```"


def _markdown_description(a: TableArtifacts) -> list[Rendered]:
    assert a.description is not None

    description = _nested_headings(a.description.rstrip())

    return [Rendered("## Description"), Rendered(""), *_quoted_lines(description)]


def _markdown_annotations(a: TableArtifacts) -> list[Rendered]:
    assert a.annotations is not None
    lines = [Rendered("## Annotations")]

    for name, entry in a.annotations.items():
        if not _annotation_entry_has_content(entry):
            continue

        note = entry.get("note")

        # A colon promises text that is not coming - a note-less header names the column and stops.
        if isinstance(note, str) and note.strip():
            lines.append(_quoted(f"- **{name}**: {spell_literal(note.strip())}"))
        else:
            lines.append(Rendered(f"- **{name}**"))

        claims = entry.get("claims")

        if isinstance(claims, dict) and claims:
            # The assertion grammar's own YAML (ASSERTIONS.md 2.1), not Python's repr.
            rendered = ", ".join(
                f"{stat}: {spell_inline(predicate)}" for stat, predicate in claims.items()
            )
            lines.append(Rendered(f"  - claims: {rendered}"))

        values = entry.get("values")

        if isinstance(values, list):
            for value_entry in values:
                if not isinstance(value_entry, dict):
                    continue

                value_note = value_entry.get("note")

                if isinstance(value_note, str) and value_note.strip():
                    value = value_entry.get("value")
                    spelled = spell_value(value)
                    lines.append(_quoted(f"  - {spelled}: {spell_literal(value_note.strip())}"))

    return lines


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
    declared = {
        str(column)
        for key in grain_reading(a.statistics, None).keys
        if key.get("detection") == "declared"
        for column in key.get("columns") or []
    }

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
            key=name in declared,
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
                f"- -> {_target(entry)} ({_how_found(entry)}); via: {via}{_on_delete(entry)}"
                f"{_unmeasured_edge(entry)}",
                _detection_terms(entry)
                | {"via"}
                | _on_delete_terms(entry)
                | _unmeasured_terms(entry),
            ),
        )
        lines.extend(_note_line(entry))
        lines.extend(_observed_lines(entry, _own_cardinality(a, entry.get("column"))))

    listed, grouped = _name_only_groups(referenced_by)

    for entry in listed:
        lines.append(
            Rendered(
                f"- <- {_referencer(entry)} ({_how_found(entry)}){_on_delete(entry)}"
                f"{_unmeasured_edge(entry)}",
                _detection_terms(entry) | _on_delete_terms(entry) | _unmeasured_terms(entry),
            ),
        )
        lines.extend(_note_line(entry))
        referencer = (entry.get("referencer_table"), _join_columns(entry.get("referencer_column")))
        lines.extend(_observed_lines(entry, a.incoming_cardinalities.get(referencer)))

    for group in grouped:
        text = f"- <- {_name_only_text(group, group[0]['referencer_column'][0])}"
        terms = {"detection:inferred"} | _unmeasured_terms(group[0])
        lines.append(Rendered(text, frozenset(terms)))

    return lines


def _note_line(entry: dict[str, Any]) -> list[Rendered]:
    note = entry.get("note")

    return [_quoted(f"  note: {_spelled_note(note)}")] if isinstance(note, str) else []


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


def _unmeasured_edge(entry: dict[str, Any]) -> str:
    return "; not measured" if _unmeasured_terms(entry) else ""


def _unmeasured_terms(entry: dict[str, Any]) -> frozenset[str]:
    observed = entry.get("observed")
    unmeasured = isinstance(observed, dict) and observed.get("scope_compatible") is False

    return frozenset({"not_measured"}) if unmeasured else frozenset()


def _observed_lines(entry: dict[str, Any], child_distinct: int | None) -> list[Rendered]:
    """SPEC 2.3.10: what joining across this edge costs, beside its declared shape.

    `child_distinct` is the referencing column's own distinct count: comparing all of it is exact.
    """

    observed = entry.get("observed")

    if not isinstance(observed, dict):
        return []

    if observed.get("scope_compatible") is False:
        return []

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
    """`_load_artifact`, withholding a body that fails admission (SPEC 2.7) with its reason.

    Each call returns a mapping of its own, so a reply never holds one object in two places.
    """

    read = _reader(read)
    data, reason = read.once(("annotations", path, kind), lambda: _admission(path, kind, read))

    return (authored.iso_dates(data) if data is not None else None), reason


def load_connection_notes(
    print_root: Path,
    read: ArtifactReader,
) -> tuple[str | None, str | None]:
    """`manifest.annotations.yaml`'s `notes` field (SPEC 2.7.3) and why it is corrupt, if it is -
    `(None, None)` covers absent and empty alike; a non-`None` reason is present but unreadable.
    """

    read = _reader(read)

    return read.once(("connection_notes", print_root), lambda: _connection_notes(print_root, read))


def _connection_notes(print_root: Path, read: PrintReader) -> tuple[str | None, str | None]:
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


def _without_rejected_fields(
    statistics: dict[str, Any] | None,
    annotations: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    columns = (statistics or {}).get("columns")

    if not isinstance(columns, dict):
        return statistics

    shown = {
        name: without_rejected(col, rejected_fields(annotations.get(name)))
        if isinstance(col, dict)
        else col
        for name, col in columns.items()
    }

    if all(shown[name] is columns[name] for name in columns):
        return statistics

    return {**(statistics or {}), "columns": shown}


def _without_rejections(annotations: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        name: kept
        for name, entry in annotations.items()
        if (kept := {key: value for key, value in entry.items() if key != "rejected"})
        or "rejected" not in entry
    }


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
    read: PrintReader,
) -> TableArtifacts:
    """Read every available per-table artifact off disk; tolerate missing optional pieces."""

    entry = _entries(manifest, read).get(fqn) or {}
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
        statistics = _without_rejected_fields(statistics, annotations)
        annotations = _without_rejections(annotations)

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

    lineage = _lineage(annotations, statistics)
    inherited = _inherited_notes(manifest, print_root, statistics, lineage, read)
    source_labels = {s["source"] for sources in inherited.values() for s in sources}
    sources = {
        (source["table"], source["column"])
        for listed in lineage.values()
        for source in (listed if isinstance(listed, list) else [])
        if isinstance(source, dict)
        and isinstance(source.get("table"), str)
        and isinstance(source.get("column"), str)
        and _column_label(source["table"], source["column"]) in source_labels
    }
    spellings, spelling_files = _column_spellings(
        manifest,
        print_root,
        fqn,
        statistics,
        _named_columns(shown, sources),
        read,
    )

    return TableArtifacts(
        shared_notes=load_connection_notes(print_root, read)[0],
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
        profiled_at=None if (stamp := entry.get("profiled_at")) is None else str(stamp),
        incoming_cardinalities=_incoming_cardinalities(manifest, print_root, relationships, read),
        inherited=inherited,
        spellings=spellings,
        source_labels={
            label: _column_label(table, spellings[table][column])
            for table, column in sources
            if column in spellings.get(table, {})
            and (label := _column_label(table, column)) in source_labels
        },
        sources=sources,
        spelling_files=spelling_files,
    )


def _lineage(
    annotations: dict[str, dict[str, Any]] | None,
    statistics: dict[str, Any] | None,
) -> dict[str, Any]:
    """Each column's `derived_from`; an annotation's replaces the parsed list, even empty."""

    columns = (statistics or {}).get("columns")
    lineage = {
        name: column_value(col, "derived_from")
        for name, col in (columns.items() if isinstance(columns, dict) else [])
        if isinstance(col, dict) and column_value(col, "derived_from") is not None
    }

    return lineage | {
        name: entry["derived_from"]
        for name, entry in (annotations or {}).items()
        if "derived_from" in entry
    }


def _inherited_notes(
    manifest: dict[str, Any],
    print_root: Path,
    statistics: dict[str, Any] | None,
    lineage: dict[str, Any],
    read: PrintReader,
) -> dict[str, list[dict[str, Any]]]:
    """Each column's `derived_from` sources that carry a note or value notes, one step deep; a
    source's value notes are withheld where it or the column is redacted (SPEC 2.7.1).
    """

    entries = _entries(manifest, read)
    own = (statistics or {}).get("columns")
    own = own if isinstance(own, dict) else {}
    parsed: dict[str, tuple[dict[str, dict[str, Any]], dict[str, Any]]] = {}
    out: dict[str, list[dict[str, Any]]] = {}

    for name, sources in lineage.items():
        for source in sources if isinstance(sources, list) else []:
            table = source.get("table") if isinstance(source, dict) else None
            column = source.get("column") if isinstance(source, dict) else None

            if not isinstance(table, str) or not isinstance(column, str) or table not in entries:
                continue

            if table not in parsed:
                parsed[table] = _live_source_annotations(print_root, table, entries[table], read)

            annotated, measured = parsed[table]
            noted = annotated.get(column)

            if not isinstance(noted, dict) or not _has_meaning(noted):
                continue

            note = noted.get("note")
            values = [
                {"value": v.get("value"), "note": v["note"]}
                for v in noted.get("values") or []
                if isinstance(v, dict)
                and isinstance(v.get("note"), str)
                and v["note"].strip()
                and not _redacted(own.get(name))
                and not _redacted(measured.get(column))
            ]

            if not values and not (isinstance(note, str) and note.strip()):
                continue

            out.setdefault(name, []).append(
                {
                    "source": _column_label(table, column),
                    **({"note": note} if isinstance(note, str) and note.strip() else {}),
                    **({"values": values} if values else {}),
                },
            )

    return out


def _column_label(table: str, column: str) -> str:
    return f"{table}.{column}"


def _named_columns(
    relationships: dict[str, Any] | None,
    sources: set[tuple[str, str]],
) -> dict[str, set[str]]:
    named: dict[str, set[str]] = {}

    for kind, table_key, columns_key in (
        ("refers_to", "target_table", "target_column"),
        ("referenced_by", "referencer_table", "referencer_column"),
    ):
        for edge in (relationships or {}).get(kind) or []:
            table = edge.get(table_key) if isinstance(edge, dict) else None
            columns = edge.get(columns_key) if isinstance(edge, dict) else None

            if isinstance(table, str) and isinstance(columns, list):
                named.setdefault(table, set()).update(str(c) for c in columns)

    for table, column in sources:
        named.setdefault(table, set()).add(column)

    return named


def _named_spellings(a: TableArtifacts, options: AssemblyOptions) -> dict[str, dict[str, str]]:
    named = _named_columns(
        a.relationships if options.include_relationships else None,
        a.sources if options.include_annotations else set(),
    )

    return {
        table: kept
        for table, spelled in a.spellings.items()
        if (
            kept := {
                k: v for k, v in spelled.items() if table == a.fqn or k in named.get(table, ())
            }
        )
    }


def _column_spellings(
    manifest: dict[str, Any],
    print_root: Path,
    fqn: str,
    statistics: dict[str, Any] | None,
    named: dict[str, set[str]],
    read: PrintReader,
) -> tuple[dict[str, dict[str, str]], tuple[Path, ...]]:
    """Every own column and each `named` other one `physical_name` spells otherwise (SPEC 2.2.4)."""

    entries = _entries(manifest, read)
    out = {fqn: own} if (own := _spelled(statistics)) else {}
    consulted: list[Path] = []

    for table in sorted(named):
        name = declared_artifacts(entries[table]).get("statistics") if table in entries else None

        if table == fqn or name is None:
            continue

        path = table_directory(print_root, table, entries[table]) / name
        consulted.append(path)
        spelled = {
            key: spelling
            for key, spelling in _spelled(_load_artifact(path, read)[0]).items()
            if key in named[table]
        }

        if spelled:
            out[table] = spelled

    return out, tuple(consulted)


def _spelled(statistics: dict[str, Any] | None) -> dict[str, str]:
    columns = (statistics or {}).get("columns")

    return {
        key: spelling
        for key, col in (columns.items() if isinstance(columns, dict) else [])
        if isinstance(col, dict)
        and isinstance(spelling := column_value(col, "physical_name"), str)
        and spelling
        and spelling != key
    }


def _live_source_annotations(
    print_root: Path,
    table: str,
    entry: dict[str, Any],
    read: ArtifactReader,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    artifacts = declared_artifacts(entry)
    directory = table_directory(print_root, table, entry)

    if "statistics_annotations" not in artifacts:
        return {}, {}

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

    columns = (statistics or {}).get("columns")

    return (
        table_readings.live_annotations(annotated, statistics),
        columns if isinstance(columns, dict) else {},
    )


def _incoming_cardinalities(
    manifest: dict[str, Any],
    print_root: Path,
    relationships: dict[str, Any] | None,
    read: PrintReader,
) -> dict[tuple[Any, str], int]:
    """The referencing column's distinct count behind each compared incoming edge.

    A referencer missing or unreadable leaves its edges out.
    """

    entries = _entries(manifest, read)
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

    read = _reader(read)
    entries = _entries(manifest, read)
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

    read = _reader(read)
    files = read.once(
        ("relationships_annotations_files", print_root),
        lambda: _relationships_annotations_files(manifest, print_root, read),
        owner=manifest,
    )
    out = {
        table: _relationship_annotation_entries(
            load_annotations(path, "relationships_annotations", read)[0],
        )
        for table, path in files
    }

    return out, tuple(path for _, path in files)


def _relationships_annotations_files(
    manifest: dict[str, Any],
    print_root: Path,
    read: PrintReader,
) -> tuple[tuple[str, Path], ...]:
    files: list[tuple[str, Path]] = []

    for table, entry in sorted(_entries(manifest, read).items()):
        name = declared_artifacts(entry).get("relationships_annotations")

        if name is not None:
            files.append((table, table_directory(print_root, table, entry) / name))

    return tuple(files)


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

    read = _reader(read)
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
    payload = _payload_from(header, candidates, budget)

    # PyYAML writes an object two tables share as an anchor, so each YAML payload gets its own.
    return copy.deepcopy(payload) if options.format == "yaml" else payload


def _structured_parts(
    a: TableArtifacts,
    options: AssemblyOptions,
) -> tuple[dict[str, Any], list[tuple[str, Any]]]:
    header: dict[str, Any] = {"table": a.fqn, "type": a.table_type, "columns_count": a.column_count}

    if a.row_count is not None:
        header["row_count"] = a.row_count

    if a.profiled_at is not None:
        header["profiled_at"] = a.profiled_at

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

    if (scope := scope_of(a.statistics)) is not None:
        header["notes"] = {"scope": FIELD_NOTES[_scope_kind(scope)]}

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

    if notes := _statistics_notes(dict(candidates).get("statistics")):
        header["notes"] = {**header.get("notes", {}), **notes}

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

    if "statistics" not in included_names and "notes" in payload:
        payload["notes"] = {k: v for k, v in payload["notes"].items() if k not in _STATISTICS_NOTES}

        if not payload["notes"]:
            del payload["notes"]

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

    if spellings := _named_spellings(a, options):
        candidates.append(("physical_names", spellings))

    if options.include_annotations and a.connection_notes:
        candidates.append(("connection_notes", a.connection_notes))

    values = _structured_values(a, notes=options.include_annotations)

    if values:
        candidates.append(("values", values))

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

    unshown = (
        {
            name: [
                {"value": noted.value, "note": noted.note}
                | ({"not_in_rows_read": True} if noted.absent else {})
                for noted in notes
            ]
            for name, notes in _unshown_value_notes(a).items()
        }
        if options.include_annotations
        else {}
    )

    if unshown:
        candidates.append(("value_notes", unshown))

    if options.include_annotations and a.inherited:
        candidates.append(("inherited_notes", a.inherited))

    if options.include_description and a.description is not None:
        candidates.append(("description", a.description))

    if options.include_relationships and a.relationships is not None:
        candidates.append(("joins", _structured_joins(a)))

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
    """The named keys of an edge as the artifact spells them, its detection, and `no_match`."""

    edge = {key: entry[key] for key in keys if key in entry}
    edge["detection"] = edge_detection(entry)

    if no_match(entry):
        edge["no_match"] = True

    return edge


def _structured_values(a: TableArtifacts, *, notes: bool) -> dict[str, Any]:
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
            block["counts"] = [count_of(e) for e in shown]
        else:
            block["entries"] = _structured_entries(shown, annotations.get(name) if notes else None)

        if name in shares:
            block["null_rate"] = shares[name][1]

        if listed is not None and _codes_undocumented(a, name, col, listed[1]):
            block["meanings_documented"] = False

        out[name] = block

    return out


def _structured_entries(entries: list[Any], annotation: dict[str, Any] | None) -> list[Any]:
    notes = value_notes(annotation)
    rendered = []

    for entry in entries:
        if not isinstance(entry, dict):
            continue

        item = {"value": entry.get("value"), "count": count_of(entry)}
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


_STATISTICS_NOTES = ("unmeasured", "disabled", "sensitivity")


def _scope_kind(scope: ScanScope) -> str:
    if scope.sample is not None:
        return "sample"
    elif scope.filter is not None and scope.filter.strip():
        return "filter"
    else:
        return "scanned"


def _statistics_notes(statistics: dict[str, Any] | None) -> dict[str, str]:
    if statistics is None:
        return {}

    columns = statistics.get("columns")
    columns = (
        [c for c in columns.values() if isinstance(c, dict)] if isinstance(columns, dict) else []
    )
    held = {
        "unmeasured": bool(block_value(statistics, "unmeasured"))
        or any(column_value(c, "unmeasured") for c in columns),
        "disabled": bool(disabled_of(statistics)),
        "sensitivity": any(not column_value(c, "inferred.sensitivity") for c in columns),
    }

    return {key: FIELD_NOTES[key] for key in _STATISTICS_NOTES if held[key]}


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


def _reader(read: ArtifactReader) -> PrintReader:
    return read if isinstance(read, PrintReader) else PrintReader(read)


def _entries(manifest: dict[str, Any], read: PrintReader) -> dict[str, dict[str, Any]]:
    return read.once("walkable_tables", lambda: walkable_tables(manifest), owner=manifest)


def _admission(
    path: Path,
    kind: str,
    read: ArtifactReader,
) -> tuple[dict[str, Any] | None, str | None]:
    data, reason = _load_artifact(path, read)

    if data is not None and (problem := authored.malformation(kind, data)) is not None:
        return None, f"does not match its schema (SPEC 2.7): {problem}"

    return data, reason
