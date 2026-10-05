"""Regenerate the v1 reference example from the producer, against a throwaway Postgres.

Everything under docs/format/v1/examples/ is what the code and database actually said, not
hand-authored: timestamps are frozen post-run and user-authored files are seeded before it.
Run via `just example`; golden-tested by tests/conformance/test_reference_example.py.
"""

from __future__ import annotations

import os
import re
import runpy
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_ROOT = REPO_ROOT / "docs/format/v1/examples/production"
PRINT_ROOT = EXAMPLE_ROOT / "prints/production"
SQL_DIR = Path(__file__).resolve().parent / "sql"

DATABASE = "arboretum"
CONNECTION = "production"


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


# `scripts/` is not a package, so the shared helpers load by path.
support = SimpleNamespace(**runpy.run_path(str(Path(__file__).with_name("example_support.py"))))


def build_example(credentials: dict[str, str], target: Path) -> None:
    """Generate the whole example tree into `target` from a database seeded by `scripts/sql/`.

    Creates the schema in the caller's database, so it must be one they can afford to lose.
    """

    from dbprint.adapters.postgres import PostgresAdapter
    from dbprint.config import load_project
    from dbprint.config.project import bind_redaction_salt
    from dbprint.engine import Engine, GenerateRequest

    support.apply_sql(credentials, SCHEMA_SQL)
    support.apply_sql(credentials, SEED_SQL)

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
    support.require_complete(first, EXPECTED_OBJECTS)

    support.apply_sql(credentials, DRIFT_SQL)
    second = Engine(PostgresAdapter(credentials), conn_config, target).generate(
        GenerateRequest(force=True),
    )
    support.require_complete(second, EXPECTED_OBJECTS)

    support.normalize_timestamps(target / "prints" / CONNECTION)
    _recompute_freshness(target / "prints" / CONNECTION, support.FROZEN_TIMESTAMP)


EXPECTED_OBJECTS = frozenset(
    {
        "arboretum.seedbank.taxon",
        "arboretum.seedbank.collector",
        "arboretum.seedbank.vault",
        "arboretum.seedbank.accession",
        "arboretum.seedbank.germination_trial",
        "arboretum.seedbank.specimen_image",
        "arboretum.seedbank.storage_reading",
        "arboretum.seedbank.accession_summary",
        "arboretum.seedbank.germination_by_taxon_mv",
        "arboretum.fixture.shape_probe",
    },
)


def _ignore_prints(directory: str, names: list[str]) -> set[str]:
    return {"prints"} if Path(directory) == EXAMPLE_ROOT else set()


_FRESHNESS_BLOCK_RE = re.compile(
    r"(freshness:\n[ ]+max_age_days: )(-?\d+)(\n[ ]+classification: )(live|stale|dormant)",
)


def _recompute_freshness(print_root: Path, profiled_at: str) -> None:
    """Make the committed `freshness` block a pure function of `range.max` and `profiled_at`.

    The engine derives `max_age_days` against the real run instant, which
    `support.normalize_timestamps` then freezes; recomputing against that constant keeps the
    committed file consistent with SPEC 2.2.9's inversion identity. Redacted columns are exempt,
    already coarsened by the run.
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


def _main() -> int:
    """Provision a throwaway cluster, regenerate the committed example, report."""

    support.require_extensions()

    return support.regenerate(
        "reference",
        DATABASE,
        build_example,
        EXAMPLE_ROOT.parent / "_regenerated",
        CONNECTION,
        PRINT_ROOT,
    )


if __name__ == "__main__":
    os.environ.setdefault("PGPASSWORD", "postgres")
    sys.exit(_main())
