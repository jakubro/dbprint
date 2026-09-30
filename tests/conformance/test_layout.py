"""A print's directory layout, per SPEC 1."""

from __future__ import annotations

from pathlib import Path

import yaml

from dbprint.conformance import layout
from dbprint.conformance.issue import Issue


def _root(
    tmp_path: Path,
    manifest: object | None,
    *,
    reading: bool = True,
    diff: bool = True,
) -> Path:
    root = tmp_path / "prints" / "c"
    root.mkdir(parents=True)

    if manifest is not None:
        (root / "manifest.yaml").write_text(yaml.safe_dump(manifest))

    if reading:
        (root / "reading.md").write_text("# guide\n")

    if diff:
        (root / "diff.yaml").write_text("format_version: 1\n")

    return root


def _table(root: Path, path: str, *files: str) -> None:
    table_dir = root / path
    table_dir.mkdir(parents=True, exist_ok=True)

    for name in files:
        (table_dir / name).write_text("x\n")


class TestTheManifestItself:
    def test_a_root_with_no_manifest(self, tmp_path: Path) -> None:
        assert layout.check(_root(tmp_path, None)) == (
            [
                Issue(
                    "manifest.yaml",
                    "layout.missing-manifest",
                    "error",
                    "Connection root is missing manifest.yaml.",
                    "§1.2",
                ),
            ],
            None,
        )

    def test_a_manifest_that_does_not_parse(self, tmp_path: Path) -> None:
        root = _root(tmp_path, None)
        (root / "manifest.yaml").write_text("tables: [unclosed\n")

        issues, data = layout.check(root)

        assert data is None
        assert [(i.path, i.code, i.severity, i.spec_ref) for i in issues] == [
            ("manifest.yaml", "schema.invalid-yaml", "error", "§2.5"),
        ]
        assert "flow sequence" in issues[0].detail

    def test_a_manifest_that_is_not_a_mapping(self, tmp_path: Path) -> None:
        assert layout.check(_root(tmp_path, ["tables"])) == (
            [
                Issue(
                    "manifest.yaml",
                    "schema.type-mismatch",
                    "error",
                    "manifest.yaml must hold a mapping, found list.",
                    "§2.5",
                ),
            ],
            None,
        )

    def test_tables_that_are_not_a_mapping(self, tmp_path: Path) -> None:
        assert layout.check(_root(tmp_path, {"tables": ["s.t"]})) == (
            [
                Issue(
                    "manifest.yaml::tables",
                    "schema.type-mismatch",
                    "error",
                    "`tables` must map each table name to its entry, found list.",
                    "§2.5",
                ),
            ],
            None,
        )

    def test_a_well_formed_empty_print_is_returned_parsed(self, tmp_path: Path) -> None:
        assert layout.check(_root(tmp_path, {"tables": {}}, diff=False)) == ([], {"tables": {}})


class TestTheConnectionRoot:
    def test_a_root_with_no_reading_guide(self, tmp_path: Path) -> None:
        issues, _ = layout.check(_root(tmp_path, {"tables": {}}, reading=False))

        assert issues == [
            Issue(
                "reading.md",
                "layout.missing-reading-guide",
                "error",
                "Connection root is missing reading.md.",
                "§1.2",
            ),
        ]

    def test_a_root_with_a_table_and_no_diff(self, tmp_path: Path) -> None:
        root = _root(tmp_path, {"tables": {"s.t": {"path": "s/t"}}}, diff=False)
        _table(root, "s/t", "ddl.sql")

        issues, _ = layout.check(root)

        assert issues == [
            Issue(
                "diff.yaml",
                "layout.missing-diff",
                "error",
                "Connection root is missing diff.yaml, though the manifest records a table.",
                "§1.2",
            ),
        ]


class TestTableDirectories:
    def test_a_directory_name_outside_the_allowlist(self, tmp_path: Path) -> None:
        root = _root(tmp_path, {"tables": {}})
        (root / "Bad Dir").mkdir()

        issues, _ = layout.check(root)

        assert issues == [
            Issue(
                "Bad Dir",
                "layout.invalid-path-segment",
                "error",
                "Path segment 'Bad Dir' fails the allowlist regex '^[a-z0-9_][a-z0-9_-]*$'. A table "
                "an earlier release printed under this name is refused now: exclude it, delete this "
                "directory, and the next generate drops its entry.",
                "§1.5.1",
            ),
        ]

    def test_a_file_outside_the_canonical_list_in_every_table(self, tmp_path: Path) -> None:
        tables = {"s.a": {"path": "s/missing"}, "s.b": {"path": "s/b"}, "s.c": {"path": "s/c"}}
        root = _root(tmp_path, {"tables": tables})
        _table(root, "s/b", "ddl.sql", "notes.txt")
        _table(root, "s/c", "extra.yaml")

        issues, _ = layout.check(root)

        assert sorted(issues) == [
            Issue(
                "s/b/notes.txt",
                "layout.unknown-file",
                "warning",
                "File 'notes.txt' is not in the canonical artifact list.",
                "§1.4",
            ),
            Issue(
                "s/c/extra.yaml",
                "layout.unknown-file",
                "warning",
                "File 'extra.yaml' is not in the canonical artifact list.",
                "§1.4",
            ),
        ]

    def test_an_entry_with_no_path_reads_the_connection_root_as_its_directory(
        self,
        tmp_path: Path,
    ) -> None:
        root = _root(tmp_path, {"tables": {"s.t": {}}})
        (root / "ddl.sql").write_text("x\n")
        (root / "stray.txt").write_text("x\n")

        issues, _ = layout.check(root)

        flagged = {i.path for i in issues if i.code == "layout.unknown-file"}

        assert "stray.txt" in flagged
        assert "ddl.sql" not in flagged
        assert all(i.code != "layout.unexpected-directory-level" for i in issues)

    def test_a_producer_artifact_outside_every_table_directory(self, tmp_path: Path) -> None:
        root = _root(tmp_path, {"tables": {"s.t": {"path": "s/t"}}})
        _table(root, "s/t", "ddl.sql")
        _table(root, "s/old", "statistics.yaml")

        issues, _ = layout.check(root)

        assert issues == [
            Issue(
                "s/old/statistics.yaml",
                "layout.unexpected-directory-level",
                "error",
                "Producer-written artifact 'statistics.yaml' appears in a directory not listed in "
                "manifest.tables[*].path.",
                "§1.4",
            ),
        ]
