"""The adversarial print: one committed fixture carrying every consumer-visible state that
has produced a rendering defect.

Generated once per pytest session and re-validated by the conformance validator on every
build, so the one hand-patched field cannot itself ship malformed. See
tests/consumer/register.py for the claim each state carries and which surface satisfies it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from dbprint.adapters import (
    ColumnMeta,
    ColumnStats,
    CommentsMeta,
    ForeignKeyMeta,
    Frequencies,
    Inferred,
    Length,
    MockAdapter,
    MockTable,
    Range,
    ValueCount,
)


# UUIDs render at a fixed width, so every uuid-typed column's length summary is a constant.
_UUID_LENGTH = Length(min=36, max=36, avg=36.0, p95=36.0)
from dbprint.cli.main import main
from dbprint.config import ConnectionConfig
from dbprint.conformance import validate_print


CONN_NAME = "primary"

# One source of truth for every value a claim's check compares the rendered output
# against - never re-derived from the artifact the check itself reads.
SCOPED_TABLE = "public.sowing_trial"
SCOPED_ROW_COUNT = 1000
SCOPED_ROWS_SCANNED = 250
REDACTED_COLUMN = "email"
REDACTED_PRIMITIVE = "mask"
FUTURE_DATED_COLUMN = "matures_at"
FUTURE_DATED_RANGE_MAX = "2099-01-01T00:00:00"
TRUNCATED_FK_COLUMN = "cultivar_id"
TRUNCATED_FK_COVERAGE = 0.4
FK_TARGET_TABLE = "public.cultivar"
UNEVALUATED_TABLE = "public.active_curators"
EMPTY_COLUMNS_TABLE = "public.empty_scan"
APPROXIMATE_ROW_COUNT_TABLE = "public.batch"
APPROXIMATE_ROW_COUNT = 40_000
INCOMPLETE_GRAIN_TABLE = "public.wide_lookup"
DECLARED_MISSING_TABLE = "public.dropped_statistics"
DECLARED_MISSING_KIND = "statistics"
# No fixture table declares one - the manifest never lists it, so it stays absent everywhere.
NEVER_DECLARED_KIND = "description"

_CREDENTIAL_ENV = {
    "DBPRINT_PRIMARY_HOST": "h",
    "DBPRINT_PRIMARY_PORT": "5432",
    "DBPRINT_PRIMARY_DATABASE": "d",
    "DBPRINT_PRIMARY_USER": "u",
    "DBPRINT_PRIMARY_PASSWORD": "p",
}

PROJECT_YAML = """\
connections:
  primary:
    adapter: postgres
    auto: true
    output: prints
    redact:
      - columns: ["*.email"]
        with: mask
    rules:
      - include: ["public.sowing_trial"]
        sample: 0.25
      - include: ["public.empty_scan"]
        filter: "rank = 'never-matches'"
"""


def _fixture_tables() -> dict[str, MockTable]:
    sowing_trial = MockTable(
        type="table",
        namespace_path=("public", "sowing_trial"),
        ddl=(
            "CREATE TABLE public.sowing_trial (id uuid PRIMARY KEY, cultivar_id uuid, "
            "email text, matures_at timestamp with time zone);\n"
        ),
        columns=[
            ColumnMeta(name="id", sql_type="uuid", nullable=False, default=None, ordinal=1),
            ColumnMeta(
                name="cultivar_id",
                sql_type="uuid",
                nullable=False,
                default=None,
                ordinal=2,
            ),
            ColumnMeta(name="email", sql_type="text", nullable=False, default=None, ordinal=3),
            ColumnMeta(
                name="matures_at",
                sql_type="timestamp with time zone",
                nullable=False,
                default=None,
                ordinal=4,
            ),
        ],
        relationships=[
            ForeignKeyMeta(
                column=("cultivar_id",),
                target_table=FK_TARGET_TABLE,
                target_column=("id",),
                on_delete="NO ACTION",
                on_update="NO ACTION",
                constraint_name="sowing_trial_cultivar_fk",
            ),
        ],
        indexes=[],
        comments=CommentsMeta(table=None, columns={}),
        stats={
            "id": ColumnStats(
                sql_type="uuid",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=SCOPED_ROWS_SCANNED,
                cardinality_ratio=1.0,
                cardinality_method="exact",
                values=tuple(
                    ValueCount(value=f"00000000-0000-7000-8000-{i:012d}", count=1) for i in range(5)
                ),
                values_coverage=0.02,
                distribution="uniform",
                empty_count=0,
                length=_UUID_LENGTH,
                inferred=Inferred(candidate_key=True),
            ),
            "cultivar_id": ColumnStats(
                sql_type="uuid",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=60,
                cardinality_ratio=0.24,
                cardinality_method="exact",
                values=tuple(ValueCount(value=f"rank-{i:02d}", count=1) for i in range(30)),
                values_coverage=TRUNCATED_FK_COVERAGE,
                distribution="uniform",
                empty_count=0,
                length=Length(min=7, max=7, avg=7.0, p95=7.0),
            ),
            "email": ColumnStats(
                sql_type="text",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=1,
                cardinality_ratio=0.004,
                cardinality_method="exact",
                values=(ValueCount(value="a@example.com", count=SCOPED_ROWS_SCANNED),),
                values_coverage=1.0,
                distribution="dominant_value",
                empty_count=0,
                length=Length(min=14, max=14, avg=14.0, p95=14.0),
            ),
            "matures_at": ColumnStats(
                sql_type="timestamp with time zone",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=SCOPED_ROWS_SCANNED,
                cardinality_ratio=1.0,
                cardinality_method="exact",
                # A non-exhaustive top-N slice (SPEC 2.2.3) - `values_coverage` stays absent, so
                # nothing claims the list is complete.
                values=tuple(ValueCount(value=f"202{i}-01-01", count=1) for i in range(4, 9)),
                distribution="uniform",
                frequencies=Frequencies(
                    top=1,
                    bottom=1,
                    listed=SCOPED_ROWS_SCANNED,
                    total=SCOPED_ROWS_SCANNED,
                ),
                range=Range(min="2024-01-01", max=FUTURE_DATED_RANGE_MAX, span_days=27394),
                percentiles={"p50": "2050-01-01"},
                quantized_count=0,
            ),
        },
        samples={"id": [f"00000000-0000-7000-8000-{i:012d}" for i in range(20)]},
        row_count=SCOPED_ROW_COUNT,
        rows_scanned=SCOPED_ROWS_SCANNED,
    )

    cultivar = MockTable(
        type="table",
        namespace_path=("public", "cultivar"),
        ddl="CREATE TABLE public.cultivar (id uuid PRIMARY KEY);\n",
        columns=[ColumnMeta(name="id", sql_type="uuid", nullable=False, default=None, ordinal=1)],
        relationships=[],
        indexes=[],
        comments=CommentsMeta(table=None, columns={}),
        stats={
            "id": ColumnStats(
                sql_type="uuid",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=5,
                cardinality_ratio=1.0,
                cardinality_method="exact",
                values=tuple(
                    ValueCount(value=f"00000000-0000-7000-8000-{i:012d}", count=1) for i in range(5)
                ),
                values_coverage=1.0,
                distribution="uniform",
                empty_count=0,
                length=_UUID_LENGTH,
                inferred=Inferred(candidate_key=True),
            ),
        },
        samples={"id": [f"00000000-0000-7000-8000-{i:012d}" for i in range(5)]},
        row_count=5,
    )

    batch = MockTable(
        type="table",
        namespace_path=("public", "batch"),
        ddl="CREATE TABLE public.batch (id uuid PRIMARY KEY);\n",
        columns=[ColumnMeta(name="id", sql_type="uuid", nullable=False, default=None, ordinal=1)],
        relationships=[],
        indexes=[],
        comments=CommentsMeta(table=None, columns={}),
        stats={
            "id": ColumnStats(
                sql_type="uuid",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=40000,
                cardinality_ratio=1.0,
                cardinality_method="exact",
                values=tuple(
                    ValueCount(value=f"00000000-0000-7000-8000-{i:012d}", count=1) for i in range(5)
                ),
                values_coverage=0.000125,
                distribution="uniform",
                empty_count=0,
                length=_UUID_LENGTH,
                inferred=Inferred(candidate_key=True),
            ),
        },
        samples={"id": [f"00000000-0000-7000-8000-{i:012d}" for i in range(5)]},
        row_count=APPROXIMATE_ROW_COUNT,
        row_count_method="approximate",
    )

    active_curators = MockTable(
        type="view",
        namespace_path=("public", "active_curators"),
        ddl="CREATE VIEW public.active_curators AS SELECT id FROM public.sowing_trial;\n",
        columns=[ColumnMeta(name="id", sql_type="uuid", nullable=False, default=None, ordinal=1)],
        relationships=[],
        indexes=[],
        comments=CommentsMeta(table=None, columns={}),
        stats={},
        samples={},
    )

    empty_scan = MockTable(
        type="table",
        namespace_path=("public", "empty_scan"),
        ddl="CREATE TABLE public.empty_scan (rank text);\n",
        columns=[
            ColumnMeta(name="rank", sql_type="text", nullable=True, default=None, ordinal=1),
        ],
        relationships=[],
        indexes=[],
        comments=CommentsMeta(table=None, columns={}),
        stats={},
        samples={},
        row_count=500,
        rows_scanned=0,
    )

    wide_lookup = MockTable(
        type="table",
        namespace_path=("public", "wide_lookup"),
        ddl="CREATE TABLE public.wide_lookup (a text, b text, c text);\n",
        columns=[
            ColumnMeta(name="a", sql_type="text", nullable=False, default=None, ordinal=1),
            ColumnMeta(name="b", sql_type="text", nullable=False, default=None, ordinal=2),
            ColumnMeta(name="c", sql_type="text", nullable=False, default=None, ordinal=3),
        ],
        relationships=[],
        indexes=[],
        comments=CommentsMeta(table=None, columns={}),
        stats={
            name: ColumnStats(
                sql_type="text",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=1,
                cardinality_ratio=0.01,
                cardinality_method="exact",
                values=(ValueCount(value="x", count=100),),
                values_coverage=1.0,
                distribution="dominant_value",
                empty_count=0,
                length=Length(min=1, max=1, avg=1.0, p95=1.0),
            )
            for name in ("a", "b", "c")
        },
        samples={},
        row_count=100,
    )

    dropped_statistics = MockTable(
        type="table",
        namespace_path=("public", "dropped_statistics"),
        ddl="CREATE TABLE public.dropped_statistics (id uuid PRIMARY KEY);\n",
        columns=[
            ColumnMeta(name="id", sql_type="uuid", nullable=False, default=None, ordinal=1),
        ],
        relationships=[],
        indexes=[],
        comments=CommentsMeta(table=None, columns={}),
        stats={
            "id": ColumnStats(
                sql_type="uuid",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=3,
                cardinality_ratio=1.0,
                cardinality_method="exact",
                values=tuple(
                    ValueCount(value=f"00000000-0000-7000-8000-{i:012d}", count=1) for i in range(3)
                ),
                values_coverage=1.0,
                distribution="uniform",
                empty_count=0,
                length=_UUID_LENGTH,
                inferred=Inferred(candidate_key=True),
            ),
        },
        samples={"id": [f"00000000-0000-7000-8000-{i:012d}" for i in range(3)]},
        row_count=3,
    )

    return {
        "public.sowing_trial": sowing_trial,
        "public.cultivar": cultivar,
        "public.batch": batch,
        "public.active_curators": active_curators,
        "public.empty_scan": empty_scan,
        "public.wide_lookup": wide_lookup,
        "public.dropped_statistics": dropped_statistics,
    }


class _MockPostgresAdapter(MockAdapter):
    """MockAdapter with REQUIRED_KEYS to satisfy the CLI's credential-resolution path."""

    REQUIRED_KEYS = ("host", "port", "database", "user", "password")

    def __init__(self, _credentials: dict[str, str]) -> None:
        super().__init__(_fixture_tables())


@dataclass(frozen=True)
class AdversarialPrint:
    """The generated print's connection + root, for a consumer surface to render against."""

    conn: ConnectionConfig
    print_root: Path


def _inject_incomplete_grain_search(print_root: Path) -> None:
    """`grain.search.exhausted: false` (SPEC 2.2.12) - a state the mock adapter never produces.

    Hand-patched after generation; the build re-runs the conformance validator, so it cannot
    ship malformed.
    """

    path = print_root / "public" / "wide_lookup" / "statistics.yaml"
    statistics = yaml.safe_load(path.read_text())
    statistics["grain"] = {"keys": [], "search": {"exhausted": False}}
    path.write_text(yaml.safe_dump(statistics))


def _drop_declared_statistics(print_root: Path) -> None:
    """Delete a declared `statistics.yaml` (SPEC 2.5) - a manifest promise disk no longer keeps.

    `manifest.missing-artifact` forbids this at ERROR, so it is injected after `build()`'s own
    conformance gate, never before.
    """

    path = print_root / "public" / "dropped_statistics" / "statistics.yaml"
    path.unlink()


def build(project_dir: Path) -> AdversarialPrint:
    """Generate the fixture under `project_dir`; validated by the conformance checker.

    Generates twice against the same, unchanged mock data. The first run has no baseline, so
    every table is `table_added` (EXIT_DRIFT). The second finds every table fresh, so every
    one - including the scoped table - lands in `diff.yaml` as `unevaluated_tables` rather
    than `unchanged_tables` (SPEC 2.6.4/2.6.8): a real flow, not a hand patch.
    """

    (project_dir / ".dbprint.yaml").write_text(PROJECT_YAML)
    runner = CliRunner(env=_CREDENTIAL_ENV)
    old_cwd = Path.cwd()
    os.chdir(project_dir)

    try:
        with patch.dict(
            "dbprint.cli.adapter_registry.ADAPTERS",
            {"postgres": _MockPostgresAdapter},
            clear=True,
        ):
            first = runner.invoke(main, ["generate", "--no-tui"])
            assert first.exit_code == 3, first.output

            second = runner.invoke(main, ["generate", "--no-tui"])
            assert second.exit_code == 0, second.output
    finally:
        os.chdir(old_cwd)

    print_root = project_dir / "prints" / CONN_NAME
    _inject_incomplete_grain_search(print_root)

    issues = validate_print(print_root)
    errors = [i for i in issues if i.severity == "error"]
    assert not errors, f"adversarial fixture is not conformant: {errors}"

    _drop_declared_statistics(print_root)

    conn = ConnectionConfig(
        name=CONN_NAME,
        adapter="postgres",
        auto=True,
        output=project_dir / "prints",
    )

    return AdversarialPrint(conn=conn, print_root=print_root)


@pytest.fixture(scope="session")
def adversarial_print(tmp_path_factory: pytest.TempPathFactory) -> AdversarialPrint:
    """The shared adversarial print, built once for the whole test session."""

    return build(tmp_path_factory.mktemp("adversarial"))
