"""MCP tool implementations per MCP.md 4."""

from __future__ import annotations

import fnmatch
import importlib.resources
import json
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
    assemble_context,
    assemble_structured_context,
    thresholds,
    value_resolution,
)
from dbprint.engine.baseline import (
    declared_artifacts,
    failed_tables,
    manifest_shape_error,
    table_directory,
    walkable_tables,
)
from dbprint.engine.freshness import age_days, evaluate
from dbprint.spec.absence import Absence, column_value, read_column_field
from dbprint.spec.classification import Classification
from dbprint.spec.looks_like import LooksLike
from dbprint.spec.redaction import Primitive as RedactionPrimitive
from dbprint.spec.scope import ScanScope, list_is_complete, reply_scope, rows_scanned, scope_of
from dbprint.spec.sensitivity import Sensitivity
from . import errors, reference
from .reference import ReferenceDocument
from .state import ServedConnections


# Reply caps count items, not bytes; each tool's own argument raises its own cap.
SEARCH_MATCH_CAP = 200
TABLE_LISTING_CAP = 500
MANIFEST_TABLE_CAP = 500
DIFF_CHANGE_CAP = 500
CONTEXT_BUDGET_TOKENS = 8000

# The three relationship events carry `source_table`/`target_table` where every other event
# carries `table`; a filter reading one field alone drops them silently (engine/diff.py).
_DIFF_TABLE_FIELDS = ("table", "source_table", "target_table")

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
            "`purpose: query`: DDL, the Joins list (every edge the print knows, declared "
            "or not, with its detection), a data dictionary, and the value lists a "
            "predicate is written from, with their counts and coverage. Call it with the "
            "default `purpose: profile` to describe the data - statistics such as null "
            "rates, cardinality and ranges, relationships, the description and notes. "
            "Use search_columns or list_tables first when the table is not yet known, "
            "and resolve_value to check how one phrase is spelled in one column. A "
            "budgeted call may omit whole sections to fit and never returns empty on "
            "success - a truncation marker names what was dropped or, for json/yaml, a "
            "`_corrupted` field names any declared artifact that failed to parse."
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
                        "the value lists with counts and coverage, with no other statistics. "
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
                        "dbprint://<connection>/<table>/statistics resource."
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
                "budget_tokens": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Soft cap in tokens, defaulting to 8000. Sections drop whole, never "
                        "truncated mid-section: the table's identity is charged first, then "
                        "each section in priority order is measured against what is left, and "
                        "one that does not fit is skipped while later, smaller ones may still "
                        "be included"
                    ),
                },
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
            "Capped at 500 entries; narrow with `pattern` to reach past the cap, and a capped "
            "reply carries `truncated: true` with the `total` it was cut from."
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
            "them returns the matched category, not just a bare column name. `limit` caps "
            "the result and defaults to 200; a capped reply carries `truncated: true` with "
            "the `total` it was cut from, and an explicit larger `limit` is honoured. A "
            "result carries `unreadable_tables` only when a table's own statistics or "
            "annotations failed to parse: a statistics failure drops that table's "
            "columns from `matches` entirely; an annotations-only failure still "
            "returns them, without the annotation."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "pattern": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "fnmatch glob over column names; optional - omit to filter by "
                        "the other predicates alone"
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
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Cap on returned matches; a capped response carries `truncated: true`",
                },
                "connection": {
                    "type": "string",
                    "description": (
                        "Connection name from .dbprint.yaml; omit it to use the server's "
                        "default connection"
                    ),
                },
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
            "`row_count`, and `domain` is the scanned rows' whole domain."
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
            "The `tables` map is capped at 500 entries and every other key of "
            "the document is returned whole, `failed_tables` filtered by `pattern` like `tables`; "
            "narrow with `pattern` to reach past the cap, and "
            "a capped reply carries `truncated: true` with the `total` it was cut from."
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
            "counted in `unevaluated_tables` was not compared. The `changes` list is capped at 500 events and every other key "
            "of the document is returned whole; narrow with `table` or `kind` to reach past the "
            "cap, and a capped reply carries `truncated: true` with the `total` it was cut from. "
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
            },
        },
    ),
    ToolDef(
        name="get_reference",
        description=(
            "Look up the dbprint format specification or the assertion DSL "
            "specification by section number: what a print's field means, or what a "
            "finding's spec_ref (e.g. '§2.2.4') refers to. Omit section for the "
            "heading tree instead of the whole document. Depends on no connection or "
            "print; what one print's tables hold is get_table_context's and list_tables' answer."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "document": {
                    "type": "string",
                    "enum": ["assertions", "spec"],
                    "description": "Which specification - the format spec, or the assertion DSL",
                },
                "section": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "A section number in the document's own scheme (e.g. '3', '2.2.4'), or "
                        "a spec_ref citation copied verbatim from a finding ('§2.2.4', "
                        "'ASSERTIONS.md §1.4') - any heading depth. Omit for the table of "
                        "contents."
                    ),
                },
            },
            "required": ["document"],
        },
    ),
)


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

    if error.validator == "minimum":
        return f"{key} {error.instance!r} must be an integer >= {schema['minimum']}."

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
    budget = arguments.get("budget_tokens", CONTEXT_BUDGET_TOKENS)
    purpose = cast(Purpose, arguments.get("purpose", "profile"))

    options = AssemblyOptions(
        format=fmt,
        purpose=purpose,
        include_ddl=True,
        include_description=bool(arguments.get("include_description", True)),
        include_annotations=bool(arguments.get("include_annotations", True)),
        include_stats=bool(arguments.get("include_stats", True)),
        include_relationships=bool(arguments.get("include_relationships", True)),
        budget=budget,
    )

    # MCP.md 4.1: json returns the structured object, yaml that object as text, md markdown.
    # `_missing`/`_corrupted` are the assembler's own - carried in the payload or header,
    # never recomputed here: one computation, one answer on every format.
    if options.format == "json":
        return assemble_structured_context(
            manifest=manifest,
            print_root=_print_root(conn),
            table=table,
            options=options,
            read=state.files.read,
        )

    if options.format == "yaml":
        structured = assemble_structured_context(
            manifest=manifest,
            print_root=_print_root(conn),
            table=table,
            options=options,
            read=state.files.read,
        )

        return yaml.safe_dump(structured, sort_keys=False, default_flow_style=False)

    return assemble_context(
        manifest=manifest,
        print_root=_print_root(conn),
        tables=[table],
        options=options,
        connection_name=conn.name,
        read=state.files.read,
    ).text


def _tool_list_tables(state: ServedConnections, arguments: dict[str, Any]) -> dict[str, Any]:
    conn = state.resolve(arguments.get("connection"))
    pattern = str(arguments.get("pattern") or "*")
    detail = bool(arguments.get("detail", False))
    manifest = _load_manifest(state, conn) or {}
    entries = manifest.get("tables") or {}
    # fnmatch.fnmatchcase never raises for a string pattern - no parse error to catch.
    matched = sorted(fqn for fqn in entries if fnmatch.fnmatchcase(fqn, pattern))
    kept = matched[:TABLE_LISTING_CAP]

    reply: dict[str, Any] = {}

    if detail:
        verdicts, size_gated = _freshness(conn, manifest)
        reply["tables"] = [
            {
                "table": fqn,
                "type": entries[fqn].get("type"),
                "row_count": entries[fqn].get("row_count"),
                "columns": entries[fqn].get("columns"),
                "profiled_at": entries[fqn].get("profiled_at"),
                **verdicts[fqn],
            }
            for fqn in kept
        ]

        if gated := [fqn for fqn in size_gated if fqn in kept]:
            reply["warnings"] = [thresholds.size_gate_warning(conn.name, gated)]
    else:
        reply["tables"] = list(kept)

    if failed := [fqn for fqn in failed_tables(manifest) if fnmatch.fnmatchcase(fqn, pattern)]:
        reply["failed_tables"] = failed

    return _capped(reply, kept=len(kept), total=len(matched))


def _freshness(
    conn: ConnectionConfig,
    manifest: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], tuple[str, ...]]:
    """A refused table gets its refusal and no verdict; the connection default never stands in."""

    resolved = thresholds.resolve(conn, manifest)
    now = datetime.now(UTC)
    tables = manifest.get("tables") or {}
    judged = {fqn: entry for fqn, entry in tables.items() if fqn not in resolved.refused}
    stale = {
        entry.fqn: entry
        for entry in evaluate(
            {**manifest, "tables": judged},
            0.0,
            now,
            threshold_for=resolved.threshold_for,
        )
    }
    verdicts: dict[str, dict[str, Any]] = {
        fqn: {"threshold_error": cause} for fqn, cause in resolved.refused.items()
    }

    for fqn, entry in judged.items():
        stale_entry = stale.get(fqn)

        if stale_entry is None:
            verdict, age = "live", age_days(entry.get("profiled_at"), now)
        elif stale_entry.age_days == float("inf"):
            verdict, age = "dormant", None
        else:
            verdict, age = "stale", stale_entry.age_days

        threshold = resolved.resolved[fqn]
        verdicts[fqn] = {
            "freshness": verdict,
            "age_days": None if age is None else round(age, 2),
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
    limit = arguments.get("limit", SEARCH_MATCH_CAP)
    conn = state.resolve(arguments.get("connection"))
    manifest = _load_manifest(state, conn) or {}
    print_root = _print_root(conn)

    matches: list[dict[str, Any]] = []
    unreadable: list[str] = []
    total = 0

    # Every declared table is loaded regardless of the cap, so corruption past it is still
    # named (MCP.md 4.3) - only match COLLECTION stops once `limit` is reached.
    for fqn, entry in sorted(walkable_tables(manifest).items()):
        artifacts = declared_artifacts(entry)
        table_dir = table_directory(print_root, fqn, entry)
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

        if "statistics" in artifacts:
            # statistics is the column list; a stale annotation key (SPEC 2.7) is not a column.
            column_names = set(stats_columns)
        else:
            # Every object type declares statistics (SPEC 2.2.15) in a conformant print;
            # this is a fallback for an older or malformed manifest that omits it.
            column_names = set(annotation_columns)

        for col_name in sorted(column_names):
            if pattern is not None and not fnmatch.fnmatchcase(col_name, pattern):
                continue

            col = stats_columns.get(col_name) or {}

            if not _column_matches(col, filters):
                continue

            annotation = annotation_columns.get(col_name) or {}
            value_notes = _matching_value_notes(annotation, text) if text is not None else []

            if text is not None and not (
                value_notes or _names_or_notes(col_name, annotation, text)
            ):
                continue

            total += 1

            if len(matches) < limit:
                match = _search_match(fqn, entry, col_name, col, annotation, scope)

                if value_notes:
                    match["value_notes"] = value_notes

                matches.append(match)

    result = _capped({"matches": matches}, kept=len(matches), total=total)

    if unreadable:
        result["unreadable_tables"] = sorted(unreadable)

    return result


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
    table_dir = table_directory(_print_root(conn), table, entry)

    if "statistics" not in artifacts:
        return {
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
    values = read_column_field(col, "values")
    entries = values.value if isinstance(values.value, list) else []
    redaction = column_value(col, "redacted")
    scope = scope_of(statistics)
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
        _column_value_notes(annotation_columns.get(column)),
        coverage=column_value(col, "values_coverage"),
        complete=list_is_complete(col),
        scope=scope,
        unavailable_reason=reason,
    )

    return {"table": table, "column": column, "text": text, **resolution}


def _column_value_notes(annotation: Any) -> dict[str, str]:
    """A column's per-value notes (SPEC 2.7.1), keyed by the value's string form."""

    if not isinstance(annotation, dict):
        return {}

    entries = annotation.get("values")

    if not isinstance(entries, list):
        return {}

    return {
        str(entry.get("value")): " ".join(entry["note"].split())
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("note"), str) and entry["note"].strip()
    }


def _tool_get_manifest(state: ServedConnections, arguments: dict[str, Any]) -> dict[str, Any]:
    conn = state.resolve(arguments.get("connection"))
    manifest = _load_manifest(state, conn)

    if manifest is None:
        raise errors.manifest_references_missing_file(
            "manifest.yaml",
            str(_print_root(conn) / "manifest.yaml"),
        )

    pattern = arguments.get("pattern")
    tables = manifest.get("tables") or {}
    matched = {
        fqn: entry
        for fqn, entry in tables.items()
        if pattern is None or fnmatch.fnmatchcase(fqn, pattern)
    }
    kept = dict(list(matched.items())[:MANIFEST_TABLE_CAP])
    reply = {**manifest, "tables": kept}
    reply.pop("failed_tables", None)

    if failed := [
        fqn
        for fqn in failed_tables(manifest)
        if pattern is None or fnmatch.fnmatchcase(fqn, pattern)
    ]:
        reply["failed_tables"] = failed

    return _capped(reply, kept=len(kept), total=len(matched))


def _tool_get_diff(state: ServedConnections, arguments: dict[str, Any]) -> dict[str, Any]:
    conn = state.resolve(arguments.get("connection"))
    diff_path = _print_root(conn) / "diff.yaml"

    if not diff_path.is_file():
        raise errors.no_diff_available(str(diff_path))

    try:
        data = state.files.read(diff_path)
    except yaml.YAMLError as exc:
        raise errors.yaml_parse_error(str(diff_path), str(exc)) from exc

    if not isinstance(data, dict):
        return {}

    changes = [c for c in (data.get("changes") or []) if isinstance(c, dict)]
    matched = [c for c in changes if _change_matches(c, arguments)]
    kept = matched[:DIFF_CHANGE_CAP]

    return _capped({**data, "changes": kept}, kept=len(kept), total=len(matched))


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
    """No `connection` - the two reference documents depend on no connection or print."""

    document_ = cast(ReferenceDocument, arguments["document"])
    section_number = arguments.get("section")

    if section_number is None:
        return reference.heading_tree(document_)

    result = reference.section(document_, section_number)

    if result is None:
        raise errors.unknown_section(
            document_,
            section_number,
            reference.section_numbers(document_),
        )

    return result


# Helpers.


def _capped(payload: dict[str, Any], *, kept: int, total: int) -> dict[str, Any]:
    """Mark a reply the cap cut, with the total it was cut from."""

    if kept >= total:
        return payload

    return {**payload, "truncated": True, "total": total}


def _print_root(conn: ConnectionConfig) -> Path:
    return conn.output / conn.name


def _load_manifest(state: ServedConnections, conn: ConnectionConfig) -> dict[str, Any] | None:
    manifest_path = _print_root(conn) / "manifest.yaml"

    if not manifest_path.is_file():
        return None

    try:
        data = state.files.read(manifest_path)
    except yaml.YAMLError as exc:
        raise errors.yaml_parse_error(str(manifest_path), str(exc)) from exc

    reason = manifest_shape_error(data)

    if reason is not None:
        raise errors.malformed_manifest(str(manifest_path), reason)

    return data if isinstance(data, dict) else None


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
