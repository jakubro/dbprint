"""validate_print's own wiring: which file each finding is addressed at, and the progress it reports."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from dbprint.conformance import ValidationTick, validate_print
from dbprint.conformance.issue import Issue


_EXAMPLE = (
    Path(__file__).resolve().parents[2] / "docs/format/v1/examples/production/prints/production"
)


@pytest.fixture
def example(tmp_path: Path) -> Path:
    root = tmp_path / "production"
    shutil.copytree(_EXAMPLE, root)

    return root


def _minimal(tmp_path: Path, tables: dict[str, Any]) -> Path:
    root = tmp_path / "c"
    root.mkdir()
    manifest = {"format_version": 1, "tables": tables}
    (root / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    (root / "reading.md").write_text("# guide\n")
    (root / "diff.yaml").write_text("format_version: 1\n")

    return root


def _last_pass_ticks(root: Path) -> dict[str, ValidationTick]:
    ticks: list[ValidationTick] = []
    validate_print(root, on_table=ticks.append)

    return {t.fqn: t for t in ticks if t.pass_index == t.pass_total}


class TestProgress:
    def test_the_artifact_pass_announces_each_table_by_name_and_position(
        self,
        example: Path,
    ) -> None:
        ticks: list[ValidationTick] = []
        validate_print(example, on_table=ticks.append)
        names = sorted(yaml.safe_load((example / "manifest.yaml").read_text())["tables"])

        artifact_pass = [(t.fqn, t.index, t.total) for t in ticks if t.pass_name == "artifacts"]

        assert sorted(artifact_pass) == [(name, i, len(names)) for i, name in enumerate(names, 1)]
        assert all(t.fqn in names for t in ticks)

    def test_a_table_file_counts_toward_its_table(self, tmp_path: Path) -> None:
        root = _minimal(tmp_path, {"t": {"path": "t", "type": "table"}})
        (root / "t").mkdir()
        (root / "t" / "notes.txt").write_text("x\n")

        tick = _last_pass_ticks(root)["t"]

        assert (tick.findings, tick.severity) == (1, "warning")

    def test_a_file_two_levels_below_a_table_counts_toward_it(self, tmp_path: Path) -> None:
        root = _minimal(tmp_path, {"s.t": {"path": "s/t", "type": "table"}})
        (root / "s" / "t" / "sub").mkdir(parents=True)
        (root / "s" / "t" / "sub" / "ddl.sql").write_text("x\n")

        tick = _last_pass_ticks(root)["s.t"]

        assert (tick.findings, tick.severity) == (2, "error")

    def test_a_finding_on_the_table_directory_itself_counts_toward_no_table(
        self,
        tmp_path: Path,
    ) -> None:
        root = _minimal(tmp_path, {"s.Bad": {"path": "s/Bad", "type": "table"}})
        (root / "s" / "Bad").mkdir(parents=True)

        tick = _last_pass_ticks(root)["s.Bad"]

        assert (tick.findings, tick.severity) == (0, None)


class TestEachFileIsReportedAtItsOwnPath:
    def test_a_table_file_that_does_not_parse(self, example: Path) -> None:
        path = example / "arboretum/seedbank/vault/statistics.yaml"
        path.write_text("columns: [unclosed\n")

        [issue] = [i for i in validate_print(example) if i.code == "schema.invalid-yaml"]

        assert (issue.path, issue.severity, issue.spec_ref) == (
            "arboretum/seedbank/vault/statistics.yaml",
            "error",
            "§2",
        )
        assert "flow sequence" in issue.detail

    @pytest.mark.parametrize(
        ("name", "spec_ref"),
        [("manifest.annotations.yaml", "§2.7.3"), ("diff.yaml", "§2.6")],
    )
    def test_a_connection_file_that_does_not_parse(
        self,
        example: Path,
        name: str,
        spec_ref: str,
    ) -> None:
        (example / name).write_text("changes: [unclosed\n")

        [issue] = [i for i in validate_print(example) if i.code == "schema.invalid-yaml"]

        assert (issue.path, issue.severity, issue.spec_ref) == (name, "error", spec_ref)
        assert "flow sequence" in issue.detail

    @pytest.mark.parametrize("name", ["manifest.annotations.yaml", "diff.yaml"])
    def test_a_connection_file_with_no_format_version(self, example: Path, name: str) -> None:
        body = yaml.safe_load((example / name).read_text())
        del body["format_version"]
        (example / name).write_text(yaml.safe_dump(body))

        issues = validate_print(example)

        assert (
            Issue(
                name,
                "version.missing-format-version",
                "error",
                "Artifact missing required format_version field.",
                "§5.1",
            )
            in issues
        )
        assert any(i.path == name and i.code == "schema.missing-required-field" for i in issues)

    def test_a_value_compared_on_a_redacted_column(self, example: Path) -> None:
        stats_path = example / "arboretum/seedbank/accession/statistics.yaml"
        stats = yaml.safe_load(stats_path.read_text())
        stats["columns"]["accession_code"]["redacted"] = "mask"
        stats_path.write_text(yaml.safe_dump(stats, sort_keys=False))
        diff_path = example / "diff.yaml"
        diff_body = yaml.safe_load(diff_path.read_text())
        diff_body["changes"] = [
            {
                "kind": "statistic_changed",
                "table": "arboretum.seedbank.accession",
                "column": "accession_code",
                "stat": "values",
                "before": ["a"],
                "after": ["b"],
            },
        ]
        diff_path.write_text(yaml.safe_dump(diff_body, sort_keys=False))

        issues = validate_print(example)

        assert [i.path for i in issues if i.code == "privacy.redacted-value-compared"] == [
            "diff.yaml::changes[0]",
        ]

    def test_an_entry_with_no_path_reads_its_files_at_the_connection_root(
        self,
        tmp_path: Path,
    ) -> None:
        root = _minimal(tmp_path, {"t": {"type": "table", "artifacts": {"ddl": "ddl.sql"}}})
        (root / "ddl.sql").write_bytes(b"CREATE TABLE t (id INT);")

        issues = validate_print(root)

        assert any(i.path == "ddl.sql" and i.code == "ddl.missing-trailing-newline" for i in issues)
