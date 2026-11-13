"""Read a dbprint print: connections, tables, and their on-disk artifacts.

Pure I/O and parsing - no presentation logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from dbprint.config import ConnectionConfig
from dbprint.engine.baseline import (
    PrintReader,
    failed_tables,
    missing_artifacts,
    read_manifest,
    table_directory,
    unprofiled_message,
)
from dbprint.spec import authored
from dbprint.spec.artifacts import declared_artifacts, walkable_tables
from dbprint.spec.fqn import join as join_fqn
from dbprint.spec.fqn import split as split_fqn


@dataclass(frozen=True)
class PrintConnection:
    """One connection's committed print: its manifest and the tables a reader can walk.

    `read` parses each of its files once, for as long as this load is used.
    """

    name: str
    root: Path
    manifest: dict[str, Any]
    tables: dict[str, dict[str, Any]]
    failed_tables: tuple[str, ...] = ()
    read: PrintReader = field(default_factory=PrintReader, compare=False)


@dataclass(frozen=True)
class TableArtifacts:
    """One table's on-disk artifacts, parsed and bundled by kind; any kind may be absent.
    `corrupted` is declared and present but unparseable - not `missing`, and not a `None` value.
    """

    fqn: str
    entry: dict[str, Any]
    ddl: str | None
    statistics: dict[str, Any] | None
    relationships: dict[str, Any] | None
    description: str | None
    statistics_annotations: dict[str, Any] | None
    relationships_annotations: dict[str, Any] | None
    missing: tuple[str, ...]
    corrupted: tuple[str, ...] = ()


@dataclass(frozen=True)
class PrefixTree:
    """One level of a dotted-name prefix tree: direct leaves, then nested groups."""

    leaves: tuple[str, ...]
    groups: dict[str, PrefixTree]


def load_connections(connections: list[ConnectionConfig]) -> list[PrintConnection]:
    """Load every connection that has a readable manifest on disk, in the order given.

    The loaded set reads each file once however many pages it serves; a later load reads afresh.
    """

    loaded = []
    read = PrintReader()

    for conn in connections:
        root = conn.print_root
        manifest = read_manifest(root, read).manifest

        if manifest is None:
            continue

        loaded.append(
            PrintConnection(
                name=conn.name,
                root=root,
                manifest=manifest,
                tables=walkable_tables(manifest),
                failed_tables=failed_tables(manifest),
                read=read,
            ),
        )

    return loaded


def find_connection(connections: list[PrintConnection], name: str) -> PrintConnection | None:
    """The loaded connection with this name, or None when it was never loaded."""

    return next((c for c in connections if c.name == name), None)


def load_table(conn: PrintConnection, fqn: str) -> TableArtifacts | None:
    """Read one table's artifacts, or None when the manifest names no such table."""

    entry = conn.tables.get(fqn)

    if entry is None:
        return None

    table_dir = table_directory(conn.root, fqn, entry)
    artifacts = declared_artifacts(entry)
    corrupted: list[str] = []

    return TableArtifacts(
        fqn=fqn,
        entry=entry,
        ddl=_read_text(table_dir, artifacts, "ddl"),
        statistics=_read_yaml(conn.read, table_dir, artifacts, "statistics", corrupted),
        relationships=_read_yaml(conn.read, table_dir, artifacts, "relationships", corrupted),
        description=_read_text(table_dir, artifacts, "description"),
        statistics_annotations=_read_yaml(
            conn.read,
            table_dir,
            artifacts,
            "statistics_annotations",
            corrupted,
        ),
        relationships_annotations=_read_yaml(
            conn.read,
            table_dir,
            artifacts,
            "relationships_annotations",
            corrupted,
        ),
        missing=missing_artifacts(table_dir, artifacts),
        corrupted=tuple(corrupted),
    )


def load_artifact(conn: PrintConnection, fqn: str, kind: str) -> dict[str, Any] | None:
    """One table's YAML artifact of `kind`, without reading its other artifacts."""

    entry = conn.tables.get(fqn)

    if entry is None:
        return None

    table_dir = table_directory(conn.root, fqn, entry)
    artifacts = declared_artifacts(entry)

    return _read_yaml(conn.read, table_dir, artifacts, kind)


def schema_key(table_name: str) -> str:
    """A table's schema-equivalent grouping key - every dotted-name segment but the leaf."""

    parts = split_fqn(table_name)

    return join_fqn(parts[:-1]) if len(parts) > 1 else "(none)"


def tables_in_schema(conn: PrintConnection, schema: str) -> dict[str, dict[str, Any]]:
    """Every table entry in one schema, keyed by FQN."""

    return {name: entry for name, entry in conn.tables.items() if schema_key(name) == schema}


def nav_tree(connections: list[PrintConnection]) -> dict[str, PrefixTree]:
    """Build a per-connection table-name tree, nested by dotted-name prefix."""

    return {c.name: prefix_tree(sorted(c.tables)) for c in connections}


def table_target(conn_name: str, fqn: str) -> str:
    """The URL path for one table's page, both segments percent-encoded."""

    return f"/t/{quote(conn_name, safe='')}/{quote(fqn, safe='')}"


def leaf_targets(conn: PrintConnection, current: str) -> dict[str, str]:
    """Map every table's leaf name to its page URL, for linkifying mentions in prose.

    A leaf name can collide across schemas; the table in the same schema as `current` wins.
    """

    current_schema = schema_key(current)
    targets: dict[str, str] = {}

    for t in sorted(conn.tables):
        leaf = split_fqn(t)[-1]

        if leaf not in targets or schema_key(t) == current_schema:
            targets[leaf] = table_target(conn.name, t)

    return targets


def prefix_tree(names: list[str], prefix_len: int = 0) -> PrefixTree:
    """Nest dotted names into a tree keyed by shared prefix segments (database, schema, ...)."""

    groups: dict[str, list[str]] = {}
    leaves: list[str] = []

    for n in names:
        rest = split_fqn(n)[prefix_len:]

        if len(rest) <= 1:
            leaves.append(n)
        else:
            groups.setdefault(rest[0], []).append(n)

    return PrefixTree(
        leaves=tuple(sorted(leaves)),
        groups={g: prefix_tree(ns, prefix_len + 1) for g, ns in sorted(groups.items())},
    )


def _read_yaml_status(read: PrintReader, path: Path) -> tuple[dict[str, Any] | None, bool]:
    """`(None, False)` covers absent; `(None, True)` is present but unparseable or not a mapping."""

    if not path.is_file():
        return None, False

    try:
        data = read(path)
    except yaml.YAMLError:
        return None, True

    return (data, False) if isinstance(data, dict) else (None, True)


def _read_text(table_dir: Path, artifacts: dict[str, str], kind: str) -> str | None:
    filename = artifacts.get(kind)

    if filename is None:
        return None

    path = table_dir / filename

    return path.read_text(encoding="utf-8") if path.is_file() else None


def _read_yaml(
    read: PrintReader,
    table_dir: Path,
    artifacts: dict[str, str],
    kind: str,
    corrupted: list[str] | None = None,
) -> dict[str, Any] | None:
    filename = artifacts.get(kind)

    if filename is None:
        return None

    path = table_dir / filename
    data, is_corrupted = _read_yaml_status(read, path)

    if kind in authored.AUTHORED_KINDS and data is not None:
        parsed = data
        data = read.once(("admitted", path, kind), lambda: authored.admitted(kind, parsed))
        is_corrupted = data is None

    if is_corrupted and corrupted is not None:
        corrupted.append(kind)

    return data


def unprofiled_note(conn: PrintConnection, fqn: str) -> str | None:
    """The phrase a page shows for a table the last run could not profile, else None."""

    return unprofiled_message(fqn) if fqn in conn.failed_tables else None
