"""A print carrying the `unmeasured` marker at both grains, written by the producer itself.

No healthy run exercises a degrade; regenerate with `python -m tests.fixtures.unmeasured_print`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from dbprint.adapters import (
    ColumnMeta,
    ColumnStats,
    Length,
    MockAdapter,
    MockTable,
    NullPatterns,
    StatisticsConfig,
    TableCounts,
    ValueCount,
)
from dbprint.adapters.base import BaseStats, TableScope, temporal_block_unmeasured
from dbprint.config import ConnectionConfig
from dbprint.engine import Engine
from tests._prints import columns, mock_table
from tests._scripts import load_script


COMMITTED = Path(__file__).resolve().parent / "unmeasured_print"
CONNECTION = "degraded"
TABLE = "seedbank.accession"
ROW_COUNT = 400

# The engine stamps the real run instant, so an unfrozen restage rewrites four files that
# carry no other change - and the gate normalizes instants on both sides, so nothing reads it.
FROZEN_INSTANT = "2026-09-04T14:00:17Z"


class CensusFails(MockAdapter):
    """A producer whose null census raises, which is the only route to the file-level marker.

    Every other absence is a finding; this one the orchestrator learns only from a raise.
    """

    def compute_null_patterns(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        config: StatisticsConfig,
        counts: TableCounts,
        base: dict[str, BaseStats],
        scope: TableScope | None = None,
    ) -> NullPatterns | None:
        del fqn, columns, config, counts, base, scope

        raise RuntimeError("null census failed")


def build(target: Path) -> Path:
    """Generate the print under `target` and return its connection root."""

    conn = ConnectionConfig(
        name=CONNECTION,
        adapter="postgres",
        output=target,
        infer_relationships=False,
    )
    Engine(CensusFails(_fixture()), conn, target).generate()

    return target / CONNECTION


def restage() -> None:
    """Rebuild the committed copy from scratch, with its run instants frozen."""

    if COMMITTED.exists():
        shutil.rmtree(COMMITTED)

    COMMITTED.mkdir(parents=True)
    load_script("example_support").normalize_timestamps(build(COMMITTED), FROZEN_INSTANT)


def _fixture() -> dict[str, MockTable]:
    """One table whose temporal block was lost and whose census failed, plus a column with nulls."""

    return {
        TABLE: mock_table(
            "seedbank.accession",
            columns(("logged_at", "timestamp"), ("field_notes", "text", True)),
            {"logged_at": _degraded(), "field_notes": _measured()},
            ddl="CREATE TABLE seedbank.accession (\n"
            "    logged_at timestamp NOT NULL,\n"
            "    field_notes text\n"
            ");\n",
            row_count=ROW_COUNT,
        ),
    }


def _degraded() -> ColumnStats:
    """The temporal column whose whole block this run did not measure."""

    return ColumnStats(
        sql_type="timestamp",
        nullable=False,
        null_count=0,
        null_rate=0.0,
        cardinality=ROW_COUNT,
        cardinality_ratio=1.0,
        cardinality_method="exact",
        unmeasured=temporal_block_unmeasured("timestamp"),
    )


def _measured() -> ColumnStats:
    """A text column that answered everything, and carries the nulls the census never read."""

    return ColumnStats(
        sql_type="text",
        nullable=True,
        null_count=40,
        null_rate=0.1,
        cardinality=360,
        cardinality_ratio=0.9,
        cardinality_method="exact",
        empty_count=0,
        length=Length(min=22, max=22, avg=22.0, p95=22.0),
        values=(ValueCount(value="collected in the field", count=40),),
        values_coverage=0.111111,
        distribution="long_tail",
    )


if __name__ == "__main__":
    restage()
    print(f"restaged {COMMITTED}")
