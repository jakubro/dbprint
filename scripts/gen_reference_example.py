"""Regenerate the v1 reference example from the producer, against a real Postgres.

Everything under docs/format/v1/examples/ is what the code and database actually said, not
hand-authored: timestamps are frozen post-run and user-authored files are seeded before it.
Run via `just example`; golden-tested by tests/conformance/test_reference_example.py.
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

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_ROOT = REPO_ROOT / "docs/format/v1/examples/production"
PRINT_ROOT = EXAMPLE_ROOT / "prints/production"
SQL_DIR = Path(__file__).resolve().parent / "sql"

DATABASE = "analytics"
CONNECTION = "production"

# Every stamped timestamp collapses here, so regeneration diffs nothing but clocks.
FROZEN_TIMESTAMP = "2026-05-17T22:48:01Z"
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")

# Run instants only; `baseline.generated_at` matches for free, one level deeper.
_INSTANT_KEYS = frozenset({"generated_at", "profiled_at", "scanned_at"})

# Fixed rather than drawn from the environment so a committed digest is reproducible; safe
# to publish because every digested value is synthetic, derived from a row ordinal.
FIXTURE_REDACTION_SALT = "seedbank-reference-example-fixture-salt-do-not-reuse"

# `seedbank` carries the format's feature coverage (SPEC 2.2); `fixture` the shapes it cannot.
SCHEMA_SQL = (SQL_DIR / "schema.sql").read_text()

# Deterministic: every value derives from the row's ordinal, so regeneration reproduces the
# same statistics, and table sizes place each column on the SPEC threshold side the coverage
# map wants. UUIDs are hand-assembled so the nibbles `_UUID_RE` requires reproduce exactly;
# `collector_id` is the same expression wherever a foreign key resolves to a `collector` row.
SEED_SQL = (SQL_DIR / "seed.sql").read_text()

# Applied between the two runs so diff.yaml's drift is real. The comment change is there
# because SPEC's diff kinds exclude comments, an omission examples/README.md documents.
DRIFT_SQL = (SQL_DIR / "drift.sql").read_text()


def build_example(credentials: dict[str, str], target: Path) -> None:
    """Generate the whole example tree into `target` from a live database.

    Creates the schema in the caller's database, so it must be one they can afford to lose.
    """

    from dbprint.adapters.postgres import PostgresAdapter
    from dbprint.config import load_project
    from dbprint.config.project import bind_redaction_salt
    from dbprint.engine import Engine, GenerateRequest

    _apply_sql(credentials, SCHEMA_SQL)
    _apply_sql(credentials, SEED_SQL)

    if target.exists():
        shutil.rmtree(target)

    target.mkdir(parents=True)
    shutil.copytree(EXAMPLE_ROOT, target, dirs_exist_ok=True, ignore=_ignore_prints)

    project = load_project(target)
    conn_config = bind_redaction_salt(project.connections[CONNECTION], FIXTURE_REDACTION_SALT)
    _seed_description(target / "prints" / CONNECTION)
    _seed_statistics_annotations(target / "prints" / CONNECTION)
    _seed_relationships_annotations(target / "prints" / CONNECTION)
    _seed_manifest_annotations(target / "prints" / CONNECTION)

    # First run establishes the baseline; the second, after the drift, writes diff.yaml.
    first = Engine(PostgresAdapter(credentials), conn_config, target).generate(
        GenerateRequest(force=True),
    )
    _require_complete(first)

    _apply_sql(credentials, DRIFT_SQL)
    second = Engine(PostgresAdapter(credentials), conn_config, target).generate(
        GenerateRequest(force=True),
    )
    _require_complete(second)

    _normalize_timestamps(target / "prints" / CONNECTION)
    _recompute_freshness(target / "prints" / CONNECTION, FROZEN_TIMESTAMP)


EXPECTED_OBJECTS = frozenset(
    {
        "seedbank.taxon",
        "seedbank.collector",
        "seedbank.vault",
        "seedbank.accession",
        "seedbank.germination_trial",
        "seedbank.specimen_image",
        "seedbank.storage_reading",
        "seedbank.accession_summary",
        "seedbank.germination_by_taxon_mv",
        "fixture.shape_probe",
    },
)


def _require_complete(result: Any) -> None:
    """Refuse a run that did not profile every object the example illustrates.

    A failed table is dropped from the manifest, so an incomplete run still looks consistent.
    """

    failed = [t for t in result.tables if t.status == "failed"]

    if failed:
        detail = "\n".join(f"  {t.fqn}: {t.error} (at {t.error_operation})" for t in failed)

        raise SystemExit(f"generate failed on {len(failed)} object(s):\n{detail}")

    profiled = {t.fqn for t in result.tables}
    missing = EXPECTED_OBJECTS - profiled

    if missing:
        raise SystemExit(
            f"generate never reached {sorted(missing)}; it saw {sorted(profiled)}",
        )


def _ignore_prints(directory: str, names: list[str]) -> set[str]:
    return {"prints"} if Path(directory) == EXAMPLE_ROOT else set()


def _normalize_timestamps(print_root: Path) -> None:
    """Freeze only the run instants, matched by YAML key rather than by pattern.

    A temporal column's own `range`/`percentiles` values are ISO instants too, so only
    lines keyed in `_INSTANT_KEYS` are rewritten - and only in `.yaml`, never a `.sql` literal.
    """

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


_FRESHNESS_BLOCK_RE = re.compile(
    r"(freshness:\n[ ]+max_age_days: )(-?\d+)(\n[ ]+classification: )(live|stale|dormant)",
)


def _recompute_freshness(print_root: Path, profiled_at: str) -> None:
    """Make the committed `freshness` block a pure function of `range.max` and `profiled_at`.

    The engine derives `max_age_days` against the real run instant, which `_normalize_timestamps`
    then freezes; recomputing against that constant keeps the committed file consistent with
    SPEC 2.2.9's inversion identity. Redacted columns are exempt: already coarsened by the run.
    """

    from dbprint.spec.temporal_age import freshness_classification
    from dbprint.spec.temporal_age import max_age_days as compute_max_age_days

    for path in sorted(print_root.rglob("statistics.yaml")):
        text = path.read_text()
        data = yaml.safe_load(text)
        columns = data.get("columns", {}) if isinstance(data, dict) else {}
        corrections: list[tuple[int, str] | None] = []

        for column in columns.values():
            if not isinstance(column, dict) or not isinstance(column.get("freshness"), dict):
                continue

            if column.get("redacted") is not None:
                corrections.append(None)

                continue

            range_block = column.get("range")
            range_max = range_block.get("max") if isinstance(range_block, dict) else None
            age = compute_max_age_days(range_max, profiled_at)
            corrections.append((age, freshness_classification(age)))

        if not corrections:
            continue

        remaining = iter(corrections)

        def _replace(match: re.Match[str]) -> str:
            correction = next(remaining, None)  # noqa: B023 - consumed by .sub() this iteration

            if correction is None:
                return match.group(0)

            age, classification = correction

            return f"{match.group(1)}{age}{match.group(3)}{classification}"

        new_text = _FRESHNESS_BLOCK_RE.sub(_replace, text)

        if new_text != text:
            path.write_text(new_text)


def _seed_description(print_root: Path) -> None:
    """Place every committed `description.md` into the target tree, before any run reads one."""

    _seed_user_content(print_root, "description.md")


def _seed_statistics_annotations(print_root: Path) -> None:
    """Place every committed `statistics.annotations.yaml` into the tree before a run reads one."""

    _seed_user_content(print_root, "statistics.annotations.yaml")


def _seed_relationships_annotations(print_root: Path) -> None:
    """Place every committed `relationships.annotations.yaml` into the tree before a run."""

    _seed_user_content(print_root, "relationships.annotations.yaml")


def _seed_manifest_annotations(print_root: Path) -> None:
    """Place the committed `manifest.annotations.yaml` into the tree before a run reads it."""

    _seed_user_content(print_root, "manifest.annotations.yaml")


def _seed_user_content(print_root: Path, filename: str) -> None:
    """Copy every committed `filename` from PRINT_ROOT into `print_root`, path preserved.

    The engine never writes these, so regeneration would otherwise drop them.
    """

    for source in sorted(PRINT_ROOT.rglob(filename)):
        destination = print_root / source.relative_to(PRINT_ROOT)

        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)


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
    data_dir = Path("/var/lib/postgresql/dbprint-example-" + secrets.token_hex(4))
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
        build_example(credentials, EXAMPLE_ROOT.parent / "_regenerated")
        _swap_in(EXAMPLE_ROOT.parent / "_regenerated")
    finally:
        _stop_cluster(bin_dir, data_dir)

    print(f"reference example regenerated at {PRINT_ROOT}")

    return 0


def _swap_in(regenerated: Path) -> None:
    shutil.rmtree(PRINT_ROOT)
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
