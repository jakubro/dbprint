"""Manifest assembly per SPEC 2.5: `build()` returns the dict serialized as `manifest.yaml`."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from dbprint import __version__ as DBPRINT_VERSION
from dbprint.adapters.base import TableType
from dbprint.config import ProfileConfig, ReadConfig
from dbprint.config.read import MODE_PARAMETERS
from dbprint.spec.artifacts import ARTIFACT_FILENAMES, MANIFEST_ANNOTATIONS_FILENAME
from dbprint.spec.v1 import FORMAT_VERSION
from .diff import DiffSelectors


@dataclass(frozen=True)
class ManifestTableEntry:
    """One table's manifest payload - input to `build`.

    `profile` carries only the blocks differing from the connection's (SPEC 2.5); None if none do.
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
    profile: dict[str, Any] | None = None
    max_rows_scanned: int | None = None


def profile_dict(profile: ProfileConfig) -> dict[str, Any]:
    """A resolved profile as the manifest records it (SPEC 2.5), without `freshness` or `redact`.

    A switched-off feature keeps no parameters, and a read block only its mode's.
    """

    statistics = asdict(profile.statistics)
    statistics["percentiles"] = list(statistics["percentiles"])

    return normalized_profile(
        {
            "read": {"tables": _read(profile.read.tables), "views": _read(profile.read.views)},
            "statistics": statistics,
            "looks_like": asdict(profile.looks_like),
            "null_patterns": asdict(profile.null_patterns),
            "parts": asdict(profile.parts),
            "timeline": asdict(profile.timeline),
            "sketches": asdict(profile.sketches),
            "relationships": asdict(profile.relationships),
        },
    )


def normalized_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    """`profile` with each off feature's parameters and each read block's foreign ones dropped."""

    out: dict[str, Any] = {}

    for name, block in profile.items():
        if name == "read" and isinstance(block, Mapping):
            out[name] = {
                kind: {
                    key: value
                    for key, value in read.items()
                    if key == "mode" or key in MODE_PARAMETERS.get(read.get("mode"), ())
                }
                if isinstance(read, Mapping)
                else read
                for kind, read in block.items()
            }
        elif isinstance(block, Mapping) and block.get("enabled") is False:
            out[name] = {"enabled": False}
        else:
            out[name] = block

    return out


def profile_delta(profile: Mapping[str, Any], base: Mapping[str, Any]) -> dict[str, Any] | None:
    """The blocks of `profile` that differ from `base`, each whole; None when none do.

    Blocks, not leaves: a sample moving from `fraction` to `max_rows` must not keep the `fraction`.
    """

    delta: dict[str, Any] = {}

    for name, block in profile.items():
        if name == "read" and isinstance(block, Mapping):
            base_read = base.get("read")
            base_read = base_read if isinstance(base_read, Mapping) else {}

            if read := {k: v for k, v in block.items() if base_read.get(k) != v}:
                delta["read"] = read
        elif base.get(name) != block:
            delta[name] = block

    return delta or None


def _read(read: ReadConfig) -> dict[str, Any]:
    return {
        "mode": read.mode,
        **{
            key: value
            for key, value in asdict(read).items()
            if key != "mode" and value is not None and key in MODE_PARAMETERS[read.mode]
        },
    }


def build(
    connection_name: str,
    adapter_kind: str,
    entries: list[ManifestTableEntry],
    generated_at: str,
    *,
    profile: dict[str, Any],
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

        if e.profile is not None:
            table_payload["profile"] = e.profile

        if e.max_rows_scanned is not None:
            table_payload["max_rows_scanned"] = e.max_rows_scanned

        tables[e.fqn] = table_payload

    payload: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "generated_at": generated_at,
        "connection": connection_name,
        "adapter": adapter_kind,
        "dbprint_version": DBPRINT_VERSION,
        "profile": profile,
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
