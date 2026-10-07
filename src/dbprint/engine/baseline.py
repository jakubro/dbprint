"""Readers of a committed manifest's shape, shared by every surface that walks a print.

A malformed artifact degrades to absent - the smallest unit holding the defect drops.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from dbprint.spec import artifact_yaml
from dbprint.spec.artifacts import MANIFEST_FILENAME, walkable_tables
from dbprint.spec.fqn import directory as fqn_directory


_LOG = logging.getLogger(__name__)

# Parses one artifact file, raising OSError or `yaml.YAMLError` as `read_artifact` does.
ArtifactReader = Callable[[Path], Any]

_UNMEASURED_BLOCK_MESSAGE = {
    "physical_layout": 'this run did not measure it, so whether a key is declared is unknown, not "none".',
    "null_patterns": (
        "this run did not measure it, so which columns are null on the same rows is unknown, "
        'not "none".'
    ),
    "dependencies": "this run did not measure it, so no dependency between columns is ruled out.",
}


def read_artifact(path: Path) -> Any:
    """Parse one artifact file afresh."""

    return artifact_yaml.load(path.read_text(encoding="utf-8"))


ManifestState = Literal["absent", "unparseable", "malformed", "ok"]


@dataclass(frozen=True)
class ManifestRead:
    """One connection's manifest as every surface reads it; `reason` says why it is unusable.

    `manifest` keeps only `walkable_tables` entries: an unusable entry drops its own table alone.
    """

    path: Path
    state: ManifestState
    manifest: dict[str, Any] | None = None
    reason: str | None = None


def read_manifest(print_root: Path, read: ArtifactReader = read_artifact) -> ManifestRead:
    """Read `print_root`'s manifest; an empty file is unusable, never an empty print."""

    path = print_root / MANIFEST_FILENAME

    if not path.is_file():
        return ManifestRead(path, "absent")

    try:
        data = read(path)
    except yaml.YAMLError as exc:
        return ManifestRead(path, "unparseable", reason=str(exc))

    reason = manifest_shape_error(data) or (None if isinstance(data, dict) else "the file is empty")

    if reason is not None:
        return ManifestRead(path, "malformed", reason=reason)

    return ManifestRead(path, "ok", {**data, "tables": walkable_tables(data)})


def unusable_manifest_message(read: ManifestRead, connection: str) -> str:
    """How every CLI surface names a manifest it cannot read; missing and unusable stay apart.

    Conflating them sends the user to `generate`, which overwrites the print instead of fixing it.
    """

    if read.state == "absent":
        return f"no manifest at {read.path}. Run `dbprint generate {connection}` first."
    elif read.state == "unparseable":
        return f"could not parse {read.path}: {read.reason}"
    else:
        return f"ignoring {read.path}: {read.reason}"


def load_baseline_manifest(prints_root: Path) -> dict[str, Any] | None:
    """Load `prints/<connection>/manifest.yaml` if present and usable; otherwise None."""

    manifest = prints_root / MANIFEST_FILENAME

    if not manifest.is_file():
        return None

    try:
        data = read_artifact(manifest)
    except yaml.YAMLError:
        return None

    reason = manifest_shape_error(data)

    if reason is not None:
        _LOG.warning("ignoring %s: %s", manifest, reason)

        return None

    return data if isinstance(data, dict) else None


def manifest_shape_error(data: Any) -> str | None:
    """Why a parsed manifest is unusable, or None when its readers can walk it.

    Usable means the document is a mapping and its `tables` is one too. An empty file is
    usable-as-absent; a written-but-empty `tables` is not, `dict.get` being unable to tell
    it from a never-written key.
    """

    if data is None:
        return None

    if not isinstance(data, dict):
        return f"expected a mapping, found {type(data).__name__}"

    if "tables" in data and not isinstance(data["tables"], dict):
        found = "nothing" if data["tables"] is None else type(data["tables"]).__name__

        return f"`tables` must be a mapping of table name to entry, found {found}"

    return None


def failed_tables(manifest: Mapping[str, Any] | None) -> tuple[str, ...]:
    """The tables the writing run attempted and could not profile (SPEC 2.5); `()` when absent.

    A malformed value reads as empty here; conformance reports it.
    """

    listed = manifest.get("failed_tables") if manifest else None

    if not isinstance(listed, list):
        return ()

    return tuple(fqn for fqn in listed if isinstance(fqn, str))


def unmeasured_block_message(name: str) -> str:
    """How every surface words a table-level block the file's `unmeasured` list names (SPEC 2.2.1)."""

    return _UNMEASURED_BLOCK_MESSAGE.get(
        name,
        "this run did not measure it; its content is unknown.",
    )


def unprofiled_message(fqn: str) -> str:
    """How every surface names a table the last run could not profile."""

    return f"the last generate run could not profile {fqn!r}; run dbprint generate to see the cause"


def table_directory(print_root: Path, fqn: str, entry: dict[str, Any]) -> Path:
    """One table's on-disk directory: its declared `path`, or the FQN's own slash form.

    The FQN fallback covers a manifest predating the field, or an entry omitting it - never
    the connection root.
    """

    path = entry.get("path")

    return print_root / (path if isinstance(path, str) and path else fqn_directory(fqn))


def missing_artifacts(table_dir: Path, artifacts: dict[str, Any]) -> tuple[str, ...]:
    """Declared kinds whose file is absent from `table_dir`, sorted - a broken promise.

    Not a kind `artifacts` never names, and not a corrupt-but-present file (SPEC 2.5).
    """

    return tuple(
        sorted(kind for kind, name in artifacts.items() if not (table_dir / name).is_file()),
    )
