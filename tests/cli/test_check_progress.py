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
from dbprint.cli.rendering.progress import LiveProgressRenderer, _eta_seconds
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
        assert "\tvalidated\t" in result.stderr

    def test_summary_elapsed_ms_is_real_not_the_online_only_diff_result(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`diff_result` exists only under `--online`; an offline run's summary must still
        report a real wall-clock duration rather than the `0` that source alone would give.
        """

        _seed_committed_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)

        result = CliRunner().invoke(main, ["check", "--format", "json", "--no-tui"])

        summary_line = next(line for line in result.stderr.splitlines() if "\tsummary\t" in line)
        elapsed = summary_line.rsplit("\t", 1)[-1]

        assert elapsed != "0.0s"

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

    def test_a_missing_manifest_still_reaches_the_connection_summary(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A connection with no manifest never runs a table, but the renderer must still be
        told it happened - otherwise the run's own ok/failed tally silently omits it.
        """

        (tmp_path / ".dbprint.yaml").write_text(
            "connections:\n  primary:\n    adapter: postgres\n    output: prints\n",
        )
        monkeypatch.chdir(tmp_path)

        result = CliRunner().invoke(main, ["check", "--format", "json", "--no-tui"])

        summary_line = next(
            (line for line in result.stderr.splitlines() if "\tsummary\t" in line),
            None,
        )

        assert summary_line is not None
        assert "primary\tsummary\t0 ok / 0 failed / 0 skipped" in summary_line


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

    def test_quiet_with_tui_prints_no_refusal_warning(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Matches `diff`'s own `--quiet` suppression."""

        _seed_committed_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)

        result = CliRunner().invoke(main, ["check", "--format", "json", "--quiet", "--tui"])

        assert "does not support the live view" not in result.stderr


class TestRefusedTuiIsStated:
    """A `--tui` request refused for being a dumb or too-short terminal says so, not just an
    outright non-terminal - both routes downgrade through the same `supports_live` check.
    """

    def test_a_dumb_terminal_states_the_downgrade(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_committed_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)
        # `TTY_COMPATIBLE=1` makes Rich report a real terminal without a pty; `TERM=dumb` then
        # makes it a dumb one, which `is_terminal` alone does not distinguish.
        monkeypatch.setenv("TTY_COMPATIBLE", "1")
        monkeypatch.setenv("TERM", "dumb")

        result = CliRunner().invoke(main, ["check", "--format", "json", "--tui"])

        assert "warning: --tui requested but stderr does not support the live view" in result.stderr


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


def _duration_seconds(line: str) -> float:
    """The trailing `tree.duration_text` field on a streaming progress line, as a float."""

    return float(line.rsplit("\t", 1)[-1].removesuffix("s"))


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
        pass_index=pass_index,
        pass_total=pass_total,
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

    def test_banner_box_prints_once_as_a_rounded_box(self) -> None:
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=100, color_system=None)

        with LiveProgressRenderer(console) as r:
            self._drive_two_tables_three_passes(r)

        lines = _strip_ansi(buf.getvalue()).splitlines()
        top_idxs = [i for i, line in enumerate(lines) if line.startswith("╭")]
        assert len(top_idxs) == 1
        top = top_idxs[0]
        assert "Validating" in lines[top + 1]
        assert lines[top + 2].startswith("╰")
        # No blank-line print of its own precedes the box - two consecutive blanks would mean
        # one survived. A pty capture covers the harness's own line cadence.
        assert not (top >= 2 and lines[top - 1].strip() == "" and lines[top - 2].strip() == "")

    def test_footer_names_the_running_pass(self) -> None:
        console = Console(file=StringIO(), force_terminal=True, width=100, color_system=None)

        with LiveProgressRenderer(console) as r:
            r.on_event(_tick("seedbank.accession", 1, 2, "manifest", 1, 3))
            assert "manifest" in r._bar_line().plain
            assert "seedbank.accession" in r._inflight_line().plain
            assert "manifest" not in r._inflight_line().plain

            r.on_event(_tick("seedbank.accession", 1, 2, "artifacts", 2, 3))
            assert "artifacts" in r._bar_line().plain

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

    def test_eta_resolves_within_the_first_tick_not_the_first_findings_tick(self) -> None:
        """Every `validate` tick carries its own `elapsed_ms`; the ETA accumulates from the
        first one rather than waiting for the tenth, closing tick that also carries `findings`.
        """

        console = Console(file=StringIO(), force_terminal=True, width=100, color_system=None)

        with LiveProgressRenderer(console) as r:
            assert "--:--" in r._bar_line().plain

            r.on_event(_tick("seedbank.accession", 1, 2, "manifest", 1, 3, elapsed_ms=5))
            assert "--:--" not in r._bar_line().plain

    def test_the_running_pass_is_priced_from_its_own_cost(self) -> None:
        """The running pass prices at its own rate; unmeasured passes fall back to the run mean."""

        console = Console(file=StringIO(), force_terminal=True, width=100, color_system=None)

        with LiveProgressRenderer(console) as r:
            for i in (1, 2):
                r.on_event(_tick("seedbank.accession", i, 2, "manifest", 1, 3, elapsed_ms=1_000))

            r.on_event(_tick("seedbank.accession", 1, 2, "artifacts", 2, 3, elapsed_ms=10))
            eta = _eta_seconds(r._costs, r._segment, *r._remaining_split())

        # One tick left in the cheap pass at its own 10ms, two beyond it at the run's 670ms mean.
        assert eta == pytest.approx((10 + 2 * 670) / 1000)

    def test_eta_resets_on_a_connection_change_with_no_connecting_event(self) -> None:
        """`check` never emits `connecting` - the reset must not depend on that phase firing."""

        console = Console(file=StringIO(), force_terminal=True, width=100, color_system=None)

        with LiveProgressRenderer(console) as r:
            r.on_event(_tick("acme.t1", 1, 1, "manifest", 1, 1, elapsed_ms=2_000))
            r.on_event(
                ProgressEvent(
                    connection="secondary",
                    phase="validate",
                    status="done",
                    index=1,
                    total=3,
                    fqn="s.t1",
                    pass_name="manifest",
                    pass_index=1,
                    pass_total=1,
                    elapsed_ms=5,
                ),
            )
            eta = _eta_seconds(r._costs, r._segment, *r._remaining_split())

        # 2 remaining ticks priced at `secondary`'s own 5ms - `acme`'s 2000ms must not blend in.
        assert eta == pytest.approx(2 * 5 / 1000)

    def test_bar_label_carries_the_pass_padded_to_a_fixed_bracket_column(self) -> None:
        console = Console(file=StringIO(), force_terminal=True, width=100, color_system=None)

        with LiveProgressRenderer(console) as r:
            r.on_event(_tick("seedbank.accession", 1, 2, "manifest", 1, 10))
            short = r._bar_line().plain
            assert short.startswith("Validating manifest")

            r.on_event(_tick("seedbank.accession", 1, 2, "edge reciprocity", 3, 10))
            long = r._bar_line().plain
            assert long.startswith("Validating edge reciprocity")

            assert short.index("[") == long.index("[")


class TestAssertionsLiveRendering:
    """The single `assertions` pass shares `validate`'s bar but has no findings count of its own."""

    def _assertions_tick(
        self,
        fqn: str,
        index: int,
        total: int,
        *,
        elapsed_ms: int | None = None,
    ) -> ProgressEvent:
        """Shaped like `_assertions_progress_adapter`'s own output - no `pass_name` or `findings`."""

        return ProgressEvent(
            connection="acme",
            phase="assertions",
            status="done",
            index=index,
            total=total,
            fqn=fqn,
            elapsed_ms=elapsed_ms,
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

    def test_the_leaf_carries_a_duration_never_a_borrowed_row_count(self) -> None:
        """Assertions read no table rows - `- rows` would claim a measurement this phase
        never took. `elapsed_ms` is real once `_assertions_progress_adapter` times it.
        """

        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=100, color_system=None)

        with LiveProgressRenderer(console) as r:
            r.on_event(self._assertions_tick("seedbank.accession", 1, 1, elapsed_ms=250))

        out = buf.getvalue()
        assert "rows" not in out
        assert "0.2s" in out

    def test_banner_reads_checking_assertions(self) -> None:
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=100, color_system=None)

        with LiveProgressRenderer(console) as r:
            r.on_event(self._assertions_tick("seedbank.accession", 1, 1))

        lines = _strip_ansi(buf.getvalue()).splitlines()
        top_idxs = [i for i, line in enumerate(lines) if line.startswith("╭")]
        assert len(top_idxs) == 1
        assert "Checking assertions" in lines[top_idxs[0] + 1]


class TestAssertionsDurationAttribution:
    """`_load_committed_statistics` driven for real - not a hand-built `ProgressEvent` - since
    the defect is in when `on_table` fires relative to the read it is supposed to time.
    """

    def test_each_tables_duration_lands_on_its_own_line(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`on_table` fires after each table's read, so a slow table's cost lands on its own line.
        `fixture.shape_probe` is the first table `check` walks; its read is made artificially slow.
        """

        import time

        import dbprint.cli.commands.check as check_module

        _seed_committed_print(tmp_path, committed_print)
        (tmp_path / ".dbprint.yaml").write_text(
            PROJECT_YAML + "    assertions:\n"
            "      tables:\n"
            "        fixture.shape_probe:\n"
            "          row_count: {min: 1}\n",
        )
        monkeypatch.chdir(tmp_path)

        # Replaces only this module's own `yaml` name - the real module and every other caller
        # stay untouched, so the delay applies exactly once, to this module's own read.
        real_yaml = check_module.yaml
        marker = "table: fixture.shape_probe"

        class _SlowedForOneTable:
            def safe_load(self, text: str) -> Any:
                if marker in text:
                    time.sleep(1.0)

                return real_yaml.safe_load(text)

            def __getattr__(self, name: str) -> Any:
                return getattr(real_yaml, name)

        monkeypatch.setattr(check_module, "yaml", _SlowedForOneTable())

        result = CliRunner().invoke(main, ["check", "--format", "json", "--no-tui"])
        lines = {
            line.split("\t", 2)[1]: line
            for line in result.stderr.splitlines()
            if "\tasserted\t" in line
        }

        slow = _duration_seconds(lines["fixture.shape_probe"])
        fast = _duration_seconds(lines["seedbank.accession"])

        # A relative comparison, not a fixed threshold: a heavily parallel run can add its own
        # noise to every table's read, but the marked table's own ~1s sleep must still dominate.
        assert slow >= 0.9, (
            f"the slowed table's own line should show ~1.0s: {lines['fixture.shape_probe']}"
        )
        assert fast < slow - 0.5, (
            f"the fast table's line should not inherit the slow table's cost: "
            f"slow={slow}s fast={fast}s"
        )
