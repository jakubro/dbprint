"""What the engine writes for a table's declared clustering/partitioning key (SPEC 2.2.11).

The introspection is the adapters'; these cover the half after it - the table block and the
per-column marker reaching the artifact consistently, and `dbprint context` naming the
columns to filter on without a reader opening the DDL.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from dbprint.adapters import (
    ColumnMeta,
    ColumnStats,
    CommentsMeta,
    MockAdapter,
    MockTable,
    PhysicalLayout,
    PhysicalLayoutKey,
)
from dbprint.config import ConnectionConfig
from dbprint.engine import Engine, GenerateRequest
from dbprint.engine.context_assembler import AssemblyOptions, assemble


CLUSTER = PhysicalLayout(
    mechanism="cluster",
    keys=(
        PhysicalLayoutKey(expression="vault_id", column="vault_id"),
        PhysicalLayoutKey(expression="logged_at::date", column="logged_at"),
    ),
)

PARTITION_ON_AN_EXPRESSION = PhysicalLayout(
    mechanism="partition",
    keys=(PhysicalLayoutKey(expression="date_trunc('day', logged_at)", column=None),),
)

SORT = PhysicalLayout(
    mechanism="sort",
    keys=(PhysicalLayoutKey(expression="vault_id", column="vault_id"),),
)


class TestEmission:
    def test_the_block_carries_the_mechanism_and_every_key_in_order(
        self,
        tmp_path: Path,
    ) -> None:
        payload = _generate(tmp_path, CLUSTER)

        assert payload["physical_layout"] == {
            "mechanism": "cluster",
            "keys": [
                {"expression": "vault_id", "column": "vault_id"},
                {"expression": "logged_at::date", "column": "logged_at"},
            ],
        }

    def test_the_block_sits_between_the_file_head_and_the_columns(self, tmp_path: Path) -> None:
        """SPEC 2.2.1 lists it there, and a reader scanning the file expects it there."""

        _generate(tmp_path, CLUSTER)
        text = (tmp_path / "w" / "seedbank" / "specimen_loan" / "statistics.yaml").read_text()

        assert text.index("physical_layout:") < text.index("columns:")
        assert text.index("row_count:") < text.index("physical_layout:")

    def test_a_key_with_a_recovered_column_marks_that_column(self, tmp_path: Path) -> None:
        payload = _generate(tmp_path, CLUSTER)

        assert payload["columns"]["vault_id"]["physical_layout_key"] is True
        assert payload["columns"]["logged_at"]["physical_layout_key"] is True

    def test_an_unresolvable_expression_marks_no_column(self, tmp_path: Path) -> None:
        """A function call names no single column - nothing to flag in the cardinality table."""

        payload = _generate(tmp_path, PARTITION_ON_AN_EXPRESSION)

        assert "physical_layout_key" not in payload["columns"]["logged_at"]

    def test_a_column_outside_the_key_carries_no_marker(self, tmp_path: Path) -> None:
        payload = _generate(tmp_path, CLUSTER)

        assert "physical_layout_key" not in payload["columns"]["condition"]

    def test_an_undeclared_key_leaves_no_block_and_no_marker(self, tmp_path: Path) -> None:
        """Absence means "not clustered", never "not checked"."""

        payload = _generate(tmp_path, None)

        assert "physical_layout" not in payload
        assert all("physical_layout_key" not in c for c in payload["columns"].values())

    def test_a_sort_mechanism_round_trips_like_the_other_two(self, tmp_path: Path) -> None:
        """Redshift's SORTKEY: a third `mechanism` value, same shape as cluster/partition."""

        payload = _generate(tmp_path, SORT)

        assert payload["physical_layout"] == {
            "mechanism": "sort",
            "keys": [{"expression": "vault_id", "column": "vault_id"}],
        }


class _LayoutReadFailingAdapter(MockAdapter):
    """Fails `introspect_physical_layout`, as a missing catalog grant would."""

    def introspect_physical_layout(self, fqn: str) -> PhysicalLayout | None:
        raise RuntimeError("simulated catalog failure")


class TestAFailedRead:
    """SPEC 2.2.11 reads an absent block as "not clustered", so a run that could not look must
    say so - and a later run must not then report the real key as newly declared.
    """

    def test_the_file_names_the_block_unmeasured(self, tmp_path: Path) -> None:
        payload = _generate(tmp_path, CLUSTER, adapter=_LayoutReadFailingAdapter)

        assert "physical_layout" not in payload
        assert payload["unmeasured"] == ["physical_layout"]

    def test_a_measured_run_names_nothing(self, tmp_path: Path) -> None:
        """The control: the same fixture read successfully carries the block and no marker."""

        payload = _generate(tmp_path, CLUSTER)

        assert "unmeasured" not in payload

    def test_the_next_run_reports_no_drift_against_the_failed_one(self, tmp_path: Path) -> None:
        """The baseline is hydrated from the artifact, so only its own marker tells a failed read
        from a key that was really dropped.
        """

        _generate(tmp_path, CLUSTER, adapter=_LayoutReadFailingAdapter)
        diff = _regenerate(tmp_path, CLUSTER)

        assert [c for c in diff["changes"] if c["kind"] == "physical_layout_changed"] == []

    def test_a_genuine_change_against_a_measured_baseline_still_fires(self, tmp_path: Path) -> None:
        """The control for the suppression: without a marker, losing the key is real drift."""

        _generate(tmp_path, CLUSTER)
        diff = _regenerate(tmp_path, None)

        assert [c["kind"] for c in diff["changes"] if c["kind"] == "physical_layout_changed"] == [
            "physical_layout_changed",
        ]


class TestContextRendering:
    def test_a_cluster_key_renders_its_label_and_columns(self, tmp_path: Path) -> None:
        text = _context(tmp_path, CLUSTER, fmt="md")

        assert "## Physical layout" in text
        assert "Clustered by: vault_id, logged_at::date" in text

    def test_a_partition_key_renders_the_partition_label(self, tmp_path: Path) -> None:
        text = _context(tmp_path, PARTITION_ON_AN_EXPRESSION, fmt="md")

        assert "Partitioned by: date_trunc('day', logged_at)" in text

    def test_a_sort_key_renders_the_sort_label(self, tmp_path: Path) -> None:
        text = _context(tmp_path, SORT, fmt="md")

        assert "Sorted by: vault_id" in text

    def test_a_table_without_a_declared_key_renders_no_section(self, tmp_path: Path) -> None:
        text = _context(tmp_path, None, fmt="md")

        assert "Physical layout" not in text

    def test_the_structured_formats_carry_the_block_whole(self, tmp_path: Path) -> None:
        """json/yaml pass the statistics through, so the block needs no second projection."""

        payload = yaml.safe_load(_context(tmp_path, CLUSTER, fmt="yaml"))

        assert payload["statistics"]["physical_layout"]["mechanism"] == "cluster"


def _generate(
    tmp_path: Path,
    layout: PhysicalLayout | None,
    adapter: type[MockAdapter] = MockAdapter,
) -> dict[str, Any]:
    Engine(adapter(_fixture(layout)), _conn(tmp_path), tmp_path).generate()

    return yaml.safe_load(
        (tmp_path / "w" / "seedbank" / "specimen_loan" / "statistics.yaml").read_text(),
    )


def _regenerate(tmp_path: Path, layout: PhysicalLayout | None) -> dict[str, Any]:
    """Re-profile over a committed print and read the diff that second run wrote."""

    Engine(MockAdapter(_fixture(layout)), _conn(tmp_path), tmp_path).generate(
        GenerateRequest(force=True),
    )

    return yaml.safe_load((tmp_path / "w" / "diff.yaml").read_text())


def _conn(tmp_path: Path) -> ConnectionConfig:
    return ConnectionConfig(
        name="w",
        adapter="postgres",
        output=tmp_path,
        infer_relationships=False,
    )


def _context(tmp_path: Path, layout: PhysicalLayout | None, fmt: str) -> str:
    _generate(tmp_path, layout)
    root = tmp_path / "w"
    manifest = yaml.safe_load((root / "manifest.yaml").read_text())

    return assemble(
        manifest,
        root,
        ["seedbank.specimen_loan"],
        AssemblyOptions(format=fmt, include_ddl=False),
    ).text


def _fixture(layout: PhysicalLayout | None) -> dict[str, MockTable]:
    """A three-column table; `vault_id`/`logged_at` are the declared key, `condition` is not."""

    def _column() -> ColumnStats:
        return ColumnStats(
            sql_type="text",
            nullable=False,
            null_count=0,
            null_rate=0.0,
            cardinality=4,
            cardinality_ratio=0.04,
            cardinality_method="exact",
            values=(),
            values_coverage=1.0,
            distribution="uniform",
        )

    return {
        "seedbank.specimen_loan": MockTable(
            type="table",
            namespace_path=("seedbank", "specimen_loan"),
            ddl=(
                "CREATE TABLE seedbank.specimen_loan (vault_id text, logged_at text, condition text);\n"
            ),
            columns=[
                ColumnMeta(
                    name="vault_id",
                    sql_type="text",
                    nullable=False,
                    default=None,
                    ordinal=1,
                ),
                ColumnMeta(
                    name="logged_at",
                    sql_type="text",
                    nullable=False,
                    default=None,
                    ordinal=2,
                ),
                ColumnMeta(
                    name="condition",
                    sql_type="text",
                    nullable=False,
                    default=None,
                    ordinal=3,
                ),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "vault_id": _column(),
                "logged_at": _column(),
                "condition": _column(),
            },
            samples={},
            physical_layout=layout,
            row_count=100,
        ),
    }
