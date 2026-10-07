"""The curator/referencing mock fixture and the connection most engine tests generate it under."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dbprint.adapters import (
    ForeignKeyMeta,
    Inferred,
    Length,
    MockTable,
    NullPattern,
    NullPatterns,
    StatisticsConfig,
    UniqueKeyMeta,
    ValueCount,
)
from dbprint.config.project import ConnectionConfig
from tests._prints import columns, connection_config, exact_stats, mock_table


def conn_config(
    tmp_path: Path,
    *,
    enumeration_threshold: int | None = None,
    **overrides: Any,
) -> ConnectionConfig:
    statistics = (
        StatisticsConfig()
        if enumeration_threshold is None
        else StatisticsConfig(enumeration_threshold=enumeration_threshold)
    )

    return connection_config(output=tmp_path, statistics=statistics, **overrides)


def curator_fixture() -> dict[str, MockTable]:
    return referencing_fixture(
        "public.curator",
        "herbarium_id",
        "public.herbarium",
        ddl="CREATE TABLE public.curator (id uuid PRIMARY KEY);\n",
        relationships=[
            ForeignKeyMeta(
                column=("herbarium_id",),
                target_table="public.herbarium",
                target_column=("id",),
                on_delete="CASCADE",
                on_update="NO ACTION",
                constraint_name="curator_herbarium_fk",
            ),
        ],
    )


def referencing_fixture(
    child: str,
    fk_column: str,
    parent: str,
    *,
    ddl: str,
    relationships: list[ForeignKeyMeta],
    parent_keys: tuple[UniqueKeyMeta, ...] = (),
) -> dict[str, MockTable]:
    uuids = [f"00000000-0000-7000-8000-{i:012d}" for i in range(20)]
    uuid_length = Length(min=36, max=36, avg=36.0, p95=36.0)

    return {
        child: mock_table(
            child,
            columns(("id", "uuid"), (fk_column, "uuid", True)),
            {
                # A fully-unique uuid classifies text (SPEC 4.2), which SPEC 2.2.3 marks R;
                # 20 of 100 distinct values, each count 1, is long_tail.
                "id": exact_stats(
                    "uuid",
                    100,
                    1.0,
                    values=tuple(ValueCount(value=u, count=1) for u in uuids),
                    values_coverage=0.2,
                    distribution="long_tail",
                    empty_count=0,
                    length=uuid_length,
                    inferred=Inferred(candidate_key=True),
                ),
                # The referencing column classifies foreign_key_candidate, for which SPEC 2.2.3
                # marks these three fields required.
                fk_column: exact_stats(
                    "uuid",
                    20,
                    0.2,
                    nullable=True,
                    null_count=10,
                    null_rate=0.1,
                    values=(
                        ValueCount(value="00000000-0000-7000-8000-000000000001", count=9),
                        ValueCount(value="00000000-0000-7000-8000-000000000002", count=8),
                    ),
                    values_coverage=0.188889,
                    distribution="uniform",
                    length=uuid_length,
                ),
            },
            ddl=ddl,
            relationships=relationships,
            # A census is owed wherever a column carries a null (SPEC 2.2.10), and is stated
            # rather than derived: per-column counts cannot say which nulls share a row.
            null_patterns=NullPatterns(
                patterns=(
                    NullPattern(columns=(), count=90),
                    NullPattern(columns=(fk_column,), count=10),
                ),
                coverage=1.0,
            ),
            samples={"id": uuids},
            row_count=100,
        ),
        parent: mock_table(
            parent,
            columns(("id", "uuid")),
            {
                # cardinality 20 is at or below enumeration_threshold(50) and top_n_values(20),
                # so the column classifies categorical with an exhaustive value list.
                "id": exact_stats(
                    "uuid",
                    20,
                    1.0,
                    values=tuple(ValueCount(value=u, count=1) for u in uuids),
                    values_coverage=1.0,
                    distribution="uniform",
                    length=uuid_length,
                    inferred=Inferred(candidate_key=True),
                ),
            },
            ddl=f"CREATE TABLE {parent} (id uuid PRIMARY KEY);\n",
            samples={"id": uuids},
            row_count=20,
            unique_keys=list(parent_keys),
        ),
    }
