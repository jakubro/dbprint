"""MCP tool implementations per MCP.md 4."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast, get_args

import yaml

from dbprint.config import ConnectionConfig
from dbprint.engine import AssemblyOptions, assemble_context, assemble_structured_context
from dbprint.engine.baseline import (
    declared_artifacts,
    manifest_shape_error,
    table_directory,
    walkable_tables,
)
from dbprint.spec.classification import Classification
from dbprint.spec.looks_like import LooksLike
from dbprint.spec.redaction import Primitive as RedactionPrimitive
from dbprint.spec.sensitivity import Sensitivity
from . import errors, reference
from .reference import ReferenceDocument
from .state import ServedConnections


TOOL_NAMES = (
    "get_table_context",
    "list_tables",
    "search_columns",
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
            "Return one table's DDL, statistics, relationships, description and "
            "annotations as an assembled context fragment. A budgeted call may "
            "omit sections to fit, and never returns empty on success - a "
            "truncation marker names what was dropped or, for json/yaml, a "
            "`_corrupted` field names any declared artifact that failed to parse."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "table": {"type": "string", "description": "Fully-qualified table name"},
                "conn": {
                    "type": "string",
                    "description": "Optional; falls back to default connection",
                },
                "format": {
                    "enum": ["md", "json", "yaml"],
                    "default": "md",
                    "description": (
                        "md renders DDL, description, annotations and a per-column Notes "
                        "summary only - not the raw statistics/relationships fields json "
                        "and yaml carry. Both omit each column's sketch payload; the "
                        "verbatim statistics.yaml, sketch included, is reachable as the "
                        "dbprint://<conn>/<fqn>/statistics resource."
                    ),
                },
                "include_stats": {
                    "type": "boolean",
                    "default": True,
                    "description": "Include the Cardinality table (md) or statistics object (json/yaml)",
                },
                "include_relationships": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Include the Relationships section (md) or relationships object (json/yaml)"
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
                        "Soft cap in tokens; sections drop whole in priority order once "
                        "exceeded, never truncated mid-section"
                    ),
                },
            },
            "required": ["table"],
        },
    ),
    ToolDef(
        name="list_tables",
        description=(
            "List tables matching an fnmatch pattern across a connection. "
            "`detail: true` projects each entry's type, row_count, columns and "
            "profiled_at from the manifest alongside its FQN, in one call."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "conn": {
                    "type": "string",
                    "description": "Optional; falls back to default connection",
                },
                "pattern": {
                    "type": "string",
                    "description": "fnmatch glob; defaults to '*'",
                },
                "detail": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Project each entry's type/row_count/columns/profiled_at from "
                        "the manifest; false returns bare FQN strings, unchanged"
                    ),
                },
            },
        },
    ),
    ToolDef(
        name="search_columns",
        description=(
            "Entry point for locating a fact across the print - an optional name glob "
            "plus optional classification/sql_type/sensitivity/looks_like/redacted glob "
            "filters and a candidate_key match, ANDed. A match on a scoped table carries "
            "rows_scanned/row_count so a caller can tell a scanned-set number from a "
            "table-wide one; a match carrying a looks_like verdict carries the "
            "sampled/matched draw behind it, since a verdict from two values reads "
            "identically to one from ten thousand otherwise. A match carries "
            "sensitivity/redacted/candidate_key (and candidate_key_exception where the "
            "ratio falls short of 1.0) whenever the column does, so filtering on any of "
            "them returns the matched category, not just a bare column name. `limit` caps the result, "
            "must be a positive integer, and the response says so when it caps. A "
            "result carries `unreadable_tables` only when a table's own statistics or "
            "annotations failed to parse: a statistics failure drops that table's "
            "columns from `matches` entirely; an annotations-only failure still "
            "returns them, without the annotation."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
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
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Cap on returned matches; a capped response carries `truncated: true`",
                },
                "conn": {
                    "type": "string",
                    "description": "Optional; falls back to default connection",
                },
            },
        },
    ),
    ToolDef(
        name="get_manifest",
        description=(
            "Return the parsed manifest.yaml for a connection - an index of "
            "tables and their artifacts, not a semantic catalogue of what they mean."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "conn": {
                    "type": "string",
                    "description": "Optional; falls back to default connection",
                },
            },
        },
    ),
    ToolDef(
        name="get_diff",
        description=(
            "Return the parsed diff.yaml for a connection - a per-column "
            "reliability signal for which statistics are stable and which "
            "drift run to run."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "conn": {
                    "type": "string",
                    "description": "Optional; falls back to default connection",
                },
            },
        },
    ),
    ToolDef(
        name="get_reference",
        description=(
            "Return a slice of the format spec or the assertion DSL spec, by section "
            "number - what a finding's own spec_ref names. Omit section for the heading "
            "tree instead of the whole document. Depends on no connection or print."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "document": {
                    "enum": ["assertions", "spec"],
                    "description": "Which specification - the format spec, or the assertion DSL",
                },
                "section": {
                    "type": "string",
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
    """Route a tool call; `get_table_context` returns a bare string for md/yaml (MCP.md 4.1)."""

    if name == "get_table_context":
        return _tool_get_table_context(state, arguments)

    if name == "list_tables":
        return _tool_list_tables(state, arguments)

    if name == "search_columns":
        return _tool_search_columns(state, arguments)

    if name == "get_manifest":
        return _tool_get_manifest(state, arguments)

    if name == "get_diff":
        return _tool_get_diff(state, arguments)

    if name == "get_reference":
        return _tool_get_reference(arguments)

    raise errors.unknown_tool(name, list(TOOL_NAMES))


def _tool_get_table_context(
    state: ServedConnections,
    arguments: dict[str, Any],
) -> dict[str, Any] | str:
    conn = state.resolve(arguments.get("conn"))
    table = arguments.get("table")

    if not table or not isinstance(table, str):
        raise errors.missing_table_argument(str(table))

    manifest = _load_manifest(conn)
    entry = (manifest.get("tables") or {}).get(table) if manifest else None

    if manifest is None or entry is None:
        raise errors.unknown_table(table, conn.name)

    fmt = _validate_format(arguments.get("format"))
    budget = _validate_budget_tokens(arguments.get("budget_tokens"))

    options = AssemblyOptions(
        format=fmt,
        include_ddl=True,
        include_description=bool(arguments.get("include_description", True)),
        include_annotations=bool(arguments.get("include_annotations", True)),
        include_stats=bool(arguments.get("include_stats", True)),
        include_relationships=bool(arguments.get("include_relationships", True)),
        budget=budget,
    )

    table_dir = table_directory(_print_root(conn), table, entry)
    declared = declared_artifacts(entry)
    corrupted = _corrupted_artifacts(table_dir, declared)

    # MCP.md 4.1: json returns the structured object, yaml that object as text, md markdown.
    # `_missing` is the assembler's own - carried in the payload or header, never prepended here.
    if options.format == "json":
        result = assemble_structured_context(
            manifest=manifest,
            print_root=_print_root(conn),
            table=table,
            options=options,
        )

        if corrupted:
            result["_corrupted"] = corrupted

        return result

    if options.format == "yaml":
        structured = assemble_structured_context(
            manifest=manifest,
            print_root=_print_root(conn),
            table=table,
            options=options,
        )

        if corrupted:
            structured["_corrupted"] = corrupted

        return yaml.safe_dump(structured, sort_keys=False, default_flow_style=False)

    result_text = assemble_context(
        manifest=manifest,
        print_root=_print_root(conn),
        tables=[table],
        options=options,
        connection_name=conn.name,
    ).text

    notes = [f"> **{kind}** did not parse: {msg}" for kind, msg in corrupted.items()]

    if notes:
        note = "\n".join(notes)

        return f"{note}\n\n{result_text}" if result_text else note

    return result_text


def _tool_list_tables(state: ServedConnections, arguments: dict[str, Any]) -> dict[str, Any]:
    conn = state.resolve(arguments.get("conn"))
    pattern = str(arguments.get("pattern") or "*")
    detail = bool(arguments.get("detail", False))
    manifest = _load_manifest(conn) or {}
    entries = manifest.get("tables") or {}
    # fnmatch.fnmatchcase never raises for a string pattern - no parse error to catch.
    matched = sorted(fqn for fqn in entries if fnmatch.fnmatchcase(fqn, pattern))

    if not detail:
        return {"tables": matched}

    return {
        "tables": [
            {
                "fqn": fqn,
                "type": entries[fqn].get("type"),
                "row_count": entries[fqn].get("row_count"),
                "columns": entries[fqn].get("columns"),
                "profiled_at": entries[fqn].get("profiled_at"),
            }
            for fqn in matched
        ],
    }


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
    inferred = col.get("inferred") or {}

    if not _field_matches(col.get("classification"), filters.classification):
        return False

    if not _field_matches(col.get("sql_type"), filters.sql_type):
        return False

    if not _field_matches(inferred.get("sensitivity"), filters.sensitivity):
        return False

    if not _field_matches(inferred.get("looks_like"), filters.looks_like):
        return False

    if not _field_matches(col.get("redacted"), filters.redacted):
        return False

    return not (
        filters.candidate_key is not None
        and bool(inferred.get("candidate_key")) != filters.candidate_key
    )


def _search_match(
    fqn: str,
    entry: dict[str, Any],
    col_name: str,
    col: dict[str, Any],
    annotation: dict[str, Any],
) -> dict[str, Any]:
    """One `search_columns` match - the artifact's own fields, no re-derivation."""

    inferred = col.get("inferred") or {}
    match: dict[str, Any] = {
        "table_fqn": fqn,
        "column": col_name,
        "sql_type": col.get("sql_type", ""),
        "classification": col.get("classification", ""),
    }

    row_count = entry.get("row_count")

    if row_count is not None:
        match["row_count"] = row_count

    rows_scanned = col.get("rows_scanned")

    if rows_scanned is not None:
        match["rows_scanned"] = rows_scanned

    # A looks_like verdict from a draw of two reads identically to one from ten
    # thousand without these - carry the evidence, not just the classification.
    if "looks_like" in inferred:
        match["looks_like"] = inferred["looks_like"]

        for key in ("sampled", "matched"):
            if inferred.get(key) is not None:
                match[key] = inferred[key]

    # The remaining predicates `_column_matches` filters on - a match needs its category.
    if "sensitivity" in inferred:
        match["sensitivity"] = inferred["sensitivity"]

    redacted = col.get("redacted")

    if isinstance(redacted, str) and redacted:
        match["redacted"] = redacted

    if inferred.get("candidate_key"):
        match["candidate_key"] = True
        exception = inferred.get("candidate_key_exception")

        if exception is not None:
            match["candidate_key_exception"] = exception

    note = annotation.get("note")

    if isinstance(note, str) and note.strip():
        match["annotation"] = note

    return match


def _tool_search_columns(state: ServedConnections, arguments: dict[str, Any]) -> dict[str, Any]:
    pattern = arguments.get("pattern")

    if pattern is not None and (not isinstance(pattern, str) or not pattern):
        raise errors.malformed_pattern(str(pattern))

    filters = _column_filters(arguments)
    limit = _validate_limit(arguments.get("limit"))
    conn = state.resolve(arguments.get("conn"))
    manifest = _load_manifest(conn) or {}
    print_root = _print_root(conn)

    matches: list[dict[str, Any]] = []
    unreadable: list[str] = []
    truncated = False

    # Every declared table is loaded regardless of the cap, so corruption past it is still
    # named (MCP.md 4.3) - only match COLLECTION stops once `limit` is reached.
    for fqn, entry in sorted(walkable_tables(manifest).items()):
        artifacts = declared_artifacts(entry)
        table_dir = table_directory(print_root, fqn, entry)
        stats_columns, stats_error = _load_statistics_columns(table_dir, artifacts)
        annotation_columns, annotation_error = _load_annotation_columns(table_dir, artifacts)

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
            if truncated:
                break

            if pattern is not None and not fnmatch.fnmatchcase(col_name, pattern):
                continue

            col = stats_columns.get(col_name) or {}

            if not _column_matches(col, filters):
                continue

            if limit is not None and len(matches) >= limit:
                truncated = True
                break

            matches.append(
                _search_match(fqn, entry, col_name, col, annotation_columns.get(col_name) or {}),
            )

    result: dict[str, Any] = {"matches": matches}

    if truncated:
        result["truncated"] = True

    if unreadable:
        result["unreadable_tables"] = sorted(unreadable)

    return result


def _tool_get_manifest(state: ServedConnections, arguments: dict[str, Any]) -> dict[str, Any]:
    conn = state.resolve(arguments.get("conn"))
    manifest = _load_manifest(conn)

    if manifest is None:
        raise errors.manifest_references_missing_file(
            "manifest.yaml",
            str(_print_root(conn) / "manifest.yaml"),
        )

    return manifest


def _tool_get_diff(state: ServedConnections, arguments: dict[str, Any]) -> dict[str, Any]:
    conn = state.resolve(arguments.get("conn"))
    diff_path = _print_root(conn) / "diff.yaml"

    if not diff_path.is_file():
        raise errors.no_diff_available(str(diff_path))

    try:
        data = yaml.safe_load(diff_path.read_text())
    except yaml.YAMLError as exc:
        raise errors.yaml_parse_error(str(diff_path), str(exc)) from exc

    return data if isinstance(data, dict) else {}


def _tool_get_reference(arguments: dict[str, Any]) -> str:
    """No `conn` - the two reference documents depend on no connection or print."""

    document = arguments.get("document")
    allowed = _tool_schema("get_reference")["document"]["enum"]

    if document not in allowed:
        raise errors.invalid_enum_argument("document", document, allowed)

    document_ = cast(ReferenceDocument, document)
    section_number = arguments.get("section")

    if section_number is None:
        return reference.heading_tree(document_)

    if not isinstance(section_number, str) or not section_number:
        raise errors.invalid_enum_argument("section", section_number, ["a non-empty string"])

    result = reference.section(document_, section_number)

    if result is None:
        raise errors.unknown_section(
            document_,
            section_number,
            reference.section_numbers(document_),
        )

    return result


# Helpers.


def _print_root(conn: ConnectionConfig) -> Path:
    return conn.output / conn.name


def _tool_schema(name: str) -> dict[str, Any]:
    """One tool's own advertised `inputSchema` properties - the source `tools/list` sends."""

    return next(t.input_schema["properties"] for t in TOOL_DEFINITIONS if t.name == name)


def _validate_format(value: Any) -> str:
    """`format` against `get_table_context`'s own declared enum (default `md`)."""

    fmt = str(value or "md").lower()
    allowed = _tool_schema("get_table_context")["format"]["enum"]

    if fmt not in allowed:
        raise errors.invalid_enum_argument("format", fmt, allowed)

    return fmt


def _validate_budget_tokens(value: Any) -> int | None:
    """`budget_tokens` against `get_table_context`'s own declared minimum; None passes through."""

    if value is None:
        return None

    minimum = _tool_schema("get_table_context")["budget_tokens"]["minimum"]

    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise errors.invalid_minimum_argument("budget_tokens", value, minimum)

    return value


def _validate_limit(value: Any) -> int | None:
    """`limit` against `search_columns`'s own declared minimum; None passes through."""

    if value is None:
        return None

    minimum = _tool_schema("search_columns")["limit"]["minimum"]

    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise errors.invalid_minimum_argument("limit", value, minimum)

    return value


_YAML_ARTIFACT_KINDS = (
    "statistics",
    "relationships",
    "statistics_annotations",
    "relationships_annotations",
)


def _corrupted_artifacts(table_dir: Path, artifacts: dict[str, str]) -> dict[str, str]:
    """Kind -> parse-error message, for every declared YAML artifact that fails to parse.

    Not an undeclared kind (silently omitted, SPEC 2.3) and not a declared-but-absent file
    (`engine.baseline.missing_artifacts`) - only bytes present and broken.
    """

    corrupted: dict[str, str] = {}

    for kind in _YAML_ARTIFACT_KINDS:
        if kind not in artifacts:
            continue

        path = table_dir / artifacts[kind]

        if not path.is_file():
            continue

        try:
            yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            corrupted[kind] = str(exc)

    return corrupted


def _load_manifest(conn: ConnectionConfig) -> dict[str, Any] | None:
    manifest_path = _print_root(conn) / "manifest.yaml"

    if not manifest_path.is_file():
        return None

    try:
        data = yaml.safe_load(manifest_path.read_text())
    except yaml.YAMLError as exc:
        raise errors.yaml_parse_error(str(manifest_path), str(exc)) from exc

    reason = manifest_shape_error(data)

    if reason is not None:
        raise errors.malformed_manifest(str(manifest_path), reason)

    return data if isinstance(data, dict) else None


def _load_statistics_columns(
    table_dir: Path,
    artifacts: dict[str, str],
) -> tuple[dict[str, Any], str | None]:
    """One table's `statistics.yaml` `columns` map, plus its own parse error if it has one.

    The error is absent when the kind was never declared, the file is missing, or the shape is
    merely malformed rather than unparseable.
    """

    if "statistics" not in artifacts:
        return {}, None

    stats_path = table_dir / artifacts["statistics"]

    if not stats_path.is_file():
        return {}, None

    try:
        data = yaml.safe_load(stats_path.read_text()) or {}
    except yaml.YAMLError as exc:
        return {}, str(exc)

    if not isinstance(data, dict):
        return {}, None

    columns = data.get("columns")

    return (columns if isinstance(columns, dict) else {}), None


def _load_annotation_columns(
    table_dir: Path,
    artifacts: dict[str, str],
) -> tuple[dict[str, dict[str, Any]], str | None]:
    """One table's `statistics.annotations.yaml` `columns` map, plus its own parse error.

    Each entry is `{note: <str>, claims: {<stat>: <predicate>}}`, both optional (SPEC 2.7.1).
    See `_load_statistics_columns` for what the error half covers.
    """

    if "statistics_annotations" not in artifacts:
        return {}, None

    ann_path = table_dir / artifacts["statistics_annotations"]

    if not ann_path.is_file():
        return {}, None

    try:
        data = yaml.safe_load(ann_path.read_text()) or {}
    except yaml.YAMLError as exc:
        return {}, str(exc)

    if not isinstance(data, dict):
        return {}, None

    columns = data.get("columns")

    if not isinstance(columns, dict):
        return {}, None

    return {name: entry for name, entry in columns.items() if isinstance(entry, dict)}, None
