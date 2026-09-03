"""dbprint check (offline) - CLI command tests."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from dbprint.cli.main import main
from dbprint.spec.temporal_age import freshness_classification, max_age_days


PROJECT_YAML = """\
connections:
  primary:
    adapter: postgres
    output: prints
    max_age_days: 7
"""


def _isoformat(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


def _seed_real(
    tmp_path: Path,
    committed_print: Path,
    connection: str,
    fqns: tuple[str, ...],
    profiled_at: datetime,
) -> Path:
    """Seed one connection from real committed-print tables, none recording its own threshold.

    Exercises `check`'s config/rules fallback rather than SPEC 2.5's recorded-threshold
    precedence. Artifacts are copied verbatim, only `profiled_at` bumped so a test controls
    staleness.
    """

    source = committed_print / "production"
    source_manifest = yaml.safe_load((source / "manifest.yaml").read_text())
    dest = tmp_path / "prints" / connection
    dest.mkdir(parents=True)

    when = _isoformat(profiled_at)
    tables: dict[str, Any] = {}

    for fqn in fqns:
        entry = dict(source_manifest["tables"][fqn])
        shutil.copytree(source / entry["path"], dest / entry["path"])
        entry.pop("max_age_days", None)
        entry["profiled_at"] = when

        stats_path = dest / entry["path"] / entry["artifacts"]["statistics"]
        stats = yaml.safe_load(stats_path.read_text())
        stats["profiled_at"] = when

        # A bumped `profiled_at` invalidates any temporal column's derived freshness
        # (SPEC 2.2.4) - recompute it the same way the conformance validator does.
        for col in (stats.get("columns") or {}).values():
            freshness = col.get("freshness")
            range_ = col.get("range")

            if isinstance(freshness, dict) and isinstance(range_, dict):
                days = max_age_days(range_.get("max"), when)
                freshness["max_age_days"] = days
                freshness["classification"] = freshness_classification(days)

        stats_path.write_text(yaml.safe_dump(stats))

        tables[fqn] = entry

    manifest = {
        "format_version": 1,
        "generated_at": when,
        "connection": connection,
        "adapter": "postgres",
        "dbprint_version": "0.1.0",
        "statistics_params": source_manifest["statistics_params"],
        "selectors": source_manifest["selectors"],
        "redaction_rules_configured": 0,
        "default_collation": source_manifest["default_collation"],
        "tables": tables,
    }
    (dest / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    shutil.copy(source / "reading.md", dest / "reading.md")

    diff_doc = yaml.safe_load((source / "diff.yaml").read_text())
    diff_doc["connection"] = connection
    diff_doc["generated_at"] = when
    diff_doc["baseline"]["path"] = f"prints/{connection}"
    (dest / "diff.yaml").write_text(yaml.safe_dump(diff_doc))

    return dest


def _seed_clean(
    tmp_path: Path,
    committed_print: Path,
    profiled_at: datetime | None = None,
    max_age_days: Any = None,
) -> Path:
    """Seed a conformance-clean `seedbank.accession` print; `max_age_days` only when supplied."""

    (tmp_path / ".dbprint.yaml").write_text(PROJECT_YAML)
    prints = _seed_real(
        tmp_path,
        committed_print,
        "primary",
        ("seedbank.accession",),
        profiled_at or datetime.now(UTC),
    )

    if max_age_days is not None:
        manifest_path = prints / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["tables"]["seedbank.accession"]["max_age_days"] = max_age_days
        manifest_path.write_text(yaml.safe_dump(manifest))

    return prints


RULES_YAML = """\
connections:
  primary:
    adapter: postgres
    output: prints
    max_age_days: 30
    rules:
      - include: ["seedbank.accession"]
        max_age_days: 1
"""


def _seed_two_tables(
    tmp_path: Path,
    committed_print: Path,
    profiled_at: datetime,
    project_yaml: str,
) -> Path:
    """Seed a conformance-clean print of the real two-table set, each falling back to its rules."""

    (tmp_path / ".dbprint.yaml").write_text(project_yaml)

    return _seed_real(
        tmp_path,
        committed_print,
        "primary",
        ("seedbank.accession", "seedbank.taxon"),
        profiled_at,
    )


class TestPerTableThresholdReachesCheck:
    """Two tables at one age, judged against two rule-resolved thresholds.

    A shared threshold can't distinguish per-table resolution from a fallback; the split can.
    """

    def test_only_the_table_past_its_own_threshold_is_stale(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_two_tables(
            tmp_path,
            committed_print,
            datetime.now(UTC) - timedelta(days=3),
            RULES_YAML,
        )
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--format", "json"])
        payload = json.loads(result.stdout)[0]

        assert result.exit_code == 2
        assert [(s["fqn"], s["max_age_days"]) for s in payload["stale_entries"]] == [
            ("seedbank.accession", 1.0),
        ]

    def test_an_override_governs_every_table_including_a_shortened_one(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_two_tables(
            tmp_path,
            committed_print,
            datetime.now(UTC) - timedelta(days=3),
            RULES_YAML,
        )
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--max-age", "0d", "--format", "json"])
        payload = json.loads(result.stdout)[0]

        assert {s["fqn"] for s in payload["stale_entries"]} == {
            "seedbank.accession",
            "seedbank.taxon",
        }
        assert {s["max_age_days"] for s in payload["stale_entries"]} == {0.0}

    def test_a_config_without_rules_behaves_as_the_connection_value(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_two_tables(
            tmp_path,
            committed_print,
            datetime.now(UTC) - timedelta(days=3),
            PROJECT_YAML,
        )
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--format", "json"])
        payload = json.loads(result.stdout)[0]

        # The connection says 7 and nothing overrides it; both are inside it.
        assert result.exit_code == 0
        assert payload["stale_entries"] == []


CASCADE_YAML = """\
connections:
  good:
    adapter: postgres
    auto: true
    output: prints
    max_age_days: 7
  bad:
    adapter: postgres
    auto: true
    output: prints
    max_age_days: 7
    rules:
      - include: ["*"]
        sample: 0.1
      - include: ["seedbank.taxon"]
        filter: "taxon_id > 0"
"""

SIZE_GATED_YAML = """\
connections:
  primary:
    adapter: postgres
    output: prints
    max_age_days: 7
    rules:
      - include: ["seedbank.accession"]
        min_rows: 1000000
        max_age_days: 30
"""

CASCADE_AND_SIZE_GATED_YAML = """\
connections:
  bad:
    adapter: postgres
    auto: true
    output: prints
    max_age_days: 7
    rules:
      - include: ["seedbank.accession"]
        min_rows: 1000000
        max_age_days: 30
      - include: ["*"]
        sample: 0.1
      - include: ["seedbank.taxon"]
        filter: "taxon_id > 0"
"""

NAME_ONLY_YAML = """\
connections:
  primary:
    adapter: postgres
    output: prints
    max_age_days: 7
    rules:
      - include: ["seedbank.accession"]
        max_age_days: 30
"""

CONNECTION_CEILING_ONLY_YAML = """\
connections:
  primary:
    adapter: postgres
    output: prints
    max_age_days: 7
    max_rows_scanned: 1000000000
"""

RULE_CEILING_ONLY_YAML = """\
connections:
  primary:
    adapter: postgres
    output: prints
    max_age_days: 7
    rules:
      - include: ["seedbank.accession"]
        max_rows_scanned: 1000000000
        max_age_days: 30
"""

MIXED_YAML = """\
connections:
  primary:
    adapter: postgres
    output: prints
    max_age_days: 7
    max_rows_scanned: 1000000000
    rules:
      - include: ["seedbank.accession"]
        min_rows: 1000000
        max_age_days: 30
"""


class TestContradictoryCascadeOffline:
    """A cascade narrowing one table two ways fails that table, not the command.

    `settings_for` needs a table name to see the collision, so a config can be contradictory
    for one table and settled for every other one in the connection.
    """

    @staticmethod
    def _seed(tmp_path: Path, committed_print: Path) -> None:
        (tmp_path / ".dbprint.yaml").write_text(CASCADE_YAML)
        when = datetime.now(UTC)
        _seed_real(tmp_path, committed_print, "good", ("seedbank.accession",), when)
        _seed_real(
            tmp_path,
            committed_print,
            "bad",
            ("seedbank.accession", "seedbank.taxon"),
            when,
        )

    def test_the_refused_table_is_named_with_both_rules(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._seed(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--format", "json"])
        payload = {entry["connection"]: entry for entry in json.loads(result.stdout)}

        not_run = payload["bad"]["not_run"]

        assert [entry["subject"] for entry in not_run] == ["seedbank.taxon"]
        assert "rules[0]" in not_run[0]["cause"]
        assert "rules[1]" in not_run[0]["cause"]
        assert payload["bad"]["exit_code"] == 1
        assert result.exit_code == 1

    def test_the_other_tables_in_that_connection_are_still_judged(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One table's contradictory rules say nothing about its neighbours."""

        self._seed(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--format", "json"])
        payload = {entry["connection"]: entry for entry in json.loads(result.stdout)}

        assert payload["bad"]["summary"]["not_run_count"] == 1
        assert payload["bad"]["stale_entries"] == []
        assert payload["bad"]["summary"]["errors"] == 0

    def test_a_clean_connection_still_reports(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`check` renders after its loop, so one failure cannot suppress the rest."""

        self._seed(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--format", "json"])
        payload = {entry["connection"]: entry for entry in json.loads(result.stdout)}

        assert payload["good"]["exit_code"] == 0
        assert payload["good"]["not_run"] == []

    def test_the_human_report_says_what_did_not_run(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._seed(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check"])

        assert "1 check did not run" in result.output
        assert "seedbank.taxon" in result.output
        assert "Connection: good" in result.output

    def test_an_explicit_override_still_reports_the_cascade_without_failing(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An explicit override reports the cascade as a warning, not a failure; exit stays 0."""

        self._seed(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--max-age", "7d", "--format", "json"])
        payload = {entry["connection"]: entry for entry in json.loads(result.stdout)}
        not_run = payload["bad"]["not_run"]

        assert result.exit_code == 0
        assert [entry["subject"] for entry in not_run] == ["seedbank.taxon"]
        assert not_run[0]["severity"] == "warning"
        assert "rules[0]" in not_run[0]["cause"]
        assert "rules[1]" in not_run[0]["cause"]
        assert "seedbank.taxon" in result.stderr

    def test_the_human_report_does_not_print_a_warning_under_the_fail_heading(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._seed(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--max-age", "7d"])

        assert "NOTE: 1 check reported, exit unaffected" in result.output
        assert "FAIL: 1 check did not run" not in result.output
        assert "seedbank.taxon" in result.output

    def test_the_size_gate_warning_stays_suppressed_under_override_with_a_cascade(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A size-gated rule's fallback does not happen under an override, cascade or not."""

        (tmp_path / ".dbprint.yaml").write_text(CASCADE_AND_SIZE_GATED_YAML)
        when = datetime.now(UTC)
        _seed_real(
            tmp_path,
            committed_print,
            "bad",
            ("seedbank.accession", "seedbank.taxon"),
            when,
        )
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--max-age", "7d", "--format", "json"])

        assert "min_rows" not in result.stderr
        assert result.exit_code == 0

    def test_a_recorded_threshold_keeps_the_cascade_out_of_it(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The fallback is the only path that resolves rules, so a recorded value skips it."""

        self._seed(tmp_path, committed_print)
        manifest_path = tmp_path / "prints" / "bad" / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["tables"]["seedbank.taxon"]["max_age_days"] = 30
        manifest_path.write_text(yaml.safe_dump(manifest))
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--format", "json"])
        payload = {entry["connection"]: entry for entry in json.loads(result.stdout)}

        assert result.exit_code == 0
        assert payload["bad"]["not_run"] == []


class TestSizeGatedRuleOffline:
    """`check` holds no database, so a rule selecting by row count cannot apply.

    The name-matched threshold governs instead, and the unapplied rule is named on stderr.
    """

    def test_the_unapplied_rule_is_named(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        old = datetime.now(UTC) - timedelta(days=10)
        _seed_clean(tmp_path, committed_print, profiled_at=old)
        (tmp_path / ".dbprint.yaml").write_text(SIZE_GATED_YAML)
        monkeypatch.chdir(tmp_path)
        # `stdout` rather than `output`, which carries the warning too.
        result = CliRunner().invoke(main, ["check", "--format", "json"])
        payload = json.loads(result.stdout)[0]

        assert "min_rows" in result.stderr
        assert "seedbank.accession" in result.stderr
        # The rule's 30 did not apply, so the connection's 7 is what judged it.
        assert [entry["max_age_days"] for entry in payload["stale_entries"]] == [7.0]

    def test_a_name_only_rule_resolves_and_says_nothing(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The same rule without the size condition applies, and warrants no warning."""

        old = datetime.now(UTC) - timedelta(days=10)
        _seed_clean(tmp_path, committed_print, profiled_at=old)
        (tmp_path / ".dbprint.yaml").write_text(NAME_ONLY_YAML)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--format", "json"])
        payload = json.loads(result.stdout)[0]

        assert "min_rows" not in result.stderr
        assert result.exit_code == 0
        assert payload["stale_entries"] == []

    def test_a_plain_view_draws_no_note_but_a_table_still_does(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A view is never queried, so no size condition can have governed it (SPEC 2.2.15)."""

        old = datetime.now(UTC) - timedelta(days=10)
        _seed_real(
            tmp_path,
            committed_print,
            "primary",
            ("seedbank.accession", "seedbank.accession_summary"),
            old,
        )
        (tmp_path / ".dbprint.yaml").write_text(
            SIZE_GATED_YAML.replace(
                'include: ["seedbank.accession"]',
                'include: ["seedbank.*"]',
            ),
        )
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--format", "json"])
        warning = next(line for line in result.stderr.splitlines() if "min_rows" in line)

        assert "seedbank.accession." in warning
        assert "seedbank.accession_summary" not in warning

    def test_a_connection_level_ceiling_alone_says_nothing(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A ceiling cannot affect `max_age_days`, so it is not this warning's concern."""

        old = datetime.now(UTC) - timedelta(days=10)
        _seed_clean(tmp_path, committed_print, profiled_at=old)
        (tmp_path / ".dbprint.yaml").write_text(CONNECTION_CEILING_ONLY_YAML)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--format", "json"])

        assert "min_rows" not in result.stderr

    def test_a_rules_ceiling_alone_says_nothing_but_its_max_age_days_is_used(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The rule applied in full - `max_rows_scanned` gates nothing `matches` checks."""

        old = datetime.now(UTC) - timedelta(days=10)
        _seed_clean(tmp_path, committed_print, profiled_at=old)
        (tmp_path / ".dbprint.yaml").write_text(RULE_CEILING_ONLY_YAML)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--format", "json"])
        payload = json.loads(result.stdout)[0]

        assert "min_rows" not in result.stderr
        assert result.exit_code == 0
        assert payload["stale_entries"] == []

    def test_a_connection_ceiling_and_a_rules_min_rows_names_only_the_gated_table(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A ceiling over every table and a `min_rows` rule over one: only the rule's own
        table is named, never the whole print the ceiling covers.
        """

        old = datetime.now(UTC) - timedelta(days=10)
        _seed_real(
            tmp_path,
            committed_print,
            "primary",
            ("seedbank.accession", "seedbank.taxon"),
            old,
        )
        (tmp_path / ".dbprint.yaml").write_text(MIXED_YAML)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--format", "json"])
        warning = next(line for line in result.stderr.splitlines() if "min_rows" in line)

        assert "seedbank.accession." in warning
        assert "seedbank.taxon" not in warning


class TestCleanState:
    def test_exit_zero(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_clean(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check"])
        assert result.exit_code == 0


class TestMissingManifest:
    def test_exit_one(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / ".dbprint.yaml").write_text(PROJECT_YAML)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check"])
        assert result.exit_code == 1


class TestStaleness:
    def test_aged_manifest_exits_two(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        old = datetime.now(UTC) - timedelta(days=30)
        _seed_clean(tmp_path, committed_print, profiled_at=old)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check"])
        assert result.exit_code == 2

    def test_max_age_override_recovers(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        old = datetime.now(UTC) - timedelta(days=30)
        _seed_clean(tmp_path, committed_print, profiled_at=old)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--max-age", "100d"])
        assert result.exit_code == 0


class TestRecordedThreshold:
    """Each table is judged against its entry's recorded threshold, not the config's (SPEC 2.5)."""

    def test_the_recorded_value_governs_over_the_config(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        old = datetime.now(UTC) - timedelta(days=10)
        _seed_clean(tmp_path, committed_print, profiled_at=old, max_age_days=30)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check"])

        # The config says 7, so re-deriving would call this print stale.
        assert result.exit_code == 0

    def test_an_entry_recording_none_falls_back_to_the_config(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        old = datetime.now(UTC) - timedelta(days=10)
        _seed_clean(tmp_path, committed_print, profiled_at=old)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--format", "json"])
        payload = json.loads(result.stdout)[0]

        # PROJECT_YAML declares 7; a "stale" exit alone would also pass for many wrong
        # fallback values, so pin the threshold the entry actually got judged against.
        assert result.exit_code == 2
        assert payload["stale_entries"][0]["max_age_days"] == 7

    def test_an_explicit_override_still_governs_every_table(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        old = datetime.now(UTC) - timedelta(days=10)
        _seed_clean(tmp_path, committed_print, profiled_at=old, max_age_days=30)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--max-age", "1d"])

        assert result.exit_code == 2

    def test_a_non_numeric_recorded_value_falls_back_instead_of_crashing(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The manifest is a file a hand can edit; a bad entry must not take the command down."""

        old = datetime.now(UTC) - timedelta(days=10)
        _seed_clean(tmp_path, committed_print, profiled_at=old, max_age_days="soon")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--format", "json"])
        payload = json.loads(result.stdout)[0]

        assert result.exit_code == 2
        assert [s["max_age_days"] for s in payload["stale_entries"]] == [7.0]


class TestMachineOutputNamesTheDefault:
    """The top-level threshold is the run-level default and may govern no table in the report."""

    def test_the_payload_reports_the_default_not_an_applied_threshold(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        old = datetime.now(UTC) - timedelta(days=10)
        _seed_clean(tmp_path, committed_print, profiled_at=old, max_age_days=30)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--format", "json"])
        payload = json.loads(result.stdout)[0]

        # The one table was judged against its own recorded 30, not this 7.
        assert payload["default_max_age_days"] == 7.0
        assert "max_age_days" not in payload
        assert result.exit_code == 0

    def test_an_explicit_override_is_reported_as_the_default(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        old = datetime.now(UTC) - timedelta(days=10)
        _seed_clean(tmp_path, committed_print, profiled_at=old, max_age_days=30)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--max-age", "12h", "--format", "json"])
        payload = json.loads(result.stdout)[0]

        # Fractional days survive: the field stays a float fed by parse_duration.
        assert payload["default_max_age_days"] == 0.5
        assert [s["max_age_days"] for s in payload["stale_entries"]] == [0.5]

    def test_a_missing_manifest_still_reports_the_default(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(PROJECT_YAML)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--format", "json"])
        payload = json.loads(result.stdout)[0]

        assert payload["manifest_present"] is False
        assert payload["default_max_age_days"] == 7.0


class TestConformanceError:
    def test_tampered_statistics_exits_one(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prints = _seed_clean(tmp_path, committed_print)
        # Break the statistics: cardinality exceeds row_count -> spec invariant.
        stats_path = prints / "seedbank" / "accession" / "statistics.yaml"
        stats: dict[str, Any] = yaml.safe_load(stats_path.read_text())
        stats["columns"]["accession_id"]["cardinality"] = 9999
        stats_path.write_text(yaml.safe_dump(stats))
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check"])
        assert result.exit_code == 1


class TestFormats:
    def test_json_emits_structured(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_clean(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert data[0]["connection"] == "primary"
        assert "summary" in data[0]

    def test_yaml_emits_multidoc(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_clean(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--format", "yaml"])
        assert result.exit_code == 0
        # Progress rides stderr; `result.output` interleaves both streams under click 8.2+.
        docs = list(yaml.safe_load_all(result.stdout))
        assert docs[0]["connection"] == "primary"


class TestWrongShapeManifest:
    """`check` survives a manifest no reader can walk, reporting it as a conformance issue."""

    @pytest.mark.parametrize(
        "body",
        [
            "- one\n- two\n",
            "just a string\n",
            "format_version: 1\ntables:\n  - public.t\n",
        ],
    )
    def test_the_command_completes_and_reports(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
        body: str,
    ) -> None:
        prints = _seed_clean(tmp_path, committed_print)
        (prints / "manifest.yaml").write_text(body)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--format", "json"])

        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload[0]["summary"]["errors"] > 0

    def test_a_table_entry_that_is_not_a_mapping_is_stale_rather_than_fatal(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Nothing in such an entry says the print is current, so it is judged stale."""

        prints = _seed_clean(tmp_path, committed_print)
        manifest_path = prints / "manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["tables"]["public.broken"] = "not an entry"
        manifest_path.write_text(yaml.safe_dump(manifest))
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--format", "json"])

        payload = json.loads(result.stdout)[0]
        assert result.exit_code == 2
        assert payload["summary"]["errors"] > 0
        assert [e["fqn"] for e in payload["stale_entries"]] == ["public.broken"]


class TestTheCauseReachesStderr:
    """Every non-zero exit prints a cause to stderr, not only into the machine envelope."""

    def test_a_refused_table_names_itself_on_stderr(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(CASCADE_YAML)
        _seed_real(
            tmp_path,
            committed_print,
            "bad",
            ("seedbank.accession", "seedbank.taxon"),
            datetime.now(UTC),
        )
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check"])

        assert "seedbank.taxon" in result.stderr
        assert result.exit_code == 1

    def test_the_machine_stream_stays_parseable_beside_it(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(CASCADE_YAML)
        _seed_real(
            tmp_path,
            committed_print,
            "bad",
            ("seedbank.accession", "seedbank.taxon"),
            datetime.now(UTC),
        )
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--format", "json"])

        payload = {entry["connection"]: entry for entry in json.loads(result.stdout)}

        assert payload["bad"]["not_run"]
        assert "seedbank.taxon" in result.stderr


class TestValuesSumMismatchIsAWarning:
    """`stats.values-sum-mismatch` reports but doesn't gate: a list drifts on a live table.

    An exhaustive list publishes `values_coverage` 1.0 regardless, so
    `values-coverage-mismatch` recomputes the same 1.0 and never fires here.
    """

    @staticmethod
    def _seed_with_sum_mismatch(tmp_path: Path, committed_print: Path) -> Path:
        """`provenance_country`'s exhaustive list undercounts row_count by one."""

        prints = _seed_clean(tmp_path, committed_print)
        target = prints / "seedbank" / "accession" / "statistics.yaml"
        data = yaml.safe_load(target.read_text())
        # Decrement the last (already lowest-ranked) entry, so the list stays ordered by
        # count descending despite the drop.
        data["columns"]["provenance_country"]["values"][-1]["count"] -= 1
        target.write_text(yaml.safe_dump(data))

        return prints

    def test_a_print_with_only_this_issue_exits_zero(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._seed_with_sum_mismatch(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--format", "json"])
        payload = json.loads(result.stdout)[0]
        codes = {i["code"]: i["severity"] for i in payload["issues"]}

        assert codes["stats.values-sum-mismatch"] == "warning"
        assert "stats.values-coverage-mismatch" not in codes
        assert result.exit_code == 0

    def test_a_genuine_error_in_the_same_print_still_exits_non_zero(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prints = self._seed_with_sum_mismatch(tmp_path, committed_print)
        target = prints / "seedbank" / "accession" / "statistics.yaml"
        data = yaml.safe_load(target.read_text())
        data["columns"]["provenance_country"]["cardinality"] = 99999
        target.write_text(yaml.safe_dump(data))
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--format", "json"])
        payload = json.loads(result.stdout)[0]
        codes = {i["code"]: i["severity"] for i in payload["issues"]}

        assert codes["stats.values-sum-mismatch"] == "warning"
        assert "stats.values-coverage-mismatch" not in codes
        assert codes["stats.cardinality-exceeds-row-count"] == "error"
        assert result.exit_code != 0
