"""What the example generators share: a throwaway Postgres cluster, frozen run instants, the swap.

Loaded by path, since `scripts/` is not a package.
"""

from __future__ import annotations

import glob
import re
import secrets
import shutil
import socket
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, LiteralString, cast


# Every stamped timestamp collapses here, so regeneration diffs nothing but clocks.
FROZEN_TIMESTAMP = "2026-03-09T14:27:36Z"
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")

# Run instants only; `baseline.generated_at` matches for free, one level deeper.
INSTANT_KEYS = frozenset({"generated_at", "profiled_at", "scanned_at"})


def regenerate(
    label: str,
    database: str,
    build: Callable[[dict[str, str], Path], None],
    scratch: Path,
    connection: str,
    print_root: Path,
) -> int:
    """Provision a throwaway cluster, build the example into `scratch`, swap it in, report."""

    bin_dir = discover_postgres_bin_dir()
    data_dir = Path(f"/var/lib/postgresql/dbprint-{label}-" + secrets.token_hex(4))
    port = free_port()

    start_cluster(bin_dir, data_dir, port)

    try:
        credentials = {
            "host": "127.0.0.1",
            "port": str(port),
            "database": database,
            "user": "postgres",
            "password": "postgres",
        }
        create_database(credentials)
        build(credentials, scratch)
        swap_in(scratch, connection, print_root)
    finally:
        stop_cluster(bin_dir, data_dir)

    print(f"{label} example regenerated at {print_root}")

    return 0


def require_complete(result: Any, expected: frozenset[str]) -> None:
    """Refuse a run that did not profile every object the example illustrates.

    A failed table is dropped from the manifest, so an incomplete run still looks consistent.
    """

    failed = [t for t in result.tables if t.status == "failed"]

    if failed:
        detail = "\n".join(f"  {t.fqn}: {t.error} (at {t.error_operation})" for t in failed)

        raise SystemExit(f"generate failed on {len(failed)} object(s):\n{detail}")

    profiled = {t.fqn for t in result.tables}
    missing = expected - profiled

    if missing:
        raise SystemExit(f"generate never reached {sorted(missing)}; it saw {sorted(profiled)}")


def normalize_timestamps(print_root: Path) -> None:
    """Freeze only the run instants: `.yaml` lines keyed in `INSTANT_KEYS`, never a `.sql` literal.

    A temporal column's `range`/`percentiles` values are ISO instants too; a pattern would hit them.
    """

    for path in print_root.rglob("*.yaml"):
        if not path.is_file():
            continue

        lines = path.read_text().splitlines(keepends=True)
        changed = False

        for i, line in enumerate(lines):
            key = line.lstrip().split(":", 1)[0]

            if key not in INSTANT_KEYS:
                continue

            frozen = _TIMESTAMP_RE.sub(FROZEN_TIMESTAMP, line)

            if frozen != line:
                lines[i] = frozen
                changed = True

        if changed:
            path.write_text("".join(lines))


def swap_in(regenerated: Path, connection: str, print_root: Path) -> None:
    """Replace the committed print with the regenerated one, then drop the scratch tree."""

    shutil.rmtree(print_root, ignore_errors=True)
    shutil.copytree(regenerated / "prints" / connection, print_root)
    shutil.rmtree(regenerated)


def apply_sql(credentials: dict[str, str], statements: str) -> None:
    """Run one SQL script against the throwaway database."""

    import psycopg

    with psycopg.connect(dsn(credentials), autocommit=True) as conn:
        # SQL is from a controlled disk path; cast for psycopg's LiteralString overload.
        conn.execute(cast(LiteralString, statements))


def dsn(credentials: dict[str, str]) -> str:
    """A libpq connection string for `credentials`."""

    return (
        f"host={credentials['host']} port={credentials['port']} "
        f"dbname={credentials['database']} user={credentials['user']} "
        f"password={credentials['password']}"
    )


def create_database(credentials: dict[str, str]) -> None:
    """Recreate `credentials`' database empty on the throwaway cluster."""

    import psycopg
    from psycopg import sql

    admin = {**credentials, "database": "postgres"}
    name = sql.Identifier(credentials["database"])

    with psycopg.connect(dsn(admin), autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(name))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(name))


def discover_postgres_bin_dir() -> Path:
    """The newest installed Postgres bin directory."""

    matches = sorted(glob.glob("/usr/lib/postgresql/*/bin/initdb"))

    if not matches:
        raise SystemExit(
            "could not locate a Postgres bin directory. Install postgresql "
            "(Debian/Ubuntu: `apt install postgresql`).",
        )

    return Path(matches[-1]).parent


def require_extensions() -> None:
    """Refuse to start when the newest Postgres lacks PostGIS or pgvector to `CREATE EXTENSION`."""

    version = discover_postgres_bin_dir().parent.name
    packages = {
        "postgis": f"postgresql-{version}-postgis-3",
        "vector": f"postgresql-{version}-pgvector",
    }
    missing = [
        package
        for extension, package in packages.items()
        if not Path("/usr/share/postgresql", version, "extension", f"{extension}.control").exists()
    ]

    if missing:
        raise SystemExit(f"Postgres {version} is missing extensions. Install {', '.join(missing)}.")


def start_cluster(bin_dir: Path, data_dir: Path, port: int) -> None:
    """Initialise and start a trust-authenticated cluster on `port`, as the postgres user."""

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


def stop_cluster(bin_dir: Path, data_dir: Path) -> None:
    """Stop the cluster immediately and delete its data directory."""

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


def free_port() -> int:
    """A port the OS has just handed out on loopback."""

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))

        return int(sock.getsockname()[1])


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
