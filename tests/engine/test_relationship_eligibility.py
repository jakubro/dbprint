"""`eligible_target` and the inferred-edge filler omission (SPEC 2.3.7/2.3.8).

One fixture carries every concern, mirroring the shipped print: a composite-key table is
ineligible, a single-column-key one is eligible, and the same target is reached by both an
inferred and a declared edge, so the two `detection` values stay visibly different.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from dbprint.adapters.base import (
    ColumnMeta,
    ColumnStats,
    CommentsMeta,
    ForeignKeyMeta,
    UniqueKeyMeta,
)
from dbprint.adapters.mock import MockAdapter, MockTable
from dbprint.config.project import ConnectionConfig, DiffConfig, StatisticsConfig
from dbprint.engine import Engine, GenerateRequest


def _stats(sql_type: str, cardinality: int, row_count: int) -> ColumnStats:
    return ColumnStats(
        sql_type=sql_type,
        nullable=False,
        null_count=0,
        null_rate=0.0,
        cardinality=cardinality,
        cardinality_ratio=round(cardinality / row_count, 6),
        cardinality_method="exact",
    )


def _uuid_samples(n: int) -> list[str]:
    return [f"00000001-84d7-40dd-a29f-3e92756f{i:04x}" for i in range(n)]


def _fixture() -> dict[str, MockTable]:
    return {
        "seedbank.vault": MockTable(
            type="table",
            namespace_path=("seedbank", "vault"),
            ddl=(
                "CREATE TABLE seedbank.vault (\n"
                "    vault_id integer NOT NULL,\n"
                "    shelf_code character varying(8) NOT NULL\n"
                ");\n"
            ),
            columns=[
                ColumnMeta(
                    name="vault_id",
                    sql_type="integer",
                    nullable=False,
                    default=None,
                    ordinal=1,
                ),
                ColumnMeta(
                    name="shelf_code",
                    sql_type="character varying(8)",
                    nullable=False,
                    default=None,
                    ordinal=2,
                ),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "vault_id": _stats("integer", 8, 48),
                "shelf_code": _stats("character varying(8)", 6, 48),
            },
            samples={"vault_id": list(range(1, 9)), "shelf_code": ["A", "B", "C", "D", "E", "F"]},
            row_count=48,
            unique_keys=[UniqueKeyMeta(columns=("vault_id", "shelf_code"), primary=True)],
        ),
        "seedbank.collector": MockTable(
            type="table",
            namespace_path=("seedbank", "collector"),
            ddl="CREATE TABLE seedbank.collector (collector_id uuid NOT NULL);\n",
            columns=[
                ColumnMeta(
                    name="collector_id",
                    sql_type="uuid",
                    nullable=False,
                    default=None,
                    ordinal=1,
                ),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={"collector_id": _stats("uuid", 400, 400)},
            samples={"collector_id": _uuid_samples(20)},
            row_count=400,
            unique_keys=[UniqueKeyMeta(columns=("collector_id",), primary=True)],
        ),
        "seedbank.germination_trial": MockTable(
            type="table",
            namespace_path=("seedbank", "germination_trial"),
            ddl=(
                "CREATE TABLE seedbank.germination_trial (\n"
                "    trial_id integer NOT NULL,\n"
                "    collector_id uuid NOT NULL\n"
                ");\n"
            ),
            columns=[
                ColumnMeta(
                    name="trial_id",
                    sql_type="integer",
                    nullable=False,
                    default=None,
                    ordinal=1,
                ),
                ColumnMeta(
                    name="collector_id",
                    sql_type="uuid",
                    nullable=False,
                    default=None,
                    ordinal=2,
                ),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "trial_id": _stats("integer", 900, 900),
                "collector_id": _stats("uuid", 400, 900),
            },
            samples={
                "trial_id": list(range(1, 21)),
                "collector_id": _uuid_samples(20),
            },
            row_count=900,
            unique_keys=[UniqueKeyMeta(columns=("trial_id",), primary=True)],
        ),
        "seedbank.accession": MockTable(
            type="table",
            namespace_path=("seedbank", "accession"),
            ddl=(
                "CREATE TABLE seedbank.accession (\n"
                "    accession_id bigint NOT NULL,\n"
                "    collector_id uuid NOT NULL\n"
                ");\n"
            ),
            columns=[
                ColumnMeta(
                    name="accession_id",
                    sql_type="bigint",
                    nullable=False,
                    default=None,
                    ordinal=1,
                ),
                ColumnMeta(
                    name="collector_id",
                    sql_type="uuid",
                    nullable=False,
                    default=None,
                    ordinal=2,
                ),
            ],
            relationships=[
                ForeignKeyMeta(
                    column=("collector_id",),
                    target_table="seedbank.collector",
                    target_column=("collector_id",),
                    on_delete="RESTRICT",
                    on_update="NO ACTION",
                    constraint_name="accession_collector_id_fkey",
                ),
            ],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "accession_id": _stats("bigint", 2500, 2500),
                "collector_id": _stats("uuid", 400, 2500),
            },
            samples={
                "accession_id": list(range(1, 21)),
                "collector_id": _uuid_samples(20),
            },
            row_count=2500,
            unique_keys=[UniqueKeyMeta(columns=("accession_id",), primary=True)],
        ),
    }


def _conn(tmp_path: Path, *, infer_relationships: bool = True) -> ConnectionConfig:
    return ConnectionConfig(
        name="primary",
        adapter="postgres",
        auto=False,
        output=tmp_path,
        include=("*",),
        exclude=(),
        max_age_days=7,
        statistics=StatisticsConfig(),
        diff=DiffConfig(),
        infer_relationships=infer_relationships,
    )


def _relationships(tmp_path: Path, table: str) -> dict[str, Any]:
    path = tmp_path / "primary" / "seedbank" / table / "relationships.yaml"

    return yaml.safe_load(path.read_text())


class TestEligibleTarget:
    def test_a_keyed_unreferenced_table_is_a_measured_absence(self, tmp_path: Path) -> None:
        Engine(MockAdapter(_fixture()), _conn(tmp_path), tmp_path).generate()
        data = _relationships(tmp_path, "collector")

        assert data["eligible_target"] is True

    def test_a_keyless_table_is_ineligible(self, tmp_path: Path) -> None:
        Engine(MockAdapter(_fixture()), _conn(tmp_path), tmp_path).generate()
        data = _relationships(tmp_path, "vault")

        assert data["eligible_target"] is False
        assert data["referenced_by"] == []

    def test_the_field_is_absent_when_inference_never_ran(self, tmp_path: Path) -> None:
        Engine(
            MockAdapter(_fixture()),
            _conn(tmp_path, infer_relationships=False),
            tmp_path,
        ).generate()
        data = _relationships(tmp_path, "collector")

        assert "eligible_target" not in data


class TestFillerOmission:
    def test_an_inferred_edge_carries_neither_action(self, tmp_path: Path) -> None:
        Engine(MockAdapter(_fixture()), _conn(tmp_path), tmp_path).generate()
        data = _relationships(tmp_path, "germination_trial")
        inferred = next(e for e in data["refers_to"] if e["detection"] == "inferred")

        assert "on_delete" not in inferred
        assert "on_update" not in inferred

    def test_a_declared_edge_still_carries_both(self, tmp_path: Path) -> None:
        Engine(MockAdapter(_fixture()), _conn(tmp_path), tmp_path).generate()
        data = _relationships(tmp_path, "accession")
        declared = next(e for e in data["refers_to"] if e["detection"] == "declared")

        assert declared["on_delete"] == "RESTRICT"
        assert declared["on_update"] == "NO ACTION"

    def test_the_two_detection_values_stay_visibly_different_on_the_same_target(
        self,
        tmp_path: Path,
    ) -> None:
        """`collector.referenced_by` carries both an inferred and a declared edge."""

        Engine(MockAdapter(_fixture()), _conn(tmp_path), tmp_path).generate()
        data = _relationships(tmp_path, "collector")
        by_detection = {e["detection"]: e for e in data["referenced_by"]}

        assert set(by_detection) == {"inferred", "declared"}
        assert "on_delete" not in by_detection["inferred"]
        assert by_detection["declared"]["on_delete"] == "RESTRICT"


class TestBaselineHydrationSurvivesTheFiller:
    """An inferred edge without action keys must not vanish or report as changed."""

    def test_a_second_unchanged_run_reports_no_relationship_events(self, tmp_path: Path) -> None:
        fixture = _fixture()
        Engine(MockAdapter(fixture), _conn(tmp_path), tmp_path).generate()
        Engine(MockAdapter(fixture), _conn(tmp_path), tmp_path).generate()
        diff = yaml.safe_load((tmp_path / "primary" / "diff.yaml").read_text())
        kinds = {c["kind"] for c in diff["changes"]}

        assert not {k for k in kinds if k.startswith("relationship_")}

    def test_a_narrowed_run_still_preserves_the_inferred_referenced_by(
        self,
        tmp_path: Path,
    ) -> None:
        """`collector` alone is re-extracted, so its `referenced_by` is rebuilt from disk."""

        fixture = _fixture()
        Engine(MockAdapter(fixture), _conn(tmp_path), tmp_path).generate()
        Engine(MockAdapter(fixture), _conn(tmp_path), tmp_path).generate(
            GenerateRequest(cli_include=("seedbank.collector",)),
        )
        data = _relationships(tmp_path, "collector")
        detections = {e["detection"] for e in data["referenced_by"]}

        assert detections == {"inferred", "declared"}
