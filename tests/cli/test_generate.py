"""dbprint generate - end-to-end via patched adapter registry."""

from __future__ import annotations

from pathlib import Path
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

EXIT_DRIFT = 3


def _fixture_multi() -> dict[str, MockTable]:
    """Two tables in the two schemas the committed print spans, with invented table names."""

    return {
        "seedbank.viability_check": _table("seedbank", "viability_check"),
        "fixture.staging": _table("fixture", "staging"),
    }


def _table(schema: str, name: str) -> MockTable:
    return MockTable(
        type="table",
        namespace_path=(schema, name),
        ddl=f"CREATE TABLE {schema}.{name} (id uuid PRIMARY KEY);\n",
        columns=[
            ColumnMeta(name="id", sql_type="uuid", nullable=False, default=None, ordinal=1),
        ],
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


def _fixture() -> dict[str, MockTable]:
    return {
        "public.t": MockTable(
            type="table",
            namespace_path=("public", "t"),
            ddl="CREATE TABLE public.t (id uuid PRIMARY KEY);\n",
            columns=[
                ColumnMeta(name="id", sql_type="uuid", nullable=False, default=None, ordinal=1),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "id": ColumnStats(
                    sql_type="uuid",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=100,
                    cardinality_ratio=1.0,
                    cardinality_method="exact",
                    inferred=Inferred(candidate_key=True),
                ),
            },
            samples={"id": [f"00000000-0000-7000-8000-{i:012d}" for i in range(20)]},
            row_count=100,
        ),
    }


class _MockPostgresAdapter(MockAdapter):
    """MockAdapter with REQUIRED_KEYS to satisfy the CLI's credential-resolution path."""

    REQUIRED_KEYS = ("host", "port", "database", "user", "password")

    def __init__(self, _credentials: dict[str, str]) -> None:
        super().__init__(_fixture())


def _setup_project(tmp_path: Path) -> None:
    """Write a minimal .dbprint.yaml in tmp_path; preset credentials via env."""

    (tmp_path / ".dbprint.yaml").write_text(PROJECT_YAML)


def _patch_registry():
    return patch.dict(
        "dbprint.cli.adapter_registry.ADAPTERS",
        {"postgres": _MockPostgresAdapter},
        clear=True,
    )


def _credential_env() -> dict[str, str]:
    return {
        "DBPRINT_PRIMARY_HOST": "h",
        "DBPRINT_PRIMARY_PORT": "5432",
        "DBPRINT_PRIMARY_DATABASE": "d",
        "DBPRINT_PRIMARY_USER": "u",
        "DBPRINT_PRIMARY_PASSWORD": "p",
    }


class TestGenerateHappyPath:
    def test_first_run_creates_artifacts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup_project(tmp_path)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        runner = CliRunner()

        with _patch_registry():
            result = runner.invoke(main, ["generate", "--no-tui"])

        # A first run has no baseline, so every table registers as added and the run
        # reports schema drift.
        assert result.exit_code == EXIT_DRIFT, result.output
        manifest = yaml.safe_load((tmp_path / "prints" / "primary" / "manifest.yaml").read_text())
        assert "public.t" in manifest["tables"]

    def test_dry_run_writes_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_project(tmp_path)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        runner = CliRunner()

        with _patch_registry():
            runner.invoke(main, ["generate", "--no-tui", "--dry-run"])

        assert not (tmp_path / "prints" / "primary" / "manifest.yaml").exists()


class TestResolutionErrors:
    def test_no_dbprint_yaml_exits_nonzero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["generate", "--no-tui"])
        assert result.exit_code != 0

    def test_unknown_connection_exits_one(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()

        with _patch_registry():
            result = runner.invoke(main, ["generate", "missing", "--no-tui"])

        assert result.exit_code == 1


class TestPipedOutput:
    def test_piped_emits_per_table_lines(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup_project(tmp_path)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        runner = CliRunner()

        with _patch_registry():
            result = runner.invoke(main, ["generate", "--no-tui"])

        assert "primary" in result.output
        assert "public.t" in result.output
        assert "summary" in result.output


# Separate adapter/fixture for selector-narrowing tests, isolated from the single-table tests above.


class _MockMultiPostgresAdapter(MockAdapter):
    REQUIRED_KEYS = ("host", "port", "database", "user", "password")

    def __init__(self, _credentials: dict[str, str]) -> None:
        super().__init__(_fixture_multi())


def _patch_registry_multi():
    return patch.dict(
        "dbprint.cli.adapter_registry.ADAPTERS",
        {"postgres": _MockMultiPostgresAdapter},
        clear=True,
    )


def _setup_multi_project(tmp_path: Path, *, config_include: list[str] | None = None) -> None:
    include_yaml = ""

    if config_include:
        include_yaml = "    include:\n" + "\n".join(f'      - "{p}"' for p in config_include) + "\n"

    (tmp_path / ".dbprint.yaml").write_text(
        f"""\
defaults:
  max_age_days: 7
  statistics: {{}}
  diff: {{}}
connections:
  primary:
    adapter: postgres
    auto: true
    output: prints
{include_yaml}""",
    )


class TestCliSelectorNarrowing:
    """CLI --include intersects with config; CLI --exclude unions. Cannot widen scope."""

    def test_cli_include_narrows_from_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Config sees both schemas; CLI narrows to seedbank.* only.
        _setup_multi_project(tmp_path)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        runner = CliRunner()

        with _patch_registry_multi():
            result = runner.invoke(main, ["generate", "--no-tui", "--include", "seedbank.*"])

        assert "seedbank.viability_check" in result.output
        assert "fixture.staging" not in result.output

    def test_cli_include_cannot_widen_beyond_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Config restricts to seedbank.*; CLI tries to reach fixture.* - must NOT match.
        _setup_multi_project(tmp_path, config_include=["seedbank.*"])
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        runner = CliRunner()

        with _patch_registry_multi():
            result = runner.invoke(main, ["generate", "--no-tui", "--include", "fixture.*"])

        # Neither table runs: seedbank.* fails CLI include, fixture.* fails config include.
        assert "seedbank.viability_check" not in result.output.split("summary")[0]
        assert "fixture.staging" not in result.output.split("summary")[0]

    def test_cli_exclude_unions_with_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup_multi_project(tmp_path)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        runner = CliRunner()

        with _patch_registry_multi():
            result = runner.invoke(main, ["generate", "--no-tui", "--exclude", "fixture.*"])

        assert "seedbank.viability_check" in result.output
        assert "fixture.staging" not in result.output


# Run log. `run_log.LOGS_ROOT` is redirected to a session-scoped scratch dir by the
# autouse `_redirect_run_log` fixture in tests/conftest.py.


def _log_text(tmp_path: Path) -> str:
    """Read the sole log file `generate` wrote for the project rooted at `tmp_path`."""

    directory = run_log.LOGS_ROOT / run_log._slug(tmp_path)
    files = list(directory.glob("*.log"))
    assert len(files) == 1, f"expected exactly one log file, found {files}"

    return files[0].read_text()


class TestRunLog:
    def test_writes_header_connection_table_and_summary_records(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup_project(tmp_path)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        runner = CliRunner()

        with _patch_registry():
            runner.invoke(main, ["generate", "--no-tui"])

        text = _log_text(tmp_path)
        assert "run version=" in text
        assert "connection 'primary'" in text
        assert "table 'public.t'" in text
        assert "outcome=ok" in text
        assert "summary exit_code=" in text

    def test_per_table_record_names_the_rules_that_matched(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(
            "defaults:\n"
            "  max_age_days: 7\n"
            "  statistics: {}\n"
            "  diff: {}\n"
            "connections:\n"
            "  primary:\n"
            "    adapter: postgres\n"
            "    auto: true\n"
            "    output: prints\n"
            "    rules:\n"
            '      - include: ["public.*"]\n'
            "        max_age_days: 1\n",
        )
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        runner = CliRunner()

        with _patch_registry():
            runner.invoke(main, ["generate", "--no-tui"])

        assert "rules=connection 'primary' rules[0]" in _log_text(tmp_path)

    def test_offline_command_writes_no_log(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()

        with _patch_registry():
            runner.invoke(main, ["list"])

        assert not (run_log.LOGS_ROOT / run_log._slug(tmp_path)).exists()

    def test_connection_resolution_failure_writes_no_log(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()

        with _patch_registry():
            runner.invoke(main, ["generate", "missing", "--no-tui"])

        assert not (run_log.LOGS_ROOT / run_log._slug(tmp_path)).exists()

    def test_four_runs_keep_exactly_three_log_files(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup_project(tmp_path)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        runner = CliRunner()

        with _patch_registry():
            for _ in range(4):
                runner.invoke(main, ["generate", "--no-tui"])

        directory = run_log.LOGS_ROOT / run_log._slug(tmp_path)
        assert len(list(directory.glob("*.log"))) == 3

    def test_print_tree_carries_no_run_log_artifacts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _setup_project(tmp_path)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        runner = CliRunner()

        with _patch_registry():
            runner.invoke(main, ["generate", "--no-tui"])

        prints_root = tmp_path / "prints"
        assert not (prints_root / ".gitignore").exists()
        assert not (prints_root / "logs").exists()
        assert not any(p.name == "logs" for p in prints_root.rglob("*") if p.is_dir())

    def test_an_unopenable_sink_does_not_fail_the_run(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A run continues and reports its own exit code even when the sink itself cannot open."""

        _setup_project(tmp_path)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        blocked_root = tmp_path / "not-a-directory"
        blocked_root.write_text("occupied")
        runner = CliRunner()

        with _patch_registry(), patch.object(run_log, "LOGS_ROOT", blocked_root):
            result = runner.invoke(main, ["generate", "--no-tui"])

        assert result.exit_code == EXIT_DRIFT, result.output
        manifest = yaml.safe_load((tmp_path / "prints" / "primary" / "manifest.yaml").read_text())
        assert "public.t" in manifest["tables"]
        assert "warning: could not open run log" in (result.stderr or "")
