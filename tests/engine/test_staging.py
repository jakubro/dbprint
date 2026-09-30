"""The run stage: nothing reaches the print root before commit; a cut-off commit rolls forward."""

from __future__ import annotations

import errno
import logging
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from dbprint.engine import staging
from dbprint.engine.staging import RunStage, StageRefused
from dbprint.engine.writer import WriterError


def _tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.fixture
def prints_root(tmp_path: Path) -> Path:
    root = tmp_path / "prints" / "wh"
    (root / "s" / "old").mkdir(parents=True)
    (root / "manifest.yaml").write_text("before\n")
    (root / "s" / "old" / "ddl.sql").write_text("old\n")
    (root / "s" / "old" / "description.md").write_text("kept\n")

    return root


def _stage_a_run(stage: RunStage, root: Path) -> None:
    stage.write(root / "s" / "new", {"ddl.sql": "new\n", "statistics.yaml": "s\n"})
    stage.write(root, {"manifest.yaml": "after\n", "diff.yaml": "d\n"})
    stage.remove(root / "s" / "old", ("ddl.sql",))


class TestBeforeCommit:
    def test_staged_writes_leave_the_print_root_untouched(self, prints_root: Path) -> None:
        before = _tree(prints_root)
        stage = RunStage.open(prints_root)
        _stage_a_run(stage, prints_root)

        assert _tree(prints_root) == before

        stage.close()

        assert _tree(prints_root) == before
        assert not stage.directory.exists()

    def test_the_stage_sits_beside_the_print_root_and_is_git_ignored(
        self,
        prints_root: Path,
    ) -> None:
        stage = RunStage.open(prints_root)

        assert stage.directory == prints_root.parent / ".dbprint-run" / "wh"
        assert (prints_root.parent / ".dbprint-run" / ".gitignore").read_text() == "*\n"

        stage.close()

    @pytest.mark.parametrize("name", ["description.md", "statistics.annotations.yaml"])
    def test_user_content_is_refused(self, prints_root: Path, name: str) -> None:
        stage = RunStage.open(prints_root)

        with pytest.raises(WriterError):
            stage.remove(prints_root / "s" / "old", (name,))

        with pytest.raises(WriterError):
            stage.write(prints_root / "s" / "old", {name: "x"})

        stage.close()


class TestCommit:
    def test_moves_every_file_applies_removals_and_prunes_nothing_holding_user_files(
        self,
        prints_root: Path,
    ) -> None:
        stage = RunStage.open(prints_root)
        _stage_a_run(stage, prints_root)
        stage.commit()
        stage.close()

        assert _tree(prints_root) == {
            "diff.yaml": b"d\n",
            "manifest.yaml": b"after\n",
            "s/new/ddl.sql": b"new\n",
            "s/new/statistics.yaml": b"s\n",
            "s/old/description.md": b"kept\n",
        }

    def test_an_emptied_directory_is_removed_up_to_the_root(self, prints_root: Path) -> None:
        (prints_root / "s" / "old" / "description.md").unlink()
        stage = RunStage.open(prints_root)
        stage.remove(prints_root / "s" / "old", ("ddl.sql",))
        stage.commit()
        stage.close()

        assert not (prints_root / "s").exists()
        assert prints_root.is_dir()

    def test_the_manifest_is_the_last_rename(self, prints_root: Path) -> None:
        moved: list[str] = []
        real = os.replace

        def record(src: Path, dst: Path) -> None:
            moved.append(Path(str(dst)).name)
            real(src, dst)

        stage = RunStage.open(prints_root)
        _stage_a_run(stage, prints_root)

        with patch.object(staging.os, "replace", record):
            stage.commit()

        stage.close()

        assert [m for m in moved if m != ".commit"][-1] == "manifest.yaml"
        assert moved.index("diff.yaml") > moved.index("statistics.yaml")

    def test_removals_land_before_the_manifest(self, prints_root: Path) -> None:
        present_at_manifest: list[bool] = []
        real = os.replace

        def record(src: Path, dst: Path) -> None:
            if Path(str(dst)).name == "manifest.yaml":
                present_at_manifest.append((prints_root / "s" / "old" / "ddl.sql").exists())

            real(src, dst)

        stage = RunStage.open(prints_root)
        _stage_a_run(stage, prints_root)

        with patch.object(staging.os, "replace", record):
            stage.commit()

        stage.close()
        assert present_at_manifest == [False]

    def test_a_failed_discard_warns_and_releases_the_lock(
        self,
        prints_root: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        stage = RunStage.open(prints_root)
        _stage_a_run(stage, prints_root)

        def refusing(path: Path) -> None:
            raise OSError(errno.EACCES, "denied")

        with (
            patch.object(staging.shutil, "rmtree", refusing),
            caplog.at_level(logging.WARNING, logger="dbprint.engine.staging"),
        ):
            stage.close()

        assert [r.levelno for r in caplog.records] == [logging.WARNING]
        assert str(stage.directory) in caplog.records[0].getMessage()
        RunStage.open(prints_root).close()


class TestRecovery:
    def _interrupted_commit(self, prints_root: Path, after_renames: int) -> None:
        count = 0
        real = os.replace

        def dying(src: Path, dst: Path) -> None:
            nonlocal count

            if Path(str(dst)).name != ".commit":
                if count == after_renames:
                    raise KeyboardInterrupt
                count += 1

            real(src, dst)

        stage = RunStage.open(prints_root)
        _stage_a_run(stage, prints_root)

        with patch.object(staging.os, "replace", dying), pytest.raises(KeyboardInterrupt):
            stage.commit()

        stage.close()

    @pytest.mark.parametrize("after_renames", [0, 1, 2, 3])
    def test_a_marked_stage_is_rolled_forward_by_the_next_open(
        self,
        tmp_path: Path,
        prints_root: Path,
        after_renames: int,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        reference_root = tmp_path / "reference" / "wh"
        reference_root.parent.mkdir()
        for rel, data in _tree(prints_root).items():
            (reference_root / rel).parent.mkdir(parents=True, exist_ok=True)
            (reference_root / rel).write_bytes(data)
        reference = RunStage.open(reference_root)
        _stage_a_run(reference, reference_root)
        reference.commit()
        reference.close()

        self._interrupted_commit(prints_root, after_renames)

        assert (prints_root.parent / ".dbprint-run" / "wh" / ".commit").is_file()

        with caplog.at_level(logging.WARNING, logger="dbprint.engine.staging"):
            RunStage.open(prints_root).close()

        assert [(r.levelno, "'wh'" in r.getMessage()) for r in caplog.records] == [
            (logging.WARNING, True),
        ]
        assert _tree(prints_root) == _tree(reference_root)

    def test_an_unmarked_stage_is_discarded(
        self,
        prints_root: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        before = _tree(prints_root)
        leftover = prints_root.parent / ".dbprint-run" / "wh" / "s" / "new"
        leftover.mkdir(parents=True)
        (leftover / "ddl.sql").write_text("half\n")

        with caplog.at_level(logging.WARNING, logger="dbprint.engine.staging"):
            RunStage.open(prints_root).close()

        assert [(r.levelno, "'wh'" in r.getMessage()) for r in caplog.records] == [
            (logging.WARNING, True),
        ]
        assert _tree(prints_root) == before


class TestRefusal:
    def test_a_second_run_of_the_same_connection_is_refused(self, prints_root: Path) -> None:
        first = RunStage.open(prints_root)

        with pytest.raises(StageRefused, match="'wh' is being generated by another run"):
            RunStage.open(prints_root)

        first.close()
        RunStage.open(prints_root).close()

    def test_a_stage_on_another_filesystem_is_refused(self, prints_root: Path) -> None:
        real = staging._device

        def split(path: Path) -> int:
            return real(path) + (1 if path.name == ".dbprint-run" else 0)

        with (
            patch.object(staging, "_device", split),
            pytest.raises(StageRefused, match="different filesystems"),
        ):
            RunStage.open(prints_root)
