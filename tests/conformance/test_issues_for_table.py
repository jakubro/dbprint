"""`issues_for_table`: which findings belong to one table, by the longest directory prefix."""

from __future__ import annotations

from pathlib import Path

import yaml

from dbprint.conformance import Issue, ValidationTick, issues_for_table, validate_print


MANIFEST = {
    "tables": {
        "a.b": {"path": "a/b"},
        "a.bc": {"path": "a/bc"},
    },
}


def _issue(path: str) -> Issue:
    return Issue(path, "annotations.unknown-column", "warning", "x", "§2.7.1")


def test_a_table_gets_the_issues_under_its_directory() -> None:
    issues = [_issue("a/b/statistics.annotations.yaml::columns.x"), _issue("a/bc/ddl.sql")]

    assert issues_for_table(issues, MANIFEST, "a.b") == [issues[0]]


def test_a_sibling_sharing_a_name_prefix_is_not_claimed() -> None:
    issues = [_issue("a/bc/statistics.annotations.yaml")]

    assert issues_for_table(issues, MANIFEST, "a.b") == []
    assert issues_for_table(issues, MANIFEST, "a.bc") == issues


def test_a_connection_root_file_is_owned_by_no_table() -> None:
    issues = [_issue("manifest.annotations.yaml"), _issue("a/manifest.yaml")]

    assert issues_for_table(issues, MANIFEST, "a.b") == []


def test_agrees_with_the_per_table_count_validate_print_reports(committed_print: Path) -> None:
    root = committed_print / "production"
    path = root / "arboretum" / "seedbank" / "vault" / "statistics.annotations.yaml"
    path.write_text("format_version: 1\ncolumns:\n  no_such_column: {note: stale}\n")
    manifest = yaml.safe_load((root / "manifest.yaml").read_text())
    reported: dict[str, int] = {}

    def record(tick: ValidationTick) -> None:
        if tick.findings is not None:
            reported[tick.fqn] = tick.findings

    issues = validate_print(root, on_table=record)

    assert reported["arboretum.seedbank.vault"] >= 1
    assert reported == {table: len(issues_for_table(issues, manifest, table)) for table in reported}
