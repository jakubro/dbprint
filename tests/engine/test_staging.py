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

    def test_what_a_failed_write_leaves_in_the_stage_is_never_committed(
        self,
        prints_root: Path,
    ) -> None:
        before = _tree(prints_root)

        def leaves_a_temp(directory: Path, artifacts: dict[str, str | bytes]) -> None:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "ddl.sql.tmp").write_text("half\n")
            (directory / "ddl.sql").write_text("half\n")

            raise WriterError("failed renaming temp artifacts")

        stage = RunStage.open(prints_root)

        with patch.object(staging, "write_atomic", leaves_a_temp), pytest.raises(WriterError):
            stage.write(prints_root / "s" / "old", {"ddl.sql": "new\n"})

        stage.commit()
        stage.close()

        assert _tree(prints_root) == before

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

    def test_a_link_out_planted_before_the_roll_forward_is_refused_and_the_plan_kept(
        self,
        tmp_path: Path,
        prints_root: Path,
    ) -> None:
        self._interrupted_commit(prints_root, 0)
        outside = tmp_path / "outside"
        outside.mkdir()
        (prints_root / "s" / "new").rmdir()
        (prints_root / "s" / "new").symlink_to(outside)

        with pytest.raises(StageRefused, match=r"^s/new is a symlink leading outside"):
            RunStage.open(prints_root)

        assert list(outside.iterdir()) == []
        (prints_root / "s" / "new").unlink()
        RunStage.open(prints_root).close()

        assert (prints_root / "s" / "new" / "ddl.sql").read_text() == "new\n"

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


class TestLinksOutOfThePrint:
    """A directory linked outside the print root is never written through; one inside is."""

    @pytest.fixture
    def outside(self, tmp_path: Path) -> Path:
        directory = tmp_path / "outside"
        directory.mkdir()

        return directory

    @pytest.mark.parametrize("below", [(), ("partition",)])
    def test_a_write_through_a_linked_directory_is_refused(
        self,
        prints_root: Path,
        outside: Path,
        below: tuple[str, ...],
    ) -> None:
        (prints_root / "s" / "plot").symlink_to(outside)
        stage = RunStage.open(prints_root)

        with pytest.raises(WriterError, match=r"^s/plot is a symlink to a directory outside"):
            stage.write(prints_root.joinpath("s", "plot", *below), {"ddl.sql": "new\n"})

        stage.commit()
        stage.close()

        assert list(outside.iterdir()) == []

    def test_a_link_planted_after_the_stage_opened_is_refused_at_commit(
        self,
        prints_root: Path,
        outside: Path,
    ) -> None:
        before = _tree(prints_root)
        stage = RunStage.open(prints_root)
        _stage_a_run(stage, prints_root)
        (prints_root / "s" / "new").symlink_to(outside)

        with pytest.raises(StageRefused, match=r"^s/new is a symlink leading outside"):
            stage.commit()

        stage.close()

        assert list(outside.iterdir()) == []
        assert _tree(prints_root) == before
        assert not stage.directory.exists()

    def test_a_connection_root_file_linked_outside_is_refused_at_commit(
        self,
        prints_root: Path,
        outside: Path,
    ) -> None:
        planted = outside / "diff.yaml"
        planted.write_text("kept\n")
        (prints_root / "diff.yaml").symlink_to(planted)
        stage = RunStage.open(prints_root)
        _stage_a_run(stage, prints_root)

        with pytest.raises(StageRefused, match=r"^diff\.yaml is a symlink leading outside"):
            stage.commit()

        stage.close()

        assert planted.read_text() == "kept\n"

    def test_a_table_file_linked_outside_is_replaced_not_written_through(
        self,
        prints_root: Path,
        outside: Path,
    ) -> None:
        planted = outside / "ddl.sql"
        planted.write_text("kept\n")
        (prints_root / "s" / "old" / "ddl.sql").unlink()
        (prints_root / "s" / "old" / "ddl.sql").symlink_to(planted)
        stage = RunStage.open(prints_root)
        stage.write(prints_root / "s" / "old", {"ddl.sql": "new\n"})
        stage.commit()
        stage.close()

        assert planted.read_text() == "kept\n"
        assert not (prints_root / "s" / "old" / "ddl.sql").is_symlink()
        assert (prints_root / "s" / "old" / "ddl.sql").read_text() == "new\n"

    def test_a_write_reaching_a_link_out_another_way_is_refused(
        self,
        prints_root: Path,
        outside: Path,
    ) -> None:
        (prints_root / "s" / "plot").symlink_to(outside)
        (prints_root / "s" / "again").symlink_to(prints_root / "s")
        stage = RunStage.open(prints_root)

        with pytest.raises(WriterError, match=r"^s/again/plot leads outside"):
            stage.write(prints_root / "s" / "again" / "plot", {"ddl.sql": "new\n"})

        stage.commit()
        stage.close()

        assert list(outside.iterdir()) == []

    def test_a_write_routed_out_and_back_through_a_loop_is_refused(
        self,
        prints_root: Path,
        outside: Path,
    ) -> None:
        (prints_root / "real").mkdir()
        (prints_root / "loop").symlink_to(prints_root)
        (prints_root / "out").symlink_to(outside)
        (outside / "t").symlink_to(prints_root / "real")
        stage = RunStage.open(prints_root)

        with pytest.raises(WriterError, match=r"^loop/out leads outside"):
            stage.write(prints_root / "loop" / "out" / "t", {"ddl.sql": "new\n"})

        stage.commit()
        stage.close()

        assert list((prints_root / "real").iterdir()) == []

    def test_a_route_out_planted_after_the_write_is_refused_at_commit(
        self,
        prints_root: Path,
        outside: Path,
    ) -> None:
        before = _tree(prints_root)
        stage = RunStage.open(prints_root)
        stage.write(prints_root / "s" / "again" / "plot", {"ddl.sql": "new\n"})
        (prints_root / "s" / "plot").symlink_to(outside)
        (prints_root / "s" / "again").symlink_to(prints_root / "s")

        with pytest.raises(StageRefused, match=r"^s/again/plot leads outside"):
            stage.commit()

        stage.close()

        assert list(outside.iterdir()) == []
        assert {k: v for k, v in _tree(prints_root).items() if "again" not in k} == {
            k: v for k, v in before.items() if "again" not in k
        }

    def test_a_dangling_link_inside_is_refused_and_leaves_no_stuck_commit(
        self,
        prints_root: Path,
    ) -> None:
        (prints_root / "s" / "plot").symlink_to(prints_root / "gone")
        stage = RunStage.open(prints_root)

        with pytest.raises(WriterError, match=r"^s/plot leads outside the connection root"):
            stage.write(prints_root / "s" / "plot", {"ddl.sql": "new\n"})

        stage.commit()
        stage.close()
        again = RunStage.open(prints_root)
        again.close()

        assert not (prints_root / "gone").exists()

    @pytest.mark.parametrize("name", [".gitignore", "wh.lock", "wh"])
    def test_a_link_among_the_stages_own_files_is_refused(
        self,
        prints_root: Path,
        outside: Path,
        name: str,
    ) -> None:
        stage_root = prints_root.parent / ".dbprint-run"
        stage_root.mkdir()
        (outside / "victim").write_text("kept\n")
        (stage_root / name).symlink_to(outside / "victim")

        with pytest.raises(StageRefused, match="is a symlink"):
            RunStage.open(prints_root)

        assert (outside / "victim").read_text() == "kept\n"

    def test_a_write_the_filesystem_refuses_is_a_writer_error(self, prints_root: Path) -> None:
        stage = RunStage.open(prints_root)

        with (
            patch.object(staging, "write_atomic", side_effect=PermissionError("denied")),
            pytest.raises(WriterError, match=r"^failed staging s/new: denied"),
        ):
            stage.write(prints_root / "s" / "new", {"ddl.sql": "new\n"})

        stage.close()

    def test_a_link_inside_the_print_root_is_written_through(self, prints_root: Path) -> None:
        (prints_root / "shared").mkdir()
        (prints_root / "s" / "plot").symlink_to(Path("..") / "shared")
        stage = RunStage.open(prints_root)
        stage.write(prints_root / "s" / "plot", {"ddl.sql": "new\n"})
        stage.commit()
        stage.close()

        assert (prints_root / "shared" / "ddl.sql").read_text() == "new\n"

    def test_a_stage_root_linked_outside_is_refused(
        self,
        prints_root: Path,
        outside: Path,
    ) -> None:
        (prints_root.parent / ".dbprint-run").symlink_to(outside)

        with pytest.raises(StageRefused, match="symlink leading outside"):
            RunStage.open(prints_root)

        assert list(outside.iterdir()) == []
