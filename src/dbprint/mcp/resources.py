"""URI parsing + per-artifact handlers (MCP.md 3); each read re-checks its file (MCP.md 7.1)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import yaml

from dbprint.config import ConnectionConfig
from dbprint.engine.baseline import (
    ArtifactReader,
    declared_artifacts,
    failed_tables,
    manifest_shape_error,
    table_directory,
    walkable_tables,
)
from dbprint.engine.reading_guide import READING_GUIDE_FILENAME
from dbprint.spec.fqn import join as join_fqn
from . import errors
from .reference import ReferenceDocument, read_document
from .state import ServedConnections


# The two server-global reference resources (MCP.md 3.2) - never per-connection.
_REFERENCE_DOCUMENTS: tuple[ReferenceDocument, ...] = ("spec", "assertions")
_REFERENCE_MIME = "text/markdown"


# Artifact kinds the server exposes; matches MCP.md 3.2 table.

ResourceKind = Literal[
    "manifest",
    "diff",
    "reading",
    "manifest_annotations",
    "ddl",
    "statistics",
    "relationships",
    "description",
    "statistics_annotations",
    "relationships_annotations",
]


_KIND_MIME = {
    "manifest": "application/yaml",
    "diff": "application/yaml",
    "reading": "text/markdown",
    "manifest_annotations": "application/yaml",
    "ddl": "application/sql",
    "statistics": "application/yaml",
    "relationships": "application/yaml",
    "description": "text/markdown",
    "statistics_annotations": "application/yaml",
    "relationships_annotations": "application/yaml",
}

# Connection-grain resources with no `fqn` - the URI form `dbprint://<connection>/<kind>`.
_CONNECTION_LEVEL_KINDS = frozenset({"manifest", "diff", "reading", "manifest_annotations"})
_CONNECTION_LEVEL_FILE = {
    "manifest": "manifest.yaml",
    "diff": "diff.yaml",
    "manifest_annotations": "manifest.annotations.yaml",
}

_KIND_FILE = {
    "ddl": "ddl.sql",
    "statistics": "statistics.yaml",
    "relationships": "relationships.yaml",
    "description": "description.md",
    "statistics_annotations": "statistics.annotations.yaml",
    "relationships_annotations": "relationships.annotations.yaml",
}

# Never declared is licensed - no human wrote one (SPEC 2.4, 2.7). Declared but missing is
# not: `manifest.missing-artifact` (SPEC 2.5) applies to these kinds exactly as to any other.
_OPTIONAL_ARTIFACT_KINDS = frozenset(
    {"description", "statistics_annotations", "relationships_annotations"},
)

_TABLE_KIND_DESCRIPTION = {
    "ddl": (
        "DDL as extracted. get_table_context returns it together with the table's joins "
        "and value lists"
    ),
    "statistics": (
        "statistics.yaml as written, including each column's sketch payload. "
        "get_table_context returns these statistics with scope, redaction and unmeasured "
        "fields applied; read this file raw only for the sketch"
    ),
    "relationships": (
        "relationships.yaml as written. get_table_context lists the same edges as its "
        "Joins list, each with its detection"
    ),
    "description": "Human-authored description.md. get_table_context includes it",
    "statistics_annotations": (
        "Human-authored column notes. get_table_context includes them, and "
        "search_columns `text` searches them"
    ),
    "relationships_annotations": (
        "Human-authored notes on relationships, including rejected edges. "
        "get_table_context marks a rejected edge in its Joins list"
    ),
}


@dataclass(frozen=True)
class ResourceRef:
    """Identifies a resource by connection + kind + optional FQN."""

    connection: str
    kind: ResourceKind
    fqn: str | None  # None for top-level (manifest, diff)


@dataclass(frozen=True)
class ReferenceRef:
    """Identifies a server-global reference document - `dbprint:///reference/<document>`.

    Carries no connection: `parse_uri` checks the empty-authority form before any other.
    """

    document: ReferenceDocument


@dataclass(frozen=True)
class ResourceEntry:
    """One row of `resources/list`."""

    uri: str
    name: str
    description: str
    mime_type: str


@dataclass(frozen=True)
class ReadResult:
    """One read from disk; carries the bytes/text and mimeType."""

    content: str
    mime_type: str


def parse_uri(uri: str) -> ResourceRef | ReferenceRef:
    """Parse a `dbprint://<connection>/<rest>` URI per MCP.md 3.1-3.2; raises McpError otherwise.

    The empty-authority form carries no connection; no kind vocabulary below contains `reference`.
    """

    if not uri.startswith("dbprint://"):
        raise errors.malformed_uri(uri)

    remainder = uri[len("dbprint://") :]
    parts = remainder.split("/") if remainder else []

    if not parts:
        raise errors.malformed_uri(uri)

    if parts[0] == "":
        if len(parts) == 3 and parts[1] == "reference" and parts[2] in _REFERENCE_DOCUMENTS:
            return ReferenceRef(document=parts[2])

        raise errors.malformed_uri(uri)

    connection = parts[0]

    if len(parts) == 2 and parts[1] in _CONNECTION_LEVEL_KINDS:
        return ResourceRef(connection=connection, kind=cast(ResourceKind, parts[1]), fqn=None)

    if len(parts) >= 3:
        fqn = join_fqn(parts[1:-1])
        kind = parts[-1]

        if kind in _KIND_FILE:
            return ResourceRef(connection=connection, kind=cast(ResourceKind, kind), fqn=fqn)

    raise errors.malformed_uri(uri)


def enumerate_for(state: ServedConnections) -> list[ResourceEntry]:
    """Deterministic resource list across every served connection, ordered per MCP.md 3.3.

    The reference documents are server-global - listed once each, ahead of every connection.
    """

    entries: list[ResourceEntry] = [
        ResourceEntry(
            uri=f"dbprint:///reference/{document}",
            name=f"{document} reference",
            description=(
                f"The {document} specification, whole. get_reference returns one section by number"
            ),
            mime_type=_REFERENCE_MIME,
        )
        for document in _REFERENCE_DOCUMENTS
    ]

    for conn_name in sorted(state.served):
        conn = state.served[conn_name]
        entries.extend(_enumerate_connection(conn, state.files.read))

    return entries


def read(state: ServedConnections, uri: str) -> ReadResult:
    """Resolve `uri` to a connection + path, or a server-global reference document."""

    ref = parse_uri(uri)

    if isinstance(ref, ReferenceRef):
        return ReadResult(content=read_document(ref.document), mime_type=_REFERENCE_MIME)

    if ref.connection not in state.served:
        configured = state.configured or frozenset(state.served)

        if ref.connection in configured:
            raise errors.unserved_connection(ref.connection, list(state.served))

        raise errors.unknown_connection(ref.connection, list(configured))

    conn = state.served[ref.connection]
    print_root = conn.output / conn.name

    if ref.kind == "manifest":
        return _read_text(print_root / "manifest.yaml", _KIND_MIME["manifest"], state.files.read)

    if ref.kind == "diff":
        diff_path = print_root / "diff.yaml"

        if not diff_path.is_file():
            raise errors.no_diff_available(str(diff_path))

        return _read_text(diff_path, _KIND_MIME["diff"], state.files.read)

    if ref.kind == "reading":
        reading_path = print_root / READING_GUIDE_FILENAME

        if not reading_path.is_file():
            raise errors.no_reading_guide_available(str(reading_path))

        return _read_text(reading_path, _KIND_MIME["reading"], state.files.read)

    if ref.kind == "manifest_annotations":
        path = print_root / _CONNECTION_LEVEL_FILE["manifest_annotations"]

        if path.is_file():
            return _read_text(path, _KIND_MIME["manifest_annotations"], state.files.read)

        # The same declared-vs-never-declared split as the per-table optional kinds (SPEC 2.5,
        # 2.7.3). A malformed manifest.yaml raises below; only an absent one is never-declared.
        declared_manifest = _load_manifest_or_none(print_root, state.files.read)
        declared = isinstance(declared_manifest, dict) and isinstance(
            declared_manifest.get("manifest_annotations"),
            str,
        )

        if declared:
            raise errors.manifest_references_missing_file(path.name, str(path))

        raise errors.missing_optional_connection_artifact(path.name, conn.name)

    assert ref.fqn is not None
    manifest = _load_manifest_or_none(print_root, state.files.read)

    if manifest is None:
        raise errors.manifest_references_missing_file(
            "manifest.yaml",
            str(print_root / "manifest.yaml"),
        )

    entry = walkable_tables(manifest).get(ref.fqn)

    if entry is None:
        raise (
            errors.unprofiled_table(ref.fqn)
            if ref.fqn in failed_tables(manifest)
            else errors.unknown_table(ref.fqn, conn.name)
        )

    artifacts = declared_artifacts(entry)
    file_name = artifacts.get(ref.kind)

    if file_name is None:
        if ref.kind in _OPTIONAL_ARTIFACT_KINDS:
            raise errors.missing_optional_artifact(_KIND_FILE[ref.kind], ref.fqn)

        # The manifest never declared this kind for this table - the caller's own request
        # against this object's type, not an inconsistency to repair (SPEC 2.3).
        raise errors.undeclared_artifact_kind(ref.kind, ref.fqn)

    file_path = table_directory(print_root, ref.fqn, entry) / file_name

    if not file_path.is_file():
        # Declared, regardless of `_OPTIONAL_ARTIFACT_KINDS` - a broken promise, the same
        # inconsistency `conformance/manifest.py` already flags at ERROR severity (SPEC 2.5).
        raise errors.manifest_references_missing_file(file_name, str(file_path))

    return _read_text(file_path, _KIND_MIME[ref.kind], state.files.read)


def _enumerate_connection(conn: ConnectionConfig, read: ArtifactReader) -> list[ResourceEntry]:
    """One connection's resource entries per MCP.md 3.3 - the producer-written kinds are listed
    unconditionally, so a run that skipped one still has a URI whose `read()` names the reason.

    `manifest_annotations` is the one conditional kind, human-authored and often absent.
    """

    print_root = conn.output / conn.name
    out: list[ResourceEntry] = [
        ResourceEntry(
            uri=f"dbprint://{conn.name}/manifest",
            name=f"{conn.name} manifest",
            description=(
                f"Manifest index for connection {conn.name}. list_tables with detail: true "
                f"projects it per table with a freshness verdict; get_manifest returns it "
                f"filtered by pattern"
            ),
            mime_type=_KIND_MIME["manifest"],
        ),
        ResourceEntry(
            uri=f"dbprint://{conn.name}/diff",
            name=f"{conn.name} diff",
            description=(
                f"What changed between the last two runs for connection {conn.name}. "
                f"get_diff returns it filtered by table or kind"
            ),
            mime_type=_KIND_MIME["diff"],
        ),
        ResourceEntry(
            uri=f"dbprint://{conn.name}/reading",
            name=f"{conn.name} reading guide",
            description=(
                "How to read this print's fields - vocabulary, traps and reading order. Read "
                "it before interpreting a field no tool description explains"
            ),
            mime_type=_KIND_MIME["reading"],
        ),
    ]

    if (print_root / _CONNECTION_LEVEL_FILE["manifest_annotations"]).is_file():
        out.append(
            ResourceEntry(
                uri=f"dbprint://{conn.name}/manifest_annotations",
                name=f"{conn.name} connection notes",
                description=("Human-authored notes on the whole connection; no tool returns them"),
                mime_type=_KIND_MIME["manifest_annotations"],
            ),
        )

    manifest = _load_manifest_or_none(print_root, read)

    if manifest is None:
        return out

    tables = walkable_tables(manifest)

    for fqn in sorted(tables):
        entry = tables[fqn]
        artifacts = declared_artifacts(entry)
        table_dir = table_directory(print_root, fqn, entry)

        for kind in (
            "ddl",
            "statistics",
            "relationships",
            "description",
            "statistics_annotations",
            "relationships_annotations",
        ):
            if kind not in artifacts:
                continue

            file_name = artifacts[kind]

            if kind in _OPTIONAL_ARTIFACT_KINDS:
                artifact_path = table_dir / file_name

                if not artifact_path.is_file():
                    continue

            out.append(
                ResourceEntry(
                    uri=f"dbprint://{conn.name}/{fqn}/{kind}",
                    name=f"{fqn} {kind}",
                    description=f"{fqn}: {_TABLE_KIND_DESCRIPTION[kind]}",
                    mime_type=_KIND_MIME[kind],
                ),
            )

    return out


def _read_text(path: Path, mime_type: str, read: ArtifactReader) -> ReadResult:
    if not path.is_file():
        raise errors.manifest_references_missing_file(path.name, str(path))

    text = path.read_text(encoding="utf-8")

    # A YAML artifact is served verbatim either way - this is a parseability check, not a
    # transform - but MCP.md 3 requires a parse failure to surface as -32603.
    if mime_type == "application/yaml":
        try:
            read(path)
        except yaml.YAMLError as exc:
            raise errors.yaml_parse_error(str(path), str(exc)) from exc

    return ReadResult(content=text, mime_type=mime_type)


def _load_manifest_or_none(print_root: Path, read: ArtifactReader) -> dict | None:
    manifest_path = print_root / "manifest.yaml"

    if not manifest_path.is_file():
        return None

    try:
        data = read(manifest_path)
    except yaml.YAMLError as exc:
        raise errors.yaml_parse_error(str(manifest_path), str(exc)) from exc

    reason = manifest_shape_error(data)

    if reason is not None:
        raise errors.malformed_manifest(str(manifest_path), reason)

    return data if isinstance(data, dict) else None
