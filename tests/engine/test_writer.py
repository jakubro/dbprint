"""Writer atomic-semantics tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from dbprint.engine.writer import (
    DESCRIPTION_FILENAME,
    MANIFEST_ANNOTATIONS_FILENAME,
    RELATIONSHIPS_ANNOTATIONS_FILENAME,
    STATISTICS_ANNOTATIONS_FILENAME,
    WriterError,
    write_atomic,
)


class TestAtomicSuccess:
    def test_creates_files_in_new_dir(self, tmp_path: Path) -> None:
        tbl_dir = tmp_path / "schema" / "curator"
        write_atomic(tbl_dir, {"ddl.sql": "CREATE TABLE t (id int);\n"})
        assert (tbl_dir / "ddl.sql").read_text() == "CREATE TABLE t (id int);\n"

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        tbl_dir = tmp_path / "t"
        tbl_dir.mkdir()
        (tbl_dir / "ddl.sql").write_text("OLD\n")
        write_atomic(tbl_dir, {"ddl.sql": "NEW\n"})
        assert (tbl_dir / "ddl.sql").read_text() == "NEW\n"

    def test_multiple_artifacts_all_land(self, tmp_path: Path) -> None:
        tbl_dir = tmp_path / "t"
        write_atomic(
            tbl_dir,
            {
                "ddl.sql": "CREATE TABLE t (id int);\n",
                "statistics.yaml": "format_version: 1\n",
                "relationships.yaml": "format_version: 1\n",
            },
        )

        for name in ("ddl.sql", "statistics.yaml", "relationships.yaml"):
            assert (tbl_dir / name).is_file()


class TestRollback:
    def test_temp_files_cleaned_on_write_failure(self, tmp_path: Path) -> None:
        tbl_dir = tmp_path / "t"
        tbl_dir.mkdir()

        good = (tbl_dir / "good.sql", "data\n")

        # Patch os.fsync to fail mid-write so the second file errors out.
        with patch("dbprint.engine.writer.os.fsync") as mock_fsync:
            mock_fsync.side_effect = [None, OSError("simulated fsync failure")]

            with pytest.raises(WriterError):
                write_atomic(tbl_dir, {"good.sql": good[1], "bad.sql": "more\n"})

        leftover_tmps = list(tbl_dir.glob("*.tmp"))
        assert leftover_tmps == []

    def test_existing_artifact_intact_when_rename_fails(self, tmp_path: Path) -> None:
        tbl_dir = tmp_path / "t"
        tbl_dir.mkdir()
        (tbl_dir / "ddl.sql").write_text("PRIOR\n")

        with patch("dbprint.engine.writer.os.replace") as mock_replace:
            mock_replace.side_effect = OSError("simulated replace failure")

            with pytest.raises(WriterError):
                write_atomic(tbl_dir, {"ddl.sql": "NEW\n"})

        assert (tbl_dir / "ddl.sql").read_text() == "PRIOR\n"
        assert list(tbl_dir.glob("*.tmp")) == []


class TestDescriptionPreservation:
    def test_description_md_rejected_at_writer(self, tmp_path: Path) -> None:
        tbl_dir = tmp_path / "t"

        with pytest.raises(WriterError, match="description.md"):
            write_atomic(tbl_dir, {DESCRIPTION_FILENAME: "user content\n"})

    def test_existing_description_left_alone(self, tmp_path: Path) -> None:
        tbl_dir = tmp_path / "t"
        tbl_dir.mkdir()
        (tbl_dir / "description.md").write_text("USER CONTENT\n")
        write_atomic(tbl_dir, {"ddl.sql": "CREATE TABLE t (id int);\n"})
        assert (tbl_dir / "description.md").read_text() == "USER CONTENT\n"

    def test_annotations_yaml_rejected_at_writer(self, tmp_path: Path) -> None:
        tbl_dir = tmp_path / "t"

        with pytest.raises(WriterError, match="statistics.annotations.yaml"):
            write_atomic(tbl_dir, {STATISTICS_ANNOTATIONS_FILENAME: "columns: {}\n"})

    def test_existing_annotations_left_alone(self, tmp_path: Path) -> None:
        tbl_dir = tmp_path / "t"
        tbl_dir.mkdir()
        (tbl_dir / "statistics.annotations.yaml").write_text(
            "format_version: 1\ncolumns: {status: note}\n",
        )
        write_atomic(tbl_dir, {"ddl.sql": "CREATE TABLE t (id int);\n"})
        assert (
            tbl_dir / "statistics.annotations.yaml"
        ).read_text() == "format_version: 1\ncolumns: {status: note}\n"

    def test_relationships_annotations_yaml_rejected_at_writer(self, tmp_path: Path) -> None:
        tbl_dir = tmp_path / "t"

        with pytest.raises(WriterError, match="relationships.annotations.yaml"):
            write_atomic(tbl_dir, {RELATIONSHIPS_ANNOTATIONS_FILENAME: "refers_to: []\n"})

    def test_existing_relationship_annotations_left_alone(self, tmp_path: Path) -> None:
        tbl_dir = tmp_path / "t"
        tbl_dir.mkdir()
        (tbl_dir / "relationships.annotations.yaml").write_text(
            "format_version: 1\nrefers_to: []\n",
        )
        write_atomic(tbl_dir, {"ddl.sql": "CREATE TABLE t (id int);\n"})
        assert (
            tbl_dir / "relationships.annotations.yaml"
        ).read_text() == "format_version: 1\nrefers_to: []\n"

    def test_manifest_annotations_yaml_rejected_at_writer(self, tmp_path: Path) -> None:
        conn_root = tmp_path / "primary"

        with pytest.raises(WriterError, match="manifest.annotations.yaml"):
            write_atomic(conn_root, {MANIFEST_ANNOTATIONS_FILENAME: "notes: x\n"})

    def test_existing_manifest_annotations_left_alone(self, tmp_path: Path) -> None:
        conn_root = tmp_path / "primary"
        conn_root.mkdir()
        (conn_root / "manifest.annotations.yaml").write_text("format_version: 1\nnotes: x\n")
        write_atomic(conn_root, {"manifest.yaml": "format_version: 1\n"})
        assert (
            conn_root / "manifest.annotations.yaml"
        ).read_text() == "format_version: 1\nnotes: x\n"


class TestInvalidNames:
    def test_path_separator_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(WriterError):
            write_atomic(tmp_path / "t", {"sub/dir/file.sql": "x"})

    def test_dotdot_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(WriterError):
            write_atomic(tmp_path / "t", {"..": "x"})
