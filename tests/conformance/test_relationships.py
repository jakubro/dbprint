"""relationships.yaml invariants: array shapes, reciprocity and observed arithmetic, per SPEC 2.3."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml

from dbprint.conformance import relationships as rs
from dbprint.conformance.issue import Issue
from dbprint.spec.sketch import pack_sketch


def _write_print(root: Path, tables: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Write each table's files under `s/<name>`; a None body declares the file but writes none."""

    manifest: dict[str, Any] = {}

    for name, files in tables.items():
        artifacts = {}
        table_dir = root / "s" / name
        table_dir.mkdir(parents=True, exist_ok=True)

        for kind, body in files.items():
            artifacts[kind] = f"{kind}.yaml"

            if body is not None:
                text = body if isinstance(body, str) else yaml.safe_dump(body, sort_keys=False)
                (table_dir / f"{kind}.yaml").write_text(text)

        manifest[f"s.{name}"] = {"path": f"s/{name}", "artifacts": artifacts}

    return {"tables": manifest}


def _edge(
    column: str = "a",
    target: str = "s.u",
    target_column: str = "id",
    **extra: Any,
) -> dict[str, Any]:
    return {"column": [column], "target_table": target, "target_column": [target_column], **extra}


def _mirror(
    referencer: str = "s.t",
    column: str = "a",
    target_column: str = "id",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "referencer_table": referencer,
        "referencer_column": [column],
        "column": [target_column],
        **extra,
    }


class TestTheFileAlone:
    def test_column_arrays_of_different_length(self) -> None:
        body = {
            "refers_to": ["x", {"column": ["a", "b"], "target_column": ["id"]}],
            "referenced_by": [{"column": ["id"], "referencer_column": ["a", "b"]}],
        }

        assert rs.check_entry(body, "r.yaml", "s.t") == [
            Issue(
                "r.yaml::refers_to[1]",
                "relationships.column-array-length-mismatch",
                "error",
                "len(column)=2 differs from len(target_column)=1; arrays must match.",
                "§2.3.4",
            ),
            Issue(
                "r.yaml::referenced_by[0]",
                "relationships.column-array-length-mismatch",
                "error",
                "len(column)=1 differs from len(referencer_column)=2; arrays must match.",
                "§2.3.4",
            ),
        ]

    def test_arrays_that_are_not_lists_are_left_to_the_schema(self) -> None:
        body = {"refers_to": [{"column": "a", "target_column": ["id", "x"]}]}

        assert rs.check_entry(body, "r.yaml", "s.t") == []

    def test_a_path_on_a_composite_endpoint(self) -> None:
        body = {
            "refers_to": [
                {"column": ["a", "b"], "target_column": ["x", "y"], "path": ["k"]},
                {"column": ["a"], "target_column": ["x", "y"], "target_path": ["k"]},
            ],
        }

        assert rs.check_entry(body, "r.yaml", "s.t") == [
            Issue(
                "r.yaml::refers_to[0]",
                "relationships.path-on-composite-endpoint",
                "error",
                "path is present but column has 2 entries; a path endpoint is legal only on a "
                "single-column endpoint.",
                "§2.3.9",
            ),
            Issue(
                "r.yaml::refers_to[1]",
                "relationships.column-array-length-mismatch",
                "error",
                "len(column)=1 differs from len(target_column)=2; arrays must match.",
                "§2.3.4",
            ),
            Issue(
                "r.yaml::refers_to[1]",
                "relationships.path-on-composite-endpoint",
                "error",
                "target_path is present but target_column has 2 entries; a path endpoint is "
                "legal only on a single-column endpoint.",
                "§2.3.9",
            ),
        ]

    @pytest.mark.parametrize(
        "entry",
        [
            {"column": ["a"], "target_column": ["x"], "path": ["k"]},
            {"column": "a", "target_column": ["x"], "path": ["k"]},
        ],
    )
    def test_a_path_on_a_single_or_unreadable_endpoint_passes(self, entry: dict[str, Any]) -> None:
        assert rs.check_entry({"refers_to": [entry]}, "r.yaml", "s.t") == []

    def test_an_ineligible_target_referenced_by_inference(self) -> None:
        body = {
            "eligible_target": False,
            "referenced_by": [
                {"detection": "inferred"},
                "x",
                {"detection": "declared"},
                {"detection": "inferred"},
            ],
        }

        assert rs.check_entry(body, "r.yaml", "s.t") == [
            Issue(
                "r.yaml::referenced_by",
                "relationships.ineligible-target-is-referenced",
                "error",
                "eligible_target is false but referenced_by carries 2 inferred entries; naming "
                "inference cannot resolve an edge to an ineligible object.",
                "§2.3.8",
            ),
        ]

    @pytest.mark.parametrize(
        "body",
        [
            {"eligible_target": True, "referenced_by": [{"detection": "inferred"}]},
            {"eligible_target": False, "referenced_by": "x"},
            {"eligible_target": False, "referenced_by": [{"detection": "declared"}]},
        ],
    )
    def test_an_eligible_or_uninferred_target_passes(self, body: dict[str, Any]) -> None:
        assert rs.check_entry(body, "r.yaml", "s.t") == []

    def test_a_body_that_is_not_a_mapping_is_left_to_the_schema(self) -> None:
        assert rs.check_entry(["refers_to"], "r.yaml", "s.t") == []


class TestReciprocity:
    def test_an_edge_stated_on_both_sides_the_same_way_passes(self, tmp_path: Path) -> None:
        manifest = _write_print(
            tmp_path,
            {
                "t": {"relationships": {"refers_to": [_edge(detection="declared")]}},
                "u": {"relationships": {"referenced_by": [_mirror(detection="declared")]}},
            },
        )

        assert rs.check_reciprocity(tmp_path, manifest) == []

    def test_a_mirror_with_no_edge_in_its_source(self, tmp_path: Path) -> None:
        manifest = _write_print(
            tmp_path,
            {
                "t": {"relationships": {"refers_to": [_edge(column="b")]}},
                "u": {
                    "relationships": {
                        "referenced_by": [_mirror(referencer="s.outside"), _mirror()],
                    },
                },
            },
        )

        issues = rs.check_reciprocity(tmp_path, manifest)

        assert [i for i in issues if i.code == "relationships.broken-reciprocity"] == [
            Issue(
                "s/u/relationships.yaml",
                "relationships.broken-reciprocity",
                "error",
                "referenced_by entry from s.t has no matching refers_to in its source table.",
                "§2.3.3",
            ),
        ]

    @pytest.mark.parametrize(
        "mirror",
        [_mirror(referencer="s.v"), _mirror(column="b"), _mirror(target_column="key")],
    )
    def test_a_mirror_must_match_its_edge_on_every_part_of_its_address(
        self,
        tmp_path: Path,
        mirror: dict[str, Any],
    ) -> None:
        manifest = _write_print(
            tmp_path,
            {
                "t": {"relationships": {"refers_to": [_edge()]}},
                "u": {"relationships": {"referenced_by": [mirror]}},
                "v": {"relationships": {"refers_to": [_edge(target="s.w")]}},
            },
        )

        codes = [i.code for i in rs.check_reciprocity(tmp_path, manifest)]

        assert "relationships.broken-reciprocity" in codes

    def test_an_edge_with_no_mirror_in_its_target(self, tmp_path: Path) -> None:
        manifest = _write_print(
            tmp_path,
            {
                "t": {"relationships": {"refers_to": ["x", _edge(target="s.outside"), _edge()]}},
                "u": {"relationships": {"referenced_by": []}},
            },
        )

        assert rs.check_reciprocity(tmp_path, manifest) == [
            Issue(
                "s/t/relationships.yaml::refers_to[2]",
                "relationships.unmirrored-refers-to",
                "error",
                "refers_to entry into s.u has no referenced_by mirror in that table's file.",
                "§2.3.6",
            ),
        ]

    def test_a_mirror_that_disagrees_on_a_shared_field(self, tmp_path: Path) -> None:
        manifest = _write_print(
            tmp_path,
            {
                "t": {
                    "relationships": {
                        "refers_to": [_edge(detection="inferred", on_delete="cascade")],
                    },
                },
                "u": {
                    "relationships": {
                        "referenced_by": ["x", _mirror(detection="declared", on_delete="cascade")],
                    },
                },
            },
        )

        assert rs.check_reciprocity(tmp_path, manifest) == [
            Issue(
                "s/u/relationships.yaml::referenced_by[1]",
                "relationships.mirror-mismatch",
                "error",
                "disagrees with its refers_to at s/t/relationships.yaml::refers_to[0]: detection "
                'refers_to="inferred" referenced_by="declared".',
                "§2.3.3",
            ),
        ]

    def test_two_edges_whose_fields_are_swapped_between_mirrors(self, tmp_path: Path) -> None:
        edges = [
            _edge(detection="inferred", on_delete="cascade"),
            _edge(detection="declared", on_delete="restrict"),
        ]
        mirrors = [
            _mirror(detection="inferred", on_delete="restrict"),
            _mirror(detection="declared", on_delete="cascade"),
        ]
        manifest = _write_print(
            tmp_path,
            {
                "t": {"relationships": {"refers_to": edges}},
                "u": {"relationships": {"referenced_by": mirrors}},
            },
        )

        [issue] = rs.check_reciprocity(tmp_path, manifest)

        assert issue.detail.endswith(": the pairing of fields across entries.")

    def test_a_field_differing_across_two_mirrors_lists_every_value_sorted(
        self,
        tmp_path: Path,
    ) -> None:
        edges = [_edge(on_delete={"b": 1, "a": 2}), _edge(on_delete="x")]
        mirrors = [_mirror(on_delete="x"), _mirror(on_delete={"a": 2, "b": 3})]
        manifest = _write_print(
            tmp_path,
            {
                "t": {"relationships": {"refers_to": edges}},
                "u": {"relationships": {"referenced_by": mirrors}},
            },
        )

        [issue] = rs.check_reciprocity(tmp_path, manifest)

        assert issue.detail.endswith(
            ': on_delete refers_to="x", {"a": 2, "b": 1} referenced_by="x", {"a": 2, "b": 3}.',
        )

    def test_unreadable_tables_are_passed_over_and_the_rest_still_read(
        self,
        tmp_path: Path,
    ) -> None:
        seen: list[tuple[str, int, int]] = []
        manifest = _write_print(
            tmp_path,
            {
                "a": {"statistics": {}},
                "b": {"relationships": None},
                "c": {"relationships": "refers_to: [unclosed\n"},
                "d": {"relationships": ["not a mapping"]},
                "t": {
                    "relationships": {
                        "refers_to": [_edge()],
                        "referenced_by": ["x", _mirror(referencer="s.u", column="q")],
                    },
                },
                "u": {"relationships": {"referenced_by": []}},
            },
        )

        issues = rs.check_reciprocity(
            tmp_path,
            manifest,
            on_table=lambda *event: seen.append(event),
        )

        assert [(i.path, i.code) for i in issues] == [
            ("s/t/relationships.yaml", "relationships.broken-reciprocity"),
            ("s/t/relationships.yaml::refers_to[0]", "relationships.unmirrored-refers-to"),
        ]
        assert seen == [(f"s.{name}", i, 6) for i, name in enumerate("abcdtu", 1)]

    def test_an_entry_with_no_path_reads_the_connection_root(self, tmp_path: Path) -> None:
        (tmp_path / "relationships.yaml").write_text(
            yaml.safe_dump({"refers_to": [_edge(target="s.t")]}),
        )
        manifest = {"tables": {"s.t": {"artifacts": {"relationships": "relationships.yaml"}}}}

        assert [i.path for i in rs.check_reciprocity(tmp_path, manifest)] == [
            "relationships.yaml::refers_to[0]",
        ]

    @pytest.mark.parametrize(
        "edge",
        [
            {"column": ["a"], "target_table": 5, "target_column": ["id"]},
            {"column": "a", "target_table": "s.u", "target_column": ["id"]},
            {"column": ["a"], "target_table": "s.u", "target_column": [7]},
        ],
    )
    def test_an_edge_whose_address_is_malformed_is_left_to_the_schema(
        self,
        tmp_path: Path,
        edge: dict[str, Any],
    ) -> None:
        manifest = _write_print(
            tmp_path,
            {
                "t": {"relationships": {"refers_to": [edge]}},
                "u": {"relationships": {"referenced_by": []}},
            },
        )

        assert rs.check_reciprocity(tmp_path, manifest) == []

    def test_a_mirror_whose_columns_are_missing_matches_an_edge_with_none(
        self,
        tmp_path: Path,
    ) -> None:
        manifest = _write_print(
            tmp_path,
            {
                "t": {"relationships": {"refers_to": [{"target_table": "s.u"}]}},
                "u": {"relationships": {"referenced_by": [{"referencer_table": "s.t"}]}},
            },
        )

        assert rs.check_reciprocity(tmp_path, manifest) == []


_CHILD = pack_sketch([10, 20, 30, 40])
_PARENT = pack_sketch([20, 40, 60])


def _observed_print(
    tmp_path: Path,
    observed: dict[str, Any],
    *,
    source: dict[str, Any] | None = None,
    target: dict[str, Any] | None = None,
    edge: dict[str, Any] | None = None,
    target_files: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[Issue]]:
    source_column = (
        source if source is not None else {"cardinality": 4, "sketch": {"values": _CHILD}}
    )
    target_column = (
        target if target is not None else {"cardinality": 3, "sketch": {"values": _PARENT}}
    )
    manifest = _write_print(
        tmp_path,
        {
            "t": {
                "relationships": {"refers_to": ["x", {**(edge or _edge()), "observed": observed}]},
                "statistics": {"row_count": 8, "columns": {"a": source_column}},
            },
            "u": target_files
            if target_files is not None
            else {"statistics": {"columns": {"id": target_column}}},
        },
    )

    return manifest, rs.check_observed_arithmetic(tmp_path, manifest)


_WHERE = "s/t/relationships.yaml::refers_to[1].observed"
_EXACT = {
    "fanout_avg": 2.0,
    "target_coverage": 0.666667,
    "containment": 0.5,
    "answerable_count": 4,
    "coherent": False,
}


def _mismatch(code: str, detail: str) -> Issue:
    return Issue(_WHERE, code, "error", detail, "§2.3.10")


class TestObservedArithmeticAgainstAnExhaustiveChild:
    def test_an_observed_block_that_recomputes_passes(self, tmp_path: Path) -> None:
        assert _observed_print(tmp_path, dict(_EXACT))[1] == []

    def test_a_fanout_that_does_not_recompute(self, tmp_path: Path) -> None:
        assert _observed_print(tmp_path, {**_EXACT, "fanout_avg": 2.5})[1] == [
            _mismatch(
                "relationships.observed-fanout-mismatch",
                "fanout_avg=2.5 does not match row_count/cardinality=2.0 from the referencing "
                "column's own statistics.",
            ),
        ]

    def test_a_coverage_that_does_not_recompute(self, tmp_path: Path) -> None:
        assert _observed_print(tmp_path, {**_EXACT, "target_coverage": 0.5})[1] == [
            _mismatch(
                "relationships.observed-coverage-mismatch",
                "target_coverage=0.5 does not match the sketch-measured estimate 0.666667 from "
                "the two endpoints' own sketches.",
            ),
        ]

    def test_a_containment_that_does_not_recompute(self, tmp_path: Path) -> None:
        assert _observed_print(tmp_path, {**_EXACT, "containment": 1.0})[1] == [
            _mismatch(
                "relationships.observed-containment-mismatch",
                "containment=1.0 does not match the sketch-measured estimate 0.5 from the two "
                "endpoints' own sketches.",
            ),
        ]

    def test_an_answerable_count_that_does_not_recompute(self, tmp_path: Path) -> None:
        assert _observed_print(tmp_path, {**_EXACT, "answerable_count": 3})[1] == [
            _mismatch(
                "relationships.observed-answerable-count-mismatch",
                "answerable_count=3 does not match 4, the count of the referencing column's own "
                "retained hashes below the shared threshold (§2.2.14).",
            ),
        ]

    def test_a_coherence_flag_the_cardinalities_contradict(self, tmp_path: Path) -> None:
        assert _observed_print(tmp_path, {**_EXACT, "coherent": True})[1] == [
            _mismatch(
                "relationships.observed-coherent-mismatch",
                "coherent=True but the referencing column's cardinality (4) exceeds the referenced "
                "column's (3).",
            ),
        ]

    def test_an_absent_coherence_flag_is_not_checked(self, tmp_path: Path) -> None:
        observed = {k: v for k, v in _EXACT.items() if k != "coherent"}

        assert _observed_print(tmp_path, observed)[1] == []


class TestObservedArithmeticWithAnEmptyAnswerableSubset:
    """The child's hashes all sit above a truncated parent's threshold: no evidence either way."""

    _PARENT_1024 = pack_sketch(list(range(1, 1025)))

    def _run(self, tmp_path: Path, observed: dict[str, Any]) -> list[Issue]:
        return _observed_print(
            tmp_path,
            observed,
            source={"cardinality": 2, "sketch": {"values": pack_sketch([2000, 3000])}},
            target={"cardinality": 5000, "sketch": {"values": self._PARENT_1024}},
        )[1]

    def test_coverage_falls_back_to_the_cardinality_ratio_and_the_rest_stay_absent(
        self,
        tmp_path: Path,
    ) -> None:
        assert self._run(tmp_path, {"fanout_avg": 4.0, "target_coverage": 0.0004}) == []

    def test_a_containment_and_count_published_anyway(self, tmp_path: Path) -> None:
        observed = {
            "fanout_avg": 4.0,
            "target_coverage": 0.0004,
            "containment": 0.0,
            "answerable_count": 0,
        }

        assert self._run(tmp_path, observed) == [
            _mismatch(
                "relationships.observed-containment-forbidden",
                "containment=0.0 is published, but the answerable subset between the two "
                "endpoints' sketches is empty - §2.3.10 forbids the field here.",
            ),
            _mismatch(
                "relationships.observed-answerable-count-mismatch",
                "answerable_count=0 is published, but the answerable subset between the two "
                "endpoints' sketches is empty - §2.3.10 forbids the field here.",
            ),
        ]


class TestObservedArithmeticAgainstATruncatedChild:
    """1,024 child hashes 1..1024 against 500 even parent hashes 2..1000, over a 2**64 domain."""

    def _run(self, tmp_path: Path, observed: dict[str, Any]) -> list[Issue]:
        return _observed_print(
            tmp_path,
            observed,
            source={"cardinality": 2**64, "sketch": {"values": pack_sketch(list(range(1, 1025)))}},
            target={
                "cardinality": 2**64,
                "sketch": {"values": pack_sketch(list(range(2, 1001, 2)))},
            },
        )[1]

    def test_the_estimate_scales_from_the_shared_threshold(self, tmp_path: Path) -> None:
        observed = {
            "fanout_avg": 0.0,
            "target_coverage": 0.488281,
            "containment": 0.488281,
            "answerable_count": 1023,
        }

        assert self._run(tmp_path, observed) == []

    def test_a_published_estimate_off_the_scaled_one(self, tmp_path: Path) -> None:
        observed = {
            "fanout_avg": 0.0,
            "target_coverage": 0.5,
            "containment": 0.5,
            "answerable_count": 1024,
        }

        assert [i.code for i in self._run(tmp_path, observed)] == [
            "relationships.observed-coverage-mismatch",
            "relationships.observed-containment-mismatch",
            "relationships.observed-answerable-count-mismatch",
        ]


class TestObservedArithmeticWithoutSketches:
    @pytest.mark.parametrize("sketch", [None, {"values": 5}, {"values": "not base64!"}, "x"])
    def test_coverage_is_the_cardinality_ratio(self, tmp_path: Path, sketch: Any) -> None:
        source = {"cardinality": 2, "sketch": sketch}

        assert _observed_print(
            tmp_path,
            {"fanout_avg": 4.0, "target_coverage": 0.9},
            source=source,
        )[1] == [
            _mismatch(
                "relationships.observed-coverage-mismatch",
                "target_coverage=0.9 does not match the cardinality ratio 0.666667 from the two "
                "endpoints' own statistics.",
            ),
        ]

    def test_a_coherence_flag_on_a_child_no_larger_than_its_parent(self, tmp_path: Path) -> None:
        observed = {"fanout_avg": 4.0, "target_coverage": 0.666667, "coherent": False}

        assert _observed_print(tmp_path, observed, source={"cardinality": 2})[1] == [
            _mismatch(
                "relationships.observed-coherent-mismatch",
                "coherent=False but the referencing column's cardinality (2) does not exceed the "
                "referenced column's (3).",
            ),
        ]


class TestObservedArithmeticIsSkippedWithoutBothEndpoints:
    _WRONG: ClassVar[dict[str, float]] = {"fanout_avg": 99.0, "target_coverage": 99.0}

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"edge": _edge(column="missing")},
            {"edge": {**_edge(), "column": ["a", "b"]}},
            {"edge": {**_edge(), "target_column": ["id", "x"]}},
            {"edge": {**_edge(), "target_column": "id"}},
            {"edge": {**_edge(), "column": "a"}},
            {"edge": _edge(target="s.outside")},
            {"edge": _edge(target_column="missing")},
            {"target_files": {"relationships": {}}},
            {"target_files": {"statistics": "columns: [unclosed\n"}},
            {"target_files": {"statistics": ["not a mapping"]}},
            {"target_files": {"statistics": None}},
            {"source": {"cardinality": 0}},
            {"source": {"cardinality": "4"}},
            {"target": {"cardinality": 0}},
            {"target": {"cardinality": 3.0}},
        ],
    )
    def test_nothing_to_recompute_against(self, tmp_path: Path, kwargs: dict[str, Any]) -> None:
        assert _observed_print(tmp_path, dict(self._WRONG), **kwargs)[1] == []

    def test_a_scope_incompatible_pair_carries_no_ratio(self, tmp_path: Path) -> None:
        assert _observed_print(tmp_path, {**self._WRONG, "scope_compatible": False})[1] == []

    def test_a_source_without_a_row_count(self, tmp_path: Path) -> None:
        manifest, _ = _observed_print(tmp_path, dict(self._WRONG))
        stats_path = tmp_path / "s" / "t" / "statistics.yaml"
        stats_path.write_text(yaml.safe_dump({"columns": {"a": {"cardinality": 4}}}))

        assert rs.check_observed_arithmetic(tmp_path, manifest) == []

    def test_unreadable_tables_are_passed_over_and_the_rest_still_read(
        self,
        tmp_path: Path,
    ) -> None:
        seen: list[tuple[str, int, int]] = []
        manifest, _ = _observed_print(tmp_path, dict(self._WRONG))
        extra = _write_print(
            tmp_path,
            {
                "a": {"relationships": {}},
                "b": {"relationships": {"refers_to": []}, "statistics": None},
                "c": {"relationships": None, "statistics": {"columns": {}}},
                "d": {"relationships": "refers_to: [unclosed\n", "statistics": {"columns": {}}},
                "e": {"relationships": ["not a mapping"], "statistics": {"columns": {}}},
                "f": {
                    "relationships": {"refers_to": [{"observed": "x"}]},
                    "statistics": {"columns": {}},
                },
            },
        )
        manifest["tables"] = {**extra["tables"], **manifest["tables"]}

        issues = rs.check_observed_arithmetic(
            tmp_path,
            manifest,
            on_table=lambda *event: seen.append(event),
        )

        assert {i.path for i in issues} == {_WHERE}
        assert seen == [(f"s.{name}", i, 8) for i, name in enumerate("abcdeftu", 1)]

    def test_an_entry_with_no_path_reads_the_connection_root(self, tmp_path: Path) -> None:
        (tmp_path / "relationships.yaml").write_text(
            yaml.safe_dump(
                {
                    "refers_to": [
                        {**_edge(target="s.t", target_column="a"), "observed": {"fanout_avg": 9.0}},
                    ],
                },
            ),
        )
        (tmp_path / "statistics.yaml").write_text(
            yaml.safe_dump({"row_count": 8, "columns": {"a": {"cardinality": 4}}}),
        )
        artifacts = {"relationships": "relationships.yaml", "statistics": "statistics.yaml"}
        manifest = {"tables": {"s.t": {"artifacts": artifacts}}}

        assert [i.path for i in rs.check_observed_arithmetic(tmp_path, manifest)] == [
            "relationships.yaml::refers_to[0].observed",
            "relationships.yaml::refers_to[0].observed",
        ]


class TestMoreReciprocity:
    def test_an_unmirrored_edge_does_not_stop_the_edges_after_it(self, tmp_path: Path) -> None:
        manifest = _write_print(
            tmp_path,
            {
                "t": {
                    "relationships": {
                        "refers_to": [_edge(), _edge(target="s.v", detection="inferred")],
                    },
                },
                "u": {"relationships": {"referenced_by": []}},
                "v": {"relationships": {"referenced_by": [_mirror(detection="declared")]}},
            },
        )

        assert [i.code for i in rs.check_reciprocity(tmp_path, manifest)] == [
            "relationships.unmirrored-refers-to",
            "relationships.mirror-mismatch",
        ]

    def test_mapping_values_compare_by_content_not_key_order(self, tmp_path: Path) -> None:
        manifest = _write_print(
            tmp_path,
            {
                "t": {"relationships": {"refers_to": [_edge(on_delete={"b": 1, "a": 2})]}},
                "u": {"relationships": {"referenced_by": [_mirror(on_delete={"a": 2, "b": 1})]}},
            },
        )

        assert rs.check_reciprocity(tmp_path, manifest) == []

    def test_every_differing_field_is_named(self, tmp_path: Path) -> None:
        manifest = _write_print(
            tmp_path,
            {
                "t": {
                    "relationships": {
                        "refers_to": [_edge(detection="inferred", on_delete="cascade")],
                    },
                },
                "u": {
                    "relationships": {
                        "referenced_by": [_mirror(detection="declared", on_delete="restrict")],
                    },
                },
            },
        )

        [issue] = rs.check_reciprocity(tmp_path, manifest)

        assert issue.detail.endswith(
            ': detection refers_to="inferred" referenced_by="declared"; '
            'on_delete refers_to="cascade" referenced_by="restrict".',
        )


class TestMoreArrayLengths:
    @pytest.mark.parametrize(
        ("entry", "detail"),
        [
            (
                {"target_column": ["id"]},
                "len(column)=0 differs from len(target_column)=1; arrays must match.",
            ),
            (
                {"column": ["a"]},
                "len(column)=1 differs from len(target_column)=0; arrays must match.",
            ),
        ],
    )
    def test_a_missing_array_reads_as_empty(self, entry: dict[str, Any], detail: str) -> None:
        assert [i.detail for i in rs.check_entry({"refers_to": [entry]}, "r.yaml", "s.t")] == [
            detail,
        ]


class TestMoreObservedArithmetic:
    def test_a_ratio_rounds_to_six_places(self, tmp_path: Path) -> None:
        source = {"cardinality": 3, "sketch": {"values": pack_sketch([10, 20, 30])}}
        target = {"cardinality": 2, "sketch": {"values": pack_sketch([20, 60])}}
        observed = {
            "fanout_avg": 2.666667,
            "target_coverage": 0.5,
            "containment": 0.333333,
            "answerable_count": 3,
        }

        assert _observed_print(tmp_path, observed, source=source, target=target)[1] == []

    def test_coverage_is_capped_at_one_when_the_child_counts_more_values(
        self,
        tmp_path: Path,
    ) -> None:
        source = {"cardinality": 3, "sketch": {"values": pack_sketch([10, 20, 30])}}
        target = {"cardinality": 2, "sketch": {"values": pack_sketch([10, 20, 30])}}
        observed = {
            "fanout_avg": 2.666667,
            "target_coverage": 1.0,
            "containment": 1.0,
            "answerable_count": 3,
        }

        assert _observed_print(tmp_path, observed, source=source, target=target)[1] == []

    def test_a_scaled_estimate_is_capped_at_one(self, tmp_path: Path) -> None:
        source = {"cardinality": 2**62, "sketch": {"values": pack_sketch(list(range(1, 1025)))}}
        target = {"cardinality": 2**62, "sketch": {"values": pack_sketch(list(range(2, 1001, 2)))}}
        observed = {
            "fanout_avg": 0.0,
            "target_coverage": 1.0,
            "containment": 1.0,
            "answerable_count": 1023,
        }

        assert _observed_print(tmp_path, observed, source=source, target=target)[1] == []

    def test_equal_cardinalities_do_not_exceed_each_other(self, tmp_path: Path) -> None:
        observed = {"fanout_avg": 2.666667, "target_coverage": 1.0, "coherent": False}

        [issue] = _observed_print(tmp_path, observed, source={"cardinality": 3})[1]

        assert issue.detail == (
            "coherent=False but the referencing column's cardinality (3) does not exceed the "
            "referenced column's (3)."
        )


class TestMirrorValuesOfAnyShape:
    def _write(self, tmp_path: Path, edge_extra: str, mirror_extra: str) -> dict[str, Any]:
        for name, text in (
            (
                "t",
                f"refers_to:\n- column: [a]\n  target_table: s.u\n  target_column: [id]\n{edge_extra}",
            ),
            (
                "u",
                f"referenced_by:\n- referencer_table: s.t\n  referencer_column: [a]\n  column: [id]\n{mirror_extra}",
            ),
        ):
            (tmp_path / "s" / name).mkdir(parents=True)
            (tmp_path / "s" / name / "relationships.yaml").write_text(text)

        artifacts = {"relationships": "relationships.yaml"}

        return {
            "tables": {
                "s.t": {"path": "s/t", "artifacts": artifacts},
                "s.u": {"path": "s/u", "artifacts": artifacts},
            },
        }

    def test_a_value_json_cannot_spell_is_compared_by_its_text(self, tmp_path: Path) -> None:
        manifest = self._write(
            tmp_path,
            "  on_delete: !!binary aGk=\n",
            "  on_delete: !!binary aGk=\n",
        )

        assert rs.check_reciprocity(tmp_path, manifest) == []

    def test_a_differing_field_beside_one_json_cannot_spell(self, tmp_path: Path) -> None:
        manifest = self._write(
            tmp_path,
            "  on_delete: !!binary aGk=\n  detection: inferred\n  on_update: {a: 2, b: 1}\n",
            "  on_delete: !!binary aGk=\n  detection: declared\n  on_update: {b: 1, a: 2}\n",
        )

        [issue] = rs.check_reciprocity(tmp_path, manifest)

        assert issue.detail.endswith(': detection refers_to="inferred" referenced_by="declared".')
