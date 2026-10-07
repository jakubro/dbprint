"""The adversarial print: one committed fixture carrying every consumer-visible state that
has produced a rendering defect.

Generated once per pytest session and re-validated by the conformance validator on every
build, so the one hand-patched field cannot itself ship malformed. See
tests/consumer/register.py for the claim each state carries and which surface satisfies it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from dbprint.adapters import (
    ColumnStats,
    ForeignKeyMeta,
    Frequencies,
    Inferred,
    Length,
    MockAdapter,
    MockTable,
    NullPattern,
    NullPatterns,
    Range,
    ValueCount,
)


# UUIDs render at a fixed width, so every uuid-typed column's length summary is a constant.
_UUID_LENGTH = Length(min=36, max=36, avg=36.0, p95=36.0)
from dbprint.cli.main import main
from dbprint.config import ConnectionConfig
from dbprint.spec.artifacts import MANIFEST_FILENAME
from tests._cli import credential_env, patch_registry
from tests._engine_run import conformance_errors
from tests._prints import columns, mock_table


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
SCOPED_KEY_COLUMN = "id"
SCOPED_COMPLETE_LIST_COLUMN = "stage"
SCOPED_LATEST_COLUMN = "updated_at"
# Its P1/P99 sit inside its min/max, so labelling them the range would hide both tails.
PERCENTILE_INSIDE_RANGE_COLUMN = "updated_at"
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

DELIMITER_TABLE = "public.curation_event"
DELIMITER_COLUMN = "condition"
DELIMITER_VALUE = "fair|poor"
LINE_BREAK_VALUE = "sound\nbut small"
# Carries the Markdown grammar's own fact, list and label separators, and a quote to double.
GRAMMAR_VALUE = "sub; species, wild: collector's"
SPELLING_COLUMN = "remark"
ORPHAN_SPELLING_TABLE = "public.cultivar"
ORPHAN_SPELLING_COLUMN = "id"
ORPHAN_SPELLING_VALUE = "orphan-spelling"
GRAIN_NO_OUTCOME_TABLE = "public.batch"
EXTREME_TABLE = "public.gauge"
# Declared, inferred-and-rejected and measured edges, listed worst-first on disk.
SEVERAL_EDGES_TABLE = "public.gauge"
SEVERAL_EDGES_COLUMN = "sparse"
REJECTED_EDGE_TARGET = "public.batch"
UNREADABLE_PROFILED_TABLE = "public.wide_lookup"
UNREADABLE_PROFILED_AT = "not-a-date"
EXTREME_ROW_COUNT = 10_000
# Every float statistic of `wide`/`tiny` sits where `str(float)` switches to exponent form.
EXTREME_WIDE_P50 = 18446744073709548000.0
EXTREME_TINY_MEAN = 0.00000005
EXTREME_NULL_RATE = 0.9996
EXPONENT_FORM = re.compile(r"(?<![\w.])\d+(?:\.\d+)?[eE][+-]?\d+(?![\w.])")
PARTIAL_AS_WHOLE = re.compile(r"(?<![\d.])(?:0|100)(?:\.0)?%")
# Each reads as something else when printed raw: nothing, a genuine null, a fold.
SPELLING_VALUES = (
    "",
    "NULL",
    "seed coat intact and no visible damage under magnification after the second germination trial",
)

_CREDENTIAL_ENV = credential_env()

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
    sowing_trial = mock_table(
        "public.sowing_trial",
        columns(
            ("id", "uuid"),
            ("cultivar_id", "uuid"),
            ("email", "text"),
            ("matures_at", "timestamp with time zone"),
            ("stage", "text"),
            ("updated_at", "timestamp with time zone"),
        ),
        {
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
            "stage": ColumnStats(
                sql_type="text",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=2,
                cardinality_ratio=0.008,
                cardinality_method="exact",
                values=(
                    ValueCount(value="sown", count=150),
                    ValueCount(value="harvested", count=100),
                ),
                values_coverage=1.0,
                distribution="imbalanced",
                empty_count=0,
                length=Length(min=4, max=9, avg=6.0, p95=9.0),
            ),
            "updated_at": ColumnStats(
                sql_type="timestamp with time zone",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=SCOPED_ROWS_SCANNED,
                cardinality_ratio=1.0,
                cardinality_method="exact",
                values=tuple(ValueCount(value=f"201{i}-03-01", count=1) for i in range(5)),
                distribution="uniform",
                frequencies=Frequencies(top=1, bottom=1, listed=5, total=SCOPED_ROWS_SCANNED),
                range=Range(min="2010-03-01", max="2014-03-01", span_days=1461),
                percentiles={"p01": "2010-04-01", "p50": "2012-03-01", "p99": "2014-02-01"},
                quantized_count=SCOPED_ROWS_SCANNED,
            ),
        },
        ddl="CREATE TABLE public.sowing_trial (id uuid PRIMARY KEY, cultivar_id uuid, "
        "email text, matures_at timestamp with time zone, stage text, "
        "updated_at timestamp with time zone);\n",
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
        samples={"id": [f"00000000-0000-7000-8000-{i:012d}" for i in range(20)]},
        row_count=SCOPED_ROW_COUNT,
        rows_scanned=SCOPED_ROWS_SCANNED,
    )

    cultivar = mock_table(
        "public.cultivar",
        columns(("id", "uuid")),
        {
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
        ddl="CREATE TABLE public.cultivar (id uuid PRIMARY KEY);\n",
        samples={"id": [f"00000000-0000-7000-8000-{i:012d}" for i in range(5)]},
        row_count=5,
    )

    batch = mock_table(
        "public.batch",
        columns(("id", "uuid")),
        {
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
        ddl="CREATE TABLE public.batch (id uuid PRIMARY KEY);\n",
        samples={"id": [f"00000000-0000-7000-8000-{i:012d}" for i in range(5)]},
        row_count=APPROXIMATE_ROW_COUNT,
        row_count_method="approximate",
    )

    active_curators = mock_table(
        "public.active_curators",
        columns(("id", "uuid")),
        {},
        type="view",
        ddl="CREATE VIEW public.active_curators AS SELECT id FROM public.sowing_trial;\n",
    )

    empty_scan = mock_table(
        "public.empty_scan",
        columns(("rank", "text", True)),
        {},
        ddl="CREATE TABLE public.empty_scan (rank text);\n",
        row_count=500,
        rows_scanned=0,
    )

    wide_lookup = mock_table(
        "public.wide_lookup",
        columns(("a", "text"), ("b", "text"), ("c", "text")),
        {
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
        ddl="CREATE TABLE public.wide_lookup (a text, b text, c text);\n",
        row_count=100,
    )

    dropped_statistics = mock_table(
        "public.dropped_statistics",
        columns(("id", "uuid")),
        {
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
        ddl="CREATE TABLE public.dropped_statistics (id uuid PRIMARY KEY);\n",
        samples={"id": [f"00000000-0000-7000-8000-{i:012d}" for i in range(3)]},
        row_count=3,
    )

    curation_event = mock_table(
        "public.curation_event",
        columns(("id", "uuid"), ("condition", "text"), ("remark", "text")),
        {
            "id": ColumnStats(
                sql_type="uuid",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=100,
                cardinality_ratio=1.0,
                cardinality_method="exact",
                values=tuple(
                    ValueCount(value=f"00000000-0000-7000-8000-{i:012d}", count=1) for i in range(5)
                ),
                values_coverage=0.05,
                distribution="uniform",
                empty_count=0,
                length=_UUID_LENGTH,
                inferred=Inferred(candidate_key=True),
            ),
            "condition": ColumnStats(
                sql_type="text",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=3,
                cardinality_ratio=0.03,
                cardinality_method="exact",
                values=(
                    ValueCount(value=DELIMITER_VALUE, count=34),
                    ValueCount(value=LINE_BREAK_VALUE, count=33),
                    ValueCount(value=GRAMMAR_VALUE, count=33),
                ),
                values_coverage=1.0,
                distribution="uniform",
                empty_count=0,
                length=Length(min=9, max=31, avg=18.24, p95=31.0),
            ),
            "remark": ColumnStats(
                sql_type="text",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=3,
                cardinality_ratio=0.03,
                cardinality_method="exact",
                values=tuple(
                    ValueCount(value=value, count=count)
                    for value, count in zip(SPELLING_VALUES, (34, 33, 33), strict=True)
                ),
                values_coverage=1.0,
                distribution="uniform",
                empty_count=34,
                length=Length(min=0, max=93, avg=32.01, p95=93.0),
            ),
        },
        ddl="CREATE TABLE public.curation_event (id uuid PRIMARY KEY, condition text, "
        "remark text);\n",
        samples={
            "condition": [DELIMITER_VALUE, LINE_BREAK_VALUE, GRAMMAR_VALUE],
            "remark": list(SPELLING_VALUES),
        },
        row_count=100,
    )

    gauge = mock_table(
        "public.gauge",
        columns(
            ("wide", "double precision"),
            ("tiny", "double precision"),
            ("sparse", "integer", True),
            ("status", "text"),
        ),
        {
            "wide": ColumnStats(
                sql_type="double precision",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=EXTREME_ROW_COUNT,
                cardinality_ratio=1.0,
                cardinality_method="exact",
                range=Range(min=18446744073709541000.0, max=18446744073709552000.0),
                percentiles={"p50": EXTREME_WIDE_P50},
                mean=EXTREME_WIDE_P50,
                sum=184467440737095480000000.0,
                zero_count=0,
                negative_count=0,
                quantized_count=EXTREME_ROW_COUNT,
                values=(ValueCount(value=EXTREME_WIDE_P50, count=1),),
                distribution="uniform",
                frequencies=Frequencies(top=1, bottom=1, listed=1, total=EXTREME_ROW_COUNT),
            ),
            "tiny": ColumnStats(
                sql_type="double precision",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=97,
                cardinality_ratio=0.0097,
                cardinality_method="exact",
                range=Range(min=0.000000001, max=0.000000097),
                percentiles={"p50": 0.000000049},
                mean=EXTREME_TINY_MEAN,
                sum=0.00049,
                zero_count=0,
                negative_count=0,
                quantized_count=0,
                values=(ValueCount(value=0.000000049, count=104),),
                distribution="uniform",
                frequencies=Frequencies(top=104, bottom=103, listed=1, total=EXTREME_ROW_COUNT),
            ),
            "sparse": ColumnStats(
                sql_type="integer",
                nullable=True,
                null_count=9996,
                null_rate=EXTREME_NULL_RATE,
                cardinality=4,
                cardinality_ratio=0.0004,
                cardinality_method="exact",
                values=tuple(ValueCount(value=i, count=1) for i in range(1, 5)),
                values_coverage=1.0,
                distribution="uniform",
            ),
            "status": ColumnStats(
                sql_type="text",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=30,
                cardinality_ratio=0.003,
                cardinality_method="exact",
                values=(
                    ValueCount(value="ok", count=9994),
                    ValueCount(value="bad", count=2),
                    ValueCount(value="lost", count=2),
                ),
                values_coverage=0.9998,
                distribution="dominant_value",
                empty_count=0,
                length=Length(min=2, max=4, avg=2.0006, p95=2.0),
            ),
        },
        ddl="CREATE TABLE public.gauge (wide double precision, tiny double precision, "
        "sparse integer, status text);\n",
        null_patterns=NullPatterns(
            patterns=(NullPattern(columns=("sparse",), count=9996),),
            coverage=EXTREME_NULL_RATE,
        ),
        row_count=EXTREME_ROW_COUNT,
    )

    return {
        "public.gauge": gauge,
        "public.sowing_trial": sowing_trial,
        "public.cultivar": cultivar,
        "public.batch": batch,
        "public.active_curators": active_curators,
        "public.empty_scan": empty_scan,
        "public.wide_lookup": wide_lookup,
        "public.dropped_statistics": dropped_statistics,
        "public.curation_event": curation_event,
    }


class _MockPostgresAdapter(MockAdapter):
    """MockAdapter with REQUIRED_KEYS to satisfy the CLI's credential-resolution path."""

    REQUIRED_KEYS = ("host", "port", "database", "user", "password")

    def __init__(self, _credentials: dict[str, str], **_options: object) -> None:
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


def _inject_reader_only_states(print_root: Path) -> None:
    """Inject, after the gate, an orphan spelling (SPEC 2.2.4), a grain search with no outcome
    (SPEC 2.2.12) and an unreadable `profiled_at` - shapes the validator refuses.
    """

    path = print_root / "public" / "cultivar" / "statistics.yaml"
    statistics = yaml.safe_load(path.read_text())
    statistics["columns"][ORPHAN_SPELLING_COLUMN]["values"].append(
        {"value": ORPHAN_SPELLING_VALUE, "count": 1, "spelling_of": "Orphan-Spelling"},
    )
    path.write_text(yaml.safe_dump(statistics))
    path = print_root / "public" / "batch" / "statistics.yaml"
    statistics = yaml.safe_load(path.read_text())
    statistics["grain"] = {"keys": [], "search": {}}
    path.write_text(yaml.safe_dump(statistics))
    path = print_root / "public" / "wide_lookup" / "statistics.yaml"
    statistics = yaml.safe_load(path.read_text())
    statistics["profiled_at"] = UNREADABLE_PROFILED_AT
    path.write_text(yaml.safe_dump(statistics))
    path = print_root / MANIFEST_FILENAME
    manifest = yaml.safe_load(path.read_text())
    manifest["tables"][UNREADABLE_PROFILED_TABLE]["profiled_at"] = UNREADABLE_PROFILED_AT
    manifest["tables"][SEVERAL_EDGES_TABLE]["artifacts"]["relationships_annotations"] = (
        "relationships.annotations.yaml"
    )
    path.write_text(yaml.safe_dump(manifest))
    _inject_several_edges(print_root / "public" / "gauge")


def _inject_several_edges(table_dir: Path) -> None:
    """Give one column three edges, measured first and declared last, and reject the inferred one.

    Reciprocity forbids these shapes, hence after the gate.
    """

    def edge(target: str, column: str, detection: str) -> dict[str, Any]:
        return {
            "column": [SEVERAL_EDGES_COLUMN],
            "target_table": target,
            "target_column": [column],
            "detection": detection,
        }

    path = table_dir / "relationships.yaml"
    relationships = yaml.safe_load(path.read_text())
    relationships["refers_to"] = [
        edge("public.wide_lookup", "a", "measured"),
        edge(REJECTED_EDGE_TARGET, "id", "inferred"),
        {**edge(FK_TARGET_TABLE, "id", "declared"), "on_delete": "NO ACTION"},
    ]
    path.write_text(yaml.safe_dump(relationships, sort_keys=False))
    rejection = {**edge(REJECTED_EDGE_TARGET, "id", "inferred"), "verdict": "rejected"}
    del rejection["detection"]
    (table_dir / "relationships.annotations.yaml").write_text(
        yaml.safe_dump({"format_version": 1, "refers_to": [rejection]}, sort_keys=False),
    )


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
        with patch_registry({"postgres": _MockPostgresAdapter}):
            first = runner.invoke(main, ["generate", "--no-tui"])
            assert first.exit_code == 3, first.output

            second = runner.invoke(main, ["generate", "--no-tui"])
            assert second.exit_code == 0, second.output
    finally:
        os.chdir(old_cwd)

    print_root = project_dir / "prints" / CONN_NAME
    _inject_incomplete_grain_search(print_root)

    errors = conformance_errors(print_root)
    assert not errors, f"adversarial fixture is not conformant: {errors}"

    _drop_declared_statistics(print_root)
    _inject_reader_only_states(print_root)

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
