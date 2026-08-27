"""Error-surfacing matrix - every non-zero exit prints a cause to stderr.

Unreachable connection, bad driver, unknown adapter, zero-match selectors, malformed
manifest, uncaught exception: in each, stdout stays data-only and no password is echoed.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from dbprint.adapters import ColumnMeta, ColumnStats, CommentsMeta, Inferred, MockAdapter, MockTable
from dbprint.cli.main import main


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


def _table() -> MockTable:
    return MockTable(
        type="table",
        namespace_path=("public", "t"),
        ddl="CREATE TABLE public.t (id uuid PRIMARY KEY);\n",
        columns=[ColumnMeta(name="id", sql_type="uuid", nullable=False, default=None, ordinal=1)],
        relationships=[],
        indexes=[],
        comments=CommentsMeta(table=None, columns={}),
        stats={
            "id": ColumnStats(
                sql_type="uuid",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=10,
                cardinality_ratio=1.0,
                cardinality_method="exact",
                inferred=Inferred(candidate_key=True),
            ),
        },
        samples={"id": [f"00000000-0000-7000-8000-{i:012d}" for i in range(10)]},
        row_count=10,
    )


class _ConnectFails(MockAdapter):
    """Adapter that fails at connect() with a fixed message (no password)."""

    REQUIRED_KEYS = ("host", "port", "database", "user", "password")
    MESSAGE = "could not connect to Postgres at badhost:5432/db as 'u': connection refused"

    def __init__(self, _credentials: dict[str, str]) -> None:
        super().__init__({"public.t": _table()})

    def connect(self) -> None:
        raise RuntimeError(self.MESSAGE)


class _MissingExtra(_ConnectFails):
    MESSAGE = "the postgres adapter requires the [postgres] extra - pip install dbprint[postgres]"


class _Healthy(MockAdapter):
    REQUIRED_KEYS = ("host", "port", "database", "user", "password")

    def __init__(self, _credentials: dict[str, str]) -> None:
        super().__init__({"public.t": _table()})


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".dbprint.yaml").write_text(PROJECT_YAML)
    monkeypatch.chdir(tmp_path)

    for k, v in _CREDS.items():
        monkeypatch.setenv(k, v)

    return tmp_path


def _registry(adapter: type[MockAdapter]) -> AbstractContextManager[None]:
    return patch.dict("dbprint.cli.adapter_registry.ADAPTERS", {"postgres": adapter}, clear=True)


def _write_manifest(project_dir: Path, body: str) -> None:
    manifest = project_dir / "prints" / "primary" / "manifest.yaml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(body)


class TestGenerateErrors:
    def test_unreachable_connection_exits_4_with_cause(self, project: Path) -> None:
        runner = CliRunner()

        with _registry(_ConnectFails):
            result = runner.invoke(main, ["generate", "--no-tui"])

        assert result.exit_code == 4
        assert "connection refused" in result.stderr
        assert "primary" in result.stderr

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


class TestDiffErrors:
    def test_unreachable_connection_exits_4_with_cause(self, project: Path) -> None:
        _write_manifest(
            project,
            "format_version: 1\nadapter: postgres\ngenerated_at: 'x'\ntables: {}\n",
        )
        runner = CliRunner()

        with _registry(_ConnectFails):
            result = runner.invoke(main, ["diff", "--no-tui"])

        assert result.exit_code == 4
        assert "connection refused" in result.stderr
        # Connection failure must NOT emit an empty diff doc to stdout.
        assert result.stdout.strip() == ""

    def test_missing_baseline_message_preserved(self, project: Path) -> None:
        runner = CliRunner()

        with _registry(_Healthy):
            result = runner.invoke(main, ["diff", "--no-tui"])

        assert result.exit_code == 1
        assert "No committed prints" in result.stderr


class TestListErrors:
    def test_malformed_manifest_reports_parse_error(self, project: Path) -> None:
        _write_manifest(project, "tables: [unterminated\n")
        runner = CliRunner()

        result = runner.invoke(main, ["list", "--no-tui"])

        assert result.exit_code == 1
        assert "could not parse" in result.stderr
        assert "no manifest" not in result.stderr

    def test_absent_manifest_reports_no_manifest(self, project: Path) -> None:
        runner = CliRunner()

        result = runner.invoke(main, ["list", "--no-tui"])

        assert result.exit_code == 1
        assert "no manifest" in result.stderr


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
                cardinality=8,
                cardinality_ratio=0.166667,
                cardinality_method="exact",
            ),
            "shelf_code": ColumnStats(
                sql_type="character varying(8)",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=6,
                cardinality_ratio=0.125,
                cardinality_method="exact",
            ),
            "site_name": ColumnStats(
                sql_type="character varying(80)",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=8,
                cardinality_ratio=0.166667,
                cardinality_method="exact",
            ),
            "target_temperature_c": ColumnStats(
                sql_type="numeric(4,1)",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=3,
                cardinality_ratio=0.0625,
                cardinality_method="exact",
            ),
            "opens_at": ColumnStats(
                sql_type="time without time zone",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=2,
                cardinality_ratio=0.041667,
                cardinality_method="exact",
            ),
            "closes_at": ColumnStats(
                sql_type="time without time zone",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=2,
                cardinality_ratio=0.041667,
                cardinality_method="exact",
            ),
        },
        samples={},
        row_count=48,
    )


def _shape_probe_table() -> MockTable:
    """`fixture.shape_probe` - the print's real 5-column format-coverage table."""

    return MockTable(
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
            ColumnMeta(name="json_text", sql_type="text", nullable=False, default=None, ordinal=3),
            ColumnMeta(
                name="payload_bytes",
                sql_type="bytea",
                nullable=True,
                default=None,
                ordinal=4,
            ),
            ColumnMeta(name="tag_list", sql_type="text[]", nullable=False, default=None, ordinal=5),
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
                cardinality=50,
                cardinality_ratio=1.0,
                cardinality_method="exact",
                inferred=Inferred(candidate_key=True),
            ),
            "logger_ipv4": ColumnStats(
                sql_type="character varying(45)",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=10,
                cardinality_ratio=0.2,
                cardinality_method="exact",
            ),
            "json_text": ColumnStats(
                sql_type="text",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=50,
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
        samples={},
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

    def __init__(self, _credentials: dict[str, str]) -> None:
        super().__init__(_three_real_tables())

    def extract_ddl(self, fqn: str) -> str:
        raise TypeError("not all arguments converted during string formatting")


class _MixedFail(MockAdapter):
    """Two tables failing for two different reasons."""

    REQUIRED_KEYS = ("host", "port", "database", "user", "password")

    def __init__(self, _credentials: dict[str, str]) -> None:
        super().__init__(_two_real_tables())

    def extract_ddl(self, fqn: str) -> str:
        if fqn == "seedbank.storage_reading":
            raise TypeError("first cause")

        raise ValueError("second cause")


class _SameMessageDifferentOps(MockAdapter):
    """Two tables failing with an identical message from two different calls."""

    REQUIRED_KEYS = ("host", "port", "database", "user", "password")
    MESSAGE = "not all arguments converted during string formatting"

    def __init__(self, _credentials: dict[str, str]) -> None:
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

        assert "3 tables failed" in result.stderr
        assert result.stderr.count("not all arguments converted") == 1

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


class _OneOfThreeFails(MockAdapter):
    """One table fails; the other two produce prints."""

    REQUIRED_KEYS = ("host", "port", "database", "user", "password")

    def __init__(self, _credentials: dict[str, str]) -> None:
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
