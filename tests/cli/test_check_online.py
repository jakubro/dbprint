"""dbprint check --online + offline statistic assertion CLI tests."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from dbprint.adapters import (
    ColumnMeta,
    ColumnStats,
    CommentsMeta,
    Inferred,
    MockAdapter,
    MockTable,
    UniqueKeyMeta,
    ValueCount,
    trace_context,
)
from dbprint.cli import run_log
from dbprint.cli.main import main


PROJECT_BASE = """\
defaults:
  max_age_days: 7
  statistics: {}
  diff: {}
connections:
  primary:
    adapter: postgres
    auto: true
    output: prints
"""


def _isoformat(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


def _copy_root_files(committed_print: Path, prints: Path, connection: str) -> None:
    """The print's real `reading.md`/`diff.yaml` - the two conformance-required root files.

    `target.selectors` is rewritten to match the hand-authored manifest's own (SPEC 2.5/2.6
    require the two to agree); the rest is left as shipped.
    """

    source = committed_print / "production"
    shutil.copy(source / "reading.md", prints / "reading.md")

    diff_doc = yaml.safe_load((source / "diff.yaml").read_text())
    diff_doc["connection"] = connection
    diff_doc["target"]["selectors"] = {"include": ["*"], "exclude": []}
    diff_doc["baseline"]["path"] = f"prints/{connection}"
    (prints / "diff.yaml").write_text(yaml.safe_dump(diff_doc))


def _seed_clean_print(tmp_path: Path, committed_print: Path) -> Path:
    """Seed a minimal conformance-clean print of the real `fixture.shape_probe`."""

    prints = tmp_path / "prints" / "primary"
    table_dir = prints / "fixture" / "shape_probe"
    table_dir.mkdir(parents=True)

    when = _isoformat(datetime.now(UTC))
    manifest = {
        "format_version": 1,
        "generated_at": when,
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
            "fixture.shape_probe": {
                "type": "table",
                "path": "fixture/shape_probe",
                "artifacts": {"ddl": "ddl.sql", "statistics": "statistics.yaml"},
                "row_count": 3,
                "columns": 5,
                "profiled_at": when,
            },
        },
    }
    statistics = {
        "format_version": 1,
        "table": "fixture.shape_probe",
        "type": "table",
        "profiled_at": when,
        "row_count": 3,
        "row_count_method": "exact",
        "grain": {"keys": [{"columns": ["probe_id"], "detection": "declared"}]},
        "columns": {
            "probe_id": {
                "sql_type": "integer",
                "nullable": False,
                "null_count": 0,
                "null_rate": 0.0,
                "cardinality": 3,
                "cardinality_ratio": 1.0,
                "cardinality_method": "exact",
                "classification": "categorical",
                "values": [
                    {"value": 1, "count": 1},
                    {"value": 2, "count": 1},
                    {"value": 3, "count": 1},
                ],
                "values_coverage": 1.0,
                "values_coverage_method": "measured",
                "distribution": "uniform",
                # No `looks_like`: an integer column withholds `numeric_string` (SPEC 4.1.5).
                "inferred": {"candidate_key": True},
            },
            "logger_ipv4": {
                "sql_type": "character varying(45)",
                "nullable": False,
                "null_count": 0,
                "null_rate": 0.0,
                "cardinality": 1,
                "cardinality_ratio": 0.333333,
                "cardinality_method": "exact",
                "classification": "categorical",
                "values": [{"value": "10.0.0.1", "count": 3}],
                "values_coverage": 1.0,
                "values_coverage_method": "measured",
                "distribution": "dominant_value",
            },
            "json_text": {
                "sql_type": "text",
                "nullable": False,
                "null_count": 0,
                "null_rate": 0.0,
                "cardinality": 3,
                "cardinality_ratio": 1.0,
                "cardinality_method": "exact",
                "classification": "categorical",
                "values": [
                    {"value": '{"reading": 1.0}', "count": 1},
                    {"value": '{"reading": 2.0}', "count": 1},
                    {"value": '{"reading": 3.0}', "count": 1},
                ],
                "values_coverage": 1.0,
                "values_coverage_method": "measured",
                "distribution": "uniform",
                "inferred": {"candidate_key": True},
            },
            "payload_bytes": {
                "sql_type": "bytea",
                "nullable": True,
                "null_count": 0,
                "null_rate": 0.0,
                "classification": "unsupported",
            },
            "tag_list": {
                "sql_type": "text[]",
                "nullable": False,
                "null_count": 0,
                "null_rate": 0.0,
                "classification": "unsupported",
            },
        },
    }
    (prints / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    (table_dir / "ddl.sql").write_text(
        "CREATE TABLE fixture.shape_probe (\n"
        "    probe_id integer NOT NULL,\n"
        "    logger_ipv4 character varying(45) NOT NULL,\n"
        "    json_text text NOT NULL,\n"
        "    payload_bytes bytea,\n"
        "    tag_list text[] NOT NULL\n"
        ");\n",
    )
    (table_dir / "statistics.yaml").write_text(yaml.safe_dump(statistics))
    _copy_root_files(committed_print, prints, "primary")

    return prints


def _project_with_assertions(extra: str) -> str:
    return PROJECT_BASE + extra


class _MockPgAdapter(MockAdapter):
    REQUIRED_KEYS = ("host", "port", "database", "user", "password")

    def __init__(self, _credentials: dict[str, str]) -> None:
        super().__init__(_fixture(), _query_results())


def _fixture() -> dict[str, MockTable]:
    """`fixture.shape_probe` - the print's real 5-column format-coverage table.

    Only `probe_id` matters here; the other four match the committed baseline so a clean run
    drifts on nothing.
    """

    return {
        "fixture.shape_probe": MockTable(
            type="table",
            namespace_path=("fixture", "shape_probe"),
            ddl=(
                "CREATE TABLE fixture.shape_probe (\n"
                "    probe_id integer NOT NULL,\n"
                "    logger_ipv4 character varying(45) NOT NULL,\n"
                "    json_text text NOT NULL,\n"
                "    payload_bytes bytea,\n"
                "    tag_list text[] NOT NULL\n"
                ");\n"
            ),
            columns=[
                ColumnMeta(
                    name="probe_id",
                    sql_type="integer",
                    nullable=False,
                    default=None,
                    ordinal=1,
                ),
                ColumnMeta(
                    name="logger_ipv4",
                    sql_type="character varying(45)",
                    nullable=False,
                    default=None,
                    ordinal=2,
                ),
                ColumnMeta(
                    name="json_text",
                    sql_type="text",
                    nullable=False,
                    default=None,
                    ordinal=3,
                ),
                ColumnMeta(
                    name="payload_bytes",
                    sql_type="bytea",
                    nullable=True,
                    default=None,
                    ordinal=4,
                ),
                ColumnMeta(
                    name="tag_list",
                    sql_type="text[]",
                    nullable=False,
                    default=None,
                    ordinal=5,
                ),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            # Matches `_seed_clean_print`'s committed `id`, so a live run drifts on
            # nothing (SPEC 4.2: a cardinality-3 integer classifies categorical).
            unique_keys=[UniqueKeyMeta(columns=("probe_id",), primary=True)],
            stats={
                "probe_id": ColumnStats(
                    sql_type="integer",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=3,
                    cardinality_ratio=1.0,
                    cardinality_method="exact",
                    values=tuple(ValueCount(value=i, count=1) for i in range(1, 4)),
                    values_coverage=1.0,
                    distribution="uniform",
                    inferred=Inferred(candidate_key=True),
                ),
                "logger_ipv4": ColumnStats(
                    sql_type="character varying(45)",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=1,
                    cardinality_ratio=0.333333,
                    cardinality_method="exact",
                    values=(ValueCount(value="10.0.0.1", count=3),),
                    values_coverage=1.0,
                    distribution="dominant_value",
                ),
                "json_text": ColumnStats(
                    sql_type="text",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=3,
                    cardinality_ratio=1.0,
                    cardinality_method="exact",
                    values=tuple(
                        ValueCount(value=f'{{"reading": {i}.0}}', count=1) for i in range(1, 4)
                    ),
                    values_coverage=1.0,
                    distribution="uniform",
                    inferred=Inferred(candidate_key=True),
                ),
                "payload_bytes": ColumnStats(
                    sql_type="bytea",
                    nullable=True,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=None,
                    cardinality_ratio=None,
                    cardinality_method=None,
                ),
                "tag_list": ColumnStats(
                    sql_type="text[]",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=None,
                    cardinality_ratio=None,
                    cardinality_method=None,
                ),
            },
            samples={"probe_id": [1, 2, 3]},
            row_count=3,
        ),
    }


def _query_results() -> dict[str, list[tuple[Any, ...]]]:
    return {
        "SELECT 0": [(0,)],
        "SELECT 7": [(7,)],
        "SELECT NULL": [(None,)],
    }


def _credential_env() -> dict[str, str]:
    return {
        "DBPRINT_PRIMARY_HOST": "h",
        "DBPRINT_PRIMARY_PORT": "5432",
        "DBPRINT_PRIMARY_DATABASE": "d",
        "DBPRINT_PRIMARY_USER": "u",
        "DBPRINT_PRIMARY_PASSWORD": "p",
    }


def _patch_registry():
    return patch.dict(
        "dbprint.cli.adapter_registry.ADAPTERS",
        {"postgres": _MockPgAdapter},
        clear=True,
    )


class TestOfflineStatisticAssertions:
    def test_clean_run_with_passing_predicate_exit_zero(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(
            _project_with_assertions(
                """\
    assertions:
      tables:
        fixture.shape_probe:
          row_count: {min: 1}
""",
            ),
        )
        _seed_clean_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check"])
        assert result.exit_code == 0

    def test_failing_statistic_predicate_offline_exit_six(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(
            _project_with_assertions(
                """\
    assertions:
      tables:
        fixture.shape_probe:
          row_count: {min: 9999}
""",
            ),
        )
        _seed_clean_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check"])
        assert result.exit_code == 6
        assert "assertion" in result.output.lower()


class TestOnlineNoDrift:
    def test_clean_with_passing_assertions_exit_zero(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(
            _project_with_assertions(
                """\
    assertions:
      tables:
        fixture.shape_probe:
          row_count: {min: 1}
      queries:
        - name: zero_check
          sql: SELECT 0
          expect: 0
""",
            ),
        )
        _seed_clean_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        with _patch_registry():
            result = CliRunner().invoke(main, ["check", "--online"])

        assert result.exit_code == 0


class TestMalformedAssertionsBlockIsNonAborting:
    """ASSERTIONS.md 5.4: one malformed query must not discard sibling assertions."""

    def test_a_malformed_query_does_not_discard_a_passing_table_predicate(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(
            _project_with_assertions(
                """\
    assertions:
      tables:
        fixture.shape_probe:
          row_count: {min: 1}
      queries:
        - name: bad
          sql: SELECT 0
          expect: bogus
""",
            ),
        )
        _seed_clean_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--format", "json"])
        payload = json.loads(result.stdout)[0]
        codes_seen = {i["code"] for i in payload["assertion_issues"]}

        assert "assertion.malformed-block" in codes_seen
        # Absence proves the row_count predicate ran and passed, not that it was skipped.
        assert "assertion.row-count-mismatch" not in codes_seen
        assert result.exit_code == 6

    def test_the_malformed_block_spec_ref_resolves(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(
            _project_with_assertions(
                """\
    assertions:
      queries:
        - name: bad
          sql: SELECT 0
          expect: bogus
""",
            ),
        )
        _seed_clean_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--format", "json"])
        payload = json.loads(result.stdout)[0]
        fault = next(
            i for i in payload["assertion_issues"] if i["code"] == "assertion.malformed-block"
        )

        assert fault["spec_ref"] == "ASSERTIONS.md §1.2"


class TestDuplicateQueryName:
    def test_duplicate_name_is_reported_and_the_first_query_still_runs(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(
            _project_with_assertions(
                """\
    assertions:
      queries:
        - name: q1
          sql: SELECT 0
          expect: 0
        - name: q1
          sql: SELECT 7
          expect: 0
""",
            ),
        )
        _seed_clean_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--format", "json"])
        payload = json.loads(result.stdout)[0]
        codes_seen = [i["code"] for i in payload["assertion_issues"]]

        assert codes_seen == ["assertion.duplicate-query-name"]
        assert result.exit_code == 6


class TestConformanceAndAssertionErrorsCoOccur:
    """ASSERTIONS.md 6.3: exit is the MAX across independently-evaluated conditions."""

    def test_exit_is_the_max_of_both(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(
            _project_with_assertions(
                """\
    assertions:
      tables:
        fixture.shape_probe:
          row_count: {min: 9999}
""",
            ),
        )
        prints = _seed_clean_print(tmp_path, committed_print)
        stats_path = prints / "fixture" / "shape_probe" / "statistics.yaml"
        stats = yaml.safe_load(stats_path.read_text())
        stats["columns"]["probe_id"]["cardinality"] = 9999  # trips a conformance error
        stats_path.write_text(yaml.safe_dump(stats))
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--format", "json"])
        payload = json.loads(result.stdout)[0]

        assert payload["summary"]["errors"] > 0
        assert any(i["code"] == "assertion.row-count-mismatch" for i in payload["assertion_issues"])
        assert result.exit_code == 6


class TestOfflineAssertionErrorDoesNotSuppressOnline:
    """Conformance and staleness gates suppress the online phase; an assertion error does not."""

    def test_the_online_phase_still_runs(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(
            _project_with_assertions(
                """\
    assertions:
      tables:
        fixture.shape_probe:
          row_count: {min: 9999}
      queries:
        - name: online_check
          sql: SELECT 7
          expect: 0
""",
            ),
        )
        _seed_clean_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        with _patch_registry():
            result = CliRunner().invoke(main, ["check", "--online", "--format", "json"])

        payload = json.loads(result.stdout)[0]
        codes_seen = {i["code"] for i in payload["assertion_issues"]}

        # Both failures present proves the online phase ran despite the offline error.
        assert "assertion.row-count-mismatch" in codes_seen
        assert "assertion.sql-non-zero" in codes_seen
        assert result.exit_code == 6

    def test_a_parse_fault_is_not_reported_twice(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Each half re-parses the malformed query separately; the fault reaches the report once."""

        (tmp_path / ".dbprint.yaml").write_text(
            _project_with_assertions(
                """\
    assertions:
      queries:
        - name: bad
          sql: SELECT 0
          expect: bogus
""",
            ),
        )
        _seed_clean_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        with _patch_registry():
            result = CliRunner().invoke(main, ["check", "--online", "--format", "json"])

        payload = json.loads(result.stdout)[0]
        codes_seen = [i["code"] for i in payload["assertion_issues"]]

        assert codes_seen == ["assertion.malformed-block"]
        assert payload["summary"]["assertion_errors"] == 1


def _seed_print_with_cardinality(tmp_path: Path, committed_print: Path, cardinality: int) -> Path:
    """`_seed_clean_print` with a `cardinality` the fixture disagrees with - a stats-only diff."""

    prints = _seed_clean_print(tmp_path, committed_print)
    stats_path = prints / "fixture" / "shape_probe" / "statistics.yaml"
    data = yaml.safe_load(stats_path.read_text())
    row_count = data["row_count"]
    data["columns"]["probe_id"]["cardinality"] = cardinality
    data["columns"]["probe_id"]["cardinality_ratio"] = round(cardinality / row_count, 6)
    # The lowered ratio no longer clears the SPEC 4.2 candidate-key threshold; the marker goes.
    inferred = data["columns"]["probe_id"]["inferred"]
    inferred.pop("candidate_key", None)
    inferred.pop("candidate_key_exception", None)

    # `Inferred`'s minProperties: a producer omits the key rather than emit an empty one.
    if not inferred:
        del data["columns"]["probe_id"]["inferred"]

    stats_path.write_text(yaml.safe_dump(data))

    return prints


class _ExtraColumnAdapter(MockAdapter):
    """Live fixture carries a column the committed print does not - a schema-only change."""

    REQUIRED_KEYS = ("host", "port", "database", "user", "password")

    def __init__(self, _credentials: dict[str, str]) -> None:
        fixture = _fixture()
        table = fixture["fixture.shape_probe"]
        fixture["fixture.shape_probe"] = MockTable(
            type=table.type,
            namespace_path=table.namespace_path,
            ddl=table.ddl,
            columns=[
                *table.columns,
                ColumnMeta(name="extra", sql_type="text", nullable=True, default=None, ordinal=6),
            ],
            relationships=table.relationships,
            indexes=table.indexes,
            comments=table.comments,
            unique_keys=table.unique_keys,
            stats={
                **table.stats,
                "extra": ColumnStats(
                    sql_type="text",
                    nullable=True,
                    null_count=3,
                    null_rate=1.0,
                    cardinality=0,
                    cardinality_ratio=0.0,
                    cardinality_method="exact",
                ),
            },
            samples=table.samples,
            row_count=table.row_count,
        )
        super().__init__(fixture, _query_results())


class _RowCountChangedAdapter(MockAdapter):
    """Live fixture disagrees on `row_count` only - table-grain data, not schema.

    `probe_id` stays fully unique at the grown count, so no grain search is triggered.
    """

    REQUIRED_KEYS = ("host", "port", "database", "user", "password")

    def __init__(self, _credentials: dict[str, str]) -> None:
        fixture = _fixture()
        table = fixture["fixture.shape_probe"]
        fixture["fixture.shape_probe"] = MockTable(
            type=table.type,
            namespace_path=table.namespace_path,
            ddl=table.ddl,
            columns=table.columns,
            relationships=table.relationships,
            indexes=table.indexes,
            comments=table.comments,
            unique_keys=table.unique_keys,
            stats={**table.stats, "probe_id": replace(table.stats["probe_id"], cardinality=5)},
            samples=table.samples,
            row_count=5,
        )
        super().__init__(fixture, _query_results())


class TestDriftVocabulary:
    """A statistics event is reported as one; a schema event keeps its own name."""

    def test_statistics_only_drift_gets_the_statistic_code_and_heading(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(_project_with_assertions(""))
        _seed_print_with_cardinality(tmp_path, committed_print, cardinality=2)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        with _patch_registry():
            json_result = CliRunner().invoke(main, ["check", "--online", "--format", "json"])
            human_result = CliRunner().invoke(main, ["check", "--online"])

        assert json_result.exit_code == 3
        data = json.loads(json_result.stdout)
        codes = {i["code"] for i in data[0]["drift_issues"]}
        assert codes == {"drift.statistic-changed"}

        assert "statistics drift" in human_result.stdout
        assert "schema" not in human_result.stdout

    def test_row_count_only_drift_gets_the_statistic_code_not_the_schema_code(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(_project_with_assertions(""))
        _seed_clean_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        with patch.dict(
            "dbprint.cli.adapter_registry.ADAPTERS",
            {"postgres": _RowCountChangedAdapter},
            clear=True,
        ):
            json_result = CliRunner().invoke(main, ["check", "--online", "--format", "json"])

        assert json_result.exit_code == 3
        data = json.loads(json_result.stdout)
        codes = {i["code"] for i in data[0]["drift_issues"]}
        assert codes == {"drift.statistic-changed"}

    def test_schema_only_drift_keeps_the_schema_code_and_heading(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(_project_with_assertions(""))
        _seed_clean_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        with patch.dict(
            "dbprint.cli.adapter_registry.ADAPTERS",
            {"postgres": _ExtraColumnAdapter},
            clear=True,
        ):
            json_result = CliRunner().invoke(main, ["check", "--online", "--format", "json"])
            human_result = CliRunner().invoke(main, ["check", "--online"])

        assert json_result.exit_code == 3
        data = json.loads(json_result.stdout)
        codes = {i["code"] for i in data[0]["drift_issues"]}
        assert codes == {"drift.schema-changed"}

        assert "schema drift" in human_result.stdout
        assert "statistics drift" not in human_result.stdout

    def test_both_at_once_reports_both_codes_and_the_mixed_heading(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(_project_with_assertions(""))
        _seed_print_with_cardinality(tmp_path, committed_print, cardinality=2)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        with patch.dict(
            "dbprint.cli.adapter_registry.ADAPTERS",
            {"postgres": _ExtraColumnAdapter},
            clear=True,
        ):
            json_result = CliRunner().invoke(main, ["check", "--online", "--format", "json"])
            human_result = CliRunner().invoke(main, ["check", "--online"])

        assert json_result.exit_code == 3
        data = json.loads(json_result.stdout)
        codes = {i["code"] for i in data[0]["drift_issues"]}
        assert codes == {"drift.schema-changed", "drift.statistic-changed"}
        assert data[0]["summary"]["drift_count"] == len(data[0]["drift_issues"])

        assert "schema" in human_result.stdout
        assert "statistics" in human_result.stdout

    def test_generate_exit_code_is_unaffected_by_the_online_split(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(PROJECT_BASE)
        _seed_print_with_cardinality(tmp_path, committed_print, cardinality=2)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        with _patch_registry():
            result = CliRunner().invoke(main, ["generate", "--no-tui", "--force"])

        assert result.exit_code == 0


class TestOnlineSqlAssertions:
    def test_failing_sql_assertion_exit_six(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(
            _project_with_assertions(
                """\
    assertions:
      queries:
        - name: bad
          sql: SELECT 7
          expect: 0
""",
            ),
        )
        _seed_clean_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        with _patch_registry():
            result = CliRunner().invoke(main, ["check", "--online"])

        assert result.exit_code == 6
        assert "assertion.sql-non-zero" in result.output

    def test_warning_severity_does_not_drive_exit(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(
            _project_with_assertions(
                """\
    assertions:
      queries:
        - name: warn_only
          sql: SELECT 7
          expect: 0
          severity: warning
""",
            ),
        )
        _seed_clean_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        with _patch_registry():
            result = CliRunner().invoke(main, ["check", "--online"])

        assert result.exit_code == 0

    def test_the_operators_statement_is_tagged_with_connection_and_phase(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`check --online`'s own SQL-assertion seam tags exec_query's trace like any other."""

        seen: list[tuple[str, str]] = []

        class _RecordingAdapter(_MockPgAdapter):
            def execute_query(self, sql: str) -> list[tuple[Any, ...]]:
                seen.append((trace_context.connection.get(), trace_context.phase.get()))

                return super().execute_query(sql)

        (tmp_path / ".dbprint.yaml").write_text(
            _project_with_assertions(
                """\
    assertions:
      queries:
        - name: tagged
          sql: SELECT 0
          expect: 0
""",
            ),
        )
        _seed_clean_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        with patch.dict(
            "dbprint.cli.adapter_registry.ADAPTERS",
            {"postgres": _RecordingAdapter},
            clear=True,
        ):
            result = CliRunner().invoke(main, ["check", "--online"])

        assert result.exit_code == 0
        assert seen == [("primary", "execute_query")]


class TestFormats:
    def test_json_includes_assertion_block(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(
            _project_with_assertions(
                """\
    assertions:
      tables:
        fixture.shape_probe:
          row_count: {min: 9999}
""",
            ),
        )
        _seed_clean_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["check", "--format", "json"])
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert "assertion_issues" in data[0]
        assert any(i["code"] == "assertion.row-count-mismatch" for i in data[0]["assertion_issues"])


class _UnreachableAdapter(MockAdapter):
    """Adapter whose connect() fails, standing in for an unreachable database."""

    REQUIRED_KEYS = ("host", "port", "database", "user", "password")

    def __init__(self, _credentials: dict[str, str]) -> None:
        super().__init__(_fixture(), _query_results())

    def connect(self) -> None:
        raise RuntimeError("could not connect to host")


class TestConnectionFailureIsReportedAsItself:
    """Drift means a comparison ran; an unreached database never compared, so it is not-run."""

    def _run(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
        *args: str,
    ):
        (tmp_path / ".dbprint.yaml").write_text(_project_with_assertions(""))
        _seed_clean_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        with patch.dict(
            "dbprint.cli.adapter_registry.ADAPTERS",
            {"postgres": _UnreachableAdapter},
            clear=True,
        ):
            return CliRunner().invoke(main, ["check", "--online", *args])

    def test_unreachable_database_exits_connection_not_ok(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = self._run(tmp_path, committed_print, monkeypatch)

        assert result.exit_code == 4

    def test_connection_failure_reaches_the_structured_output(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = self._run(tmp_path, committed_print, monkeypatch, "--format", "json")
        data = json.loads(result.stdout)

        assert [n["subject"] for n in data[0]["not_run"]] == ["primary"]
        assert "could not connect" in data[0]["not_run"][0]["cause"]

    def test_the_failure_is_not_filed_under_drift(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = self._run(tmp_path, committed_print, monkeypatch, "--format", "json")
        data = json.loads(result.stdout)

        assert data[0]["drift_issues"] == []
        assert data[0]["summary"]["drift_count"] == 0

    def test_the_human_output_does_not_claim_drift(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = self._run(tmp_path, committed_print, monkeypatch)

        assert "schema drift" not in result.stdout
        assert "did not run" in result.stdout
        assert "could not connect" in result.stdout


CONTRADICTORY_RULES = """\
    rules:
      - include: ["*"]
        sample: 0.1
      - include: ["fixture.shape_probe"]
        filter: "probe_id > 0"
    assertions:
      queries:
        - name: bad
          sql: SELECT 7
          expect: 0
"""


class TestARefusedTableDoesNotCostTheConnectionItsOnlinePhase:
    """One table its own rules contradict is reported; the connection is still verified."""

    def _run(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
        *args: str,
    ):
        (tmp_path / ".dbprint.yaml").write_text(_project_with_assertions(CONTRADICTORY_RULES))
        _seed_clean_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        with _patch_registry():
            return CliRunner().invoke(main, ["check", "--online", *args])

    def test_the_online_phase_runs(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The failing SQL assertion is only reachable online, so exit 6 proves it ran."""

        result = self._run(tmp_path, committed_print, monkeypatch)

        assert result.exit_code == 6, result.output

    def test_the_refused_table_is_still_reported(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = self._run(tmp_path, committed_print, monkeypatch, "--format", "json")
        payload = json.loads(result.stdout)[0]

        assert [n["subject"] for n in payload["not_run"]] == ["fixture.shape_probe"]

    def test_a_conformance_error_still_suppresses_the_online_phase(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The narrowing is to the refusal trigger, not to the gate."""

        (tmp_path / ".dbprint.yaml").write_text(
            _project_with_assertions(
                """\
    assertions:
      queries:
        - name: bad
          sql: SELECT 7
          expect: 0
""",
            ),
        )
        prints = _seed_clean_print(tmp_path, committed_print)
        stats_path = prints / "fixture" / "shape_probe" / "statistics.yaml"
        stats = yaml.safe_load(stats_path.read_text())
        stats["columns"]["probe_id"]["cardinality"] = 9999
        stats_path.write_text(yaml.safe_dump(stats))
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        with _patch_registry():
            result = CliRunner().invoke(main, ["check", "--online"])

        assert result.exit_code == 1, result.output


def _vault_table() -> MockTable:
    """`seedbank.vault` - the print's real 6-column storage-site table."""

    return MockTable(
        type="table",
        namespace_path=("seedbank", "vault"),
        ddl=(
            "CREATE TABLE seedbank.vault (\n"
            "    vault_id integer NOT NULL,\n"
            "    shelf_code character varying(8) NOT NULL,\n"
            "    site_name character varying(80) NOT NULL,\n"
            "    target_temperature_c numeric(4,1) NOT NULL,\n"
            "    opens_at time without time zone NOT NULL,\n"
            "    closes_at time without time zone NOT NULL\n"
            ");\n"
        ),
        columns=[
            ColumnMeta(
                name="vault_id",
                sql_type="integer",
                nullable=False,
                default=None,
                ordinal=1,
            ),
            ColumnMeta(
                name="shelf_code",
                sql_type="character varying(8)",
                nullable=False,
                default=None,
                ordinal=2,
            ),
            ColumnMeta(
                name="site_name",
                sql_type="character varying(80)",
                nullable=False,
                default=None,
                ordinal=3,
            ),
            ColumnMeta(
                name="target_temperature_c",
                sql_type="numeric(4,1)",
                nullable=False,
                default=None,
                ordinal=4,
            ),
            ColumnMeta(
                name="opens_at",
                sql_type="time without time zone",
                nullable=False,
                default=None,
                ordinal=5,
            ),
            ColumnMeta(
                name="closes_at",
                sql_type="time without time zone",
                nullable=False,
                default=None,
                ordinal=6,
            ),
        ],
        relationships=[],
        indexes=[],
        comments=CommentsMeta(table=None, columns={}),
        unique_keys=[UniqueKeyMeta(columns=("vault_id",), primary=True)],
        stats={
            "vault_id": ColumnStats(
                sql_type="integer",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=3,
                cardinality_ratio=1.0,
                cardinality_method="exact",
                values=tuple(ValueCount(value=i, count=1) for i in range(1, 4)),
                values_coverage=1.0,
                distribution="uniform",
                inferred=Inferred(candidate_key=True),
            ),
            "shelf_code": ColumnStats(
                sql_type="character varying(8)",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=1,
                cardinality_ratio=0.333333,
                cardinality_method="exact",
                values=(ValueCount(value="A", count=3),),
                values_coverage=1.0,
                distribution="dominant_value",
            ),
            "site_name": ColumnStats(
                sql_type="character varying(80)",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=1,
                cardinality_ratio=0.333333,
                cardinality_method="exact",
                values=(ValueCount(value="Example Vault", count=3),),
                values_coverage=1.0,
                distribution="dominant_value",
            ),
            "target_temperature_c": ColumnStats(
                sql_type="numeric(4,1)",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=1,
                cardinality_ratio=0.333333,
                cardinality_method="exact",
                values=(ValueCount(value=-20.0, count=3),),
                values_coverage=1.0,
                distribution="dominant_value",
            ),
            "opens_at": ColumnStats(
                sql_type="time without time zone",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=1,
                cardinality_ratio=0.333333,
                cardinality_method="exact",
                values=(ValueCount(value="07:30:00", count=3),),
                values_coverage=1.0,
                distribution="dominant_value",
            ),
            "closes_at": ColumnStats(
                sql_type="time without time zone",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=1,
                cardinality_ratio=0.333333,
                cardinality_method="exact",
                values=(ValueCount(value="17:00:00", count=3),),
                values_coverage=1.0,
                distribution="dominant_value",
            ),
        },
        samples={"vault_id": [1, 2, 3]},
        row_count=3,
    )


def _two_table_fixture() -> dict[str, MockTable]:
    """`fixture.shape_probe` (stays extractable) plus `seedbank.vault` (the one that fails)."""

    fixture = _fixture()
    fixture["seedbank.vault"] = _vault_table()

    return fixture


def _seed_two_table_print(tmp_path: Path, committed_print: Path) -> Path:
    """`_seed_clean_print` plus a second conformance-clean table, `seedbank.vault`."""

    prints = _seed_clean_print(tmp_path, committed_print)
    table_dir = prints / "seedbank" / "vault"
    table_dir.mkdir(parents=True)

    when = _isoformat(datetime.now(UTC))
    manifest_path = prints / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest["tables"]["seedbank.vault"] = {
        "type": "table",
        "path": "seedbank/vault",
        "artifacts": {"ddl": "ddl.sql", "statistics": "statistics.yaml"},
        "row_count": 3,
        "columns": 6,
        "profiled_at": when,
    }
    manifest_path.write_text(yaml.safe_dump(manifest))

    statistics = {
        "format_version": 1,
        "table": "seedbank.vault",
        "type": "table",
        "profiled_at": when,
        "row_count": 3,
        "row_count_method": "exact",
        "grain": {"keys": [{"columns": ["vault_id"], "detection": "declared"}]},
        "columns": {
            "vault_id": {
                "sql_type": "integer",
                "nullable": False,
                "null_count": 0,
                "null_rate": 0.0,
                "cardinality": 3,
                "cardinality_ratio": 1.0,
                "cardinality_method": "exact",
                "classification": "categorical",
                "values": [
                    {"value": 1, "count": 1},
                    {"value": 2, "count": 1},
                    {"value": 3, "count": 1},
                ],
                "values_coverage": 1.0,
                "values_coverage_method": "measured",
                "distribution": "uniform",
                # No `looks_like`: an integer column withholds `numeric_string` (SPEC 4.1.5).
                "inferred": {"candidate_key": True},
            },
            "shelf_code": {
                "sql_type": "character varying(8)",
                "nullable": False,
                "null_count": 0,
                "null_rate": 0.0,
                "cardinality": 1,
                "cardinality_ratio": 0.333333,
                "cardinality_method": "exact",
                "classification": "categorical",
                "values": [{"value": "A", "count": 3}],
                "values_coverage": 1.0,
                "values_coverage_method": "measured",
                "distribution": "dominant_value",
            },
            "site_name": {
                "sql_type": "character varying(80)",
                "nullable": False,
                "null_count": 0,
                "null_rate": 0.0,
                "cardinality": 1,
                "cardinality_ratio": 0.333333,
                "cardinality_method": "exact",
                "classification": "categorical",
                "values": [{"value": "Example Vault", "count": 3}],
                "values_coverage": 1.0,
                "values_coverage_method": "measured",
                "distribution": "dominant_value",
            },
            "target_temperature_c": {
                "sql_type": "numeric(4,1)",
                "nullable": False,
                "null_count": 0,
                "null_rate": 0.0,
                "cardinality": 1,
                "cardinality_ratio": 0.333333,
                "cardinality_method": "exact",
                "classification": "categorical",
                "values": [{"value": -20.0, "count": 3}],
                "values_coverage": 1.0,
                "values_coverage_method": "measured",
                "distribution": "dominant_value",
            },
            "opens_at": {
                "sql_type": "time without time zone",
                "nullable": False,
                "null_count": 0,
                "null_rate": 0.0,
                "cardinality": 1,
                "cardinality_ratio": 0.333333,
                "cardinality_method": "exact",
                "classification": "categorical",
                "values": [{"value": "07:30:00", "count": 3}],
                "values_coverage": 1.0,
                "values_coverage_method": "measured",
                "distribution": "dominant_value",
            },
            "closes_at": {
                "sql_type": "time without time zone",
                "nullable": False,
                "null_count": 0,
                "null_rate": 0.0,
                "cardinality": 1,
                "cardinality_ratio": 0.333333,
                "cardinality_method": "exact",
                "classification": "categorical",
                "values": [{"value": "17:00:00", "count": 3}],
                "values_coverage": 1.0,
                "values_coverage_method": "measured",
                "distribution": "dominant_value",
            },
        },
    }
    (table_dir / "ddl.sql").write_text(
        "CREATE TABLE seedbank.vault (\n"
        "    vault_id integer NOT NULL,\n"
        "    shelf_code character varying(8) NOT NULL,\n"
        "    site_name character varying(80) NOT NULL,\n"
        "    target_temperature_c numeric(4,1) NOT NULL,\n"
        "    opens_at time without time zone NOT NULL,\n"
        "    closes_at time without time zone NOT NULL\n"
        ");\n",
    )
    (table_dir / "statistics.yaml").write_text(yaml.safe_dump(statistics))

    return prints


class _TwoTableAdapter(MockAdapter):
    """The clean control: both tables extract, standing beside `_PartiallyFailingAdapter`."""

    REQUIRED_KEYS = ("host", "port", "database", "user", "password")

    def __init__(self, _credentials: dict[str, str]) -> None:
        super().__init__(_two_table_fixture(), _query_results())


class _PartiallyFailingAdapter(_TwoTableAdapter):
    """Introspection raises for one table, simulating a permission revoke mid-scan."""

    def introspect_columns(self, fqn: str) -> list[ColumnMeta]:
        if fqn == "seedbank.vault":
            raise RuntimeError("permission denied for table vault")

        return super().introspect_columns(fqn)


class TestAPartialOnlineScanIsReported:
    """A table that fails extraction online is named; the rest of the scan still counts.

    `compute_diff` returns EXIT_PARTIAL with the failed FQNs, not EXIT_CONNECTION.
    """

    def _run(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
        *args: str,
    ):
        (tmp_path / ".dbprint.yaml").write_text(_project_with_assertions(""))
        _seed_two_table_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        with patch.dict(
            "dbprint.cli.adapter_registry.ADAPTERS",
            {"postgres": _PartiallyFailingAdapter},
            clear=True,
        ):
            return CliRunner().invoke(main, ["check", "--online", *args])

    def test_a_partial_scan_does_not_exit_zero(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = self._run(tmp_path, committed_print, monkeypatch)

        assert result.exit_code == 5

    def test_the_failed_table_reaches_not_run_and_stderr(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = self._run(tmp_path, committed_print, monkeypatch, "--format", "json")
        payload = json.loads(result.stdout)[0]

        assert [n["subject"] for n in payload["not_run"]] == ["seedbank.vault"]
        assert payload["not_run"][0]["severity"] == "error"
        assert "seedbank.vault" in result.stderr

    def test_the_extracted_table_still_produces_a_verdict(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A partial scan is not a blocked one - the tables that did extract are still compared."""

        (tmp_path / ".dbprint.yaml").write_text(
            _project_with_assertions(
                """\
    assertions:
      tables:
        fixture.shape_probe:
          row_count: {min: 100}
""",
            ),
        )
        _seed_two_table_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        with patch.dict(
            "dbprint.cli.adapter_registry.ADAPTERS",
            {"postgres": _PartiallyFailingAdapter},
            clear=True,
        ):
            result = CliRunner().invoke(main, ["check", "--online", "--format", "json"])

        payload = json.loads(result.stdout)[0]
        codes = [
            i["code"] for i in payload["assertion_issues"] if "fixture.shape_probe" in i["path"]
        ]

        assert "assertion.row-count-mismatch" in codes

    def test_a_clean_full_scan_still_exits_zero(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Control: without the failing adapter, the same two-table print is clean."""

        (tmp_path / ".dbprint.yaml").write_text(_project_with_assertions(""))
        _seed_two_table_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        with patch.dict(
            "dbprint.cli.adapter_registry.ADAPTERS",
            {"postgres": _TwoTableAdapter},
            clear=True,
        ):
            result = CliRunner().invoke(main, ["check", "--online", "--format", "json"])

        assert result.exit_code == 0
        assert json.loads(result.stdout)[0]["not_run"] == []

    def test_an_assertion_on_the_failed_table_still_warns_without_moving_the_exit(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The warning and the not-run entry are separate; only the entry drives the exit code."""

        (tmp_path / ".dbprint.yaml").write_text(
            _project_with_assertions(
                """\
    assertions:
      tables:
        seedbank.vault:
          row_count: {min: 1}
""",
            ),
        )
        _seed_two_table_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        with patch.dict(
            "dbprint.cli.adapter_registry.ADAPTERS",
            {"postgres": _PartiallyFailingAdapter},
            clear=True,
        ):
            result = CliRunner().invoke(main, ["check", "--online", "--format", "json"])

        payload = json.loads(result.stdout)[0]
        codes = [i["code"] for i in payload["assertion_issues"]]

        assert "assertion.unknown-table" in codes
        assert all(
            i["severity"] == "warning"
            for i in payload["assertion_issues"]
            if i["code"] == "assertion.unknown-table"
        )
        assert result.exit_code == 5

    def test_diff_online_already_names_the_failed_table_unchanged(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`diff`'s JSON payload is the diff dict alone; the failed-table cause reaches stderr."""

        (tmp_path / ".dbprint.yaml").write_text(_project_with_assertions(""))
        _seed_two_table_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        with patch.dict(
            "dbprint.cli.adapter_registry.ADAPTERS",
            {"postgres": _PartiallyFailingAdapter},
            clear=True,
        ):
            result = CliRunner().invoke(main, ["diff", "--format", "json"])

        assert result.exit_code == 5
        assert "seedbank.vault" in result.stderr


CONTRADICTORY_RULES_ONLINE = """\
    rules:
      - include: ["*"]
        sample: 0.1
      - include: ["seedbank.vault"]
        filter: "vault_id > 0"
"""


class _RefusedTablePartiallyFailingAdapter(_PartiallyFailingAdapter):
    """Same as `_PartiallyFailingAdapter`; also refused offline by a contradictory cascade."""


class TestTheSameTableIsNotReportedTwice:
    """A table refused offline and failed per-table online is one `not_run` entry: both
    `thresholds.resolve` and the engine's `_process_table` would produce one, and the
    engine's is kept.
    """

    def _run(self, tmp_path: Path, committed_print: Path, monkeypatch: pytest.MonkeyPatch):
        (tmp_path / ".dbprint.yaml").write_text(
            _project_with_assertions(CONTRADICTORY_RULES_ONLINE),
        )
        _seed_two_table_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        with patch.dict(
            "dbprint.cli.adapter_registry.ADAPTERS",
            {"postgres": _RefusedTablePartiallyFailingAdapter},
            clear=True,
        ):
            return CliRunner().invoke(main, ["check", "--online", "--format", "json"])

    def test_the_table_appears_once(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = self._run(tmp_path, committed_print, monkeypatch)
        payload = json.loads(result.stdout)[0]
        subjects = [n["subject"] for n in payload["not_run"]]

        assert subjects.count("seedbank.vault") == 1

    def test_the_kept_entry_is_the_engines(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The kept cause is the online producer's, not the offline resolver's rules prose."""

        result = self._run(tmp_path, committed_print, monkeypatch)
        payload = json.loads(result.stdout)[0]
        entry = next(n for n in payload["not_run"] if n["subject"] == "seedbank.vault")

        assert "online scan" in entry["cause"]
        assert "rules[" not in entry["cause"]


def _age_the_print(prints: Path, days: int) -> None:
    """Backdate every timestamp the print carries, both files, so the freshness gate fires."""

    stamp = _isoformat(datetime.now(UTC) - timedelta(days=days))

    manifest_path = prints / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest["generated_at"] = stamp

    for entry in manifest["tables"].values():
        entry["profiled_at"] = stamp

    manifest_path.write_text(yaml.safe_dump(manifest))

    stats_path = prints / "fixture" / "shape_probe" / "statistics.yaml"
    stats = yaml.safe_load(stats_path.read_text())
    stats["profiled_at"] = stamp
    stats_path.write_text(yaml.safe_dump(stats))


class TestAStalePrintStillSuppressesTheOnlinePhase:
    """The narrowing is to the refused-table trigger, not to the gate itself."""

    def test_the_online_phase_does_not_run(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The failing SQL assertion would exit 6 if it were reached."""

        (tmp_path / ".dbprint.yaml").write_text(
            _project_with_assertions(
                """\
    assertions:
      queries:
        - name: bad
          sql: SELECT 7
          expect: 0
""",
            ),
        )
        prints = _seed_clean_print(tmp_path, committed_print)
        _age_the_print(prints, days=30)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        with _patch_registry():
            result = CliRunner().invoke(main, ["check", "--online"])

        assert result.exit_code == 2, result.output


# Run log. `run_log.LOGS_ROOT` is redirected to a session-scoped scratch dir by the
# autouse `_redirect_run_log` fixture in tests/conftest.py.


class TestRunLog:
    def test_online_writes_a_log_with_connection_and_summary_records(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(PROJECT_BASE)
        _seed_clean_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        with _patch_registry():
            CliRunner().invoke(main, ["check", "--online"])

        directory = run_log.LOGS_ROOT / run_log._slug(tmp_path)
        files = list(directory.glob("*-check.log"))
        assert len(files) == 1
        text = files[0].read_text()
        assert "connection 'primary'" in text
        assert "summary exit_code=" in text

    def test_offline_check_writes_no_log(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(PROJECT_BASE)
        _seed_clean_print(tmp_path, committed_print)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        with _patch_registry():
            CliRunner().invoke(main, ["check"])

        assert not (run_log.LOGS_ROOT / run_log._slug(tmp_path)).exists()
