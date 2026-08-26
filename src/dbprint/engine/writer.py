"""Per-table atomic writer (temp + os.replace).

`write_atomic` is all-or-nothing: either every artifact lands in `tbl_dir` under its final
name, or none do and the existing ones are untouched. User content - the description and
annotation files - is never in the write set, for a per-table directory or a connection
root alike. Each artifact goes to a `<name>.tmp` sibling first (same filesystem, so
`os.replace` is atomic); a failure before the rename phase deletes the temps and re-raises.
"""

from __future__ import annotations

import os
from pathlib import Path


DESCRIPTION_FILENAME = "description.md"
STATISTICS_ANNOTATIONS_FILENAME = "statistics.annotations.yaml"
RELATIONSHIPS_ANNOTATIONS_FILENAME = "relationships.annotations.yaml"
MANIFEST_ANNOTATIONS_FILENAME = "manifest.annotations.yaml"


class WriterError(RuntimeError):
    """Raised when atomic writes cannot be completed; temps cleaned before raise."""


def write_atomic(tbl_dir: Path, artifacts: dict[str, str | bytes]) -> None:
    """Write `artifacts` to `tbl_dir` all-or-nothing.

    `artifacts` maps filename to its final content. A name with a path separator, or naming
    user content the engine never writes, raises `WriterError`.
    """

    for name in artifacts:
        _validate_name(name)

    tbl_dir.mkdir(parents=True, exist_ok=True)

    temps: list[tuple[Path, Path]] = []

    for name, content in artifacts.items():
        final = tbl_dir / name
        tmp = tbl_dir / (name + ".tmp")

        try:
            _write_one(tmp, content)
        except OSError as exc:
            _cleanup(t for t, _ in temps + [(tmp, final)])

            raise WriterError(f"failed writing {tmp}: {exc}") from exc

        temps.append((tmp, final))

    try:
        for tmp, final in temps:
            os.replace(tmp, final)
    except OSError as exc:
        _cleanup(t for t, _ in temps)

        raise WriterError(f"failed renaming temp artifacts in {tbl_dir}: {exc}") from exc


def _write_one(path: Path, content: str | bytes) -> None:
    mode = "wb" if isinstance(content, bytes) else "w"
    encoding = None if isinstance(content, bytes) else "utf-8"

    with path.open(mode, encoding=encoding) as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())


def _cleanup(paths) -> None:
    for p in paths:
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def _validate_name(name: str) -> None:
    if name in (
        DESCRIPTION_FILENAME,
        STATISTICS_ANNOTATIONS_FILENAME,
        RELATIONSHIPS_ANNOTATIONS_FILENAME,
        MANIFEST_ANNOTATIONS_FILENAME,
    ):
        raise WriterError(f"writer must not touch {name} — user content")

    if "/" in name or "\\" in name or name in (".", ".."):
        raise WriterError(f"invalid artifact filename: {name!r}")
