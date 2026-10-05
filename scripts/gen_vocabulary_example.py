"""Regenerate the v1 vocabulary example from the producer, against a throwaway Postgres.

A small second print alongside `docs/format/v1/examples/production/`, covering the
`looks_like` values the seed-bank domain has no honest column for. Run via
`just example-vocabulary`; golden-tested by tests/conformance/test_reference_example.py.
"""

from __future__ import annotations

import os
import runpy
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_ROOT = REPO_ROOT / "docs/format/v1/examples/vocabulary"
PRINT_ROOT = EXAMPLE_ROOT / "prints/vocabulary"
SQL_DIR = Path(__file__).resolve().parent / "sql"

DATABASE = "vocabulary"
CONNECTION = "vocabulary"


SCHEMA_SQL = (SQL_DIR / "vocabulary_schema.sql").read_text()
SEED_SQL = (SQL_DIR / "vocabulary_seed.sql").read_text()


# `scripts/` is not a package, so the shared helpers load by path.
support = SimpleNamespace(**runpy.run_path(str(Path(__file__).with_name("example_support.py"))))


def build_example(credentials: dict[str, str], target: Path) -> None:
    """Generate the whole example tree into `target` from a database seeded by `scripts/sql/`.

    Creates the schema in the caller's own database, so it must be one the caller can lose.
    """

    from dbprint.adapters.postgres import PostgresAdapter
    from dbprint.config import load_project
    from dbprint.engine import Engine, GenerateRequest

    support.apply_sql(credentials, SCHEMA_SQL)
    support.apply_sql(credentials, SEED_SQL)

    if target.exists():
        shutil.rmtree(target)

    target.mkdir(parents=True)
    shutil.copytree(EXAMPLE_ROOT, target, dirs_exist_ok=True, ignore=_ignore_prints)

    project = load_project(target)
    conn_config = project.connections[CONNECTION]

    result = Engine(PostgresAdapter(credentials), conn_config, target).generate(
        GenerateRequest(force=True),
    )
    support.require_complete(result, EXPECTED_OBJECTS)

    support.normalize_timestamps(target / "prints" / CONNECTION)


EXPECTED_OBJECTS = frozenset({"vocabulary.public.shapes"})


def _ignore_prints(directory: str, names: list[str]) -> set[str]:
    return {"prints"} if Path(directory) == EXAMPLE_ROOT else set()


def _main() -> int:
    """Provision a throwaway cluster, regenerate the committed example, report."""

    return support.regenerate(
        "vocabulary",
        DATABASE,
        build_example,
        EXAMPLE_ROOT.parent / "_vocabulary_regenerated",
        CONNECTION,
        PRINT_ROOT,
    )


if __name__ == "__main__":
    os.environ.setdefault("PGPASSWORD", "postgres")
    sys.exit(_main())
