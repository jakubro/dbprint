"""Column lineage traced from a dbt manifest's compiled SQL (SPEC 2.2.4 `derived_from`).

The one module importing sqlglot (the `[dbt]` extra), loaded only when a connection names a manifest.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.lineage import lineage
from sqlglot.schema import MappingSchema

from dbprint.spec.fqn import join as join_fqn
from dbprint.spec.fqn import split as split_fqn


# dbt 1.3 wrote v7, the first manifest with `compiled_code`; v12 is the newest this was read against.
MIN_SCHEMA_VERSION = 7
NEWEST_KNOWN_SCHEMA_VERSION = 12

DBT_ADAPTERS: Mapping[str, frozenset[str]] = {
    "postgres": frozenset({"postgres"}),
    "redshift": frozenset({"redshift"}),
    "snowflake": frozenset({"snowflake"}),
    "bigquery": frozenset({"bigquery"}),
    "duckdb": frozenset({"duckdb"}),
    "mysql": frozenset({"mysql"}),
    "clickhouse": frozenset({"clickhouse"}),
    "databricks": frozenset({"databricks", "spark"}),
}

_VERSION_RE = re.compile(r"/manifest/v(\d+)\.json$")
_TRACED_RESOURCES = frozenset({"model", "snapshot"})


class ManifestUnreadable(Exception):
    """The configured dbt manifest cannot be used; the message names the path and the fix."""


@dataclass(frozen=True, order=True)
class SourceColumn:
    """One column a model column is traced to: its table's print FQN and its folded name."""

    table: str
    column: str


@dataclass(frozen=True)
class DbtModel:
    """One compiled SQL model or snapshot, projected to what lineage reads."""

    unique_id: str
    relation_name: str
    database: str | None
    schema: str | None
    compiled_code: str


@dataclass(frozen=True)
class DbtManifest:
    """The compiled SQL models of one dbt manifest."""

    schema_version: int
    adapter_type: str | None
    models: tuple[DbtModel, ...]


@dataclass(frozen=True)
class LineageReading:
    """Per print FQN, each column's traced sources, plus what could not be parsed."""

    tables: Mapping[str, Mapping[str, tuple[SourceColumn, ...]]]
    unparsed: tuple[tuple[str, str], ...]
    models_parsed: int
    models_eligible: int


def load_manifest(path: Path) -> DbtManifest:
    """Read the manifest's compiled models; refuse one too old, unreadable or never compiled."""

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        message = f"dbt manifest {path}: cannot read it: {exc.strerror or exc}"

        raise ManifestUnreadable(message) from exc
    except ValueError as exc:
        message = f"dbt manifest {path}: not valid JSON: {exc}"

        raise ManifestUnreadable(message) from exc

    metadata = data.get("metadata") if isinstance(data, dict) else None
    metadata = metadata if isinstance(metadata, dict) else {}
    version_url = metadata.get("dbt_schema_version")
    match = _VERSION_RE.search(version_url) if isinstance(version_url, str) else None

    if match is None:
        message = f"dbt manifest {path}: no metadata.dbt_schema_version - is this a dbt manifest?"

        raise ManifestUnreadable(message)

    version = int(match.group(1))

    if version < MIN_SCHEMA_VERSION:
        message = (
            f"dbt manifest {path}: manifest v{version} predates dbt 1.3; recompile with dbt >= 1.3"
        )

        raise ManifestUnreadable(message)

    nodes = data.get("nodes")
    eligible = [
        node
        for node in (nodes.values() if isinstance(nodes, dict) else [])
        if isinstance(node, dict)
        and node.get("resource_type") in _TRACED_RESOURCES
        and node.get("language", "sql") == "sql"
        and isinstance(node.get("relation_name"), str)
    ]
    models = tuple(
        DbtModel(
            unique_id=str(node.get("unique_id")),
            relation_name=node["relation_name"],
            database=node.get("database") if isinstance(node.get("database"), str) else None,
            schema=node.get("schema") if isinstance(node.get("schema"), str) else None,
            compiled_code=node["compiled_code"],
        )
        for node in eligible
        if isinstance(node.get("compiled_code"), str)
    )

    if eligible and not models:
        message = (
            f"dbt manifest {path}: no model carries compiled SQL - it came from `dbt parse`; "
            f"run `dbt compile` first"
        )

        raise ManifestUnreadable(message)

    adapter_type = metadata.get("adapter_type")

    return DbtManifest(
        schema_version=version,
        adapter_type=adapter_type if isinstance(adapter_type, str) else None,
        models=models,
    )


def trace(
    manifest: DbtManifest,
    *,
    vendor: str,
    depth: int,
    columns: Mapping[str, Sequence[str]],
) -> LineageReading:
    """Each printed model column's direct source columns, read from its compiled SELECT.

    `columns` (print FQN -> folded names) resolves `SELECT *` and bare columns; unprinted models skip.
    """

    schema = _schema(columns, depth, vendor)
    tables: dict[str, dict[str, tuple[SourceColumn, ...]]] = {}
    unparsed: list[tuple[str, str]] = []
    eligible = 0

    for model in manifest.models:
        try:
            target = _fqn(_relation_parts(model.relation_name, vendor), depth)
        except (SqlglotError, ValueError) as exc:
            eligible += 1
            unparsed.append((model.unique_id, _cause(exc)))

            continue

        if target is None or target not in columns:
            continue

        eligible += 1

        try:
            traced = _trace_model(model, target, vendor=vendor, depth=depth, schema=schema)
        except (SqlglotError, ValueError) as exc:
            unparsed.append((model.unique_id, _cause(exc)))

            continue

        known = set(columns[target])

        if kept := {name: sources for name, sources in traced.items() if name in known}:
            tables[target] = kept

    return LineageReading(
        tables=tables,
        unparsed=tuple(unparsed),
        models_parsed=eligible - len(unparsed),
        models_eligible=eligible,
    )


def _trace_model(
    model: DbtModel,
    target: str,
    *,
    vendor: str,
    depth: int,
    schema: MappingSchema,
) -> dict[str, tuple[SourceColumn, ...]]:
    tree = sqlglot.parse_one(model.compiled_code, dialect=vendor)

    # Quoted, so a dialect that folds unquoted names upward (Snowflake) keeps the print's lowercase.
    for identifier in tree.find_all(exp.Identifier):
        identifier.set("this", identifier.name.lower())
        identifier.set("quoted", True)

    nodes = lineage(
        None,
        tree,
        schema=schema,
        dialect=vendor,
        catalog=(model.database or "").lower() or None,
        db=(model.schema or "").lower() or None,
    )
    out: dict[str, tuple[SourceColumn, ...]] = {}

    for name, node in nodes.items():
        sources = {
            SourceColumn(fqn, exp.to_column(leaf.name, dialect=vendor).name.lower())
            for leaf in node.walk()
            if not leaf.downstream and isinstance(leaf.expression, exp.Table)
            if (fqn := _fqn([part.name for part in leaf.expression.parts], depth)) is not None
            and fqn != target
        }

        if sources:
            out[name.lower()] = tuple(sorted(sources))

    return out


def _cause(exc: Exception) -> str:
    return str(exc).strip().splitlines()[0] if str(exc).strip() else ""


def _relation_parts(relation_name: str, vendor: str) -> list[str]:
    return [part.name for part in exp.to_table(relation_name, dialect=vendor).parts]


def _fqn(parts: Sequence[str], depth: int) -> str | None:
    if len(parts) < depth:
        return None

    return join_fqn([part.lower() for part in parts[-depth:]])


def _schema(columns: Mapping[str, Sequence[str]], depth: int, vendor: str) -> MappingSchema:
    nested: dict[str, Any] = {}

    for fqn, names in columns.items():
        parts = split_fqn(fqn)

        if len(parts) != depth:
            continue

        level = nested

        for part in parts[:-1]:
            level = level.setdefault(part, {})

        level[parts[-1]] = dict.fromkeys(names, "unknown")

    return MappingSchema(nested, dialect=vendor, normalize=False)
