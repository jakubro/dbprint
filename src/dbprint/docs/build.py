"""Crawl the docs app in-process and write a static site to disk.

Uses Flask's test client - no port binding, no subprocess.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from flask.testing import FlaskClient

from dbprint.config import ConnectionConfig
from . import catalogue
from .web import create_app


MARKER_FILENAME = ".dbprint-docs"
_STATIC_DIR = Path(__file__).parent / "static"


class OutputNotOwnedError(RuntimeError):
    """Raised when `--output` exists, carries no marker, and `--force` was not passed."""


@dataclass(frozen=True)
class BuildResult:
    """What one `build_site` call wrote - and, distinctly, what it could not."""

    output: Path
    pages_written: int
    failed_routes: tuple[str, ...] = ()


def build_site(
    connections: list[ConnectionConfig],
    output: Path,
    *,
    force: bool = False,
) -> BuildResult:
    """Recreate `output` from scratch with a full static crawl of `connections`' prints.

    Raises `OutputNotOwnedError` when `output` exists without this tool's marker and `force`
    is false.
    """

    _prepare_output(output, force=force)

    app = create_app(connections)
    client = app.test_client()
    conns = catalogue.load_connections(connections)

    pages = 0
    failed: list[str] = []

    def write(path: str, dest: Path) -> None:
        nonlocal pages

        if _write_page(client, path, dest):
            pages += 1
        else:
            failed.append(path)

    write("/", output / "index.html")

    for conn in conns:
        for name in sorted(conn.tables):
            write(f"/t/{conn.name}/{quote(name)}", output / "t" / conn.name / name / "index.html")

        for schema in sorted(_schemas(conn)):
            write(
                f"/s/{conn.name}/{quote(schema)}",
                output / "s" / conn.name / schema / "index.html",
            )

    shutil.copytree(_STATIC_DIR, output / "static", dirs_exist_ok=True)
    (output / MARKER_FILENAME).write_text(
        "dbprint docs build - safe to recreate.\n",
        encoding="utf-8",
    )

    return BuildResult(output=output, pages_written=pages, failed_routes=tuple(failed))


def _schemas(conn: catalogue.PrintConnection) -> set[str]:
    """Every real schema grouping - `(none)` is a sentinel, never a page."""

    return {catalogue.schema_key(t) for t in conn.tables} - {"(none)"}


def _prepare_output(output: Path, *, force: bool) -> None:
    """Remove and recreate `output`, refusing a directory this tool did not create."""

    if output.exists():
        if not (output / MARKER_FILENAME).is_file() and not force:
            raise OutputNotOwnedError(
                f"{output} exists and carries no {MARKER_FILENAME} marker - recreating it "
                f"would delete a directory this tool did not create. Pass --force to proceed "
                f"anyway.",
            )

        shutil.rmtree(output)

    output.mkdir(parents=True)


def _write_page(client: FlaskClient, path: str, dest: Path) -> bool:
    """Request one route and write its body to `dest`; `False` on a non-200 response."""

    response = client.get(path)

    if response.status_code != 200:
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(response.data)

    return True
