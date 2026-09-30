"""Shared pytest fixtures and helpers spanning multiple test suites.

`postgres_cluster` is one cluster per run for every xdist worker, run by local `initdb` + `pg_ctl`
as the system `postgres` user - a postgresql-server install, no Docker.
"""

from __future__ import annotations

import fcntl
import json
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
from dataclasses import asdict, dataclass
from pathlib import Path

import psycopg
import pytest
from hypothesis import HealthCheck, settings
from hypothesis.configuration import set_hypothesis_home_dir
from hypothesis.database import DirectoryBasedExampleDatabase

from dbprint.cli import run_log
from tests import _containment, _substrates
from tests._provisioning import INSTALL_LOCK_PATH, discover_or_install, in_container


# `check` must not vary between runs, so its profile derandomizes; `local` searches wider and keeps
# what it finds. DBPRINT_HYPOTHESIS_PROFILE picks one; Hypothesis' own state lives under /tmp.
set_hypothesis_home_dir("/tmp/.hypothesis-dbprint")
settings.register_profile("check", derandomize=True, database=None, deadline=None)
# mutmut runs the suite several times in one process, so a test method meets a new `self` each run.
settings.register_profile(
    "mutate",
    parent=settings.get_profile("check"),
    suppress_health_check=[HealthCheck.differing_executors],
)
settings.register_profile(
    "local",
    max_examples=2000,
    deadline=None,
    database=DirectoryBasedExampleDatabase("/tmp/.hypothesis-dbprint/examples"),
)
settings.load_profile(os.environ.get("DBPRINT_HYPOTHESIS_PROFILE", "check"))

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

# A private root is one filesystem shared by every xdist worker, so the same escape is visible to
# all of them - one marker per claimed entry turns "every worker reports it" into "one does".
_CONTAINMENT_CLAIMS_DIR = Path("/tmp/dbprint--containment-claims")

_UNSAFE_CLAIM_CHARS_RE = re.compile(r"[^A-Za-z0-9_.-]")

# `-n auto` sized by cores alone stands up more live substrates than the host has memory for.
_MEMORY_CEILING_MB = 32 * 1024

# Estimate, not a measurement - the expensive worker is the one holding Spark or ClickHouse.
_WORKER_MEMORY_MB = 2 * 1024

_MEMORY_CEILING_ENV = "DBPRINT_TEST_MEMORY_MB"

# Every worker opens its own sessions to the one shared server, several per test at once.
_MAX_CONNECTIONS = 400

# Both servers are deleted at session end, so nothing they write needs to survive a crash.
_NO_DURABILITY = "-c fsync=off -c synchronous_commit=off -c full_page_writes=off"

_MEM_AVAILABLE_RE = re.compile(r"^MemAvailable:\s+(\d+) kB$", re.MULTILINE)

# Vendors a test can be parameterised on; the value names the substrate it reads.
_SUBSTRATE_VENDORS = frozenset(
    {
        "postgres",
        "mysql",
        "snowflake",
        "clickhouse",
        "redshift",
        "databricks",
        "bigquery",
    },
)

# Parameterised fixtures that build a connected adapter for the vendor they are given.
_VENDOR_FIXTURES = frozenset({"adapter_factory", "sql_adapter_factory"})

_SUBSTRATE_FIXTURES = {
    "postgres_cluster": "postgres",
    "mysql_cluster": "mysql",
    "databricks_spark_session": "databricks",
    "bigquery_emulator": "bigquery",
}

# xdist groups per substrate, one instance each: a Spark session lives in its worker's JVM, and the
# BigQuery emulator times out under concurrent workers and slows per dataset held (both measured).
_GROUPED_SUBSTRATES = {"databricks": 6, "bigquery": 8}

_EVERY_SUBSTRATE_MARK = "every_substrate"

# Vendors whose substrate is a server, a container or a JVM - what `just test-fast` never starts.
_SERVER_VENDORS = frozenset({"postgres", "mysql", "redshift", "databricks", "bigquery"})

_LIVE_SERVER_MARK = "live_server"

# Where a worker finds the process that owns the run's shared servers - xdist's controller, or
# the only process of a run without it.
_RUN_OWNER_ENV = "DBPRINT_TEST_RUN_OWNER"

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
    "--unshare-pid",
    "--die-with-parent",
)

_STOPPERS = {
    "postgres": lambda handle, _owner: _substrates.stop_postgres(handle, _run_as_postgres),
    "mariadb": _substrates.stop_mariadb,
    "bigquery": _substrates.stop_bigquery,
    "spark-warehouse": _substrates.stop_directory,
}


def pytest_configure(config: pytest.Config) -> None:
    """Pin the rendering environment the help assertions compare against.

    rich enables color when it detects CI, and `FORCE_COLOR` outranks `NO_COLOR`, so an ambient
    CI variable puts escape sequences into `--help`. The variables are removed rather than set
    falsy: rich reads their presence, not their value. `TERM` is left alone, since the progress
    renderer selects on a dumb terminal and pinning it would decide tests about that choice.
    """

    _reexec_under_sandbox()

    config.addinivalue_line(
        "markers",
        f"{_EVERY_SUBSTRATE_MARK}: the test reads every vendor's substrate in one run",
    )
    config.addinivalue_line("markers", f"{_LIVE_SERVER_MARK}: set by conftest; needs a server")

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


def pytest_sessionstart(session: pytest.Session) -> None:
    """Reclaim what an interrupted run left running, before any worker starts a substrate.

    Only the controller sweeps, and it runs ahead of xdist's own hook, which spawns the workers.
    """

    if hasattr(session.config, "workerinput"):
        return

    os.environ[_RUN_OWNER_ENV] = json.dumps(asdict(_substrates.current_owner()))
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    write = reporter.write_line if reporter is not None else print

    for line in _substrates.sweep(_scratch_root(), _STOPPERS):
        write(f"substrate sweep: {line}")


def pytest_xdist_auto_num_workers() -> int:
    """Size `-n auto` by the memory budget rather than by the core count."""

    cores = os.cpu_count() or 1

    return max(1, min(cores, _memory_budget_mb() // _WORKER_MEMORY_MB))


def _memory_budget_mb() -> int:
    """Return the smallest of the ceiling, this cgroup's limit, and what the host has free."""

    ceiling = int(os.environ.get(_MEMORY_CEILING_ENV) or _MEMORY_CEILING_MB)
    measured = (_cgroup_limit_mb(), _available_mb())

    return min([ceiling, *(value for value in measured if value is not None)])


def _cgroup_limit_mb() -> int | None:
    """Return this cgroup's v2 memory ceiling, or None where it is unset or unreadable."""

    try:
        raw = Path("/sys/fs/cgroup/memory.max").read_text(encoding="utf-8").strip()
    except OSError:
        return None

    if raw == "max":
        return None

    return int(raw) // (1024 * 1024)


def _available_mb() -> int | None:
    """Return MemAvailable - what the host can give up without swapping - or None."""

    try:
        meminfo = Path("/proc/meminfo").read_text(encoding="utf-8")
    except OSError:
        return None

    match = _MEM_AVAILABLE_RE.search(meminfo)

    return int(match.group(1)) // 1024 if match is not None else None


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark tests that need a live server; spread each `_GROUPED_SUBSTRATES` vendor over its groups.

    Runs first: xdist's own hook reads the group into the node id, and sees no marker added later.
    """

    placed = dict.fromkeys(_GROUPED_SUBSTRATES, 0)

    for item in items:
        substrates = _substrates_of(item)

        if substrates & _SERVER_VENDORS:
            item.add_marker(_LIVE_SERVER_MARK)

        vendor = next((vendor for vendor in _GROUPED_SUBSTRATES if vendor in substrates), None)

        if vendor is not None:
            group = placed[vendor] % _GROUPED_SUBSTRATES[vendor]
            item.add_marker(pytest.mark.xdist_group(f"{vendor}-{group}"))
            placed[vendor] += 1


def _substrates_of(item: pytest.Item) -> frozenset[str]:
    """Return every vendor whose substrate an item reads - by parameter, fixture or mark."""

    fixturenames = frozenset(getattr(item, "fixturenames", ()))

    if item.get_closest_marker(_EVERY_SUBSTRATE_MARK) or "all_sql_adapters" in fixturenames:
        return _SUBSTRATE_VENDORS

    # A vendor parameter reaches a substrate only through a fixture that resolves it; elsewhere
    # (a registry sweep, a credentials unit) it is just a name.
    callspec = getattr(item, "callspec", None)
    params = callspec.params.items() if callspec is not None else ()
    resolves = "request" in getattr(getattr(item, "_fixtureinfo", None), "argnames", ())
    named = {
        value
        for key, value in params
        if isinstance(value, str)
        and value in _SUBSTRATE_VENDORS
        and (resolves or key in _VENDOR_FIXTURES)
    }
    fixtures = {vendor for name, vendor in _SUBSTRATE_FIXTURES.items() if name in fixturenames}

    return frozenset(named | fixtures)


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Stop the run's shared servers and delete the lock files and containment claims it created.

    Controller only: xdist finishes it after every worker, so no peer still holds what this deletes.
    """

    if hasattr(session.config, "workerinput"):
        return

    _substrates.stop_owned(_scratch_root(), _STOPPERS, _run_owner())

    for path in (_CLUSTER_BOOTSTRAP_LOCK_PATH, INSTALL_LOCK_PATH):
        path.unlink(missing_ok=True)

    shutil.rmtree(_CONTAINMENT_CLAIMS_DIR, ignore_errors=True)


def _claim(key: str) -> bool:
    """Atomically claim a containment violation; True only for the first caller to claim it.

    A private root is one filesystem shared by every worker and test, so the same escape is
    visible to every later check - the claim is what makes it report exactly once.
    """

    _CONTAINMENT_CLAIMS_DIR.mkdir(parents=True, exist_ok=True)
    marker = _CONTAINMENT_CLAIMS_DIR / _UNSAFE_CLAIM_CHARS_RE.sub("_", key)

    try:
        marker.touch(exist_ok=False)
    except FileExistsError:
        return False

    return True


def _unclaimed_problems(before: dict[str, frozenset[str]]) -> list[str]:
    """Diff since `before` against the live filesystem, keeping only what nothing has claimed."""

    problems = []
    appeared = {
        root: [name for name in names if _claim(f"appeared::{root}::{name}")]
        for root, names in _containment.escaped(before, _containment.snapshot()).items()
    }
    appeared = {root: names for root, names in appeared.items() if names}
    strays = [entry for entry in _containment.suite_entries() if _claim(f"stray::{entry}")]

    if appeared:
        problems.append(f"new entries under a private root: {appeared}")

    if strays:
        problems.append(f"suite-named entries outside the scratch tree: {strays}")

    return problems


@pytest.fixture(scope="session", autouse=True)
def _contained_to_scratch_session() -> Iterator[None]:
    """Catch a write a per-test check cannot see: one made during a session fixture's setup,
    before the first test that triggers it has taken its snapshot.

    The fallback, not the primary attribution - a per-test write is claimed there first.
    """

    before = _containment.snapshot()

    yield

    problems = _unclaimed_problems(before)

    if problems:
        raise AssertionError("; ".join(problems))


@pytest.fixture(autouse=True)
def _contained_to_scratch_per_test() -> Iterator[None]:
    """Fail the one test that wrote outside the scratch tree, naming what it created.

    Snapshotting per test pins the writer: the failing test is whichever one's own window
    contains the write, not whichever ran last in a session shared by every check.
    """

    before = _containment.snapshot()

    yield

    problems = _unclaimed_problems(before)

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

    with _CLUSTER_BOOTSTRAP_LOCK_PATH.open("r+", encoding="utf-8") as lock_file:
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
def postgres_cluster() -> PostgresCluster:
    """The run's one ephemeral cluster, started by whichever process needs it first.

    Every test takes its own database in it, so workers share it; the run's owner stops it.
    """

    handle = _substrates.shared(_scratch_root(), "postgres", _run_owner(), _start_postgres)

    return PostgresCluster(port=int(handle["port"]))


def _start_postgres() -> dict[str, str]:
    bin_dir = _discover_postgres_bin_dir()
    root = _scratch_root()
    name = "dbprint-test-postgres-" + secrets.token_hex(4)
    data_dir = root / name
    port = _free_port()
    handle = {"path": str(data_dir), "bin_dir": str(bin_dir), "port": str(port)}
    marker = _substrates.register(root, name, "postgres", _run_owner(), **handle)

    # `initdb` refuses a directory it does not own, hence the mode-0700 create as the account the
    # server runs as.
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
                    f"-p {port} -h 127.0.0.1 -c max_connections={_MAX_CONNECTIONS} {_NO_DURABILITY}",
                    "-l",
                    str(data_dir / "postgres.log"),
                    "-w",
                    "start",
                ],
            )

            _wait_for_postgres("127.0.0.1", port, timeout=10.0)
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        said = _server_said(exc, data_dir / "postgres.log")
        message = _substrates.start_failure("postgres", said, root, port)
        _STOPPERS["postgres"](handle, _run_owner())
        marker.unlink(missing_ok=True)

        raise RuntimeError(message) from exc

    return handle


def _server_said(exc: BaseException, log: Path) -> str:
    captured = ""

    if isinstance(exc, subprocess.CalledProcessError):
        captured = "\n".join(part.strip() for part in (exc.stdout, exc.stderr) if part)

    return "\n".join(part for part in (str(exc), captured, _substrates.tail(log)) if part)


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
def mysql_cluster() -> MysqlCluster:
    """The run's one ephemeral MariaDB server, shared by every worker like `postgres_cluster`."""

    handle = _substrates.shared(_scratch_root(), "mariadb", _run_owner(), _start_mariadb)

    return MysqlCluster(port=int(handle["port"]))


def _start_mariadb() -> dict[str, str]:
    install_db = _discover_mysql_tool("mariadb-install-db")
    mariadbd = _discover_mysql_tool("mariadbd", candidate_globs=("/usr/sbin/mariadbd",))
    root = _scratch_root()
    name = "dbprint-test-mariadb-" + secrets.token_hex(4)
    data_dir = root / name
    port = _free_port()
    socket_path = data_dir / "mysqld.sock"
    handle = {"path": str(data_dir), "port": str(port)}
    marker = _substrates.register(root, name, "mariadb", _run_owner(), **handle)
    # Held so a readiness wait that expires before the pid file exists can still kill the server.
    server: subprocess.Popen[bytes] | None = None

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

            # Outlives the worker that starts it; the run's owner stops it through the pid file.
            server = subprocess.Popen(
                [
                    str(mariadbd),
                    "--no-defaults",
                    f"--datadir={data_dir}",
                    f"--tmpdir={data_dir}",
                    f"--socket={socket_path}",
                    f"--port={port}",
                    "--bind-address=127.0.0.1",
                    "--user=root",
                    f"--max-connections={_MAX_CONNECTIONS}",
                    "--innodb-flush-log-at-trx-commit=0",
                    "--innodb-doublewrite=0",
                    "--skip-log-bin",
                    f"--pid-file={data_dir / 'mariadb.pid'}",
                    f"--log-error={data_dir / 'mariadb.err'}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            _wait_for_mysql("127.0.0.1", port, timeout=30.0)
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        said = _server_said(exc, data_dir / "mariadb.err")
        message = _substrates.start_failure("mariadb", said, root, port)

        if server is not None:
            server.kill()

        _STOPPERS["mariadb"](handle, _run_owner())
        marker.unlink(missing_ok=True)

        raise RuntimeError(message) from exc

    return handle


def _run_owner() -> _substrates.Owner:
    """Return the process that owns this run's shared servers, as `pytest_sessionstart` published it."""

    return _substrates.Owner(**json.loads(os.environ[_RUN_OWNER_ENV]))


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
