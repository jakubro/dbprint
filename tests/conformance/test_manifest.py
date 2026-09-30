"""Manifest entries cross-checked against the files on disk and against diff.yaml, per SPEC 2.5."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from dbprint.conformance import manifest
from dbprint.conformance.issue import Issue


def _write(root: Path, rel: str, body: Any) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body if isinstance(body, str) else yaml.safe_dump(body))


def _entry(path: str = "s/t", **artifacts: str) -> dict[str, Any]:
    return {"path": path, "artifacts": artifacts}


class TestConnectionNotes:
    def test_claimed_notes_that_are_missing(self, tmp_path: Path) -> None:
        assert manifest.check_manifest_annotations_presence(
            tmp_path,
            {"manifest_annotations": "manifest.annotations.yaml"},
        ) == [
            Issue(
                "manifest.yaml",
                "manifest.missing-artifact",
                "error",
                "Manifest claims manifest.annotations.yaml but file does not exist.",
                "§2.5",
            ),
        ]

    def test_claimed_notes_that_exist(self, tmp_path: Path) -> None:
        _write(tmp_path, "notes.yaml", "format_version: 1\n")

        assert (
            manifest.check_manifest_annotations_presence(
                tmp_path,
                {"manifest_annotations": "notes.yaml"},
            )
            == []
        )

    def test_no_claim_needs_no_file(self, tmp_path: Path) -> None:
        assert (
            manifest.check_manifest_annotations_presence(tmp_path, {"manifest_annotations": 3})
            == []
        )


class TestEachEntryAgainstItsFiles:
    def test_every_table_is_announced_in_order_with_its_position(self, tmp_path: Path) -> None:
        seen: list[tuple[str, int, int]] = []
        tables = {"s.a": {"path": "s/a"}, "s.b": {"path": "s/b"}}

        manifest.check(tmp_path, {"tables": tables}, on_table=lambda *event: seen.append(event))

        assert seen == [("s.a", 1, 2), ("s.b", 2, 2)]

    def test_a_key_naming_another_table_than_its_path(self, tmp_path: Path) -> None:
        assert manifest.check(tmp_path, {"tables": {"s.t": {"path": "s/u"}}}) == [
            Issue(
                "manifest.yaml::tables.s.t.path",
                "manifest.path-fqn-mismatch",
                "error",
                "Manifest key 's.t' names another table than its path 's/u'.",
                "§1.3",
            ),
        ]

    def test_an_entry_with_no_path_is_not_compared_with_its_key(self, tmp_path: Path) -> None:
        assert manifest.check(tmp_path, {"tables": {"s.t": {}}}) == []

    def test_every_claimed_file_that_is_missing_is_reported(self, tmp_path: Path) -> None:
        tables = {"s.t": _entry(ddl="ddl.sql", relationships="relationships.yaml")}

        assert manifest.check(tmp_path, {"tables": tables}) == [
            Issue(
                "s/t/ddl.sql",
                "manifest.missing-artifact",
                "error",
                "Manifest claims ddl.sql for table s.t but file does not exist.",
                "§2.5",
            ),
            Issue(
                "s/t/relationships.yaml",
                "manifest.missing-artifact",
                "error",
                "Manifest claims relationships.yaml for table s.t but file does not exist.",
                "§2.5",
            ),
        ]

    def test_a_kind_the_entry_does_not_declare_does_not_stop_the_kinds_after_it(
        self,
        tmp_path: Path,
    ) -> None:
        tables = {"s.t": _entry(statistics="statistics.yaml")}

        assert [i.code for i in manifest.check(tmp_path, {"tables": tables})] == [
            "manifest.missing-artifact",
        ]

    @pytest.mark.parametrize("kind", ["statistics", "relationships"])
    def test_a_file_naming_another_table(self, tmp_path: Path, kind: str) -> None:
        _write(tmp_path, f"s/t/{kind}.yaml", {"table": "s.u", "columns": {}})
        tables = {"s.t": _entry(**{kind: f"{kind}.yaml"})}

        assert manifest.check(tmp_path, {"tables": tables}) == [
            Issue(
                f"s/t/{kind}.yaml",
                "manifest.table-fqn-mismatch",
                "error",
                f"Manifest FQN 's.t' does not match {kind}.yaml table field 's.u'.",
                "§2.5",
            ),
        ]

    def test_an_unparseable_file_does_not_stop_the_kinds_after_it(self, tmp_path: Path) -> None:
        _write(tmp_path, "s/t/statistics.yaml", "columns: [unclosed\n")
        _write(tmp_path, "s/t/relationships.yaml", {"table": "s.u"})
        tables = {"s.t": _entry(statistics="statistics.yaml", relationships="relationships.yaml")}

        assert [i.path for i in manifest.check(tmp_path, {"tables": tables})] == [
            "s/t/relationships.yaml",
        ]

    def test_a_missing_file_does_not_stop_the_kinds_after_it(self, tmp_path: Path) -> None:
        _write(tmp_path, "s/t/relationships.yaml", {"table": "s.u"})
        tables = {"s.t": _entry(ddl="ddl.sql", relationships="relationships.yaml")}

        assert [i.code for i in manifest.check(tmp_path, {"tables": tables})] == [
            "manifest.missing-artifact",
            "manifest.table-fqn-mismatch",
        ]


class TestTheColumnCount:
    def _check(self, tmp_path: Path, count: Any, statistics: dict[str, Any]) -> list[Issue]:
        _write(tmp_path, "s/t/statistics.yaml", statistics)
        entry = {**_entry(statistics="statistics.yaml"), "columns": count}

        return manifest.check(tmp_path, {"tables": {"s.t": entry}})

    def test_a_count_the_file_disagrees_with(self, tmp_path: Path) -> None:
        assert self._check(tmp_path, 3, {"columns": {"a": {}, "b": {}}}) == [
            Issue(
                "s/t/statistics.yaml",
                "manifest.columns-count-mismatch",
                "error",
                "Manifest declares columns: 3 for table s.t but statistics.yaml's columns map "
                "carries 2.",
                "§2.5",
            ),
        ]

    def test_an_empty_map_under_a_scope_is_exempt(self, tmp_path: Path) -> None:
        assert self._check(tmp_path, 3, {"columns": {}, "scope": {"rows_scanned": 0}}) == []

    def test_a_scoped_map_that_is_not_empty_is_still_counted(self, tmp_path: Path) -> None:
        statistics = {"columns": {"a": {}}, "scope": {"rows_scanned": 5}}

        assert [i.code for i in self._check(tmp_path, 3, statistics)] == [
            "manifest.columns-count-mismatch",
        ]

    def test_an_empty_map_with_no_scope_is_counted(self, tmp_path: Path) -> None:
        assert [i.code for i in self._check(tmp_path, 3, {"columns": {}})] == [
            "manifest.columns-count-mismatch",
        ]

    @pytest.mark.parametrize(
        ("count", "columns"),
        [("3", {"a": {}}), (3, ["a", "b"])],
    )
    def test_shapes_the_schema_rejects_are_not_counted(
        self,
        tmp_path: Path,
        count: Any,
        columns: Any,
    ) -> None:
        assert self._check(tmp_path, count, {"columns": columns}) == []


class TestFilesNoEntryDeclares:
    def test_a_canonical_file_outside_every_declared_set(self, tmp_path: Path) -> None:
        _write(tmp_path, "description.md", "# notes\n")
        _write(tmp_path, "s/t/ddl.sql", "x\n")
        _write(tmp_path, "s/t/notes.txt", "x\n")
        _write(tmp_path, "s/t/description.md", "x\n")
        _write(tmp_path, "s/old/ddl.sql", "x\n")

        issues = manifest.check(tmp_path, {"tables": {"s.t": _entry(ddl="ddl.sql")}})

        assert sorted(issues) == [
            Issue(
                "s/old/ddl.sql",
                "manifest.orphaned-artifact",
                "warning",
                "File 'ddl.sql' on disk is not listed in any manifest entry.",
                "§2.5",
            ),
            Issue(
                "s/t/description.md",
                "manifest.orphaned-artifact",
                "warning",
                "File 'description.md' on disk is not listed in any manifest entry.",
                "§2.5",
            ),
        ]


class TestSelectorsAgreeWithTheDiff:
    def test_disagreeing_copies(self) -> None:
        assert manifest.check_selectors_agree_with_diff(
            {"selectors": {"include": ["a.*"]}},
            {"target": {"selectors": {"include": ["b.*"]}}},
            "diff.yaml",
        ) == [
            Issue(
                "diff.yaml",
                "manifest.selectors-mismatch-diff",
                "error",
                "diff.yaml target.selectors={'include': ['b.*']} disagrees with manifest.yaml "
                "selectors={'include': ['a.*']}.",
                "§2.5",
            ),
        ]

    @pytest.mark.parametrize(
        ("manifest_data", "diff_data"),
        [
            ({"selectors": {"include": ["a.*"]}}, {"target": {"selectors": {"include": ["a.*"]}}}),
            ({"selectors": {"include": ["a.*"]}}, {"target": ["selectors"]}),
            ({"selectors": {"include": ["a.*"]}}, {"target": {}}),
            ({}, {"target": {"selectors": {"include": ["a.*"]}}}),
        ],
    )
    def test_agreement_or_one_missing_copy_passes(
        self,
        manifest_data: dict[str, Any],
        diff_data: dict[str, Any],
    ) -> None:
        assert manifest.check_selectors_agree_with_diff(manifest_data, diff_data, "diff.yaml") == []
