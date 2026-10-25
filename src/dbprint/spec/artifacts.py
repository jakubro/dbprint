"""The files a print holds and the names SPEC 2.5 gives them."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from types import MappingProxyType
from typing import Any

from .fqn import directory as fqn_directory


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


def walkable_tables(
    manifest: Mapping[str, Any] | None,
    links: Collection[str] = (),
) -> dict[str, dict[str, Any]]:
    """The manifest entries a reader can follow - mappings whose `path`, if any, is a string,
    and that lead nowhere outside the connection root (SPEC 2.5).

    `links` are the print-root-relative links that resolve outside it; without them only the
    lexical rule applies. One unusable entry drops its own table; conformance reports it.
    """

    refused = outside_print(manifest, links)

    return {fqn: entry for fqn, entry in _shaped(manifest).items() if fqn not in refused}


def outside_print(
    manifest: Mapping[str, Any] | None,
    links: Collection[str] = (),
) -> dict[str, str]:
    """Each otherwise followable entry that leads outside the connection root, with the cause."""

    out: dict[str, str] = {}

    for fqn, entry in _shaped(manifest).items():
        path = table_path(str(fqn), entry)

        if _LEAVING_PATH_RE.search(path):
            out[fqn] = f"its path {path!r} leaves the connection root"
        elif cause := _linked_out(path, entry, links):
            out[fqn] = cause

    return out


def table_path(fqn: str, entry: Mapping[str, Any]) -> str:
    """One table's directory relative to the connection root: its `path`, or its FQN's form."""

    path = entry.get("path")

    return path if isinstance(path, str) and path else fqn_directory(fqn)


def opened_paths(fqn: str, entry: Mapping[str, Any]) -> dict[str, str]:
    """Each path a reader passes through or opens for one entry - every directory on the way, its
    own as written, and every declared artifact - keyed the way `outside_print` looks it up.
    """

    path = table_path(fqn, entry)
    parts = _parts(path)

    return {
        **{"/".join(parts[:depth]): "/".join(parts[:depth]) for depth in range(1, len(parts))},
        "/".join(parts): path,
        **{
            "/".join((*parts, name)): f"{path}/{name}"
            for name in declared_artifacts(entry).values()
        },
    }


def declared_artifacts(entry: Mapping[str, Any]) -> dict[str, str]:
    """The artifact filenames a reader can open from one manifest entry: each kind's own name."""

    artifacts = entry.get("artifacts") or {}

    if not isinstance(artifacts, dict):
        return {}

    return {kind: name for kind, name in artifacts.items() if ARTIFACT_FILENAMES.get(kind) == name}


# The `path` forms SPEC 2.5 bars a consumer from following: absolute, a drive letter, a `..` segment.
_LEAVING_PATH_RE = re.compile(r"^(?:[/\\]|[A-Za-z]:)|(?:^|[/\\])\.\.(?:[/\\]|$)")


def _shaped(manifest: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    return {
        fqn: entry
        for fqn, entry in ((manifest or {}).get("tables") or {}).items()
        if isinstance(entry, dict) and isinstance(entry.get("path", ""), str)
    }


def _linked_out(path: str, entry: Mapping[str, Any], links: Collection[str]) -> str | None:
    if not links:
        return None

    parts = _parts(path)

    for depth in range(1, len(parts) + 1):
        if (prefix := "/".join(parts[:depth])) in links:
            return f"{prefix} is a symlink to a directory outside the connection root"

    names = declared_artifacts(entry).values()

    for name in names:
        if (key := "/".join((*parts, name))) in links:
            return f"{key} is a symlink to a file outside the connection root"

    # Some readers join an absent `path` as the connection root itself.
    for name in names if not entry.get("path") else ():
        if name in links:
            return f"{name} is a symlink to a file outside the connection root"

    return None


def _parts(path: str) -> list[str]:
    return [part for part in path.split("/") if part not in ("", ".")]
