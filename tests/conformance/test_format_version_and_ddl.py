"""The format_version field (SPEC 5) and ddl.sql's own content rules (SPEC 2.1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from dbprint.conformance import ddl, format_version
from dbprint.conformance.issue import Issue


class TestFormatVersion:
    def test_version_one_passes(self) -> None:
        assert format_version.check({"format_version": 1}, "a.yaml") == []

    def test_a_body_that_is_not_a_mapping_is_left_to_the_schema(self) -> None:
        assert format_version.check([1], "a.yaml") == []

    def test_a_missing_version(self) -> None:
        assert format_version.check({}, "a.yaml") == [
            Issue(
                "a.yaml",
                "version.missing-format-version",
                "error",
                "Artifact missing required format_version field.",
                "§5.1",
            ),
        ]

    @pytest.mark.parametrize("value", [0, -1, "1", 1.0, None])
    def test_a_version_that_is_not_a_positive_integer(self, value: Any) -> None:
        assert format_version.check({"format_version": value}, "a.yaml") == [
            Issue(
                "a.yaml",
                "version.invalid-format-version",
                "error",
                f"format_version must be a positive integer; got {value!r}.",
                "§5.1",
            ),
        ]

    def test_a_later_major_version(self) -> None:
        assert format_version.check({"format_version": 2}, "a.yaml") == [
            Issue(
                "a.yaml",
                "version.unknown-format-version",
                "error",
                "format_version 2 is not v1; this validator only handles MAJOR=1.",
                "§5.2",
            ),
        ]


class TestDdlFile:
    def _check(self, tmp_path: Path, content: bytes) -> list[Issue]:
        path = tmp_path / "ddl.sql"
        path.write_bytes(content)

        return ddl.check(path, "t/ddl.sql")

    def test_a_statement_ending_in_a_newline_passes(self, tmp_path: Path) -> None:
        assert self._check(tmp_path, b"CREATE TABLE t (id INT);\n") == []

    @pytest.mark.parametrize("content", [b"", b"  \n\t\n"])
    def test_an_empty_file(self, tmp_path: Path, content: bytes) -> None:
        assert self._check(tmp_path, content) == [
            Issue("t/ddl.sql", "ddl.empty-file", "error", "ddl.sql exists but is empty.", "§2.1.4"),
        ]

    def test_a_file_without_a_trailing_newline(self, tmp_path: Path) -> None:
        assert self._check(tmp_path, b"CREATE TABLE t (id INT);") == [
            Issue(
                "t/ddl.sql",
                "ddl.missing-trailing-newline",
                "warning",
                "ddl.sql does not end with a newline (POSIX convention).",
                "§2.1.3",
            ),
        ]
