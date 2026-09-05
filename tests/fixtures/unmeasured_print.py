"""A print carrying the `unmeasured` marker at both grains, written by the producer itself.

No healthy run exercises a degrade; regenerate with `python -m tests.fixtures.unmeasured_print`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from dbprint.adapters import (
    ColumnMeta,
    ColumnStats,
    CommentsMeta,
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


COMMITTED = Path(__file__).resolve().parent / "unmeasured_print"
CONNECTION = "degraded"
TABLE = "seedbank.accession"
ROW_COUNT = 400


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
    """Rebuild the committed copy from scratch."""

    if COMMITTED.exists():
        shutil.rmtree(COMMITTED)

    COMMITTED.mkdir(parents=True)
    build(COMMITTED)


def _fixture() -> dict[str, MockTable]:
    """One table whose temporal block was lost and whose census failed, plus a column with nulls."""

    return {
        TABLE: MockTable(
            type="table",
            namespace_path=("seedbank", "accession"),
            ddl=(
                "CREATE TABLE seedbank.accession (\n"
                "    logged_at timestamp NOT NULL,\n"
                "    field_notes text\n"
                ");\n"
            ),
            columns=[
                ColumnMeta(
                    name="logged_at",
                    sql_type="timestamp",
                    nullable=False,
                    default=None,
                    ordinal=1,
                ),
                ColumnMeta(
                    name="field_notes",
                    sql_type="text",
                    nullable=True,
                    default=None,
                    ordinal=2,
                ),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={"logged_at": _degraded(), "field_notes": _measured()},
            samples={},
            row_count=ROW_COUNT,
        ),
    }


def _degraded() -> ColumnStats:
    """The temporal column whose whole block was attempted and lost."""

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
