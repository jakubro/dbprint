"""Per-invocation run log under ~/.dbprint/logs/<project-slug>/ - see docs/CLI.md.

Producer telemetry only, never written into a print tree. The sink runs at DEBUG; the renderer
handler stays at WARNING, so raising the `dbprint` logger here reaches no terminal.
"""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import rich_click as click


LOGS_ROOT = Path("~/.dbprint/logs").expanduser()

_DBPRINT_LOGGER_NAME = "dbprint"
_RETENTION = 3
_STAMP_FORMAT = "%Y%m%dT%H%M%S_%f"
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
_LOG_NAME_RE = re.compile(r"^\d{8}T\d{6}_\d{6}-[a-z]+\.log$")

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunLogHandle:
    """An open sink; pass to `close_run_log` when the command exits."""

    handler: logging.Handler
    previous_level: int


def open_run_log(project_root: Path, command: str) -> RunLogHandle | None:
    """Open one log file for this invocation; None (plus one stderr warning) if it can't.

    Prunes to the 3 newest before opening, then raises the `dbprint` logger to DEBUG.
    """

    directory = LOGS_ROOT / _slug(project_root)

    try:
        directory.mkdir(parents=True, exist_ok=True)
        _prune(directory)
        path = directory / f"{_stamp()}-{command}.log"
        handler: logging.Handler = logging.FileHandler(path, encoding="utf-8")
    except OSError as exc:
        click.echo(f"warning: could not open run log under {directory}: {exc}", err=True)

        return None

    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger = logging.getLogger(_DBPRINT_LOGGER_NAME)
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)

    return RunLogHandle(handler=handler, previous_level=previous_level)


def close_run_log(handle: RunLogHandle | None) -> None:
    """Undo `open_run_log` - the counterpart every caller must run. `handle=None` is a no-op."""

    if handle is None:
        return

    logger = logging.getLogger(_DBPRINT_LOGGER_NAME)
    logger.removeHandler(handle.handler)
    logger.setLevel(handle.previous_level)
    handle.handler.close()


def install_stderr_warning_handler() -> logging.Handler:
    """WARNING-level stderr handler for a command with no progress renderer.

    `open_run_log` raises the `dbprint` logger off its default level, which stops
    `logging.lastResort` from firing; this replaces it.
    """

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.WARNING)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger(_DBPRINT_LOGGER_NAME).addHandler(handler)

    return handler


def remove_stderr_warning_handler(handler: logging.Handler) -> None:
    """Undo `install_stderr_warning_handler`."""

    logging.getLogger(_DBPRINT_LOGGER_NAME).removeHandler(handler)


def log_run_header(project_root: Path, connections: list[str]) -> None:
    """RUN record: what was invoked and against what config - the file's first line."""

    from dbprint import __version__

    _LOG.info(
        "run version=%s argv=%s config=%s connections=%s start=%s",
        __version__,
        sys.argv,
        project_root / ".dbprint.yaml",
        ",".join(connections),
        datetime.now(UTC).isoformat(timespec="seconds"),
    )


def log_run_summary(exit_code: int) -> None:
    """SUMMARY record: the run's own verdict, logged once the exit code is known."""

    _LOG.info("summary exit_code=%d", exit_code)


def _slug(project_root: Path) -> str:
    """Filesystem-safe encoding of the whole resolved project path, not just its name.

    Two projects sharing a directory name must not share a log directory - or a retention window.
    """

    return _SLUG_RE.sub("-", str(project_root.resolve())).strip("-")


def _stamp() -> str:
    return datetime.now(UTC).strftime(_STAMP_FORMAT)


def _prune(directory: Path) -> None:
    """Trim to the 2 newest files this sink wrote, reserving the 3rd slot for the new one.

    Matched on the naming scheme alone, never everything in the directory - a user may have put
    other files here. The stamp's fixed-width fields sort lexicographically by recency.
    """

    ours = sorted(
        (p for p in directory.iterdir() if p.is_file() and _LOG_NAME_RE.match(p.name)),
        reverse=True,
    )

    for stale in ours[_RETENTION - 1 :]:
        stale.unlink(missing_ok=True)
