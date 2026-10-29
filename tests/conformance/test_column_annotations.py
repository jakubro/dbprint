"""statistics.annotations.yaml checked against the table's own statistics.yaml, per SPEC 2.7.1."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from dbprint.conformance import column_annotations as ca
from dbprint.conformance.issue import Issue


_ANN = "s/t/statistics.annotations.yaml"
_BOTH = {"statistics": "statistics.yaml", "statistics_annotations": "statistics.annotations.yaml"}
_COLOR = {
    "sql_type": "text",
    "nullable": False,
    "null_count": 0,
    "null_rate": 0.0,
    "classification": "categorical",
    "cardinality": 2,
    "cardinality_ratio": 0.2,
    "cardinality_method": "exact",
    "values": [{"value": "red", "count": 6}, {"value": "blue", "count": 4}],
    "values_coverage": 1.0,
    "distribution": "uniform",
}
_STATS = {"row_count": 10, "columns": {"color": _COLOR, "code": {**_COLOR, "redacted": "mask"}}}
_CHECKS = [ca.check_stale_keys, ca.check_grain_annotations, ca.check_claims, ca.check_value_notes]


def _print(
    root: Path,
    annotations: Any,
    statistics: Any = _STATS,
    *,
    path: str = "s/t",
) -> dict[str, Any]:
    table_dir = root / path
    table_dir.mkdir(parents=True, exist_ok=True)
    (table_dir / "statistics.yaml").write_text(yaml.safe_dump(statistics))
    (table_dir / "statistics.annotations.yaml").write_text(
        yaml.safe_dump(_admissible(annotations), sort_keys=False),
    )

    return {"tables": {"s.t": {"path": path, "artifacts": dict(_BOTH)}}}


def _admissible(annotations: Any) -> Any:
    """The fields the schema requires, around a body that names only what its test reads."""

    if not isinstance(annotations, dict):
        return annotations

    return {"format_version": 1, "columns": {}, **annotations}


class TestStaleKeys:
    def test_a_note_on_a_column_the_statistics_lack(self, tmp_path: Path) -> None:
        manifest = _print(tmp_path, {"columns": {"color": {}, "shade": {"note": "x"}}})

        assert ca.check_stale_keys(tmp_path, manifest) == [
            Issue(
                f"{_ANN}::columns.shade",
                "annotations.unknown-column",
                "warning",
                "statistics.annotations.yaml names column 'shade', which statistics.yaml does not "
                "have.",
                "§2.7.1",
            ),
        ]


class TestGrainKeys:
    def test_a_grain_key_naming_an_unknown_column(self, tmp_path: Path) -> None:
        keys = [{"columns": ["color"]}, {"columns": ["color", "shade", "hue"]}]
        manifest = _print(tmp_path, {"grain": {"keys": keys}})

        assert ca.check_grain_annotations(tmp_path, manifest) == [
            Issue(
                f"{_ANN}::grain.keys[1]",
                "annotations.grain-unknown-column",
                "warning",
                "statistics.annotations.yaml's grain names column(s) ['shade', 'hue'], which "
                "statistics.yaml does not have.",
                "§2.7.1",
            ),
        ]

    @pytest.mark.parametrize(
        "grain",
        ["color", {"keys": "color"}, {"keys": [{"columns": ["color"]}]}],
    )
    def test_a_grain_with_nothing_unknown_passes(self, tmp_path: Path, grain: Any) -> None:
        assert ca.check_grain_annotations(tmp_path, _print(tmp_path, {"grain": grain})) == []


def _claims(tmp_path: Path, column: str, claims: dict[str, Any]) -> list[Issue]:
    annotations = {
        "columns": {"second": {"note": "x"}, column: {"claims": claims}},
    }

    return ca.check_claims(tmp_path, _print(tmp_path, annotations))


def _unassertable(column: str, stat: str, reason: str) -> Issue:
    return Issue(
        f"{_ANN}::columns.{column}.claims.{stat}",
        "annotations.claim-unassertable",
        "warning",
        reason,
        "§2.7.1",
    )


class TestClaims:
    def test_a_claim_the_column_meets_passes(self, tmp_path: Path) -> None:
        assert _claims(tmp_path, "color", {"null_count": 0}) == []

    def test_a_claim_the_column_contradicts(self, tmp_path: Path) -> None:
        assert _claims(tmp_path, "color", {"cardinality": {"max": 1}}) == [
            Issue(
                f"{_ANN}::columns.color.claims.cardinality",
                "annotations.claim-contradicts-statistic",
                "warning",
                "claims.cardinality={'max': 1} contradicts the measured value: actual 2 > max 1",
                "§2.7.1",
            ),
        ]

    def test_a_contradicted_verdict_names_the_measurement_behind_it(self, tmp_path: Path) -> None:
        assert _claims(tmp_path, "color", {"candidate_key": True}) == [
            Issue(
                f"{_ANN}::columns.color.claims.candidate_key",
                "annotations.claim-contradicts-statistic",
                "warning",
                "claims.candidate_key=True contradicts the measured value: expected True, actual "
                "False (cardinality_ratio 0.2)",
                "§2.7.1",
            ),
        ]

    def test_a_stat_outside_the_vocabulary(self, tmp_path: Path) -> None:
        assert _claims(tmp_path, "color", {"spread": 1}) == [
            _unassertable("color", "spread", "'spread' is not a checkable stat"),
        ]

    def test_a_value_bearing_stat_on_a_redacted_column(self, tmp_path: Path) -> None:
        assert _claims(tmp_path, "code", {"accepted_values": ["red"]}) == [
            _unassertable("code", "accepted_values", "column is redacted ('mask')"),
        ]

    def test_a_predicate_of_the_wrong_shape(self, tmp_path: Path) -> None:
        assert _claims(tmp_path, "color", {"accepted_values": "red"}) == [
            _unassertable("color", "accepted_values", "accepted_values requires a list"),
        ]

    def test_a_range_with_no_bound_is_never_silently_accepted(self, tmp_path: Path) -> None:
        assert _claims(tmp_path, "color", {"null_rate": {"max": None}}) == [
            _unassertable(
                "color",
                "null_rate",
                "range predicate needs a min or a max that is not null",
            ),
        ]

    def test_a_stat_the_column_does_not_carry(self, tmp_path: Path) -> None:
        assert _claims(tmp_path, "color", {"percentiles.p50": 3}) == [
            _unassertable(
                "color",
                "percentiles.p50",
                "stat 'percentiles.p50' not emitted for column 'color': not carried by categorical",
            ),
        ]

    def test_a_predicate_that_cannot_be_compared(self, tmp_path: Path) -> None:
        assert _claims(tmp_path, "color", {"null_count": [0]}) == [
            _unassertable("color", "null_count", "expected [0], actual 0 - incompatible types"),
        ]

    def test_a_claim_on_a_column_the_statistics_lack_is_left_to_the_stale_key_check(
        self,
        tmp_path: Path,
    ) -> None:
        assert _claims(tmp_path, "shade", {"null_count": 5}) == []


class TestValueNotes:
    def _notes(
        self,
        tmp_path: Path,
        column: str,
        values: list[Any],
        statistics: dict[str, Any] = _STATS,
    ) -> list[Issue]:
        annotations = {
            "columns": {"second": {"note": "y"}, column: {"values": values}},
        }

        return ca.check_value_notes(tmp_path, _print(tmp_path, annotations, statistics))

    def test_a_note_on_a_value_the_exhaustive_list_lacks(self, tmp_path: Path) -> None:
        values = [{"value": "red", "note": "warm"}, {"value": "green", "note": "x"}]

        assert self._notes(tmp_path, "color", values) == [
            Issue(
                f"{_ANN}::columns.color.values[1]",
                "annotations.unknown-value",
                "warning",
                "note names value 'green', which statistics.yaml's exhaustive values list does not "
                "have.",
                "§2.7.1",
            ),
        ]

    def test_every_note_on_a_redacted_column_is_unassertable(self, tmp_path: Path) -> None:
        values = [{"value": "a", "note": "x"}, {"value": "b", "note": "y"}]

        assert self._notes(tmp_path, "code", values) == [
            Issue(
                f"{_ANN}::columns.code.values[{i}]",
                "annotations.value-note-unassertable",
                "warning",
                "column is redacted",
                "§2.7.1",
            )
            for i in (0, 1)
        ]

    def test_a_list_exhaustive_over_a_scoped_read_is_not_the_table_domain(
        self,
        tmp_path: Path,
    ) -> None:
        statistics = {**_STATS, "scope": {"rows_scanned": 10, "sample": 0.5}}

        assert self._notes(tmp_path, "color", [{"value": "green", "note": "x"}], statistics) == []

    def test_a_note_on_a_column_the_statistics_lack_is_left_to_the_stale_key_check(
        self,
        tmp_path: Path,
    ) -> None:
        assert self._notes(tmp_path, "shade", [{"value": "green", "note": "x"}]) == []


class TestEveryTableIsVisited:
    """A table that cannot be checked is passed over; the tables after it are still read."""

    def _manifest(self, tmp_path: Path) -> dict[str, Any]:
        finding = {
            "columns": {
                "color": {"claims": {"null_count": 3}, "values": [{"value": "green", "note": "x"}]},
                "shade": {},
            },
            "grain": {"keys": [{"columns": ["shade"]}]},
        }
        tables: dict[str, Any] = {}

        for name, statistics, annotated in (
            ("a", None, None),
            ("b", "missing", None),
            ("c", "columns: [unclosed\n", finding),
            ("d", _STATS, ["not a mapping"]),
            ("e", _STATS, {"columns": "not a mapping", "grain": "none"}),
            ("f", {"columns": ["not a mapping"]}, finding),
            ("g", _STATS, finding),
        ):
            artifacts: dict[str, str] = {}

            if statistics is not None:
                artifacts = dict(_BOTH)
                table_dir = tmp_path / "s" / name
                table_dir.mkdir(parents=True)

                if statistics != "missing":
                    text = statistics if isinstance(statistics, str) else yaml.safe_dump(statistics)
                    (table_dir / "statistics.yaml").write_text(text)
                    (table_dir / "statistics.annotations.yaml").write_text(
                        yaml.safe_dump(_admissible(annotated), sort_keys=False),
                    )

            tables[f"s.{name}"] = {"path": f"s/{name}", "artifacts": artifacts}

        return {"tables": tables}

    @pytest.mark.parametrize(
        ("check", "paths"),
        [
            (
                ca.check_stale_keys,
                [
                    "s/f/statistics.annotations.yaml::columns.color",
                    "s/f/statistics.annotations.yaml::columns.shade",
                    "s/g/statistics.annotations.yaml::columns.shade",
                ],
            ),
            (
                ca.check_grain_annotations,
                [
                    "s/f/statistics.annotations.yaml::grain.keys[0]",
                    "s/g/statistics.annotations.yaml::grain.keys[0]",
                ],
            ),
            (ca.check_claims, ["s/g/statistics.annotations.yaml::columns.color.claims.null_count"]),
            (ca.check_value_notes, ["s/g/statistics.annotations.yaml::columns.color.values[0]"]),
        ],
    )
    def test_findings_come_from_every_readable_table(
        self,
        tmp_path: Path,
        check: Any,
        paths: list[str],
    ) -> None:
        assert [i.path for i in check(tmp_path, self._manifest(tmp_path))] == paths

    @pytest.mark.parametrize("check", _CHECKS)
    def test_each_table_is_announced_by_name_and_position(self, tmp_path: Path, check: Any) -> None:
        seen: list[tuple[str, int, int]] = []

        check(tmp_path, self._manifest(tmp_path), on_table=lambda *event: seen.append(event))

        assert seen == [(f"s.{name}", i, 7) for i, name in enumerate("abcdefg", 1)]

    @pytest.mark.parametrize("check", _CHECKS)
    def test_an_entry_with_no_path_reads_the_connection_root(
        self,
        tmp_path: Path,
        check: Any,
    ) -> None:
        annotations = {
            "columns": {
                "color": {"claims": {"null_count": 3}, "values": [{"value": "green", "note": "x"}]},
                "shade": {},
            },
            "grain": {"keys": [{"columns": ["shade"]}]},
        }
        manifest = _print(tmp_path, annotations, path="")
        manifest["tables"]["s.t"].pop("path")

        assert [i.path.split("::")[0] for i in check(tmp_path, manifest)] == [
            "statistics.annotations.yaml",
        ]


def test_the_annotation_file_alone_carries_no_content_rule() -> None:
    assert ca.check_entry({"columns": {"a": {"note": "x"}}}, "a.yaml", "s.t") == []


class TestAClaimIsReadAgainstTheScopeAndTypeOfItsColumn:
    def test_a_value_list_exhaustive_over_a_scoped_read_cannot_settle_a_domain_claim(
        self,
        tmp_path: Path,
    ) -> None:
        statistics = {**_STATS, "scope": {"rows_scanned": 10, "sample": 0.5}}
        annotations = {"columns": {"color": {"claims": {"accepted_values": ["red", "blue"]}}}}

        [issue] = ca.check_claims(tmp_path, _print(tmp_path, annotations, statistics))

        assert (issue.code, issue.path) == (
            "annotations.claim-unassertable",
            f"{_ANN}::columns.color.claims.accepted_values",
        )
        assert "complete over the rows scanned only" in issue.detail

    def test_a_numeric_string_verdict_is_never_expected_on_a_numeric_type(
        self,
        tmp_path: Path,
    ) -> None:
        column = {**_COLOR, "sql_type": "integer", "inferred": {"looks_like": "phone"}}
        statistics = {"row_count": 10, "columns": {"color": column}}
        annotations = {"columns": {"color": {"claims": {"looks_like": "numeric_string"}}}}

        assert ca.check_claims(tmp_path, _print(tmp_path, annotations, statistics)) == [
            _unassertable(
                "color",
                "looks_like",
                "looks_like 'numeric_string' is never published on a numeric SQL type",
            ),
        ]

    def test_a_claim_on_a_column_the_statistics_lack_does_not_stop_the_columns_after_it(
        self,
        tmp_path: Path,
    ) -> None:
        annotations = {
            "columns": {
                "shade": {"claims": {"null_count": 5}},
                "color": {"claims": {"null_count": 5}},
            },
        }

        assert [i.path for i in ca.check_claims(tmp_path, _print(tmp_path, annotations))] == [
            f"{_ANN}::columns.color.claims.null_count",
        ]


def test_grain_keys_that_are_not_a_list_do_not_stop_the_tables_after_them(tmp_path: Path) -> None:
    manifest = _print(tmp_path, {"grain": {"keys": "color"}}, path="s/a")
    later = _print(tmp_path, {"grain": {"keys": [{"columns": ["shade"]}]}}, path="s/b")
    manifest["tables"] = {"s.a": manifest["tables"]["s.t"], "s.b": later["tables"]["s.t"]}

    assert [i.path for i in ca.check_grain_annotations(tmp_path, manifest)] == [
        "s/b/statistics.annotations.yaml::grain.keys[0]",
    ]


def test_a_value_note_on_a_column_the_statistics_lack_does_not_stop_the_columns_after_it(
    tmp_path: Path,
) -> None:
    annotations = {
        "columns": {
            "shade": {"values": [{"value": "x", "note": "y"}]},
            "color": {"values": [{"value": "green", "note": "y"}]},
        },
    }

    assert [i.path for i in ca.check_value_notes(tmp_path, _print(tmp_path, annotations))] == [
        f"{_ANN}::columns.color.values[0]",
    ]


class TestDerivedFrom:
    """SPEC 2.7.1: a lineage source the print lacks is stale, at warning severity."""

    def _sources(self, tmp_path: Path, *sources: dict[str, str]) -> list[Issue]:
        manifest = _print(tmp_path, {"columns": {"color": {"derived_from": list(sources)}}})

        return ca.check_derived_from(tmp_path, manifest)

    def test_a_source_the_print_holds_is_quiet(self, tmp_path: Path) -> None:
        assert self._sources(tmp_path, {"table": "s.t", "column": "code"}) == []

    def test_an_unknown_table_and_an_unknown_column_are_each_named(self, tmp_path: Path) -> None:
        issues = self._sources(
            tmp_path,
            {"table": "s.t", "column": "shade"},
            {"table": "s.elsewhere", "column": "color"},
        )

        assert [(i.path, i.code, i.severity) for i in issues] == [
            (f"{_ANN}::columns.color.derived_from[0]", "annotations.unknown-source", "warning"),
            (f"{_ANN}::columns.color.derived_from[1]", "annotations.unknown-source", "warning"),
        ]
        assert "s.elsewhere.color" in issues[1].detail

    def test_a_malformed_source_is_left_to_the_schema(self, tmp_path: Path) -> None:
        manifest = _print(
            tmp_path,
            {"columns": {"color": {"derived_from": [{"table": {"a": 1}, "column": ["x"]}]}}},
        )

        assert ca.check_derived_from(tmp_path, manifest) == []


_INFERRED = {
    **_COLOR,
    "cardinality_ratio": 1.0,
    "inferred": {
        "looks_like": "email",
        "sampled": 10,
        "matched": 10,
        "candidate_key": True,
        "sensitivity": "contact",
        "epoch_unit": "seconds",
    },
}
_NEAR_MISS = {
    **_COLOR,
    "inferred": {"looks_like_candidate": "email", "looks_like_candidate_share": 0.6},
}


class TestRejections:
    """SPEC 2.7.1: a rejection naming a field the column does not carry is stale, at warning."""

    def _rejections(
        self,
        tmp_path: Path,
        rejected: list[str],
        column: dict[str, Any] = _COLOR,
        **statistics: Any,
    ) -> list[Issue]:
        stats = {**_STATS, "columns": {"color": column}, **statistics}
        manifest = _print(tmp_path, {"columns": {"color": {"rejected": rejected}}}, stats)

        return ca.check_rejections(tmp_path, manifest)

    def test_a_field_the_column_carries_is_quiet(self, tmp_path: Path) -> None:
        rejected = ["candidate_key", "looks_like", "sensitivity", "epoch_unit"]

        assert self._rejections(tmp_path, rejected, _INFERRED) == []
        assert self._rejections(tmp_path, ["looks_like_candidate"], _NEAR_MISS) == []

    @pytest.mark.parametrize(
        "field",
        ["candidate_key", "looks_like", "looks_like_candidate", "sensitivity", "epoch_unit"],
    )
    def test_a_field_the_column_lacks_is_stale(self, tmp_path: Path, field: str) -> None:
        assert self._rejections(tmp_path, [field]) == [
            Issue(
                f"{_ANN}::columns.color.rejected",
                "annotations.unknown-inference",
                "warning",
                f"rejected names [{field!r}], which statistics.yaml does not carry for this column.",
                "§2.7.1",
            ),
        ]

    def test_every_stale_name_on_a_column_is_one_finding(self, tmp_path: Path) -> None:
        issues = self._rejections(tmp_path, ["epoch_unit", "candidate_key"])

        assert len(issues) == 1
        assert "['epoch_unit', 'candidate_key']" in issues[0].detail

    def test_a_field_unmeasured_on_the_column_is_quiet(self, tmp_path: Path) -> None:
        column = {**_COLOR, "unmeasured": ["inferred.looks_like"]}

        assert self._rejections(tmp_path, ["looks_like"], column) == []

    def test_a_field_switched_off_for_the_table_is_quiet(self, tmp_path: Path) -> None:
        assert self._rejections(tmp_path, ["looks_like"], disabled=["looks_like"]) == []

    def test_a_column_the_statistics_lack_is_only_a_stale_key(self, tmp_path: Path) -> None:
        manifest = _print(tmp_path, {"columns": {"shade": {"rejected": ["epoch_unit"]}}})

        assert ca.check_rejections(tmp_path, manifest) == []
        assert [i.code for i in ca.check_stale_keys(tmp_path, manifest)] == [
            "annotations.unknown-column",
        ]


_FINDING = {
    "color": {
        "claims": {"null_count": 3},
        "values": [{"value": "green", "note": "x"}],
        "rejected": ["epoch_unit"],
    },
    "shade": {"note": "a column the statistics lack"},
}


class TestAFileFailingAdmissionIsReadByNoPass:
    """SPEC 2.7: a body that fails its schema, or keys a mapping by a non-string, is withheld."""

    @pytest.mark.parametrize(
        "malformed",
        [
            {"columns": {**_FINDING, "first": "not an entry"}},
            {"columns": _FINDING, "grain": {"keys": [{"columns": [{"id": 1}]}]}},
            {"columns": {**_FINDING, "code": {"values": [{"value": ["red"], "note": "x"}]}}},
            {"columns": {**_FINDING, "code": {"claims": {1: 3}}}},
            {"columns": {**_FINDING, "code": {"note": 5}}},
        ],
    )
    @pytest.mark.parametrize("check", [*_CHECKS, ca.check_derived_from, ca.check_rejections])
    def test_no_annotations_finding_is_reported(
        self,
        tmp_path: Path,
        malformed: dict[str, Any],
        check: Any,
    ) -> None:
        assert check(tmp_path, _print(tmp_path, malformed)) == []

    def test_the_same_body_admitted_is_reported(self, tmp_path: Path) -> None:
        assert ca.check_stale_keys(tmp_path, _print(tmp_path, {"columns": _FINDING})) != []
