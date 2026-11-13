"""Crawl the docs app and write a static site to disk.

Uses Flask's test client - no port binding; a large site's table pages render in worker processes.
"""

from __future__ import annotations

import math
import multiprocessing
import os
import shutil
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from flask.testing import FlaskClient

from dbprint.config import ConnectionConfig
from dbprint.engine.context_assembler import neighbour_weight
from . import catalogue
from .web import create_app


MARKER_FILENAME = ".dbprint-docs"
_STATIC_DIR = Path(__file__).parent / "static"
# Below this many table pages a worker process's start costs more than the pages it would render.
_PARALLEL_MIN_PAGES = 100
_PAGES_PER_PROCESS = 64


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
    workers: int | None = None,
) -> BuildResult:
    """Recreate `output` from scratch with a full static crawl of `connections`' prints.

    The print is read once for every page; a site of many table pages renders them in up to
    `workers` processes, the machine's cores by default. Raises `OutputNotOwnedError` when
    `output` exists without this tool's marker and `force` is false.
    """

    _prepare_output(output, force=force)

    workers = (os.process_cpu_count() or 1) if workers is None else workers
    conns = catalogue.load_connections(connections)
    tables: list[tuple[str, Path, int]] = []
    others: list[tuple[str, Path]] = [("/", output / "index.html")]
    order = ["/"]

    for conn in conns:
        for name in sorted(conn.tables):
            path = f"/t/{conn.name}/{quote(name)}"
            weight = neighbour_weight(conn.root, name, conn.tables[name])
            tables.append((path, output / "t" / conn.name / name / "index.html", weight))
            order.append(path)

        for schema in sorted(_schemas(conn)):
            path = f"/s/{conn.name}/{quote(schema)}"
            others.append((path, output / "s" / conn.name / schema / "index.html"))
            order.append(path)

    page_workers = min(workers, math.ceil(len(tables) / _PAGES_PER_PROCESS))
    page_workers = page_workers if workers > 1 and len(tables) >= _PARALLEL_MIN_PAGES else 0
    # The schema pages render here while the page workers run, so they get the cores left over.
    app = create_app(connections, static=True, prints=conns, workers=max(1, workers - page_workers))
    client = app.test_client()

    if page_workers:
        written = _write_in_processes(client, connections, tables, others, page_workers)
    else:
        written = {path: _write_page(client, path, dest) for path, dest, _ in tables}
        written |= {path: _write_page(client, path, dest) for path, dest in others}

    failed = [path for path in order if not written[path]]

    shutil.copytree(_STATIC_DIR, output / "static", dirs_exist_ok=True)
    (output / MARKER_FILENAME).write_text(
        "dbprint docs build - safe to recreate.\n",
        encoding="utf-8",
    )

    return BuildResult(
        output=output,
        pages_written=len(order) - len(failed),
        failed_routes=tuple(failed),
    )


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


def _write_in_processes(
    client: FlaskClient,
    connections: list[ConnectionConfig],
    tables: list[tuple[str, Path, int]],
    others: list[tuple[str, Path]],
    processes: int,
) -> dict[str, bool]:
    """Table pages in worker processes, heaviest first; index and schema pages here meanwhile."""

    with ProcessPoolExecutor(
        processes,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_start_page_worker,
        initargs=(connections,),
    ) as pool:
        heaviest_first = sorted(tables, key=lambda table: -table[2])
        pending = {
            path: pool.submit(_write_in_worker, path, dest) for path, dest, _ in heaviest_first
        }
        written = {path: _write_page(client, path, dest) for path, dest in others}

        return written | {path: future.result() for path, future in pending.items()}


# A worker process's own app over its own load of the prints, set once by `_start_page_worker`.
_PAGE_WORKER: dict[str, Any] = {}


def _start_page_worker(connections: list[ConnectionConfig]) -> None:
    prints = catalogue.load_connections(connections)
    _PAGE_WORKER["client"] = create_app(connections, static=True, prints=prints).test_client()


def _write_in_worker(path: str, dest: Path) -> bool:
    return _write_page(_PAGE_WORKER["client"], path, dest)


def _write_page(client: FlaskClient, path: str, dest: Path) -> bool:
    """Request one route and write its body to `dest`; `False` on a non-200 response."""

    response = client.get(path)

    if response.status_code != 200:
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(response.data)

    return True
