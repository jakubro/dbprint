"""What the engine writes for a `detection: measured` relationship edge (SPEC 2.3.11) - the
algorithm is covered in isolation elsewhere; these cover the wiring around it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from dbprint.adapters import (
    ColumnMeta,
    CommentsMeta,
    ForeignKeyMeta,
    MockAdapter,
    MockTable,
    UniqueKeyMeta,
    ValueCount,
)
from dbprint.adapters.base import ColumnStats
from dbprint.config import ConnectionConfig
from dbprint.engine import Engine, GenerateRequest


ROW_COUNT = 60


def _column(cardinality: int, values: tuple[ValueCount, ...]) -> ColumnStats:
    return ColumnStats(
        sql_type="integer",
        nullable=False,
        null_count=0,
        null_rate=0.0,
        cardinality=cardinality,
        cardinality_ratio=cardinality / ROW_COUNT,
        cardinality_method="exact",
        values=values,
        values_coverage=1.0,
        distribution="uniform",
    )


def _values(ints: range) -> tuple[ValueCount, ...]:
    return tuple(ValueCount(value=i, count=1) for i in ints)


def _fixture(
    *,
    # Strictly below the unique parent's `ROW_COUNT`: SPEC 2.3.11 requires the parent's cardinality
    # to be the larger, so an equal pair proposes nothing in either direction.
    child_cardinality: int = ROW_COUNT - 5,
    parent_unique: bool = True,
    declared_edge: bool = False,
    second_child: bool = False,
) -> dict[str, MockTable]:
    # A non-key parent still needs `cardinality_ratio < 0.9999` (SPEC 4.2), or the recomputed
    # `candidate_key` makes it eligible anyway - one row short of `cardinality` clears that.
    parent_cardinality = ROW_COUNT if parent_unique else ROW_COUNT - 5
    b_columns = [ColumnMeta(name="id", sql_type="integer", nullable=False, default=None, ordinal=1)]
    b_stats = {"id": _column(parent_cardinality, _values(range(parent_cardinality)))}
    b_unique_keys = [UniqueKeyMeta(columns=("id",), primary=True)] if parent_unique else []

    if second_child:
        # Offset ranges, disjoint from `id`'s - `code` must qualify only `linked_ref2`, never
        # `linked_ref` too, or the accumulation this fixture proves would over-count edges.
        b_columns.append(
            ColumnMeta(name="code", sql_type="integer", nullable=False, default=None, ordinal=2),
        )
        b_stats["code"] = _column(
            parent_cardinality,
            _values(range(1000, 1000 + parent_cardinality)),
        )

        if parent_unique:
            b_unique_keys.append(UniqueKeyMeta(columns=("code",), primary=False))

    b = MockTable(
        type="table",
        namespace_path=("public", "b"),
        ddl="CREATE TABLE public.b (id integer);\n",
        columns=b_columns,
        relationships=[],
        indexes=[],
        comments=CommentsMeta(table=None, columns={}),
        stats=b_stats,
        samples={},
        row_count=ROW_COUNT,
        unique_keys=b_unique_keys,
    )
    relationships = (
        [
            ForeignKeyMeta(
                column=("linked_ref",),
                target_table="public.b",
                target_column=("id",),
                on_delete="NO ACTION",
                on_update="NO ACTION",
                constraint_name="a_linked_ref_fkey",
                detection="declared",
            ),
        ]
        if declared_edge
        else []
    )
    a_columns = [
        ColumnMeta(name="linked_ref", sql_type="integer", nullable=False, default=None, ordinal=1),
    ]
    a_stats = {"linked_ref": _column(child_cardinality, _values(range(child_cardinality)))}

    if second_child:
        a_columns.append(
            ColumnMeta(
                name="linked_ref2",
                sql_type="integer",
                nullable=False,
                default=None,
                ordinal=2,
            ),
        )
        a_stats["linked_ref2"] = _column(
            child_cardinality,
            _values(range(1000, 1000 + child_cardinality)),
        )

    a = MockTable(
        type="table",
        namespace_path=("public", "a"),
        ddl="CREATE TABLE public.a (linked_ref integer);\n",
        columns=a_columns,
        relationships=relationships,
        indexes=[],
        comments=CommentsMeta(table=None, columns={}),
        stats=a_stats,
        samples={},
        # Fixed, independent of `child_cardinality`: a row_count tracking it would make the
        # ratio 1.0 and the column its own candidate key, whatever this fixture isolates.
        row_count=ROW_COUNT,
    )

    return {"public.b": b, "public.a": a}


def _generate(tmp_path: Path, **kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    conn = ConnectionConfig(
        name="w",
        adapter="postgres",
        output=tmp_path,
        infer_relationships=False,
    )
    Engine(MockAdapter(_fixture(**kwargs)), conn, tmp_path).generate()
    root = tmp_path / "w" / "public"
    a = yaml.safe_load((root / "a" / "relationships.yaml").read_text())
    b = yaml.safe_load((root / "b" / "relationships.yaml").read_text())

    return a, b


class TestEligibility:
    def test_a_child_below_the_enumeration_threshold_is_not_proposed(
        self,
        tmp_path: Path,
    ) -> None:
        """A 30-value column is enumerable in full - a lookup, not a reference (floor 1)."""

        a, _ = _generate(tmp_path, child_cardinality=30)

        assert a["refers_to"] == []

    def test_a_non_key_parent_is_not_proposed_into(self, tmp_path: Path) -> None:
        """The parent carries no single-column key and no candidate_key (floor 2)."""

        a, _ = _generate(tmp_path, parent_unique=False)

        assert a["refers_to"] == []


class TestMultipleChildColumns:
    def test_two_eligible_columns_of_one_table_both_publish_their_edges(
        self,
        tmp_path: Path,
    ) -> None:
        """The per-table storage line one eligible column cannot exercise - a second one must not
        overwrite the first's entry in `relationships.yaml`.
        """

        a, _ = _generate(tmp_path, second_child=True)

        # Ordered by target column ("code" < "id"), per `infer_value_derived_edges`'s sort key.
        assert [(e["column"], e["target_column"]) for e in a["refers_to"]] == [
            (["linked_ref2"], ["code"]),
            (["linked_ref"], ["id"]),
        ]


class TestDuplicateSuppression:
    def test_a_pair_already_declared_is_not_also_proposed_as_measured(
        self,
        tmp_path: Path,
    ) -> None:
        a, _ = _generate(tmp_path, declared_edge=True)

        assert [e["detection"] for e in a["refers_to"]] == ["declared"]


class TestReciprocity:
    def test_a_measured_edge_reaches_the_targets_referenced_by(self, tmp_path: Path) -> None:
        a, b = _generate(tmp_path)

        assert [e["detection"] for e in a["refers_to"]] == ["measured"]
        incoming = b["referenced_by"]
        assert len(incoming) == 1
        assert incoming[0]["detection"] == "measured"
        assert incoming[0]["referencer_table"] == "public.a"


class TestNoReferentialActionFiller:
    def test_a_measured_edge_carries_no_on_delete_on_update_or_constraint_name(
        self,
        tmp_path: Path,
    ) -> None:
        a, _ = _generate(tmp_path)

        edge = a["refers_to"][0]
        assert "on_delete" not in edge
        assert "on_update" not in edge
        assert "constraint_name" not in edge


class TestDiffExclusion:
    def test_a_measured_edge_produces_no_diff_event_across_two_runs(self, tmp_path: Path) -> None:
        """SPEC 2.6.6: a `detection: measured` edge is dropped from both sides before diff runs."""

        conn = ConnectionConfig(
            name="w",
            adapter="postgres",
            output=tmp_path,
            infer_relationships=False,
        )
        Engine(MockAdapter(_fixture()), conn, tmp_path).generate()
        Engine(MockAdapter(_fixture()), conn, tmp_path).generate(GenerateRequest(force=True))
        diff = yaml.safe_load((tmp_path / "w" / "diff.yaml").read_text())
        events = [
            e
            for e in diff.get("events", [])
            if e.get("kind")
            in {"relationship_added", "relationship_removed", "relationship_modified"}
        ]

        assert events == []
