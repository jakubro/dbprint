"""dbprint check - progress rendering, offline and --online (stderr, never stdout)."""

from __future__ import annotations

import json
import re
import shutil
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from rich.console import Console

from dbprint.cli.main import main
from dbprint.cli.rendering.progress import LiveProgressRenderer
from dbprint.engine import ProgressEvent


PROJECT_YAML = """\
connections:
  primary:
    adapter: postgres
    output: prints
    max_age_days: 36500
"""


def _seed_committed_print(tmp_path: Path, committed_print: Path) -> None:
    """A verbatim copy of the packaged reference print, renamed to this project's connection."""

    (tmp_path / ".dbprint.yaml").write_text(PROJECT_YAML)
    shutil.copytree(committed_print / "production", tmp_path / "prints" / "primary")


def _rounded_age(payload: str) -> Any:
    """`age_days` rides wall-clock `now()`, so two runs never agree past a few decimals."""

    data = json.loads(payload)

    for connection in data:
        for entry in connection.get("stale_entries") or []:
            if "age_days" in entry:
                entry["age_days"] = round(entry["age_days"], 2)

    return data


class TestOfflineProgress:
    def test_stderr_carries_per_table_progress(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_committed_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)

        result = CliRunner().invoke(main, ["check", "--format", "json"])

        assert "seedbank.accession" in result.stderr
        assert "\tok\t" in result.stderr

    def test_stdout_stays_a_clean_envelope(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_committed_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)

        result = CliRunner().invoke(main, ["check", "--format", "json"])

        payload = json.loads(result.stdout)
        assert payload[0]["connection"] == "primary"

    def test_no_tui_still_emits_progress(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`--no-tui` selects the plain streaming renderer, never silence (that is `--quiet`)."""

        _seed_committed_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)

        result = CliRunner().invoke(
            main,
            ["check", "--format", "json", "--no-tui"],
        )

        assert result.stderr.strip() != ""

    def test_validate_print_called_positionally_is_unaffected(self, committed_print: Path) -> None:
        """SPEC 6.7's normative call - one positional argument."""

        from dbprint.conformance import validate_print

        issues = validate_print(committed_print / "production")
        assert isinstance(issues, list)


class TestQuiet:
    """`-q`/`--quiet` silences stderr progress; the stdout envelope is untouched by it."""

    def test_quiet_silences_stderr(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_committed_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)

        result = CliRunner().invoke(main, ["check", "--format", "json", "--quiet"])

        assert result.stderr == ""

    def test_short_form_matches_the_long_one(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_committed_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)

        result = CliRunner().invoke(main, ["check", "--format", "json", "-q"])

        assert result.stderr == ""

    def test_stdout_payload_and_exit_code_are_unaffected(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_committed_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()

        loud = runner.invoke(main, ["check", "--format", "json", "--no-tui"])
        quiet = runner.invoke(main, ["check", "--format", "json", "--quiet"])

        assert quiet.exit_code == loud.exit_code
        assert _rounded_age(quiet.stdout) == _rounded_age(loud.stdout)

    def test_quiet_with_tui_prints_no_not_a_tty_warning(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Matches `diff`'s own `--quiet` suppression."""

        _seed_committed_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)

        result = CliRunner().invoke(main, ["check", "--format", "json", "--quiet", "--tui"])

        assert "not a TTY" not in result.stderr


class TestOnlineProgress:
    def test_online_invocation_still_reports_offline_progress(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No adapter is patched in, so the online phase never reaches the database; the
        offline validate pass still ran and must still report progress on stderr."""

        _seed_committed_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)

        result = CliRunner().invoke(
            main,
            ["check", "--online", "--format", "json"],
        )

        assert "seedbank.accession" in result.stderr
        payload = json.loads(result.stdout)
        assert payload[0]["connection"] == "primary"


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[=>]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _tick(
    fqn: str,
    index: int,
    total: int,
    pass_name: str,
    pass_index: int,
    pass_total: int,
    *,
    findings: int | None = None,
    elapsed_ms: int | None = None,
) -> ProgressEvent:
    """One `validate`-phase event, shaped like `_validation_progress_adapter`'s own output."""

    return ProgressEvent(
        connection="acme",
        phase="validate",
        status="done",
        index=(pass_index - 1) * total + index,
        total=pass_total * total,
        fqn=fqn,
        pass_name=pass_name,
        findings=findings,
        elapsed_ms=elapsed_ms,
    )


class TestValidationLiveRendering:
    """`LiveProgressRenderer` over `check`'s offline passes - two tables, three passes."""

    def _drive_two_tables_three_passes(self, r: LiveProgressRenderer) -> None:
        for pass_index, pass_name in enumerate(("manifest", "artifacts", "edge claims"), start=1):
            for index, fqn in enumerate(("seedbank.accession", "seedbank.taxon"), start=1):
                findings = (
                    (1 if fqn == "seedbank.taxon" else 0) if pass_name == "edge claims" else None
                )
                r.on_event(
                    _tick(
                        fqn,
                        index,
                        2,
                        pass_name,
                        pass_index,
                        3,
                        findings=findings,
                        elapsed_ms=5 if findings is not None else None,
                    ),
                )

    def test_bar_spans_every_pass_and_advances_monotonically(self) -> None:
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=100, color_system=None)

        with LiveProgressRenderer(console) as r:
            self._drive_two_tables_three_passes(r)
            assert r._total == 6  # 2 tables * 3 passes
            assert r._index == 6  # last tick: pass 3, table 2

    def test_banner_prints_once_preceded_by_a_blank_line(self) -> None:
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=100, color_system=None)

        with LiveProgressRenderer(console) as r:
            self._drive_two_tables_three_passes(r)

        lines = _strip_ansi(buf.getvalue()).splitlines()
        banner_idxs = [i for i, line in enumerate(lines) if re.match(r"^-- .+ -+$", line)]
        assert len(banner_idxs) == 1
        assert lines[banner_idxs[0]].startswith("-- Validating ")
        assert lines[banner_idxs[0] - 1].strip() == ""

    def test_leaf_prints_only_on_the_findings_tick_with_a_count_not_rows(self) -> None:
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=100, color_system=None)

        with LiveProgressRenderer(console) as r:
            self._drive_two_tables_three_passes(r)

        out = buf.getvalue()
        assert "- rows" not in out
        assert "no findings" in out  # seedbank.accession: 0
        assert "1 finding" in out  # seedbank.taxon: 1
        assert out.count("accession") == 1  # header + leaf, once - not once per pass
        assert out.count("taxon") == 1

    def test_footer_names_the_running_pass(self) -> None:
        console = Console(file=StringIO(), force_terminal=True, width=100, color_system=None)

        with LiveProgressRenderer(console) as r:
            r.on_event(_tick("seedbank.accession", 1, 2, "manifest", 1, 3))
            assert "manifest" in r._inflight_line().plain

            r.on_event(_tick("seedbank.accession", 1, 2, "artifacts", 2, 3))
            assert "artifacts" in r._inflight_line().plain

    def test_eta_resolves_once_a_findings_tick_lands(self) -> None:
        console = Console(file=StringIO(), force_terminal=True, width=100, color_system=None)

        with LiveProgressRenderer(console) as r:
            r.on_event(_tick("seedbank.accession", 1, 2, "manifest", 1, 3))
            assert "--:--" in r._bar_line().plain

            r.on_event(
                _tick("seedbank.accession", 1, 2, "edge claims", 3, 3, findings=0, elapsed_ms=5),
            )
            assert "--:--" not in r._bar_line().plain


class TestAssertionsLiveRendering:
    """The single `assertions` pass shares `validate`'s bar but has no findings count of its own."""

    def _assertions_tick(self, fqn: str, index: int, total: int) -> ProgressEvent:
        """Shaped like `_assertions_progress_adapter`'s own output - no `pass_name` or `findings`."""

        return ProgressEvent(
            connection="acme",
            phase="assertions",
            status="done",
            index=index,
            total=total,
            fqn=fqn,
        )

    def test_every_tick_prints_its_leaf(self) -> None:
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=100, color_system=None)

        with LiveProgressRenderer(console) as r:
            r.on_event(self._assertions_tick("seedbank.accession", 1, 2))
            r.on_event(self._assertions_tick("seedbank.taxon", 2, 2))

        out = buf.getvalue()
        assert out.count("accession") == 1
        assert out.count("taxon") == 1

    def test_banner_reads_checking_assertions(self) -> None:
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=100, color_system=None)

        with LiveProgressRenderer(console) as r:
            r.on_event(self._assertions_tick("seedbank.accession", 1, 1))

        lines = _strip_ansi(buf.getvalue()).splitlines()
        banner_idxs = [i for i, line in enumerate(lines) if re.match(r"^-- .+ -+$", line)]
        assert len(banner_idxs) == 1
        assert lines[banner_idxs[0]].startswith("-- Checking assertions ")
