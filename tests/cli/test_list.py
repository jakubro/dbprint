"""dbprint list - reads on-disk manifest; offline."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from dbprint.cli.main import main


PROJECT_YAML = """\
connections:
  primary:
    adapter: postgres
    output: prints
"""

PRODUCTION_PROJECT_YAML = """\
connections:
  production:
    adapter: postgres
    output: prints
"""


def _write_manifest(
    tmp_path: Path,
    fqn: str,
    profiled_at: str = "2026-06-08T00:00:00Z",
    max_age_days: float | None = None,
) -> Path:
    prints = tmp_path / "prints" / "primary"
    prints.mkdir(parents=True, exist_ok=True)
    entry: dict[str, Any] = {
        "type": "table",
        "path": fqn.replace(".", "/"),
        "artifacts": {"ddl": "ddl.sql", "statistics": "statistics.yaml"},
        "columns": 3,
        "profiled_at": profiled_at,
    }

    if max_age_days is not None:
        entry["max_age_days"] = max_age_days

    manifest = {
        "format_version": 1,
        "generated_at": profiled_at,
        "connection": "primary",
        "adapter": "postgres",
        "dbprint_version": "0.1.0",
        "statistics_params": {
            "enumeration_threshold": 50,
            "top_n_values": 30,
            "top_n_null_patterns": 20,
            "looks_like_sample_size": 1000,
            "percentiles": [1, 25, 50, 75, 99],
        },
        "selectors": {"include": ["*"], "exclude": []},
        "redaction_rules_configured": 0,
        "default_collation": "en_US.UTF-8",
        "tables": {fqn: entry},
    }
    (prints / "manifest.yaml").write_text(yaml.safe_dump(manifest))

    return prints / "manifest.yaml"


def _days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat().replace("+00:00", "Z")


def _setup_project(tmp_path: Path) -> None:
    (tmp_path / ".dbprint.yaml").write_text(PROJECT_YAML)


def _age_every_table(manifest_path: Path, profiled_at: str) -> None:
    """Bump every table entry's `profiled_at` in a copy of the committed manifest.

    The print's tables already carry two recorded thresholds - 1 day on nine objects, 30 on
    the matview - so a fixed ageing splits them into live/stale.
    """

    manifest = yaml.safe_load(manifest_path.read_text())
    manifest["generated_at"] = profiled_at

    for entry in manifest["tables"].values():
        entry["profiled_at"] = profiled_at

    manifest_path.write_text(yaml.safe_dump(manifest))


class TestListHappyPath:
    def test_lists_tables_in_manifest(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup_project(tmp_path)
        _write_manifest(tmp_path, "public.curator")
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["list", "--no-tui"])
        assert result.exit_code == 0
        assert "primary" in result.output
        assert "table_count\t1" in result.output


class TestListNoManifest:
    def test_missing_manifest_reports_and_errors(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["list", "--no-tui"])
        assert result.exit_code == 1
        assert "no manifest" in result.stdout
        assert "no manifest" in result.stderr


class TestFreshnessClassification:
    def test_recent_profile_counts_as_live(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup_project(tmp_path)
        _write_manifest(tmp_path, "public.t", profiled_at="2099-01-01T00:00:00Z")
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["list", "--no-tui"])
        assert "live\t1" in result.output

    def test_the_recorded_threshold_decides_the_bucket(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Buckets follow the print's own threshold, so they agree with `check`."""

        _setup_project(tmp_path)
        _write_manifest(tmp_path, "public.t", profiled_at=_days_ago(10), max_age_days=30)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["list", "--no-tui"])

        assert "live\t1" in result.output

    def test_an_entry_recording_none_falls_back_to_the_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup_project(tmp_path)
        _write_manifest(tmp_path, "public.t", profiled_at=_days_ago(10))
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["list", "--no-tui"])

        assert "stale\t1" in result.output


def _write_two_table_manifest(tmp_path: Path, profiled_at: str) -> None:
    """Two tables at one age, neither recording a threshold of its own."""

    prints = tmp_path / "prints" / "primary"
    prints.mkdir(parents=True, exist_ok=True)
    manifest = {
        "format_version": 1,
        "generated_at": profiled_at,
        "connection": "primary",
        "adapter": "postgres",
        "dbprint_version": "0.1.0",
        "statistics_params": {
            "enumeration_threshold": 50,
            "top_n_values": 30,
            "top_n_null_patterns": 20,
            "looks_like_sample_size": 1000,
            "percentiles": [1, 25, 50, 75, 99],
        },
        "selectors": {"include": ["*"], "exclude": []},
        "redaction_rules_configured": 0,
        "default_collation": "en_US.UTF-8",
        "tables": {
            f"public.{name}": {
                "type": "table",
                "path": f"public/{name}",
                "artifacts": {"ddl": "ddl.sql", "statistics": "statistics.yaml"},
                "columns": 3,
                "profiled_at": profiled_at,
            }
            for name in ("curator", "herbarium")
        },
    }
    (prints / "manifest.yaml").write_text(yaml.safe_dump(manifest))


CASCADE_YAML = """\
connections:
  good:
    adapter: postgres
    auto: true
    output: prints
  bad:
    adapter: postgres
    auto: true
    output: prints
    rules:
      - include: ["*"]
        sample: 0.1
      - include: ["public.herbarium"]
        filter: "id > 0"
"""

SIZE_GATED_YAML = """\
connections:
  primary:
    adapter: postgres
    output: prints
    max_age_days: 7
    rules:
      - include: ["public.t"]
        min_rows: 1000000
        max_age_days: 30
"""


def _write_named_manifest(tmp_path: Path, connection: str, fqns: tuple[str, ...]) -> None:
    """Manifest for one connection, no entry recording a threshold of its own."""

    prints = tmp_path / "prints" / connection
    prints.mkdir(parents=True, exist_ok=True)
    stamp = _days_ago(1)
    manifest = {
        "format_version": 1,
        "generated_at": stamp,
        "connection": connection,
        "adapter": "postgres",
        "dbprint_version": "0.1.0",
        "statistics_params": {
            "enumeration_threshold": 50,
            "top_n_values": 30,
            "top_n_null_patterns": 20,
            "looks_like_sample_size": 1000,
            "percentiles": [1, 25, 50, 75, 99],
        },
        "selectors": {"include": ["*"], "exclude": []},
        "redaction_rules_configured": 0,
        "default_collation": "en_US.UTF-8",
        "tables": {
            fqn: {
                "type": "table",
                "path": fqn.replace(".", "/"),
                "artifacts": {"ddl": "ddl.sql", "statistics": "statistics.yaml"},
                "columns": 3,
                "profiled_at": stamp,
            }
            for fqn in fqns
        },
    }
    (prints / "manifest.yaml").write_text(yaml.safe_dump(manifest))


class TestContradictoryCascadeOffline:
    """A connection whose rules refuse a table is reported and skipped whole, not fatal -
    a partial one would leave `table_count` wrong.
    """

    @staticmethod
    def _seed(tmp_path: Path) -> None:
        (tmp_path / ".dbprint.yaml").write_text(CASCADE_YAML)
        _write_named_manifest(tmp_path, "good", ("public.curator",))
        _write_named_manifest(tmp_path, "bad", ("public.curator", "public.herbarium"))

    def test_the_refusal_names_the_table_and_both_rules(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._seed(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["list", "--no-tui"])

        assert "public.herbarium" in result.stderr
        assert "rules[0]" in result.stderr
        assert "rules[1]" in result.stderr
        assert result.exit_code == 1

    def test_a_connection_after_the_bad_one_still_renders(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`list` renders inside its loop; an uncaught raise would stop everything after it."""

        (tmp_path / ".dbprint.yaml").write_text(
            CASCADE_YAML.replace("  good:", "  zzz_good:"),
        )
        _write_named_manifest(tmp_path, "zzz_good", ("public.curator",))
        _write_named_manifest(tmp_path, "bad", ("public.curator", "public.herbarium"))
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["list", "--no-tui"])

        assert "zzz_good\ttable_count\t1" in result.stdout

    def test_the_refused_connection_renders_no_counts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Totals that describe fewer tables than `table_count` are worse than none."""

        self._seed(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["list", "--no-tui"])

        assert "good\ttable_count\t1" in result.stdout
        assert "bad\ttable_count" not in result.stdout


class TestForbiddenRecordedThreshold:
    """A recorded `max_age_days` the schema forbids is refused, not silently bucketed."""

    def test_a_negative_recorded_threshold_is_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup_project(tmp_path)
        _write_manifest(tmp_path, "public.t", profiled_at=_days_ago(3), max_age_days=-1)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["list", "--no-tui"])

        assert result.exit_code == 1
        assert "public.t" in result.stderr
        assert "max_age_days is -1" in result.stderr
        assert "dormant" not in result.stdout

    def test_a_non_integer_recorded_threshold_is_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup_project(tmp_path)
        _write_manifest(tmp_path, "public.t", profiled_at=_days_ago(3), max_age_days=7.5)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["list", "--no-tui"])

        assert result.exit_code == 1
        assert "public.t" in result.stderr
        assert "max_age_days is 7.5" in result.stderr

    def test_zero_is_accepted_and_not_conflated_with_the_forbidden_value(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup_project(tmp_path)
        _write_manifest(tmp_path, "public.t", profiled_at=_days_ago(3), max_age_days=0)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["list", "--no-tui"])

        assert result.exit_code == 0
        assert result.stderr == ""
        assert "table_count\t1" in result.stdout

    def test_an_absent_field_still_resolves_from_the_rules(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup_project(tmp_path)
        _write_manifest(tmp_path, "public.t", profiled_at=_days_ago(3))
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["list", "--no-tui"])

        assert result.exit_code == 0
        assert "table_count\t1" in result.stdout

    def test_list_and_check_do_not_contradict_each_other(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup_project(tmp_path)
        _write_manifest(tmp_path, "public.t", profiled_at=_days_ago(3), max_age_days=-1)
        monkeypatch.chdir(tmp_path)

        list_result = CliRunner().invoke(main, ["list", "--no-tui"])
        check_result = CliRunner().invoke(main, ["check"])

        assert list_result.exit_code != 0
        assert check_result.exit_code != 0


class TestSizeGatedRuleOffline:
    def test_the_unapplied_rule_is_named(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`list` holds no database either, so the same rule cannot apply here."""

        (tmp_path / ".dbprint.yaml").write_text(SIZE_GATED_YAML)
        _write_manifest(tmp_path, "public.t", profiled_at=_days_ago(10))
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["list", "--no-tui"])

        assert "min_rows" in result.stderr
        assert "public.t" in result.stderr
        # The rule's 30 did not apply, so the connection's 7 buckets it stale.
        assert "stale\t1" in result.stdout


class TestPerTableBuckets:
    """Buckets follow each table's own threshold, so `list` agrees with `check`."""

    def test_one_age_two_thresholds_splits_the_buckets(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The print's own recorded thresholds split the buckets: nine record 1 day, one 30."""

        (tmp_path / ".dbprint.yaml").write_text(PRODUCTION_PROJECT_YAML)
        _age_every_table(committed_print / "production" / "manifest.yaml", _days_ago(3))
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["list", "--no-tui"])

        # The nine 1-day objects are now past their own threshold; the 30-day matview is not.
        assert "live\t1" in result.output
        assert "stale\t9" in result.output

    def test_a_config_without_rules_buckets_both_the_same_way(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A same-bucket-by-fallback state the committed print has no counterpart for (SPEC 2.5)."""

        _setup_project(tmp_path)
        _write_two_table_manifest(tmp_path, _days_ago(3))
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["list", "--no-tui"])

        assert "live\t2" in result.output


TWO_CONNECTIONS_YAML = """\
connections:
  aaa_good:
    adapter: postgres
    auto: true
    output: prints
  zzz_broken:
    adapter: postgres
    auto: true
    output: prints
"""


class TestWrongShapeManifest:
    """A manifest that parses but no reader can walk is named and skipped, not fatal."""

    @staticmethod
    def _seed(tmp_path: Path, body: str) -> Path:
        (tmp_path / ".dbprint.yaml").write_text(TWO_CONNECTIONS_YAML)
        _write_named_manifest(tmp_path, "aaa_good", ("public.curator",))
        _write_named_manifest(tmp_path, "zzz_broken", ("public.curator",))
        broken = tmp_path / "prints" / "zzz_broken" / "manifest.yaml"
        broken.write_text(body)

        return broken

    @pytest.mark.parametrize(
        ("body", "found"),
        [
            ("- one\n- two\n", "list"),
            ("just a string\n", "str"),
            ("format_version: 1\ntables:\n  - public.curator\n", "list"),
            ("format_version: 1\ntables:\n", "nothing"),
        ],
    )
    def test_the_ignored_manifest_is_named_with_what_it_holds(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        body: str,
        found: str,
    ) -> None:
        broken = self._seed(tmp_path, body)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["list", "--no-tui"])

        assert str(broken) in result.stderr
        assert found in result.stderr
        assert result.exit_code == 1

    def test_the_other_connection_still_reports(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._seed(tmp_path, "- one\n- two\n")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["list", "--no-tui"])

        assert "aaa_good\ttable_count\t1" in result.stdout
        assert "zzz_broken\ttable_count" not in result.stdout

    def test_a_table_entry_that_is_not_a_mapping_costs_the_connection(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An entry with no bucket would leave the totals short of `table_count`."""

        _setup_project(tmp_path)
        manifest_path = _write_manifest(tmp_path, "public.curator")
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["tables"]["public.herbarium"] = "not an entry"
        manifest_path.write_text(yaml.safe_dump(manifest))
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["list", "--no-tui"])

        assert result.exit_code == 1
        assert "public.herbarium" in result.stderr
        assert "primary\ttable_count" not in result.stdout


class TestADroppedConnectionIsReported:
    """A connection `list` could not summarise reaches stdout, not stderr alone."""

    def test_a_missing_manifest_is_named_on_stdout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["list", "--no-tui"])

        assert "primary\tnot_run\tno manifest at" in result.stdout
        assert result.exit_code == 1

    def test_an_unparseable_manifest_is_named_on_stdout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup_project(tmp_path)
        manifest_path = _write_manifest(tmp_path, "public.curator")
        manifest_path.write_text("not: valid: yaml: :")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["list", "--no-tui"])

        assert "primary\tnot_run\tcould not parse" in result.stdout

    def test_a_wrongly_shaped_manifest_is_named_on_stdout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup_project(tmp_path)
        manifest_path = _write_manifest(tmp_path, "public.curator")
        manifest_path.write_text("- one\n- two\n")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["list", "--no-tui"])

        assert "primary\tnot_run\tignoring" in result.stdout

    def test_a_refused_cascade_is_named_on_stdout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(CASCADE_YAML)
        _write_named_manifest(tmp_path, "good", ("public.curator",))
        _write_named_manifest(tmp_path, "bad", ("public.curator", "public.herbarium"))
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["list", "--no-tui"])

        assert "bad\tnot_run\t" in result.stdout
        assert "public.herbarium" in result.stdout

    def test_one_line_per_cause_and_no_summary_beside_it(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(CASCADE_YAML)
        _write_named_manifest(tmp_path, "good", ("public.curator",))
        _write_named_manifest(tmp_path, "bad", ("public.curator", "public.herbarium"))
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["list", "--no-tui"])
        bad_lines = [line for line in result.stdout.splitlines() if line.startswith("bad\t")]

        assert all(line.startswith("bad\tnot_run\t") for line in bad_lines)
        assert "good\ttable_count\t1" in result.stdout


class TestTheCauseIsNotDuplicatedForAHuman:
    """stderr always carries the cause; stdout carries it only in machine mode."""

    def test_the_piped_form_carries_it_on_both_streams(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["list", "--no-tui"])

        assert "primary\tnot_run\t" in result.stdout
        assert "no manifest at" in result.stderr

    def test_the_terminal_form_carries_it_once(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["list", "--tui"])

        # The terminal form must not duplicate the cause on stdout as well as stderr.
        assert "no manifest at" in result.stderr
        assert "no manifest at" not in result.stdout
