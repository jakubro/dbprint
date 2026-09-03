"""Diff computation tests - 19 v1 change kinds."""

from __future__ import annotations

import dataclasses
from typing import Any

from dbprint.adapters.base import ColumnStats
from dbprint.engine import diff as diff_module
from dbprint.engine.diff import (
    ColumnState,
    DiffSelectors,
    FkState,
    GrainKeyState,
    IndexState,
    PhysicalLayoutKeyState,
    PhysicalLayoutState,
    TableGrainState,
    TableState,
    comparable_columns,
    compute,
    grain_from_block,
    has_schema_changes,
    physical_layout_from_block,
)


SELECTORS = DiffSelectors(include=("*",), exclude=())
SCAN_TS = "2026-06-08T00:00:00Z"
GEN_TS = "2026-06-08T00:00:01Z"


def _table(fqn: str = "public.t", **kwargs: Any) -> TableState:
    """Build a fully-known TableState; the diff-tests probe diff logic, not baseline hydration."""

    state = TableState(
        fqn=fqn,
        type="table",
        columns={},
        relationships=[],
        indexes={},
        column_comments={},
        statistics={},
        table_comment_known=True,
    )

    for k, v in kwargs.items():
        setattr(state, k, v)

    return state


def _compute(baseline, current, **overrides) -> dict[str, Any]:
    return compute(
        baseline,
        current,
        connection_name=overrides.get("connection_name", "primary"),
        adapter_kind=overrides.get("adapter_kind", "postgres"),
        baseline_path=overrides.get("baseline_path", "prints/primary"),
        baseline_generated_at=overrides.get("baseline_generated_at"),
        baseline_dbprint_version=overrides.get("baseline_dbprint_version"),
        scanned_at=overrides.get("scanned_at", SCAN_TS),
        selectors=overrides.get("selectors", SELECTORS),
        generated_at=overrides.get("generated_at", GEN_TS),
        carried=overrides.get("carried", frozenset()),
    )


def _kinds(diff: dict[str, Any]) -> list[str]:
    return [c["kind"] for c in diff["changes"]]


class TestEmpty:
    def test_empty_baseline_empty_current(self) -> None:
        diff = _compute({}, {})
        assert diff["changes"] == []
        assert diff["summary"]["tables_added"] == 0
        assert diff["summary"]["unchanged_tables"] == 0

    def test_first_run_baseline_none(self) -> None:
        diff = _compute(None, {"public.t": _table()})
        kinds = _kinds(diff)
        assert kinds == ["table_added"]


class TestBaselineProvenance:
    def test_no_recorded_version_stays_none_even_with_a_recorded_timestamp(self) -> None:
        """A print recording `generated_at` but no `dbprint_version` must not have this run's own
        version substituted in - that misattributes the baseline to a process that never wrote it.
        """

        diff = _compute({}, {}, baseline_generated_at=GEN_TS)

        assert diff["baseline"]["dbprint_version"] is None

    def test_a_recorded_version_passes_through(self) -> None:
        diff = _compute({}, {}, baseline_generated_at=GEN_TS, baseline_dbprint_version="0.1.0")

        assert diff["baseline"]["dbprint_version"] == "0.1.0"


class TestTableLifecycle:
    def test_table_added(self) -> None:
        diff = _compute({}, {"public.t": _table()})
        assert _kinds(diff) == ["table_added"]
        assert diff["summary"]["tables_added"] == 1

    def test_table_removed(self) -> None:
        diff = _compute({"public.t": _table()}, {})
        assert _kinds(diff) == ["table_removed"]
        assert diff["summary"]["tables_removed"] == 1

    def test_table_modified_counts_unique_table(self) -> None:
        before = _table(columns={"a": ColumnState("a", "int", True, None)})
        after = _table(
            columns={
                "a": ColumnState("a", "int", True, None),
                "b": ColumnState("b", "text", True, None),
            },
        )
        diff = _compute({"public.t": before}, {"public.t": after})
        assert diff["summary"]["tables_modified"] == 1
        assert "column_added" in _kinds(diff)


class TestColumnChanges:
    def test_column_added_removed(self) -> None:
        before = _table(columns={"a": ColumnState("a", "int", True, None)})
        after = _table(columns={"b": ColumnState("b", "text", True, None)})
        diff = _compute({"public.t": before}, {"public.t": after})
        kinds = _kinds(diff)
        assert "column_added" in kinds
        assert "column_removed" in kinds

    def test_column_type_changed(self) -> None:
        before = _table(columns={"a": ColumnState("a", "int", True, None)})
        after = _table(columns={"a": ColumnState("a", "bigint", True, None)})
        diff = _compute({"public.t": before}, {"public.t": after})
        change = next(c for c in diff["changes"] if c["kind"] == "column_type_changed")
        assert change["before"] == "int"
        assert change["after"] == "bigint"

    def test_column_nullable_changed(self) -> None:
        before = _table(columns={"a": ColumnState("a", "int", True, None)})
        after = _table(columns={"a": ColumnState("a", "int", False, None)})
        diff = _compute({"public.t": before}, {"public.t": after})
        assert "column_nullable_changed" in _kinds(diff)

    def test_column_default_changed(self) -> None:
        before = _table(columns={"a": ColumnState("a", "int", True, "0")})
        after = _table(columns={"a": ColumnState("a", "int", True, "42")})
        diff = _compute({"public.t": before}, {"public.t": after})
        assert "column_default_changed" in _kinds(diff)

    def test_column_default_unknown_baseline_suppresses_event(self) -> None:
        before = _table(columns={"a": ColumnState("a", "int", True, None, default_known=False)})
        after = _table(columns={"a": ColumnState("a", "int", True, "0")})
        diff = _compute({"public.t": before}, {"public.t": after})
        assert "column_default_changed" not in _kinds(diff)


class TestRelationshipChanges:
    def _fk(self, src: tuple[str, ...] = ("a",), on_delete: str = "CASCADE") -> FkState:
        return FkState(
            source_columns=src,
            target_table="public.other",
            target_columns=("id",),
            on_delete=on_delete,
            on_update="NO ACTION",
        )

    def test_relationship_added(self) -> None:
        before = _table(relationships=[])
        after = _table(relationships=[self._fk()])
        diff = _compute({"public.t": before}, {"public.t": after})
        assert "relationship_added" in _kinds(diff)

    def test_relationship_removed(self) -> None:
        before = _table(relationships=[self._fk()])
        after = _table(relationships=[])
        diff = _compute({"public.t": before}, {"public.t": after})
        assert "relationship_removed" in _kinds(diff)

    def test_relationship_modified_on_action_change(self) -> None:
        before = _table(relationships=[self._fk(on_delete="CASCADE")])
        after = _table(relationships=[self._fk(on_delete="SET NULL")])
        diff = _compute({"public.t": before}, {"public.t": after})
        change = next(c for c in diff["changes"] if c["kind"] == "relationship_modified")
        assert change["on_delete"] == {"before": "CASCADE", "after": "SET NULL"}


class TestIndexChanges:
    def test_index_added(self) -> None:
        after = _table(indexes={"i": IndexState("i", ("a",), False, "btree")})
        diff = _compute({"public.t": _table()}, {"public.t": after})
        assert "index_added" in _kinds(diff)

    def test_index_removed(self) -> None:
        before = _table(indexes={"i": IndexState("i", ("a",), False, "btree")})
        diff = _compute({"public.t": before}, {"public.t": _table()})
        assert "index_removed" in _kinds(diff)

    def test_index_modified_columns_change(self) -> None:
        before = _table(indexes={"i": IndexState("i", ("a",), False, "btree")})
        after = _table(indexes={"i": IndexState("i", ("a", "b"), False, "btree")})
        diff = _compute({"public.t": before}, {"public.t": after})
        change = next(c for c in diff["changes"] if c["kind"] == "index_modified")
        assert change["before"]["columns"] == ["a"]
        assert change["after"]["columns"] == ["a", "b"]


class TestCommentChanges:
    def test_table_comment_changed(self) -> None:
        before = _table(table_comment="old")
        after = _table(table_comment="new")
        diff = _compute({"public.t": before}, {"public.t": after})
        change = next(c for c in diff["changes"] if c["kind"] == "comment_changed")
        assert change["target"] == "table"
        assert "column" not in change

    def test_column_comment_added(self) -> None:
        before = _table(column_comments={})
        after = _table(column_comments={"a": "new"})
        diff = _compute({"public.t": before}, {"public.t": after})
        change = next(c for c in diff["changes"] if c["kind"] == "comment_changed")
        assert change["target"] == "column"
        assert change["column"] == "a"
        assert change["before"] is None
        assert change["after"] == "new"


class TestRowCountChanges:
    def test_row_count_grew(self) -> None:
        before = _table(row_count=100, row_count_method="exact")
        after = _table(row_count=120, row_count_method="exact")
        diff = _compute({"public.t": before}, {"public.t": after})
        change = next(c for c in diff["changes"] if c["kind"] == "table_row_count_changed")
        assert change["before"] == 100
        assert change["after"] == 120
        assert change["delta"] == 20
        assert change["before_method"] == "exact"
        assert change["after_method"] == "exact"

    def test_row_count_unchanged_emits_nothing(self) -> None:
        before = _table(row_count=100, row_count_method="exact")
        after = _table(row_count=100, row_count_method="approximate")
        diff = _compute({"public.t": before}, {"public.t": after})
        assert "table_row_count_changed" not in _kinds(diff)

    def test_either_side_unknown_emits_nothing(self) -> None:
        before = _table(row_count=None, row_count_method=None)
        after = _table(row_count=120, row_count_method="exact")
        diff = _compute({"public.t": before}, {"public.t": after})
        assert "table_row_count_changed" not in _kinds(diff)

    def test_row_count_only_change_counts_the_table_as_modified(self) -> None:
        before = _table(row_count=100, row_count_method="exact")
        after = _table(row_count=120, row_count_method="exact")
        diff = _compute({"public.t": before}, {"public.t": after})
        assert diff["summary"]["tables_modified"] == 1
        assert diff["summary"]["unchanged_tables"] == 0

    def test_row_count_change_is_not_a_schema_change(self) -> None:
        before = _table(row_count=100, row_count_method="exact")
        after = _table(row_count=120, row_count_method="exact")
        diff = _compute({"public.t": before}, {"public.t": after})
        assert has_schema_changes(diff) is False

    def test_both_approximate_still_reports_delta(self) -> None:
        """The event carries the estimate difference; a consumer reads the method, not this code."""

        before = _table(row_count=1_000_000, row_count_method="approximate")
        after = _table(row_count=1_050_000, row_count_method="approximate")
        diff = _compute({"public.t": before}, {"public.t": after})
        change = next(c for c in diff["changes"] if c["kind"] == "table_row_count_changed")
        assert change["delta"] == 50_000
        assert change["before_method"] == "approximate"
        assert change["after_method"] == "approximate"

    def test_row_count_beside_a_schema_change_still_reports_schema_change(self) -> None:
        before = _table(
            row_count=100,
            row_count_method="exact",
            columns={"a": ColumnState("a", "int", True, None)},
        )
        after = _table(
            row_count=120,
            row_count_method="exact",
            columns={
                "a": ColumnState("a", "int", True, None),
                "b": ColumnState("b", "int", True, None),
            },
        )
        diff = _compute({"public.t": before}, {"public.t": after})
        assert has_schema_changes(diff) is True


class TestGrainChanges:
    def test_a_declared_key_change_is_reportable(self) -> None:
        before = _table(grain=TableGrainState(keys=(GrainKeyState(("id",), "declared"),)))
        after = _table(
            grain=TableGrainState(
                keys=(
                    GrainKeyState(("id",), "declared"),
                    GrainKeyState(("id", "herbarium_id"), "declared"),
                ),
            ),
        )
        diff = _compute({"public.t": before}, {"public.t": after})
        change = next(c for c in diff["changes"] if c["kind"] == "grain_changed")
        assert change["before"]["keys"] == [{"columns": ["id"], "detection": "declared"}]
        assert change["after"]["keys"] == [
            {"columns": ["id"], "detection": "declared"},
            {"columns": ["id", "herbarium_id"], "detection": "declared"},
        ]

    def test_a_measured_entry_appearing_is_reportable(self) -> None:
        before = _table(grain=TableGrainState(keys=(), search_exhausted=True))
        after = _table(
            grain=TableGrainState(
                keys=(GrainKeyState(("a", "b"), "measured"),),
                search_exhausted=True,
            ),
        )
        diff = _compute({"public.t": before}, {"public.t": after})
        assert "grain_changed" in _kinds(diff)

    def test_search_exhausted_flipping_alone_is_reportable(self) -> None:
        before = _table(grain=TableGrainState(keys=(), search_exhausted=False))
        after = _table(grain=TableGrainState(keys=(), search_exhausted=True))
        diff = _compute({"public.t": before}, {"public.t": after})
        change = next(c for c in diff["changes"] if c["kind"] == "grain_changed")
        assert change["before"].get("search") == {"exhausted": False}
        assert change["after"]["search"] == {"exhausted": True}

    def test_a_detection_change_reports_one_event_not_a_remove_then_add(self) -> None:
        """The same column combination becoming a stronger claim, not two events."""

        before = _table(grain=TableGrainState(keys=(GrainKeyState(("a", "b"), "measured"),)))
        after = _table(grain=TableGrainState(keys=(GrainKeyState(("a", "b"), "declared"),)))
        diff = _compute({"public.t": before}, {"public.t": after})
        assert _kinds(diff).count("grain_changed") == 1
        change = next(c for c in diff["changes"] if c["kind"] == "grain_changed")
        assert change["before"]["keys"][0]["columns"] == change["after"]["keys"][0]["columns"]
        assert change["before"]["keys"][0]["detection"] != change["after"]["keys"][0]["detection"]

    def test_grain_unchanged_emits_nothing(self) -> None:
        grain = TableGrainState(keys=(GrainKeyState(("id",), "declared"),), search_exhausted=None)
        before = _table(grain=grain)
        after = _table(grain=grain)
        diff = _compute({"public.t": before}, {"public.t": after})
        assert "grain_changed" not in _kinds(diff)

    def test_either_side_unknown_emits_nothing(self) -> None:
        """A baseline predating `grain` must not read as every key newly added."""

        before = _table(grain=None)
        after = _table(grain=TableGrainState(keys=(GrainKeyState(("id",), "declared"),)))
        diff = _compute({"public.t": before}, {"public.t": after})
        assert "grain_changed" not in _kinds(diff)

    def test_grain_only_change_counts_the_table_as_modified(self) -> None:
        before = _table(grain=TableGrainState(keys=()))
        after = _table(grain=TableGrainState(keys=(GrainKeyState(("id",), "declared"),)))
        diff = _compute({"public.t": before}, {"public.t": after})
        assert diff["summary"]["tables_modified"] == 1

    def test_grain_change_is_a_schema_change(self) -> None:
        before = _table(grain=TableGrainState(keys=()))
        after = _table(grain=TableGrainState(keys=(GrainKeyState(("id",), "declared"),)))
        diff = _compute({"public.t": before}, {"public.t": after})
        assert has_schema_changes(diff) is True


class TestPhysicalLayoutChanges:
    def test_a_genuine_gain_is_reportable(self) -> None:
        before = _table(physical_layout=PhysicalLayoutState(mechanism="", keys=()))
        after = _table(
            physical_layout=PhysicalLayoutState(
                mechanism="cluster",
                keys=(PhysicalLayoutKeyState(expression="logged_at::date", column="logged_at"),),
            ),
        )
        diff = _compute({"public.t": before}, {"public.t": after})
        change = next(c for c in diff["changes"] if c["kind"] == "physical_layout_changed")
        assert change["before"] is None
        assert change["after"] == {
            "mechanism": "cluster",
            "keys": [{"expression": "logged_at::date", "column": "logged_at"}],
        }

    def test_unchanged_emits_nothing(self) -> None:
        layout = PhysicalLayoutState(mechanism="", keys=())
        before = _table(physical_layout=layout)
        after = _table(physical_layout=layout)
        diff = _compute({"public.t": before}, {"public.t": after})
        assert "physical_layout_changed" not in _kinds(diff)

    def test_either_side_unknown_emits_nothing(self) -> None:
        before = _table(physical_layout=None)
        after = _table(physical_layout=PhysicalLayoutState(mechanism="", keys=()))
        diff = _compute({"public.t": before}, {"public.t": after})
        assert "physical_layout_changed" not in _kinds(diff)

    def test_physical_layout_change_is_a_schema_change(self) -> None:
        before = _table(physical_layout=PhysicalLayoutState(mechanism="", keys=()))
        after = _table(
            physical_layout=PhysicalLayoutState(
                mechanism="partition",
                keys=(PhysicalLayoutKeyState(expression="herbarium_id"),),
            ),
        )
        diff = _compute({"public.t": before}, {"public.t": after})
        assert has_schema_changes(diff) is True


class TestDependsOnChanges:
    """`depends_on` (SPEC 2.2.17) is a declared fact, not data drift - a view/matview redefined
    to read a different object reports here even though no table's own rows moved.
    """

    def test_a_redefined_view_is_reportable(self) -> None:
        before = _table(type="view", depends_on=("seedbank.germination_trial",))
        after = _table(type="view", depends_on=("seedbank.germination_reading",))
        diff = _compute({"public.t": before}, {"public.t": after})
        change = next(c for c in diff["changes"] if c["kind"] == "depends_on_changed")

        assert change["before"] == ["seedbank.germination_trial"]
        assert change["after"] == ["seedbank.germination_reading"]

    def test_unchanged_emits_nothing(self) -> None:
        before = _table(type="view", depends_on=("seedbank.germination_trial",))
        after = _table(type="view", depends_on=("seedbank.germination_trial",))
        diff = _compute({"public.t": before}, {"public.t": after})

        assert "depends_on_changed" not in _kinds(diff)

    def test_either_side_unknown_emits_nothing(self) -> None:
        """A table (never carries the field) or a view the catalog could not answer for -
        both must read as nothing to compare, never as a removal.
        """

        before = _table(type="view", depends_on=None)
        after = _table(type="view", depends_on=("seedbank.germination_trial",))
        diff = _compute({"public.t": before}, {"public.t": after})

        assert "depends_on_changed" not in _kinds(diff)

    def test_absent_on_both_sides_emits_nothing(self) -> None:
        """A view whose catalog cannot answer the question reads as absent on both
        sides - never as a removal.
        """

        before = _table(type="view", depends_on=None)
        after = _table(type="view", depends_on=None)
        diff = _compute({"public.t": before}, {"public.t": after})

        assert "depends_on_changed" not in _kinds(diff)

    def test_depends_on_change_counts_the_table_as_modified(self) -> None:
        before = _table(type="view", depends_on=("seedbank.germination_trial",))
        after = _table(type="view", depends_on=("seedbank.germination_reading",))
        diff = _compute({"public.t": before}, {"public.t": after})

        assert diff["summary"]["tables_modified"] == 1

    def test_depends_on_change_is_a_schema_change(self) -> None:
        before = _table(type="view", depends_on=("seedbank.germination_trial",))
        after = _table(type="view", depends_on=("seedbank.germination_reading",))
        diff = _compute({"public.t": before}, {"public.t": after})

        assert has_schema_changes(diff) is True


class TestGrainAndPhysicalLayoutParsing:
    """Unit coverage for the shared block-to-state parsers both hydration sites call."""

    def test_grain_from_block_none_on_absence(self) -> None:
        assert grain_from_block(None) is None
        assert grain_from_block("not a dict") is None
        assert grain_from_block({"no_keys_field": True}) is None

    def test_grain_from_block_parses_keys_and_search(self) -> None:
        state = grain_from_block(
            {
                "keys": [{"columns": ["a", "b"], "detection": "measured"}],
                "search": {"exhausted": False},
            },
        )
        assert state == TableGrainState(
            keys=(GrainKeyState(("a", "b"), "measured"),),
            search_exhausted=False,
        )

    def test_grain_from_block_search_omitted_stays_none(self) -> None:
        state = grain_from_block({"keys": []})
        assert state == TableGrainState(keys=(), search_exhausted=None)

    def test_physical_layout_from_block_absence_is_the_unclustered_sentinel(self) -> None:
        assert physical_layout_from_block(None) == PhysicalLayoutState(mechanism="", keys=())
        assert physical_layout_from_block("not a dict") == PhysicalLayoutState(
            mechanism="",
            keys=(),
        )

    def test_physical_layout_from_block_parses_keys(self) -> None:
        state = physical_layout_from_block(
            {
                "mechanism": "cluster",
                "keys": [{"expression": "a"}, {"expression": "b", "column": "b"}],
            },
        )
        assert state == PhysicalLayoutState(
            mechanism="cluster",
            keys=(
                PhysicalLayoutKeyState(expression="a", column=None),
                PhysicalLayoutKeyState(expression="b", column="b"),
            ),
        )


class TestStatisticChanges:
    def test_statistic_changed_with_numeric_delta(self) -> None:
        before = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={"a": {"cardinality": 10}},
        )
        after = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={"a": {"cardinality": 15}},
        )
        diff = _compute({"public.t": before}, {"public.t": after})
        change = next(c for c in diff["changes"] if c["kind"] == "statistic_changed")
        assert change["stat"] == "cardinality"
        assert change["before"] == 10
        assert change["after"] == 15
        assert change["delta"] == 5
        assert change["delta_pct"] == 0.5

    def test_statistic_changed_non_numeric_no_delta(self) -> None:
        before = _table(
            columns={"a": ColumnState("a", "text", True, None)},
            statistics={"a": {"distribution": "uniform"}},
        )
        after = _table(
            columns={"a": ColumnState("a", "text", True, None)},
            statistics={"a": {"distribution": "imbalanced"}},
        )
        diff = _compute({"public.t": before}, {"public.t": after})
        change = next(c for c in diff["changes"] if c["kind"] == "statistic_changed")
        assert "delta" not in change

    def test_zero_before_omits_delta_pct(self) -> None:
        before = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={"a": {"cardinality": 0}},
        )
        after = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={"a": {"cardinality": 5}},
        )
        diff = _compute({"public.t": before}, {"public.t": after})
        change = next(c for c in diff["changes"] if c["kind"] == "statistic_changed")
        assert "delta" in change
        assert "delta_pct" not in change

    def test_negative_before_falling_reports_negative_delta_pct(self) -> None:
        before = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={"a": {"range": {"min": -100}}},
        )
        after = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={"a": {"range": {"min": -110}}},
        )
        diff = _compute({"public.t": before}, {"public.t": after})
        change = next(c for c in diff["changes"] if c["kind"] == "statistic_changed")
        assert change["delta"] == -10
        assert change["delta_pct"] == -0.1

    def test_negative_before_rising_reports_positive_delta_pct(self) -> None:
        before = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={"a": {"range": {"min": -100}}},
        )
        after = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={"a": {"range": {"min": -90}}},
        )
        diff = _compute({"public.t": before}, {"public.t": after})
        change = next(c for c in diff["changes"] if c["kind"] == "statistic_changed")
        assert change["delta"] == 10
        assert change["delta_pct"] == 0.1

    def test_crossing_zero_is_not_clamped(self) -> None:
        before = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={"a": {"range": {"min": -50}}},
        )
        after = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={"a": {"range": {"min": 50}}},
        )
        diff = _compute({"public.t": before}, {"public.t": after})
        change = next(c for c in diff["changes"] if c["kind"] == "statistic_changed")
        assert change["delta"] == 100
        assert change["delta_pct"] == 2.0


class TestValuesRestored:
    """SPEC 2.6.6/2.6.10: `values` is diffable, carries the full list, no `delta`."""

    def test_a_grown_enum_reports_both_full_lists(self) -> None:
        before = _table(
            columns={"a": ColumnState("a", "text", True, None)},
            statistics={"a": {"values": [{"value": "active", "count": 5}]}},
        )
        after = _table(
            columns={"a": ColumnState("a", "text", True, None)},
            statistics={
                "a": {
                    "values": [
                        {"value": "active", "count": 5},
                        {"value": "enterprize", "count": 1},
                    ],
                },
            },
        )
        diff = _compute({"public.t": before}, {"public.t": after})
        change = next(c for c in diff["changes"] if c["kind"] == "statistic_changed")

        assert change["stat"] == "values"
        assert change["before"] == [{"value": "active", "count": 5}]
        assert change["after"] == [
            {"value": "active", "count": 5},
            {"value": "enterprize", "count": 1},
        ]
        assert "delta" not in change
        assert "delta_pct" not in change

    def test_counts_moving_alone_still_reports(self) -> None:
        """Same members, different counts - the SPEC draws no distinction (SPEC 2.6.6)."""

        before = _table(
            columns={"a": ColumnState("a", "text", True, None)},
            statistics={"a": {"values": [{"value": "active", "count": 5}]}},
        )
        after = _table(
            columns={"a": ColumnState("a", "text", True, None)},
            statistics={"a": {"values": [{"value": "active", "count": 9}]}},
        )
        diff = _compute({"public.t": before}, {"public.t": after})

        assert any(
            c["kind"] == "statistic_changed" and c["stat"] == "values" for c in diff["changes"]
        )

    def test_an_unchanged_list_reports_nothing(self) -> None:
        payload = {"values": [{"value": "active", "count": 5}]}
        before = _table(
            columns={"a": ColumnState("a", "text", True, None)},
            statistics={"a": dict(payload)},
        )
        after = _table(
            columns={"a": ColumnState("a", "text", True, None)},
            statistics={"a": dict(payload)},
        )
        diff = _compute({"public.t": before}, {"public.t": after})

        assert diff["changes"] == []

    def test_a_stable_masked_list_reports_nothing(self) -> None:
        """A stable redaction marker over unchanged data is stable bytes, not drift."""

        payload = {"values": [{"value": "[redacted]", "count": 5}], "redacted": "mask"}
        before = _table(
            columns={"a": ColumnState("a", "text", True, None)},
            statistics={"a": dict(payload)},
        )
        after = _table(
            columns={"a": ColumnState("a", "text", True, None)},
            statistics={"a": dict(payload)},
        )
        diff = _compute({"public.t": before}, {"public.t": after})

        assert diff["changes"] == []

    def test_freshness_still_reports_nothing(self) -> None:
        """`values` is compared, `freshness` is not.

        The raw payload is routed through `comparable_columns` - what strips `freshness` in
        production - rather than assigned to `statistics` directly.
        """

        raw_before = {"a": {"freshness": {"classification": "live", "max_age_days": 1}}}
        raw_after = {"a": {"freshness": {"classification": "dormant", "max_age_days": 400}}}
        before = _table(
            columns={"a": ColumnState("a", "temporal", True, None)},
            statistics=comparable_columns(raw_before),
        )
        after = _table(
            columns={"a": ColumnState("a", "temporal", True, None)},
            statistics=comparable_columns(raw_after),
        )
        diff = _compute({"public.t": before}, {"public.t": after})

        assert diff["changes"] == []


class TestLooksLikeEvidenceExclusion:
    """A redrawn sample moves `inferred.sampled`/`inferred.matched` on every run - not drift."""

    def test_a_changed_sample_size_reports_nothing(self) -> None:
        before = _table(
            columns={"a": ColumnState("a", "text", True, None)},
            statistics={"a": {"inferred": {"looks_like": "email", "sampled": 100, "matched": 96}}},
        )
        after = _table(
            columns={"a": ColumnState("a", "text", True, None)},
            statistics={"a": {"inferred": {"looks_like": "email", "sampled": 140, "matched": 133}}},
        )
        diff = _compute({"public.t": before}, {"public.t": after})

        assert [c for c in diff["changes"] if c["kind"] == "statistic_changed"] == []

    def test_the_verdict_itself_still_reports(self) -> None:
        """The control: `inferred.looks_like` is not swept up by the same exclusion."""

        before = _table(
            columns={"a": ColumnState("a", "text", True, None)},
            statistics={"a": {"inferred": {"looks_like": "email", "sampled": 100, "matched": 96}}},
        )
        after = _table(
            columns={"a": ColumnState("a", "text", True, None)},
            statistics={"a": {"inferred": {"looks_like": "url", "sampled": 100, "matched": 97}}},
        )
        diff = _compute({"public.t": before}, {"public.t": after})
        changed = [c for c in diff["changes"] if c["kind"] == "statistic_changed"]

        assert {c["stat"] for c in changed} == {"inferred.looks_like"}

    def test_a_near_miss_share_that_moved_with_the_redraw_reports_nothing(self) -> None:
        """`looks_like_candidate`/`_share` are drawn from the same sample as `sampled`/
        `matched` and move for the identical reason - excluded on the same terms.
        """

        before = _table(
            columns={"a": ColumnState("a", "text", True, None)},
            statistics={
                "a": {
                    "inferred": {
                        "looks_like_candidate": "email",
                        "looks_like_candidate_share": 0.62,
                    },
                },
            },
        )
        after = _table(
            columns={"a": ColumnState("a", "text", True, None)},
            statistics={
                "a": {
                    "inferred": {
                        "looks_like_candidate": "email",
                        "looks_like_candidate_share": 0.58,
                    },
                },
            },
        )
        diff = _compute({"public.t": before}, {"public.t": after})

        assert [c for c in diff["changes"] if c["kind"] == "statistic_changed"] == []


class TestNormalizedCardinalityPresenceGate:
    """A presence gate, not a blanket exclusion: `normalized_cardinality` has no current side
    on a diff-only run (no sketch pass), but both sides are populated on `generate`.
    """

    def test_a_genuine_change_reports_when_both_sides_carry_it(self) -> None:
        before = _table(
            columns={"a": ColumnState("a", "text", True, None)},
            statistics={"a": {"normalized_cardinality": 2}},
        )
        after = _table(
            columns={"a": ColumnState("a", "text", True, None)},
            statistics={"a": {"normalized_cardinality": 1}},
        )
        diff = _compute({"public.t": before}, {"public.t": after})
        change = next(c for c in diff["changes"] if c["kind"] == "statistic_changed")

        assert change["stat"] == "normalized_cardinality"
        assert change["before"] == 2
        assert change["after"] == 1

    def test_absent_on_the_current_side_reports_nothing(self) -> None:
        """A diff-only run: the baseline (a prior `generate`) carries it, the live side never
        computes it - absence must not read as the field dropping to nothing.
        """

        before = _table(
            columns={"a": ColumnState("a", "text", True, None)},
            statistics={"a": {"normalized_cardinality": 2}},
        )
        after = _table(
            columns={"a": ColumnState("a", "text", True, None)},
            statistics={"a": {}},
        )
        diff = _compute({"public.t": before}, {"public.t": after})

        assert [c for c in diff["changes"] if c["kind"] == "statistic_changed"] == []

    def test_absent_on_both_sides_reports_nothing(self) -> None:
        before = _table(
            columns={"a": ColumnState("a", "text", True, None)},
            statistics={"a": {}},
        )
        after = _table(
            columns={"a": ColumnState("a", "text", True, None)},
            statistics={"a": {}},
        )
        diff = _compute({"public.t": before}, {"public.t": after})

        assert [c for c in diff["changes"] if c["kind"] == "statistic_changed"] == []


class TestApproximateCardinalityExclusion:
    """A cardinality read from the planner, not counted, must not report false drift."""

    def test_both_sides_approximate_suppresses_cardinality_and_ratio(self) -> None:
        before = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={
                "a": {
                    "cardinality": 900_000,
                    "cardinality_ratio": 0.9,
                    "cardinality_method": "approximate",
                },
            },
        )
        after = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={
                "a": {
                    "cardinality": 905_000,
                    "cardinality_ratio": 0.905,
                    "cardinality_method": "approximate",
                },
            },
        )
        diff = _compute({"public.t": before}, {"public.t": after})

        assert diff["changes"] == []

    def test_exact_on_both_sides_still_compares(self) -> None:
        """The exclusion is conditional, not a blanket suppression of the two fields."""

        before = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={"a": {"cardinality": 10, "cardinality_method": "exact"}},
        )
        after = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={"a": {"cardinality": 15, "cardinality_method": "exact"}},
        )
        diff = _compute({"public.t": before}, {"public.t": after})
        stats_changed = {c["stat"] for c in diff["changes"] if c["kind"] == "statistic_changed"}

        assert "cardinality" in stats_changed

    def test_missing_method_reads_as_exact(self) -> None:
        """A baseline predating cardinality_method must keep comparing, not go silent."""

        before = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={"a": {"cardinality": 10}},
        )
        after = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={"a": {"cardinality": 15, "cardinality_method": "exact"}},
        )
        diff = _compute({"public.t": before}, {"public.t": after})
        stats_changed = {c["stat"] for c in diff["changes"] if c["kind"] == "statistic_changed"}

        assert "cardinality" in stats_changed

    def test_a_gate_flip_reports_exactly_one_event_with_no_phantom(self) -> None:
        """A method flip is real; the value swing it carries with it must not also fire."""

        before = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={
                "a": {"cardinality": 100, "cardinality_ratio": 0.1, "cardinality_method": "exact"},
            },
        )
        after = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={
                "a": {
                    "cardinality": 105,
                    "cardinality_ratio": 0.105,
                    "cardinality_method": "approximate",
                },
            },
        )
        diff = _compute({"public.t": before}, {"public.t": after})
        changes = [c for c in diff["changes"] if c["kind"] == "statistic_changed"]

        assert len(changes) == 1
        assert changes[0]["stat"] == "cardinality_method"
        assert changes[0]["before"] == "exact"
        assert changes[0]["after"] == "approximate"
        assert not any(c.get("before") is None or c.get("after") is None for c in changes)

    def test_approximate_does_not_widen_to_other_stats(self) -> None:
        """Only cardinality and cardinality_ratio are excluded - not every stat on the column."""

        before = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={
                "a": {
                    "cardinality": 900_000,
                    "cardinality_method": "approximate",
                    "null_rate": 0.01,
                },
            },
        )
        after = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={
                "a": {
                    "cardinality": 905_000,
                    "cardinality_method": "approximate",
                    "null_rate": 0.5,
                },
            },
        )
        diff = _compute({"public.t": before}, {"public.t": after})
        stats_changed = {c["stat"] for c in diff["changes"] if c["kind"] == "statistic_changed"}

        assert stats_changed == {"null_rate"}


class TestPopulationSuppression:
    """A re-scanned table's absolute counts are scan-scale, not data drift (SPEC 2.2.8)."""

    def test_absolute_counts_suppressed_ratios_still_compared(self) -> None:
        before = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={
                "a": {
                    "cardinality": 100,
                    "null_count": 5,
                    "null_rate": 0.05,
                    "cardinality_ratio": 0.1,
                },
            },
            scoped=True,
        )
        after = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={
                "a": {
                    "cardinality": 900,
                    "null_count": 40,
                    "null_rate": 0.3,
                    "cardinality_ratio": 0.5,
                },
            },
            scoped=True,
        )
        diff = _compute({"public.t": before}, {"public.t": after})
        stats_changed = {c["stat"] for c in diff["changes"] if c["kind"] == "statistic_changed"}

        assert stats_changed == {"null_rate", "cardinality_ratio"}

    def test_values_suppressed_under_scope(self) -> None:
        """`values` is a compared stat now; a re-scanned table must not re-noise it."""

        before = _table(
            columns={"a": ColumnState("a", "text", True, None)},
            statistics={"a": {"values": [{"value": "x", "count": 5}]}},
            scoped=True,
        )
        after = _table(
            columns={"a": ColumnState("a", "text", True, None)},
            statistics={"a": {"values": [{"value": "y", "count": 900}]}},
            scoped=True,
        )
        diff = _compute({"public.t": before}, {"public.t": after})

        assert diff["changes"] == []

    def test_unscoped_both_sides_is_unaffected(self) -> None:
        """The control: ordinary drift on an ordinary table still reports."""

        before = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={"a": {"cardinality": 100}},
        )
        after = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={"a": {"cardinality": 900}},
        )
        diff = _compute({"public.t": before}, {"public.t": after})
        stats_changed = {c["stat"] for c in diff["changes"] if c["kind"] == "statistic_changed"}

        assert stats_changed == {"cardinality"}

    def test_either_side_scoped_suppresses(self) -> None:
        """A table becoming scoped, or ceasing to be, is still a population change either way."""

        scoped = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={"a": {"cardinality": 100}},
            scoped=True,
        )
        unscoped = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={"a": {"cardinality": 900}},
        )
        became_scoped = _compute({"public.t": unscoped}, {"public.t": scoped})
        stopped_scoped = _compute({"public.t": scoped}, {"public.t": unscoped})

        assert became_scoped["changes"] == []
        assert stopped_scoped["changes"] == []

    def test_a_scoped_table_with_no_events_is_unevaluated_not_unchanged(self) -> None:
        """Suppressed absolute counts mean the diff does not know, not that nothing moved."""

        state = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={"a": {"cardinality": 100, "cardinality_ratio": 0.1}},
            scoped=True,
        )
        diff = _compute({"public.t": state}, {"public.t": state})

        assert diff["changes"] == []
        assert diff["summary"]["unevaluated_tables"] == 1
        assert diff["summary"]["unchanged_tables"] == 0

    def test_statistics_drifted_counts_emitted_events_only(self) -> None:
        before = _table(
            fqn="a.scoped",
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={"a": {"cardinality": 100, "null_rate": 0.1}},
            scoped=True,
        )
        after = _table(
            fqn="a.scoped",
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={"a": {"cardinality": 900, "null_rate": 0.4}},
            scoped=True,
        )
        diff = _compute({"a.scoped": before}, {"a.scoped": after})

        assert diff["summary"]["statistics_drifted"] == 1
        assert diff["summary"]["statistics_drifted"] == len(
            [c for c in diff["changes"] if c["kind"] == "statistic_changed"],
        )

    def test_the_six_scale_dependent_counts_are_suppressed_under_scope(self) -> None:
        """The count fields scale with `rows_scanned` exactly as `null_count` does (SPEC 2.2.8) -
        a re-scanned table's own growth must not read as drift on any of them.
        """

        before = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={
                "a": {
                    "sum": 1000,
                    "zero_count": 10,
                    "negative_count": 5,
                    "empty_count": 2,
                    "quantized_count": 900,
                    "normalized_cardinality": 800,
                },
            },
            scoped=True,
        )
        after = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={
                "a": {
                    "sum": 1100,
                    "zero_count": 11,
                    "negative_count": 6,
                    "empty_count": 3,
                    "quantized_count": 990,
                    "normalized_cardinality": 880,
                },
            },
            scoped=True,
        )
        diff = _compute({"public.t": before}, {"public.t": after})

        assert diff["changes"] == []

    def test_the_scanned_set_marker_itself_never_compares(self) -> None:
        """`rows_scanned` rides every column of a scoped file (SPEC 2.2.8) and describes the read,
        not the data - comparing it reports drift on the one field guaranteed to move.
        """

        before = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={"a": {"rows_scanned": 10_000, "null_rate": 0.1}},
            scoped=True,
        )
        after = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={"a": {"rows_scanned": 12_500, "null_rate": 0.1}},
            scoped=True,
        )
        diff = _compute({"public.t": before}, {"public.t": after})

        assert diff["changes"] == []

    def test_a_field_either_side_names_unmeasured_is_not_compared(self) -> None:
        """SPEC 2.2.4: the baseline is hydrated from an artifact, so only its own marker tells a
        failed read from a value that really moved.

        Both sides are projected through `comparable_columns`, where the gate reads the marker.
        """

        before = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics=comparable_columns({"a": {"cardinality": 900, "null_count": 4}}),
        )
        after = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics=comparable_columns(
                {"a": {"null_count": 4, "unmeasured": ["cardinality"]}},
            ),
        )
        diff = _compute({"public.t": before}, {"public.t": after})

        assert diff["changes"] == []

    def test_the_marker_itself_is_never_an_event(self) -> None:
        """A marker lifted between two runs is not drift about the column - and the field it named
        has a real value on one side only, which is the marker's whole point.
        """

        before = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics=comparable_columns(
                {"a": {"null_count": 4, "unmeasured": ["cardinality"]}},
            ),
        )
        after = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics=comparable_columns({"a": {"cardinality": 900, "null_count": 4}}),
        )
        diff = _compute({"public.t": before}, {"public.t": after})

        assert diff["changes"] == []

    def test_mean_and_length_are_not_swept_up_by_the_same_suppression(self) -> None:
        """The control: `mean` is normalised (like `null_rate`) and `length` is scale-free
        (SPEC 2.2.4) - neither belongs in the scale-dependent set, and both keep comparing.
        """

        before = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={"a": {"mean": 42.5, "length": {"min": 1, "max": 10}}},
            scoped=True,
        )
        after = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={"a": {"mean": 50.1, "length": {"min": 1, "max": 12}}},
            scoped=True,
        )
        diff = _compute({"public.t": before}, {"public.t": after})
        stats_changed = {c["stat"] for c in diff["changes"] if c["kind"] == "statistic_changed"}

        assert stats_changed == {"mean", "length.max"}

    def test_the_five_counts_still_compare_unscoped(self) -> None:
        """The suppression is scope-conditional, not blanket - real drift still reports."""

        before = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={"a": {"zero_count": 10}},
        )
        after = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={"a": {"zero_count": 900}},
        )
        diff = _compute({"public.t": before}, {"public.t": after})
        stats_changed = {c["stat"] for c in diff["changes"] if c["kind"] == "statistic_changed"}

        assert stats_changed == {"zero_count"}


class TestCatalogOnlySuppression:
    """A table nothing was queried for has no measurement to compare (SPEC 2.2.15)."""

    def test_statistics_comparison_suppressed_entirely(self) -> None:
        before = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={"a": {"cardinality": 100}},
            row_count=1000,
            row_count_method="exact",
        )
        after = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={},
            catalog_only=True,
        )
        diff = _compute({"public.t": before}, {"public.t": after})

        assert diff["changes"] == []

    def test_either_side_catalog_only_suppresses(self) -> None:
        queried = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={"a": {"cardinality": 100}},
            row_count=1000,
            row_count_method="exact",
        )
        marked = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={},
            catalog_only=True,
        )
        became_catalog_only = _compute({"public.t": queried}, {"public.t": marked})
        stopped_being_catalog_only = _compute({"public.t": marked}, {"public.t": queried})

        assert became_catalog_only["changes"] == []
        assert stopped_being_catalog_only["changes"] == []

    def test_a_catalog_only_table_with_no_events_is_unevaluated_not_unchanged(self) -> None:
        """Suppressed statistics mean the diff does not know, not that nothing moved."""

        state = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={},
            catalog_only=True,
        )
        diff = _compute({"public.t": state}, {"public.t": state})

        assert diff["changes"] == []
        assert diff["summary"]["unevaluated_tables"] == 1
        assert diff["summary"]["unchanged_tables"] == 0

    def test_row_count_moving_to_absent_is_not_reported_as_a_removal(self) -> None:
        """`row_count` is absent under catalog_only (SPEC 2.2.15) - a stated fact, not a delta."""

        before = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={},
            row_count=1000,
            row_count_method="exact",
        )
        after = _table(
            columns={"a": ColumnState("a", "int", True, None)},
            statistics={},
            catalog_only=True,
        )
        diff = _compute({"public.t": before}, {"public.t": after})

        assert "table_row_count_changed" not in _kinds(diff)


class TestComparableColumns:
    """The projection both sides pass through decides which fields can fire an event."""

    def test_a_value_list_is_compared(self) -> None:
        projected = comparable_columns(
            {"a": {"cardinality": 3, "values": [{"value": "x", "count": 1}]}},
        )

        assert projected == {"a": {"cardinality": 3, "values": [{"value": "x", "count": 1}]}}

    def test_freshness_is_not_compared(self) -> None:
        projected = comparable_columns(
            {"a": {"cardinality": 3, "freshness": {"classification": "live", "max_age_days": 1}}},
        )

        assert projected == {"a": {"cardinality": 3}}

    def test_a_shape_claim_and_a_redaction_marker_are_compared(self) -> None:
        payload = {"a": {"inferred": {"looks_like": "email"}, "redacted": "hash"}}

        assert comparable_columns(payload) == payload

    def test_a_column_that_is_not_a_mapping_is_dropped(self) -> None:
        """statistics.yaml is a file a hand can edit, and a bad column must not diff."""

        assert comparable_columns({"a": "corrupted", "b": {"cardinality": 1}}) == {
            "b": {"cardinality": 1},
        }

    def test_the_unmeasured_marker_survives_the_projection(self) -> None:
        """It is read per comparison, not compared (SPEC 2.2.4) - stripping it here would leave
        the gate that reads it looking at an empty set on every real diff.
        """

        payload = {"a": {"cardinality": 3, "unmeasured": ["distribution"]}}

        assert comparable_columns(payload) == payload


class TestUnchangedTablesCount:
    def test_two_tables_one_unchanged_one_modified(self) -> None:
        before = {
            "a.b": _table(fqn="a.b", columns={"x": ColumnState("x", "int", True, None)}),
            "a.c": _table(fqn="a.c", columns={"y": ColumnState("y", "int", True, None)}),
        }
        after = {
            "a.b": _table(fqn="a.b", columns={"x": ColumnState("x", "int", True, None)}),
            "a.c": _table(fqn="a.c", columns={"y": ColumnState("y", "bigint", True, None)}),
        }
        diff = _compute(before, after)
        assert diff["summary"]["unchanged_tables"] == 1
        assert diff["summary"]["tables_modified"] == 1


def _view_baseline(fqn: str) -> TableState:
    """A plain view as `baseline.py` hydrates one: no `statistics.yaml` to read (SPEC 1.4)."""

    return TableState(fqn=fqn, type="view", relationships=[])


def _view_current(fqn: str, relationships: list[FkState] | None = None) -> TableState:
    """A plain view as the orchestrator builds one: columns, but an empty statistics payload."""

    return TableState(
        fqn=fqn,
        type="view",
        columns={"x": ColumnState("x", "int", True, None)},
        relationships=relationships if relationships is not None else [],
        indexes={},
        column_comments={},
        statistics={},
        table_comment_known=True,
    )


class TestUnevaluatedTables:
    """An object the diff had no basis to compare is counted apart from one it compared."""

    def test_a_view_is_not_certified_unchanged(self) -> None:
        diff = _compute({"a.v": _view_baseline("a.v")}, {"a.v": _view_current("a.v")})

        assert diff["summary"]["unevaluated_tables"] == 1
        assert diff["summary"]["unchanged_tables"] == 0

    def test_a_view_is_counted_unevaluated_with_no_change_events(self) -> None:
        """v1 defines no DDL comparison, so a view lands in that counter whatever its body."""

        result = _compute({"a.v": _view_baseline("a.v")}, {"a.v": _view_current("a.v")})

        assert result["changes"] == []
        assert result["summary"]["unevaluated_tables"] == 1

    def test_a_materialized_view_is_compared_like_a_table(self) -> None:
        """It carries `statistics.yaml`, so the gate is the artifact and never `type`."""

        matview = _table(fqn="a.mv")
        matview.type = "matview"
        diff = _compute({"a.mv": matview}, {"a.mv": matview})

        assert diff["summary"]["unchanged_tables"] == 1
        assert diff["summary"]["unevaluated_tables"] == 0

    def test_a_view_that_emitted_an_event_is_modified_rather_than_unevaluated(self) -> None:
        """Something was compared and it moved; only silence means no basis to compare."""

        edge = FkState(("x",), "a.t", ("id",), "NO ACTION", "NO ACTION")
        diff = _compute(
            {"a.v": _view_baseline("a.v")},
            {"a.v": _view_current("a.v", relationships=[edge])},
        )

        assert diff["summary"]["tables_modified"] == 1
        assert diff["summary"]["unevaluated_tables"] == 0

    def test_a_carried_forward_table_is_unevaluated_though_both_sides_hydrated(self) -> None:
        """Its current state IS its baseline state, so equality is arithmetic, not evidence."""

        state = _table(fqn="a.t")
        diff = _compute({"a.t": state}, {"a.t": state}, carried=frozenset({"a.t"}))

        assert diff["summary"]["unevaluated_tables"] == 1
        assert diff["summary"]["unchanged_tables"] == 0

    def test_a_re_read_table_is_unchanged_rather_than_carried(self) -> None:
        """The control: the same equality, with the run having actually looked."""

        state = _table(fqn="a.t")
        diff = _compute({"a.t": state}, {"a.t": state})

        assert diff["summary"]["unchanged_tables"] == 1
        assert diff["summary"]["unevaluated_tables"] == 0

    def test_the_four_counters_partition_the_scanned_set(self) -> None:
        """SPEC 2.6.4's identity, over one of each population plus an addition."""

        column = {"c": ColumnState("c", "int", True, None)}
        drifted = _table(fqn="a.drift", columns=column, statistics={"c": {"cardinality": 2}})
        before = {
            "a.same": _table(fqn="a.same"),
            "a.drift": _table(fqn="a.drift", columns=column, statistics={"c": {"cardinality": 1}}),
            "a.v": _view_baseline("a.v"),
            "a.carried": _table(fqn="a.carried"),
        }
        after = {
            "a.same": _table(fqn="a.same"),
            "a.drift": drifted,
            "a.v": _view_current("a.v"),
            "a.carried": before["a.carried"],
            "a.new": _table(fqn="a.new"),
        }
        diff = _compute(before, after, carried=frozenset({"a.carried"}))
        summary = diff["summary"]
        total = (
            summary["tables_modified"]
            + summary["unchanged_tables"]
            + summary["unevaluated_tables"]
            + summary["tables_added"]
        )

        assert (summary["tables_modified"], summary["unchanged_tables"]) == (1, 1)
        assert (summary["unevaluated_tables"], summary["tables_added"]) == (2, 1)
        assert total == diff["target"]["tables_scanned"]


class TestSelectorScoping:
    """The caller pre-narrows `current` to the selector scope, so `compute` must apply the
    same scope to the baseline or out-of-scope tables surface as table_removed (SPEC 2.6.8).
    """

    def test_out_of_scope_baseline_not_removed(self) -> None:
        baseline = {fqn: _table(fqn=fqn) for fqn in ("a.a", "a.b", "a.c")}
        current = {"a.a": _table(fqn="a.a")}  # caller narrowed to a.a
        diff = _compute(baseline, current, selectors=DiffSelectors(include=("a.a",), exclude=()))
        assert diff["changes"] == []
        assert "table_removed" not in _kinds(diff)

    def test_in_scope_addition_only(self) -> None:
        baseline = {"a.a": _table(fqn="a.a")}
        current = {"a.b": _table(fqn="a.b")}  # caller narrowed to a.b; a.a out of scope
        diff = _compute(baseline, current, selectors=DiffSelectors(include=("a.b",), exclude=()))
        kinds = _kinds(diff)
        assert "table_added" in kinds
        assert "table_removed" not in kinds  # a.a out of scope -> baseline filtered

    def test_genuine_removal_in_scope(self) -> None:
        baseline = {"a.a": _table(fqn="a.a"), "a.b": _table(fqn="a.b")}
        current: dict[str, TableState] = {}  # a.a gone from live; --include a.b narrows it out too
        diff = _compute(baseline, current, selectors=DiffSelectors(include=("a.b",), exclude=()))
        removed = [c["table"] for c in diff["changes"] if c["kind"] == "table_removed"]
        assert removed == ["a.b"]
        assert "table_added" not in _kinds(diff)

    def test_exclude_unions_to_filter_baseline(self) -> None:
        baseline = {"a.a": _table(fqn="a.a"), "a.b": _table(fqn="a.b")}
        current = {"a.a": _table(fqn="a.a")}  # caller narrowed via --exclude a.b
        diff = _compute(
            baseline,
            current,
            selectors=DiffSelectors(include=("*",), exclude=("a.b",)),
        )
        assert diff["changes"] == []
        assert "table_removed" not in _kinds(diff)


# `ColumnStats` fields whose artifact value nests, flattening under `_stat_paths` into dotted
# sub-paths ("range.min") rather than a bare name - the sub-paths are what needs classifying.
_CONTAINER_COLUMN_STATS_FIELDS = frozenset(
    {"frequencies", "range", "percentiles", "length", "inferred"},
)

# Flat fields reviewed and confirmed correct to compare unconditionally, scope included: ratios
# and non-numeric verdicts that are not scan-scale, plus `mean` (normalised like `null_rate`).
_DELIBERATELY_UNSUPPRESSED_STATS = frozenset(
    {
        "null_rate",
        "cardinality_ratio",
        "cardinality_method",
        "values_coverage",
        "distribution",
        "mean",
        "unrepresentable",
    },
)


class TestColumnStatsCompleteness:
    """Every scalar `ColumnStats` field is triaged in `diff.py` - population-absolute, uncompared,
    marker, presence-gated or compared unconditionally - so a new one cannot slip through.
    """

    @staticmethod
    def _flat_fields() -> set[str]:
        return {
            f.name
            for f in dataclasses.fields(ColumnStats)
            if f.name not in _CONTAINER_COLUMN_STATS_FIELDS
        }

    @staticmethod
    def _triaged() -> set[str]:
        uncompared = {name for name in diff_module._UNCOMPARED_STATS if "." not in name}

        return (
            uncompared
            | diff_module._MARKER_STATS
            | diff_module._POPULATION_ABSOLUTE_STATS
            | diff_module._PRESENCE_GATED_STATS
            | _DELIBERATELY_UNSUPPRESSED_STATS
        )

    def test_every_flat_field_is_triaged(self) -> None:
        untriaged = self._flat_fields() - self._triaged()

        assert untriaged == set(), (
            f"{sorted(untriaged)} landed on ColumnStats with no diff.py triage - classify each "
            "in _POPULATION_ABSOLUTE_STATS, _UNCOMPARED_STATS, _PRESENCE_GATED_STATS, or record "
            "it as deliberately unsuppressed"
        )

    def test_the_guard_actually_fires_on_an_untriaged_field(self) -> None:
        """Proves the sweep isn't vacuous - not just that today's fields happen to pass."""

        assert "a_future_field" in (self._flat_fields() | {"a_future_field"}) - self._triaged()
