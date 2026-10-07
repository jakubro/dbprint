"""The files a print holds and the names SPEC 2.5 gives them."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any


MANIFEST_FILENAME = "manifest.yaml"
DIFF_FILENAME = "diff.yaml"
# Lowercase: SPEC 1.5.1's path-segment allowlist is case-sensitive.
READING_GUIDE_FILENAME = "reading.md"
MANIFEST_ANNOTATIONS_FILENAME = "manifest.annotations.yaml"

# A table directory's files by manifest `artifacts` kind, in the order the manifest lists them.
ARTIFACT_FILENAMES = MappingProxyType(
    {
        "ddl": "ddl.sql",
        "statistics": "statistics.yaml",
        "relationships": "relationships.yaml",
        "description": "description.md",
        "statistics_annotations": "statistics.annotations.yaml",
        "relationships_annotations": "relationships.annotations.yaml",
    },
)

DDL_FILENAME = ARTIFACT_FILENAMES["ddl"]
STATISTICS_FILENAME = ARTIFACT_FILENAMES["statistics"]
RELATIONSHIPS_FILENAME = ARTIFACT_FILENAMES["relationships"]
DESCRIPTION_FILENAME = ARTIFACT_FILENAMES["description"]
STATISTICS_ANNOTATIONS_FILENAME = ARTIFACT_FILENAMES["statistics_annotations"]
RELATIONSHIPS_ANNOTATIONS_FILENAME = ARTIFACT_FILENAMES["relationships_annotations"]

# What a run writes, and what only a human does (SPEC 2.4, 2.7).
PRODUCER_ARTIFACTS = (DDL_FILENAME, STATISTICS_FILENAME, RELATIONSHIPS_FILENAME)
USER_ARTIFACTS = (
    DESCRIPTION_FILENAME,
    STATISTICS_ANNOTATIONS_FILENAME,
    RELATIONSHIPS_ANNOTATIONS_FILENAME,
    MANIFEST_ANNOTATIONS_FILENAME,
)
CANONICAL_ARTIFACTS = frozenset(ARTIFACT_FILENAMES.values())


def walkable_tables(manifest: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """The manifest entries a reader can follow - mappings whose `path`, if any, is a string.

    One unusable entry drops its own table and nothing else; conformance is what reports it.
    """

    out: dict[str, dict[str, Any]] = {}

    for fqn, entry in ((manifest or {}).get("tables") or {}).items():
        if isinstance(entry, dict) and isinstance(entry.get("path", ""), str):
            out[fqn] = entry

    return out


def declared_artifacts(entry: Mapping[str, Any]) -> dict[str, str]:
    """The artifact filenames a reader can open from one manifest entry; non-strings drop."""

    artifacts = entry.get("artifacts") or {}

    if not isinstance(artifacts, dict):
        return {}

    return {kind: name for kind, name in artifacts.items() if isinstance(name, str)}
