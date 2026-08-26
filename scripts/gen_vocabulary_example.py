"""Regenerate the v1 vocabulary example from the producer, against a real Postgres.

A small second print alongside `docs/format/v1/examples/production/`, covering the
`looks_like` values the seed-bank domain has no honest column for. Run via
`just example-vocabulary`; golden-tested by tests/conformance/test_reference_example.py.
"""

from __future__ import annotations

import glob
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, LiteralString, cast


REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_ROOT = REPO_ROOT / "docs/format/v1/examples/vocabulary"
PRINT_ROOT = EXAMPLE_ROOT / "prints/vocabulary"
SQL_DIR = Path(__file__).resolve().parent / "sql"

DATABASE = "vocabulary"
CONNECTION = "vocabulary"

# Every stamped timestamp collapses here, matching the production example's convention.
FROZEN_TIMESTAMP = "2026-05-17T22:48:01Z"
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")
_INSTANT_KEYS = frozenset({"generated_at", "profiled_at", "scanned_at"})

SCHEMA_SQL = (SQL_DIR / "vocabulary_schema.sql").read_text()
SEED_SQL = (SQL_DIR / "vocabulary_seed.sql").read_text()


def build_example(credentials: dict[str, str], target: Path) -> None:
    """Generate the whole example tree into `target` from a live database.

    Creates the schema in the caller's own database, so it must be one the caller can lose.
    """

    from dbprint.adapters.postgres import PostgresAdapter
    from dbprint.config import load_project
    from dbprint.engine import Engine, GenerateRequest

    _apply_sql(credentials, SCHEMA_SQL)
    _apply_sql(credentials, SEED_SQL)

    if target.exists():
        shutil.rmtree(target)

    target.mkdir(parents=True)
    shutil.copytree(EXAMPLE_ROOT, target, dirs_exist_ok=True, ignore=_ignore_prints)

    project = load_project(target)
    conn_config = project.connections[CONNECTION]

    result = Engine(PostgresAdapter(credentials), conn_config, target).generate(
        GenerateRequest(force=True),
    )
    _require_complete(result)

    _normalize_timestamps(target / "prints" / CONNECTION)


EXPECTED_OBJECTS = frozenset({"public.shapes"})


def _require_complete(result: Any) -> None:
    """Refuse a run that did not profile the one object this example illustrates."""

    failed = [t for t in result.tables if t.status == "failed"]

    if failed:
        detail = "\n".join(f"  {t.fqn}: {t.error} (at {t.error_operation})" for t in failed)

        raise SystemExit(f"generate failed on {len(failed)} object(s):\n{detail}")

    profiled = {t.fqn for t in result.tables}
    missing = EXPECTED_OBJECTS - profiled

    if missing:
        raise SystemExit(f"generate never reached {sorted(missing)}; it saw {sorted(profiled)}")


def _ignore_prints(directory: str, names: list[str]) -> set[str]:
    return {"prints"} if Path(directory) == EXAMPLE_ROOT else set()


def _normalize_timestamps(print_root: Path) -> None:
    """Freeze only the run instants, by line-scoped key rather than by pattern."""

    for path in print_root.rglob("*.yaml"):
        if not path.is_file():
            continue

        lines = path.read_text().splitlines(keepends=True)
        changed = False

        for i, line in enumerate(lines):
            key = line.lstrip().split(":", 1)[0]

            if key not in _INSTANT_KEYS:
                continue

            frozen = _TIMESTAMP_RE.sub(FROZEN_TIMESTAMP, line)

            if frozen != line:
                lines[i] = frozen
                changed = True

        if changed:
            path.write_text("".join(lines))


def _apply_sql(credentials: dict[str, str], statements: str) -> None:
    import psycopg

    with psycopg.connect(_dsn(credentials), autocommit=True) as conn:
        # SQL is from a controlled disk path; cast for psycopg's LiteralString overload.
        conn.execute(cast(LiteralString, statements))


def _dsn(credentials: dict[str, str]) -> str:
    return (
        f"host={credentials['host']} port={credentials['port']} "
        f"dbname={credentials['database']} user={credentials['user']} "
        f"password={credentials['password']}"
    )


def _main() -> int:
    """Provision a throwaway cluster, regenerate the committed example, report."""

    bin_dir = _discover_postgres_bin_dir()
    data_dir = Path("/var/lib/postgresql/dbprint-vocabulary-" + secrets.token_hex(4))
    port = _free_port()

    _start_cluster(bin_dir, data_dir, port)

    try:
        credentials = {
            "host": "127.0.0.1",
            "port": str(port),
            "database": DATABASE,
            "user": "postgres",
            "password": "postgres",
        }
        _create_database(credentials)
        build_example(credentials, EXAMPLE_ROOT.parent / "_vocabulary_regenerated")
        _swap_in(EXAMPLE_ROOT.parent / "_vocabulary_regenerated")
    finally:
        _stop_cluster(bin_dir, data_dir)

    print(f"vocabulary example regenerated at {PRINT_ROOT}")

    return 0


def _swap_in(regenerated: Path) -> None:
    shutil.rmtree(PRINT_ROOT, ignore_errors=True)
    shutil.copytree(regenerated / "prints" / CONNECTION, PRINT_ROOT)
    shutil.rmtree(regenerated)


def _create_database(credentials: dict[str, str]) -> None:
    import psycopg
    from psycopg import sql

    admin = {**credentials, "database": "postgres"}
    name = sql.Identifier(DATABASE)

    with psycopg.connect(_dsn(admin), autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(name))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(name))


def _discover_postgres_bin_dir() -> Path:
    matches = sorted(glob.glob("/usr/lib/postgresql/*/bin/initdb"))

    if not matches:
        raise SystemExit(
            "could not locate a Postgres bin directory. Install postgresql "
            "(Debian/Ubuntu: `apt install postgresql`).",
        )

    return Path(matches[-1]).parent


def _run_as_postgres(cmd: list[str]) -> None:
    quoted = " ".join(f"'{part}'" for part in cmd)
    result = subprocess.run(
        ["su", "postgres", "-s", "/bin/bash", "-c", quoted],
        check=False,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise SystemExit(
            f"command failed ({result.returncode}): {quoted}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}",
        )


def _start_cluster(bin_dir: Path, data_dir: Path, port: int) -> None:
    _run_as_postgres(["mkdir", "-p", str(data_dir)])
    _run_as_postgres(["chmod", "700", str(data_dir)])
    _run_as_postgres(
        [
            str(bin_dir / "initdb"),
            "-D",
            str(data_dir),
            "--auth-local=trust",
            "--username=postgres",
            "-E",
            "UTF8",
        ],
    )
    _run_as_postgres(
        [
            str(bin_dir / "pg_ctl"),
            "-D",
            str(data_dir),
            "-o",
            f"-p {port} -h 127.0.0.1",
            "-l",
            str(data_dir / "postgres.log"),
            "-w",
            "start",
        ],
    )
    _wait_for_postgres(port)


def _stop_cluster(bin_dir: Path, data_dir: Path) -> None:
    subprocess.run(
        [
            "su",
            "postgres",
            "-s",
            "/bin/bash",
            "-c",
            f"'{bin_dir / 'pg_ctl'}' -D '{data_dir}' -m immediate stop",
        ],
        check=False,
        capture_output=True,
    )
    subprocess.run(["rm", "-rf", str(data_dir)], check=False)


def _wait_for_postgres(port: int, timeout: float = 20.0) -> None:
    import psycopg

    deadline = time.time() + timeout
    last: Exception | None = None

    while time.time() < deadline:
        try:
            with psycopg.connect(
                f"host=127.0.0.1 port={port} dbname=postgres user=postgres",
            ):
                return
        except Exception as exc:  # noqa: BLE001 - poll until ready or timeout; any error retries
            last = exc
            time.sleep(0.2)

    raise SystemExit(f"postgres did not become ready on port {port}: {last}")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))

        return int(sock.getsockname()[1])


if __name__ == "__main__":
    os.environ.setdefault("PGPASSWORD", "postgres")
    sys.exit(_main())
