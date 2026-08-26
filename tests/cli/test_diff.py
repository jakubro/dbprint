"""dbprint diff - CLI command tests."""

from __future__ import annotations

import json
from dataclasses import replace
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
)
from dbprint.cli import run_log
from dbprint.cli.main import main
from tests.conftest import normalize_instants


PROJECT_YAML = """\
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


def _base_fixture() -> dict[str, MockTable]:
    """`fixture.shape_probe` - the print's real 5-column table; only `probe_id` is exercised."""

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
                ");\n\n"
                "ALTER TABLE ONLY fixture.shape_probe\n"
                "    ADD CONSTRAINT shape_probe_pkey PRIMARY KEY (probe_id);\n"
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
            stats={
                "probe_id": ColumnStats(
                    sql_type="integer",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=10,
                    cardinality_ratio=1.0,
                    cardinality_method="exact",
                    inferred=Inferred(candidate_key=True),
                ),
                "logger_ipv4": ColumnStats(
                    sql_type="character varying(45)",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=2,
                    cardinality_ratio=0.2,
                    cardinality_method="exact",
                ),
                "json_text": ColumnStats(
                    sql_type="text",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=10,
                    cardinality_ratio=1.0,
                    cardinality_method="exact",
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
            samples={"probe_id": list(range(1, 11))},
            row_count=10,
        ),
    }


def _added_table_fixture() -> dict[str, MockTable]:
    """`_base_fixture` plus `seedbank.vault` - a real object appearing wholesale as added."""

    fixture = _base_fixture()
    fixture["seedbank.vault"] = MockTable(
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
            ");\n\n"
            "ALTER TABLE ONLY seedbank.vault\n"
            "    ADD CONSTRAINT vault_pkey PRIMARY KEY (vault_id, shelf_code);\n"
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
        stats={
            "vault_id": ColumnStats(
                sql_type="integer",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=5,
                cardinality_ratio=1.0,
                cardinality_method="exact",
                inferred=Inferred(candidate_key=True),
            ),
            "shelf_code": ColumnStats(
                sql_type="character varying(8)",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=1,
                cardinality_ratio=0.2,
                cardinality_method="exact",
            ),
            "site_name": ColumnStats(
                sql_type="character varying(80)",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=5,
                cardinality_ratio=1.0,
                cardinality_method="exact",
            ),
            "target_temperature_c": ColumnStats(
                sql_type="numeric(4,1)",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=1,
                cardinality_ratio=0.2,
                cardinality_method="exact",
            ),
            "opens_at": ColumnStats(
                sql_type="time without time zone",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=1,
                cardinality_ratio=0.2,
                cardinality_method="exact",
            ),
            "closes_at": ColumnStats(
                sql_type="time without time zone",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=1,
                cardinality_ratio=0.2,
                cardinality_method="exact",
            ),
        },
        samples={"vault_id": list(range(1, 6))},
        row_count=5,
    )

    return fixture


class _MockPostgresAdapterBase(MockAdapter):
    REQUIRED_KEYS = ("host", "port", "database", "user", "password")

    def __init__(self, _credentials: dict[str, str]) -> None:
        super().__init__(_base_fixture())


class _MockPostgresAdapterDrifted(MockAdapter):
    REQUIRED_KEYS = ("host", "port", "database", "user", "password")

    def __init__(self, _credentials: dict[str, str]) -> None:
        super().__init__(_added_table_fixture())


def _moved_statistics_fixture() -> dict[str, MockTable]:
    """Variant moving `probe_id`'s distinct count by a tenth - data drift, no schema change."""

    fixture = _base_fixture()
    probe = fixture["fixture.shape_probe"]
    moved = replace(probe.stats["probe_id"], cardinality=9, cardinality_ratio=0.9)
    fixture["fixture.shape_probe"] = replace(probe, stats={**probe.stats, "probe_id": moved})

    return fixture


class _MockPostgresAdapterMovedStatistics(MockAdapter):
    REQUIRED_KEYS = ("host", "port", "database", "user", "password")

    def __init__(self, _credentials: dict[str, str]) -> None:
        super().__init__(_moved_statistics_fixture())


def _setup_project(tmp_path: Path) -> None:
    (tmp_path / ".dbprint.yaml").write_text(PROJECT_YAML)


def _credential_env() -> dict[str, str]:
    return {
        "DBPRINT_PRIMARY_HOST": "h",
        "DBPRINT_PRIMARY_PORT": "5432",
        "DBPRINT_PRIMARY_DATABASE": "d",
        "DBPRINT_PRIMARY_USER": "u",
        "DBPRINT_PRIMARY_PASSWORD": "p",
    }


def _patch_registry(adapter_class: type) -> Any:
    return patch.dict(
        "dbprint.cli.adapter_registry.ADAPTERS",
        {"postgres": adapter_class},
        clear=True,
    )


class TestNoBaseline:
    def test_returns_exit_one_with_hard_error_message(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup_project(tmp_path)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        runner = CliRunner()

        with _patch_registry(_MockPostgresAdapterBase):
            result = runner.invoke(main, ["diff", "--no-tui"])

        assert result.exit_code == 1
        assert "No committed prints at prints/primary/" in result.output + (result.stderr or "")


class TestCleanState:
    def test_exit_zero_when_no_drift(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_project(tmp_path)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        runner = CliRunner()

        with _patch_registry(_MockPostgresAdapterBase):
            runner.invoke(main, ["generate", "--no-tui"])
            result = runner.invoke(main, ["diff", "--no-tui"])

        assert result.exit_code == 0


class TestEmptySelectorMatch:
    """A selector matching no table is reported, not silently compared as clean."""

    def test_reports_the_empty_match_and_still_exits_zero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup_project(tmp_path)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        runner = CliRunner()

        with _patch_registry(_MockPostgresAdapterBase):
            runner.invoke(main, ["generate", "--no-tui"])
            result = runner.invoke(main, ["diff", "--no-tui", "--include", "no.such.table"])

        combined = result.output + (result.stderr or "")

        assert result.exit_code == 0
        assert "no tables matched selectors" in combined


class TestDriftState:
    def test_added_section_lists_new_table(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup_project(tmp_path)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        runner = CliRunner()

        # Generate baseline with base fixture (one table).
        with _patch_registry(_MockPostgresAdapterBase):
            runner.invoke(main, ["generate", "--no-tui"])

        # Diff against drifted fixture (extra table).
        with _patch_registry(_MockPostgresAdapterDrifted):
            result = runner.invoke(main, ["diff", "--no-tui"])

        assert result.exit_code == 0
        assert "seedbank.vault" in result.output
        assert "Added:" in result.output


class TestNoDiskWritesOnDiff:
    def test_diff_does_not_overwrite_baseline_files(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup_project(tmp_path)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        runner = CliRunner()

        with _patch_registry(_MockPostgresAdapterBase):
            runner.invoke(main, ["generate", "--no-tui"])

        manifest = tmp_path / "prints" / "primary" / "manifest.yaml"
        diff_file = tmp_path / "prints" / "primary" / "diff.yaml"
        manifest_mtime = manifest.stat().st_mtime_ns
        diff_mtime = diff_file.stat().st_mtime_ns

        with _patch_registry(_MockPostgresAdapterDrifted):
            runner.invoke(main, ["diff", "--no-tui"])

        assert manifest.stat().st_mtime_ns == manifest_mtime
        assert diff_file.stat().st_mtime_ns == diff_mtime


class TestFormats:
    def test_yaml_emits_spec_shape(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_project(tmp_path)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        runner = CliRunner()

        with _patch_registry(_MockPostgresAdapterBase):
            runner.invoke(main, ["generate", "--no-tui"])
            result = runner.invoke(main, ["diff", "--no-tui", "--format", "yaml"])

        assert result.exit_code == 0
        # Payload is on stdout; progress (if any) is on stderr and must not pollute it.
        docs = list(yaml.safe_load_all(result.stdout))
        assert len(docs) >= 1
        assert docs[0]["connection"] == "primary"
        assert "changes" in docs[0]
        assert "summary" in docs[0]

    def test_json_emits_array(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_project(tmp_path)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        runner = CliRunner()

        with _patch_registry(_MockPostgresAdapterBase):
            runner.invoke(main, ["generate", "--no-tui"])
            result = runner.invoke(main, ["diff", "--no-tui", "--format", "json"])

        assert result.exit_code == 0
        # Payload is on stdout; progress (if any) is on stderr and must not pollute it.
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert data[0]["connection"] == "primary"


class TestOutputFile:
    def test_writes_to_file_overwriting_silently(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup_project(tmp_path)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        out_path = tmp_path / "diff.txt"
        out_path.write_text("stale content\n")
        runner = CliRunner()

        with _patch_registry(_MockPostgresAdapterBase):
            runner.invoke(main, ["generate", "--no-tui"])
            result = runner.invoke(main, ["diff", "--no-tui", "--output", str(out_path)])

        assert result.exit_code == 0
        contents = out_path.read_text()
        assert "Connection: primary" in contents
        assert "stale content" not in contents


class TestThresholdOverride:
    def test_threshold_zero_is_accepted(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The mock produces only structural drift, so the floor has nothing to admit."""

        _setup_project(tmp_path)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        runner = CliRunner()

        with _patch_registry(_MockPostgresAdapterBase):
            runner.invoke(main, ["generate", "--no-tui"])
            result = runner.invoke(main, ["diff", "--no-tui", "--threshold", "0"])

        assert result.exit_code == 0


class TestProgress:
    def _seed_and_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_project(tmp_path)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

    def test_default_run_streams_progress_to_stderr_clean_stdout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._seed_and_env(tmp_path, monkeypatch)
        runner = CliRunner()

        with _patch_registry(_MockPostgresAdapterBase):
            runner.invoke(main, ["generate", "--no-tui"])
            result = runner.invoke(main, ["diff", "--no-tui", "--format", "json"])

        assert result.exit_code == 0

        # stdout is the clean payload (progress did not leak into it).
        data = json.loads(result.stdout)
        assert data[0]["connection"] == "primary"

        # stderr carries the streaming progress + the per-connection summary.
        assert "fixture.shape_probe" in result.stderr
        assert "summary" in result.stderr

    def test_quiet_silences_stderr_and_preserves_stdout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._seed_and_env(tmp_path, monkeypatch)
        runner = CliRunner()

        with _patch_registry(_MockPostgresAdapterBase):
            runner.invoke(main, ["generate", "--no-tui"])
            default = runner.invoke(main, ["diff", "--no-tui", "--format", "json"])
            quiet = runner.invoke(main, ["diff", "-q", "--no-tui", "--format", "json"])

        assert quiet.exit_code == 0
        assert quiet.stderr == ""
        # Progress is additive: payload matches with/without it, aside from stamped instants.
        assert normalize_instants(quiet.stdout) == normalize_instants(default.stdout)

    def test_json_stdout_stays_parseable_with_progress(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._seed_and_env(tmp_path, monkeypatch)
        runner = CliRunner()

        with _patch_registry(_MockPostgresAdapterBase):
            runner.invoke(main, ["generate", "--no-tui"])
            with_progress = runner.invoke(main, ["diff", "--no-tui", "--format", "json"])
            without_progress = runner.invoke(main, ["diff", "-q", "--no-tui", "--format", "json"])

        # stdout parses cleanly and matches the no-progress payload, aside from stamped instants.
        assert json.loads(normalize_instants(with_progress.stdout)) == json.loads(
            normalize_instants(without_progress.stdout),
        )


class TestResolutionErrors:
    def test_no_config_exits_nonzero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["diff", "--no-tui"])
        assert result.exit_code != 0


class TestUnreadableBaseline:
    """A manifest parsing to nothing shares the exit code with a refused config, not its cause."""

    def test_an_empty_manifest_is_not_reported_as_a_configuration_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup_project(tmp_path)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        runner = CliRunner()

        with _patch_registry(_MockPostgresAdapterBase):
            runner.invoke(main, ["generate", "--no-tui"])
            (tmp_path / "prints" / "primary" / "manifest.yaml").write_text("")
            result = runner.invoke(main, ["diff", "--no-tui"])

        combined = result.output + (result.stderr or "")

        assert result.exit_code == 1
        assert "configuration error" not in combined
        assert "redaction_salt" not in combined


def _project_yaml(
    *,
    project_ratio: float | None = None,
    connection_ratio: float | None = None,
) -> str:
    """Project config setting `cardinality_ratio`'s threshold at either level, or at neither -
    not `default`, which the parser seeds from spec defaults.
    """

    connection: dict[str, Any] = {"adapter": "postgres", "auto": True, "output": "prints"}
    project: dict[str, Any] = {
        "defaults": {"max_age_days": 7, "statistics": {}, "diff": {}},
        "connections": {"primary": connection},
    }

    if project_ratio is not None:
        project["defaults"]["diff"] = {
            "stat_change_threshold": {"cardinality_ratio": project_ratio},
        }

    if connection_ratio is not None:
        connection["diff"] = {"stat_change_threshold": {"cardinality_ratio": connection_ratio}}

    return yaml.dump(project, sort_keys=False)


def _diff_against_moved_statistics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    project_yaml: str,
    *args: str,
) -> str:
    """Commit a print, then diff a database whose statistics moved, and return stdout."""

    (tmp_path / ".dbprint.yaml").write_text(project_yaml)
    monkeypatch.chdir(tmp_path)

    for k, v in _credential_env().items():
        monkeypatch.setenv(k, v)

    runner = CliRunner()

    with _patch_registry(_MockPostgresAdapterBase):
        runner.invoke(main, ["generate", "--no-tui"])

    with _patch_registry(_MockPostgresAdapterMovedStatistics):
        return runner.invoke(main, ["diff", "--no-tui", "-q", *args]).stdout


def _two_connection_yaml(*, coarse: float, fine: float) -> str:
    """Two connections differing only in the threshold each configures."""

    project: dict[str, Any] = {
        "defaults": {"max_age_days": 7, "statistics": {}, "diff": {}},
        "connections": {
            "coarse": {
                "adapter": "postgres",
                "auto": True,
                "output": "prints",
                "diff": {"stat_change_threshold": {"cardinality_ratio": coarse}},
            },
            "fine": {
                "adapter": "postgres",
                "auto": True,
                "output": "prints",
                "diff": {"stat_change_threshold": {"cardinality_ratio": fine}},
            },
        },
    }

    return yaml.dump(project, sort_keys=False)


def _sections(stdout: str, first: str, second: str) -> tuple[str, str]:
    """Split a multi-connection render into one block per connection."""

    blocks = {
        block.splitlines()[0].strip(): block
        for block in stdout.split("Connection: ")
        if block.strip()
    }

    return blocks[first], blocks[second]


class TestConfiguredThreshold:
    """SPEC 2.6.9: the human render filters on the connection's own thresholds - the shift sits
    between the coarse and fine ones, so which is read decides visibility.
    """

    STAT_LINE = "probe_id cardinality_ratio"

    def test_a_coarse_threshold_hides_the_line(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stdout = _diff_against_moved_statistics(
            tmp_path,
            monkeypatch,
            _project_yaml(connection_ratio=0.5),
        )

        assert self.STAT_LINE not in stdout

    def test_a_fine_threshold_shows_it(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stdout = _diff_against_moved_statistics(
            tmp_path,
            monkeypatch,
            _project_yaml(connection_ratio=0.0001),
        )

        assert self.STAT_LINE in stdout

    def test_the_connection_value_beats_the_project_default(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stdout = _diff_against_moved_statistics(
            tmp_path,
            monkeypatch,
            _project_yaml(project_ratio=0.5, connection_ratio=0.0001),
        )

        assert self.STAT_LINE in stdout

    def test_the_flag_still_overrides_a_configured_threshold(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stdout = _diff_against_moved_statistics(
            tmp_path,
            monkeypatch,
            _project_yaml(connection_ratio=0.5),
            "--threshold",
            "0.0001",
        )

        assert self.STAT_LINE in stdout

    def test_machine_output_ignores_the_configured_threshold(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stdout = _diff_against_moved_statistics(
            tmp_path,
            monkeypatch,
            _project_yaml(connection_ratio=0.5),
            "--format",
            "json",
        )
        changes = json.loads(stdout)[0]["changes"]

        # .get, not []: the moved cardinality drops `id` off the candidate-key threshold, so
        # the grain search runs too and its changes carry no `stat` key.
        assert [c for c in changes if c.get("stat") == "cardinality_ratio"]

    def test_each_connection_is_filtered_by_its_own_threshold(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two connections, two thresholds: neither render may read the other's."""

        (tmp_path / ".dbprint.yaml").write_text(_two_connection_yaml(coarse=0.5, fine=0.0001))
        monkeypatch.chdir(tmp_path)

        for name in ("COARSE", "FINE"):
            for key, value in _credential_env().items():
                monkeypatch.setenv(key.replace("PRIMARY", name), value)

        runner = CliRunner()

        with _patch_registry(_MockPostgresAdapterBase):
            runner.invoke(main, ["generate", "--no-tui"])

        with _patch_registry(_MockPostgresAdapterMovedStatistics):
            stdout = runner.invoke(main, ["diff", "--no-tui", "-q"]).stdout

        coarse_section, fine_section = _sections(stdout, "coarse", "fine")

        assert self.STAT_LINE not in coarse_section
        assert self.STAT_LINE in fine_section


# Run log. `run_log.LOGS_ROOT` is redirected to a session-scoped scratch dir by the
# autouse `_redirect_run_log` fixture in tests/conftest.py.


class TestRunLog:
    def test_writes_a_log_with_per_table_and_summary_records(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup_project(tmp_path)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        runner = CliRunner()

        with _patch_registry(_MockPostgresAdapterBase):
            runner.invoke(main, ["generate", "--no-tui"])
            runner.invoke(main, ["diff", "--no-tui"])

        directory = run_log.LOGS_ROOT / run_log._slug(tmp_path)
        files = sorted(directory.glob("*-diff.log"))
        assert len(files) == 1
        text = files[0].read_text()
        assert "connection 'primary'" in text
        assert "summary exit_code=" in text

    def test_quiet_still_writes_a_log(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`-q` silences stderr progress only - the file sink is a separate handler."""

        _setup_project(tmp_path)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        runner = CliRunner()

        with _patch_registry(_MockPostgresAdapterBase):
            runner.invoke(main, ["generate", "--no-tui"])
            runner.invoke(main, ["diff", "--no-tui", "-q"])

        directory = run_log.LOGS_ROOT / run_log._slug(tmp_path)
        assert len(list(directory.glob("*-diff.log"))) == 1
