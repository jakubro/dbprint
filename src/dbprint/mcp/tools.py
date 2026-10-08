"""MCP tool implementations per MCP.md 4."""

from __future__ import annotations

import fnmatch
import importlib.resources
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast, get_args

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from dbprint.config import ConnectionConfig
from dbprint.engine import (
    AssemblyOptions,
    Purpose,
    context_sections,
    context_terms,
    structured_context_sections,
    thresholds,
    value_resolution,
)
from dbprint.engine.baseline import (
    failed_tables,
    read_manifest,
    table_directory,
)
from dbprint.engine.context_assembler import incoming_rejections
from dbprint.engine.freshness import classify
from dbprint.engine.table_readings import live_annotations
from dbprint.engine.value_list import value_notes
from dbprint.spec.absence import Absence, column_value, read_column_field
from dbprint.spec.artifacts import (
    DIFF_FILENAME,
    MANIFEST_FILENAME,
    declared_artifacts,
    walkable_tables,
)
from dbprint.spec.classification import Classification
from dbprint.spec.looks_like import LooksLike
from dbprint.spec.parts import display
from dbprint.spec.redaction import Primitive as RedactionPrimitive
from dbprint.spec.scope import ScanScope, list_is_complete, reply_scope, rows_scanned, scope_of
from dbprint.spec.sensitivity import Sensitivity
from . import errors, paging, reference
from .reference import ReferenceDocument
from .resources import diff_without_rejected, parsed_mapping
from .state import ServedConnections


_CURSOR_PROPERTY: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "`next_cursor` from this call's previous page; omit for the first page",
}

# The three relationship events carry `source_table`/`target_table` where every other event
# carries `table`; a filter reading one field alone drops them silently (engine/diff.py).
_DIFF_TABLE_FIELDS = ("table", "source_table", "target_table")

_MANIFEST_FILTERED_KEYS = frozenset({"tables", "failed_tables"})

_SEARCHED_KINDS = ("statistics", "statistics_annotations")

_PER_COLUMN_SECTIONS = frozenset({"annotations", "values", "dictionary"})

_RESOLUTION_LISTS = ("spellings", "candidates", "domain")

_DIFF_KINDS: list[str] = json.loads(
    importlib.resources.files("dbprint.spec.v1").joinpath("diff.schema.json").read_text("utf-8"),
)["$defs"]["Kind"]["enum"]


TOOL_NAMES = (
    "get_table_context",
    "list_tables",
    "search_columns",
    "resolve_value",
    "get_manifest",
    "get_diff",
    "get_reference",
)


@dataclass(frozen=True)
class ToolDef:
    """Static tool definition advertised in tools/list."""

    name: str
    description: str
    input_schema: dict[str, Any]


TOOL_DEFINITIONS: tuple[ToolDef, ...] = (
    ToolDef(
        name="get_table_context",
        description=(
            "Read one table. Call it before writing SQL against a table, with "
            "`purpose: query`: DDL, the Joins list (every edge relationships.yaml carries - "
            "declared, inferred or measured - except edges a human rejected; a join described "
            "only in the table's description is not in it), a data dictionary, the value "
            "lists a predicate is written from, with their counts and coverage, and each "
            "nullable column's null share. Call it with the default `purpose: profile` to "
            "describe the data - statistics such as null "
            "rates, cardinality and ranges, relationships, the description and notes. "
            "Use search_columns or list_tables first when the table is not yet known, "
            "and resolve_value to check how one phrase is spelled in one column. Paged: md "
            "and yaml end every page but the last with a `next_cursor` line, json carries "
            "it as a key; a section too long for one page continues on the next, never "
            "dropped. Under `profile`, md Notes summarise each column - a long value list "
            "shows its 5 most frequent values - while json and yaml carry the statistics "
            "whole; for json/yaml a `_corrupted` field names any declared artifact that "
            "failed to parse."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "table": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Dotted fully-qualified table name, as list_tables and "
                        "search_columns return it"
                    ),
                },
                "connection": {
                    "type": "string",
                    "description": (
                        "Connection name from .dbprint.yaml; omit it to use the server's "
                        "default connection"
                    ),
                },
                "purpose": {
                    "type": "string",
                    "enum": ["profile", "query"],
                    "default": "profile",
                    "description": (
                        "query before writing SQL: DDL, the Joins list, data dictionary and "
                        "the value lists with counts and coverage, and each nullable column's "
                        "null share, with no other statistics. "
                        "profile (default) to describe the data: statistics, relationships, "
                        "notes"
                    ),
                },
                "format": {
                    "type": "string",
                    "enum": ["md", "json", "yaml"],
                    "default": "md",
                    "description": (
                        "md renders the chosen purpose as Markdown - under `profile`, a "
                        "per-column Notes summary rather than the raw statistics fields "
                        "json and yaml carry. All three omit each column's sketch payload; "
                        "the verbatim statistics.yaml, sketch included, is reachable as the "
                        "dbprint://<connection>/<table>/statistics resource. get_reference "
                        "document: guide explains each json/yaml field."
                    ),
                },
                "include_stats": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Include the Cardinality table (md) or statistics object (json/yaml); "
                        "no effect under `query`, which carries neither"
                    ),
                },
                "include_relationships": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Include the Relationships section (md) or relationships object "
                        "(json/yaml); under `query`, the Joins list"
                    ),
                },
                "include_description": {
                    "type": "boolean",
                    "default": True,
                    "description": "Include the table's description.md, when authored",
                },
                "include_annotations": {
                    "type": "boolean",
                    "default": True,
                    "description": "Include statistics.annotations.yaml notes and claims, when authored",
                },
                "cursor": _CURSOR_PROPERTY,
            },
            "required": ["table"],
        },
    ),
    ToolDef(
        name="list_tables",
        description=(
            "List a connection's tables, optionally narrowed by an fnmatch pattern over "
            "the dotted names. Use it to see which tables exist, their row counts, and "
            "whether each table's statistics are stale: `detail: true` adds each "
            "table's type, row_count, columns and profiled_at, and the freshness verdict "
            "`dbprint list` and `dbprint check` give it - `live`, `stale`, or `dormant` "
            "when profiled_at is unreadable - with its age_days and the max_age_days it "
            "is judged against. A table whose threshold cannot be resolved carries "
            "`threshold_error` instead of a verdict. To find columns rather than tables, "
            "use search_columns; for the raw manifest index, get_manifest. "
            "Paged: every reply carries the `total` matched, and `next_cursor` while more remain."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "connection": {
                    "type": "string",
                    "description": (
                        "Connection name from .dbprint.yaml; omit it to use the server's "
                        "default connection"
                    ),
                },
                "pattern": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "fnmatch glob over dotted table names, e.g. 'sales.*'; defaults to '*'"
                    ),
                },
                "detail": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Project each entry's type/row_count/columns/profiled_at from "
                        "the manifest plus its freshness verdict; false returns bare FQN "
                        "strings, unchanged"
                    ),
                },
                "cursor": _CURSOR_PROPERTY,
            },
        },
    ),
    ToolDef(
        name="search_columns",
        description=(
            "Find columns across every table - the first call when the table holding a "
            "fact is not yet known. Filters, all optional and ANDed: a name glob, a `text` "
            "search over column names and their notes, and classification/sql_type/"
            "sensitivity/looks_like/redacted globs and a candidate_key match - for "
            "example every column holding an email address (`looks_like: email`) or "
            "contact details (`sensitivity: contact`). Read a table found this way with "
            "get_table_context; list tables rather than columns with list_tables. A match on a table read in part "
            "carries its `scope` block with rows_scanned/row_count, so a caller can tell a "
            "scanned-set number or key from a table-wide one; a match carrying a looks_like verdict carries the "
            "sampled/matched draw behind it, since a verdict from two values reads "
            "identically to one from ten thousand otherwise. A match carries "
            "sensitivity/redacted/candidate_key (and candidate_key_exception where the "
            "ratio falls short of 1.0) whenever the column does, so filtering on any of "
            "them returns the matched category, not just a bare column name. Paged: every "
            "reply carries the `total` matched, and `next_cursor` while more remain. A "
            "result carries `unreadable_tables` only when a table's own statistics or "
            "annotations failed to parse, on its first page: a statistics failure drops "
            "that table's columns from `matches` entirely; an annotations-only failure "
            "still returns them, without the annotation."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "pattern": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "fnmatch glob over column names, and over a part as `<column><path>` "
                        "(`items[*].sku`); optional - omit to filter by the other predicates alone"
                    ),
                },
                "classification": {
                    "type": "string",
                    "description": (
                        f"fnmatch glob against the column's classification "
                        f"({', '.join(sorted(get_args(Classification)))})"
                    ),
                },
                "sql_type": {
                    "type": "string",
                    "description": "fnmatch glob against the column's sql_type",
                },
                "sensitivity": {
                    "type": "string",
                    "description": (
                        f"fnmatch glob against inferred.sensitivity "
                        f"({', '.join(sorted(get_args(Sensitivity)))}) - a glob of '*' "
                        f"sweeps every column carrying any detection. A detection, never "
                        f"a verdict; its absence on a column is not an assertion that "
                        f"the column is safe"
                    ),
                },
                "looks_like": {
                    "type": "string",
                    "description": (
                        f"fnmatch glob against inferred.looks_like "
                        f"({', '.join(sorted(get_args(LooksLike)))})"
                    ),
                },
                "redacted": {
                    "type": "string",
                    "description": (
                        f"fnmatch glob against the column's redacted marker "
                        f"({', '.join(sorted(get_args(RedactionPrimitive)))})"
                    ),
                },
                "candidate_key": {
                    "type": "boolean",
                    "description": "Exact match against inferred.candidate_key",
                },
                "text": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Case-insensitive substring matched against the column name, its "
                        "annotation note and its per-value notes - words, not meaning. A "
                        "match found through per-value notes carries them as `value_notes`"
                    ),
                },
                "connection": {
                    "type": "string",
                    "description": (
                        "Connection name from .dbprint.yaml; omit it to use the server's "
                        "default connection"
                    ),
                },
                "cursor": _CURSOR_PROPERTY,
            },
        },
    ),
    ToolDef(
        name="resolve_value",
        description=(
            "Resolve a phrase, a code or a spelling against one column's published "
            "values, before writing a literal into a filter whose stored spelling "
            "get_table_context does not already list in full. Answers, in order of "
            "preference: `stored` - the text is a listed value, or folds to one, and "
            "the reply carries the spelling a predicate must use; `definition` - the "
            "text names what a value's note says it means; `nearest` - the listed "
            "values closest to the text, ranked; `none`; or `unavailable` where the "
            "column publishes no value list, lost it this run, or redacts it. Every "
            "reply carries the column's `coverage` and how many values the print "
            "`listed`, plus a caveat sentence wherever that list is a sample - a "
            "spelling absent from a sample is not evidence it is absent from the "
            "column. An exhaustive list of at most fifty values rides along whole as "
            "`domain`, so a small vocabulary needs one call; on a table read in part, "
            "`exhaustive` is false, the reply carries the table's `scope` and "
            "`row_count`, and `domain` is the scanned rows' whole domain. Paged: every "
            "page repeats the scalar keys; the answer list comes before `domain`."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "table": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Fully-qualified table name",
                },
                "column": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Column name as the print spells it",
                },
                "part": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "A part of the column, by its path as the print keys it (`.status`, "
                        "`[*]`); omit it to resolve against the column itself"
                    ),
                },
                "text": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The phrase, code or spelling from the question, as written",
                },
                "connection": {
                    "type": "string",
                    "description": (
                        "Connection name from .dbprint.yaml; omit it to use the server's "
                        "default connection"
                    ),
                },
                "cursor": _CURSOR_PROPERTY,
            },
            "required": ["table", "column", "text"],
        },
    ),
    ToolDef(
        name="get_manifest",
        description=(
            "Return the parsed manifest.yaml for a connection - an index of "
            "tables and their artifacts, not a semantic catalogue of what they mean. "
            "Use it for a manifest field no other tool projects; for table names, row "
            "counts and staleness, list_tables with `detail: true` is shorter and "
            "already judges each table's freshness. "
            "The `tables` map is paged in FQN order with `total` and `next_cursor`; every "
            "other key of the document rides the first page whole, `failed_tables` "
            "filtered by `pattern` like `tables`."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "connection": {
                    "type": "string",
                    "description": (
                        "Connection name from .dbprint.yaml; omit it to use the server's "
                        "default connection"
                    ),
                },
                "pattern": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "fnmatch glob over the FQN keys of `tables`, the same spelling "
                        "`list_tables` takes; filters that map and `failed_tables` only"
                    ),
                },
                "cursor": _CURSOR_PROPERTY,
            },
        },
    ),
    ToolDef(
        name="get_diff",
        description=(
            "Answer what changed between the connection's last two generate runs: "
            "the parsed diff.yaml, whose `summary` counts every kind of change and whose "
            "`changes` list names each one - tables and columns added, removed or "
            "retyped, relationships changed, statistics that drifted. A statistic with "
            "no drift event did not change between the two runs; a table "
            "counted in `unevaluated_tables` was not compared. The `changes` list is paged in "
            "file order with `total` and `next_cursor`; every other key of the document rides "
            "the first page whole. "
            "A table's current state, rather than what changed, is get_table_context's answer."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "connection": {
                    "type": "string",
                    "description": (
                        "Connection name from .dbprint.yaml; omit it to use the server's "
                        "default connection"
                    ),
                },
                "table": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Keep only changes naming this fully-qualified table, including the "
                        "relationship events that name it as source or target"
                    ),
                },
                "kind": {
                    "type": "string",
                    "enum": _DIFF_KINDS,
                    "description": "Keep only changes of this kind",
                },
                "cursor": _CURSOR_PROPERTY,
            },
        },
    ),
    ToolDef(
        name="get_reference",
        description=(
            "Look up the dbprint format specification or the assertion DSL "
            "specification by section number, or the reading guide by heading: what a "
            "print's field means, or what a finding's spec_ref (e.g. '§2.2.4') refers to. "
            "A section returns its own text and lists its direct subsections, each read "
            "by its own number or heading; omit section for the heading tree. The guide "
            "is the one this dbprint version ships, not a print's own reading.md. Paged "
            "like get_table_context's md. Depends on no connection or print; what one "
            "print's tables hold is get_table_context's and list_tables' answer."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "document": {
                    "type": "string",
                    "enum": ["assertions", "guide", "spec"],
                    "description": (
                        "Which document - the format spec, the assertion DSL, or the reading "
                        "guide (how to read each json/yaml field)"
                    ),
                },
                "section": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "A section number in the document's own scheme (e.g. '3', '2.2.4'), or "
                        "a spec_ref citation copied verbatim from a finding ('§2.2.4', "
                        "'ASSERTIONS.md §1.4') - any heading depth. Omit for the table of "
                        "contents. For guide, a heading's text (e.g. 'Vocabulary'), matched "
                        "case-insensitively."
                    ),
                },
                "cursor": _CURSOR_PROPERTY,
            },
            "required": ["document"],
        },
    ),
)


def list_page(cursor: str | None) -> tuple[list[ToolDef], str | None]:
    """The `tools/list` page `cursor` points at, and the cursor to the next when one exists."""

    def render(units: Sequence[Any], first: bool, next_cursor: str | None) -> dict[str, Any]:
        del first
        reply: dict[str, Any] = {
            "tools": [
                {"name": d.name, "description": d.description, "inputSchema": d.input_schema}
                for _, d in (paging.entry(unit) for unit in units)
            ],
        }

        if next_cursor is not None:
            reply["nextCursor"] = next_cursor

        return reply

    call = paging.Call("tools/list", {}, ())
    reply = paging.page(call, list(enumerate(TOOL_DEFINITIONS)), render, cursor)
    by_name = {definition.name: definition for definition in TOOL_DEFINITIONS}

    return [by_name[tool["name"]] for tool in reply["tools"]], reply.get("nextCursor")


def dispatch(
    state: ServedConnections,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any] | str:
    """Route a tool call; `get_table_context` returns a bare string for md/yaml (MCP.md 4.1).

    The pinned SDK checks no arguments, so every call is validated against the tool's own schema.
    """

    definition = next((t for t in TOOL_DEFINITIONS if t.name == name), None)

    if definition is None:
        raise errors.unknown_tool(name, list(TOOL_NAMES))

    arguments = _validated(definition, arguments)

    if name == "get_table_context":
        return _tool_get_table_context(state, arguments)

    if name == "list_tables":
        return _tool_list_tables(state, arguments)

    if name == "search_columns":
        return _tool_search_columns(state, arguments)

    if name == "resolve_value":
        return _tool_resolve_value(state, arguments)

    if name == "get_manifest":
        return _tool_get_manifest(state, arguments)

    if name == "get_diff":
        return _tool_get_diff(state, arguments)

    return _tool_get_reference(arguments)


# Deliberate: `format`/`purpose` fold before validation; `get_reference`'s `document` does not.
_CASE_FOLDED_ARGUMENTS = frozenset({"format", "purpose"})


def _validated(definition: ToolDef, arguments: dict[str, Any]) -> dict[str, Any]:
    """Case-fold what folds, then check the call against the tool's own declared schema."""

    folded = {
        key: value.lower() if key in _CASE_FOLDED_ARGUMENTS and isinstance(value, str) else value
        for key, value in arguments.items()
    }
    validator = Draft202012Validator(definition.input_schema)
    violations = sorted(validator.iter_errors(folded), key=lambda e: list(e.absolute_path))

    if violations:
        raise errors.invalid_argument(_argument_fault(definition, violations[0]))

    return folded


def _argument_fault(definition: ToolDef, error: ValidationError) -> str:
    """One violation as the caller needs it: the key, what arrived, and what is accepted."""

    properties = definition.input_schema["properties"]

    if error.validator == "additionalProperties":
        unknown = sorted(set(error.instance) - set(properties))

        return (
            f"{definition.name} takes no argument {unknown[0]!r}. Accepted: {sorted(properties)}."
        )

    if error.validator == "required":
        missing = error.message.split("'")[1]

        return f"{definition.name} requires {missing!r}."

    key = str(next(iter(error.absolute_path))) if error.absolute_path else "(argument)"
    schema = properties.get(key, {})

    if error.validator == "enum":
        return f"{key} {error.instance!r} must be one of {schema['enum']}."

    if error.validator == "minLength":
        return f"{key} {error.instance!r} must be a non-empty string."

    return f"{key} {error.instance!r} must be of type {schema.get('type', 'the declared type')}."


def _tool_get_table_context(
    state: ServedConnections,
    arguments: dict[str, Any],
) -> dict[str, Any] | str:
    conn = state.resolve(arguments.get("connection"))
    table = arguments["table"]
    manifest = _load_manifest(state, conn)
    entry = (manifest.get("tables") or {}).get(table) if manifest else None

    if manifest is None or entry is None:
        raise _absent_table(table, conn.name, manifest)

    fmt = arguments.get("format", "md")
    purpose = cast(Purpose, arguments.get("purpose", "profile"))
    options = AssemblyOptions(
        format=fmt,
        purpose=purpose,
        include_ddl=True,
        include_description=bool(arguments.get("include_description", True)),
        include_annotations=bool(arguments.get("include_annotations", True)),
        include_stats=bool(arguments.get("include_stats", True)),
        include_relationships=bool(arguments.get("include_relationships", True)),
    )
    print_root = conn.print_root
    table_dir = table_directory(print_root, table, entry)
    artifacts = declared_artifacts(entry)
    relationships = (
        parsed_mapping(table_dir / artifacts["relationships"], state.files.read)
        if "relationships" in artifacts
        else None
    )
    _, referencers = incoming_rejections(
        manifest,
        print_root,
        table,
        relationships,
        state.files.read,
    )
    files = (
        print_root / MANIFEST_FILENAME,
        *(table_dir / name for name in artifacts.values()),
        *referencers,
    )
    call = paging.Call("get_table_context", arguments, files)

    # MCP.md 4.1: json returns the structured object, yaml that object as text, md markdown.
    # `_missing`/`_corrupted` are the assembler's own - carried in the payload or header,
    # never recomputed here: one computation, one answer on every format.
    if options.format == "md":
        sections = context_sections(manifest, print_root, table, options, read=state.files.read)

        return paging.legend_text_page(
            call,
            [(text, line_terms) for _, text, line_terms in sections],
            arguments.get("cursor"),
            _markdown_marker,
            context_terms.legend,
        )

    header, candidates = structured_context_sections(
        manifest,
        print_root,
        table,
        options,
        read=state.files.read,
    )

    def render(units: Sequence[Any], first: bool, next_cursor: str | None) -> dict[str, Any] | str:
        del first
        head, rest = _split_head(units)
        reply = _nested(rest, head)

        if options.format == "yaml":
            text = yaml.safe_dump(reply, sort_keys=False, default_flow_style=False)

            return text if next_cursor is None else f"{text}# next_cursor: {next_cursor}\n"

        if next_cursor is not None:
            reply["next_cursor"] = next_cursor

        return reply

    return _page(call, _context_units(candidates), render, arguments, head=header)


def _context_units(candidates: list[tuple[str, Any]]) -> list[tuple[tuple[str, ...], Any]]:
    units: list[tuple[tuple[str, ...], Any]] = []

    for name, value in candidates:
        if name == "statistics" and isinstance(value.get("columns"), dict) and value["columns"]:
            units.append(((name,), {k: v for k, v in value.items() if k != "columns"}))
            units.extend(((name, "columns", col), v) for col, v in value["columns"].items())
        elif name in _PER_COLUMN_SECTIONS and isinstance(value, dict) and value:
            units.extend(((name, col), v) for col, v in value.items())
        else:
            units.append(((name,), value))

    return units


def _nested(units: Sequence[Any], reply: dict[str, Any]) -> dict[str, Any]:
    for path, value in (paging.entry(unit) for unit in units):
        target = reply

        for key in path[:-1]:
            target = target.setdefault(key, {})

        if isinstance(value, dict) and isinstance(target.get(path[-1]), dict):
            target[path[-1]].update(value)
        else:
            target[path[-1]] = dict(value) if isinstance(value, dict) else value

    return reply


def _markdown_marker(cursor: str) -> str:
    return f"\n<!-- next_cursor: {cursor} -->"


def _tool_list_tables(state: ServedConnections, arguments: dict[str, Any]) -> dict[str, Any]:
    conn = state.resolve(arguments.get("connection"))
    pattern = str(arguments.get("pattern") or "*")
    detail = bool(arguments.get("detail"))
    manifest = _load_manifest(state, conn) or {}
    entries = walkable_tables(manifest)
    # fnmatch.fnmatchcase never raises for a string pattern - no parse error to catch.
    matched = sorted(fqn for fqn in entries if fnmatch.fnmatchcase(fqn, pattern))
    failed = [fqn for fqn in failed_tables(manifest) if fnmatch.fnmatchcase(fqn, pattern)]
    items: list[tuple[str, Any]] = [(fqn, fqn) for fqn in matched]
    size_gated: frozenset[str] = frozenset()

    if detail:
        verdicts, gated = _freshness(conn, manifest)
        size_gated = frozenset(gated)
        items = [
            (
                fqn,
                {
                    "table": fqn,
                    "type": entries[fqn].get("type"),
                    "row_count": entries[fqn].get("row_count"),
                    "columns": entries[fqn].get("columns"),
                    "profiled_at": entries[fqn].get("profiled_at"),
                    **verdicts[fqn],
                },
            )
            for fqn in matched
        ]

    def render(units: Sequence[Any], first: bool, next_cursor: str | None) -> dict[str, Any]:
        del first
        reply, rest = _split_head(units)
        rows = [paging.entry(unit) for unit in rest]
        reply["tables"] = [value for _, value in rows]

        if gated_here := [fqn for fqn, _ in rows if fqn in size_gated]:
            reply["warnings"] = [thresholds.size_gate_warning(conn.name, gated_here)]

        return _paged(reply, total=len(matched), next_cursor=next_cursor)

    call = paging.Call("list_tables", arguments, (conn.print_root / MANIFEST_FILENAME,))
    head = {"failed_tables": failed} if failed else {}

    return _page(call, items, render, arguments, head=head)


def _freshness(
    conn: ConnectionConfig,
    manifest: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], tuple[str, ...]]:
    """A refused table gets its refusal and no verdict; the connection default never stands in."""

    resolved = thresholds.resolve(conn, manifest)
    now = datetime.now(UTC)
    tables = manifest.get("tables") or {}
    judged = {fqn: entry for fqn, entry in tables.items() if fqn not in resolved.refused}
    verdicts: dict[str, dict[str, Any]] = {
        fqn: {"threshold_error": cause} for fqn, cause in resolved.refused.items()
    }

    for fqn, judged_table in classify(
        {**manifest, "tables": judged},
        now,
        threshold_for=resolved.threshold_for,
    ).items():
        threshold = judged_table.max_age_days
        verdicts[fqn] = {
            "freshness": judged_table.verdict,
            "age_days": None if judged_table.age_days is None else round(judged_table.age_days, 2),
            "max_age_days": int(threshold) if threshold.is_integer() else threshold,
        }

    return verdicts, resolved.size_gated


@dataclass(frozen=True)
class _ColumnFilters:
    """`search_columns`'s optional predicates, ANDed - an absent (`None`) one always passes."""

    classification: str | None = None
    sql_type: str | None = None
    sensitivity: str | None = None
    looks_like: str | None = None
    redacted: str | None = None
    candidate_key: bool | None = None


def _column_filters(arguments: dict[str, Any]) -> _ColumnFilters:
    return _ColumnFilters(
        classification=arguments.get("classification"),
        sql_type=arguments.get("sql_type"),
        sensitivity=arguments.get("sensitivity"),
        looks_like=arguments.get("looks_like"),
        redacted=arguments.get("redacted"),
        candidate_key=arguments.get("candidate_key"),
    )


def _field_matches(value: Any, glob: str | None) -> bool:
    """An unset `glob` always passes; a set one needs a present `value` to fnmatch against.

    `sensitivity: "*"` sweeps every column carrying any detection, and would match an empty
    string too, so an absent field (`None`) is checked for presence first.
    """

    if glob is None:
        return True

    if value is None:
        return False

    return fnmatch.fnmatchcase(str(value), glob)


def _column_matches(col: dict[str, Any], filters: _ColumnFilters) -> bool:
    if not _field_matches(col.get("classification"), filters.classification):
        return False

    if not _field_matches(col.get("sql_type"), filters.sql_type):
        return False

    if not _field_matches(column_value(col, "inferred.sensitivity"), filters.sensitivity):
        return False

    if not _field_matches(column_value(col, "inferred.looks_like"), filters.looks_like):
        return False

    if not _field_matches(column_value(col, "redacted"), filters.redacted):
        return False

    if filters.candidate_key is None:
        return True

    candidate_key = read_column_field(col, "inferred.candidate_key")

    return candidate_key.known and bool(candidate_key.value) == filters.candidate_key


def _search_match(
    fqn: str,
    entry: dict[str, Any],
    col_name: str,
    col: dict[str, Any],
    annotation: dict[str, Any],
    scope: ScanScope | None,
) -> dict[str, Any]:
    """One `search_columns` match - the artifact's own fields, no re-derivation."""

    match: dict[str, Any] = {
        "table": fqn,
        "column": col_name,
        "sql_type": col.get("sql_type", ""),
        "classification": col.get("classification", ""),
    }

    row_count = entry.get("row_count")

    if row_count is not None:
        match["row_count"] = row_count

    scanned = rows_scanned(col, scope)

    if scanned is not None:
        match["rows_scanned"] = scanned

    match.update(reply_scope(scope))

    # A looks_like verdict from a draw of two reads identically to one from ten
    # thousand without these - carry the evidence, not just the classification.
    looks_like = read_column_field(col, "inferred.looks_like")

    if looks_like.state is Absence.PRESENT:
        match["looks_like"] = looks_like.value

        for key in ("sampled", "matched"):
            if (evidence := column_value(col, f"inferred.{key}")) is not None:
                match[key] = evidence

    # The remaining predicates `_column_matches` filters on - a match needs its category.
    sensitivity = read_column_field(col, "inferred.sensitivity")

    if sensitivity.state is Absence.PRESENT:
        match["sensitivity"] = sensitivity.value

    redacted = column_value(col, "redacted")

    if isinstance(redacted, str) and redacted:
        match["redacted"] = redacted

    if column_value(col, "inferred.candidate_key"):
        match["candidate_key"] = True
        exception = column_value(col, "inferred.candidate_key_exception")

        if exception is not None:
            match["candidate_key_exception"] = exception

    note = annotation.get("note")

    if isinstance(note, str) and note.strip():
        match["annotation"] = note

    return match


def _tool_search_columns(state: ServedConnections, arguments: dict[str, Any]) -> dict[str, Any]:
    pattern = arguments.get("pattern")
    text = arguments.get("text")
    filters = _column_filters(arguments)
    conn = state.resolve(arguments.get("connection"))
    manifest = _load_manifest(state, conn) or {}
    print_root = conn.print_root
    files = [print_root / MANIFEST_FILENAME]

    matches: list[dict[str, Any]] = []
    unreadable: list[str] = []

    for fqn, entry in sorted(walkable_tables(manifest).items()):
        artifacts = declared_artifacts(entry)
        table_dir = table_directory(print_root, fqn, entry)
        files += [table_dir / artifacts[kind] for kind in _SEARCHED_KINDS if kind in artifacts]
        statistics, stats_error = _load_statistics(state, table_dir, artifacts)
        stats_columns = _columns_of(statistics)
        scope = scope_of(statistics)
        annotation_columns, annotation_error = _load_annotation_columns(
            state,
            table_dir,
            artifacts,
        )

        if stats_error is not None or annotation_error is not None:
            unreadable.append(fqn)

        column_names = set(stats_columns) | set(
            live_annotations(annotation_columns, statistics or None),
        )

        for col_name in sorted(column_names):
            col = stats_columns.get(col_name) or {}
            annotation = annotation_columns.get(col_name) or {}
            matches += [
                _search_match(fqn, entry, col_name, col, annotation, scope) | extra
                for extra in _column_hit(col_name, col, annotation, pattern, text, filters)
            ]
            matches += [
                _search_match(fqn, entry, col_name, block, {}, None)
                | {"part": path, "occurrences": column_value(block, "occurrences")}
                for path, block in _parts_of(col).items()
                if _part_hit(display(col_name, path), block, pattern, text, filters)
            ]

    def render(units: Sequence[Any], first: bool, next_cursor: str | None) -> dict[str, Any]:
        del first
        reply, rest = _split_head(units)
        reply["matches"] = [paging.entry(unit)[1] for unit in rest]

        return _paged(reply, total=len(matches), next_cursor=next_cursor)

    call = paging.Call("search_columns", arguments, tuple(files))
    head = {"unreadable_tables": sorted(unreadable)} if unreadable else {}

    return _page(call, list(enumerate(matches)), render, arguments, head=head)


def _column_hit(
    col_name: str,
    col: dict[str, Any],
    annotation: dict[str, Any],
    pattern: str | None,
    text: str | None,
    filters: _ColumnFilters,
) -> list[dict[str, Any]]:
    """The extra fields one matching column's entry carries, as a one-item list; empty on a miss."""

    if pattern is not None and not fnmatch.fnmatchcase(col_name, pattern):
        return []

    if not _column_matches(col, filters):
        return []

    value_notes = _matching_value_notes(annotation, text) if text is not None else []

    if text is not None and not (value_notes or _names_or_notes(col_name, annotation, text)):
        return []

    return [{"value_notes": value_notes} if value_notes else {}]


def _part_hit(
    label: str,
    block: dict[str, Any],
    pattern: str | None,
    text: str | None,
    filters: _ColumnFilters,
) -> bool:
    if pattern is not None and not fnmatch.fnmatchcase(label, pattern):
        return False

    return _column_matches(block, filters) and (text is None or text.casefold() in label.casefold())


def _parts_of(col: dict[str, Any]) -> dict[str, dict[str, Any]]:
    parts = column_value(col, "parts")

    if not isinstance(parts, dict):
        return {}

    return {path: block for path, block in parts.items() if isinstance(block, dict)}


def _names_or_notes(col_name: str, annotation: dict[str, Any], text: str) -> bool:
    note = annotation.get("note")
    needle = text.casefold()

    return needle in col_name.casefold() or (isinstance(note, str) and needle in note.casefold())


def _matching_value_notes(annotation: dict[str, Any], text: str) -> list[dict[str, Any]]:
    entries = annotation.get("values")

    if not isinstance(entries, list):
        return []

    needle = text.casefold()

    return [
        {"value": entry.get("value"), "note": " ".join(entry["note"].split())}
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("note"), str)
        and needle in entry["note"].casefold()
    ]


def _tool_resolve_value(state: ServedConnections, arguments: dict[str, Any]) -> dict[str, Any]:
    """MCP.md 4.7: one column's answer to a phrase, read off the print alone."""

    conn = state.resolve(arguments.get("connection"))
    table = arguments["table"]
    column = arguments["column"]
    text = arguments["text"]
    manifest = _load_manifest(state, conn)
    entry = (manifest.get("tables") or {}).get(table) if manifest else None

    if manifest is None or entry is None:
        raise _absent_table(table, conn.name, manifest)

    artifacts = declared_artifacts(entry)
    table_dir = table_directory(conn.print_root, table, entry)

    files = (conn.print_root / MANIFEST_FILENAME,)

    if "statistics" not in artifacts:
        reply = {
            "table": table,
            "column": column,
            "text": text,
            **value_resolution.resolve(
                text,
                [],
                {},
                coverage=None,
                unavailable_reason=f"table {table!r} declares no statistics artifact",
            ),
        }

        return _paged_resolution(paging.Call("resolve_value", arguments, files), reply, arguments)

    stats_path = table_dir / artifacts["statistics"]

    if not stats_path.is_file():
        raise errors.manifest_references_missing_file(artifacts["statistics"], str(stats_path))

    statistics, stats_error = _load_statistics(state, table_dir, artifacts)

    if stats_error is not None:
        raise errors.yaml_parse_error(str(stats_path), stats_error)

    stats_columns = _columns_of(statistics)

    annotation_columns, annotation_error = _load_annotation_columns(state, table_dir, artifacts)

    if annotation_error is not None:
        annotation_path = table_dir / artifacts["statistics_annotations"]

        raise errors.yaml_parse_error(str(annotation_path), annotation_error)

    if column not in stats_columns:
        raise errors.unknown_column(column, table, sorted(stats_columns))

    col = stats_columns[column] or {}
    part = arguments.get("part")
    scope = scope_of(statistics)

    if part is not None:
        parts = _parts_of(col)

        if part not in parts:
            raise errors.unknown_part(part, column, table, sorted(parts))

        # A part counts over its own occurrences, never the table's scanned rows (SPEC 2.2.18).
        col, scope = parts[part], None

    values = read_column_field(col, "values")
    entries = values.value if isinstance(values.value, list) else []
    redaction = column_value(col, "redacted")
    reason = None

    if values.state is Absence.UNMEASURED:
        reason = f"column {column!r} values are unmeasured: {values.cause}"
    elif values.state is not Absence.PRESENT:
        reason = f"column {column!r} publishes no values: {values.cause}"
    elif isinstance(redaction, str) and redaction:
        reason = f"column {column!r} is redacted ({redaction}), so its values are withheld"
    elif scope is not None and scope.rows_scanned == 0:
        reason = "the scan read no rows, so there is no value list to match the phrase against"

    resolution = value_resolution.resolve(
        text,
        entries,
        value_notes(annotation_columns.get(column)),
        coverage=column_value(col, "values_coverage"),
        complete=list_is_complete(col),
        scope=scope,
        unavailable_reason=reason,
    )

    target = {"table": table, "column": column} | ({"part": part} if part is not None else {})
    files += tuple(table_dir / artifacts[kind] for kind in _SEARCHED_KINDS if kind in artifacts)
    call = paging.Call("resolve_value", arguments, files)

    return _paged_resolution(call, {**target, "text": text, **resolution}, arguments)


def _paged_resolution(
    call: paging.Call,
    reply: dict[str, Any],
    arguments: dict[str, Any],
) -> dict[str, Any]:
    lists = [key for key in _RESOLUTION_LISTS if isinstance(reply.get(key), list)]
    scalars = {key: value for key, value in reply.items() if key not in lists}
    items = [((key, index), entry) for key in lists for index, entry in enumerate(reply[key])]

    def render(units: Sequence[Any], first: bool, next_cursor: str | None) -> dict[str, Any]:
        page = dict(scalars)
        entries = [paging.entry(unit) for unit in units]

        for key in lists:
            listed = [value for (owner, _), value in entries if owner == key]

            if listed or (first and not reply[key]):
                page[key] = listed

        return _paged(page, total=len(items), next_cursor=next_cursor)

    return _page(call, items, render, arguments)


def _tool_get_manifest(state: ServedConnections, arguments: dict[str, Any]) -> dict[str, Any]:
    conn = state.resolve(arguments.get("connection"))
    manifest = _load_manifest(state, conn)

    if manifest is None:
        raise errors.manifest_references_missing_file(
            MANIFEST_FILENAME,
            str(conn.print_root / MANIFEST_FILENAME),
        )

    pattern = arguments.get("pattern")
    tables = manifest.get("tables") or {}
    matched = [
        (fqn, tables[fqn])
        for fqn in sorted(tables)
        if pattern is None or fnmatch.fnmatchcase(fqn, pattern)
    ]
    header = {key: value for key, value in manifest.items() if key not in _MANIFEST_FILTERED_KEYS}
    failed = [
        fqn
        for fqn in failed_tables(manifest)
        if pattern is None or fnmatch.fnmatchcase(fqn, pattern)
    ]

    def render(units: Sequence[Any], first: bool, next_cursor: str | None) -> dict[str, Any]:
        del first
        reply, rest = _split_head(units)
        reply["tables"] = dict(paging.entry(unit) for unit in rest)

        return _paged(reply, total=len(matched), next_cursor=next_cursor)

    call = paging.Call(
        "get_manifest",
        arguments,
        (conn.print_root / MANIFEST_FILENAME,),
    )
    head = {**header, **({"failed_tables": failed} if failed else {})}

    return _page(call, matched, render, arguments, head=head)


def _tool_get_diff(state: ServedConnections, arguments: dict[str, Any]) -> dict[str, Any]:
    conn = state.resolve(arguments.get("connection"))
    diff_path = conn.print_root / DIFF_FILENAME

    if not diff_path.is_file():
        raise errors.no_diff_available(str(diff_path))

    try:
        data = state.files.read(diff_path)
    except yaml.YAMLError as exc:
        raise errors.yaml_parse_error(str(diff_path), str(exc)) from exc

    if not isinstance(data, dict):
        return {}

    manifest = read_manifest(conn.print_root, state.files.read).manifest
    shown, decided_by = diff_without_rejected(data, manifest, conn.print_root, state.files.read)
    matched = [c for c in shown["changes"] if _change_matches(c, arguments)]
    header = {key: value for key, value in shown.items() if key != "changes"}

    def render(units: Sequence[Any], first: bool, next_cursor: str | None) -> dict[str, Any]:
        del first
        reply, rest = _split_head(units)
        reply["changes"] = [paging.entry(unit)[1] for unit in rest]

        return _paged(reply, total=len(matched), next_cursor=next_cursor)

    call = paging.Call("get_diff", arguments, (diff_path, *decided_by))

    return _page(call, list(enumerate(matched)), render, arguments, head=header)


def _change_matches(change: dict[str, Any], arguments: dict[str, Any]) -> bool:
    """One diff event against the `table` and `kind` filters, both optional and ANDed."""

    table = arguments.get("table")
    kind = arguments.get("kind")

    if kind is not None and change.get("kind") != kind:
        return False

    if table is None:
        return True

    return any(change.get(field) == table for field in _DIFF_TABLE_FIELDS)


def _tool_get_reference(arguments: dict[str, Any]) -> str:
    """No `connection` - the three reference documents depend on no connection or print."""

    section_number = arguments.get("section")
    call = paging.Call("get_reference", arguments, ())

    document_ = arguments["document"]

    if document_ == "guide":
        text = _guide_reference(section_number)
    elif section_number is None:
        text = reference.heading_tree(cast(ReferenceDocument, document_))
    else:
        result = reference.section(cast(ReferenceDocument, document_), section_number)

        if result is None:
            raise errors.unknown_section(
                document_,
                section_number,
                reference.section_numbers(cast(ReferenceDocument, document_)),
            )

        text = result

    return paging.text_page(call, [text], arguments.get("cursor"), _markdown_marker)


def _guide_reference(heading: str | None) -> str:
    if heading is None:
        return reference.guide_heading_tree()

    found = reference.guide_section(heading)

    if found is None:
        raise errors.unknown_section("guide", heading, reference.guide_headings())

    return found


# Helpers.


def _page(
    call: paging.Call,
    items: list[tuple[Any, Any]],
    render: paging.Render,
    arguments: dict[str, Any],
    *,
    head: dict[str, Any] | None = None,
) -> dict[str, Any]:
    keyed = [(_Head(key), value) for key, value in (head or {}).items()]
    units = paging.split_oversize([*keyed, *items], render, call.cursor(0))

    return paging.page(call, units, render, arguments.get("cursor"))


def _split_head(units: Sequence[Any]) -> tuple[dict[str, Any], list[Any]]:
    head: dict[str, Any] = {}
    rest: list[Any] = []

    for unit in units:
        key, value = paging.entry(unit)

        if isinstance(key, _Head):
            head[key.name] = value
        else:
            rest.append(unit)

    return head, rest


def _paged(reply: dict[str, Any], *, total: int, next_cursor: str | None) -> dict[str, Any]:
    reply["total"] = total

    if next_cursor is not None:
        reply["next_cursor"] = next_cursor

    return reply


def _load_manifest(state: ServedConnections, conn: ConnectionConfig) -> dict[str, Any] | None:
    return errors.manifest_or_error(read_manifest(conn.print_root, state.files.read))


def _load_statistics(
    state: ServedConnections,
    table_dir: Path,
    artifacts: dict[str, str],
) -> tuple[dict[str, Any], str | None]:
    """One table's `statistics.yaml` document, plus its own parse error if it has one.

    The error is absent for an undeclared kind, a missing file, or a merely malformed shape.
    """

    if "statistics" not in artifacts:
        return {}, None

    stats_path = table_dir / artifacts["statistics"]

    if not stats_path.is_file():
        return {}, None

    try:
        data = state.files.read(stats_path) or {}
    except yaml.YAMLError as exc:
        return {}, str(exc)

    return (data if isinstance(data, dict) else {}), None


def _columns_of(statistics: dict[str, Any]) -> dict[str, Any]:
    columns = statistics.get("columns")

    return columns if isinstance(columns, dict) else {}


def _load_annotation_columns(
    state: ServedConnections,
    table_dir: Path,
    artifacts: dict[str, str],
) -> tuple[dict[str, dict[str, Any]], str | None]:
    """One table's `statistics.annotations.yaml` `columns` map, plus its own parse error.

    Each entry is `{note, claims}`, both optional (SPEC 2.7.1).
    """

    if "statistics_annotations" not in artifacts:
        return {}, None

    ann_path = table_dir / artifacts["statistics_annotations"]

    if not ann_path.is_file():
        return {}, None

    try:
        data = state.files.read(ann_path) or {}
    except yaml.YAMLError as exc:
        return {}, str(exc)

    if not isinstance(data, dict):
        return {}, None

    columns = data.get("columns")

    if not isinstance(columns, dict):
        return {}, None

    return {name: entry for name, entry in columns.items() if isinstance(entry, dict)}, None


def _absent_table(
    table: str,
    connection: str,
    manifest: dict[str, Any] | None,
) -> errors.McpError:
    if table in failed_tables(manifest):
        return errors.unprofiled_table(table)

    return errors.unknown_table(table, connection)


@dataclass(frozen=True)
class _Head:
    name: str
