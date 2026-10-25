"""Readers of a committed manifest's shape, shared by every surface that walks a print.

A malformed artifact degrades to absent - the smallest unit holding the defect drops.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from dbprint.spec import artifact_yaml
from dbprint.spec.artifacts import (
    MANIFEST_FILENAME,
    PRODUCER_ARTIFACTS,
    opened_paths,
    table_path,
    walkable_tables,
)


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

_DISABLED_CONSEQUENCE = {
    "looks_like": (
        'no column\'s value shape was detected; an absent looks_like is not "no pattern"'
    ),
    "null_patterns": 'which columns are null on the same rows is unknown, not "none"',
    "parts": "no nested column was described part by part",
    "relationships.infer": "no foreign key was inferred from its column names",
    "sketches": "no value sketch was taken and no join was measured from one",
    "timeline": "its activity over time and each column's populated span are unknown",
}


def read_artifact(path: Path) -> Any:
    """Parse one artifact file afresh."""

    return artifact_yaml.load(path.read_text(encoding="utf-8"))


ManifestState = Literal["absent", "unparseable", "malformed", "ok"]


@dataclass(frozen=True)
class ManifestRead:
    """One connection's manifest as every surface reads it; `reason` says why it is unusable.

    `manifest` keeps only `walkable_tables` entries; `dropped` names the entries it left out.
    """

    path: Path
    state: ManifestState
    manifest: dict[str, Any] | None = None
    reason: str | None = None
    dropped: tuple[str, ...] = ()


def read_manifest(print_root: Path, read: ArtifactReader = read_artifact) -> ManifestRead:
    """Read `print_root`'s manifest; an empty file is unusable, never an empty print."""

    path = print_root / MANIFEST_FILENAME

    if not path.is_file():
        return ManifestRead(path, "absent")

    if not contained(print_root, path):
        return ManifestRead(path, "malformed", reason="it resolves outside the connection root")

    try:
        data = read(path)
    except yaml.YAMLError as exc:
        return ManifestRead(path, "unparseable", reason=str(exc))

    reason = manifest_shape_error(data) or (None if isinstance(data, dict) else "the file is empty")

    if reason is not None:
        return ManifestRead(path, "malformed", reason=reason)

    walkable = walkable_tables(data, escaping_links(print_root, data))
    dropped = tuple(sorted(str(fqn) for fqn in data.get("tables") or {} if fqn not in walkable))

    return ManifestRead(path, "ok", {**data, "tables": walkable}, dropped=dropped)


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


def generate_refusal(read: ManifestRead, print_root: Path, connection: str) -> str | None:
    """Why `generate` must not write over this baseline, or None when it is safe to.

    A manifest the run cannot fully read would cost the prints of every table it does not re-read.
    """

    until = "generate writes nothing until then."

    if read.state in ("unparseable", "malformed"):
        return (
            f"{unusable_manifest_message(read, connection)}. Repair or restore it (a merge "
            f"leaves conflict markers in it); {until}"
        )

    if read.state == "ok" and read.dropped:
        return (
            f"{read.path} has entries generate cannot follow: {', '.join(read.dropped)} (an "
            "entry is a mapping whose `path` is a relative string, and neither its directory nor "
            "a file it declares leads outside the connection root; `dbprint check` names the "
            f"cause). Repair them, or delete them to have a listed table re-read as new; {until}"
        )

    listed = (read.manifest or {}).get("failed_tables", [])

    if not isinstance(listed, list) or not all(isinstance(fqn, str) for fqn in listed):
        return f"{read.path}: `failed_tables` must be a list of table names. Repair it; {until}"

    if read.state == "absent":
        printed = {
            path.parent
            for path in print_root.rglob("*")
            if path.name in PRODUCER_ARTIFACTS and path.is_file() and path.parent != print_root
        }

        if printed:
            return (
                f"no manifest at {read.path}, but {len(printed)} table directories below it hold "
                f"producer-written files. Restore it (`git checkout -- {read.path}`), or delete "
                f"{print_root} to start the print over; {until}"
            )

    return None


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


def disabled_feature_message(name: str) -> str:
    """How every surface words a measurement the file's `disabled` list names (SPEC 2.2.21)."""

    consequence = _DISABLED_CONSEQUENCE.get(name, "its reading is unknown, not absent")

    return f"not measured: switched off for this table, so {consequence}."


def unprofiled_message(fqn: str) -> str:
    """How every surface names a table the last run could not profile."""

    return f"the last generate run could not profile {fqn!r}; run dbprint generate to see the cause"


def table_directory(print_root: Path, fqn: str, entry: dict[str, Any]) -> Path:
    """One table's on-disk directory: its declared `path`, or the FQN's own slash form.

    The FQN fallback covers a manifest predating the field, or an entry omitting it - never
    the connection root.
    """

    return print_root / table_path(fqn, entry)


def missing_artifacts(table_dir: Path, artifacts: dict[str, Any]) -> tuple[str, ...]:
    """Declared kinds whose file is absent from `table_dir`, sorted - a broken promise.

    Not a kind `artifacts` never names, and not a corrupt-but-present file (SPEC 2.5).
    """

    return tuple(
        sorted(kind for kind, name in artifacts.items() if not (table_dir / name).is_file()),
    )


def escaping_links(
    print_root: Path,
    manifest: Mapping[str, Any] | None = None,
) -> frozenset[str]:
    """The print-root-relative POSIX paths of the links below `print_root` resolving outside it.

    A link to a directory inside the root is walked once more under its own path, so a link out
    below it is found where a reader reaches it; an unreadable directory is skipped. Every path
    an entry of `manifest` opens is resolved as well, catching a spelling no walk reaches.
    """

    root = print_root.resolve()
    found: set[str] = set()
    # Each directory carries the resolved directories above it, so a link back up is not walked.
    pending: list[tuple[Path, tuple[Path, ...]]] = [(print_root, (root,))]

    while pending:
        directory, chain = pending.pop()

        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)

                    if entry.is_symlink() or entry.is_junction():
                        target = path.resolve()

                        if not target.is_relative_to(root):
                            found.add(path.relative_to(print_root).as_posix())
                        elif target.is_dir() and target not in chain:
                            pending.append((path, (*chain, target)))
                    elif entry.is_dir(follow_symlinks=False):
                        pending.append((path, (*chain, chain[-1] / entry.name)))
        except OSError:
            continue

    for fqn, entry in walkable_tables(manifest).items():
        found.update(
            key
            for key, path in opened_paths(str(fqn), entry).items()
            if not contained(print_root, print_root / path)
        )

    return frozenset(found)


def contained(print_root: Path, path: Path) -> bool:
    """True when `path`, links followed, lies inside `print_root`; never for one that will not
    resolve (a NUL byte in a name).
    """

    try:
        return path.resolve().is_relative_to(print_root.resolve())
    except ValueError:
        return False
