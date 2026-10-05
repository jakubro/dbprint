"""Error-surfacing matrix - every non-zero exit prints a cause to stderr.

Unreachable connection, bad driver, unknown adapter, zero-match selectors, malformed
manifest, uncaught exception: in each, stdout stays data-only and no password is echoed.
"""

from __future__ import annotations

import json
from contextlib import AbstractContextManager
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from dbprint.adapters import ColumnMeta, ColumnStats, CommentsMeta, Inferred, MockAdapter, MockTable
from dbprint.cli.main import main
from tests._prints import (
    SHAPE_PROBE_COLUMNS,
    VAULT_COLUMNS,
    exact_stats,
    mock_table,
    unmeasured_stats,
    uuid_id_table,
)


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

_PASSWORD = "topsecret-pw"

_CREDS = {
    "DBPRINT_PRIMARY_HOST": "badhost",
    "DBPRINT_PRIMARY_PORT": "5432",
    "DBPRINT_PRIMARY_DATABASE": "db",
    "DBPRINT_PRIMARY_USER": "u",
    "DBPRINT_PRIMARY_PASSWORD": _PASSWORD,
}


class _ConnectFails(MockAdapter):
    """Adapter that fails at connect() with a fixed message (no password)."""

    REQUIRED_KEYS = ("host", "port", "database", "user", "password")
    MESSAGE = "could not connect to Postgres at badhost:5432/db as 'u': connection refused"

    def __init__(self, _credentials: dict[str, str], **_options: object) -> None:
        super().__init__({"public.t": uuid_id_table("public.t")})

    def connect(self) -> None:
        raise RuntimeError(self.MESSAGE)


class _MissingExtra(_ConnectFails):
    MESSAGE = "the postgres adapter requires the [postgres] extra - pip install dbprint[postgres]"


class _Healthy(MockAdapter):
    REQUIRED_KEYS = ("host", "port", "database", "user", "password")

    def __init__(self, _credentials: dict[str, str], **_options: object) -> None:
        super().__init__({"public.t": uuid_id_table("public.t")})


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".dbprint.yaml").write_text(PROJECT_YAML)
    monkeypatch.chdir(tmp_path)

    for k, v in _CREDS.items():
        monkeypatch.setenv(k, v)

    return tmp_path


_UNPARSEABLE_PROJECT_YAML = PROJECT_YAML.replace("  primary:", "  production:")


@pytest.fixture
def unparseable_credentials(
    tmp_path: Path,
    committed_print: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """A project whose credentials file fails to parse on the line holding the password.

    Named for the shipped print's connection, so `check` reaches the online phase at all.
    """

    del committed_print
    (tmp_path / ".dbprint.yaml").write_text(_UNPARSEABLE_PROJECT_YAML)
    monkeypatch.chdir(tmp_path)

    for key in (*_CREDS, *(k.replace("PRIMARY", "PRODUCTION") for k in _CREDS)):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("HOME", str(tmp_path))
    creds = tmp_path / ".dbprint" / "connections.yaml"
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text(f"production:\n  host: a\n  password: *{_PASSWORD}\n", encoding="utf-8")

    return tmp_path


def _registry(adapter: type[MockAdapter]) -> AbstractContextManager[None]:
    return patch.dict("dbprint.cli.adapter_registry.ADAPTERS", {"postgres": adapter}, clear=True)


def _write_manifest(project_dir: Path, body: str) -> None:
    manifest = project_dir / "prints" / "primary" / "manifest.yaml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(body)


class TestUnreachableConnection:
    @pytest.mark.parametrize("command", ["generate", "diff"])
    def test_exits_4_with_the_cause_and_no_payload(self, project: Path, command: str) -> None:
        _write_manifest(
            project,
            "format_version: 1\nadapter: postgres\ngenerated_at: 'x'\ntables: {}\n",
        )
        runner = CliRunner()

        with _registry(_ConnectFails):
            result = runner.invoke(main, [command, "--no-tui"])

        assert result.exit_code == 4
        assert "connection refused" in result.stderr
        assert "primary" in result.stderr
        assert result.stdout.strip() == ""


class TestGenerateErrors:
    def test_missing_driver_extra_hint_reaches_stderr(self, project: Path) -> None:
        runner = CliRunner()

        with _registry(_MissingExtra):
            result = runner.invoke(main, ["generate", "--no-tui"])

        assert result.exit_code == 4
        assert "pip install dbprint[postgres]" in result.stderr

    def test_unknown_adapter_named_in_stderr(self, project: Path) -> None:
        runner = CliRunner()

        # Empty registry -> get_adapter_class('postgres') raises the unknown-adapter error.
        with patch.dict("dbprint.cli.adapter_registry.ADAPTERS", {}, clear=True):
            result = runner.invoke(main, ["generate", "--no-tui"])

        assert result.exit_code == 4
        assert "postgres" in result.stderr
        assert "adapter" in result.stderr.lower()

    def test_no_password_in_error_text(self, project: Path) -> None:
        runner = CliRunner()

        with _registry(_ConnectFails):
            result = runner.invoke(main, ["generate", "--no-tui"])

        assert _PASSWORD not in result.stderr
        assert _PASSWORD not in result.output


def _storage_reading_table() -> MockTable:
    """`seedbank.storage_reading` - the print's real, currently-empty partitioned table."""

    columns = [
        ("reading_id", "bigint"),
        ("vault_id", "integer"),
        ("shelf_code", "character varying(8)"),
        ("reading_date", "date"),
        ("temperature_c", "numeric(4,1)"),
    ]

    return MockTable(
        type="table",
        namespace_path=("seedbank", "storage_reading"),
        ddl=(
            "CREATE TABLE seedbank.storage_reading (\n"
            "    reading_id bigint NOT NULL,\n"
            "    vault_id integer NOT NULL,\n"
            "    shelf_code character varying(8) NOT NULL,\n"
            "    reading_date date NOT NULL,\n"
            "    temperature_c numeric(4,1) NOT NULL\n"
            ")\n"
            "PARTITION BY RANGE (reading_date);\n"
        ),
        columns=[
            ColumnMeta(name=name, sql_type=sql_type, nullable=False, default=None, ordinal=i)
            for i, (name, sql_type) in enumerate(columns, start=1)
        ],
        relationships=[],
        indexes=[],
        comments=CommentsMeta(table=None, columns={}),
        stats={
            name: ColumnStats(
                sql_type=sql_type,
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=0,
                cardinality_ratio=0.0,
                cardinality_method="exact",
            )
            for name, sql_type in columns
        },
        samples={},
        row_count=0,
    )


def _vault_table() -> MockTable:
    """`seedbank.vault` - the print's real 6-column storage-site table."""

    return mock_table(
        "seedbank.vault",
        VAULT_COLUMNS,
        {
            "vault_id": exact_stats("integer", 8, 0.166667),
            "shelf_code": exact_stats("character varying(8)", 6, 0.125),
            "site_name": exact_stats("character varying(80)", 8, 0.166667),
            "target_temperature_c": exact_stats("numeric(4,1)", 3, 0.0625),
            "opens_at": exact_stats("time without time zone", 2, 0.041667),
            "closes_at": exact_stats("time without time zone", 2, 0.041667),
        },
        primary_key=("vault_id", "shelf_code"),
        row_count=48,
    )


def _shape_probe_table() -> MockTable:
    """`fixture.shape_probe` - the print's real 5-column format-coverage table."""

    return mock_table(
        "fixture.shape_probe",
        SHAPE_PROBE_COLUMNS,
        {
            "probe_id": exact_stats("integer", 50, 1.0, inferred=Inferred(candidate_key=True)),
            "logger_ipv4": exact_stats("character varying(45)", 10, 0.2),
            "json_text": exact_stats("text", 50, 1.0),
            "payload_bytes": unmeasured_stats("bytea", nullable=True),
            "tag_list": unmeasured_stats("text[]"),
        },
        primary_key=("probe_id",),
        row_count=50,
    )


def _three_real_tables() -> dict[str, MockTable]:
    """Three real objects from the committed print, used across this file's error scenarios."""

    return {
        "seedbank.storage_reading": _storage_reading_table(),
        "seedbank.vault": _vault_table(),
        "fixture.shape_probe": _shape_probe_table(),
    }


def _two_real_tables() -> dict[str, MockTable]:
    """Two of the three real objects above, for the two-table failure scenarios."""

    return {
        "seedbank.storage_reading": _storage_reading_table(),
        "seedbank.vault": _vault_table(),
    }


class _AllDdlFail(MockAdapter):
    """Every table fails identically in extract_ddl, as a driver fault would."""

    REQUIRED_KEYS = ("host", "port", "database", "user", "password")

    def __init__(self, _credentials: dict[str, str], **_options: object) -> None:
        super().__init__(_three_real_tables())

    def extract_ddl(self, fqn: str) -> str:
        raise TypeError("not all arguments converted during string formatting")


class _MixedFail(MockAdapter):
    """Two tables failing for two different reasons."""

    REQUIRED_KEYS = ("host", "port", "database", "user", "password")

    def __init__(self, _credentials: dict[str, str], **_options: object) -> None:
        super().__init__(_two_real_tables())

    def extract_ddl(self, fqn: str) -> str:
        if fqn == "seedbank.storage_reading":
            raise TypeError("first cause")

        raise ValueError("second cause")


class _SameMessageDifferentOps(MockAdapter):
    """Two tables failing with an identical message from two different calls."""

    REQUIRED_KEYS = ("host", "port", "database", "user", "password")
    MESSAGE = "not all arguments converted during string formatting"

    def __init__(self, _credentials: dict[str, str], **_options: object) -> None:
        super().__init__(_two_real_tables())

    def extract_ddl(self, fqn: str) -> str:
        if fqn == "seedbank.storage_reading":
            raise TypeError(self.MESSAGE)

        return super().extract_ddl(fqn)

    def introspect_columns(self, fqn: str):
        if fqn == "seedbank.vault":
            raise TypeError(self.MESSAGE)

        return super().introspect_columns(fqn)


class TestPerTableFailureContext:
    def test_exception_type_and_operation_are_named(self, project: Path) -> None:
        runner = CliRunner()

        with _registry(_AllDdlFail):
            result = runner.invoke(main, ["generate", "--no-tui"])

        assert "TypeError" in result.stderr
        assert "extract_ddl" in result.stderr

    def test_identical_causes_collapse_to_one_block_with_count(self, project: Path) -> None:
        runner = CliRunner()

        with _registry(_AllDdlFail):
            result = runner.invoke(main, ["generate", "--no-tui"])

        # Progress and the deferred failure block share stderr, and the raw message also appears
        # on each table's progress line - so the count targets the collapsed block's own line.
        assert "3 tables failed" in result.stderr
        assert result.stderr.count("3 tables failed: TypeError: not all arguments converted") == 1

    def test_distinct_causes_report_separately(self, project: Path) -> None:
        runner = CliRunner()

        with _registry(_MixedFail):
            result = runner.invoke(main, ["generate", "--no-tui"])

        assert "TypeError: first cause" in result.stderr
        assert "ValueError: second cause" in result.stderr

    def test_debug_appends_traceback_and_default_does_not(self, project: Path) -> None:
        runner = CliRunner()

        with _registry(_AllDdlFail):
            plain = runner.invoke(main, ["generate", "--no-tui"])

        with _registry(_AllDdlFail):
            debug = runner.invoke(main, ["--debug", "generate", "--no-tui"])

        assert "Traceback (most recent call last)" in debug.stderr
        assert "Traceback (most recent call last)" not in plain.stderr

    def test_same_message_from_different_operations_stays_separate(self, project: Path) -> None:
        """The operation is part of a failure's identity, not just its detail."""

        runner = CliRunner()

        with _registry(_SameMessageDifferentOps):
            result = runner.invoke(main, ["generate", "--no-tui"])

        assert "extract_ddl" in result.stderr
        assert "introspect_columns" in result.stderr
        assert "2 tables failed" not in result.stderr
        assert result.stderr.count("1 table failed") == 2

    def test_no_password_in_grouped_failure_report(self, project: Path) -> None:
        runner = CliRunner()

        with _registry(_AllDdlFail):
            result = runner.invoke(main, ["--debug", "generate", "--no-tui"])

        assert _PASSWORD not in result.stderr
        assert _PASSWORD not in result.output


class TestAnUnparseableCredentialsFile:
    """The file that fails to parse is the one holding secrets, so its text stays unquoted."""

    def test_generate_reports_the_position_and_not_the_credential(
        self,
        unparseable_credentials: Path,
    ) -> None:
        runner = CliRunner()

        with _registry(MockAdapter):
            result = runner.invoke(main, ["generate", "--no-tui"])

        assert _PASSWORD not in result.stderr
        assert _PASSWORD not in result.output
        assert "invalid YAML" in result.stderr
        assert "line 3" in result.stderr

    def test_the_json_envelope_carries_no_credential(
        self,
        unparseable_credentials: Path,
    ) -> None:
        del unparseable_credentials
        runner = CliRunner()

        with _registry(MockAdapter):
            result = runner.invoke(
                main,
                ["check", "--online", "--max-age", "36500d", "--format", "json"],
            )

        assert _PASSWORD not in result.stdout
        assert _PASSWORD not in result.stderr

        causes = [n["cause"] for entry in json.loads(result.stdout) for n in entry["not_run"]]

        assert causes
        assert all(_PASSWORD not in cause for cause in causes)
        assert any("invalid YAML" in cause for cause in causes)


class _OneOfThreeFails(MockAdapter):
    """One table fails; the other two produce prints."""

    REQUIRED_KEYS = ("host", "port", "database", "user", "password")

    def __init__(self, _credentials: dict[str, str], **_options: object) -> None:
        super().__init__(_three_real_tables())

    def extract_ddl(self, fqn: str) -> str:
        if fqn == "seedbank.storage_reading":
            raise TypeError("only this one")

        return super().extract_ddl(fqn)


class TestTotalVersusPartialExit:
    def test_all_tables_failing_exits_seven(self, project: Path) -> None:
        runner = CliRunner()

        with _registry(_AllDdlFail):
            result = runner.invoke(main, ["generate", "--no-tui"])

        assert result.exit_code == 7
        assert "no tables were profiled" in result.stderr

    def test_some_tables_failing_still_exits_five(self, project: Path) -> None:
        runner = CliRunner()

        with _registry(_OneOfThreeFails):
            result = runner.invoke(main, ["generate", "--no-tui"])

        assert result.exit_code == 5
        assert "no tables were profiled" not in result.stderr
        assert (project / "prints" / "primary" / "seedbank" / "vault" / "ddl.sql").is_file()

    def test_zero_matched_is_not_conflated_with_total_failure(self, project: Path) -> None:
        runner = CliRunner()

        with _registry(_Healthy):
            result = runner.invoke(main, ["generate", "--no-tui", "--include", "nope.*"])

        assert result.exit_code == 0
        assert "no tables matched selectors" in result.stderr
        assert "no tables were profiled" not in result.stderr


class TestFailFast:
    def test_stops_at_the_first_failure(self, project: Path) -> None:
        runner = CliRunner()

        with _registry(_AllDdlFail):
            result = runner.invoke(main, ["generate", "--no-tui", "--fail-fast"])

        assert result.exit_code == 7
        assert "2 matched table(s) not attempted" in result.stderr
        assert result.stderr.count("1 table failed") == 1

    def test_without_the_flag_every_table_is_attempted(self, project: Path) -> None:
        runner = CliRunner()

        with _registry(_AllDdlFail):
            result = runner.invoke(main, ["generate", "--no-tui"])

        assert "not attempted" not in result.stderr
        assert "3 tables failed" in result.stderr

    def test_abort_leaves_the_previous_manifest_untouched(self, project: Path) -> None:
        runner = CliRunner()

        with _registry(_Healthy):
            runner.invoke(main, ["generate", "--no-tui"])

        manifest = project / "prints" / "primary" / "manifest.yaml"
        before = manifest.read_text()

        with _registry(_AllDdlFail):
            runner.invoke(main, ["generate", "--no-tui", "--fail-fast", "--force"])

        assert manifest.read_text() == before


class TestTopLevelHandler:
    def test_uncaught_exception_friendly_one_line(self, project: Path) -> None:
        runner = CliRunner()

        with patch(
            "dbprint.cli.commands.list_cmd.resolve_project",
            side_effect=RuntimeError("kaboom"),
        ):
            result = runner.invoke(main, ["list"])

        assert result.exit_code == 1
        assert result.stderr.strip() == "error: kaboom"

    def test_debug_flag_reraises_traceback(self, project: Path) -> None:
        runner = CliRunner()

        with patch(
            "dbprint.cli.commands.list_cmd.resolve_project",
            side_effect=RuntimeError("kaboom"),
        ):
            result = runner.invoke(main, ["--debug", "list"])

        assert result.exit_code != 0
        assert isinstance(result.exception, RuntimeError)
        assert "kaboom" in str(result.exception)
