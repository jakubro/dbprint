"""URI parsing + per-artifact handlers (MCP.md 3); each read re-checks its file (MCP.md 7.1)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import parse_qs

import yaml

from dbprint.config import ConnectionConfig
from dbprint.engine.baseline import (
    ArtifactReader,
    failed_tables,
    read_manifest,
    table_directory,
)
from dbprint.engine.context_assembler import incoming_rejections
from dbprint.engine.relationship_graph import (
    edge_detection,
    edge_key,
    rejected_edges,
    withhold_rejected,
)
from dbprint.engine.yaml_dumper import dump_yaml
from dbprint.spec.artifacts import (
    ARTIFACT_FILENAMES,
    DIFF_FILENAME,
    MANIFEST_ANNOTATIONS_FILENAME,
    MANIFEST_FILENAME,
    READING_GUIDE_FILENAME,
    declared_artifacts,
    walkable_tables,
)
from dbprint.spec.fqn import join as join_fqn
from . import errors, paging
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
    "manifest": MANIFEST_FILENAME,
    "diff": DIFF_FILENAME,
    "manifest_annotations": MANIFEST_ANNOTATIONS_FILENAME,
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
        "relationships.yaml without the edges a human rejected in relationships_annotations. "
        "get_table_context lists the same edges as its Joins list, each with its detection"
    ),
    "description": "Human-authored description.md. get_table_context includes it",
    "statistics_annotations": (
        "Human-authored column notes. get_table_context includes them, and "
        "search_columns `text` searches them"
    ),
    "relationships_annotations": (
        "Human-authored notes on relationships, including the edges a human rejected; no "
        "other resource or tool lists a rejected edge"
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
    """One page of a resource: its text, mimeType, and where it sits among the file's pages."""

    content: str
    mime_type: str
    page: int = 1
    pages: int = 1
    version: str = ""

    def meta(self) -> dict[str, Any]:
        """The `_meta` every read carries (MCP.md 3.4)."""

        return {"page": self.page, "pages": self.pages, "version": self.version}


@dataclass(frozen=True)
class ListPage:
    """One page of `resources/list`, and the cursor to the next when one exists."""

    entries: list[ResourceEntry]
    next_cursor: str | None


TEMPLATES: tuple[tuple[str, str, str], ...] = (
    ("dbprint:///reference/{document}{?page,version}", "reference document", _REFERENCE_MIME),
    *(
        (
            f"dbprint://{{connection}}/{kind}{{?page,version}}",
            f"connection {kind}",
            _KIND_MIME[kind],
        )
        for kind in ("manifest", "diff", "reading", "manifest_annotations")
    ),
    *(
        (f"dbprint://{{connection}}/{{table}}/{kind}{{?page,version}}", f"table {kind}", mime)
        for kind, mime in _KIND_MIME.items()
        if kind in ARTIFACT_FILENAMES
    ),
)


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

        if kind in ARTIFACT_FILENAMES:
            return ResourceRef(connection=connection, kind=cast(ResourceKind, kind), fqn=fqn)

    raise errors.malformed_uri(uri)


def list_page(state: ServedConnections, cursor: str | None) -> ListPage:
    """The `resources/list` page `cursor` points at, under MCP.md 4.8's bound and cursor rules."""

    entries = enumerate_for(state)
    manifests = tuple(conn.print_root / MANIFEST_FILENAME for conn in state.served.values())
    call = paging.Call("resources/list", {}, manifests)

    def render(units: Any, first: bool, next_cursor: str | None) -> dict[str, Any]:
        del first
        reply: dict[str, Any] = {"resources": [_listed(paging.entry(u)[1]) for u in units]}

        if next_cursor is not None:
            reply["nextCursor"] = next_cursor

        return reply

    reply = paging.page(call, list(enumerate(entries)), render, cursor)
    by_uri = {entry.uri: entry for entry in entries}

    return ListPage(
        entries=[by_uri[item["uri"]] for item in reply["resources"]],
        next_cursor=reply.get("nextCursor"),
    )


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
    """The page of a resource `uri` names; the bare URI is page 1 (MCP.md 3.4)."""

    bare, page, version = _page_query(uri)
    whole = _read_whole(state, bare)
    pages = paging.line_pages(whole.content, width=paging.escaped_width)

    if not (page.isascii() and page.isdecimal()) or not 1 <= int(page) <= len(pages):
        raise errors.page_out_of_range(uri, len(pages))

    if version is None and int(page) > 1:
        raise errors.missing_page_version(uri)

    if version is not None and version != whole.version:
        raise errors.stale_page_version(uri)

    return ReadResult(
        content=pages[int(page) - 1],
        mime_type=whole.mime_type,
        page=int(page),
        pages=len(pages),
        version=whole.version,
    )


def _read_whole(state: ServedConnections, uri: str) -> ReadResult:
    ref = parse_uri(uri)

    if isinstance(ref, ReferenceRef):
        text = read_document(ref.document)
        version = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

        return ReadResult(content=text, mime_type=_REFERENCE_MIME, version=version)

    if ref.connection not in state.served:
        configured = state.configured or frozenset(state.served)

        if ref.connection in configured:
            raise errors.unserved_connection(ref.connection, list(state.served))

        raise errors.unknown_connection(ref.connection, list(configured))

    conn = state.served[ref.connection]
    print_root = conn.print_root

    if ref.kind == "manifest":
        return _read_text(print_root / MANIFEST_FILENAME, _KIND_MIME["manifest"], state.files.read)

    if ref.kind == "diff":
        diff_path = print_root / DIFF_FILENAME

        if not diff_path.is_file():
            raise errors.no_diff_available(str(diff_path))

        whole = _read_text(diff_path, _KIND_MIME["diff"], state.files.read)

        return _diff_without_rejected_events(whole, print_root, diff_path, state.files.read)

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
            MANIFEST_FILENAME,
            str(print_root / MANIFEST_FILENAME),
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
            raise errors.missing_optional_artifact(ARTIFACT_FILENAMES[ref.kind], ref.fqn)

        # The manifest never declared this kind for this table - the caller's own request
        # against this object's type, not an inconsistency to repair (SPEC 2.3).
        raise errors.undeclared_artifact_kind(ref.kind, ref.fqn)

    file_path = table_directory(print_root, ref.fqn, entry) / file_name

    if not file_path.is_file():
        # Declared, regardless of `_OPTIONAL_ARTIFACT_KINDS` - a broken promise, the same
        # inconsistency `conformance/manifest.py` already flags at ERROR severity (SPEC 2.5).
        raise errors.manifest_references_missing_file(file_name, str(file_path))

    whole = _read_text(file_path, _KIND_MIME[ref.kind], state.files.read)

    if ref.kind != "relationships":
        return whole

    return _without_rejected_edges(
        whole,
        manifest,
        print_root,
        ref.fqn,
        file_path,
        state.files.read,
    )


def _page_query(uri: str) -> tuple[str, str, str | None]:
    bare, _, query = uri.partition("?")

    if not query:
        return bare, "1", None

    try:
        fields = parse_qs(query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise errors.malformed_uri(uri) from exc

    if set(fields) - {"page", "version"} or any(len(v) > 1 for v in fields.values()):
        raise errors.malformed_uri(uri)

    return bare, fields.get("page", ["1"])[0], fields.get("version", [None])[0]


def _listed(entry: ResourceEntry) -> dict[str, Any]:
    return {
        "uri": entry.uri,
        "name": entry.name,
        "description": entry.description,
        "mimeType": entry.mime_type,
    }


def _enumerate_connection(conn: ConnectionConfig, read: ArtifactReader) -> list[ResourceEntry]:
    """One connection's resource entries per MCP.md 3.3 - the producer-written kinds are listed
    unconditionally, so a run that skipped one still has a URI whose `read()` names the reason.

    `manifest_annotations` is the one conditional kind, human-authored and often absent.
    """

    print_root = conn.print_root
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
                f"What changed between the last two runs for connection {conn.name}, without "
                f"events about an edge a human rejected. get_diff returns it filtered by table or "
                f"kind"
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

    # An unreadable manifest withholds this connection's tables, never another's listing; its
    # manifest URI stays, and reading it names the error.
    manifest = read_manifest(print_root, read).manifest

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

    text, version = _read_unchanged(path)

    # A YAML artifact is served verbatim either way - this is a parseability check, not a
    # transform - but MCP.md 3 requires a parse failure to surface as -32603.
    if mime_type == "application/yaml":
        try:
            read(path)
        except yaml.YAMLError as exc:
            raise errors.yaml_parse_error(str(path), str(exc)) from exc

    return ReadResult(content=text, mime_type=mime_type, version=version)


def _without_rejected_edges(
    whole: ReadResult,
    manifest: dict[str, Any],
    print_root: Path,
    fqn: str,
    path: Path,
    read: ArtifactReader,
) -> ReadResult:
    """`relationships.yaml` less the edges a human rejected, versioned over the files deciding it.

    Verbatim, with the file's own version, when nothing is withheld (MCP.md 3.4).
    """

    raw = read(path)

    if not isinstance(raw, dict):
        return whole

    entry = walkable_tables(manifest)[fqn]
    own_name = declared_artifacts(entry).get("relationships_annotations")
    own_path = table_directory(print_root, fqn, entry) / own_name if own_name else None
    incoming, consulted = incoming_rejections(manifest, print_root, fqn, raw, read)
    shown = withhold_rejected(raw, _own_rejections(own_path, read), incoming, fqn)

    if shown is raw:
        return whole

    decided_by = [path, *([own_path] if own_path else []), *consulted]

    return ReadResult(
        content=dump_yaml(shown),
        mime_type=whole.mime_type,
        version=paging.files_digest(decided_by),
    )


def diff_without_rejected(
    data: dict[str, Any],
    manifest: dict[str, Any] | None,
    print_root: Path,
    read: ArtifactReader,
) -> tuple[dict[str, Any], tuple[Path, ...]]:
    """`diff.yaml` less its relationship events about a human-rejected edge (SPEC 2.7.2).

    `summary.relationships_changed` counts the events kept; the files read to decide are returned.
    """

    changes = [c for c in (data.get("changes") or []) if isinstance(c, dict)]
    tables = walkable_tables(manifest) if manifest else {}
    verdicts: dict[str, tuple[dict[tuple[Any, ...], Any], set[tuple[Any, ...]]]] = {}
    consulted: list[Path] = []
    kept: list[dict[str, Any]] = []

    for change in changes:
        source = change.get("source_table")

        if not str(change.get("kind", "")).startswith("relationship_") or source not in tables:
            kept.append(change)
            continue

        if source not in verdicts:
            verdicts[source] = _source_verdicts(print_root, source, tables[source], consulted, read)

        rejected, declared = verdicts[source]
        key = (
            tuple(change.get("source_column") or ()),
            change.get("target_table"),
            tuple(change.get("target_column") or ()),
        )

        if key not in rejected or key in declared:
            kept.append(change)

    shown = {**data, "changes": kept}
    summary = data.get("summary")
    changed = summary.get("relationships_changed") if isinstance(summary, dict) else None

    if len(kept) < len(changes) and isinstance(summary, dict) and isinstance(changed, int):
        shown["summary"] = {
            **summary,
            "relationships_changed": changed - (len(changes) - len(kept)),
        }

    return shown, tuple(consulted)


def _diff_without_rejected_events(
    whole: ReadResult,
    print_root: Path,
    path: Path,
    read: ArtifactReader,
) -> ReadResult:
    raw = read(path)

    if not isinstance(raw, dict):
        return whole

    manifest = read_manifest(print_root, read).manifest
    shown, consulted = diff_without_rejected(raw, manifest, print_root, read)

    if len(shown["changes"]) == len([c for c in (raw.get("changes") or []) if isinstance(c, dict)]):
        return whole

    return ReadResult(
        content=dump_yaml(shown),
        mime_type=whole.mime_type,
        version=paging.files_digest([path, *consulted]),
    )


def _source_verdicts(
    print_root: Path,
    source: str,
    entry: dict[str, Any],
    consulted: list[Path],
    read: ArtifactReader,
) -> tuple[dict[tuple[Any, ...], Any], set[tuple[Any, ...]]]:
    table_dir = table_directory(print_root, source, entry)
    artifacts = declared_artifacts(entry)
    rejected: dict[tuple[Any, ...], Any] = {}
    declared: set[tuple[Any, ...]] = set()

    if "relationships_annotations" in artifacts:
        path = table_dir / artifacts["relationships_annotations"]
        consulted.append(path)
        rejected = _own_rejections(path, read)

    if rejected and "relationships" in artifacts:
        path = table_dir / artifacts["relationships"]
        consulted.append(path)
        edges = (parsed_mapping(path, read) or {}).get("refers_to") or []
        declared = {
            edge_key(e) for e in edges if isinstance(e, dict) and edge_detection(e) == "declared"
        }

    return rejected, declared


def parsed_mapping(path: Path, read: ArtifactReader) -> dict[str, Any] | None:
    """A YAML artifact as a mapping; None when it is absent, unparseable or not a mapping."""

    if not path.is_file():
        return None

    try:
        data = read(path)
    except yaml.YAMLError:
        return None

    return data if isinstance(data, dict) else None


def _own_rejections(path: Path | None, read: ArtifactReader) -> dict[tuple[Any, ...], Any]:
    if path is None or not path.is_file():
        return {}

    try:
        data = read(path)
    except yaml.YAMLError:
        return {}

    entries = data.get("refers_to") if isinstance(data, dict) else None

    return rejected_edges(entries if isinstance(entries, list) else None)


def _read_unchanged(path: Path) -> tuple[str, str]:
    # The version must name the text it is served with, so a rewrite during the read retries.
    for _ in range(2):
        before = paging.files_digest([path])
        text = path.read_text(encoding="utf-8")

        if paging.files_digest([path]) == before:
            return text, before

    raise errors.stale_page_version(str(path))


def _load_manifest_or_none(print_root: Path, read: ArtifactReader) -> dict | None:
    return errors.manifest_or_error(read_manifest(print_root, read))
