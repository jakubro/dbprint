"""Per-table atomic writer (temp + os.replace), called only by the run's stage (`staging.py`).

All-or-nothing per directory; description and annotation files are never in the write set.
"""

from __future__ import annotations

import os
from pathlib import Path

from dbprint.spec.artifacts import USER_ARTIFACTS


class WriterError(RuntimeError):
    """Raised when atomic writes cannot be completed; temps cleaned before raise."""


def write_atomic(tbl_dir: Path, artifacts: dict[str, str | bytes]) -> None:
    """Write `artifacts` to `tbl_dir` all-or-nothing.

    `artifacts` maps filename to its final content. A name with a path separator, or naming
    user content the engine never writes, raises `WriterError`.
    """

    for name in artifacts:
        validate_artifact_name(name)

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


def validate_artifact_name(name: str) -> None:
    """Raise `WriterError` for a name naming user content or not a bare file name."""

    if name in USER_ARTIFACTS:
        raise WriterError(f"writer must not touch {name} — user content")

    if "/" in name or "\\" in name or name in (".", ".."):
        raise WriterError(f"invalid artifact filename: {name!r}")


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
