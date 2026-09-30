"""relationships.annotations.yaml checked against the table's own relationships.yaml, per SPEC 2.7.2."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from dbprint.conformance import relationship_annotations as ra
from dbprint.conformance.issue import Issue


_ANN = "s/t/relationships.annotations.yaml"
_EDGE = {"column": ["a"], "target_table": "s.u", "target_column": ["id"]}
_BOTH = {
    "relationships": "relationships.yaml",
    "relationships_annotations": "relationships.annotations.yaml",
}


def _print(
    root: Path,
    refers_to: list[Any],
    annotations: Any,
    *,
    path: str = "s/t",
) -> dict[str, Any]:
    table_dir = root / path
    table_dir.mkdir(parents=True, exist_ok=True)
    (table_dir / "relationships.yaml").write_text(yaml.safe_dump({"refers_to": refers_to}))
    (table_dir / "relationships.annotations.yaml").write_text(yaml.safe_dump(annotations))

    return {"tables": {"s.t": {"path": path, "artifacts": dict(_BOTH)}}}


def _unknown(index: int = 0, rel: str = _ANN) -> Issue:
    return Issue(
        f"{rel}::refers_to[{index}]",
        "annotations.unknown-edge",
        "warning",
        "relationships.annotations.yaml carries a verdict for an edge relationships.yaml does "
        "not emit.",
        "§2.7.2",
    )


class TestVerdicts:
    def test_a_verdict_on_an_edge_the_table_does_not_emit(self, tmp_path: Path) -> None:
        manifest = _print(tmp_path, [], {"refers_to": [{**_EDGE, "verdict": "rejected"}]})

        assert ra.check_verdicts(tmp_path, manifest) == [_unknown()]

    def test_a_verdict_on_a_declared_edge(self, tmp_path: Path) -> None:
        emitted = [{**_EDGE, "detection": "declared"}]
        manifest = _print(tmp_path, emitted, {"refers_to": [{**_EDGE, "verdict": "rejected"}]})

        assert ra.check_verdicts(tmp_path, manifest) == [
            Issue(
                f"{_ANN}::refers_to[0]",
                "annotations.verdict-on-declared-edge",
                "warning",
                "verdict addresses a declared edge; an annotation may correct an inference, never "
                "contradict a measurement (§2.4).",
                "§2.7.2",
            ),
        ]

    def test_a_verdict_on_an_inferred_edge_passes(self, tmp_path: Path) -> None:
        emitted = [{**_EDGE, "detection": "inferred"}]
        manifest = _print(tmp_path, emitted, {"refers_to": [{**_EDGE, "verdict": "rejected"}]})

        assert ra.check_verdicts(tmp_path, manifest) == []

    def test_an_emitted_edge_with_no_detection_is_still_emitted(self, tmp_path: Path) -> None:
        manifest = _print(
            tmp_path,
            [dict(_EDGE)],
            {"refers_to": [{**_EDGE, "verdict": "rejected"}]},
        )

        assert ra.check_verdicts(tmp_path, manifest) == []

    def test_an_entry_with_no_verdict_is_a_human_addition(self, tmp_path: Path) -> None:
        manifest = _print(tmp_path, [], {"refers_to": [dict(_EDGE)]})

        assert ra.check_verdicts(tmp_path, manifest) == []

    def test_an_edge_to_another_table_is_another_edge(self, tmp_path: Path) -> None:
        emitted = [{**_EDGE, "target_table": "s.v", "detection": "inferred"}]
        manifest = _print(tmp_path, emitted, {"refers_to": [{**_EDGE, "verdict": "rejected"}]})

        assert ra.check_verdicts(tmp_path, manifest) == [_unknown()]

    def test_an_address_whose_target_columns_are_not_a_list_matches_nothing(
        self,
        tmp_path: Path,
    ) -> None:
        emitted = [{**_EDGE, "detection": "inferred"}, {**_EDGE, "target_column": "id"}]
        annotated = {**_EDGE, "target_column": "id", "verdict": "rejected"}
        manifest = _print(tmp_path, emitted, {"refers_to": [annotated]})

        assert ra.check_verdicts(tmp_path, manifest) == [_unknown()]

    def test_every_entry_is_read_past_one_that_is_not_a_mapping(self, tmp_path: Path) -> None:
        manifest = _print(
            tmp_path,
            ["edge", {**_EDGE, "detection": "declared"}],
            {"refers_to": ["x", {**_EDGE, "verdict": "rejected"}]},
        )

        assert [i.code for i in ra.check_verdicts(tmp_path, manifest)] == [
            "annotations.verdict-on-declared-edge",
        ]

    def test_an_entry_with_no_path_reads_the_connection_root(self, tmp_path: Path) -> None:
        manifest = _print(tmp_path, [], {"refers_to": [{**_EDGE, "verdict": "rejected"}]}, path="")
        manifest["tables"]["s.t"].pop("path")

        assert ra.check_verdicts(tmp_path, manifest) == [
            _unknown(rel="relationships.annotations.yaml"),
        ]


class TestEveryTableIsVisited:
    """A table that cannot be checked is passed over; the tables after it are still read."""

    def _manifest(self, tmp_path: Path) -> dict[str, Any]:
        stale = {
            "refers_to": [
                {**_EDGE, "verdict": "rejected", "claims": {"observed.containment": 0.5}},
            ],
        }
        tables: dict[str, Any] = {}

        for name, relationships, annotated in (
            ("a", None, None),
            ("b", "missing", None),
            ("c", "refers_to: [unclosed\n", stale),
            ("d", {"refers_to": []}, ["not a mapping"]),
            ("e", {"refers_to": []}, {"refers_to": "not a list"}),
            ("f", {"refers_to": []}, stale),
        ):
            artifacts: dict[str, str] = {}

            if relationships is not None:
                artifacts = dict(_BOTH)
                table_dir = tmp_path / "s" / name
                table_dir.mkdir(parents=True)

                if relationships != "missing":
                    text = (
                        relationships
                        if isinstance(relationships, str)
                        else yaml.safe_dump(relationships)
                    )
                    (table_dir / "relationships.yaml").write_text(text)
                    (table_dir / "relationships.annotations.yaml").write_text(
                        yaml.safe_dump(annotated),
                    )

            tables[f"s.{name}"] = {"path": f"s/{name}", "artifacts": artifacts}

        return {"tables": tables}

    def test_verdicts(self, tmp_path: Path) -> None:
        assert ra.check_verdicts(tmp_path, self._manifest(tmp_path)) == [
            _unknown(rel="s/f/relationships.annotations.yaml"),
        ]

    def test_claims(self, tmp_path: Path) -> None:
        assert [i.path for i in ra.check_claims(tmp_path, self._manifest(tmp_path))] == [
            "s/f/relationships.annotations.yaml::refers_to[0].claims.observed.containment",
        ]

    @pytest.mark.parametrize("check", [ra.check_verdicts, ra.check_claims])
    def test_each_table_is_announced_by_name_and_position(self, tmp_path: Path, check: Any) -> None:
        seen: list[tuple[str, int, int]] = []

        check(tmp_path, self._manifest(tmp_path), on_table=lambda *event: seen.append(event))

        assert seen == [(f"s.{name}", i, 6) for i, name in enumerate("abcdef", 1)]


def _claim(tmp_path: Path, stat: Any, raw: Any, observed: dict[str, Any] | None) -> list[Issue]:
    edge = dict(_EDGE) if observed is None else {**_EDGE, "observed": observed}
    manifest = _print(tmp_path, [edge], {"refers_to": ["x", {**_EDGE, "claims": {stat: raw}}]})

    return ra.check_claims(tmp_path, manifest)


def _claim_issue(stat: str, code: str, detail: str) -> Issue:
    return Issue(f"{_ANN}::refers_to[1].claims.{stat}", code, "warning", detail, "§2.7.2")


class TestClaims:
    def test_a_claim_the_edge_meets_passes(self, tmp_path: Path) -> None:
        assert _claim(tmp_path, "observed.containment", {"min": 0.9}, {"containment": 0.95}) == []

    def test_a_claim_the_edge_contradicts(self, tmp_path: Path) -> None:
        assert _claim(tmp_path, "observed.containment", {"min": 0.9}, {"containment": 0.5}) == [
            _claim_issue(
                "observed.containment",
                "annotations.claim-contradicts-statistic",
                "claims.observed.containment={'min': 0.9} contradicts the measured value: actual "
                "0.5 < min 0.9",
            ),
        ]

    def test_a_stat_outside_the_edge_vocabulary(self, tmp_path: Path) -> None:
        assert _claim(tmp_path, "observed.fanout", 1, {"fanout_avg": 1}) == [
            _claim_issue(
                "observed.fanout",
                "annotations.claim-unassertable",
                "'observed.fanout' is not a checkable edge stat",
            ),
        ]

    def test_a_predicate_of_no_known_shape(self, tmp_path: Path) -> None:
        assert _claim(tmp_path, "observed.fanout_max", {"above": 1}, {"fanout_max": 1}) == [
            _claim_issue(
                "observed.fanout_max",
                "annotations.claim-unassertable",
                "range predicate accepts only min and/or max keys",
            ),
        ]

    def test_a_stat_the_edge_does_not_carry(self, tmp_path: Path) -> None:
        assert _claim(tmp_path, "observed.fanout_max", 1, None) == [
            _claim_issue(
                "observed.fanout_max",
                "annotations.claim-unassertable",
                "'observed.fanout_max' not emitted for this edge",
            ),
        ]

    def test_a_predicate_that_cannot_be_compared(self, tmp_path: Path) -> None:
        assert _claim(tmp_path, "observed.fanout_max", [1], {"fanout_max": 1}) == [
            _claim_issue(
                "observed.fanout_max",
                "annotations.claim-unassertable",
                "expected [1], actual 1 - incompatible types",
            ),
        ]

    def test_entries_with_no_claims_do_not_stop_the_entries_after_them(
        self,
        tmp_path: Path,
    ) -> None:
        edge = {**_EDGE, "observed": {"containment": 0.5}}
        entries = [
            {**_EDGE, "claims": "none"},
            dict(_EDGE),
            {**_EDGE, "claims": {"observed.containment": 1.0}},
        ]
        manifest = _print(tmp_path, ["edge", edge], {"refers_to": entries})

        assert [i.code for i in ra.check_claims(tmp_path, manifest)] == [
            "annotations.claim-contradicts-statistic",
        ]

    def test_an_entry_with_no_path_reads_the_connection_root(self, tmp_path: Path) -> None:
        edge = {**_EDGE, "observed": {"containment": 0.5}}
        annotated = {**_EDGE, "claims": {"observed.containment": 1.0}}
        manifest = _print(tmp_path, [edge], {"refers_to": [annotated]}, path="")
        manifest["tables"]["s.t"].pop("path")

        assert [i.path for i in ra.check_claims(tmp_path, manifest)] == [
            "relationships.annotations.yaml::refers_to[0].claims.observed.containment",
        ]


class TestTheAnnotationFileItself:
    def test_a_path_endpoint_in_refers_to_is_checked(self) -> None:
        body = {"refers_to": ["x", {**_EDGE, "column": ["a", "b"], "path": ["k"]}]}

        assert [(i.path, i.code) for i in ra.check_entry(body, "r.yaml", "s.t")] == [
            ("r.yaml::refers_to[1]", "relationships.path-on-composite-endpoint"),
        ]

    def test_a_body_that_is_not_a_mapping_is_left_to_the_schema(self) -> None:
        assert ra.check_entry(["refers_to"], "r.yaml", "s.t") == []
