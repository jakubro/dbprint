"""Engine orchestrator tests using MockAdapter."""

from __future__ import annotations

import logging
import shutil
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from dbprint.adapters import (
    BaseStats,
    ColumnMeta,
    ColumnStats,
    CommentsMeta,
    ForeignKeyMeta,
    Frequencies,
    IndexMeta,
    Inferred,
    Length,
    MockAdapter,
    MockTable,
    NullPattern,
    NullPatterns,
    PhysicalLayout,
    PhysicalLayoutKey,
    Range,
    StatisticsConfig,
    TableCounts,
    TableScope,
    UniqueKeyMeta,
    ValueCount,
)
from dbprint.config.project import ConnectionConfig, DiffConfig, RedactRule, RuleConfig
from dbprint.conformance import validate_print
from dbprint.engine import (
    EXIT_DRIFT,
    EXIT_OK,
    EXIT_PARTIAL,
    EXIT_TOTAL_FAILURE,
    DiffRequest,
    DiffResult,
    Engine,
    GenerateRequest,
    GenerateResult,
    SketchFailure,
    orchestrator,
)
from dbprint.spec.sketch import K as SPEC_SKETCH_K
from dbprint.spec.sketch import canonical_form, decode_sketch, low64_md5
from tests.conftest import normalize_instants


def _conn_config(
    tmp_path: Path,
    *,
    include=("*",),
    exclude=(),
    max_age_days=7,
    enumeration_threshold: int | None = None,
) -> ConnectionConfig:
    statistics = (
        StatisticsConfig()
        if enumeration_threshold is None
        else StatisticsConfig(enumeration_threshold=enumeration_threshold)
    )

    return ConnectionConfig(
        name="primary",
        adapter="postgres",
        auto=False,
        output=tmp_path,
        include=include,
        exclude=exclude,
        max_age_days=max_age_days,
        statistics=statistics,
        diff=DiffConfig(),
    )


def _curator_fixture() -> dict[str, MockTable]:
    return {
        "public.curator": MockTable(
            type="table",
            namespace_path=("public", "curator"),
            ddl="CREATE TABLE public.curator (id uuid PRIMARY KEY);\n",
            columns=[
                ColumnMeta(name="id", sql_type="uuid", nullable=False, default=None, ordinal=1),
                ColumnMeta(
                    name="herbarium_id",
                    sql_type="uuid",
                    nullable=True,
                    default=None,
                    ordinal=2,
                ),
            ],
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
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                # A fully-unique uuid classifies text (SPEC 4.2), which SPEC 2.2.3 marks R;
                # 20 of 100 distinct values, each count 1, is long_tail.
                "id": ColumnStats(
                    sql_type="uuid",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=100,
                    cardinality_ratio=1.0,
                    cardinality_method="exact",
                    values=tuple(
                        ValueCount(value=f"00000000-0000-7000-8000-{i:012d}", count=1)
                        for i in range(20)
                    ),
                    values_coverage=0.2,
                    distribution="long_tail",
                    empty_count=0,
                    length=Length(min=36, max=36, avg=36.0, p95=36.0),
                    inferred=Inferred(candidate_key=True),
                ),
                # herbarium_id carries a declared FK, so it classifies foreign_key_candidate,
                # for which SPEC 2.2.3 marks these three fields required.
                "herbarium_id": ColumnStats(
                    sql_type="uuid",
                    nullable=True,
                    null_count=10,
                    null_rate=0.1,
                    cardinality=20,
                    cardinality_ratio=0.2,
                    cardinality_method="exact",
                    values=(
                        ValueCount(value="00000000-0000-7000-8000-000000000001", count=9),
                        ValueCount(value="00000000-0000-7000-8000-000000000002", count=8),
                    ),
                    values_coverage=0.188889,
                    distribution="uniform",
                    length=Length(min=36, max=36, avg=36.0, p95=36.0),
                ),
            },
            # A census is owed wherever a column carries a null (SPEC 2.2.10), and is stated
            # rather than derived: per-column counts cannot say which nulls share a row.
            null_patterns=NullPatterns(
                patterns=(
                    NullPattern(columns=(), count=90),
                    NullPattern(columns=("herbarium_id",), count=10),
                ),
                coverage=1.0,
            ),
            samples={"id": [f"00000000-0000-7000-8000-{i:012d}" for i in range(20)]},
            row_count=100,
        ),
        "public.herbarium": MockTable(
            type="table",
            namespace_path=("public", "herbarium"),
            ddl="CREATE TABLE public.herbarium (id uuid PRIMARY KEY);\n",
            columns=[
                ColumnMeta(name="id", sql_type="uuid", nullable=False, default=None, ordinal=1),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                # cardinality 20 is at or below enumeration_threshold(50) and top_n_values(20),
                # so the column classifies categorical with an exhaustive value list.
                "id": ColumnStats(
                    sql_type="uuid",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=20,
                    cardinality_ratio=1.0,
                    cardinality_method="exact",
                    values=tuple(
                        ValueCount(value=f"00000000-0000-7000-8000-{i:012d}", count=1)
                        for i in range(20)
                    ),
                    values_coverage=1.0,
                    distribution="uniform",
                    length=Length(min=36, max=36, avg=36.0, p95=36.0),
                    inferred=Inferred(candidate_key=True),
                ),
            },
            samples={"id": [f"00000000-0000-7000-8000-{i:012d}" for i in range(20)]},
            row_count=20,
        ),
    }


def _numeric_looks_like_fixture() -> dict[str, MockTable]:
    """Four numeric-typed columns reaching `looks_like` through different classifications,
    plus one text control - the suppression keys on SQL type, not on classification.
    """

    return {
        "fixture.probe_target": MockTable(
            type="table",
            namespace_path=("fixture", "probe_target"),
            ddl="CREATE TABLE fixture.probe_target (id integer PRIMARY KEY);\n",
            columns=[
                ColumnMeta(name="id", sql_type="integer", nullable=False, default=None, ordinal=1),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "id": ColumnStats(
                    sql_type="integer",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=20,
                    cardinality_ratio=1.0,
                    cardinality_method="exact",
                    values=tuple(ValueCount(value=1000 + i, count=1) for i in range(20)),
                    values_coverage=1.0,
                    distribution="uniform",
                    inferred=Inferred(candidate_key=True),
                ),
            },
            samples={"id": [str(1000 + i) for i in range(20)]},
            row_count=20,
        ),
        "fixture.type_probe": MockTable(
            type="table",
            namespace_path=("fixture", "type_probe"),
            ddl=(
                "CREATE TABLE fixture.type_probe (\n"
                "    target_id integer NOT NULL,\n"
                "    status_code integer NOT NULL,\n"
                "    serial_label character varying(20) NOT NULL,\n"
                "    device_imei bigint NOT NULL,\n"
                "    partial_code integer NOT NULL\n"
                ");\n"
            ),
            columns=[
                ColumnMeta(
                    name="target_id",
                    sql_type="integer",
                    nullable=False,
                    default=None,
                    ordinal=1,
                ),
                ColumnMeta(
                    name="status_code",
                    sql_type="integer",
                    nullable=False,
                    default=None,
                    ordinal=2,
                ),
                ColumnMeta(
                    name="serial_label",
                    sql_type="character varying(20)",
                    nullable=False,
                    default=None,
                    ordinal=3,
                ),
                ColumnMeta(
                    name="device_imei",
                    sql_type="bigint",
                    nullable=False,
                    default=None,
                    ordinal=4,
                ),
                ColumnMeta(
                    name="partial_code",
                    sql_type="integer",
                    nullable=False,
                    default=None,
                    ordinal=5,
                ),
            ],
            relationships=[
                ForeignKeyMeta(
                    column=("target_id",),
                    target_table="fixture.probe_target",
                    target_column=("id",),
                    on_delete="CASCADE",
                    on_update="NO ACTION",
                    constraint_name="type_probe_target_fk",
                ),
            ],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                # Declared FK -> foreign_key_candidate. Digit-shaped samples would otherwise
                # publish numeric_string on a column whose own type already says "integer".
                "target_id": ColumnStats(
                    sql_type="integer",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=20,
                    cardinality_ratio=0.2,
                    cardinality_method="exact",
                    values=tuple(ValueCount(value=1000 + i, count=5) for i in range(20)),
                    values_coverage=0.2,
                    distribution="uniform",
                ),
                # cardinality 3 <= enumeration_threshold(50) -> categorical.
                "status_code": ColumnStats(
                    sql_type="integer",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=3,
                    cardinality_ratio=0.03,
                    cardinality_method="exact",
                    values=(
                        ValueCount(value=1, count=40),
                        ValueCount(value=2, count=35),
                        ValueCount(value=3, count=25),
                    ),
                    values_coverage=1.0,
                    distribution="uniform",
                ),
                # cardinality above enumeration_threshold(50) -> text: the control case, where
                # a digit-shaped sample on a non-numeric type must still publish the verdict.
                "serial_label": ColumnStats(
                    sql_type="character varying(20)",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=60,
                    cardinality_ratio=0.6,
                    cardinality_method="exact",
                    values=tuple(ValueCount(value=str(9000 + i), count=1) for i in range(60)),
                    values_coverage=0.6,
                    distribution="uniform",
                    empty_count=0,
                    length=Length(min=4, max=4, avg=4.0, p95=4.0),
                ),
                # cardinality 1 <= enumeration_threshold -> categorical, but its samples are a
                # valid IMEI - a pattern that outranks numeric_string, so suppression skips it.
                "device_imei": ColumnStats(
                    sql_type="bigint",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=1,
                    cardinality_ratio=0.01,
                    cardinality_method="exact",
                    values=(ValueCount(value=352099001761481, count=100),),
                    values_coverage=1.0,
                    distribution="uniform",
                ),
                # A near-miss numeric_string (60%, between the floor and the verdict bar), so
                # suppression must withhold the candidate/share too, not only a verdict.
                "partial_code": ColumnStats(
                    sql_type="integer",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=5,
                    cardinality_ratio=0.05,
                    cardinality_method="exact",
                    values=tuple(ValueCount(value=i, count=20) for i in range(1, 6)),
                    values_coverage=1.0,
                    distribution="uniform",
                ),
            },
            samples={
                "target_id": [str(1000 + i) for i in range(20)],
                "status_code": ["1", "2", "3"] * 20,
                "serial_label": [str(9000 + i) for i in range(60)],
                "device_imei": ["352099001761481"] * 20,
                "partial_code": [str(i) for i in range(1, 13)]
                + ["ref-a", "ref-b", "ref-c", "ref-d", "ref-e", "ref-f", "ref-g", "ref-h"],
            },
            row_count=100,
        ),
    }


def _build_engine(tmp_path: Path, fixture: dict[str, MockTable]) -> Engine:
    adapter = MockAdapter(fixture)
    conn = _conn_config(tmp_path)

    return Engine(adapter, conn, tmp_path)


def _profiled_at(manifest: Path) -> dict[str, str]:
    """Map each manifest table to its `profiled_at` stamp."""

    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))

    return {fqn: entry["profiled_at"] for fqn, entry in data["tables"].items()}


def _thresholds(manifest: Path) -> dict[str, Any]:
    """Map each manifest table to the freshness threshold its entry records."""

    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))

    return {fqn: entry.get("max_age_days") for fqn, entry in data["tables"].items()}


def _age_manifest_entry(manifest: Path, fqn: str, profiled_at: str) -> None:
    """Backdate one table so the next run finds it stale while its siblings stay fresh."""

    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["tables"][fqn]["profiled_at"] = profiled_at
    manifest.write_text(
        yaml.dump(data, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def _referencers(relationships: Path) -> list[str]:
    """Referencer tables listed under a print's `referenced_by`."""

    data = yaml.safe_load(relationships.read_text(encoding="utf-8"))

    return [e["referencer_table"] for e in data.get("referenced_by") or []]


def _changes_by_kind(diff_path: Path) -> dict[str, list[dict[str, Any]]]:
    """Group a written diff.yaml's events by kind; empty when the run found nothing."""

    grouped: dict[str, list[dict[str, Any]]] = {}

    for change in yaml.safe_load(diff_path.read_text(encoding="utf-8"))["changes"]:
        grouped.setdefault(change["kind"], []).append(change)

    return grouped


def _drifted_curator_fixture() -> dict[str, MockTable]:
    """The curator table one migration on: a new FK column, a retyped one, moved nulls."""

    fixture = _curator_fixture()
    curator = fixture["public.curator"]
    fixture["public.curator"] = replace(
        curator,
        columns=[
            *(
                c if c.name != "herbarium_id" else replace(c, sql_type="text")
                for c in curator.columns
            ),
            ColumnMeta(
                name="parent_herbarium_id",
                sql_type="uuid",
                nullable=True,
                default=None,
                ordinal=3,
            ),
        ],
        relationships=[
            *curator.relationships,
            ForeignKeyMeta(
                column=("parent_herbarium_id",),
                target_table="public.herbarium",
                target_column=("id",),
                on_delete="SET NULL",
                on_update="NO ACTION",
                constraint_name="curator_parent_herbarium_fk",
            ),
        ],
        stats={
            **curator.stats,
            "herbarium_id": replace(
                curator.stats["herbarium_id"],
                sql_type="text",
                null_count=12,
                null_rate=0.12,
            ),
            "parent_herbarium_id": ColumnStats(
                sql_type="uuid",
                nullable=True,
                null_count=5,
                null_rate=0.05,
                cardinality=4,
                cardinality_ratio=0.04,
                cardinality_method="exact",
                values=(ValueCount(value="00000000-0000-7000-8000-000000000001", count=3),),
                values_coverage=0.031579,
                distribution="uniform",
                length=Length(min=36, max=36, avg=36.0, p95=36.0),
            ),
        },
    )

    return fixture


def _narrowed_curator_fixture() -> dict[str, MockTable]:
    """The curator table with `herbarium_id` and the foreign key it carried both gone."""

    fixture = _curator_fixture()
    curator = fixture["public.curator"]
    fixture["public.curator"] = replace(
        curator,
        columns=[c for c in curator.columns if c.name != "herbarium_id"],
        relationships=[],
        stats={name: s for name, s in curator.stats.items() if name != "herbarium_id"},
    )

    return fixture


def _bare_empty_stats(sql_type: str, *, nullable: bool = True) -> ColumnStats:
    """Cardinality-0 column with NO value collections - the adapters' empty-table fast path."""

    return ColumnStats(
        sql_type=sql_type,
        nullable=nullable,
        null_count=0,
        null_rate=0.0,
        cardinality=0,
        cardinality_ratio=0.0,
        cardinality_method="exact",
    )


def _empty_table_fixture() -> dict[str, MockTable]:
    """Empty table covering every value-shape branch at cardinality 0: categorical, FK, boolean."""

    return {
        "public.empty_t": MockTable(
            type="table",
            namespace_path=("public", "empty_t"),
            ddl="CREATE TABLE public.empty_t (id uuid, herbarium_id uuid, status text, flag boolean);\n",
            columns=[
                ColumnMeta(name="id", sql_type="uuid", nullable=False, default=None, ordinal=1),
                ColumnMeta(
                    name="herbarium_id",
                    sql_type="uuid",
                    nullable=True,
                    default=None,
                    ordinal=2,
                ),
                ColumnMeta(name="status", sql_type="text", nullable=True, default=None, ordinal=3),
                ColumnMeta(name="flag", sql_type="boolean", nullable=True, default=None, ordinal=4),
            ],
            relationships=[
                ForeignKeyMeta(
                    column=("herbarium_id",),
                    target_table="public.herbarium",
                    target_column=("id",),
                    on_delete="CASCADE",
                    on_update="NO ACTION",
                    constraint_name="empty_herbarium_fk",
                ),
            ],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "id": _bare_empty_stats("uuid", nullable=False),
                "herbarium_id": _bare_empty_stats("uuid"),
                "status": _bare_empty_stats("text"),
                "flag": _bare_empty_stats("boolean"),
            },
            samples={},
            row_count=0,
        ),
    }


class TestEmptyTableConformance:
    """Empty table -> engine fills empty value collections -> conformance-clean."""

    def test_empty_table_generates_conformant_print(self, tmp_path: Path) -> None:
        result = _build_engine(tmp_path, _empty_table_fixture()).generate()
        assert result.summary.failed == 0

        print_dir = tmp_path / "primary"
        errors = [i for i in validate_print(print_dir) if i.severity == "error"]
        assert errors == [], "Conformance violations:\n" + "\n".join(
            f"  {e.code} at {e.path}: {e.detail}" for e in errors
        )

        cols = yaml.safe_load((print_dir / "public/empty_t/statistics.yaml").read_text())["columns"]

        assert cols["id"]["classification"] == "categorical"
        assert cols["id"]["values"] == []
        # Nothing to list is everything there is to list.
        assert cols["id"]["values_coverage"] == 1.0
        assert "distribution" in cols["id"]

        assert cols["status"]["classification"] == "categorical"
        assert cols["status"]["values"] == []

        assert cols["herbarium_id"]["classification"] == "foreign_key_candidate"
        assert cols["herbarium_id"]["values"] == []
        assert cols["herbarium_id"]["values_coverage"] == 1.0
        assert "distribution" in cols["herbarium_id"]

        assert cols["flag"]["classification"] == "boolean"
        assert cols["flag"]["values"] == []
        assert cols["flag"]["values_coverage"] == 1.0


class TestHappyPath:
    def test_first_run_creates_artifacts(self, tmp_path: Path) -> None:
        engine = _build_engine(tmp_path, _curator_fixture())
        result = engine.generate()

        assert isinstance(result, GenerateResult)
        assert result.summary.ok == 2
        assert result.summary.failed == 0

        curator_dir = tmp_path / "primary" / "public" / "curator"
        assert (curator_dir / "ddl.sql").is_file()
        assert (curator_dir / "statistics.yaml").is_file()
        assert (curator_dir / "relationships.yaml").is_file()

        manifest = yaml.safe_load((tmp_path / "primary" / "manifest.yaml").read_text())
        assert manifest["format_version"] == 1
        assert "public.curator" in manifest["tables"]
        assert "public.herbarium" in manifest["tables"]

    def test_first_run_diff_lists_all_tables_added(self, tmp_path: Path) -> None:
        engine = _build_engine(tmp_path, _curator_fixture())
        engine.generate()
        diff = yaml.safe_load((tmp_path / "primary" / "diff.yaml").read_text())
        kinds = {c["kind"] for c in diff["changes"]}
        assert "table_added" in kinds
        assert diff["summary"]["tables_added"] == 2


class TestGeneratePathDiff:
    """The diff a run writes compares the live database against the committed print.

    Artifacts are written inside the extraction loop, so a baseline read after the loop is
    the print this run already overwrote - the new state compared against itself.
    """

    def _regenerate(
        self,
        tmp_path: Path,
        fixture: dict[str, MockTable],
    ) -> dict[str, list[dict[str, Any]]]:
        """Commit the base print, re-profile `fixture`, and read the diff that run wrote."""

        _build_engine(tmp_path, _curator_fixture()).generate()
        _build_engine(tmp_path, fixture).generate(GenerateRequest(force=True))

        return _changes_by_kind(tmp_path / "primary" / "diff.yaml")

    def test_a_new_column_is_reported(self, tmp_path: Path) -> None:
        events = self._regenerate(tmp_path, _drifted_curator_fixture())

        assert [(c["table"], c["column"]) for c in events["column_added"]] == [
            ("public.curator", "parent_herbarium_id"),
        ]

    def test_a_retyped_column_is_reported(self, tmp_path: Path) -> None:
        events = self._regenerate(tmp_path, _drifted_curator_fixture())
        [event] = events["column_type_changed"]

        assert (event["column"], event["before"], event["after"]) == (
            "herbarium_id",
            "uuid",
            "text",
        )

    def test_a_new_relationship_is_reported(self, tmp_path: Path) -> None:
        events = self._regenerate(tmp_path, _drifted_curator_fixture())

        assert [c["source_column"] for c in events["relationship_added"]] == [
            ["parent_herbarium_id"],
        ]

    def test_moved_statistics_are_reported(self, tmp_path: Path) -> None:
        events = self._regenerate(tmp_path, _drifted_curator_fixture())
        moved = {(c["column"], c["stat"]) for c in events["statistic_changed"]}

        assert ("herbarium_id", "null_count") in moved

    def test_a_dropped_column_takes_its_relationship_with_it(self, tmp_path: Path) -> None:
        events = self._regenerate(tmp_path, _narrowed_curator_fixture())

        assert [c["column"] for c in events["column_removed"]] == ["herbarium_id"]
        assert [c["source_column"] for c in events["relationship_removed"]] == [["herbarium_id"]]

    def test_an_unchanged_database_reports_nothing(self, tmp_path: Path) -> None:
        """The control: a run must report what moved, not manufacture events."""

        assert self._regenerate(tmp_path, _curator_fixture()) == {}

    def test_a_table_skipped_as_fresh_is_not_a_change(self, tmp_path: Path) -> None:
        """A skipped table is not rewritten, so its committed print is already the baseline."""

        _build_engine(tmp_path, _curator_fixture()).generate()
        _build_engine(tmp_path, _curator_fixture()).generate()
        diff = yaml.safe_load((tmp_path / "primary" / "diff.yaml").read_text())

        assert diff["changes"] == []

    def test_a_table_skipped_as_fresh_is_not_certified_unchanged_either(
        self,
        tmp_path: Path,
    ) -> None:
        """It compared equal to itself, which is no evidence about the database."""

        _build_engine(tmp_path, _curator_fixture()).generate()
        _build_engine(tmp_path, _curator_fixture()).generate()
        summary = yaml.safe_load((tmp_path / "primary" / "diff.yaml").read_text())["summary"]

        assert summary["unevaluated_tables"] == 2
        assert summary["unchanged_tables"] == 0


class TestRelationshipGraph:
    def test_referenced_by_populated_second_pass(self, tmp_path: Path) -> None:
        engine = _build_engine(tmp_path, _curator_fixture())
        engine.generate()
        herbarium = yaml.safe_load(
            (tmp_path / "primary" / "public" / "herbarium" / "relationships.yaml").read_text(),
        )
        assert herbarium["referenced_by"][0]["referencer_table"] == "public.curator"
        assert herbarium["referenced_by"][0]["referencer_column"] == ["herbarium_id"]


class TestClassification:
    def test_a_unique_uuid_column_classifies_text(self, tmp_path: Path) -> None:
        """Uniqueness is not a classification (SPEC 4.2) - a unique uuid stays text."""

        engine = _build_engine(tmp_path, _curator_fixture())
        engine.generate()
        stats = yaml.safe_load(
            (tmp_path / "primary" / "public" / "curator" / "statistics.yaml").read_text(),
        )
        assert stats["columns"]["id"]["classification"] == "text"

    def test_looks_like_uuid_detected(self, tmp_path: Path) -> None:
        engine = _build_engine(tmp_path, _curator_fixture())
        engine.generate()
        stats = yaml.safe_load(
            (tmp_path / "primary" / "public" / "curator" / "statistics.yaml").read_text(),
        )
        assert stats["columns"]["id"]["inferred"]["looks_like"] == "uuid"

    def test_a_genuinely_unique_column_carries_no_exception_marker(self, tmp_path: Path) -> None:
        engine = _build_engine(tmp_path, _curator_fixture())
        engine.generate()
        stats = yaml.safe_load(
            (tmp_path / "primary" / "public" / "curator" / "statistics.yaml").read_text(),
        )
        assert "candidate_key_exception" not in stats["columns"]["id"]["inferred"]

    def test_looks_like_carries_its_own_evidence(self, tmp_path: Path) -> None:
        """A published verdict is beside the draw size and how much of it agreed."""

        engine = _build_engine(tmp_path, _curator_fixture())
        engine.generate()
        stats = yaml.safe_load(
            (tmp_path / "primary" / "public" / "curator" / "statistics.yaml").read_text(),
        )
        inferred = stats["columns"]["id"]["inferred"]

        assert inferred["sampled"] > 0
        # Every seeded id is a genuine uuid, so the whole draw agreed.
        assert inferred["matched"] == inferred["sampled"]

    def test_a_measured_shortfall_carries_the_exception_marker(self, tmp_path: Path) -> None:
        """SPEC 4.2: `candidate_key_exception` is wired end to end, not only in `spec/`."""

        engine = _build_engine(tmp_path, _near_unique_fixture())
        engine.generate()
        stats = yaml.safe_load(
            (tmp_path / "primary" / "public" / "curator" / "statistics.yaml").read_text(),
        )

        assert stats["columns"]["id"]["classification"] == "text"
        assert (
            stats["columns"]["id"]["inferred"]["candidate_key_exception"] == "measured_duplicates"
        )


class TestNumericStringSuppressedOnNumericType:
    """SPEC 4.1.5: `numeric_string` is withheld where the column's own SQL type is already
    numeric, on every classification that would otherwise publish it."""

    def test_a_numeric_fk_source_carries_no_looks_like(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _numeric_looks_like_fixture()).generate()
        stats = yaml.safe_load(
            (tmp_path / "primary" / "fixture" / "type_probe" / "statistics.yaml").read_text(),
        )
        inferred = stats["columns"]["target_id"].get("inferred") or {}

        assert stats["columns"]["target_id"]["classification"] == "foreign_key_candidate"
        assert "looks_like" not in inferred
        assert "sampled" not in inferred
        assert "matched" not in inferred

    def test_a_numeric_categorical_column_carries_no_looks_like(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _numeric_looks_like_fixture()).generate()
        stats = yaml.safe_load(
            (tmp_path / "primary" / "fixture" / "type_probe" / "statistics.yaml").read_text(),
        )
        inferred = stats["columns"]["status_code"].get("inferred") or {}

        assert stats["columns"]["status_code"]["classification"] == "categorical"
        assert "looks_like" not in inferred
        assert "sampled" not in inferred
        assert "matched" not in inferred

    def test_a_numeric_near_miss_carries_no_looks_like_candidate_either(
        self,
        tmp_path: Path,
    ) -> None:
        """SPEC 4.1.5's suppression reaches the near-miss pair too, not only a published verdict -
        a numeric-typed column withholds `numeric_string` whichever share it scored.
        """

        _build_engine(tmp_path, _numeric_looks_like_fixture()).generate()
        stats = yaml.safe_load(
            (tmp_path / "primary" / "fixture" / "type_probe" / "statistics.yaml").read_text(),
        )
        inferred = stats["columns"]["partial_code"].get("inferred") or {}

        assert stats["columns"]["partial_code"]["classification"] == "categorical"
        assert "looks_like" not in inferred
        assert "looks_like_candidate" not in inferred
        assert "looks_like_candidate_share" not in inferred

    def test_a_text_column_of_digit_strings_still_publishes_the_verdict(
        self,
        tmp_path: Path,
    ) -> None:
        """The control case: the exclusion is type-aware, not classification-aware alone."""

        _build_engine(tmp_path, _numeric_looks_like_fixture()).generate()
        stats = yaml.safe_load(
            (tmp_path / "primary" / "fixture" / "type_probe" / "statistics.yaml").read_text(),
        )

        assert stats["columns"]["serial_label"]["classification"] == "text"
        assert stats["columns"]["serial_label"]["inferred"]["looks_like"] == "numeric_string"

    def test_a_numeric_column_matching_a_higher_priority_pattern_still_publishes_it(
        self,
        tmp_path: Path,
    ) -> None:
        """Suppression, not fall-through: `imei` already outranks `numeric_string`."""

        _build_engine(tmp_path, _numeric_looks_like_fixture()).generate()
        stats = yaml.safe_load(
            (tmp_path / "primary" / "fixture" / "type_probe" / "statistics.yaml").read_text(),
        )

        assert stats["columns"]["device_imei"]["classification"] == "categorical"
        assert stats["columns"]["device_imei"]["inferred"]["looks_like"] == "imei"

    def test_the_print_conforms(self, tmp_path: Path) -> None:
        """Absence of `looks_like` on a numeric column is licensed, not a conformance finding."""

        _build_engine(tmp_path, _numeric_looks_like_fixture()).generate()
        issues = validate_print(tmp_path / "primary")
        errors = [i for i in issues if i.severity == "error"]
        assert errors == [], "Conformance violations:\n" + "\n".join(str(e) for e in errors)


class TestUnsupportedFallthroughFollowsMeasurement:
    """A type no list names classifies by whether the adapter measured a cardinality."""

    def test_mysql_unsigned_integer_is_numeric_end_to_end(self, tmp_path: Path) -> None:
        result = _build_engine(tmp_path, _unnamed_type_fixture()).generate()
        assert result.summary.failed == 0

        stats = yaml.safe_load(
            (tmp_path / "primary" / "public" / "viability_check" / "statistics.yaml").read_text(),
        )
        assert stats["columns"]["viability_pct"]["classification"] == "numeric"
        assert "cardinality" in stats["columns"]["viability_pct"]

    def test_a_measured_column_of_an_unnamed_type_is_text(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _unnamed_type_fixture()).generate()
        stats = yaml.safe_load(
            (tmp_path / "primary" / "public" / "viability_check" / "statistics.yaml").read_text(),
        )
        assert stats["columns"]["address"]["classification"] == "text"
        assert "values" in stats["columns"]["address"]

    def test_an_unmeasured_column_of_an_unnamed_type_stays_unsupported(
        self,
        tmp_path: Path,
    ) -> None:
        _build_engine(tmp_path, _unnamed_type_fixture()).generate()
        stats = yaml.safe_load(
            (tmp_path / "primary" / "public" / "viability_check" / "statistics.yaml").read_text(),
        )
        assert stats["columns"]["location"]["classification"] == "unsupported"
        assert "cardinality" not in stats["columns"]["location"]

    def test_the_print_conforms(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _unnamed_type_fixture()).generate()
        issues = validate_print(tmp_path / "primary")
        errors = [i for i in issues if i.severity == "error"]
        assert errors == [], "Conformance violations:\n" + "\n".join(
            f"  {e.code} at {e.path}: {e.detail}" for e in errors
        )


def _unnamed_type_fixture() -> dict[str, MockTable]:
    """One table with three columns no `_UNSUPPORTED_TYPES` list names by type alone.

    `viability_pct` matches `numeric` once `base_type` strips `unsigned`; the two `inet`
    columns differ only in whether the adapter measured a cardinality.
    """

    return {
        "public.viability_check": MockTable(
            type="table",
            namespace_path=("public", "viability_check"),
            ddl=(
                "CREATE TABLE public.viability_check (viability_pct bigint unsigned, address inet, "
                "location inet);\n"
            ),
            columns=[
                ColumnMeta(
                    name="viability_pct",
                    sql_type="bigint unsigned",
                    nullable=False,
                    default=None,
                    ordinal=1,
                ),
                ColumnMeta(
                    name="address",
                    sql_type="inet",
                    nullable=False,
                    default=None,
                    ordinal=2,
                ),
                ColumnMeta(
                    name="location",
                    sql_type="inet",
                    nullable=True,
                    default=None,
                    ordinal=3,
                ),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "viability_pct": ColumnStats(
                    sql_type="bigint unsigned",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=60,
                    cardinality_ratio=0.6,
                    cardinality_method="exact",
                    distribution="uniform",
                    frequencies=Frequencies(top=2, bottom=1, listed=60, total=80),
                    range=Range(min=1, max=1000),
                    percentiles={"p50": 500},
                    mean=480.0,
                    sum=28800.0,
                    zero_count=0,
                    negative_count=0,
                    quantized_count=0,
                    values=(ValueCount(value=500, count=2), ValueCount(value=600, count=1)),
                ),
                "address": ColumnStats(
                    sql_type="inet",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=60,
                    cardinality_ratio=0.6,
                    cardinality_method="exact",
                    values=(ValueCount(value="10.0.0.1", count=1),),
                    values_coverage=0.016667,
                    distribution="uniform",
                    empty_count=0,
                    length=Length(min=8, max=8, avg=8.0, p95=8.0),
                ),
                "location": ColumnStats(
                    sql_type="inet",
                    nullable=True,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=None,
                    cardinality_ratio=None,
                    cardinality_method=None,
                ),
            },
            samples={},
            row_count=100,
        ),
    }


def _near_unique_fixture() -> dict[str, MockTable]:
    """One table whose key column clears the SPEC 4.2 threshold with a duplicate.

    10000 scanned, 9999 distinct and exact: the ratio sets `candidate_key`, yet a value repeats.
    """

    return {
        "public.curator": MockTable(
            type="table",
            namespace_path=("public", "curator"),
            ddl="CREATE TABLE public.curator (id uuid PRIMARY KEY);\n",
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
                    cardinality=9999,
                    cardinality_ratio=0.9999,
                    cardinality_method="exact",
                ),
            },
            samples={"id": [f"00000000-0000-7000-8000-{i:012d}" for i in range(20)]},
            row_count=10000,
        ),
    }


def _epoch_fixture() -> dict[str, MockTable]:
    """One table exercising both `epoch_unit` evidence rules and their negatives.

    A `numeric` column takes the bounds rule, which reads Phase B's own `range` and draws no
    sample; a `text` one takes the per-value rule over the sample `looks_like` reads.
    `epoch_dropped` is `numeric` under a `drop` rule, so the unit survives redaction.
    """

    def _numeric_stats(lo: int, hi: int) -> ColumnStats:
        return ColumnStats(
            sql_type="bigint",
            nullable=False,
            null_count=0,
            null_rate=0.0,
            cardinality=60,
            cardinality_ratio=0.6,
            cardinality_method="exact",
            distribution="uniform",
            range=Range(min=lo, max=hi),
            percentiles={"p50": (lo + hi) // 2},
            mean=(lo + hi) / 2,
            sum=float((lo + hi) // 2 * 60),
            zero_count=0,
            negative_count=0,
            quantized_count=0,
        )

    # Consecutive epoch seconds from 1704067201, skipping 1704067219 and 1704067227: both
    # satisfy isbn's check digit, splitting the looks_like vote below the 95% threshold.
    epoch_text_samples = [
        str(v) for v in range(1704067201, 1704067233) if v not in (1704067219, 1704067227)
    ]

    return {
        "public.viability_check": MockTable(
            type="table",
            namespace_path=("public", "viability_check"),
            ddl="CREATE TABLE public.viability_check (epoch_seconds bigint);\n",
            columns=[
                ColumnMeta(
                    name="epoch_seconds",
                    sql_type="bigint",
                    nullable=False,
                    default=None,
                    ordinal=1,
                ),
                ColumnMeta(
                    name="epoch_millis",
                    sql_type="bigint",
                    nullable=False,
                    default=None,
                    ordinal=2,
                ),
                ColumnMeta(
                    name="ordinary_count",
                    sql_type="bigint",
                    nullable=False,
                    default=None,
                    ordinal=3,
                ),
                ColumnMeta(
                    name="epoch_text",
                    sql_type="varchar",
                    nullable=False,
                    default=None,
                    ordinal=4,
                ),
                ColumnMeta(
                    name="epoch_dropped",
                    sql_type="bigint",
                    nullable=False,
                    default=None,
                    ordinal=5,
                ),
                ColumnMeta(
                    name="no_shape",
                    sql_type="varchar",
                    nullable=False,
                    default=None,
                    ordinal=6,
                ),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "epoch_seconds": _numeric_stats(1704067200, 1786492800),
                "epoch_millis": _numeric_stats(1704067200000, 1786492800000),
                "ordinary_count": _numeric_stats(0, 5000),
                "epoch_text": ColumnStats(
                    sql_type="varchar",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=60,
                    cardinality_ratio=0.6,
                    cardinality_method="exact",
                    values=(ValueCount(value="1704067200", count=1),),
                    values_coverage=0.016667,
                    distribution="uniform",
                    empty_count=0,
                    length=Length(min=10, max=10, avg=10.0, p95=10.0),
                ),
                "epoch_dropped": _numeric_stats(1704067200, 1786492800),
                # Sampled as `text`, but its draw clears no pattern and no near-miss floor
                # either (SPEC 4.1.3): evidence, no verdict, no candidate.
                "no_shape": ColumnStats(
                    sql_type="varchar",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=30,
                    cardinality_ratio=0.3,
                    cardinality_method="exact",
                    values=tuple(
                        ValueCount(value=f"noshape-value-{i}", count=1) for i in range(30)
                    ),
                    values_coverage=0.3,
                    distribution="uniform",
                    empty_count=0,
                    length=Length(min=15, max=16, avg=15.666667, p95=16.0),
                ),
            },
            samples={
                "epoch_text": epoch_text_samples,
                "no_shape": [f"noshape-value-{i}" for i in range(30)],
            },
            row_count=100,
        ),
    }


def _misfiled_datum_fixture() -> dict[str, MockTable]:
    """SPEC 4.1.5's `numeric`/`temporal` exclusion: a misfiled datum for each of six shapes.

    Every `samples` entry holds real shape-matching values, so a gate that failed open would
    find something for `looks_like` to detect rather than an empty sample.
    """

    def _numeric(sql_type: str, lo: int, hi: int) -> ColumnStats:
        mid = (lo + hi) // 2

        return ColumnStats(
            sql_type=sql_type,
            nullable=False,
            null_count=0,
            null_rate=0.0,
            cardinality=60,
            cardinality_ratio=0.6,
            cardinality_method="exact",
            distribution="uniform",
            frequencies=Frequencies(top=2, bottom=1, listed=60, total=80),
            range=Range(min=lo, max=hi),
            percentiles={"p50": mid},
            mean=float(mid),
            sum=float(mid * 60),
            zero_count=0,
            negative_count=0,
            quantized_count=0,
            values=(ValueCount(value=mid, count=2), ValueCount(value=hi, count=1)),
        )

    return {
        "public.misfiled_datum": MockTable(
            type="table",
            namespace_path=("public", "misfiled_datum"),
            ddl=(
                "CREATE TABLE public.misfiled_datum (pan bigint, accession_number numeric, "
                "occurred_at bigint, date_of_birth date, viability_pct numeric, owner numeric);\n"
            ),
            columns=[
                ColumnMeta(name="pan", sql_type="bigint", nullable=False, default=None, ordinal=1),
                ColumnMeta(
                    name="accession_number",
                    sql_type="numeric",
                    nullable=False,
                    default=None,
                    ordinal=2,
                ),
                ColumnMeta(
                    name="occurred_at",
                    sql_type="bigint",
                    nullable=False,
                    default=None,
                    ordinal=3,
                ),
                ColumnMeta(
                    name="date_of_birth",
                    sql_type="date",
                    nullable=False,
                    default=None,
                    ordinal=4,
                ),
                ColumnMeta(
                    name="viability_pct",
                    sql_type="numeric",
                    nullable=False,
                    default=None,
                    ordinal=5,
                ),
                ColumnMeta(
                    name="owner",
                    sql_type="numeric",
                    nullable=False,
                    default=None,
                    ordinal=6,
                ),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "pan": _numeric("bigint", 4111111111111111, 5500005555555559),
                "accession_number": _numeric("numeric", 100000000, 999999999),
                "occurred_at": _numeric("bigint", 1704067200, 1786492800),
                "date_of_birth": ColumnStats(
                    sql_type="date",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=60,
                    cardinality_ratio=0.6,
                    cardinality_method="exact",
                    range=Range(min="1960-01-01", max="2005-01-01", span_days=16437),
                    percentiles={"p50": "1980-04-02"},
                    values=(
                        ValueCount(value="1980-04-02", count=2),
                        ValueCount(value="2005-01-01", count=1),
                    ),
                    distribution="uniform",
                    frequencies=Frequencies(top=2, bottom=1, listed=60, total=80),
                ),
                "viability_pct": _numeric("numeric", 1999, 24500),
                "owner": _numeric("numeric", 1, 3),
            },
            samples={
                "pan": ["4111111111111111", "5500005555555559"],
                "accession_number": ["123456789", "987654321"],
                "occurred_at": ["1755000000"],
                "date_of_birth": ["1980-04-02", "1960-01-01"],
                "viability_pct": ["19.99", "245.00"],
                "owner": ["1", "2", "3"],
            },
            row_count=100,
        ),
    }


class _CountingSampleAdapter(MockAdapter):
    """Records every column `sample_values` was called for."""

    def __init__(self, fixture: dict[str, MockTable]) -> None:
        super().__init__(fixture)
        self.sampled_columns: list[str] = []

    def sample_values(
        self,
        fqn: str,
        column: str,
        n: int,
        scope: TableScope | None = None,
    ) -> list[Any]:
        self.sampled_columns.append(f"{fqn}.{column}")

        return super().sample_values(fqn, column, n, scope)


class TestNumericAndTemporalStayUnsampled:
    """SPEC 4.1.5/4.4.3: `numeric`/`temporal` never draw a sample, name evidence or nothing."""

    def _generate(self, tmp_path: Path) -> tuple[dict[str, Any], list[str]]:
        adapter = _CountingSampleAdapter(_misfiled_datum_fixture())
        conn = _conn_config(tmp_path)
        Engine(adapter, conn, tmp_path).generate()
        payload = yaml.safe_load(
            (tmp_path / "primary" / "public" / "misfiled_datum" / "statistics.yaml").read_text(),
        )

        return payload["columns"], adapter.sampled_columns

    def test_no_looks_like_is_ever_reported(self, tmp_path: Path) -> None:
        columns, _ = self._generate(tmp_path)

        for name in ("pan", "accession_number", "occurred_at", "date_of_birth", "viability_pct"):
            assert "looks_like" not in (columns[name].get("inferred") or {})

    def test_no_sample_is_drawn_for_any_of_the_six_columns(self, tmp_path: Path) -> None:
        _, sampled = self._generate(tmp_path)

        assert sampled == []

    def test_a_weak_name_token_gets_no_sensitivity_without_a_sample(self, tmp_path: Path) -> None:
        """`_share([])` is 0.0 - the corroborated path can never fire on `numeric`."""

        columns, _ = self._generate(tmp_path)

        assert "sensitivity" not in (columns["owner"].get("inferred") or {})

    def test_date_of_birth_is_flagged_by_name_alone(self, tmp_path: Path) -> None:
        """The unambiguous strong-token path needs no sample - unlike `owner`'s weak one."""

        columns, _ = self._generate(tmp_path)

        assert columns["date_of_birth"]["inferred"]["sensitivity"] == "date_of_birth"

    def test_conformance_still_forbids_looks_like_on_these_classifications(
        self,
        tmp_path: Path,
    ) -> None:
        adapter = _CountingSampleAdapter(_misfiled_datum_fixture())
        Engine(adapter, _conn_config(tmp_path), tmp_path).generate()
        errors = [i for i in validate_print(tmp_path / "primary") if i.severity == "error"]

        assert errors == []


class TestEpochUnit:
    """SPEC 4.5: an integer that is an instant says so, on either evidence rule."""

    def _stats(self, tmp_path: Path, conn=None) -> dict[str, Any]:
        conn = conn or _conn_config(tmp_path)
        Engine(MockAdapter(_epoch_fixture()), conn, tmp_path).generate()

        return yaml.safe_load(
            (tmp_path / "primary" / "public" / "viability_check" / "statistics.yaml").read_text(),
        )["columns"]

    def test_epoch_seconds_bounds_rule(self, tmp_path: Path) -> None:
        stats = self._stats(tmp_path)
        assert stats["epoch_seconds"]["inferred"]["epoch_unit"] == "seconds"
        assert stats["epoch_seconds"]["range"] == {"min": 1704067200, "max": 1786492800}

    def test_epoch_millis_bounds_rule(self, tmp_path: Path) -> None:
        stats = self._stats(tmp_path)
        assert stats["epoch_millis"]["inferred"]["epoch_unit"] == "milliseconds"

    def test_ordinary_numeric_column_carries_no_epoch_unit(self, tmp_path: Path) -> None:
        stats = self._stats(tmp_path)
        assert "epoch_unit" not in stats["ordinary_count"].get("inferred", {})

    def test_a_bounds_derived_epoch_unit_carries_no_looks_like_evidence(
        self,
        tmp_path: Path,
    ) -> None:
        """`epoch_seconds` is `numeric` - no draw ever happens for it to describe."""

        stats = self._stats(tmp_path)
        inferred = stats["epoch_seconds"]["inferred"]

        assert "sampled" not in inferred
        assert "matched" not in inferred

    def test_a_sampled_column_that_clears_nothing_carries_no_inferred_container(
        self,
        tmp_path: Path,
    ) -> None:
        """`no_shape` is sampled but reaches no verdict; evidence rides only beside a
        published `looks_like`.
        """

        stats = self._stats(tmp_path)

        assert "inferred" not in stats["no_shape"]

    def test_epoch_text_per_value_rule(self, tmp_path: Path) -> None:
        stats = self._stats(tmp_path)
        inferred = stats["epoch_text"]["inferred"]
        assert inferred["epoch_unit"] == "seconds"
        assert inferred["looks_like"] == "numeric_string"

    def test_a_dropped_epoch_column_still_reports_its_unit(self, tmp_path: Path) -> None:
        conn = replace(
            _conn_config(tmp_path),
            redact=(RedactRule(columns=("*.epoch_dropped",), with_="drop"),),
        )
        stats = self._stats(tmp_path, conn)
        assert stats["epoch_dropped"]["inferred"]["epoch_unit"] == "seconds"
        assert "range" not in stats["epoch_dropped"]
        assert "percentiles" not in stats["epoch_dropped"]


class _CuratorDdlFailingAdapter(MockAdapter):
    def extract_ddl(self, fqn: str) -> str:
        if fqn == "public.curator":
            raise RuntimeError("simulated")

        return super().extract_ddl(fqn)


class _NoColumnsAdapter(MockAdapter):
    """Catalog returns no columns - the shape a mis-cased catalog filter produces."""

    def introspect_columns(self, fqn: str) -> list[ColumnMeta]:
        return []


class TestZeroColumnInvariant:
    def test_table_with_no_columns_fails_instead_of_writing_empty_print(
        self,
        tmp_path: Path,
    ) -> None:
        conn = _conn_config(tmp_path)
        engine = Engine(_NoColumnsAdapter(_curator_fixture()), conn, tmp_path)

        result = engine.generate()

        statuses = {t.fqn: t.status for t in result.tables}
        assert statuses["public.curator"] == "failed"

        failure = next(t for t in result.tables if t.fqn == "public.curator")
        assert "no columns" in (failure.error or "")
        assert not (tmp_path / "primary" / "public" / "curator" / "statistics.yaml").exists()


class TestFaultIsolation:
    def test_one_table_failure_does_not_block_others(self, tmp_path: Path) -> None:
        fixture = _curator_fixture()
        broken_adapter = _CuratorDdlFailingAdapter(fixture)
        conn = _conn_config(tmp_path)
        engine = Engine(broken_adapter, conn, tmp_path)
        result = engine.generate()

        statuses = {t.fqn: t.status for t in result.tables}
        assert statuses["public.curator"] == "failed"
        assert statuses["public.herbarium"] == "ok"
        assert result.exit_code == EXIT_PARTIAL

    def test_a_table_narrowed_two_ways_fails_alone(self, tmp_path: Path) -> None:
        """Rules that settle one table with a filter and a sample say nothing about the rest."""

        conn = replace(
            _conn_config(tmp_path),
            rules=(
                RuleConfig(include=("public.curator",), filter="id IS NOT NULL"),
                RuleConfig(include=("public.curator",), sample=0.5),
            ),
        )
        result = Engine(MockAdapter(_curator_fixture()), conn, tmp_path).generate()

        statuses = {t.fqn: t.status for t in result.tables}
        assert statuses == {"public.curator": "failed", "public.herbarium": "ok"}
        assert result.exit_code == EXIT_PARTIAL

        failure = next(t for t in result.tables if t.status == "failed")
        assert "rules[0]" in (failure.error or "")
        assert "rules[1]" in (failure.error or "")


class _IncoherentSampleAdapter(MockAdapter):
    """Declares its fallback incoherent, like Snowflake/MySQL/ClickHouse/Redshift/BigQuery."""

    SAMPLE_FALLBACK_COHERENT = False


class _RefusingMaterializeAdapter(_IncoherentSampleAdapter):
    """Also refuses the copy itself - the write-refused path, not the config-off one."""

    def materialize_scope(self, fqn: str, scope: TableScope) -> TableScope:
        raise RuntimeError("simulated write refusal")


class TestSampleFallbackCoherence:
    """An adapter whose per-statement fallback cannot be seeded refuses the table rather than
    measuring it over rows that would differ between statements.
    """

    def test_materialize_sample_false_refuses_an_incoherent_adapter(
        self,
        tmp_path: Path,
    ) -> None:
        conn = replace(
            _conn_config(tmp_path),
            materialize_sample=False,
            rules=(RuleConfig(include=("public.curator",), sample=0.5),),
        )
        result = Engine(_IncoherentSampleAdapter(_curator_fixture()), conn, tmp_path).generate()

        assert {t.fqn: t.status for t in result.tables} == {
            "public.curator": "failed",
            "public.herbarium": "ok",
        }

        failure = next(t for t in result.tables if t.fqn == "public.curator")
        assert "materialize_sample" in (failure.error or "")

    def test_a_refused_write_names_the_underlying_cause(self, tmp_path: Path) -> None:
        conn = replace(
            _conn_config(tmp_path),
            rules=(RuleConfig(include=("public.curator",), sample=0.5),),
        )
        result = Engine(_RefusingMaterializeAdapter(_curator_fixture()), conn, tmp_path).generate()

        assert {t.fqn: t.status for t in result.tables} == {
            "public.curator": "failed",
            "public.herbarium": "ok",
        }

        failure = next(t for t in result.tables if t.fqn == "public.curator")
        assert "simulated write refusal" in (failure.error or "")

    def test_a_coherent_adapter_is_unaffected_by_materialize_sample_false(
        self,
        tmp_path: Path,
    ) -> None:
        conn = replace(
            _conn_config(tmp_path),
            materialize_sample=False,
            rules=(RuleConfig(include=("public.curator",), sample=0.5),),
        )
        result = Engine(MockAdapter(_curator_fixture()), conn, tmp_path).generate()

        assert {t.status for t in result.tables} == {"ok"}

    def test_a_filter_scope_is_unaffected_on_an_incoherent_adapter(self, tmp_path: Path) -> None:
        conn = replace(
            _conn_config(tmp_path),
            materialize_sample=False,
            rules=(RuleConfig(include=("public.curator",), filter="id IS NOT NULL"),),
        )
        result = Engine(_IncoherentSampleAdapter(_curator_fixture()), conn, tmp_path).generate()

        assert {t.status for t in result.tables} == {"ok"}

    def test_an_unsampled_table_is_unaffected_on_an_incoherent_adapter(
        self,
        tmp_path: Path,
    ) -> None:
        conn = replace(_conn_config(tmp_path), materialize_sample=False)
        result = Engine(_IncoherentSampleAdapter(_curator_fixture()), conn, tmp_path).generate()

        assert {t.status for t in result.tables} == {"ok"}


class TestClassifierFaultIsolation:
    """A classifier fault costs one column's verdict, never the table (run-all-then-report)."""

    @staticmethod
    def _stats(tmp_path: Path, table: str) -> dict[str, Any]:
        return yaml.safe_load(
            (tmp_path / "primary" / "public" / table / "statistics.yaml").read_text(),
        )

    def test_a_raising_looks_like_detector_still_completes_the_table(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            orchestrator,
            "detect_with_evidence",
            lambda values: (_ for _ in ()).throw(ValueError("hostile value")),
        )
        result = _build_engine(tmp_path, _curator_fixture()).generate()

        statuses = {t.fqn: t.status for t in result.tables}
        assert statuses == {"public.curator": "ok", "public.herbarium": "ok"}
        assert result.summary.failed == 0

        curator_id = self._stats(tmp_path, "curator")["columns"]["id"]
        assert "looks_like" not in (curator_id.get("inferred") or {})
        # candidate_key does not depend on the raising detector and must survive.
        assert curator_id["inferred"]["candidate_key"] is True

    def test_a_raising_sensitivity_detector_still_completes_the_table(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            orchestrator,
            "detect_sensitivity",
            lambda physical_name, samples, looks_like: (_ for _ in ()).throw(
                ValueError("hostile value"),
            ),
        )
        result = _build_engine(tmp_path, _curator_fixture()).generate()

        statuses = {t.fqn: t.status for t in result.tables}
        assert statuses == {"public.curator": "ok", "public.herbarium": "ok"}
        assert result.summary.failed == 0

    def test_a_raising_bounds_epoch_unit_does_not_raise_out_of_assemble_stats(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The second seam: reached from `_assemble_stats` after Phase B, not `_detect_columns` -
        it cannot move earlier because it reads Phase B's own `range`."""

        monkeypatch.setattr(
            orchestrator,
            "bounds_epoch_unit",
            lambda lo, hi: (_ for _ in ()).throw(ValueError("hostile value")),
        )
        columns = [
            ColumnMeta(name="ts", sql_type="bigint", nullable=False, default=None, ordinal=1),
        ]
        stats = {
            "ts": ColumnStats(
                sql_type="bigint",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=100,
                cardinality_ratio=1.0,
                cardinality_method="exact",
                range=Range(min=1704067200, max=1786492800),
            ),
        }
        detected = {"ts": orchestrator._ColumnDetection(classification="numeric", inferred=None)}

        enriched = orchestrator._assemble_stats(
            "public.t",
            columns,
            stats,
            detected,
            frozenset(),
            "2026-01-01T00:00:00Z",
        )

        assert "ts" in enriched
        assert enriched["ts"].inferred is None


class TestFreshness:
    def test_skip_when_within_max_age(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()

        # Both tables are within the default max_age_days=7.
        result = _build_engine(tmp_path, _curator_fixture()).generate()
        statuses = {t.status for t in result.tables}
        assert statuses == {"skipped"}

    def test_force_bypasses_freshness(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()
        result = _build_engine(tmp_path, _curator_fixture()).generate(GenerateRequest(force=True))
        assert all(t.status == "ok" for t in result.tables)


class TestPerTableFreshnessSkip:
    """One age, two thresholds: a shared threshold passes just as well when the skip falls
    back to the connection value, so the two tables get different numbers.
    """

    @staticmethod
    def _age_manifest(manifest: Path, days: float) -> None:
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        stamp = (datetime.now(UTC) - timedelta(days=days)).isoformat()

        for entry in data["tables"].values():
            entry["profiled_at"] = stamp.replace("+00:00", "Z")

        manifest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    def test_one_table_reprofiles_while_the_other_skips(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()
        manifest = tmp_path / "primary" / "manifest.yaml"
        self._age_manifest(manifest, days=3.0)

        # curator is past its own 1 day; herbarium is well inside the 30.
        conn = replace(
            _conn_config(tmp_path, max_age_days=30),
            rules=(RuleConfig(include=("public.curator",), max_age_days=1),),
        )
        result = Engine(MockAdapter(_curator_fixture()), conn, tmp_path).generate()

        assert {t.fqn: t.status for t in result.tables} == {
            "public.curator": "ok",
            "public.herbarium": "skipped",
        }

    def test_force_still_reprofiles_both(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()
        self._age_manifest(tmp_path / "primary" / "manifest.yaml", days=3.0)

        conn = replace(
            _conn_config(tmp_path, max_age_days=30),
            rules=(RuleConfig(include=("public.curator",), max_age_days=1),),
        )
        result = Engine(MockAdapter(_curator_fixture()), conn, tmp_path).generate(
            GenerateRequest(force=True),
        )

        assert {t.status for t in result.tables} == {"ok"}


class _EstimateCountingAdapter(MockAdapter):
    """Records every catalog size read so "pays nothing when unused" is observable."""

    def __init__(self, fixture: dict[str, MockTable]) -> None:
        super().__init__(fixture)
        self.estimate_calls: list[str] = []

    def estimate_row_count(self, fqn: str) -> int | None:
        self.estimate_calls.append(fqn)

        return super().estimate_row_count(fqn)


class _EstimateFailingAdapter(MockAdapter):
    def estimate_row_count(self, fqn: str) -> int | None:
        if fqn == "public.curator":
            raise RuntimeError("simulated catalog failure")

        return super().estimate_row_count(fqn)


def _sized_fixture(curator: int | None, herbarium: int | None) -> dict[str, MockTable]:
    """The standard fixture with catalog estimates attached, `None` meaning unavailable."""

    fixture = _curator_fixture()
    fixture["public.curator"] = replace(fixture["public.curator"], row_count_estimate=curator)
    fixture["public.herbarium"] = replace(
        fixture["public.herbarium"],
        row_count_estimate=herbarium,
    )

    return fixture


def _incoherent_coverage_fixture() -> dict[str, MockTable]:
    """`herbarium_id`'s value list count exceeds its own non-null row total; coverage reads 1.0."""

    fixture = _curator_fixture()
    curator = fixture["public.curator"]
    herbarium_id = curator.stats["herbarium_id"]
    fixture["public.curator"] = replace(
        curator,
        stats={
            **curator.stats,
            "herbarium_id": replace(
                herbarium_id,
                values=(ValueCount(value="00000000-0000-7000-8000-000000000001", count=150),),
                values_coverage=1.0,
            ),
        },
    )

    return fixture


class TestIncoherentCoverageDetection:
    """A value list whose counts exceed the non-null rows it is drawn from is `bounded`."""

    def test_the_warning_names_the_table_and_column(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="dbprint.engine.orchestrator"):
            Engine(
                MockAdapter(_incoherent_coverage_fixture()),
                _conn_config(tmp_path),
                tmp_path,
            ).generate()

        assert "public.curator" in caplog.text
        assert "herbarium_id" in caplog.text

    def test_the_exit_code_is_unaffected(self, tmp_path: Path) -> None:
        """A warning is not a failure - the exit code matches the coherent fixture's."""

        coherent = _build_engine(tmp_path / "coherent", _curator_fixture()).generate()
        incoherent = Engine(
            MockAdapter(_incoherent_coverage_fixture()),
            _conn_config(tmp_path / "incoherent"),
            tmp_path / "incoherent",
        ).generate()

        assert incoherent.exit_code == coherent.exit_code

    def test_every_written_column_stays_within_the_schema_bound(self, tmp_path: Path) -> None:
        """Walks the generated print: `values_coverage` never leaves [0, 1], anywhere."""

        Engine(
            MockAdapter(_incoherent_coverage_fixture()),
            _conn_config(tmp_path),
            tmp_path,
        ).generate()

        checked = 0

        for stats_path in (tmp_path / "primary").rglob("statistics.yaml"):
            columns = yaml.safe_load(stats_path.read_text())["columns"]

            for name, col in columns.items():
                coverage = col.get("values_coverage")

                if coverage is not None:
                    assert 0 <= coverage <= 1, f"{stats_path}:{name} = {coverage}"
                    checked += 1

        assert checked > 0, "fixture produced no values_coverage field to check"

    def test_a_coherent_fixture_warns_about_nothing(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="dbprint.engine.orchestrator"):
            _build_engine(tmp_path, _curator_fixture()).generate()

        assert "exceed the" not in caplog.text

    def test_the_affected_column_is_marked_and_its_siblings_are_not(self, tmp_path: Path) -> None:
        """The marker reaches the bytes on disk, keyed per column, not per file."""

        Engine(
            MockAdapter(_incoherent_coverage_fixture()),
            _conn_config(tmp_path),
            tmp_path,
        ).generate()

        curator = yaml.safe_load((tmp_path / "primary/public/curator/statistics.yaml").read_text())
        herbarium = yaml.safe_load(
            (tmp_path / "primary/public/herbarium/statistics.yaml").read_text(),
        )

        assert curator["columns"]["herbarium_id"]["values_coverage_method"] == "bounded"
        # curator.id's list is truncated (long_tail); herbarium.id's is exhaustive and
        # agrees with its own rows_scanned - neither sibling gets `bounded`.
        assert "values_coverage_method" not in curator["columns"]["id"]
        assert herbarium["columns"]["id"]["values_coverage_method"] == "measured"

    def test_a_coherent_fixture_marks_only_its_exhaustive_columns(self, tmp_path: Path) -> None:
        """`bounded` never fires; a truncated list stays absent; an exhaustive one is `measured`."""

        _build_engine(tmp_path, _curator_fixture()).generate()

        curator = yaml.safe_load((tmp_path / "primary/public/curator/statistics.yaml").read_text())
        herbarium = yaml.safe_load(
            (tmp_path / "primary/public/herbarium/statistics.yaml").read_text(),
        )

        # curator.id and curator.herbarium_id both carry a truncated (long_tail) list.
        assert "values_coverage_method" not in curator["columns"]["id"]
        assert "values_coverage_method" not in curator["columns"]["herbarium_id"]
        # herbarium.id is exhaustive (values_coverage 1.0) and agrees with rows_scanned.
        assert herbarium["columns"]["id"]["values_coverage_method"] == "measured"


class TestForbiddenFieldGate:
    """A field SPEC 2.2.3 forbids for the stamped classification is dropped, with a warning."""

    def test_a_forbidden_field_is_dropped(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _drifted_numeric_fixture()).generate()
        col = yaml.safe_load(
            (tmp_path / "primary" / "public" / "drifted" / "statistics.yaml").read_text(),
        )["columns"]["viability_pct"]

        assert col["classification"] == "numeric"
        # `values` itself is legitimate on `numeric` (SPEC 2.2.3) and survives untouched;
        # `values_coverage` stays forbidden there and is what this gate actually drops.
        assert col["values"] == [{"value": "1", "count": 10}]
        assert "values_coverage" not in col

    def test_the_drop_is_warned_about(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="dbprint.engine.orchestrator"):
            _build_engine(tmp_path, _drifted_numeric_fixture()).generate()

        assert "public.drifted" in caplog.text
        assert "viability_pct" in caplog.text
        assert "values" in caplog.text

    def test_the_print_still_conforms(self, tmp_path: Path) -> None:
        """The gate is what makes it conform - the fields it removes are what would not."""

        _build_engine(tmp_path, _drifted_numeric_fixture()).generate()
        errors = [i for i in validate_print(tmp_path / "primary") if i.severity == "error"]
        assert errors == [], "Conformance violations:\n" + "\n".join(
            f"  {e.code} at {e.path}: {e.detail}" for e in errors
        )


def _drifted_numeric_fixture() -> dict[str, MockTable]:
    """A `numeric` column whose adapter also hands back `values_coverage` - `values` is included
    so the gate is shown dropping the one forbidden field, not the whole block.
    """

    return {
        "public.drifted": MockTable(
            type="table",
            namespace_path=("public", "drifted"),
            ddl="CREATE TABLE public.drifted (viability_pct numeric);\n",
            columns=[
                ColumnMeta(
                    name="viability_pct",
                    sql_type="numeric",
                    nullable=False,
                    default=None,
                    ordinal=1,
                ),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "viability_pct": ColumnStats(
                    sql_type="numeric",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=60,
                    cardinality_ratio=0.6,
                    cardinality_method="exact",
                    distribution="uniform",
                    frequencies=Frequencies(top=2, bottom=1, listed=60, total=80),
                    range=Range(min=1, max=100),
                    percentiles={"p50": 50},
                    mean=45.0,
                    sum=2700.0,
                    zero_count=0,
                    negative_count=0,
                    quantized_count=0,
                    values=(ValueCount(value="1", count=10),),
                    values_coverage=1.0,
                ),
            },
            samples={},
            row_count=100,
        ),
    }


class TestSizeConditionedRules:
    """A rule may select by size, which means the estimate is read before anything uses it."""

    @staticmethod
    def _scope_of(tmp_path: Path, table: str) -> dict[str, Any] | None:
        stats = yaml.safe_load(
            (tmp_path / "primary" / "public" / table / "statistics.yaml").read_text(),
        )

        return stats.get("scope")

    def test_only_the_table_over_the_bar_is_narrowed(self, tmp_path: Path) -> None:
        conn = replace(
            _conn_config(tmp_path),
            rules=(RuleConfig(min_rows=100_000, sample=0.01),),
        )
        Engine(
            MockAdapter(_sized_fixture(curator=280_421, herbarium=1000)),
            conn,
            tmp_path,
        ).generate()

        assert self._scope_of(tmp_path, "curator") is not None
        assert self._scope_of(tmp_path, "herbarium") is None

    def test_an_unavailable_estimate_takes_the_unnarrowed_path(self, tmp_path: Path) -> None:
        """Sampling degrades the artifact, so an unknown size is not assumed to clear the bar."""

        conn = replace(
            _conn_config(tmp_path),
            rules=(RuleConfig(min_rows=100_000, sample=0.01),),
        )
        Engine(
            MockAdapter(_sized_fixture(curator=None, herbarium=None)),
            conn,
            tmp_path,
        ).generate()

        assert self._scope_of(tmp_path, "curator") is None

    def test_an_unavailable_estimate_is_reported(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Declining to narrow a table the config named must not look like a non-match."""

        conn = replace(
            _conn_config(tmp_path),
            rules=(RuleConfig(min_rows=100_000, sample=0.01),),
        )

        with caplog.at_level(logging.WARNING, logger="dbprint.engine.orchestrator"):
            Engine(
                MockAdapter(_sized_fixture(curator=None, herbarium=None)),
                conn,
                tmp_path,
            ).generate()

        assert "public.curator" in caplog.text
        assert "min_rows" in caplog.text

    def test_a_table_no_size_rule_names_is_not_reported(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Views carry no catalog count; warning about every one buries the tables that matter."""

        conn = replace(
            _conn_config(tmp_path),
            rules=(RuleConfig(include=("public.curator",), min_rows=100_000, sample=0.01),),
        )

        with caplog.at_level(logging.WARNING, logger="dbprint.engine.orchestrator"):
            Engine(
                MockAdapter(_sized_fixture(curator=None, herbarium=None)),
                conn,
                tmp_path,
            ).generate()

        assert "public.curator" in caplog.text
        assert "public.herbarium" not in caplog.text

    def test_an_available_estimate_is_not_reported(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        conn = replace(
            _conn_config(tmp_path),
            rules=(RuleConfig(min_rows=100_000, sample=0.01),),
        )

        with caplog.at_level(logging.WARNING, logger="dbprint.engine.orchestrator"):
            Engine(
                MockAdapter(_sized_fixture(curator=280_421, herbarium=1000)),
                conn,
                tmp_path,
            ).generate()

        assert "min_rows" not in caplog.text

    def test_no_estimate_is_read_when_no_rule_carries_one(self, tmp_path: Path) -> None:
        adapter = _EstimateCountingAdapter(_sized_fixture(curator=280_421, herbarium=1000))
        conn = replace(_conn_config(tmp_path), rules=(RuleConfig(sample=0.01),))
        Engine(adapter, conn, tmp_path).generate()

        assert adapter.estimate_calls == []

    def test_the_estimate_is_read_once_per_table_when_a_rule_carries_one(
        self,
        tmp_path: Path,
    ) -> None:
        adapter = _EstimateCountingAdapter(_sized_fixture(curator=280_421, herbarium=1000))
        conn = replace(
            _conn_config(tmp_path),
            rules=(RuleConfig(min_rows=100_000, sample=0.01),),
        )
        Engine(adapter, conn, tmp_path).generate()

        assert sorted(adapter.estimate_calls) == ["public.curator", "public.herbarium"]

    def test_the_bar_gates_the_freshness_threshold_so_it_is_read_before_the_skip(
        self,
        tmp_path: Path,
    ) -> None:
        """`max_age_days` is decided after the estimate, or a size-gated override never fires.

        Both are three days old against a threshold of 30; only `curator` clears the size bar.
        """

        _build_engine(tmp_path, _curator_fixture()).generate()
        TestPerTableFreshnessSkip._age_manifest(tmp_path / "primary" / "manifest.yaml", days=3.0)

        conn = replace(
            _conn_config(tmp_path, max_age_days=30),
            rules=(RuleConfig(min_rows=100_000, max_age_days=1),),
        )
        result = Engine(
            MockAdapter(_sized_fixture(curator=280_421, herbarium=1000)),
            conn,
            tmp_path,
        ).generate()

        assert {t.fqn: t.status for t in result.tables} == {
            "public.curator": "ok",
            "public.herbarium": "skipped",
        }

    def test_a_raising_estimate_fails_that_table_alone(self, tmp_path: Path) -> None:
        """The pre-flight sits outside the guard around extraction, so it carries its own."""

        conn = replace(
            _conn_config(tmp_path),
            rules=(RuleConfig(min_rows=100_000, sample=0.01),),
        )
        result = Engine(
            _EstimateFailingAdapter(_sized_fixture(curator=280_421, herbarium=1000)),
            conn,
            tmp_path,
        ).generate()

        statuses = {t.fqn: t.status for t in result.tables}

        assert statuses == {"public.curator": "failed", "public.herbarium": "ok"}
        assert result.exit_code == EXIT_PARTIAL

    def test_a_failed_estimate_names_the_operation(self, tmp_path: Path) -> None:
        conn = replace(
            _conn_config(tmp_path),
            rules=(RuleConfig(min_rows=100_000, sample=0.01),),
        )
        result = Engine(
            _EstimateFailingAdapter(_sized_fixture(curator=280_421, herbarium=1000)),
            conn,
            tmp_path,
        ).generate()

        failure = next(t for t in result.tables if t.status == "failed")

        assert failure.error_operation == "estimate_row_count"

    def test_manifest_override_reflects_the_estimate_a_rule_matched_against(
        self,
        tmp_path: Path,
    ) -> None:
        """The catalog estimate and the later-profiled row count can disagree across
        `min_rows`, and `statistics_params` tracks the estimate the rule was gated on.
        """

        fixture = _sized_fixture(curator=50_000, herbarium=1000)
        fixture["public.curator"] = replace(fixture["public.curator"], row_count=200_000)
        conn = replace(
            _conn_config(tmp_path),
            rules=(RuleConfig(min_rows=100_000, statistics={"top_n_values": 5}),),
        )
        Engine(MockAdapter(fixture), conn, tmp_path).generate()

        manifest = yaml.safe_load((tmp_path / "primary" / "manifest.yaml").read_text())

        assert "statistics_params" not in manifest["tables"]["public.curator"]


class TestMaxRowsScannedCeiling:
    """A row-count ceiling narrows through the same `scope` block a `sample` rule does."""

    @staticmethod
    def _scope_of(tmp_path: Path, table: str) -> dict[str, Any] | None:
        stats = yaml.safe_load(
            (tmp_path / "primary" / "public" / table / "statistics.yaml").read_text(),
        )

        return stats.get("scope")

    def test_only_the_table_over_the_ceiling_is_narrowed(self, tmp_path: Path) -> None:
        conn = replace(_conn_config(tmp_path), max_rows_scanned=100_000)
        Engine(
            MockAdapter(_sized_fixture(curator=280_421, herbarium=1000)),
            conn,
            tmp_path,
        ).generate()

        assert self._scope_of(tmp_path, "curator") is not None
        assert self._scope_of(tmp_path, "herbarium") is None

    def test_an_unavailable_estimate_takes_the_unnarrowed_path(self, tmp_path: Path) -> None:
        conn = replace(_conn_config(tmp_path), max_rows_scanned=100_000)
        Engine(
            MockAdapter(_sized_fixture(curator=None, herbarium=None)),
            conn,
            tmp_path,
        ).generate()

        assert self._scope_of(tmp_path, "curator") is None

    def test_an_unavailable_estimate_is_reported(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        conn = replace(_conn_config(tmp_path), max_rows_scanned=100_000)

        with caplog.at_level(logging.WARNING, logger="dbprint.engine.orchestrator"):
            Engine(
                MockAdapter(_sized_fixture(curator=None, herbarium=None)),
                conn,
                tmp_path,
            ).generate()

        assert "public.curator" in caplog.text
        assert "max_rows_scanned" in caplog.text

    def test_a_ceiling_yields_to_a_filter_and_the_run_warns(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The predicate already narrows, so the ceiling stands down rather than colliding."""

        conn = replace(
            _conn_config(tmp_path),
            max_rows_scanned=100_000,
            rules=(RuleConfig(include=("public.curator",), filter="id IS NOT NULL"),),
        )

        with caplog.at_level(logging.WARNING, logger="dbprint.engine.orchestrator"):
            Engine(
                MockAdapter(_sized_fixture(curator=280_421, herbarium=1000)),
                conn,
                tmp_path,
            ).generate()

        scope = self._scope_of(tmp_path, "curator")

        assert scope is not None
        assert scope.get("filter") == "id IS NOT NULL"
        assert "sample" not in scope
        assert "public.curator" in caplog.text

    def test_no_estimate_is_read_when_no_ceiling_or_min_rows_is_set(self, tmp_path: Path) -> None:
        adapter = _EstimateCountingAdapter(_sized_fixture(curator=280_421, herbarium=1000))
        Engine(adapter, _conn_config(tmp_path), tmp_path).generate()

        assert adapter.estimate_calls == []

    @staticmethod
    def _with_a_view(fixture: dict[str, MockTable]) -> dict[str, MockTable]:
        fixture["public.active_curators_v"] = MockTable(
            type="view",
            namespace_path=("public", "active_curators_v"),
            ddl="CREATE VIEW public.active_curators_v AS SELECT id FROM public.curator;\n",
            columns=[
                ColumnMeta(name="id", sql_type="uuid", nullable=False, default=None, ordinal=1),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={},
            samples={},
        )

        return fixture

    def test_a_view_draws_no_estimate_under_a_connection_level_ceiling(
        self,
        tmp_path: Path,
    ) -> None:
        """A view is never queried (SPEC 2.2.15), so no size condition can govern it."""

        fixture = self._with_a_view(_sized_fixture(curator=280_421, herbarium=1000))
        adapter = _EstimateCountingAdapter(fixture)
        conn = replace(_conn_config(tmp_path), max_rows_scanned=100_000)
        Engine(adapter, conn, tmp_path).generate()

        assert "public.active_curators_v" not in adapter.estimate_calls

    def test_a_view_draws_no_warning_but_an_unread_table_still_does(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        fixture = self._with_a_view(_sized_fixture(curator=None, herbarium=1000))
        conn = replace(_conn_config(tmp_path), max_rows_scanned=100_000)

        with caplog.at_level(logging.WARNING, logger="dbprint.engine.orchestrator"):
            Engine(MockAdapter(fixture), conn, tmp_path).generate()

        assert "public.curator" in caplog.text
        assert "public.active_curators_v" not in caplog.text

    @staticmethod
    def _with_a_matview(fixture: dict[str, MockTable]) -> dict[str, MockTable]:
        fixture["public.germination_by_taxon_mv"] = MockTable(
            type="matview",
            namespace_path=("public", "germination_by_taxon_mv"),
            ddl="CREATE MATERIALIZED VIEW public.germination_by_taxon_mv AS SELECT 1;\n",
            columns=[
                ColumnMeta(name="id", sql_type="uuid", nullable=False, default=None, ordinal=1),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={},
            samples={},
            row_count_estimate=None,
        )

        return fixture

    def test_a_matview_with_no_estimate_still_warns(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A matview can carry an estimate, so an absent one is a state that can change."""

        fixture = self._with_a_matview(_sized_fixture(curator=280_421, herbarium=1000))
        conn = replace(_conn_config(tmp_path), max_rows_scanned=100_000)

        with caplog.at_level(logging.WARNING, logger="dbprint.engine.orchestrator"):
            Engine(MockAdapter(fixture), conn, tmp_path).generate()

        assert "public.germination_by_taxon_mv" in caplog.text

    def test_a_view_under_a_ceiling_writes_the_same_catalog_only_statistics(
        self,
        tmp_path: Path,
    ) -> None:
        """The view's file is unaffected by the read it no longer takes."""

        without_ceiling = self._with_a_view(_sized_fixture(curator=280_421, herbarium=1000))
        Engine(MockAdapter(without_ceiling), _conn_config(tmp_path), tmp_path).generate()
        baseline = tmp_path / "primary" / "public" / "active_curators_v" / "statistics.yaml"
        baseline_text = normalize_instants(baseline.read_text())

        shutil.rmtree(tmp_path / "primary")
        with_ceiling = self._with_a_view(_sized_fixture(curator=280_421, herbarium=1000))
        conn = replace(_conn_config(tmp_path), max_rows_scanned=100_000)
        Engine(MockAdapter(with_ceiling), conn, tmp_path).generate()

        assert normalize_instants(baseline.read_text()) == baseline_text


class TestRecordedFreshnessThreshold:
    """Every entry records the threshold that governed its table on this run (SPEC 2.5), so a
    consumer reads the producer's decision rather than re-deriving it from a moved config.
    """

    @staticmethod
    def _with_a_view(fixture: dict[str, MockTable]) -> dict[str, MockTable]:
        fixture["public.active_curators_v"] = MockTable(
            type="view",
            namespace_path=("public", "active_curators_v"),
            ddl="CREATE VIEW public.active_curators_v AS SELECT id FROM public.curator;\n",
            columns=[
                ColumnMeta(name="id", sql_type="uuid", nullable=False, default=None, ordinal=1),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={},
            samples={},
        )

        return fixture

    def test_each_entry_records_what_its_own_rules_resolved(self, tmp_path: Path) -> None:
        conn = replace(
            _conn_config(tmp_path, max_age_days=7),
            rules=(RuleConfig(include=("public.curator",), max_age_days=30),),
        )
        Engine(MockAdapter(_curator_fixture()), conn, tmp_path).generate()

        assert _thresholds(tmp_path / "primary" / "manifest.yaml") == {
            "public.curator": 30,
            "public.herbarium": 7,
        }

    def test_a_view_records_it_like_a_table(self, tmp_path: Path) -> None:
        """A threshold resolves by name, so a view has one whether or not it has statistics."""

        engine = _build_engine(tmp_path, self._with_a_view(_curator_fixture()))
        engine.generate()

        assert _thresholds(tmp_path / "primary" / "manifest.yaml")["public.active_curators_v"] == 7

    def test_a_skipped_table_records_the_freshly_resolved_threshold(self, tmp_path: Path) -> None:
        """The skip read the new number, so the entry advertising the old one would disagree."""

        _build_engine(tmp_path, _curator_fixture()).generate()
        manifest = tmp_path / "primary" / "manifest.yaml"
        profiled_before = _profiled_at(manifest)

        assert _thresholds(manifest) == {"public.curator": 7, "public.herbarium": 7}

        raised = replace(_conn_config(tmp_path), rules=(RuleConfig(max_age_days=30),))
        result = Engine(MockAdapter(_curator_fixture()), raised, tmp_path).generate()

        assert {t.status for t in result.tables} == {"skipped"}
        assert _thresholds(manifest) == {"public.curator": 30, "public.herbarium": 30}
        assert _profiled_at(manifest) == profiled_before

    def test_a_failed_table_records_it_too(self, tmp_path: Path) -> None:
        """It was judged stale under this number - that is what the entry states."""

        _build_engine(tmp_path, _curator_fixture()).generate()
        manifest = tmp_path / "primary" / "manifest.yaml"
        conn = replace(
            _conn_config(tmp_path),
            rules=(RuleConfig(include=("public.curator",), max_age_days=30),),
        )
        result = Engine(_CuratorDdlFailingAdapter(_curator_fixture()), conn, tmp_path).generate(
            GenerateRequest(force=True),
        )

        assert result.summary.failed == 1
        assert _thresholds(manifest)["public.curator"] == 30

    def test_an_out_of_scope_table_keeps_the_threshold_it_was_written_with(
        self,
        tmp_path: Path,
    ) -> None:
        """This run never evaluated it, so it has nothing truer to say about it."""

        _build_engine(tmp_path, _curator_fixture()).generate()
        manifest = tmp_path / "primary" / "manifest.yaml"

        narrowed = replace(_conn_config(tmp_path), rules=(RuleConfig(max_age_days=30),))
        Engine(MockAdapter(_curator_fixture()), narrowed, tmp_path).generate(
            GenerateRequest(force=True, cli_include=("public.curator",)),
        )

        assert _thresholds(manifest) == {"public.curator": 30, "public.herbarium": 7}


class _NoQueryAgainstTheViewAdapter(MockAdapter):
    """Raises if any measurement method is called for the named view (SPEC 2.2.15).

    Only the guarded fqn is affected; a table sharing the run still measures normally.
    """

    def __init__(self, fixture: dict[str, MockTable], guarded_fqn: str) -> None:
        super().__init__(fixture)
        self._guarded_fqn = guarded_fqn

    def _refuse(self, fqn: str, method: str) -> None:
        if fqn == self._guarded_fqn:
            raise AssertionError(f"{method} issued a query against {fqn!r}, a catalog-only view")

    def compute_base_statistics(self, fqn, columns, config, scope=None):
        self._refuse(fqn, "compute_base_statistics")

        return super().compute_base_statistics(fqn, columns, config, scope)

    def compute_column_statistics(
        self,
        fqn,
        columns,
        config,
        counts,
        base,
        fk_source_columns,
        **kwargs,
    ):
        self._refuse(fqn, "compute_column_statistics")

        return super().compute_column_statistics(
            fqn,
            columns,
            config,
            counts,
            base,
            fk_source_columns,
            **kwargs,
        )

    def compute_null_patterns(self, fqn, columns, config, counts, base, scope=None):
        self._refuse(fqn, "compute_null_patterns")

        return super().compute_null_patterns(fqn, columns, config, counts, base, scope)

    def compute_key_sketch(self, fqn, column, sql_type, kind, k):
        self._refuse(fqn, "compute_key_sketch")

        return super().compute_key_sketch(fqn, column, sql_type, kind, k)

    def introspect_physical_layout(self, fqn):
        self._refuse(fqn, "introspect_physical_layout")

        return super().introspect_physical_layout(fqn)

    def introspect_unique_keys(self, fqn):
        self._refuse(fqn, "introspect_unique_keys")

        return super().introspect_unique_keys(fqn)


class TestCatalogOnlyViewStatistics:
    """A plain view's `statistics.yaml` is built from introspected columns alone (SPEC 2.2.15)."""

    @staticmethod
    def _view_fixture() -> dict[str, MockTable]:
        fixture = _curator_fixture()
        fixture["public.active_curators_v"] = MockTable(
            type="view",
            namespace_path=("public", "active_curators_v"),
            ddl=(
                "CREATE VIEW public.active_curators_v AS SELECT id, herbarium_id, "
                "display_name, matures_at FROM public.curator;\n"
            ),
            columns=[
                ColumnMeta(name="id", sql_type="uuid", nullable=False, default=None, ordinal=1),
                ColumnMeta(
                    name="herbarium_id",
                    sql_type="uuid",
                    nullable=True,
                    default=None,
                    ordinal=2,
                ),
                ColumnMeta(
                    name="display_name",
                    sql_type="character varying(120)",
                    nullable=True,
                    default=None,
                    ordinal=3,
                    physical_name="DisplayName",
                ),
                ColumnMeta(
                    name="matures_at",
                    sql_type="date",
                    nullable=False,
                    default=None,
                    ordinal=4,
                ),
            ],
            relationships=[
                ForeignKeyMeta(
                    column=("herbarium_id",),
                    target_table="public.herbarium",
                    target_column=("id",),
                    on_delete="CASCADE",
                    on_update="NO ACTION",
                    constraint_name="active_curators_v_herbarium_fk",
                ),
            ],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={},
            samples={},
        )

        return fixture

    def _statistics(self, tmp_path: Path) -> dict[str, Any]:
        path = tmp_path / "primary" / "public" / "active_curators_v" / "statistics.yaml"

        return yaml.safe_load(path.read_text())

    def test_the_marker_and_column_classifications(self, tmp_path: Path) -> None:
        adapter = _NoQueryAgainstTheViewAdapter(self._view_fixture(), "public.active_curators_v")
        Engine(adapter, _conn_config(tmp_path), tmp_path).generate()

        statistics = self._statistics(tmp_path)

        assert statistics["catalog_only"] is True
        assert statistics["grain"] == {"keys": []}
        assert "row_count" not in statistics
        assert "row_count_method" not in statistics

        columns = statistics["columns"]
        assert columns["id"]["classification"] == "text"  # uuid, no FK, unmeasured
        assert columns["herbarium_id"]["classification"] == "foreign_key_candidate"
        assert columns["display_name"]["classification"] == "text"
        assert columns["display_name"]["physical_name"] == "DisplayName"
        assert columns["matures_at"]["classification"] == "temporal"

    def test_no_column_carries_a_measured_field(self, tmp_path: Path) -> None:
        adapter = _NoQueryAgainstTheViewAdapter(self._view_fixture(), "public.active_curators_v")
        Engine(adapter, _conn_config(tmp_path), tmp_path).generate()

        allowed = {"sql_type", "nullable", "classification", "physical_name", "collation"}

        for col in self._statistics(tmp_path)["columns"].values():
            assert set(col) <= allowed

    def test_zero_queries_issued_against_the_view(self, tmp_path: Path) -> None:
        """The adapter raises if a measurement reaches the view; a clean run proves none did."""

        adapter = _NoQueryAgainstTheViewAdapter(self._view_fixture(), "public.active_curators_v")
        result = Engine(adapter, _conn_config(tmp_path), tmp_path).generate()

        assert result.summary.failed == 0

    def test_manifest_columns_count_matches_the_map(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, self._view_fixture()).generate()

        manifest = yaml.safe_load((tmp_path / "primary" / "manifest.yaml").read_text())
        entry = manifest["tables"]["public.active_curators_v"]

        assert entry["columns"] == len(self._statistics(tmp_path)["columns"])
        assert "statistics" in entry["artifacts"]

    def test_conformant(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, self._view_fixture()).generate()

        issues = validate_print(tmp_path / "primary")
        errors = [i for i in issues if i.severity == "error"]

        assert errors == []

    def _engine(self, tmp_path: Path) -> Engine:
        adapter = _NoQueryAgainstTheViewAdapter(self._view_fixture(), "public.active_curators_v")

        return Engine(adapter, _conn_config(tmp_path), tmp_path)

    def test_second_run_reports_no_change_for_the_view(self, tmp_path: Path) -> None:
        self._engine(tmp_path).generate()
        result = self._engine(tmp_path).generate()

        diff_path = tmp_path / "primary" / "diff.yaml"
        diff = yaml.safe_load(diff_path.read_text())
        view_events = [c for c in diff["changes"] if c.get("table") == "public.active_curators_v"]

        assert view_events == []
        assert result.summary.failed == 0


class TestAViewsRelationshipsAreAlwaysDeclared:
    """`_write_relationships_artifacts` writes and the manifest declares for every table,
    edgeless views included: an empty `refers_to` is a measurement, not nothing to declare.
    """

    @staticmethod
    def _edgeless_view_fixture() -> dict[str, MockTable]:
        fixture = _curator_fixture()
        fixture["public.active_curators_v"] = MockTable(
            type="view",
            namespace_path=("public", "active_curators_v"),
            ddl="CREATE VIEW public.active_curators_v AS SELECT id FROM public.curator;\n",
            columns=[
                ColumnMeta(name="id", sql_type="uuid", nullable=False, default=None, ordinal=1),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={},
            samples={},
        )

        return fixture

    def test_the_manifest_declares_relationships_for_an_edgeless_view(
        self,
        tmp_path: Path,
    ) -> None:
        _build_engine(tmp_path, self._edgeless_view_fixture()).generate()

        manifest = yaml.safe_load((tmp_path / "primary" / "manifest.yaml").read_text())
        entry = manifest["tables"]["public.active_curators_v"]

        assert "relationships" in entry["artifacts"]
        assert (tmp_path / "primary" / entry["path"] / "relationships.yaml").is_file()

    def test_the_declared_file_enters_conformance_validation(self, tmp_path: Path) -> None:
        """Content is only checked once declared - a corrupt file is now visible."""

        _build_engine(tmp_path, self._edgeless_view_fixture()).generate()
        print_root = tmp_path / "primary"
        rel_path = print_root / "public" / "active_curators_v" / "relationships.yaml"

        assert not any(i.code == "schema.invalid-yaml" for i in validate_print(print_root))

        rel_path.write_text("refers_to: [\n")

        issues = validate_print(print_root)
        assert any(
            i.code == "schema.invalid-yaml"
            and i.path == "public/active_curators_v/relationships.yaml"
            for i in issues
        )

    def test_the_edgeless_view_adds_no_reciprocity_issues(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, self._edgeless_view_fixture()).generate()

        issues = validate_print(tmp_path / "primary")

        assert not any(i.code.startswith("relationships.") for i in issues)


class TestTablesNotReExtracted:
    """A table the run did not re-read keeps its manifest entry and its edges: the manifest
    indexes what is on disk, and only a table the target stopped listing has gone.
    """

    def test_two_consecutive_runs_leave_the_manifest_byte_identical(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()
        manifest = tmp_path / "primary" / "manifest.yaml"
        before = manifest.read_text()

        _build_engine(tmp_path, _curator_fixture()).generate()

        assert manifest.read_text() == before

    def test_skip_only_run_reports_no_removals(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()
        result = _build_engine(tmp_path, _curator_fixture()).generate()

        assert result.diff_summary.tables_removed == 0
        diff = yaml.safe_load((tmp_path / "primary" / "diff.yaml").read_text())
        assert [c for c in diff["changes"] if c["kind"] == "table_removed"] == []

    def test_skip_only_run_exits_ok(self, tmp_path: Path) -> None:
        """A run that skipped every matched table as already-current is success, not staleness."""

        _build_engine(tmp_path, _curator_fixture()).generate()
        result = _build_engine(tmp_path, _curator_fixture()).generate()

        assert result.summary.skipped == 2
        assert result.summary.ok == 0
        assert result.exit_code == EXIT_OK

    def test_skipped_table_keeps_its_original_profiled_at(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()
        manifest = tmp_path / "primary" / "manifest.yaml"
        before = _profiled_at(manifest)

        _build_engine(tmp_path, _curator_fixture()).generate()

        # Advancing it renews freshness off a read that never happened, so it never reprofiles.
        assert _profiled_at(manifest) == before

    def test_partial_skip_keeps_every_table_and_refreshes_only_the_stale_one(
        self,
        tmp_path: Path,
    ) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()
        manifest = tmp_path / "primary" / "manifest.yaml"
        _age_manifest_entry(manifest, "public.curator", "2020-01-01T00:00:00Z")
        untouched = _profiled_at(manifest)["public.herbarium"]

        result = _build_engine(tmp_path, _curator_fixture()).generate()

        statuses = {t.fqn: t.status for t in result.tables}
        assert statuses == {"public.curator": "ok", "public.herbarium": "skipped"}

        after = _profiled_at(manifest)
        assert set(after) == {"public.curator", "public.herbarium"}
        assert after["public.curator"] != "2020-01-01T00:00:00Z"
        assert after["public.herbarium"] == untouched

    def test_failed_table_keeps_its_manifest_entry(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()
        manifest = tmp_path / "primary" / "manifest.yaml"
        before = _profiled_at(manifest)

        engine = Engine(
            _CuratorDdlFailingAdapter(_curator_fixture()),
            _conn_config(tmp_path),
            tmp_path,
        )
        result = engine.generate(GenerateRequest(force=True))

        assert result.summary.failed == 1
        after = _profiled_at(manifest)
        assert set(after) == set(before)
        assert after["public.curator"] == before["public.curator"]

    def test_selector_narrowed_run_keeps_out_of_scope_entries(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()
        manifest = tmp_path / "primary" / "manifest.yaml"
        before = _profiled_at(manifest)

        engine = Engine(MockAdapter(_curator_fixture()), _conn_config(tmp_path), tmp_path)
        result = engine.generate(GenerateRequest(force=True, cli_include=("public.curator",)))

        assert result.diff_summary.tables_removed == 0
        after = _profiled_at(manifest)
        assert set(after) == {"public.curator", "public.herbarium"}
        assert after["public.herbarium"] == before["public.herbarium"]

    def test_table_dropped_from_the_target_is_still_reported_removed(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()

        shrunk = _curator_fixture()
        del shrunk["public.herbarium"]
        result = _build_engine(tmp_path, shrunk).generate()

        assert result.diff_summary.tables_removed == 1
        assert set(_profiled_at(tmp_path / "primary" / "manifest.yaml")) == {"public.curator"}


class TestIncomingEdgesSurviveAPartialRun:
    """A table must not lose incoming references because some other table did not run: pass 2
    rewrites `relationships.yaml` in full from a graph over only the tables this run
    re-extracted.
    """

    HERBARIUM = ("primary", "public", "herbarium", "relationships.yaml")

    def test_failed_referencer_keeps_its_incoming_edge(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()
        rel = tmp_path.joinpath(*self.HERBARIUM)
        assert _referencers(rel) == ["public.curator"]

        engine = Engine(
            _CuratorDdlFailingAdapter(_curator_fixture()),
            _conn_config(tmp_path),
            tmp_path,
        )
        result = engine.generate(GenerateRequest(force=True))

        assert result.summary.failed == 1
        assert _referencers(rel) == ["public.curator"]

    def test_skipped_referencer_keeps_its_incoming_edge(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()
        manifest = tmp_path / "primary" / "manifest.yaml"

        # Only herbarium goes stale, so curator is skipped and contributes nothing to the graph.
        _age_manifest_entry(manifest, "public.herbarium", "2020-01-01T00:00:00Z")
        result = _build_engine(tmp_path, _curator_fixture()).generate()

        statuses = {t.fqn: t.status for t in result.tables}
        assert statuses == {"public.curator": "skipped", "public.herbarium": "ok"}
        assert _referencers(tmp_path.joinpath(*self.HERBARIUM)) == ["public.curator"]

    def test_removed_referencer_loses_its_incoming_edge(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()

        shrunk = _curator_fixture()
        del shrunk["public.curator"]
        _build_engine(tmp_path, shrunk).generate(GenerateRequest(force=True))

        assert _referencers(tmp_path.joinpath(*self.HERBARIUM)) == []

    def test_clean_run_resolves_edges_without_duplicating_them(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()
        _build_engine(tmp_path, _curator_fixture()).generate(GenerateRequest(force=True))

        assert _referencers(tmp_path.joinpath(*self.HERBARIUM)) == ["public.curator"]

    def test_partly_failed_run_does_not_break_reciprocity(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()

        engine = Engine(
            _CuratorDdlFailingAdapter(_curator_fixture()),
            _conn_config(tmp_path),
            tmp_path,
        )
        engine.generate(GenerateRequest(force=True))

        errors = [i for i in validate_print(tmp_path / "primary") if i.severity == "error"]
        assert errors == [], "Conformance violations:\n" + "\n".join(
            f"  {e.code} at {e.path}: {e.detail}" for e in errors
        )


class TestCarriedEntryRequiresItsPrintOnDisk:
    """Carrying a table forward vouches for a print that exists (SPEC 2.5, 2.3.6).

    An excluded table whose print is intact keeps its incoming edge; delete the print and
    the manifest stops indexing it, and the preserved edge stops being re-published.
    """

    HERBARIUM = ("primary", "public", "herbarium", "relationships.yaml")
    CURATOR_DIR = ("primary", "public", "curator")
    # force=True: a skipped-as-fresh table's file is never touched (per-table atomicity),
    # so `herbarium` must be re-extracted for its relationships.yaml to be rewritten.
    EXCLUDE_CURATOR = GenerateRequest(force=True, cli_exclude=("public.curator",))

    @staticmethod
    def _manifest_tables(tmp_path: Path) -> dict[str, Any]:
        return yaml.safe_load((tmp_path / "primary" / "manifest.yaml").read_text())["tables"]

    def test_excluded_table_with_its_print_intact_is_carried_unchanged(
        self,
        tmp_path: Path,
    ) -> None:
        """An out-of-scope referencer whose print exists keeps its incoming edge."""

        _build_engine(tmp_path, _curator_fixture()).generate()
        Engine(MockAdapter(_curator_fixture()), _conn_config(tmp_path), tmp_path).generate(
            self.EXCLUDE_CURATOR,
        )

        assert "public.curator" in self._manifest_tables(tmp_path)
        assert _referencers(tmp_path.joinpath(*self.HERBARIUM)) == ["public.curator"]
        assert (tmp_path.joinpath(*self.CURATOR_DIR) / "ddl.sql").is_file()

    def test_excluded_table_with_its_directory_deleted_is_dropped(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()
        carried = Engine(
            MockAdapter(_curator_fixture()),
            _conn_config(tmp_path),
            tmp_path,
        ).generate(self.EXCLUDE_CURATOR)
        shutil.rmtree(tmp_path.joinpath(*self.CURATOR_DIR))

        with caplog.at_level(logging.WARNING, logger="dbprint.engine.orchestrator"):
            dropped = Engine(
                MockAdapter(_curator_fixture()),
                _conn_config(tmp_path),
                tmp_path,
            ).generate(self.EXCLUDE_CURATOR)

        assert "public.curator" not in self._manifest_tables(tmp_path)
        assert "public.curator" in caplog.text
        assert _referencers(tmp_path.joinpath(*self.HERBARIUM)) == []
        diff = yaml.safe_load((tmp_path / "primary" / "diff.yaml").read_text())
        assert [c for c in diff["changes"] if c["kind"] == "table_removed"] == []
        assert dropped.exit_code == carried.exit_code

        errors = [i for i in validate_print(tmp_path / "primary") if i.severity == "error"]
        assert errors == [], "Conformance violations:\n" + "\n".join(
            f"  {e.code} at {e.path}: {e.detail}" for e in errors
        )

    def test_excluded_table_missing_one_artifact_is_dropped(self, tmp_path: Path) -> None:
        """The manifest cannot claim an artifact that is not there."""

        _build_engine(tmp_path, _curator_fixture()).generate()
        Engine(MockAdapter(_curator_fixture()), _conn_config(tmp_path), tmp_path).generate(
            self.EXCLUDE_CURATOR,
        )
        (tmp_path.joinpath(*self.CURATOR_DIR) / "ddl.sql").unlink()

        Engine(MockAdapter(_curator_fixture()), _conn_config(tmp_path), tmp_path).generate(
            self.EXCLUDE_CURATOR,
        )

        assert "public.curator" not in self._manifest_tables(tmp_path)

    def test_a_second_unchanged_run_is_byte_identical(self, tmp_path: Path) -> None:
        """A dropped table does not keep re-triggering the warning path. `herbarium` is
        force-reprocessed every call, so its fresh `profiled_at` reaches `relationships.yaml`
        and `manifest.yaml`'s entry (SPEC 2.5) - both compare through `normalize_instants`.
        """

        _build_engine(tmp_path, _curator_fixture()).generate()
        Engine(MockAdapter(_curator_fixture()), _conn_config(tmp_path), tmp_path).generate(
            self.EXCLUDE_CURATOR,
        )
        shutil.rmtree(tmp_path.joinpath(*self.CURATOR_DIR))
        Engine(MockAdapter(_curator_fixture()), _conn_config(tmp_path), tmp_path).generate(
            self.EXCLUDE_CURATOR,
        )
        manifest = tmp_path / "primary" / "manifest.yaml"
        relationships = tmp_path.joinpath(*self.HERBARIUM)
        manifest_before, relationships_before = manifest.read_text(), relationships.read_text()

        Engine(MockAdapter(_curator_fixture()), _conn_config(tmp_path), tmp_path).generate(
            self.EXCLUDE_CURATOR,
        )

        assert normalize_instants(manifest.read_text()) == normalize_instants(manifest_before)
        assert normalize_instants(relationships.read_text()) == normalize_instants(
            relationships_before,
        )

    def test_re_including_the_table_brings_back_the_entry_and_the_edge(
        self,
        tmp_path: Path,
    ) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()
        Engine(MockAdapter(_curator_fixture()), _conn_config(tmp_path), tmp_path).generate(
            self.EXCLUDE_CURATOR,
        )
        shutil.rmtree(tmp_path.joinpath(*self.CURATOR_DIR))
        Engine(MockAdapter(_curator_fixture()), _conn_config(tmp_path), tmp_path).generate(
            self.EXCLUDE_CURATOR,
        )

        _build_engine(tmp_path, _curator_fixture()).generate(GenerateRequest(force=True))

        assert "public.curator" in self._manifest_tables(tmp_path)
        assert _referencers(tmp_path.joinpath(*self.HERBARIUM)) == ["public.curator"]


class TestDryRun:
    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        engine = _build_engine(tmp_path, _curator_fixture())
        result = engine.generate(GenerateRequest(dry_run=True))
        assert result.summary.ok == 2
        prints_dir = tmp_path / "primary"
        assert not (prints_dir / "manifest.yaml").exists()
        assert not (prints_dir / "diff.yaml").exists()
        assert not (prints_dir / "public" / "curator" / "ddl.sql").exists()


class TestExitCodes:
    def test_exit_ok_on_clean_run_no_drift(self, tmp_path: Path) -> None:
        engine = _build_engine(tmp_path, _curator_fixture())
        engine.generate()
        result = _build_engine(tmp_path, _curator_fixture()).generate(GenerateRequest(force=True))
        assert result.exit_code == EXIT_OK

    def test_exit_drift_first_run(self, tmp_path: Path) -> None:
        result = _build_engine(tmp_path, _curator_fixture()).generate()
        assert result.exit_code == EXIT_DRIFT


class _AllDdlFailingAdapter(MockAdapter):
    """Every table fails identically, the shape a systemic driver fault takes."""

    def extract_ddl(self, fqn: str) -> str:
        raise RuntimeError("simulated systemic fault")


class TestTotalFailure:
    def test_all_tables_failing_exits_distinct_from_partial(self, tmp_path: Path) -> None:
        engine = Engine(_AllDdlFailingAdapter(_curator_fixture()), _conn_config(tmp_path), tmp_path)
        result = engine.generate()

        assert result.summary.ok == 0
        assert result.summary.failed == 2
        assert result.exit_code == EXIT_TOTAL_FAILURE
        assert result.exit_code != EXIT_PARTIAL

    def test_skipped_survivors_keep_the_run_partial(self, tmp_path: Path) -> None:
        """Per `_derive_generate_exit_code`: a skip still counts as usable output."""

        # Prime a baseline covering herbarium only: next run it is fresh and skipped,
        # while curator has no entry, is extracted, and fails.
        primed = Engine(
            MockAdapter(_curator_fixture()),
            _conn_config(tmp_path, include=("public.herbarium",)),
            tmp_path,
        )
        primed.generate()

        engine = Engine(
            _CuratorDdlFailingAdapter(_curator_fixture()),
            _conn_config(tmp_path),
            tmp_path,
        )
        result = engine.generate()

        statuses = {t.fqn: t.status for t in result.tables}
        assert statuses == {"public.curator": "failed", "public.herbarium": "skipped"}
        assert result.summary.ok == 0
        assert result.exit_code == EXIT_PARTIAL

    def test_zero_matched_tables_is_not_total_failure(self, tmp_path: Path) -> None:
        conn = _conn_config(tmp_path, include=("nope.*",))
        result = Engine(MockAdapter(_curator_fixture()), conn, tmp_path).generate()

        assert result.tables == ()
        assert result.exit_code == EXIT_OK


class TestFailFast:
    def test_abort_leaves_later_tables_unattempted(self, tmp_path: Path) -> None:
        engine = Engine(_AllDdlFailingAdapter(_curator_fixture()), _conn_config(tmp_path), tmp_path)
        result = engine.generate(GenerateRequest(fail_fast=True))

        assert len(result.tables) == 1
        assert result.not_attempted == 1
        assert result.exit_code == EXIT_TOTAL_FAILURE

    def test_abort_leaves_the_previous_manifest_untouched(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()
        manifest = tmp_path / "primary" / "manifest.yaml"
        before = manifest.read_text()

        engine = Engine(_AllDdlFailingAdapter(_curator_fixture()), _conn_config(tmp_path), tmp_path)
        engine.generate(GenerateRequest(fail_fast=True, force=True))

        assert manifest.read_text() == before

    def test_clean_run_is_unchanged_by_the_flag(self, tmp_path: Path) -> None:
        # Separate roots: a second run against the same one would see a baseline and lose drift.
        plain = _build_engine(tmp_path / "plain", _curator_fixture()).generate()
        fast = _build_engine(tmp_path / "fast", _curator_fixture()).generate(
            GenerateRequest(fail_fast=True),
        )

        assert [t.status for t in fast.tables] == [t.status for t in plain.tables]
        assert fast.exit_code == plain.exit_code
        assert fast.summary == plain.summary
        assert fast.not_attempted == 0

    def test_default_run_still_attempts_every_table(self, tmp_path: Path) -> None:
        engine = Engine(_AllDdlFailingAdapter(_curator_fixture()), _conn_config(tmp_path), tmp_path)
        result = engine.generate()

        assert len(result.tables) == 2
        assert result.not_attempted == 0

    def test_failure_on_the_last_table_is_not_a_truncated_run(self, tmp_path: Path) -> None:
        """No table was left unattempted, so the flag changed nothing observable."""

        fixture = _curator_fixture()

        # The test needs the table the loop reaches last, and `list_tables` preserves
        # fixture order rather than sorting, so ask the adapter for its own ordering.
        probe = MockAdapter(fixture)
        probe.connect()
        last_fqn = probe.list_tables(include=["*"], exclude=[])[-1].fqn
        probe.close()

        class _LastFails(MockAdapter):
            def extract_ddl(self, fqn: str) -> str:
                if fqn == last_fqn:
                    raise RuntimeError("simulated")

                return super().extract_ddl(fqn)

        engine = Engine(_LastFails(fixture), _conn_config(tmp_path), tmp_path)
        result = engine.generate(GenerateRequest(fail_fast=True))

        assert result.not_attempted == 0
        assert result.exit_code == EXIT_PARTIAL
        assert (tmp_path / "primary" / "manifest.yaml").is_file()

    def test_truncated_run_does_not_rewrite_relationships(self, tmp_path: Path) -> None:
        """Pass 2 resolves incoming edges from every table it saw, so a truncated run's graph
        would strip real `referenced_by` entries from prints the manifest still points at.
        """

        _build_engine(tmp_path, _curator_fixture()).generate()
        relationships = tmp_path / "primary" / "public" / "herbarium" / "relationships.yaml"
        before = relationships.read_text()

        engine = Engine(_AllDdlFailingAdapter(_curator_fixture()), _conn_config(tmp_path), tmp_path)
        result = engine.generate(GenerateRequest(fail_fast=True, force=True))

        assert result.not_attempted == 1
        assert relationships.read_text() == before

    def test_diff_path_ignores_fail_fast(self, tmp_path: Path) -> None:
        """compute_diff shares the pipeline; its fault tolerance must not shift."""

        _build_engine(tmp_path, _curator_fixture()).generate()

        engine = Engine(_AllDdlFailingAdapter(_curator_fixture()), _conn_config(tmp_path), tmp_path)
        result = engine.compute_diff()

        assert len(result.failed_tables) == 2


class TestSelectors:
    def test_exclude_filters_tables(self, tmp_path: Path) -> None:
        fixture = _curator_fixture()
        adapter = MockAdapter(fixture)
        conn = _conn_config(tmp_path, exclude=("public.herbarium",))
        engine = Engine(adapter, conn, tmp_path)
        result = engine.generate()
        fqns = {t.fqn for t in result.tables}
        assert fqns == {"public.curator"}


def _added_table_fixture() -> dict[str, MockTable]:
    """Variant adding a brand-new `public.curation_event` table to the baseline two."""

    base = _curator_fixture()
    base["public.curation_event"] = MockTable(
        type="table",
        namespace_path=("public", "curation_event"),
        ddl="CREATE TABLE public.curation_event (id uuid PRIMARY KEY);\n",
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
                cardinality=5,
                cardinality_ratio=1.0,
                cardinality_method="exact",
                inferred=Inferred(candidate_key=True),
            ),
        },
        samples={"id": [f"00000000-0000-7000-8000-{i:012d}" for i in range(5)]},
        row_count=5,
    )

    return base


def _curator_with_email_column_fixture() -> dict[str, MockTable]:
    """Variant adding an `email` column to public.curator on top of the baseline."""

    base = _curator_fixture()
    curator = base["public.curator"]
    new_columns = list(curator.columns) + [
        ColumnMeta(name="email", sql_type="varchar", nullable=True, default=None, ordinal=3),
    ]
    new_stats = dict(curator.stats)
    new_stats["email"] = ColumnStats(
        sql_type="varchar",
        nullable=True,
        null_count=0,
        null_rate=0.0,
        cardinality=100,
        cardinality_ratio=1.0,
        cardinality_method="exact",
    )
    base["public.curator"] = MockTable(
        type=curator.type,
        namespace_path=curator.namespace_path,
        ddl=curator.ddl,
        columns=new_columns,
        relationships=curator.relationships,
        indexes=curator.indexes,
        comments=curator.comments,
        stats=new_stats,
        samples=curator.samples,
        row_count=curator.row_count,
    )

    return base


def _curator_with_herbarium_id_int_fixture() -> dict[str, MockTable]:
    """Variant changing herbarium_id's sql_type from uuid -> bigint on public.curator."""

    base = _curator_fixture()
    curator = base["public.curator"]
    new_columns = [
        c
        if c.name != "herbarium_id"
        else ColumnMeta(
            name="herbarium_id",
            sql_type="bigint",
            nullable=c.nullable,
            default=c.default,
            ordinal=c.ordinal,
        )
        for c in curator.columns
    ]
    new_stats = dict(curator.stats)
    new_stats["herbarium_id"] = ColumnStats(
        sql_type="bigint",
        nullable=curator.stats["herbarium_id"].nullable,
        null_count=curator.stats["herbarium_id"].null_count,
        null_rate=curator.stats["herbarium_id"].null_rate,
        cardinality=curator.stats["herbarium_id"].cardinality,
        cardinality_ratio=curator.stats["herbarium_id"].cardinality_ratio,
        cardinality_method=curator.stats["herbarium_id"].cardinality_method,
    )
    base["public.curator"] = MockTable(
        type=curator.type,
        namespace_path=curator.namespace_path,
        ddl=curator.ddl,
        columns=new_columns,
        relationships=curator.relationships,
        indexes=curator.indexes,
        comments=curator.comments,
        stats=new_stats,
        samples=curator.samples,
        row_count=curator.row_count,
    )

    return base


def _curator_without_herbarium_id_fixture() -> dict[str, MockTable]:
    """Variant dropping the `herbarium_id` column (and its FK) from public.curator."""

    base = _curator_fixture()
    curator = base["public.curator"]
    new_columns = [c for c in curator.columns if c.name != "herbarium_id"]
    new_stats = {name: s for name, s in curator.stats.items() if name != "herbarium_id"}
    base["public.curator"] = MockTable(
        type=curator.type,
        namespace_path=curator.namespace_path,
        ddl=curator.ddl,
        columns=new_columns,
        relationships=[],
        indexes=curator.indexes,
        comments=curator.comments,
        stats=new_stats,
        samples=curator.samples,
        row_count=curator.row_count,
    )

    return base


def _curator_herbarium_id_not_null_fixture() -> dict[str, MockTable]:
    """Variant flipping herbarium_id from nullable to NOT NULL on public.curator."""

    base = _curator_fixture()
    curator = base["public.curator"]
    new_columns = [
        c
        if c.name != "herbarium_id"
        else ColumnMeta(
            name="herbarium_id",
            sql_type=c.sql_type,
            nullable=False,
            default=c.default,
            ordinal=c.ordinal,
        )
        for c in curator.columns
    ]
    old = curator.stats["herbarium_id"]
    new_stats = dict(curator.stats)
    new_stats["herbarium_id"] = ColumnStats(
        sql_type=old.sql_type,
        nullable=False,
        null_count=old.null_count,
        null_rate=old.null_rate,
        cardinality=old.cardinality,
        cardinality_ratio=old.cardinality_ratio,
        cardinality_method=old.cardinality_method,
    )
    base["public.curator"] = MockTable(
        type=curator.type,
        namespace_path=curator.namespace_path,
        ddl=curator.ddl,
        columns=new_columns,
        relationships=curator.relationships,
        indexes=curator.indexes,
        comments=curator.comments,
        stats=new_stats,
        samples=curator.samples,
        row_count=curator.row_count,
    )

    return base


def _curator_with_defaulted_col_fixture() -> dict[str, MockTable]:
    """Variant adding an `is_active boolean DEFAULT true` column to public.curator."""

    base = _curator_fixture()
    curator = base["public.curator"]
    new_columns = list(curator.columns) + [
        ColumnMeta(name="is_active", sql_type="boolean", nullable=False, default="true", ordinal=3),
    ]
    new_stats = dict(curator.stats)
    new_stats["is_active"] = ColumnStats(
        sql_type="boolean",
        nullable=False,
        null_count=0,
        null_rate=0.0,
        cardinality=2,
        cardinality_ratio=0.02,
        cardinality_method="exact",
    )
    base["public.curator"] = MockTable(
        type=curator.type,
        namespace_path=curator.namespace_path,
        ddl=curator.ddl,
        columns=new_columns,
        relationships=curator.relationships,
        indexes=curator.indexes,
        comments=curator.comments,
        stats=new_stats,
        samples=curator.samples,
        row_count=curator.row_count,
    )

    return base


def _curator_with_moved_herbarium_id_stats_fixture() -> dict[str, MockTable]:
    """Variant where herbarium_id's distinct count moved - the data changed, the schema did not."""

    base = _curator_fixture()
    curator = base["public.curator"]
    moved = replace(curator.stats["herbarium_id"], cardinality=35, cardinality_ratio=0.35)
    base["public.curator"] = replace(curator, stats={**curator.stats, "herbarium_id": moved})

    return base


def _curator_with_a_declared_grain_key_fixture() -> dict[str, MockTable]:
    """Variant declaring `id` a unique key that the baseline fixture leaves undeclared."""

    base = _curator_fixture()
    curator = base["public.curator"]
    base["public.curator"] = replace(
        curator,
        unique_keys=[UniqueKeyMeta(columns=("id",), primary=True)],
    )

    return base


def _curator_with_a_physical_layout_fixture() -> dict[str, MockTable]:
    """Variant declaring a clustering key that the baseline fixture leaves unclustered."""

    base = _curator_fixture()
    curator = base["public.curator"]
    base["public.curator"] = replace(
        curator,
        physical_layout=PhysicalLayout(
            mechanism="cluster",
            keys=(PhysicalLayoutKey(expression="id", column="id"),),
        ),
    )

    return base


def _curator_with_reshuffled_herbarium_id_values_fixture() -> dict[str, MockTable]:
    """Variant trading a row between two of herbarium_id's value entries; sum and coverage hold."""

    base = _curator_fixture()
    curator = base["public.curator"]
    reshuffled = replace(
        curator.stats["herbarium_id"],
        values=(
            ValueCount(value="00000000-0000-7000-8000-000000000001", count=10),
            ValueCount(value="00000000-0000-7000-8000-000000000002", count=7),
        ),
    )
    base["public.curator"] = replace(curator, stats={**curator.stats, "herbarium_id": reshuffled})

    return base


def _driver_typed_fixture() -> dict[str, MockTable]:
    """A table whose bounds and percentiles are driver objects (`datetime`, `Decimal`) that
    the artifact renders as strings: a projection built from the objects rather than the
    rendered artifact compares unequal to a committed print on every run.
    """

    return {
        "public.specimen_loan": MockTable(
            type="table",
            namespace_path=("public", "specimen_loan"),
            ddl="CREATE TABLE public.specimen_loan (withdrawn_at timestamptz, viability_pct numeric(12, 2));\n",
            columns=[
                ColumnMeta(
                    name="withdrawn_at",
                    sql_type="timestamp with time zone",
                    nullable=False,
                    default=None,
                    ordinal=1,
                ),
                ColumnMeta(
                    name="viability_pct",
                    sql_type="numeric(12, 2)",
                    nullable=False,
                    default=None,
                    ordinal=2,
                ),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "withdrawn_at": ColumnStats(
                    sql_type="timestamp with time zone",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=40,
                    cardinality_ratio=0.4,
                    cardinality_method="exact",
                    distribution="uniform",
                    range=Range(
                        min=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
                        max=datetime(2026, 3, 2, 9, 30, tzinfo=UTC),
                        span_days=60,
                    ),
                    percentiles={
                        "p01": datetime(2026, 1, 2, tzinfo=UTC),
                        "p50": datetime(2026, 2, 1, tzinfo=UTC),
                        "p99": datetime(2026, 3, 1, tzinfo=UTC),
                    },
                ),
                "viability_pct": ColumnStats(
                    sql_type="numeric(12, 2)",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=90,
                    cardinality_ratio=0.9,
                    cardinality_method="exact",
                    distribution="long_tail",
                    range=Range(min=Decimal("0.50"), max=Decimal("14999.99")),
                    percentiles={
                        "p01": Decimal("1.20"),
                        "p50": Decimal("42.00"),
                        "p99": Decimal("9100.75"),
                    },
                    mean=118.42,
                    sum=10658.0,
                    zero_count=0,
                    negative_count=0,
                    quantized_count=0,
                ),
            },
            samples={},
            row_count=100,
        ),
    }


def _unsupported_column_fixture() -> dict[str, MockTable]:
    """A `bytea` column, which SPEC 2.2.3 leaves without the cardinality trio."""

    return {
        "public.blobs": MockTable(
            type="table",
            namespace_path=("public", "blobs"),
            ddl="CREATE TABLE public.blobs (payload bytea);\n",
            columns=[
                ColumnMeta(
                    name="payload",
                    sql_type="bytea",
                    nullable=True,
                    default=None,
                    ordinal=1,
                ),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "payload": ColumnStats(
                    sql_type="bytea",
                    nullable=True,
                    null_count=2,
                    null_rate=0.02,
                    cardinality=None,
                    cardinality_ratio=None,
                    cardinality_method=None,
                ),
            },
            samples={},
            row_count=100,
        ),
    }


def _nan_bound_fixture() -> dict[str, MockTable]:
    """A `double precision` column whose bounds are NaN, which a real one may hold."""

    return {
        "public.viability_check": MockTable(
            type="table",
            namespace_path=("public", "viability_check"),
            ddl="CREATE TABLE public.viability_check (value double precision);\n",
            columns=[
                ColumnMeta(
                    name="value",
                    sql_type="double precision",
                    nullable=False,
                    default=None,
                    ordinal=1,
                ),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "value": ColumnStats(
                    sql_type="double precision",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=80,
                    cardinality_ratio=0.8,
                    cardinality_method="exact",
                    distribution="long_tail",
                    range=Range(min=float("nan"), max=float("nan")),
                    percentiles={"p50": float("nan")},
                    mean=float("nan"),
                    sum=float("nan"),
                    zero_count=0,
                    negative_count=0,
                    quantized_count=0,
                ),
            },
            samples={},
            row_count=100,
        ),
    }


def _curator_with_shaped_herbarium_id_samples_fixture() -> dict[str, MockTable]:
    """Variant whose herbarium_id sample carries a shape, so `looks_like` starts matching."""

    base = _curator_fixture()
    curator = base["public.curator"]
    base["public.curator"] = replace(
        curator,
        samples={
            **curator.samples,
            "herbarium_id": [f"00000000-0000-7000-8000-{i:012d}" for i in range(20)],
        },
    )

    return base


class TestComputeDiff:
    def test_clean_state_empty_changes(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()
        result = _build_engine(tmp_path, _curator_fixture()).compute_diff()
        assert isinstance(result, DiffResult)
        assert result.exit_code == 0
        assert result.diff["changes"] == []

    def test_drift_state_table_added_event(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()
        result = _build_engine(tmp_path, _added_table_fixture()).compute_diff()
        assert result.exit_code == 0
        added = [c for c in result.diff["changes"] if c["kind"] == "table_added"]
        assert any(c["table"] == "public.curation_event" for c in added)

    def test_drift_state_column_added_on_existing_table(self, tmp_path: Path) -> None:
        """Exercises the baseline.hydrate path synthesising columns from statistics.yaml."""

        _build_engine(tmp_path, _curator_fixture()).generate()
        result = _build_engine(tmp_path, _curator_with_email_column_fixture()).compute_diff()
        assert result.exit_code == 0
        added = [c for c in result.diff["changes"] if c["kind"] == "column_added"]
        assert any(c["table"] == "public.curator" and c["column"] == "email" for c in added)

    def test_drift_state_column_type_changed_on_existing_table(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()
        result = _build_engine(tmp_path, _curator_with_herbarium_id_int_fixture()).compute_diff()
        assert result.exit_code == 0
        type_changes = [c for c in result.diff["changes"] if c["kind"] == "column_type_changed"]
        herbarium_id_changes = [
            c
            for c in type_changes
            if c["table"] == "public.curator" and c["column"] == "herbarium_id"
        ]
        assert len(herbarium_id_changes) == 1
        assert herbarium_id_changes[0]["before"] == "uuid"
        assert herbarium_id_changes[0]["after"] == "bigint"

    def test_drift_state_column_removed_on_existing_table(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()
        result = _build_engine(tmp_path, _curator_without_herbarium_id_fixture()).compute_diff()
        assert result.exit_code == 0
        removed = [c for c in result.diff["changes"] if c["kind"] == "column_removed"]
        assert any(
            c["table"] == "public.curator" and c["column"] == "herbarium_id" for c in removed
        )

    def test_drift_state_column_nullable_changed_on_existing_table(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()
        result = _build_engine(tmp_path, _curator_herbarium_id_not_null_fixture()).compute_diff()
        assert result.exit_code == 0
        nullable_changes = [
            c for c in result.diff["changes"] if c["kind"] == "column_nullable_changed"
        ]
        herbarium_id_changes = [
            c
            for c in nullable_changes
            if c["table"] == "public.curator" and c["column"] == "herbarium_id"
        ]
        assert len(herbarium_id_changes) == 1
        assert herbarium_id_changes[0]["before"] is True
        assert herbarium_id_changes[0]["after"] is False

    def test_drift_state_column_default_not_detected_v1_boundary(self, tmp_path: Path) -> None:
        """A hydrated baseline's default_known is always False (diff.ColumnState) - v1 boundary."""

        _build_engine(tmp_path, _curator_with_defaulted_col_fixture()).generate()
        result = _build_engine(tmp_path, _curator_with_defaulted_col_fixture()).compute_diff()
        assert result.exit_code == 0
        kinds = {c["kind"] for c in result.diff["changes"]}
        assert "column_default_changed" not in kinds

    def test_compute_diff_does_not_write_disk(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()
        manifest = tmp_path / "primary" / "manifest.yaml"
        diff_file = tmp_path / "primary" / "diff.yaml"
        manifest_mtime = manifest.stat().st_mtime_ns
        diff_mtime = diff_file.stat().st_mtime_ns

        _build_engine(tmp_path, _added_table_fixture()).compute_diff()

        assert manifest.stat().st_mtime_ns == manifest_mtime
        assert diff_file.stat().st_mtime_ns == diff_mtime
        curator_rel = tmp_path / "primary" / "public" / "curator" / "relationships.yaml"
        assert curator_rel.is_file()
        assert not any(p.suffix == ".tmp" for p in (tmp_path / "primary").rglob("*"))

    def test_no_baseline_returns_exit_one(self, tmp_path: Path) -> None:
        result = _build_engine(tmp_path, _curator_fixture()).compute_diff()
        assert result.exit_code == 1
        assert result.diff["changes"] == []
        assert result.target_scanned_tables == 0

    def test_cli_include_narrows_extraction_scope(self, tmp_path: Path) -> None:
        """--include narrows the baseline scope too, so no out-of-scope table_removed fires
        (SPEC 2.6.8).
        """

        _build_engine(tmp_path, _curator_fixture()).generate()
        result = _build_engine(tmp_path, _added_table_fixture()).compute_diff(
            DiffRequest(cli_include=("public.curator",)),
        )
        assert result.exit_code == 0
        assert result.target_scanned_tables == 1
        removed = [c for c in result.diff["changes"] if c["kind"] == "table_removed"]
        assert removed == []

    def test_connection_error_exit_four(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()

        class _Failing(MockAdapter):
            def connect(self) -> None:
                raise RuntimeError("db unreachable")

        adapter = _Failing(_curator_fixture())
        engine = Engine(adapter, _conn_config(tmp_path), tmp_path)
        result = engine.compute_diff()
        assert result.exit_code == 4
        assert result.failed_tables == ("db unreachable",)

    def test_partial_extraction_exit_five(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()

        broken_adapter = _CuratorDdlFailingAdapter(_added_table_fixture())
        engine = Engine(broken_adapter, _conn_config(tmp_path), tmp_path)
        result = engine.compute_diff()
        assert result.exit_code == 5
        assert "public.curator" in result.failed_tables


class TestStatisticDrift:
    """Drift measured against a committed print, and the round-trip that makes it usable.

    Every case compares rather than re-generates: `generate` hydrates its baseline from
    artifacts the same run already wrote, so it cannot show drift against a committed print.
    """

    def test_a_moved_statistic_is_reported_against_the_committed_print(
        self,
        tmp_path: Path,
    ) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()
        result = _build_engine(
            tmp_path,
            _curator_with_moved_herbarium_id_stats_fixture(),
        ).compute_diff()
        moved = [
            c
            for c in result.diff["changes"]
            if c["kind"] == "statistic_changed" and c["column"] == "herbarium_id"
        ]

        assert {c["stat"] for c in moved} == {"cardinality", "cardinality_ratio"}

        cardinality = next(c for c in moved if c["stat"] == "cardinality")

        assert cardinality["before"] == 20
        assert cardinality["after"] == 35
        assert cardinality["delta"] == 15
        assert result.diff["summary"]["statistics_drifted"] == 2
        assert result.diff["summary"]["tables_modified"] == 1

    def test_a_scoped_read_suppresses_the_absolute_count_but_not_the_ratio(
        self,
        tmp_path: Path,
    ) -> None:
        """`cardinality_ratio` is scan-normalised, so a `scope` block cannot suppress it."""

        sampled = replace(_conn_config(tmp_path), rules=(RuleConfig(sample=0.5),))
        Engine(MockAdapter(_curator_fixture()), sampled, tmp_path).generate()
        result = Engine(
            MockAdapter(_curator_with_moved_herbarium_id_stats_fixture()),
            sampled,
            tmp_path,
        ).compute_diff()
        moved = [
            c
            for c in result.diff["changes"]
            if c["kind"] == "statistic_changed" and c["column"] == "herbarium_id"
        ]

        assert {c["stat"] for c in moved} == {"cardinality_ratio"}

    def test_an_unmoved_read_reports_no_statistics_at_all(self, tmp_path: Path) -> None:
        """The round-trip guard: serialization alone must not look like drift."""

        _build_engine(tmp_path, _curator_fixture()).generate()
        result = _build_engine(tmp_path, _curator_fixture()).compute_diff()

        assert result.diff["changes"] == []
        assert result.diff["summary"]["statistics_drifted"] == 0
        assert result.diff["summary"]["unchanged_tables"] == 2

    def test_driver_typed_bounds_survive_the_round_trip(self, tmp_path: Path) -> None:
        """Timestamps and decimals reach the artifact as strings and must re-read equal."""

        _build_engine(tmp_path, _driver_typed_fixture()).generate()
        result = _build_engine(tmp_path, _driver_typed_fixture()).compute_diff()

        assert result.diff["changes"] == []

    def test_a_day_passing_alone_reports_nothing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`freshness` is measured against the run and engine-derived, so the second run's
        clock is pushed forward rather than the fixture edited.
        """

        _build_engine(tmp_path, _driver_typed_fixture()).generate()

        later = (datetime.now(UTC) + timedelta(days=13)).isoformat(timespec="seconds")
        monkeypatch.setattr(orchestrator, "_utc_iso_now", lambda: later.replace("+00:00", "Z"))
        result = _build_engine(tmp_path, _driver_typed_fixture()).compute_diff()

        assert result.diff["changes"] == []

    def test_a_retyped_column_is_reported_once(self, tmp_path: Path) -> None:
        """`column_type_changed` owns the field; a statistics event would double-count it."""

        _build_engine(tmp_path, _driver_typed_fixture()).generate()
        retyped = _driver_typed_fixture()
        specimen_loan = retyped["public.specimen_loan"]
        retyped["public.specimen_loan"] = replace(
            specimen_loan,
            columns=[
                replace(c, sql_type="numeric(14, 2)") if c.name == "viability_pct" else c
                for c in specimen_loan.columns
            ],
            stats={
                **specimen_loan.stats,
                "viability_pct": replace(
                    specimen_loan.stats["viability_pct"],
                    sql_type="numeric(14, 2)",
                ),
            },
        )
        result = _build_engine(tmp_path, retyped).compute_diff()
        kinds = [c["kind"] for c in result.diff["changes"]]

        assert kinds == ["column_type_changed"]
        assert result.diff["summary"]["statistics_drifted"] == 0

    def test_a_nan_bound_does_not_drift(self, tmp_path: Path) -> None:
        """Exercises the NaN-equality guard in `diff._same_reading`."""

        _build_engine(tmp_path, _nan_bound_fixture()).generate()
        result = _build_engine(tmp_path, _nan_bound_fixture()).compute_diff()

        assert result.diff["changes"] == []

    def test_a_value_list_moving_alone_is_reported(self, tmp_path: Path) -> None:
        """`values` is back in the compared projection; a count trade fires `stat: values`."""

        _build_engine(tmp_path, _curator_fixture()).generate()
        result = _build_engine(
            tmp_path,
            _curator_with_reshuffled_herbarium_id_values_fixture(),
        ).compute_diff()
        moved = [
            c
            for c in result.diff["changes"]
            if c["kind"] == "statistic_changed" and c["stat"] == "values"
        ]

        assert len(moved) == 1
        assert moved[0]["column"] == "herbarium_id"
        assert moved[0]["before"] == [
            {"value": "00000000-0000-7000-8000-000000000001", "count": 9},
            {"value": "00000000-0000-7000-8000-000000000002", "count": 8},
        ]
        assert moved[0]["after"] == [
            {"value": "00000000-0000-7000-8000-000000000001", "count": 10},
            {"value": "00000000-0000-7000-8000-000000000002", "count": 7},
        ]
        assert "delta" not in moved[0]

    def test_a_new_shape_claim_is_reported(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()
        result = _build_engine(
            tmp_path,
            _curator_with_shaped_herbarium_id_samples_fixture(),
        ).compute_diff()
        claims = [c for c in result.diff["changes"] if c["stat"] == "inferred.looks_like"]

        assert len(claims) == 1
        assert claims[0]["column"] == "herbarium_id"
        assert claims[0]["before"] is None
        assert claims[0]["after"] == "uuid"
        assert "delta" not in claims[0]

    def test_a_redacted_column_reports_no_drift_from_its_placeholders(
        self,
        tmp_path: Path,
    ) -> None:
        conn = replace(
            _conn_config(tmp_path),
            redact=(RedactRule(columns=("*.herbarium_id",), with_="hash"),),
            redaction_salt="pepper",
        )
        Engine(MockAdapter(_curator_fixture()), conn, tmp_path).generate()
        result = Engine(MockAdapter(_curator_fixture()), conn, tmp_path).compute_diff()

        assert result.diff["changes"] == []

    def test_an_unreadable_baseline_file_reports_no_statistics(self, tmp_path: Path) -> None:
        """A statistics.yaml that will not parse leaves that table's stats unknown."""

        _build_engine(tmp_path, _curator_fixture()).generate()
        (tmp_path / "primary" / "public" / "curator" / "statistics.yaml").write_text("{[:\n")
        result = _build_engine(
            tmp_path,
            _curator_with_moved_herbarium_id_stats_fixture(),
        ).compute_diff()
        curator_events = [
            c
            for c in result.diff["changes"]
            if c["kind"] == "statistic_changed" and c["table"] == "public.curator"
        ]

        assert curator_events == []
        assert result.exit_code == 0


class TestGrainAndPhysicalLayoutDrift:
    """The full round trip: a declared change reaches the diff through a committed print."""

    def test_a_declared_grain_key_gained_is_reported(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()
        result = _build_engine(
            tmp_path,
            _curator_with_a_declared_grain_key_fixture(),
        ).compute_diff()
        change = next(
            c
            for c in result.diff["changes"]
            if c["kind"] == "grain_changed" and c["table"] == "public.curator"
        )

        assert change["before"]["keys"] == []
        assert change["after"]["keys"] == [{"columns": ["id"], "detection": "declared"}]

    def test_a_physical_layout_gained_is_reported(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()
        result = _build_engine(tmp_path, _curator_with_a_physical_layout_fixture()).compute_diff()
        change = next(
            c
            for c in result.diff["changes"]
            if c["kind"] == "physical_layout_changed" and c["table"] == "public.curator"
        )

        assert change["before"] is None
        assert change["after"] == {
            "mechanism": "cluster",
            "keys": [{"expression": "id", "column": "id"}],
        }


class TestFreshnessIsDerivedOnceFromTheRunsOwnInstant:
    """`max_age_days` and its band come from `range.max` and `profiled_at` alone (SPEC 2.2.4).

    `generated_at` is stamped once per run and threaded to every table as `profiled_at`, so
    two tables sharing a bound cannot disagree whatever order the run reached them in.
    """

    def test_two_tables_sharing_a_bound_publish_the_same_age(self, tmp_path: Path) -> None:
        shared_max = datetime(2026, 1, 1, tzinfo=UTC)
        fixture = _shared_bound_fixture(shared_max)

        _build_engine(tmp_path, fixture).generate(GenerateRequest(force=True))

        first = yaml.safe_load(
            (tmp_path / "primary" / "public" / "first" / "statistics.yaml").read_text(),
        )["columns"]["seen_at"]["freshness"]
        second = yaml.safe_load(
            (tmp_path / "primary" / "public" / "second" / "statistics.yaml").read_text(),
        )["columns"]["seen_at"]["freshness"]

        assert first == second

    def test_a_future_bound_clamps_to_zero_and_classifies_live(self, tmp_path: Path) -> None:
        future = datetime.now(UTC) + timedelta(days=365)
        fixture = _shared_bound_fixture(future)

        _build_engine(tmp_path, fixture).generate(GenerateRequest(force=True))

        freshness = yaml.safe_load(
            (tmp_path / "primary" / "public" / "first" / "statistics.yaml").read_text(),
        )["columns"]["seen_at"]["freshness"]

        assert freshness == {"max_age_days": 0, "classification": "live"}

    def test_a_dormant_bound_crosses_the_ninety_day_band(self, tmp_path: Path) -> None:
        # `_utc_iso_now()` truncates to the second; a sub-second `old` could cross the band.
        old = datetime.now(UTC).replace(microsecond=0) - timedelta(days=91)
        fixture = _shared_bound_fixture(old)

        _build_engine(tmp_path, fixture).generate(GenerateRequest(force=True))

        freshness = yaml.safe_load(
            (tmp_path / "primary" / "public" / "first" / "statistics.yaml").read_text(),
        )["columns"]["seen_at"]["freshness"]

        assert freshness["max_age_days"] == 91
        assert freshness["classification"] == "dormant"


def _shared_bound_fixture(bound: datetime) -> dict[str, MockTable]:
    """Two tables, each one temporal column whose newest value is the same instant."""

    def _table(name: str) -> MockTable:
        return MockTable(
            type="table",
            namespace_path=("public", name),
            ddl=f"CREATE TABLE public.{name} (seen_at timestamptz);\n",
            columns=[
                ColumnMeta(
                    name="seen_at",
                    sql_type="timestamp with time zone",
                    nullable=False,
                    default=None,
                    ordinal=1,
                ),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "seen_at": ColumnStats(
                    sql_type="timestamp with time zone",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=60,
                    cardinality_ratio=0.6,
                    cardinality_method="exact",
                    range=Range(min=bound - timedelta(days=10), max=bound, span_days=10),
                    percentiles={"p50": bound - timedelta(days=5)},
                    distribution="uniform",
                ),
            },
            samples={},
            row_count=100,
        )

    return {"public.first": _table("first"), "public.second": _table("second")}


class TestOnlineAndOfflineReadTheSameStatistics:
    """Offline evaluates the committed `statistics.yaml`, online the live payload - the same
    artifact by construction, so no predicate can tell which mode answered it.
    """

    def test_the_live_payload_is_the_committed_artifact(self, tmp_path: Path) -> None:
        fixture = _unsupported_column_fixture()
        _build_engine(tmp_path, fixture).generate()
        committed = _read_yaml(tmp_path / "primary" / "public" / "blobs" / "statistics.yaml")
        live = _build_engine(tmp_path, fixture).compute_diff().live_statistics["public.blobs"]

        assert live["columns"] == committed["columns"]
        assert live["row_count"] == committed["row_count"]
        assert live["type"] == committed["type"]

    def test_a_field_the_matrix_forbids_is_absent_from_both(self, tmp_path: Path) -> None:
        """SPEC 2.2.3 marks the cardinality trio forbidden for `unsupported`."""

        fixture = _unsupported_column_fixture()
        _build_engine(tmp_path, fixture).generate()
        committed = _read_yaml(tmp_path / "primary" / "public" / "blobs" / "statistics.yaml")
        live = _build_engine(tmp_path, fixture).compute_diff().live_statistics["public.blobs"]

        for payload in (committed["columns"]["payload"], live["columns"]["payload"]):
            assert "cardinality" not in payload
            assert "cardinality_ratio" not in payload
            assert "cardinality_method" not in payload


def _read_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _inferrable_fixture(*, declared: bool = False, target_key: bool = True) -> dict[str, MockTable]:
    """A `curator_id` column naming `curator`: the stem must equal the in-scope object name."""

    declared_fks = [
        ForeignKeyMeta(
            column=("curator_id",),
            target_table="public.curator",
            target_column=("id",),
            on_delete="CASCADE",
            on_update="NO ACTION",
            constraint_name="specimen_loan_curator_fk",
        ),
    ]

    return {
        "public.specimen_loan": MockTable(
            type="table",
            namespace_path=("public", "specimen_loan"),
            ddl="CREATE TABLE public.specimen_loan (id uuid PRIMARY KEY, curator_id uuid);\n",
            columns=[
                ColumnMeta(name="id", sql_type="uuid", nullable=False, default=None, ordinal=1),
                ColumnMeta(
                    name="curator_id",
                    sql_type="uuid",
                    nullable=True,
                    default=None,
                    ordinal=2,
                ),
            ],
            relationships=declared_fks if declared else [],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                # A fully-unique uuid classifies text (SPEC 4.2), which SPEC 2.2.3 marks R;
                # 20 of 100 distinct values, each count 1, is long_tail.
                "id": ColumnStats(
                    sql_type="uuid",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=100,
                    cardinality_ratio=1.0,
                    cardinality_method="exact",
                    values=tuple(
                        ValueCount(value=f"00000000-0000-7000-8000-{i:012d}", count=1)
                        for i in range(20)
                    ),
                    values_coverage=0.2,
                    distribution="long_tail",
                    empty_count=0,
                    length=Length(min=36, max=36, avg=36.0, p95=36.0),
                    inferred=Inferred(candidate_key=True),
                ),
                "curator_id": ColumnStats(
                    sql_type="uuid",
                    nullable=True,
                    null_count=10,
                    null_rate=0.1,
                    cardinality=20,
                    cardinality_ratio=0.2,
                    cardinality_method="exact",
                    values=(
                        ValueCount(value="00000000-0000-7000-8000-000000000001", count=9),
                        ValueCount(value="00000000-0000-7000-8000-000000000002", count=8),
                    ),
                    values_coverage=0.188889,
                    distribution="uniform",
                    length=Length(min=36, max=36, avg=36.0, p95=36.0),
                ),
            },
            null_patterns=NullPatterns(
                patterns=(
                    NullPattern(columns=(), count=90),
                    NullPattern(columns=("curator_id",), count=10),
                ),
                coverage=1.0,
            ),
            samples={"id": [f"00000000-0000-7000-8000-{i:012d}" for i in range(20)]},
            row_count=100,
        ),
        "public.curator": MockTable(
            type="table",
            namespace_path=("public", "curator"),
            ddl="CREATE TABLE public.curator (id uuid PRIMARY KEY);\n",
            columns=[
                ColumnMeta(name="id", sql_type="uuid", nullable=False, default=None, ordinal=1),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                # cardinality 20 is at or below enumeration_threshold(50) and top_n_values(20),
                # so the column classifies categorical with an exhaustive value list.
                "id": ColumnStats(
                    sql_type="uuid",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=20,
                    cardinality_ratio=1.0,
                    cardinality_method="exact",
                    values=tuple(
                        ValueCount(value=f"00000000-0000-7000-8000-{i:012d}", count=1)
                        for i in range(20)
                    ),
                    values_coverage=1.0,
                    distribution="uniform",
                    length=Length(min=36, max=36, avg=36.0, p95=36.0),
                    inferred=Inferred(candidate_key=True),
                ),
            },
            samples={"id": [f"00000000-0000-7000-8000-{i:012d}" for i in range(20)]},
            row_count=20,
            unique_keys=([UniqueKeyMeta(columns=("id",), primary=True)] if target_key else []),
        ),
    }


class TestInferredForeignKeys:
    """A warehouse declaring no constraints still gets a graph, from column naming."""

    @staticmethod
    def _refers_to(tmp_path: Path, table: str) -> list[dict[str, Any]]:
        data = yaml.safe_load(
            (tmp_path / "primary" / "public" / table / "relationships.yaml").read_text(),
        )

        return data["refers_to"]

    @staticmethod
    def _referenced_by(tmp_path: Path, table: str) -> list[dict[str, Any]]:
        data = yaml.safe_load(
            (tmp_path / "primary" / "public" / table / "relationships.yaml").read_text(),
        )

        return data["referenced_by"]

    def test_an_undeclared_pair_produces_an_inferred_edge(self, tmp_path: Path) -> None:
        Engine(
            MockAdapter(_inferrable_fixture()),
            _conn_config(tmp_path),
            tmp_path,
        ).generate()
        edges = self._refers_to(tmp_path, "specimen_loan")

        assert [(e["target_table"], e["detection"]) for e in edges] == [
            ("public.curator", "inferred"),
        ]

    def test_the_reciprocal_entry_is_written(self, tmp_path: Path) -> None:
        """Inferred edges flow through the same resolve path, so reciprocity holds."""

        Engine(
            MockAdapter(_inferrable_fixture()),
            _conn_config(tmp_path),
            tmp_path,
        ).generate()
        incoming = self._referenced_by(tmp_path, "curator")

        assert [(e["referencer_table"], e["detection"]) for e in incoming] == [
            ("public.specimen_loan", "inferred"),
        ]

    def test_a_declared_edge_is_not_duplicated(self, tmp_path: Path) -> None:
        """The same pair with a real constraint yields one edge, marked declared."""

        fixture = _inferrable_fixture(declared=True)
        Engine(MockAdapter(fixture), _conn_config(tmp_path), tmp_path).generate()
        edges = self._refers_to(tmp_path, "specimen_loan")

        assert [(e["target_table"], e["detection"]) for e in edges] == [
            ("public.curator", "declared"),
        ]

    def test_an_unchanged_inferred_edge_produces_no_diff_event(self, tmp_path: Path) -> None:
        """The live side must not compare a real-looking placeholder against the baseline's
        genuine absence (SPEC 2.3.8) - an unchanged inferred edge diffs as unchanged.
        """

        fixture = _inferrable_fixture()
        _build_engine(tmp_path, fixture).generate()
        result = _build_engine(tmp_path, fixture).compute_diff()

        modified = [c for c in result.diff["changes"] if c["kind"] == "relationship_modified"]
        assert modified == []

    def test_an_inferred_key_promotes_the_column(self, tmp_path: Path) -> None:
        """SPEC 3.1: an inferred FK makes the column foreign_key_candidate."""

        Engine(
            MockAdapter(_inferrable_fixture()),
            _conn_config(tmp_path),
            tmp_path,
        ).generate()
        stats = yaml.safe_load(
            (tmp_path / "primary" / "public" / "specimen_loan" / "statistics.yaml").read_text(),
        )

        assert stats["columns"]["curator_id"]["classification"] == "foreign_key_candidate"

    def test_the_kill_switch_leaves_the_graph_declared_only(self, tmp_path: Path) -> None:
        conn = replace(_conn_config(tmp_path), infer_relationships=False)
        Engine(MockAdapter(_inferrable_fixture()), conn, tmp_path).generate()

        assert self._refers_to(tmp_path, "specimen_loan") == []

    def test_a_target_without_a_declared_key_infers_nothing(self, tmp_path: Path) -> None:
        """The fixture's herbarium declares no key unless the test gives it one."""

        fixture = _inferrable_fixture(target_key=False)
        Engine(MockAdapter(fixture), _conn_config(tmp_path), tmp_path).generate()

        assert self._refers_to(tmp_path, "specimen_loan") == []

    def test_a_print_with_inferred_edges_conforms(self, tmp_path: Path) -> None:
        Engine(
            MockAdapter(_inferrable_fixture()),
            _conn_config(tmp_path),
            tmp_path,
        ).generate()
        issues = [i for i in validate_print(tmp_path / "primary") if i.severity == "error"]

        assert issues == []

    def test_a_fresh_table_still_contributes_to_the_inventory(self, tmp_path: Path) -> None:
        """The pre-pass covers skipped tables, or classifications move with profiling order."""

        Engine(
            MockAdapter(_inferrable_fixture()),
            _conn_config(tmp_path),
            tmp_path,
        ).generate()
        first = self._refers_to(tmp_path, "specimen_loan")

        # specimen_loan is past its own 1-day threshold; curator stays inside the connection's 30.
        TestPerTableFreshnessSkip._age_manifest(tmp_path / "primary" / "manifest.yaml", days=3.0)
        conn = replace(
            _conn_config(tmp_path, max_age_days=30),
            rules=(RuleConfig(include=("public.specimen_loan",), max_age_days=1),),
        )
        result = Engine(MockAdapter(_inferrable_fixture()), conn, tmp_path).generate()

        assert {t.fqn: t.status for t in result.tables} == {
            "public.specimen_loan": "ok",
            "public.curator": "skipped",
        }
        assert self._refers_to(tmp_path, "specimen_loan") == first

    def test_a_cli_include_narrowed_run_still_infers_the_full_runs_edge(
        self,
        tmp_path: Path,
    ) -> None:
        """The universe is the committed print's table set, not one flag's ask."""

        Engine(MockAdapter(_inferrable_fixture()), _conn_config(tmp_path), tmp_path).generate()
        first = self._refers_to(tmp_path, "specimen_loan")

        Engine(
            MockAdapter(_inferrable_fixture()),
            _conn_config(tmp_path),
            tmp_path,
        ).generate(GenerateRequest(force=True, cli_include=("public.specimen_loan",)))

        assert self._refers_to(tmp_path, "specimen_loan") == first

    def test_a_cli_include_narrowed_run_keeps_the_columns_classification(
        self,
        tmp_path: Path,
    ) -> None:
        Engine(MockAdapter(_inferrable_fixture()), _conn_config(tmp_path), tmp_path).generate()

        Engine(
            MockAdapter(_inferrable_fixture()),
            _conn_config(tmp_path),
            tmp_path,
        ).generate(GenerateRequest(force=True, cli_include=("public.specimen_loan",)))

        stats = yaml.safe_load(
            (tmp_path / "primary" / "public" / "specimen_loan" / "statistics.yaml").read_text(),
        )
        assert stats["columns"]["curator_id"]["classification"] == "foreign_key_candidate"

    def test_a_cli_exclude_narrowing_the_target_still_infers_the_edge(
        self,
        tmp_path: Path,
    ) -> None:
        """`--exclude` narrows the same list through the same call as `--include`."""

        Engine(MockAdapter(_inferrable_fixture()), _conn_config(tmp_path), tmp_path).generate()
        first = self._refers_to(tmp_path, "specimen_loan")

        Engine(
            MockAdapter(_inferrable_fixture()),
            _conn_config(tmp_path),
            tmp_path,
        ).generate(GenerateRequest(force=True, cli_exclude=("public.curator",)))

        assert self._refers_to(tmp_path, "specimen_loan") == first

    def test_a_narrowed_run_against_an_unchanged_database_exits_clean(
        self,
        tmp_path: Path,
    ) -> None:
        """No edge lost, no phantom `relationship_removed`, no conformance violation."""

        Engine(MockAdapter(_inferrable_fixture()), _conn_config(tmp_path), tmp_path).generate()

        result = Engine(
            MockAdapter(_inferrable_fixture()),
            _conn_config(tmp_path),
            tmp_path,
        ).generate(GenerateRequest(force=True, cli_include=("public.specimen_loan",)))

        assert result.exit_code == EXIT_OK
        errors = [i for i in validate_print(tmp_path / "primary") if i.severity == "error"]
        assert errors == []

    def test_narrowing_to_the_target_still_preserves_its_incoming_edge(
        self,
        tmp_path: Path,
    ) -> None:
        """The referencer sits out of this run's selectors, and `dbprint check`'s reciprocity
        rule only walks `referenced_by`, so an emptied list passes unnoticed there.
        """

        Engine(MockAdapter(_inferrable_fixture()), _conn_config(tmp_path), tmp_path).generate()

        Engine(
            MockAdapter(_inferrable_fixture()),
            _conn_config(tmp_path),
            tmp_path,
        ).generate(GenerateRequest(force=True, cli_include=("public.curator",)))

        assert self._referenced_by(tmp_path, "curator") == [
            {
                "column": ["id"],
                "referencer_table": "public.specimen_loan",
                "referencer_column": ["curator_id"],
                "detection": "inferred",
                "observed": {
                    "fanout_avg": 5.0,
                    "fanout_max": 9,
                    "target_coverage": 1.0,
                    "coherent": True,
                    "scope_compatible": True,
                },
            },
        ]

    def test_narrowing_to_the_target_leaves_a_conformant_print(self, tmp_path: Path) -> None:
        Engine(MockAdapter(_inferrable_fixture()), _conn_config(tmp_path), tmp_path).generate()

        Engine(
            MockAdapter(_inferrable_fixture()),
            _conn_config(tmp_path),
            tmp_path,
        ).generate(GenerateRequest(force=True, cli_include=("public.curator",)))

        errors = [i for i in validate_print(tmp_path / "primary") if i.severity == "error"]
        assert errors == []

    def test_both_ends_narrowed_in_matches_a_full_run(self, tmp_path: Path) -> None:
        """A selector wide enough to cover both tables changes nothing about the graph."""

        Engine(MockAdapter(_inferrable_fixture()), _conn_config(tmp_path), tmp_path).generate()
        full = self._refers_to(tmp_path, "specimen_loan")

        Engine(
            MockAdapter(_inferrable_fixture()),
            _conn_config(tmp_path),
            tmp_path,
        ).generate(GenerateRequest(force=True, cli_include=("public.*",)))

        assert self._refers_to(tmp_path, "specimen_loan") == full

    def test_a_narrowed_first_run_infers_nothing_extra(self, tmp_path: Path) -> None:
        """No baseline: the universe is just this run's matches (`_baseline_only_tables`)."""

        Engine(
            MockAdapter(_inferrable_fixture()),
            _conn_config(tmp_path),
            tmp_path,
        ).generate(GenerateRequest(cli_include=("public.specimen_loan",)))

        assert self._refers_to(tmp_path, "specimen_loan") == []


def _candidate_key_column(cardinality: int) -> ColumnStats:
    """A single-column PK: exhaustive, unique, unrestricted by anything `observed` reads."""

    return ColumnStats(
        sql_type="integer",
        nullable=False,
        null_count=0,
        null_rate=0.0,
        cardinality=cardinality,
        cardinality_ratio=1.0,
        cardinality_method="exact",
        # SPEC 2.2.4: tied counts break lexicographically by value, not numerically - "10"
        # sorts before "2". sorted(..., key=str) holds for every cardinality this helper sees.
        values=tuple(ValueCount(value=i, count=1) for i in sorted(range(cardinality), key=str)),
        values_coverage=1.0,
        distribution="uniform",
        inferred=Inferred(candidate_key=True),
    )


def _fk_column(counts: tuple[int, ...], rows_scanned: int) -> ColumnStats:
    """A referencing FK column with an explicit per-value split, summing to `rows_scanned`."""

    return ColumnStats(
        sql_type="integer",
        nullable=True,
        null_count=0,
        null_rate=0.0,
        cardinality=len(counts),
        cardinality_ratio=round(len(counts) / rows_scanned, 6),
        cardinality_method="exact",
        values=tuple(ValueCount(value=i, count=c) for i, c in enumerate(counts)),
        values_coverage=1.0,
        distribution="long_tail",
    )


def _sketch_probe_column(cardinality: int, cardinality_ratio: float) -> ColumnStats:
    """An integer column whose ratio flips `candidate_key` only at SPEC 4.2's own threshold."""

    return ColumnStats(
        sql_type="integer",
        nullable=False,
        null_count=0,
        null_rate=0.0,
        cardinality=cardinality,
        cardinality_ratio=cardinality_ratio,
        cardinality_method="exact",
        values=tuple(ValueCount(value=i, count=1) for i in sorted(range(cardinality), key=str)),
        values_coverage=1.0,
        distribution="uniform",
    )


def _observed_fixture() -> dict[str, MockTable]:
    """One parent and three children covering `observed` (SPEC 2.3.10): a sound many-to-one
    edge, one whose cardinality contradicts containment, and a composite FK that gets none.
    """

    def table(
        name: str,
        columns: list[ColumnMeta],
        stats: dict[str, ColumnStats],
        relationships: list[ForeignKeyMeta],
        row_count: int,
    ) -> MockTable:
        return MockTable(
            type="table",
            namespace_path=("public", name),
            ddl=f"CREATE TABLE public.{name} (id int PRIMARY KEY);\n",
            columns=columns,
            relationships=relationships,
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats=stats,
            samples={},
            row_count=row_count,
        )

    def fk(column: str, target: str, target_column: str = "id") -> ForeignKeyMeta:
        return ForeignKeyMeta(
            column=(column,),
            target_table=f"public.{target}",
            target_column=(target_column,),
            on_delete="CASCADE",
            on_update="NO ACTION",
            constraint_name=f"{column}_fk",
        )

    return {
        "public.parent": table(
            "parent",
            [ColumnMeta(name="id", sql_type="integer", nullable=False, default=None, ordinal=1)],
            {"id": _candidate_key_column(10)},
            [],
            row_count=10,
        ),
        "public.child": table(
            "child",
            [
                ColumnMeta(name="id", sql_type="integer", nullable=False, default=None, ordinal=1),
                ColumnMeta(
                    name="parent_id",
                    sql_type="integer",
                    nullable=True,
                    default=None,
                    ordinal=2,
                ),
            ],
            {
                "id": _candidate_key_column(40),
                # 4 of 10 parents (coverage 0.4), 40 rows / 4 keys (fanout_avg 10.0), top 15.
                "parent_id": _fk_column((15, 15, 5, 5), rows_scanned=40),
            },
            [fk("parent_id", "parent")],
            row_count=40,
        ),
        "public.orphan": table(
            "orphan",
            [
                ColumnMeta(name="id", sql_type="integer", nullable=False, default=None, ordinal=1),
                ColumnMeta(
                    name="bad_parent_id",
                    sql_type="integer",
                    nullable=True,
                    default=None,
                    ordinal=2,
                ),
            ],
            {
                "id": _candidate_key_column(15),
                # 15 distinct values against a 10-row parent: what `coherent: false` is for.
                "bad_parent_id": _candidate_key_column(15),
            },
            [fk("bad_parent_id", "parent")],
            row_count=15,
        ),
        "public.composite_child": table(
            "composite_child",
            [
                ColumnMeta(name="a", sql_type="integer", nullable=False, default=None, ordinal=1),
                ColumnMeta(name="b", sql_type="integer", nullable=False, default=None, ordinal=2),
            ],
            {
                "a": _candidate_key_column(5),
                "b": _candidate_key_column(5),
            },
            [
                ForeignKeyMeta(
                    column=("a", "b"),
                    target_table="public.parent",
                    target_column=("id", "id"),
                    on_delete="CASCADE",
                    on_update="NO ACTION",
                    constraint_name="composite_fk",
                ),
            ],
            row_count=5,
        ),
    }


def _widened_candidates_fixture() -> dict[str, MockTable]:
    """SPEC 2.2.14's widened MUST-set: one column per bullet, a control, and a composite pair."""

    above_k = SPEC_SKETCH_K + 1

    wide = MockTable(
        type="table",
        namespace_path=("public", "wide"),
        ddl="CREATE TABLE public.wide (id int PRIMARY KEY);\n",
        columns=[
            ColumnMeta(name="id", sql_type="integer", nullable=False, default=None, ordinal=1),
            ColumnMeta(
                name="accession_number",
                sql_type="integer",
                nullable=False,
                default=None,
                ordinal=2,
            ),
            ColumnMeta(name="rank", sql_type="integer", nullable=False, default=None, ordinal=3),
            ColumnMeta(
                name="recorded_by",
                sql_type="integer",
                nullable=False,
                default=None,
                ordinal=4,
            ),
        ],
        relationships=[],
        indexes=[],
        comments=CommentsMeta(table=None, columns={}),
        stats={
            "id": _candidate_key_column(3),
            # Declared UNIQUE, not the primary key; ratio stays short of SPEC 4.2's
            # candidate_key threshold - isolates the declared-unique bullet alone.
            "accession_number": _sketch_probe_column(above_k, 0.5),
            # Below k, no declared constraint, ratio short of candidate_key - isolates
            # the at-or-below-k bullet alone.
            "rank": _sketch_probe_column(5, 0.01),
            # Above k, no constraint, ratio short of candidate_key - none of the three
            # bullets apply; the control for `sketch_all_columns`.
            "recorded_by": _sketch_probe_column(above_k, 0.4),
        },
        samples={},
        row_count=above_k * 2,
        unique_keys=[UniqueKeyMeta(columns=("accession_number",), primary=False)],
    )

    composite_probe = MockTable(
        type="table",
        namespace_path=("public", "composite_probe"),
        ddl="CREATE TABLE public.composite_probe (cohort int, plot int);\n",
        columns=[
            ColumnMeta(name="cohort", sql_type="integer", nullable=False, default=None, ordinal=1),
            ColumnMeta(name="plot", sql_type="integer", nullable=False, default=None, ordinal=2),
        ],
        relationships=[
            ForeignKeyMeta(
                column=("cohort", "plot"),
                target_table="public.wide",
                target_column=("id", "id"),
                on_delete="CASCADE",
                on_update="NO ACTION",
                constraint_name="composite_probe_wide_fk",
            ),
        ],
        indexes=[],
        comments=CommentsMeta(table=None, columns={}),
        stats={
            # Composite members: cardinality and ratio clear none of the widened bullets, and a
            # composite edge never seeds a sketch (SPEC 2.2.14).
            "cohort": _sketch_probe_column(above_k, 0.3),
            "plot": _sketch_probe_column(above_k, 0.6),
        },
        samples={},
        row_count=above_k * 2,
    )

    return {"public.wide": wide, "public.composite_probe": composite_probe}


def _overlap_fixture() -> dict[str, MockTable]:
    """`parent` (id 0-9) plus a child sharing no value and one sharing some, so that
    containment and target_coverage genuinely differ (SPEC 2.3.10).
    """

    def table(name: str, values: tuple[int, ...]) -> MockTable:
        return MockTable(
            type="table",
            namespace_path=("public", name),
            ddl=f"CREATE TABLE public.{name} (id int PRIMARY KEY, parent_id int);\n",
            columns=[
                ColumnMeta(name="id", sql_type="integer", nullable=False, default=None, ordinal=1),
                ColumnMeta(
                    name="parent_id",
                    sql_type="integer",
                    nullable=True,
                    default=None,
                    ordinal=2,
                ),
            ],
            relationships=[
                ForeignKeyMeta(
                    column=("parent_id",),
                    target_table="public.parent",
                    target_column=("id",),
                    on_delete="CASCADE",
                    on_update="NO ACTION",
                    constraint_name=f"{name}_parent_fk",
                ),
            ],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "id": _candidate_key_column(len(values)),
                "parent_id": ColumnStats(
                    sql_type="integer",
                    nullable=True,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=len(values),
                    cardinality_ratio=1.0,
                    cardinality_method="exact",
                    values=tuple(ValueCount(value=v, count=1) for v in values),
                    values_coverage=1.0,
                    distribution="uniform",
                ),
            },
            samples={},
            row_count=len(values),
        )

    parent = MockTable(
        type="table",
        namespace_path=("public", "parent"),
        ddl="CREATE TABLE public.parent (id int PRIMARY KEY);\n",
        columns=[
            ColumnMeta(name="id", sql_type="integer", nullable=False, default=None, ordinal=1),
        ],
        relationships=[],
        indexes=[],
        comments=CommentsMeta(table=None, columns={}),
        stats={"id": _candidate_key_column(10)},
        samples={},
        row_count=10,
    )

    return {
        "public.parent": parent,
        # None of these five values (100-104) exist among parent's 0-9.
        "public.disjoint_child": table("disjoint_child", (100, 101, 102, 103, 104)),
        # 0, 1, 2 exist in parent (3 of 5); 100, 101 do not.
        "public.partial_child": table("partial_child", (0, 1, 2, 100, 101)),
    }


def _truncated_parent_fixture() -> dict[str, MockTable]:
    """`big_parent` genuinely exceeds `SPEC_SKETCH_K`, so its sketch truncates for real."""

    pool = range(SPEC_SKETCH_K + 76)  # 1100 values; truncation drops the 76 highest-hash ones
    ranked = sorted(pool, key=lambda v: low64_md5(canonical_form(v, "integer")))
    retained, dropped = ranked[:SPEC_SKETCH_K], ranked[SPEC_SKETCH_K:]

    # 4 child values whose hash the parent's truncation kept (answerable and matched), 1 it
    # dropped (unanswerable) - so the answerable count lands below the child's sketch length.
    child_values = (*retained[:4], dropped[0])

    big_parent = MockTable(
        type="table",
        namespace_path=("public", "big_parent"),
        ddl="CREATE TABLE public.big_parent (id int PRIMARY KEY);\n",
        columns=[
            ColumnMeta(name="id", sql_type="integer", nullable=False, default=None, ordinal=1),
        ],
        relationships=[],
        indexes=[],
        comments=CommentsMeta(table=None, columns={}),
        stats={"id": _candidate_key_column(len(pool))},
        samples={},
        row_count=len(pool),
    )

    big_child = MockTable(
        type="table",
        namespace_path=("public", "big_child"),
        ddl="CREATE TABLE public.big_child (id int PRIMARY KEY, parent_id int);\n",
        columns=[
            ColumnMeta(name="id", sql_type="integer", nullable=False, default=None, ordinal=1),
            ColumnMeta(
                name="parent_id",
                sql_type="integer",
                nullable=True,
                default=None,
                ordinal=2,
            ),
        ],
        relationships=[
            ForeignKeyMeta(
                column=("parent_id",),
                target_table="public.big_parent",
                target_column=("id",),
                on_delete="CASCADE",
                on_update="NO ACTION",
                constraint_name="big_child_parent_fk",
            ),
        ],
        indexes=[],
        comments=CommentsMeta(table=None, columns={}),
        stats={
            "id": _candidate_key_column(len(child_values)),
            "parent_id": ColumnStats(
                sql_type="integer",
                nullable=True,
                null_count=0,
                null_rate=0.0,
                cardinality=len(child_values),
                cardinality_ratio=1.0,
                cardinality_method="exact",
                values=tuple(ValueCount(value=v, count=1) for v in sorted(child_values, key=str)),
                values_coverage=1.0,
                distribution="uniform",
            ),
        },
        samples={},
        row_count=len(child_values),
    )

    return {"public.big_parent": big_parent, "public.big_child": big_child}


class TestMeasuredOverlap:
    """SPEC 2.3.10: `containment`/`target_coverage` measured from two sketches, not counts."""

    @staticmethod
    def _refers_to(tmp_path: Path, table: str) -> dict[str, Any]:
        data = yaml.safe_load(
            (tmp_path / "primary" / "public" / table / "relationships.yaml").read_text(),
        )

        return data["refers_to"][0]

    def test_a_disjoint_edge_measures_zero_overlap(self, tmp_path: Path) -> None:
        Engine(MockAdapter(_overlap_fixture()), _conn_config(tmp_path), tmp_path).generate()
        observed = self._refers_to(tmp_path, "disjoint_child")["observed"]

        assert observed["containment"] == 0.0
        assert observed["target_coverage"] == 0.0

    def test_a_partial_edge_measures_the_true_share_on_each_side(self, tmp_path: Path) -> None:
        Engine(MockAdapter(_overlap_fixture()), _conn_config(tmp_path), tmp_path).generate()
        observed = self._refers_to(tmp_path, "partial_child")["observed"]

        # 3 of the child's 5 values are in the parent: containment = 3/5.
        assert observed["containment"] == 0.6
        # Those same 3 matches cover 3 of the parent's 10 values: target_coverage = 3/10.
        assert observed["target_coverage"] == 0.3

    def test_repeated_generation_is_byte_identical(self, tmp_path: Path) -> None:
        Engine(MockAdapter(_overlap_fixture()), _conn_config(tmp_path), tmp_path).generate()
        first = self._refers_to(tmp_path, "partial_child")["observed"]

        Engine(
            MockAdapter(_overlap_fixture()),
            _conn_config(tmp_path),
            tmp_path,
        ).generate(GenerateRequest(force=True))
        second = self._refers_to(tmp_path, "partial_child")["observed"]

        assert first == second

    def test_an_exhaustive_child_against_a_truncated_parent_needs_no_scale_up(
        self,
        tmp_path: Path,
    ) -> None:
        """SPEC 2.2.14: a truncating parent (1100 values) against an exhaustive child."""

        # A 1100-distinct-value PK stays `categorical` (SPEC 3.1) rather than `numeric`,
        # so its fixture needs only the values `_candidate_key_column` already supplies.
        Engine(
            MockAdapter(_truncated_parent_fixture()),
            _conn_config(tmp_path, enumeration_threshold=2000),
            tmp_path,
        ).generate()

        assert validate_print(tmp_path / "primary") == []

        child_stats = yaml.safe_load(
            (tmp_path / "primary" / "public" / "big_child" / "statistics.yaml").read_text(),
        )
        parent_stats = yaml.safe_load(
            (tmp_path / "primary" / "public" / "big_parent" / "statistics.yaml").read_text(),
        )
        child_sketch = decode_sketch(child_stats["columns"]["parent_id"]["sketch"]["values"])
        parent_sketch = decode_sketch(parent_stats["columns"]["id"]["sketch"]["values"])

        assert child_sketch is not None
        assert parent_sketch is not None
        assert len(child_sketch) == 5  # exhaustive: cardinality never reached SPEC_SKETCH_K
        assert len(parent_sketch) == SPEC_SKETCH_K  # truncated: 1100 distinct values

        theta_parent = max(parent_sketch)
        answerable_count = sum(1 for h in child_sketch if h < theta_parent)

        # The property a patched SKETCH_K never proved: the fixture's own truncation, not
        # a test-side clamp, is what shrinks the answerable set below the child's sketch.
        assert answerable_count < len(child_sketch)

        edge = self._refers_to(tmp_path, "big_child")["observed"]

        assert edge["containment"] == 1.0
        assert edge["target_coverage"] == 0.004545
        assert edge["answerable_count"] == answerable_count


class TestObservedBlock:
    """SPEC 2.3.10: what joining across an edge costs, from statistics already on hand."""

    @staticmethod
    def _refers_to(tmp_path: Path, table: str) -> list[dict[str, Any]]:
        data = yaml.safe_load(
            (tmp_path / "primary" / "public" / table / "relationships.yaml").read_text(),
        )

        return data["refers_to"]

    def test_a_many_to_one_edge_states_its_fanout_and_coverage(self, tmp_path: Path) -> None:
        Engine(MockAdapter(_observed_fixture()), _conn_config(tmp_path), tmp_path).generate()
        edge = self._refers_to(tmp_path, "child")[0]

        assert edge["observed"] == {
            "fanout_avg": 10.0,
            "fanout_max": 15,
            "target_coverage": 0.4,
            "containment": 1.0,
            "answerable_count": 4,
            "coherent": True,
            "scope_compatible": True,
        }

    def test_a_one_to_one_edge_reads_fanout_1(self, tmp_path: Path) -> None:
        fixture = _observed_fixture()
        # Re-key child.parent_id 1:1 against the 10-row parent: cardinality 10, no repeats.
        fixture["public.child"] = replace(
            fixture["public.child"],
            row_count=10,
            stats={
                **fixture["public.child"].stats,
                "parent_id": _candidate_key_column(10),
            },
        )
        Engine(MockAdapter(fixture), _conn_config(tmp_path), tmp_path).generate()
        edge = self._refers_to(tmp_path, "child")[0]

        assert edge["observed"]["fanout_avg"] == 1.0
        assert edge["observed"]["target_coverage"] == 1.0
        assert edge["observed"]["coherent"] is True

    def test_an_edge_whose_child_outnumbers_its_parent_is_incoherent(
        self,
        tmp_path: Path,
    ) -> None:
        Engine(MockAdapter(_observed_fixture()), _conn_config(tmp_path), tmp_path).generate()
        edge = self._refers_to(tmp_path, "orphan")[0]

        assert edge["observed"]["coherent"] is False

    def test_a_composite_edge_carries_no_observed_block(self, tmp_path: Path) -> None:
        Engine(MockAdapter(_observed_fixture()), _conn_config(tmp_path), tmp_path).generate()
        edge = self._refers_to(tmp_path, "composite_child")[0]

        assert "observed" not in edge

    def test_a_mismatched_scope_publishes_no_ratio(self, tmp_path: Path) -> None:
        fixture = _observed_fixture()
        fixture["public.child"] = replace(fixture["public.child"], rows_scanned=20)
        conn = replace(
            _conn_config(tmp_path),
            rules=(RuleConfig(include=("public.child",), sample=0.5),),
        )
        Engine(MockAdapter(fixture), conn, tmp_path).generate()
        edge = self._refers_to(tmp_path, "child")[0]

        assert edge["observed"] == {"scope_compatible": False}

    def test_the_mirror_referenced_by_entry_carries_the_same_numbers(
        self,
        tmp_path: Path,
    ) -> None:
        """The same edge, read from the other table's file, states identical numbers."""

        Engine(MockAdapter(_observed_fixture()), _conn_config(tmp_path), tmp_path).generate()
        data = yaml.safe_load(
            (tmp_path / "primary" / "public" / "parent" / "relationships.yaml").read_text(),
        )
        incoming = next(e for e in data["referenced_by"] if e["referencer_table"] == "public.child")

        assert incoming["observed"] == self._refers_to(tmp_path, "child")[0]["observed"]


class TestKeySketch:
    """SPEC 2.2.14: every join-key column, either side, carries a KMV sketch."""

    @staticmethod
    def _stats(tmp_path: Path, table: str) -> dict[str, Any]:
        return yaml.safe_load(
            (tmp_path / "primary" / "public" / table / "statistics.yaml").read_text(),
        )

    def test_the_referencing_column_carries_a_sketch(self, tmp_path: Path) -> None:
        Engine(MockAdapter(_observed_fixture()), _conn_config(tmp_path), tmp_path).generate()
        col = self._stats(tmp_path, "child")["columns"]["parent_id"]

        assert col["sketch"]["method"] == "kmv_md5_lo64"
        assert decode_sketch(col["sketch"]["values"]) is not None

    def test_the_referenced_column_also_carries_a_sketch(self, tmp_path: Path) -> None:
        """The parent side of the same edge - a different table's own file."""

        Engine(MockAdapter(_observed_fixture()), _conn_config(tmp_path), tmp_path).generate()
        col = self._stats(tmp_path, "parent")["columns"]["id"]

        assert col["sketch"]["method"] == "kmv_md5_lo64"

    def test_a_composite_edges_columns_still_sketch_via_the_widened_set(
        self,
        tmp_path: Path,
    ) -> None:
        """A composite FK seeds no endpoint; the widened set sketches `a`/`b` anyway (2.2.14)."""

        Engine(MockAdapter(_observed_fixture()), _conn_config(tmp_path), tmp_path).generate()
        columns = self._stats(tmp_path, "composite_child")["columns"]

        assert columns["a"]["sketch"]["method"] == "kmv_md5_lo64"
        assert columns["b"]["sketch"]["method"] == "kmv_md5_lo64"

    def test_a_measured_candidate_key_with_no_edge_still_carries_a_sketch(
        self,
        tmp_path: Path,
    ) -> None:
        """`child.id` names no edge; small cardinality and measured `candidate_key` widen it in."""

        Engine(MockAdapter(_observed_fixture()), _conn_config(tmp_path), tmp_path).generate()

        col = self._stats(tmp_path, "child")["columns"]["id"]
        assert col["sketch"]["method"] == "kmv_md5_lo64"

    def test_a_redacted_join_key_carries_no_sketch(self, tmp_path: Path) -> None:
        conn = replace(
            _conn_config(tmp_path),
            redact=(RedactRule(columns=("*.parent_id",), with_="hash"),),
            redaction_salt="pepper",
        )
        Engine(MockAdapter(_observed_fixture()), conn, tmp_path).generate()

        assert "sketch" not in self._stats(tmp_path, "child")["columns"]["parent_id"]

    def test_a_scoped_tables_join_key_carries_no_sketch(self, tmp_path: Path) -> None:
        fixture = _observed_fixture()
        fixture["public.child"] = replace(fixture["public.child"], rows_scanned=20)
        conn = replace(
            _conn_config(tmp_path),
            rules=(RuleConfig(include=("public.child",), sample=0.5),),
        )
        Engine(MockAdapter(fixture), conn, tmp_path).generate()

        assert "sketch" not in self._stats(tmp_path, "child")["columns"]["parent_id"]

    def test_repeated_generation_is_byte_identical(self, tmp_path: Path) -> None:
        """SPEC 2.2.14: unkeyed and deterministic - two runs, same bytes."""

        Engine(MockAdapter(_observed_fixture()), _conn_config(tmp_path), tmp_path).generate()
        first = self._stats(tmp_path, "child")["columns"]["parent_id"]["sketch"]

        Engine(
            MockAdapter(_observed_fixture()),
            _conn_config(tmp_path),
            tmp_path,
        ).generate(GenerateRequest(force=True))
        second = self._stats(tmp_path, "child")["columns"]["parent_id"]["sketch"]

        assert first == second


class TestWidenedSketchCandidates:
    """SPEC 2.2.14's widened MUST-set: declared-unique, at-or-below k, or a candidate key."""

    @staticmethod
    def _stats(tmp_path: Path, table: str) -> dict[str, Any]:
        return yaml.safe_load(
            (tmp_path / "primary" / "public" / table / "statistics.yaml").read_text(),
        )

    @staticmethod
    def _conn(tmp_path: Path, *, sketch_all_columns: bool = False) -> ConnectionConfig:
        conn = _conn_config(tmp_path, enumeration_threshold=SPEC_SKETCH_K + 10)

        return replace(conn, sketch_all_columns=sketch_all_columns) if sketch_all_columns else conn

    def test_a_declared_unique_column_above_k_carries_a_sketch(self, tmp_path: Path) -> None:
        Engine(
            MockAdapter(_widened_candidates_fixture()),
            self._conn(tmp_path),
            tmp_path,
        ).generate()

        col = self._stats(tmp_path, "wide")["columns"]["accession_number"]
        assert col["sketch"]["method"] == "kmv_md5_lo64"

    def test_a_column_at_or_below_k_carries_a_sketch(self, tmp_path: Path) -> None:
        Engine(
            MockAdapter(_widened_candidates_fixture()),
            self._conn(tmp_path),
            tmp_path,
        ).generate()

        col = self._stats(tmp_path, "wide")["columns"]["rank"]
        assert col["sketch"]["method"] == "kmv_md5_lo64"

    def test_a_plain_high_cardinality_column_carries_no_sketch_by_default(
        self,
        tmp_path: Path,
    ) -> None:
        Engine(
            MockAdapter(_widened_candidates_fixture()),
            self._conn(tmp_path),
            tmp_path,
        ).generate()

        assert "sketch" not in self._stats(tmp_path, "wide")["columns"]["recorded_by"]

    def test_sketch_all_columns_reaches_the_plain_high_cardinality_column(
        self,
        tmp_path: Path,
    ) -> None:
        Engine(
            MockAdapter(_widened_candidates_fixture()),
            self._conn(tmp_path, sketch_all_columns=True),
            tmp_path,
        ).generate()

        col = self._stats(tmp_path, "wide")["columns"]["recorded_by"]
        assert col["sketch"]["method"] == "kmv_md5_lo64"

    def test_a_composite_member_with_no_qualifying_stat_carries_no_sketch(
        self,
        tmp_path: Path,
    ) -> None:
        """`cohort`/`plot` are composite FK members clearing none of the widened bullets."""

        Engine(
            MockAdapter(_widened_candidates_fixture()),
            self._conn(tmp_path),
            tmp_path,
        ).generate()

        columns = self._stats(tmp_path, "composite_probe")["columns"]
        assert "sketch" not in columns["cohort"]
        assert "sketch" not in columns["plot"]


class _KeySketchFailingAdapter(MockAdapter):
    """Fails `compute_key_sketch` for one column, or every column, as a statement timeout would."""

    def __init__(
        self,
        fixture: dict[str, MockTable],
        *,
        failing_column: str | None = None,
        fail_every_column: bool = False,
    ) -> None:
        super().__init__(fixture)
        self._failing_column = failing_column
        self._fail_every_column = fail_every_column

    def compute_key_sketch(
        self,
        fqn: str,
        column: str,
        sql_type: str,
        kind: Any,
        k: int,
    ) -> tuple[int, ...]:
        if self._fail_every_column or column == self._failing_column:
            raise RuntimeError("simulated sketch query timeout")

        return super().compute_key_sketch(fqn, column, sql_type, kind, k)


class _KeySketchKeyErrorAdapter(MockAdapter):
    """Raises `KeyError`, `MockAdapter`'s own undeclined shape, not the vendor `QueryFailed` one."""

    def compute_key_sketch(
        self,
        fqn: str,
        column: str,
        sql_type: str,
        kind: Any,
        k: int,
    ) -> tuple[int, ...]:
        if column == "parent_id":
            raise KeyError(column)

        return super().compute_key_sketch(fqn, column, sql_type, kind, k)


class TestKeySketchFaultIsolation:
    """One column's sketch query failure fails that column, never the run."""

    @staticmethod
    def _stats(tmp_path: Path, table: str) -> dict[str, Any]:
        return yaml.safe_load(
            (tmp_path / "primary" / "public" / table / "statistics.yaml").read_text(),
        )

    def test_the_failing_column_carries_no_sketch(self, tmp_path: Path) -> None:
        adapter = _KeySketchFailingAdapter(_observed_fixture(), failing_column="parent_id")
        result = Engine(adapter, _conn_config(tmp_path), tmp_path).generate()

        assert "sketch" not in self._stats(tmp_path, "child")["columns"]["parent_id"]
        assert result.sketch_failures == (
            SketchFailure(
                table="public.child",
                column="parent_id",
                error=result.sketch_failures[0].error,
            ),
        )

    def test_every_other_column_still_gets_its_sketch(self, tmp_path: Path) -> None:
        adapter = _KeySketchFailingAdapter(_observed_fixture(), failing_column="parent_id")
        Engine(adapter, _conn_config(tmp_path), tmp_path).generate()

        assert "sketch" in self._stats(tmp_path, "parent")["columns"]["id"]

    def test_every_artifact_is_still_written(self, tmp_path: Path) -> None:
        adapter = _KeySketchFailingAdapter(_observed_fixture(), failing_column="parent_id")
        Engine(adapter, _conn_config(tmp_path), tmp_path).generate()
        prints = tmp_path / "primary"

        assert (prints / "manifest.yaml").is_file()
        assert (prints / "diff.yaml").is_file()
        assert (prints / "reading.md").is_file()
        assert (prints / "public" / "child" / "relationships.yaml").is_file()
        assert (prints / "public" / "parent" / "relationships.yaml").is_file()

    def test_the_exit_code_is_partial_not_generic(self, tmp_path: Path) -> None:
        adapter = _KeySketchFailingAdapter(_observed_fixture(), failing_column="parent_id")
        result = Engine(adapter, _conn_config(tmp_path), tmp_path).generate()

        assert result.exit_code == EXIT_PARTIAL

    def test_a_successful_run_carries_no_sketch_failures(self, tmp_path: Path) -> None:
        result = Engine(
            MockAdapter(_observed_fixture()),
            _conn_config(tmp_path),
            tmp_path,
        ).generate()

        assert result.sketch_failures == ()
        assert result.exit_code != EXIT_PARTIAL

    def test_every_column_failing_still_completes_the_run(self, tmp_path: Path) -> None:
        """Every join-key participant in the fixture fails; none aborts a sibling."""

        adapter = _KeySketchFailingAdapter(_observed_fixture(), fail_every_column=True)
        result = Engine(adapter, _conn_config(tmp_path), tmp_path).generate()

        assert "sketch" not in self._stats(tmp_path, "child")["columns"]["parent_id"]
        assert "sketch" not in self._stats(tmp_path, "parent")["columns"]["id"]
        assert len(result.sketch_failures) >= 2
        assert {t.status for t in result.tables} == {"ok"}
        assert (tmp_path / "primary" / "manifest.yaml").is_file()

    def test_a_keyerror_is_caught_the_same_as_a_query_failure(self, tmp_path: Path) -> None:
        adapter = _KeySketchKeyErrorAdapter(_observed_fixture())
        result = Engine(adapter, _conn_config(tmp_path), tmp_path).generate()

        assert "sketch" not in self._stats(tmp_path, "child")["columns"]["parent_id"]
        assert any(f.column == "parent_id" for f in result.sketch_failures)
        assert result.exit_code == EXIT_PARTIAL


def _folded_fixture() -> dict[str, MockTable]:
    """One parent and one child linked by a STRING-typed FK, for `normalized_cardinality`
    (SPEC 2.2.4) - each also carries a plain `id`, outside the join-key population.
    """

    def col(cardinality: int, normalized: int) -> ColumnStats:
        return ColumnStats(
            sql_type="text",
            nullable=True,
            null_count=0,
            null_rate=0.0,
            cardinality=cardinality,
            cardinality_ratio=round(cardinality / 10, 6),
            cardinality_method="exact",
            values=tuple(ValueCount(value=f"v{i}", count=1) for i in range(cardinality)),
            values_coverage=1.0,
            distribution="uniform",
        )

    parent = MockTable(
        type="table",
        namespace_path=("public", "parent"),
        ddl="CREATE TABLE public.parent (code text PRIMARY KEY, id text);\n",
        columns=[
            ColumnMeta(name="code", sql_type="text", nullable=False, default=None, ordinal=1),
            ColumnMeta(name="id", sql_type="text", nullable=False, default=None, ordinal=2),
        ],
        relationships=[],
        indexes=[],
        comments=CommentsMeta(table=None, columns={}),
        # `id`'s cardinality stays below `row_count` so its ratio misses the SPEC 4.2
        # candidate-key threshold - it must NOT enter the join-key population by accident.
        stats={"code": col(10, 10), "id": col(6, 6)},
        samples={},
        row_count=10,
        unique_keys=[UniqueKeyMeta(columns=("code",), primary=True)],
        normalized_cardinalities={"code": 8},
    )
    child = MockTable(
        type="table",
        namespace_path=("public", "child"),
        ddl="CREATE TABLE public.child (id text, parent_code text);\n",
        columns=[
            ColumnMeta(name="id", sql_type="text", nullable=False, default=None, ordinal=1),
            ColumnMeta(
                name="parent_code",
                sql_type="text",
                nullable=True,
                default=None,
                ordinal=2,
            ),
        ],
        relationships=[
            ForeignKeyMeta(
                column=("parent_code",),
                target_table="public.parent",
                target_column=("code",),
                on_delete="CASCADE",
                on_update="NO ACTION",
                constraint_name="parent_code_fk",
            ),
        ],
        indexes=[],
        comments=CommentsMeta(table=None, columns={}),
        # `id`'s cardinality stays below `row_count` for the same reason as `parent.id`.
        stats={"id": col(15, 15), "parent_code": col(10, 7)},
        samples={},
        row_count=40,
        normalized_cardinalities={"parent_code": 7},
    )

    return {"public.parent": parent, "public.child": child}


class TestNormalizedCardinality:
    """SPEC 2.2.4: the trimmed/case-folded distinct count for the join-key population - narrower
    than `sketch`'s, and unlike it a redacted column and a scoped table both stay eligible.
    """

    @staticmethod
    def _stats(tmp_path: Path, table: str) -> dict[str, Any]:
        return yaml.safe_load(
            (tmp_path / "primary" / "public" / table / "statistics.yaml").read_text(),
        )

    def test_the_referencing_column_carries_it(self, tmp_path: Path) -> None:
        Engine(MockAdapter(_folded_fixture()), _conn_config(tmp_path), tmp_path).generate()
        col = self._stats(tmp_path, "child")["columns"]["parent_code"]

        assert col["normalized_cardinality"] == 7

    def test_the_referenced_column_also_carries_it(self, tmp_path: Path) -> None:
        """The parent side of the same edge - also a declared unique key, either way eligible."""

        Engine(MockAdapter(_folded_fixture()), _conn_config(tmp_path), tmp_path).generate()
        col = self._stats(tmp_path, "parent")["columns"]["code"]

        assert col["normalized_cardinality"] == 8

    def test_a_plain_column_outside_the_population_carries_no_field(self, tmp_path: Path) -> None:
        """`id` on either table names no edge, no unique key, no candidate key."""

        Engine(MockAdapter(_folded_fixture()), _conn_config(tmp_path), tmp_path).generate()

        assert "normalized_cardinality" not in self._stats(tmp_path, "child")["columns"]["id"]
        assert "normalized_cardinality" not in self._stats(tmp_path, "parent")["columns"]["id"]

    def test_a_redacted_join_key_still_carries_it(self, tmp_path: Path) -> None:
        """Unlike `sketch`: a merge count discloses no literal (SPEC 2.2.4)."""

        conn = replace(
            _conn_config(tmp_path),
            redact=(RedactRule(columns=("*.parent_code",), with_="hash"),),
            redaction_salt="pepper",
        )
        Engine(MockAdapter(_folded_fixture()), conn, tmp_path).generate()
        col = self._stats(tmp_path, "child")["columns"]["parent_code"]

        assert col["redacted"] == "hash"
        assert col["normalized_cardinality"] == 7

    def test_a_scoped_tables_join_key_still_carries_it(self, tmp_path: Path) -> None:
        """Unlike `sketch`: a scanned-set count like every other (SPEC 2.2.8)."""

        fixture = _folded_fixture()
        fixture["public.child"] = replace(fixture["public.child"], rows_scanned=20)
        conn = replace(
            _conn_config(tmp_path),
            rules=(RuleConfig(include=("public.child",), sample=0.5),),
        )
        Engine(MockAdapter(fixture), conn, tmp_path).generate()

        assert (
            self._stats(tmp_path, "child")["columns"]["parent_code"]["normalized_cardinality"] == 7
        )

    def test_a_non_string_typed_join_key_carries_no_field(self, tmp_path: Path) -> None:
        """`_observed_fixture`'s integer FK: the type gate excludes it, unlike `sketch`."""

        Engine(MockAdapter(_observed_fixture()), _conn_config(tmp_path), tmp_path).generate()
        col = yaml.safe_load(
            (tmp_path / "primary" / "public" / "child" / "statistics.yaml").read_text(),
        )["columns"]["parent_id"]

        assert "normalized_cardinality" not in col

    def test_never_exceeds_cardinality(self, tmp_path: Path) -> None:
        Engine(MockAdapter(_folded_fixture()), _conn_config(tmp_path), tmp_path).generate()
        col = self._stats(tmp_path, "child")["columns"]["parent_code"]

        assert col["normalized_cardinality"] <= col["cardinality"]


class _NormalizedCardinalityFailingAdapter(MockAdapter):
    """Fails `compute_normalized_cardinality` for one column, as a statement timeout would."""

    def __init__(self, fixture: dict[str, MockTable], *, failing_column: str) -> None:
        super().__init__(fixture)
        self._failing_column = failing_column

    def compute_normalized_cardinality(
        self,
        fqn: str,
        column: str,
        scope: Any = None,
    ) -> int:
        if column == self._failing_column:
            raise RuntimeError("simulated normalization query timeout")

        return super().compute_normalized_cardinality(fqn, column, scope)


class TestNormalizedCardinalityFaultIsolation:
    """One column's query failure leaves the field absent for that column, never aborts the run."""

    @staticmethod
    def _stats(tmp_path: Path, table: str) -> dict[str, Any]:
        return yaml.safe_load(
            (tmp_path / "primary" / "public" / table / "statistics.yaml").read_text(),
        )

    def test_the_failing_column_carries_no_field(self, tmp_path: Path) -> None:
        adapter = _NormalizedCardinalityFailingAdapter(
            _folded_fixture(),
            failing_column="parent_code",
        )
        result = Engine(adapter, _conn_config(tmp_path), tmp_path).generate()

        assert (
            "normalized_cardinality" not in self._stats(tmp_path, "child")["columns"]["parent_code"]
        )
        assert {t.status for t in result.tables} == {"ok"}

    def test_every_other_column_still_gets_its_value(self, tmp_path: Path) -> None:
        adapter = _NormalizedCardinalityFailingAdapter(
            _folded_fixture(),
            failing_column="parent_code",
        )
        Engine(adapter, _conn_config(tmp_path), tmp_path).generate()

        assert self._stats(tmp_path, "parent")["columns"]["code"]["normalized_cardinality"] == 8

    def test_every_artifact_is_still_written(self, tmp_path: Path) -> None:
        adapter = _NormalizedCardinalityFailingAdapter(
            _folded_fixture(),
            failing_column="parent_code",
        )
        Engine(adapter, _conn_config(tmp_path), tmp_path).generate()
        prints = tmp_path / "primary"

        assert (prints / "manifest.yaml").is_file()
        assert (prints / "public" / "child" / "relationships.yaml").is_file()
        assert (prints / "public" / "parent" / "relationships.yaml").is_file()


class TestTheInferenceUniverse:
    """What one object's presence, or a failed read of it, may do to another's edges."""

    @staticmethod
    def _relationships(tmp_path: Path, *path: str) -> dict[str, Any]:
        return yaml.safe_load(
            (tmp_path / "primary" / Path(*path) / "relationships.yaml").read_text(),
        )

    def test_a_failed_key_read_cannot_hand_another_table_an_edge(self, tmp_path: Path) -> None:
        """Two tables answer to `curator` and the source is in neither namespace, so the stem is
        ambiguous and infers nothing; one of them failing its key read must not make it
        unique - a catalog failure may suppress an edge, never invent one.
        """

        engine = Engine(
            _KeyReadFailingAdapter(_ambiguous_fixture(), "b.curator"),
            _conn_config(tmp_path),
            tmp_path,
        )
        engine.generate()

        assert self._relationships(tmp_path, "fixture", "specimen_loan")["refers_to"] == []

    def test_a_failed_key_read_on_a_local_table_does_not_redirect_to_a_distant_one(
        self,
        tmp_path: Path,
    ) -> None:
        """The failing table sits in the source's own namespace and wins the first resolution
        pass outright, so a read failure that made it invisible rather than unanswering would
        let the keyed table elsewhere answer in its place.
        """

        engine = Engine(
            _KeyReadFailingAdapter(_locally_shadowed_fixture(), "fixture.curator"),
            _conn_config(tmp_path),
            tmp_path,
        )
        engine.generate()

        assert self._relationships(tmp_path, "fixture", "specimen_loan")["refers_to"] == []

    def test_a_view_referencer_is_named_in_the_targets_incoming_edges(
        self,
        tmp_path: Path,
    ) -> None:
        """A view originates edges, and the reciprocal says which object holds them."""

        Engine(
            MockAdapter(_view_source_fixture()),
            _conn_config(tmp_path),
            tmp_path,
        ).generate()
        outgoing = self._relationships(tmp_path, "public", "specimen_loan_v")["refers_to"]
        incoming = self._relationships(tmp_path, "public", "curator")["referenced_by"]

        assert [(e["target_table"], e["detection"]) for e in outgoing] == [
            ("public.curator", "inferred"),
        ]
        assert ("public.specimen_loan_v", "inferred") in [
            (e["referencer_table"], e["detection"]) for e in incoming
        ]

    def test_a_failed_key_read_says_which_object_and_which_call(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Suppressing an inferred edge marks no artifact, so the pre-pass warning is the only
        record - extraction reads declared keys again, degrading to `grain` and warning anew.
        """

        with caplog.at_level(logging.WARNING, logger="dbprint.engine.orchestrator"):
            Engine(
                _KeyReadFailingAdapter(_ambiguous_fixture(), "b.curator"),
                _conn_config(tmp_path),
                tmp_path,
            ).generate()

        assert [r.getMessage() for r in caplog.records if "b.curator" in r.getMessage()] == [
            (
                "catalog pre-pass introspect_unique_keys failed for 'b.curator': "
                "simulated catalog failure"
            ),
            "introspect_unique_keys failed for 'b.curator': RuntimeError: simulated catalog failure",
        ]

    def test_an_object_whose_columns_failed_is_named_but_never_targeted(
        self,
        tmp_path: Path,
    ) -> None:
        """It keeps its place in the universe and can never satisfy the type check."""

        healthy, failed = tmp_path / "healthy", tmp_path / "failed"
        Engine(MockAdapter(_single_target_fixture()), _conn_config(healthy), healthy).generate()
        Engine(
            _ColumnReadFailingAdapter(_single_target_fixture(), "b.curator"),
            _conn_config(failed),
            failed,
        ).generate()

        assert [
            e["target_table"]
            for e in self._relationships(healthy, "fixture", "specimen_loan")["refers_to"]
        ] == [
            "b.curator",
        ]
        assert self._relationships(failed, "fixture", "specimen_loan")["refers_to"] == []

    def test_a_view_is_not_a_target(self, tmp_path: Path) -> None:
        """The view keeps its declared key, so the refusal follows from its type, not its keys."""

        fixture = _inferrable_fixture()
        fixture["public.curator"] = replace(fixture["public.curator"], type="view")
        Engine(MockAdapter(fixture), _conn_config(tmp_path), tmp_path).generate()

        assert self._relationships(tmp_path, "public", "specimen_loan")["refers_to"] == []


class TestCatalogReadsPerRun:
    """One column read per object per run - the pre-pass's list is what extraction profiles."""

    def test_a_full_run_reads_each_objects_columns_once(self, tmp_path: Path) -> None:
        adapter = _ColumnCountingAdapter(_curator_fixture())
        Engine(adapter, _conn_config(tmp_path), tmp_path).generate()

        assert adapter.column_reads == {"public.curator": 1, "public.herbarium": 1}

    def test_a_skipped_table_is_covered_by_one_read_like_any_other(self, tmp_path: Path) -> None:
        """The pre-pass still spans the whole scope, and still at one read each."""

        _build_engine(tmp_path, _curator_fixture()).generate()
        TestPerTableFreshnessSkip._age_manifest(tmp_path / "primary" / "manifest.yaml", days=3.0)
        conn = replace(
            _conn_config(tmp_path, max_age_days=30),
            rules=(RuleConfig(include=("public.curator",), max_age_days=1),),
        )
        adapter = _ColumnCountingAdapter(_curator_fixture())
        result = Engine(adapter, conn, tmp_path).generate()

        assert {t.fqn: t.status for t in result.tables} == {
            "public.curator": "ok",
            "public.herbarium": "skipped",
        }
        assert adapter.column_reads == {"public.curator": 1, "public.herbarium": 1}

    def test_inference_off_leaves_extraction_as_the_only_reader(self, tmp_path: Path) -> None:
        """No pre-pass runs, so each profiled table is read where it always was."""

        conn = replace(_conn_config(tmp_path), infer_relationships=False)
        adapter = _ColumnCountingAdapter(_curator_fixture())
        Engine(adapter, conn, tmp_path).generate()

        assert adapter.column_reads == {"public.curator": 1, "public.herbarium": 1}

    def test_an_object_the_pre_pass_could_not_read_is_asked_again(self, tmp_path: Path) -> None:
        """An empty pre-pass entry is not an answer, so the catalog decides - and fails alone."""

        adapter = _ColumnCountingAdapter(_curator_fixture(), failing_fqn="public.curator")
        result = Engine(adapter, _conn_config(tmp_path), tmp_path).generate()

        assert {t.fqn: t.status for t in result.tables} == {
            "public.curator": "failed",
            "public.herbarium": "ok",
        }
        assert adapter.column_reads == {"public.curator": 2, "public.herbarium": 1}


class _ColumnCountingAdapter(MockAdapter):
    """Counts column reads per object, and optionally fails one: a second read of the same
    list changes no artifact, so only the call log separates one from two.
    """

    def __init__(
        self,
        fixture: dict[str, MockTable],
        *,
        failing_fqn: str | None = None,
    ) -> None:
        super().__init__(fixture)
        self.column_reads: Counter[str] = Counter()
        self._failing_fqn = failing_fqn

    def introspect_columns(self, fqn: str) -> list[ColumnMeta]:
        self.column_reads[fqn] += 1

        if fqn == self._failing_fqn:
            raise RuntimeError("simulated catalog failure")

        return super().introspect_columns(fqn)


class _KeyReadFailingAdapter(MockAdapter):
    """Fails the declared-key read for one object, as a permission or dialect error would."""

    def __init__(self, fixture: dict[str, MockTable], failing_fqn: str) -> None:
        super().__init__(fixture)
        self._failing_fqn = failing_fqn

    def introspect_unique_keys(self, fqn: str) -> list[UniqueKeyMeta]:
        if fqn == self._failing_fqn:
            raise RuntimeError("simulated catalog failure")

        return super().introspect_unique_keys(fqn)


class _ColumnReadFailingAdapter(MockAdapter):
    """Fails the column read for one object; its declared keys still answer."""

    def __init__(self, fixture: dict[str, MockTable], failing_fqn: str) -> None:
        super().__init__(fixture)
        self._failing_fqn = failing_fqn

    def introspect_columns(self, fqn: str) -> list[ColumnMeta]:
        if fqn == self._failing_fqn:
            raise RuntimeError("simulated catalog failure")

        return super().introspect_columns(fqn)


def _ambiguous_fixture() -> dict[str, MockTable]:
    """Two tables answering to `curator`, and a source in a third namespace."""

    base = _inferrable_fixture()
    curator = base["public.curator"]

    return {
        "fixture.specimen_loan": replace(
            base["public.specimen_loan"],
            namespace_path=("fixture", "specimen_loan"),
        ),
        "core.curator": replace(curator, namespace_path=("core", "curator")),
        "b.curator": replace(curator, namespace_path=("b", "curator")),
    }


def _locally_shadowed_fixture() -> dict[str, MockTable]:
    """A same-namespace `curator` beside the source, and a keyed one elsewhere: the first
    resolution pass returns the local one outright, no cross-namespace pass reached.
    """

    base = _inferrable_fixture()
    curator = base["public.curator"]

    return {
        "fixture.specimen_loan": replace(
            base["public.specimen_loan"],
            namespace_path=("fixture", "specimen_loan"),
        ),
        "fixture.curator": replace(curator, namespace_path=("fixture", "curator")),
        "core.curator": replace(curator, namespace_path=("core", "curator")),
    }


def _single_target_fixture() -> dict[str, MockTable]:
    """One table answering to `curator`, so the stem resolves rather than refusing."""

    fixture = _ambiguous_fixture()
    del fixture["core.curator"]

    return fixture


def _view_source_fixture() -> dict[str, MockTable]:
    """The inferrable pair plus a view over the source, carrying the same columns."""

    base = _inferrable_fixture()

    return {
        **base,
        "public.specimen_loan_v": replace(
            base["public.specimen_loan"],
            type="view",
            namespace_path=("public", "specimen_loan_v"),
        ),
    }


class TestWrongShapeBaselineArtifacts:
    """A corrupt committed artifact costs its own table a baseline, not the run: the run that
    survives it overwrites it with a good one.
    """

    def test_a_statistics_yaml_holding_a_list_does_not_abort_the_run(
        self,
        tmp_path: Path,
    ) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()
        stats = tmp_path / "primary" / "public" / "curator" / "statistics.yaml"
        stats.write_text("- a\n- b\n")

        result = _build_engine(tmp_path, _curator_fixture()).generate(GenerateRequest(force=True))

        assert result.summary.failed == 0
        assert result.summary.ok == 2

    def test_the_corrupt_artifact_is_rewritten_valid(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()
        stats = tmp_path / "primary" / "public" / "curator" / "statistics.yaml"
        stats.write_text("- a\n- b\n")
        _build_engine(tmp_path, _curator_fixture()).generate(GenerateRequest(force=True))

        assert isinstance(yaml.safe_load(stats.read_text()), dict)

    def test_a_relationships_yaml_holding_a_scalar_does_not_abort_the_run(
        self,
        tmp_path: Path,
    ) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()
        (tmp_path / "primary" / "public" / "curator" / "relationships.yaml").write_text("nope\n")

        result = _build_engine(tmp_path, _curator_fixture()).generate(GenerateRequest(force=True))

        assert result.summary.failed == 0

    def test_a_manifest_holding_a_sequence_runs_as_a_first_run(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()
        (tmp_path / "primary" / "manifest.yaml").write_text("- one\n- two\n")

        result = _build_engine(tmp_path, _curator_fixture()).generate()

        assert result.summary.failed == 0
        assert result.summary.ok == 2

    def test_a_manifest_whose_tables_key_is_empty_runs_as_a_first_run(
        self,
        tmp_path: Path,
    ) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()
        (tmp_path / "primary" / "manifest.yaml").write_text("format_version: 1\ntables:\n")

        result = _build_engine(tmp_path, _curator_fixture()).generate()

        assert result.summary.failed == 0
        assert result.summary.ok == 2

    def test_the_ignored_artifact_is_named(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()
        stats = tmp_path / "primary" / "public" / "curator" / "statistics.yaml"
        stats.write_text("- a\n- b\n")

        with caplog.at_level(logging.WARNING):
            _build_engine(tmp_path, _curator_fixture()).generate(GenerateRequest(force=True))

        assert str(stats) in caplog.text


def _restated_curator_fixture() -> dict[str, MockTable]:
    """The same shape, more rows: only statistics move.

    `id` grows with the row count to hold ratio 1.0; dropping it below the candidate-key
    threshold would be a grain signal, not the data movement this isolates.
    """

    fixture = _curator_fixture()
    curator = fixture["public.curator"]
    fixture["public.curator"] = replace(
        curator,
        stats={
            **curator.stats,
            "id": replace(curator.stats["id"], cardinality=120),
            "herbarium_id": replace(
                curator.stats["herbarium_id"],
                null_count=14,
                null_rate=0.116667,
                cardinality=24,
                cardinality_ratio=0.2,
            ),
        },
        row_count=120,
    )

    return fixture


def _row_count_only_fixture(row_count: int) -> dict[str, MockTable]:
    """One table, one low-cardinality column whose stats never move - only `row_count` varies.

    `cardinality` (3) stays far below `enumeration_threshold` at either row count, so growth
    alone cannot flip the classification the way it could for a unique column (SPEC 4.2).
    """

    return {
        "public.curation_event": MockTable(
            type="table",
            namespace_path=("public", "curation_event"),
            ddl="CREATE TABLE public.curation_event (kind text);\n",
            columns=[
                ColumnMeta(name="kind", sql_type="text", nullable=False, default=None, ordinal=1),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "kind": ColumnStats(
                    sql_type="text",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=3,
                    cardinality_ratio=0.03,
                    cardinality_method="exact",
                ),
            },
            samples={"kind": ["a", "b", "c"]},
            row_count=row_count,
        ),
    }


class TestExitCodeSeparatesDataFromShape:
    """EXIT_DRIFT fires only for shape changes, never data movement."""

    @staticmethod
    def _baseline(tmp_path: Path) -> None:
        _build_engine(tmp_path, _curator_fixture()).generate()

    def test_data_movement_alone_exits_ok(self, tmp_path: Path) -> None:
        self._baseline(tmp_path)

        result = _build_engine(tmp_path, _restated_curator_fixture()).generate(
            GenerateRequest(force=True),
        )

        assert result.diff_summary.statistics_drifted > 0
        assert result.exit_code == EXIT_OK

    def test_row_count_movement_alone_exits_ok(self, tmp_path: Path) -> None:
        _build_engine(tmp_path, _row_count_only_fixture(100)).generate()

        result = _build_engine(tmp_path, _row_count_only_fixture(120)).generate(
            GenerateRequest(force=True),
        )

        diff = yaml.safe_load((tmp_path / "primary" / "diff.yaml").read_text())
        kinds = {c["kind"] for c in diff["changes"]}

        assert kinds == {"table_row_count_changed"}
        assert result.exit_code == EXIT_OK

    def test_schema_movement_alone_exits_drift(self, tmp_path: Path) -> None:
        self._baseline(tmp_path)

        result = _build_engine(tmp_path, _drifted_curator_fixture()).generate(
            GenerateRequest(force=True),
        )

        assert result.diff_summary.columns_added > 0
        assert result.exit_code == EXIT_DRIFT

    def test_data_movement_beside_schema_movement_exits_drift(self, tmp_path: Path) -> None:
        """The schema change decides; the statistics riding with it do not soften it."""

        self._baseline(tmp_path)
        fixture = _drifted_curator_fixture()
        fixture["public.curator"] = replace(fixture["public.curator"], row_count=120)

        result = _build_engine(tmp_path, fixture).generate(GenerateRequest(force=True))

        assert result.diff_summary.statistics_drifted > 0
        assert result.diff_summary.columns_added > 0
        assert result.exit_code == EXIT_DRIFT

    def test_the_events_are_recorded_whatever_the_exit_code(self, tmp_path: Path) -> None:
        """The artifact stays the complete account; only the gate is selective."""

        self._baseline(tmp_path)
        _build_engine(tmp_path, _restated_curator_fixture()).generate(GenerateRequest(force=True))
        diff = yaml.safe_load((tmp_path / "primary" / "diff.yaml").read_text())
        kinds = {c["kind"] for c in diff["changes"]}

        assert kinds == {"statistic_changed", "table_row_count_changed"}

    def test_a_failed_table_still_outranks_a_clean_shape(self, tmp_path: Path) -> None:
        self._baseline(tmp_path)
        engine = Engine(
            _PartiallyFailingAdapter(_restated_curator_fixture()),
            _conn_config(tmp_path),
            tmp_path,
        )

        assert engine.generate(GenerateRequest(force=True)).exit_code == EXIT_PARTIAL


class _PartiallyFailingAdapter(MockAdapter):
    """One table fails, the other succeeds - the shape a partial run takes."""

    def extract_ddl(self, fqn: str) -> str:
        if fqn == "public.herbarium":
            raise RuntimeError("simulated fault")

        return super().extract_ddl(fqn)


class _TraceContextRecordingAdapter(MockAdapter):
    """Records the statement-trace context visible at each traced call."""

    def __init__(self, fixture: dict[str, MockTable]) -> None:
        super().__init__(fixture)
        self.seen: dict[str, tuple[str, str, str]] = {}
        self.seen_columns: dict[str, tuple[str, str, str]] = {}
        self.seen_unique_keys: dict[str, tuple[str, str, str]] = {}

    def extract_ddl(self, fqn: str) -> str:
        self.seen[fqn] = self._context()

        return super().extract_ddl(fqn)

    def introspect_columns(self, fqn: str) -> list[ColumnMeta]:
        self.seen_columns[fqn] = self._context()

        return super().introspect_columns(fqn)

    def introspect_unique_keys(self, fqn: str) -> list[UniqueKeyMeta]:
        self.seen_unique_keys[fqn] = self._context()

        return super().introspect_unique_keys(fqn)

    def _context(self) -> tuple[str, str, str]:
        return (
            orchestrator.trace_context.connection.get(),
            orchestrator.trace_context.fqn.get(),
            orchestrator.trace_context.phase.get(),
        )


class TestStatementTraceContext:
    """The engine sets connection/fqn/phase so exec_query's own trace can read them."""

    def test_each_table_sees_its_own_fqn_and_the_extract_ddl_phase(self, tmp_path: Path) -> None:
        adapter = _TraceContextRecordingAdapter(_curator_fixture())
        engine = Engine(adapter, _conn_config(tmp_path), tmp_path)
        engine.generate()

        assert adapter.seen == {
            "public.curator": ("primary", "public.curator", "extract_ddl"),
            "public.herbarium": ("primary", "public.herbarium", "extract_ddl"),
        }

    def test_the_catalog_pre_pass_tags_its_own_operation_too(self, tmp_path: Path) -> None:
        """`_build_inventory` runs ahead of the per-table loop, on the same tag-setting seam."""

        adapter = _TraceContextRecordingAdapter(_curator_fixture())
        engine = Engine(adapter, _conn_config(tmp_path), tmp_path)
        engine.generate()

        assert adapter.seen_columns == {
            "public.curator": ("primary", "public.curator", "introspect_columns"),
            "public.herbarium": ("primary", "public.herbarium", "introspect_columns"),
        }
        assert adapter.seen_unique_keys == {
            "public.curator": ("primary", "public.curator", "introspect_unique_keys"),
            "public.herbarium": ("primary", "public.herbarium", "introspect_unique_keys"),
        }

    def test_tags_are_cleared_once_the_run_completes(self, tmp_path: Path) -> None:
        adapter = _TraceContextRecordingAdapter(_curator_fixture())
        engine = Engine(adapter, _conn_config(tmp_path), tmp_path)
        engine.generate()

        assert orchestrator.trace_context.connection.get() == ""
        assert orchestrator.trace_context.fqn.get() == ""
        assert orchestrator.trace_context.phase.get() == ""


def _grain_search_fixture() -> dict[str, MockTable]:
    """A single table with two non-unique, null-free columns and a declared key.

    Neither column is a candidate key, so `_compute_grain`'s measured search actually runs.
    """

    return {
        "public.herbarium": MockTable(
            type="table",
            namespace_path=("public", "herbarium"),
            ddl="CREATE TABLE public.herbarium (a integer NOT NULL, b integer NOT NULL);\n",
            columns=[
                ColumnMeta(name="a", sql_type="integer", nullable=False, default=None, ordinal=1),
                ColumnMeta(name="b", sql_type="integer", nullable=False, default=None, ordinal=2),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                name: ColumnStats(
                    sql_type="integer",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=10,
                    cardinality_ratio=0.1,
                    cardinality_method="exact",
                    values=tuple(ValueCount(value=i, count=10) for i in range(10)),
                    values_coverage=1.0,
                    distribution="uniform",
                )
                for name in ("a", "b")
            },
            samples={},
            row_count=100,
            unique_keys=[UniqueKeyMeta(columns=("a",))],
        ),
    }


class _RelationshipsFailingAdapter(MockAdapter):
    """Fails the relationship read for one object, as a permission or dialect error would."""

    def introspect_relationships(self, fqn: str) -> list[ForeignKeyMeta]:
        if fqn == "public.herbarium":
            raise RuntimeError("simulated catalog failure")

        return super().introspect_relationships(fqn)


class _IndexesFailingAdapter(MockAdapter):
    """Fails the index read for one object."""

    def introspect_indexes(self, fqn: str) -> list[IndexMeta]:
        if fqn == "public.herbarium":
            raise RuntimeError("simulated catalog failure")

        return super().introspect_indexes(fqn)


class _CommentsFailingAdapter(MockAdapter):
    """Fails the comment read for one object."""

    def extract_comments(self, fqn: str) -> CommentsMeta:
        if fqn == "public.herbarium":
            raise RuntimeError("simulated catalog failure")

        return super().extract_comments(fqn)


class _PhysicalLayoutFailingAdapter(MockAdapter):
    """Fails the physical-layout read for one object."""

    def introspect_physical_layout(self, fqn: str) -> PhysicalLayout | None:
        if fqn == "public.herbarium":
            raise RuntimeError("simulated catalog failure")

        return super().introspect_physical_layout(fqn)


class _NullPatternsFailingAdapter(MockAdapter):
    """Fails the null-pattern scan for one object."""

    def compute_null_patterns(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        config: StatisticsConfig,
        counts: TableCounts,
        base: dict[str, BaseStats],
        scope: TableScope | None = None,
    ) -> NullPatterns | None:
        if fqn == "public.herbarium":
            raise RuntimeError("simulated catalog failure")

        return super().compute_null_patterns(fqn, columns, config, counts, base, scope)


class TestCatalogReadDegrade:
    """Each optional catalog read omits its own field and leaves the table profiled rather than
    failing it - `introspect_relationships` also stops the manifest declaring an unwritten file.
    """

    def test_a_failed_relationship_read_still_fails_a_table(self, tmp_path: Path) -> None:
        """SPEC 1.4 makes `relationships.yaml` REQUIRED for a table: omitting it says "plain view"
        (SPEC 7.3) and an empty one asserts no foreign keys, so the only honest outcome is failure.
        """

        result = Engine(
            _RelationshipsFailingAdapter(_curator_fixture()),
            _conn_config(tmp_path),
            tmp_path,
        ).generate()

        assert {t.fqn: t.status for t in result.tables} == {
            "public.curator": "ok",
            "public.herbarium": "failed",
        }

    def test_a_failed_relationship_read_degrades_a_plain_view(self, tmp_path: Path) -> None:
        """The one object SPEC 1.4 lets go without the file, so the degrade is legal - and the
        manifest must not declare a file this run did not write.
        """

        fixture = _curator_fixture()
        fixture["public.herbarium"] = replace(fixture["public.herbarium"], type="view")
        result = Engine(
            _RelationshipsFailingAdapter(fixture),
            _conn_config(tmp_path),
            tmp_path,
        ).generate()

        assert next(t for t in result.tables if t.fqn == "public.herbarium").status == "ok"

        manifest = yaml.safe_load((tmp_path / "primary" / "manifest.yaml").read_text())

        assert "relationships" not in manifest["tables"]["public.herbarium"]["artifacts"]
        assert not (tmp_path / "primary" / "public" / "herbarium" / "relationships.yaml").exists()
        assert "relationships" in manifest["tables"]["public.curator"]["artifacts"]
        assert (tmp_path / "primary" / "public" / "curator" / "relationships.yaml").exists()

    def test_a_failed_index_read_omits_indexes_and_leaves_the_table_profiled(
        self,
        tmp_path: Path,
    ) -> None:
        result = Engine(
            _IndexesFailingAdapter(_curator_fixture()),
            _conn_config(tmp_path),
            tmp_path,
        ).generate()

        assert next(t for t in result.tables if t.fqn == "public.herbarium").status == "ok"
        manifest = yaml.safe_load((tmp_path / "primary" / "manifest.yaml").read_text())
        assert "statistics" in manifest["tables"]["public.herbarium"]["artifacts"]

    def test_a_failed_comment_read_omits_descriptions_and_leaves_the_table_profiled(
        self,
        tmp_path: Path,
    ) -> None:
        result = Engine(
            _CommentsFailingAdapter(_curator_fixture()),
            _conn_config(tmp_path),
            tmp_path,
        ).generate()

        assert next(t for t in result.tables if t.fqn == "public.herbarium").status == "ok"

    def test_a_failed_physical_layout_read_leaves_the_table_profiled(self, tmp_path: Path) -> None:
        result = Engine(
            _PhysicalLayoutFailingAdapter(_curator_fixture()),
            _conn_config(tmp_path),
            tmp_path,
        ).generate()

        assert next(t for t in result.tables if t.fqn == "public.herbarium").status == "ok"
        stats = yaml.safe_load(
            (tmp_path / "primary" / "public" / "herbarium" / "statistics.yaml").read_text(),
        )
        assert "physical_layout" not in stats

    def test_a_failed_null_pattern_scan_leaves_the_table_profiled(self, tmp_path: Path) -> None:
        result = Engine(
            _NullPatternsFailingAdapter(_curator_fixture()),
            _conn_config(tmp_path),
            tmp_path,
        ).generate()

        assert next(t for t in result.tables if t.fqn == "public.herbarium").status == "ok"
        stats = yaml.safe_load(
            (tmp_path / "primary" / "public" / "herbarium" / "statistics.yaml").read_text(),
        )
        assert "null_patterns" not in stats

    def test_a_failed_unique_key_read_loses_the_declared_key_but_not_the_table(
        self,
        tmp_path: Path,
    ) -> None:
        result = Engine(
            _KeyReadFailingAdapter(_grain_search_fixture(), "public.herbarium"),
            _conn_config(tmp_path),
            tmp_path,
        ).generate()

        assert next(t for t in result.tables if t.fqn == "public.herbarium").status == "ok"
        stats = yaml.safe_load(
            (tmp_path / "primary" / "public" / "herbarium" / "statistics.yaml").read_text(),
        )
        keys = stats["grain"]["keys"]

        assert not any(k["detection"] == "declared" for k in keys)

    def test_a_failed_unique_key_read_marks_the_measured_search_incomplete(
        self,
        tmp_path: Path,
    ) -> None:
        """A search run without knowing every declared key cannot claim exhaustiveness -
        `search.exhausted` reads `false`, never `true` or absent.
        """

        result = Engine(
            _KeyReadFailingAdapter(_grain_search_fixture(), "public.herbarium"),
            _conn_config(tmp_path),
            tmp_path,
        ).generate()

        assert next(t for t in result.tables if t.fqn == "public.herbarium").status == "ok"
        stats = yaml.safe_load(
            (tmp_path / "primary" / "public" / "herbarium" / "statistics.yaml").read_text(),
        )

        assert stats["grain"]["search"]["exhausted"] is False

    def test_an_unaffected_table_still_reports_its_declared_key(self, tmp_path: Path) -> None:
        """The control: the same fixture, no failing read, still reports `a` as declared."""

        result = Engine(
            MockAdapter(_grain_search_fixture()),
            _conn_config(tmp_path),
            tmp_path,
        ).generate()

        assert next(t for t in result.tables if t.fqn == "public.herbarium").status == "ok"
        stats = yaml.safe_load(
            (tmp_path / "primary" / "public" / "herbarium" / "statistics.yaml").read_text(),
        )
        keys = stats["grain"]["keys"]

        assert any(k["detection"] == "declared" and k["columns"] == ["a"] for k in keys)
        assert stats["grain"]["search"]["exhausted"] is True
