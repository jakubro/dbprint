"""Read a dbprint print: connections, tables, and their on-disk artifacts.

Pure I/O and parsing - no presentation logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from dbprint.config import ConnectionConfig
from dbprint.engine.baseline import (
    declared_artifacts,
    manifest_shape_error,
    missing_artifacts,
    walkable_tables,
)


_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


@dataclass(frozen=True)
class PrintConnection:
    """One connection's committed print: its manifest and the tables a reader can walk."""

    name: str
    root: Path
    manifest: dict[str, Any]
    tables: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class TableArtifacts:
    """One table's on-disk artifacts, parsed and bundled by kind. Any kind may be absent."""

    fqn: str
    entry: dict[str, Any]
    ddl: str | None
    statistics: dict[str, Any] | None
    relationships: dict[str, Any] | None
    description: str | None
    statistics_annotations: dict[str, Any] | None
    relationships_annotations: dict[str, Any] | None
    missing: tuple[str, ...]


@dataclass(frozen=True)
class PrefixTree:
    """One level of a dotted-name prefix tree: direct leaves, then nested groups."""

    leaves: tuple[str, ...]
    groups: dict[str, PrefixTree]


def load_connections(connections: list[ConnectionConfig]) -> list[PrintConnection]:
    """Load every connection that has a readable manifest on disk, in the order given."""

    loaded = []

    for conn in connections:
        root = conn.output / conn.name
        manifest = _read_yaml_mapping(root / "manifest.yaml")

        if manifest is None or manifest_shape_error(manifest) is not None:
            continue

        loaded.append(
            PrintConnection(
                name=conn.name,
                root=root,
                manifest=manifest,
                tables=walkable_tables(manifest),
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

    table_dir = conn.root / entry.get("path", fqn.replace(".", "/"))
    artifacts = declared_artifacts(entry)

    return TableArtifacts(
        fqn=fqn,
        entry=entry,
        ddl=_read_text(table_dir, artifacts, "ddl"),
        statistics=_read_yaml(table_dir, artifacts, "statistics"),
        relationships=_read_yaml(table_dir, artifacts, "relationships"),
        description=_read_text(table_dir, artifacts, "description"),
        statistics_annotations=_read_yaml(table_dir, artifacts, "statistics_annotations"),
        relationships_annotations=_read_yaml(table_dir, artifacts, "relationships_annotations"),
        missing=missing_artifacts(table_dir, artifacts),
    )


def load_relationships(conn: PrintConnection, fqn: str) -> dict[str, Any] | None:
    """One table's `relationships.yaml`, without reading its other artifacts."""

    entry = conn.tables.get(fqn)

    if entry is None:
        return None

    table_dir = conn.root / entry.get("path", fqn.replace(".", "/"))
    artifacts = declared_artifacts(entry)

    return _read_yaml(table_dir, artifacts, "relationships")


def schema_key(table_name: str) -> str:
    """A table's schema-equivalent grouping key - every dotted-name segment but the leaf."""

    parts = table_name.split(".")

    return ".".join(parts[:-1]) if len(parts) > 1 else "(none)"


def tables_in_schema(conn: PrintConnection, schema: str) -> dict[str, dict[str, Any]]:
    """Every table entry in one schema, keyed by FQN."""

    return {name: entry for name, entry in conn.tables.items() if schema_key(name) == schema}


def nav_tree(connections: list[PrintConnection]) -> dict[str, PrefixTree]:
    """Build a per-connection table-name tree, nested by dotted-name prefix."""

    return {c.name: prefix_tree(sorted(c.tables)) for c in connections}


def table_target(conn_name: str, fqn: str) -> str:
    """The URL path for one table's page."""

    from urllib.parse import quote

    return f"/t/{conn_name}/{quote(fqn)}"


def leaf_targets(conn: PrintConnection, current: str) -> dict[str, str]:
    """Map every table's leaf name to its page URL, for linkifying mentions in prose.

    A leaf name can collide across schemas; the table in the same schema as `current` wins.
    """

    current_schema = schema_key(current)
    targets: dict[str, str] = {}

    for t in sorted(conn.tables):
        leaf = t.split(".")[-1]

        if leaf not in targets or schema_key(t) == current_schema:
            targets[leaf] = table_target(conn.name, t)

    return targets


def prefix_tree(names: list[str], prefix_len: int = 0) -> PrefixTree:
    """Nest dotted names into a tree keyed by shared prefix segments (database, schema, ...)."""

    groups: dict[str, list[str]] = {}
    leaves: list[str] = []

    for n in names:
        rest = n.split(".")[prefix_len:]

        if len(rest) <= 1:
            leaves.append(n)
        else:
            groups.setdefault(rest[0], []).append(n)

    return PrefixTree(
        leaves=tuple(sorted(leaves)),
        groups={g: prefix_tree(ns, prefix_len + 1) for g, ns in sorted(groups.items())},
    )


def _read_yaml_mapping(path: Path) -> dict[str, Any] | None:
    """Parse a YAML file as a mapping, or None when absent, unparseable, or not a mapping."""

    if not path.is_file():
        return None

    try:
        data = yaml.load(path.read_text(), _LOADER)
    except yaml.YAMLError:
        return None

    return data if isinstance(data, dict) else None


def _read_text(table_dir: Path, artifacts: dict[str, str], kind: str) -> str | None:
    filename = artifacts.get(kind)

    if filename is None:
        return None

    path = table_dir / filename

    return path.read_text() if path.is_file() else None


def _read_yaml(table_dir: Path, artifacts: dict[str, str], kind: str) -> dict[str, Any] | None:
    filename = artifacts.get(kind)

    if filename is None:
        return None

    return _read_yaml_mapping(table_dir / filename)
