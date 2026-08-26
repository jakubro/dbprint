"""Shared pytest fixtures and helpers spanning multiple test suites.

`postgres_cluster` is session-scoped and shared by the adapter-contract and integration
suites; it runs local `initdb` + `pg_ctl` as the system `postgres` user, so it needs a
postgresql-server install but no Docker.
"""

from __future__ import annotations

import fcntl
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest

from dbprint.cli import run_log
from tests import _containment
from tests._provisioning import INSTALL_LOCK_PATH, discover_or_install, in_container


# Not a plausible timestamp, so a normalized payload cannot pass for one a producer wrote.
INSTANT_PLACEHOLDER = "<instant>"

_INSTANT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")

# Run instants only - a temporal column's range/percentiles are ISO instants too.
# Mirrors gen_reference_example.py's _INSTANT_KEYS.
_INSTANT_KEYS = frozenset({"generated_at", "profiled_at", "scanned_at"})

# The print the package ships, and the only one carrying real producer output.
_COMMITTED_PRINTS = Path(__file__).resolve().parent.parent / "docs/format/v1/examples"
_COMMITTED_PRINTS = _COMMITTED_PRINTS / "production/prints"

# One fixed path outside any per-cluster data_dir (which does not exist yet when the lock
# is first needed), so every xdist worker in the run agrees on the same lock file.
_CLUSTER_BOOTSTRAP_LOCK_PATH = Path("/tmp/dbprint--test-cluster-bootstrap.lock")

# Set on the re-exec, so the sandboxed process does not sandbox itself again.
_SANDBOX_MARKER = "DBPRINT_TEST_SANDBOXED"

# Host read-only, one writable scratch, no network but loopback, and nothing outliving the
# run. `/tmp` is created sticky because the servers write there under their own accounts.
_SANDBOX_ARGV = (
    "--ro-bind",
    "/",
    "/",
    "--dev",
    "/dev",
    "--proc",
    "/proc",
    "--perms",
    "1777",
    "--tmpfs",
    "/tmp",
    "--unshare-net",
    "--die-with-parent",
)


def pytest_configure(config: pytest.Config) -> None:
    """Pin the rendering environment the help assertions compare against.

    rich enables color when it detects CI, and `FORCE_COLOR` outranks `NO_COLOR`, so an ambient
    CI variable puts escape sequences into `--help`. The variables are removed rather than set
    falsy: rich reads their presence, not their value. `TERM` is left alone, since the progress
    renderer selects on a dumb terminal and pinning it would decide tests about that choice.
    """

    del config

    _reexec_under_sandbox()

    os.environ["NO_COLOR"] = "1"

    for name in ("FORCE_COLOR", "CLICOLOR_FORCE", "CI", "GITHUB_ACTIONS"):
        os.environ.pop(name, None)


def _reexec_under_sandbox() -> None:
    """Replace this process with one whose only writable mount is its own scratch tree.

    Runs before collection, so nothing a fixture does can reach the machine underneath.
    Skipped inside a container, where that machine is already disposable and namespace
    creation is usually unavailable. A missing `bwrap` on a host raises rather than
    continuing: a sandbox that quietly does not run is worse than none.
    """

    if os.environ.get(_SANDBOX_MARKER) or in_container():
        return

    bwrap = shutil.which("bwrap")

    if bwrap is None:
        raise RuntimeError(
            "bwrap not found on PATH, and the suite sandboxes itself outside a container so "
            "a stray write cannot reach the machine. Install bubblewrap, or run in a container.",
        )

    os.environ[_SANDBOX_MARKER] = "1"

    os.execv(bwrap, [bwrap, *_SANDBOX_ARGV, sys.executable, "-m", "pytest", *sys.argv[1:]])


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Remove the lock files the run created, once no worker can still take one.

    Only the controller does it. xdist finishes the controller after every worker, so a peer
    cannot be holding a lock this deletes.
    """

    if hasattr(session.config, "workerinput"):
        return

    for path in (_CLUSTER_BOOTSTRAP_LOCK_PATH, INSTALL_LOCK_PATH):
        path.unlink(missing_ok=True)


@pytest.fixture(scope="session", autouse=True)
def _contained_to_scratch() -> Iterator[None]:
    """Fail the session when it left anything outside the scratch tree."""

    before = _containment.snapshot()

    yield

    problems = []
    appeared = _containment.escaped(before, _containment.snapshot())
    strays = _containment.suite_entries()

    if appeared:
        problems.append(f"new entries under a private root: {appeared}")

    if strays:
        problems.append(f"suite-named entries outside the scratch tree: {strays}")

    if problems:
        raise AssertionError("; ".join(problems))


@pytest.fixture(scope="session", autouse=True)
def _redirect_run_log(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Point the run-log sink at a scratch dir for the whole session - never ~/.dbprint/logs.

    Set directly rather than via `monkeypatch`: a function-scoped redirect is still unpatched
    when the first session-scoped fixture invokes the CLI.
    """

    run_log.LOGS_ROOT = tmp_path_factory.mktemp("dbprint-logs")


@pytest.fixture
def committed_print(tmp_path: Path) -> Path:
    """A writable copy of the print the package ships, one per test.

    Copying costs about five milliseconds, so each test gets its own tree; no test can
    observe another's mutation, and one that needs to tamper tampers in place.
    """

    destination = tmp_path / "prints"
    shutil.copytree(_COMMITTED_PRINTS, destination)

    return destination


@contextmanager
def _serialize_cluster_bootstrap() -> Iterator[None]:
    """Hold an exclusive cross-process lock across a live-cluster bootstrap.

    Cluster fixtures are session-scoped per xdist worker, so several can bootstrap at once,
    and concurrent `mariadb-install-db` runs fail with `ERROR: 1051 Unknown table
    'mysql.tmp_user_sys'`. Only the bootstrap is serialized, not a cluster's lifetime.
    """

    _CLUSTER_BOOTSTRAP_LOCK_PATH.touch(exist_ok=True)

    with _CLUSTER_BOOTSTRAP_LOCK_PATH.open("r+") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)

        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def normalize_instants(text: str) -> str:
    """Collapse what a clock decided, so two producer runs compare on content alone.

    Every run stamps its own instants, so comparing raw payloads holds only when the pair lands
    inside one second. Scoped to keys in `_INSTANT_KEYS`, so a temporal column's own
    `range`/`percentiles` still show a real difference.
    """

    return "".join(_normalize_instant_line(line) for line in text.splitlines(keepends=True))


def _normalize_instant_line(line: str) -> str:
    key = line.lstrip().split(":", 1)[0].strip().strip('"')

    if key not in _INSTANT_KEYS:
        return line

    return _INSTANT_RE.sub(INSTANT_PLACEHOLDER, line)


def normalize_print_tree(root: Path) -> dict[str, str]:
    """Read a print tree into {relative path: text} with `normalize_instants` applied."""

    return {
        str(path.relative_to(root)): normalize_instants(path.read_text())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@dataclass
class PostgresCluster:
    """Running ephemeral Postgres cluster reusable across tests."""

    port: int
    superuser: str = "postgres"


@pytest.fixture(scope="session")
def postgres_cluster() -> Iterator[PostgresCluster]:
    """Start an ephemeral cluster once per session; tear down on session end."""

    bin_dir = _discover_postgres_bin_dir()
    data_dir = _scratch_root() / ("dbprint-test-postgres-" + secrets.token_hex(4))
    port = _free_port()

    # `initdb` refuses a directory it does not own, hence the mode-0700 create as the
    # account the server runs as.
    try:
        with _serialize_cluster_bootstrap():
            _run_as_postgres(["mkdir", "-p", str(data_dir)])
            _run_as_postgres(["chmod", "700", str(data_dir)])
            _run_as_postgres(
                [
                    str(bin_dir / "initdb"),
                    "-D",
                    str(data_dir),
                    "--auth-host=trust",
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

            _wait_for_postgres("127.0.0.1", port, timeout=10.0)

        yield PostgresCluster(port=port)
    finally:
        _run_as_postgres(
            [str(bin_dir / "pg_ctl"), "-D", str(data_dir), "-m", "immediate", "stop"],
            check=False,
        )
        _run_as_postgres(["rm", "-rf", str(data_dir)], check=False)


def _scratch_root() -> Path:
    """The first ephemeral directory the server accounts can reach.

    Postgres runs as its own account, so it needs a root that account can both enter and
    create in - the sticky mode a system temp directory carries. A container may mount the
    platform temp directory privately, so a second candidate is tried before giving up.
    Raising here beats letting `initdb` fail, whose error names the path, not the reason.
    """

    wanted = stat.S_IXOTH | stat.S_IWOTH

    for candidate in (Path(tempfile.gettempdir()), Path("/var/tmp")):
        if candidate.is_dir() and candidate.stat().st_mode & wanted == wanted:
            return candidate

    raise RuntimeError(
        "no scratch directory the server accounts can write to: tried "
        f"{tempfile.gettempdir()} and /var/tmp",
    )


def _discover_postgres_bin_dir() -> Path:
    """Find the directory holding initdb / pg_ctl, installing postgresql in-container."""

    initdb = discover_or_install(
        "initdb",
        apt_packages=("postgresql", "postgresql-client"),
        candidate_globs=("/usr/lib/postgresql/*/bin/initdb",),
        host_install_hint=(
            "Could not locate Postgres bin dir. Install postgresql-<version> "
            "(Debian/Ubuntu: `apt install postgresql`)."
        ),
    )

    return initdb.parent


def _run_as_postgres(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a command as the system 'postgres' user."""

    return subprocess.run(
        ["su", "postgres", "-s", "/bin/bash", "-c", " ".join(_shell_quote(p) for p in cmd)],
        check=check,
        capture_output=True,
        text=True,
    )


def _shell_quote(arg: str) -> str:
    if arg and all(c.isalnum() or c in "/_-.,=:@" for c in arg):
        return arg

    return "'" + arg.replace("'", "'\\''") + "'"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))

        return s.getsockname()[1]


def _wait_for_postgres(host: str, port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None

    while time.monotonic() < deadline:
        try:
            with psycopg.connect(
                host=host,
                port=port,
                dbname="postgres",
                user="postgres",
                password="",
                connect_timeout=2,
            ):
                return
        except psycopg.Error as exc:
            last_exc = exc
            time.sleep(0.1)

    raise RuntimeError(
        f"postgres did not become ready on {host}:{port} within {timeout}s: {last_exc}",
    )


@dataclass
class MysqlCluster:
    """Running ephemeral MariaDB instance reusable across tests.

    MariaDB serves the same wire protocol as Oracle MySQL; parity against real MySQL is
    covered by the environment-gated live suite.
    """

    port: int
    superuser: str = "root"


@pytest.fixture(scope="session")
def mysql_cluster() -> Iterator[MysqlCluster]:
    """Start an ephemeral MariaDB instance once per session; tear down on session end."""

    install_db = _discover_mysql_tool("mariadb-install-db")
    mariadbd = _discover_mysql_tool("mariadbd", candidate_globs=("/usr/sbin/mariadbd",))
    data_dir = _scratch_root() / ("dbprint-test-mariadb-" + secrets.token_hex(4))
    port = _free_port()
    socket_path = data_dir / "mysqld.sock"

    # Bound before the `try` so the `finally` can tell "never started" from "started and
    # must be killed" - a readiness wait that expires would otherwise leave the server up.
    proc: subprocess.Popen[bytes] | None = None

    try:
        data_dir.mkdir(parents=True, exist_ok=True)

        with _serialize_cluster_bootstrap():
            subprocess.run(
                [
                    str(install_db),
                    "--no-defaults",
                    f"--datadir={data_dir}",
                    "--auth-root-authentication-method=normal",
                    "--user=root",
                    "--skip-test-db",
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            proc = subprocess.Popen(
                [
                    str(mariadbd),
                    "--no-defaults",
                    f"--datadir={data_dir}",
                    f"--socket={socket_path}",
                    f"--port={port}",
                    "--bind-address=127.0.0.1",
                    "--user=root",
                    f"--pid-file={data_dir / 'mariadb.pid'}",
                    f"--log-error={data_dir / 'mariadb.err'}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            _wait_for_mysql("127.0.0.1", port, timeout=30.0)

        yield MysqlCluster(port=port)
    finally:
        if proc is not None:
            proc.terminate()

            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

        shutil.rmtree(data_dir, ignore_errors=True)


def _discover_mysql_tool(binary: str, candidate_globs: tuple[str, ...] = ()) -> Path:
    """Locate a MariaDB tool, installing mariadb-server in-container on miss."""

    return discover_or_install(
        binary,
        apt_packages=("mariadb-server", "mariadb-client"),
        candidate_globs=candidate_globs,
        host_install_hint=(
            f"Could not locate {binary!r}. Install mariadb-server "
            "(Debian/Ubuntu: `apt install mariadb-server mariadb-client`)."
        ),
    )


def _wait_for_mysql(host: str, port: int, timeout: float) -> None:
    import mysql.connector

    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None

    while time.monotonic() < deadline:
        try:
            conn = mysql.connector.connect(
                host=host,
                port=port,
                user="root",
                password="",
                connection_timeout=2,
            )
            conn.close()

            return
        except mysql.connector.Error as exc:
            last_exc = exc
            time.sleep(0.2)

    raise RuntimeError(
        f"mariadb did not become ready on {host}:{port} within {timeout}s: {last_exc}",
    )
