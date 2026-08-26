"""cli.run_log - slug, retention, sink open/close, the check-online stderr fallback."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from dbprint.cli import run_log


class TestSlug:
    def test_encodes_the_whole_path_with_separators_as_hyphens(self) -> None:
        assert run_log._slug(Path("/srv/projects/warehouse")) == "srv-projects-warehouse"

    def test_contains_no_path_separator(self, tmp_path: Path) -> None:
        project = tmp_path / "srv" / "projects" / "warehouse"
        project.mkdir(parents=True)

        assert "/" not in run_log._slug(project)

    def test_two_projects_sharing_a_directory_name_get_different_slugs(
        self,
        tmp_path: Path,
    ) -> None:
        a = tmp_path / "a" / "warehouse"
        b = tmp_path / "b" / "warehouse"
        a.mkdir(parents=True)
        b.mkdir(parents=True)

        assert run_log._slug(a) != run_log._slug(b)


class TestPrune:
    def test_reserves_one_slot_for_the_file_about_to_be_created(self, tmp_path: Path) -> None:
        """`_prune` runs before the new file exists, so 4 in -> 2 out, not 3."""

        names = [
            "20260101T000000_000001-generate.log",
            "20260101T000000_000002-generate.log",
            "20260101T000000_000003-generate.log",
            "20260101T000000_000004-generate.log",
        ]

        for name in names:
            (tmp_path / name).write_text("x")

        run_log._prune(tmp_path)

        assert sorted(p.name for p in tmp_path.iterdir()) == names[2:]

    def test_leaves_a_file_outside_the_naming_scheme_untouched(self, tmp_path: Path) -> None:
        for i in range(4):
            (tmp_path / f"2026010{i}T000000_000000-generate.log").write_text("x")

        (tmp_path / "notes.txt").write_text("mine")

        run_log._prune(tmp_path)

        assert (tmp_path / "notes.txt").is_file()


class TestRetentionAcrossRuns:
    def test_four_consecutive_opens_leave_exactly_three_files(self, tmp_path: Path) -> None:
        project = tmp_path / "project"

        with patch.object(run_log, "LOGS_ROOT", tmp_path / "logs"):
            for _ in range(4):
                handle = run_log.open_run_log(project, "generate")
                assert handle is not None
                run_log.close_run_log(handle)

        files = list((tmp_path / "logs" / run_log._slug(project)).glob("*.log"))
        assert len(files) == 3

    def test_retention_is_per_project(self, tmp_path: Path) -> None:
        project_a = tmp_path / "a"
        project_b = tmp_path / "b"

        with patch.object(run_log, "LOGS_ROOT", tmp_path / "logs"):
            for _ in range(4):
                handle = run_log.open_run_log(project_a, "generate")
                assert handle is not None
                run_log.close_run_log(handle)

            handle = run_log.open_run_log(project_b, "generate")
            assert handle is not None
            run_log.close_run_log(handle)

        a_files = list((tmp_path / "logs" / run_log._slug(project_a)).glob("*.log"))
        b_files = list((tmp_path / "logs" / run_log._slug(project_b)).glob("*.log"))
        assert len(a_files) == 3
        assert len(b_files) == 1


class TestOpenClose:
    def test_creates_the_slug_directory_and_one_named_file(self, tmp_path: Path) -> None:
        project = tmp_path / "project"

        with patch.object(run_log, "LOGS_ROOT", tmp_path / "logs"):
            handle = run_log.open_run_log(project, "generate")

            try:
                assert handle is not None
                files = list((tmp_path / "logs" / run_log._slug(project)).glob("*.log"))
                assert len(files) == 1
                assert files[0].name.endswith("-generate.log")
            finally:
                run_log.close_run_log(handle)

    def test_open_raises_the_dbprint_logger_to_debug_and_close_restores_it(
        self,
        tmp_path: Path,
    ) -> None:
        logger = logging.getLogger("dbprint")
        before_level = logger.level

        with patch.object(run_log, "LOGS_ROOT", tmp_path / "logs"):
            handle = run_log.open_run_log(tmp_path / "project", "generate")
            assert handle is not None
            assert logger.level == logging.DEBUG
            assert handle.handler in logger.handlers

            run_log.close_run_log(handle)

        assert logger.level == before_level
        assert handle.handler not in logger.handlers

    def test_close_of_none_is_a_no_op(self) -> None:
        run_log.close_run_log(None)

    def test_unopenable_root_warns_once_on_stderr_and_returns_none(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """A file where the sink needs a directory - `mkdir` raises, not a crash."""

        blocked_root = tmp_path / "not-a-directory"
        blocked_root.write_text("occupied")

        with patch.object(run_log, "LOGS_ROOT", blocked_root):
            handle = run_log.open_run_log(tmp_path / "project", "generate")

        assert handle is None
        assert "warning" in capsys.readouterr().err.lower()


class TestStderrWarningHandler:
    def test_installs_and_removes_from_the_dbprint_logger(self) -> None:
        logger = logging.getLogger("dbprint")

        handler = run_log.install_stderr_warning_handler()
        try:
            assert handler in logger.handlers
            assert handler.level == logging.WARNING
        finally:
            run_log.remove_stderr_warning_handler(handler)

        assert handler not in logger.handlers

    def test_a_warning_raised_while_installed_reaches_stderr(
        self,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """The mechanism `check --online` relies on to keep its stderr warnings visible."""

        handler = run_log.install_stderr_warning_handler()

        try:
            logging.getLogger("dbprint.engine.orchestrator").warning("table %r: no estimate", "t")
        finally:
            run_log.remove_stderr_warning_handler(handler)

        assert "table 't': no estimate" in capsys.readouterr().err
