"""Manifest assembly per SPEC 2.5: `build()` returns the dict serialized as `manifest.yaml`."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from dbprint import __version__ as DBPRINT_VERSION
from dbprint.adapters.base import TableType
from dbprint.config import ConnectionConfig, StatisticsConfig
from dbprint.spec.artifacts import ARTIFACT_FILENAMES, MANIFEST_ANNOTATIONS_FILENAME
from dbprint.spec.v1 import FORMAT_VERSION
from .diff import DiffSelectors


@dataclass(frozen=True)
class ManifestTableEntry:
    """One table's manifest payload - input to `build`.

    The params blocks carry only keys differing from the connection's (SPEC 2.5); None means they matched.
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
    max_rows_scanned: int | None = None
    profiling_params: dict[str, Any] | None = None


def statistics_params_dict(cfg: StatisticsConfig) -> dict[str, Any]:
    """A `StatisticsConfig` as the manifest records it, `percentiles` as a list."""

    values = asdict(cfg)
    values["percentiles"] = list(values["percentiles"])

    return values


def profiling_params_dict(conn: ConnectionConfig) -> dict[str, bool]:
    """The connection's profiling switches as the manifest records them (SPEC 2.5)."""

    return {
        "infer_relationships": conn.infer_relationships,
        "sketch_all_columns": conn.sketch_all_columns,
        "compute_timeline": conn.compute_timeline,
        "materialize_sample": conn.materialize_sample,
    }


def build(
    connection_name: str,
    adapter_kind: str,
    entries: list[ManifestTableEntry],
    generated_at: str,
    *,
    statistics_params: dict[str, Any],
    profiling_params: dict[str, Any],
    selectors: DiffSelectors,
    redaction_rules_configured: int,
    default_collation: str,
    failed_tables: tuple[str, ...] = (),
    has_manifest_annotations: bool = False,
) -> dict[str, Any]:
    """Return the manifest dict ready for YAML serialization.

    The provenance fields record what this run resolved, so the print decodes itself (SPEC 2.5).
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

        if e.max_rows_scanned is not None:
            table_payload["max_rows_scanned"] = e.max_rows_scanned

        if e.profiling_params is not None:
            table_payload["profiling_params"] = e.profiling_params
        tables[e.fqn] = table_payload

    payload: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "generated_at": generated_at,
        "connection": connection_name,
        "adapter": adapter_kind,
        "dbprint_version": DBPRINT_VERSION,
        "statistics_params": statistics_params,
        "profiling_params": profiling_params,
        "selectors": {"include": list(selectors.include), "exclude": list(selectors.exclude)},
        "redaction_rules_configured": redaction_rules_configured,
        "default_collation": default_collation,
    }

    if failed_tables:
        payload["failed_tables"] = sorted(failed_tables)

    if has_manifest_annotations:
        payload["manifest_annotations"] = MANIFEST_ANNOTATIONS_FILENAME

    payload["tables"] = tables

    return payload
