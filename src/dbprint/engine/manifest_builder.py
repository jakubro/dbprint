"""Manifest assembly per SPEC 2.5: `build()` returns the dict serialized as `manifest.yaml`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dbprint import __version__ as DBPRINT_VERSION
from dbprint.adapters.base import TableType
from dbprint.spec.v1 import FORMAT_VERSION
from .diff import DiffSelectors


ARTIFACT_FILENAMES = {
    "ddl": "ddl.sql",
    "statistics": "statistics.yaml",
    "relationships": "relationships.yaml",
    "description": "description.md",
    "statistics_annotations": "statistics.annotations.yaml",
    "relationships_annotations": "relationships.annotations.yaml",
}


@dataclass(frozen=True)
class ManifestTableEntry:
    """One table's manifest payload - input to `build`.

    `max_age_days` is the freshness threshold that governed this table on the run writing
    the entry (SPEC 2.5); None means never recorded, and emits an absent key, not null.
    `statistics_params` carries only the keys where this table's resolved
    `StatisticsConfig` differs from the connection default (SPEC 2.5), None when it matched.
    """

    fqn: str
    type: TableType
    path: str
    has_statistics: bool
    has_relationships: bool
    has_description: bool
    has_statistics_annotations: bool
    has_relationships_annotations: bool
    row_count: int | None
    columns: int
    profiled_at: str
    max_age_days: int | None = None
    statistics_params: dict[str, Any] | None = None


def build(
    connection_name: str,
    adapter_kind: str,
    entries: list[ManifestTableEntry],
    generated_at: str,
    *,
    statistics_params: dict[str, Any],
    selectors: DiffSelectors,
    redaction_rules_configured: int,
    default_collation: str,
    has_manifest_annotations: bool = False,
) -> dict[str, Any]:
    """Return the manifest dict ready for YAML serialization.

    `statistics_params`/`selectors`/`redaction_rules_configured`/`default_collation` are the
    producer-provenance fields (SPEC 2.5): what this run resolved, so the print decodes
    itself without the `.dbprint.yaml` that produced it. `has_manifest_annotations` reports
    whether the human-authored `manifest.annotations.yaml` exists (SPEC 2.7.3).
    """

    tables: dict[str, dict[str, Any]] = {}

    for e in entries:
        artifacts: dict[str, str] = {"ddl": ARTIFACT_FILENAMES["ddl"]}

        if e.has_statistics:
            artifacts["statistics"] = ARTIFACT_FILENAMES["statistics"]

        if e.has_relationships:
            artifacts["relationships"] = ARTIFACT_FILENAMES["relationships"]

        if e.has_description:
            artifacts["description"] = ARTIFACT_FILENAMES["description"]

        if e.has_statistics_annotations:
            artifacts["statistics_annotations"] = ARTIFACT_FILENAMES["statistics_annotations"]

        if e.has_relationships_annotations:
            artifacts["relationships_annotations"] = ARTIFACT_FILENAMES["relationships_annotations"]

        table_payload: dict[str, Any] = {
            "type": e.type,
            "path": e.path,
            "artifacts": artifacts,
            "columns": e.columns,
            "profiled_at": e.profiled_at,
        }

        if e.row_count is not None:
            table_payload["row_count"] = e.row_count

        if e.max_age_days is not None:
            table_payload["max_age_days"] = e.max_age_days

        if e.statistics_params is not None:
            table_payload["statistics_params"] = e.statistics_params
        tables[e.fqn] = table_payload

    payload: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "generated_at": generated_at,
        "connection": connection_name,
        "adapter": adapter_kind,
        "dbprint_version": DBPRINT_VERSION,
        "statistics_params": statistics_params,
        "selectors": {"include": list(selectors.include), "exclude": list(selectors.exclude)},
        "redaction_rules_configured": redaction_rules_configured,
        "default_collation": default_collation,
    }

    if has_manifest_annotations:
        payload["manifest_annotations"] = "manifest.annotations.yaml"

    payload["tables"] = tables

    return payload


def entry_from_payload(fqn: str, payload: dict[str, Any]) -> ManifestTableEntry:
    """Rebuild an entry from a payload a previous run wrote.

    A run re-extracts only part of its scope, so tables it left alone reach the new manifest
    from the old one. `profiled_at` rides along unchanged, since advancing it would renew
    freshness for a table never re-read; `max_age_days` too, and an absent key stays absent.
    """

    artifacts = payload.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    statistics_params = payload.get("statistics_params")

    return ManifestTableEntry(
        fqn=fqn,
        type=payload.get("type", "table"),
        path=payload.get("path", ""),
        has_statistics="statistics" in artifacts,
        has_relationships="relationships" in artifacts,
        has_description="description" in artifacts,
        has_statistics_annotations="statistics_annotations" in artifacts,
        has_relationships_annotations="relationships_annotations" in artifacts,
        row_count=payload.get("row_count"),
        columns=payload.get("columns", 0),
        profiled_at=payload.get("profiled_at", ""),
        max_age_days=payload.get("max_age_days"),
        statistics_params=statistics_params if isinstance(statistics_params, dict) else None,
    )
