"""`dbprint diff`'s per-connection loop must flush held warnings on every early-exit branch."""

from __future__ import annotations

import logging
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner
from rich.console import Console

from dbprint.adapters import Inferred, MockAdapter, MockTable
from dbprint.cli.main import main
from dbprint.cli.rendering.progress import LiveProgressRenderer
from tests._prints import SHAPE_PROBE_COLUMNS, exact_stats, mock_table, unmeasured_stats


PROJECT_TWO_CONNECTIONS_YAML = """\
defaults:
  max_age_days: 7
  statistics: {}
  diff: {}
connections:
  a:
    adapter: postgres
    auto: true
    output: prints
  b:
    adapter: postgres
    auto: true
    output: prints
"""


def _fixture() -> dict[str, MockTable]:
    """`fixture.shape_probe` - the print's real 5-column table; only `probe_id` matters."""

    return {
        "fixture.shape_probe": mock_table(
            "fixture.shape_probe",
            SHAPE_PROBE_COLUMNS,
            {
                "probe_id": exact_stats("integer", 3, 1.0, inferred=Inferred(candidate_key=True)),
                "logger_ipv4": exact_stats("character varying(45)", 1, 0.333333),
                "json_text": exact_stats("text", 3, 1.0),
                "payload_bytes": unmeasured_stats("bytea", nullable=True),
                "tag_list": unmeasured_stats("text[]"),
            },
            primary_key=("probe_id",),
            samples={"probe_id": [1, 2, 3]},
            row_count=3,
        ),
    }


class _CleanAdapter(MockAdapter):
    REQUIRED_KEYS = ("host", "port", "database", "user", "password")

    def __init__(self, _credentials: dict[str, str], **_options: object) -> None:
        super().__init__(_fixture())


class _ConnectFailsAdapter(MockAdapter):
    """`connect()` raises for every connection - standing in for EXIT_CONNECTION."""

    REQUIRED_KEYS = ("host", "port", "database", "user", "password")

    def __init__(self, _credentials: dict[str, str], **_options: object) -> None:
        super().__init__(_fixture())

    def connect(self) -> None:
        raise RuntimeError("could not connect to host")


def _seed_baseline(tmp_path: Path, connection: str) -> None:
    """A committed `fixture.shape_probe` baseline - the print's real 5-column table."""

    prints = tmp_path / "prints" / connection
    table_dir = prints / "fixture" / "shape_probe"
    table_dir.mkdir(parents=True)
    when = "2026-06-08T00:00:00Z"
    manifest = {
        "format_version": 1,
        "generated_at": when,
        "connection": connection,
        "adapter": "postgres",
        "dbprint_version": "0.1.0",
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
    (table_dir / "statistics.yaml").write_text(
        yaml.safe_dump(
            {
                "format_version": 1,
                "table": "fixture.shape_probe",
                "type": "table",
                "profiled_at": when,
                "row_count": 3,
                "row_count_method": "exact",
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
                        # An integer column withholds `numeric_string` (SPEC 4.1.5).
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
            },
        ),
    )


def _credential_env(name: str) -> dict[str, str]:
    prefix = f"DBPRINT_{name.upper()}"

    return {
        f"{prefix}_HOST": "h",
        f"{prefix}_PORT": "5432",
        f"{prefix}_DATABASE": f"db_{name}",
        f"{prefix}_USER": "u",
        f"{prefix}_PASSWORD": "p",
    }


def _seed_project(tmp_path: Path) -> None:
    (tmp_path / ".dbprint.yaml").write_text(PROJECT_TWO_CONNECTIONS_YAML)


def _set_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in {**_credential_env("a"), **_credential_env("b")}.items():
        monkeypatch.setenv(k, v)


# The start of connection b's summary line, which follows everything b itself printed.
_B_SUMMARY = "b  -  "


def _run_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adapter_class: type) -> str:
    """Run `diff` over both connections with the real live renderer; return what it printed."""

    monkeypatch.chdir(tmp_path)
    _set_credentials(monkeypatch)
    buf = StringIO()
    console = Console(file=buf, force_terminal=True, width=120, color_system=None)
    monkeypatch.setattr(
        "dbprint.cli.commands.diff.build_progress_renderer",
        lambda **kwargs: LiveProgressRenderer(console),
    )

    with patch.dict(
        "dbprint.cli.adapter_registry.ADAPTERS",
        {"postgres": adapter_class},
        clear=True,
    ):
        CliRunner().invoke(main, ["diff", "--no-tui"])

    return buf.getvalue()


class _WarnsOnConnect(_CleanAdapter):
    """Logs one warning naming its own database before any table is in flight."""

    def __init__(self, credentials: dict[str, str], **options: object) -> None:
        super().__init__(credentials, **options)
        self._database = credentials["database"]

    def connect(self) -> None:
        logging.getLogger("dbprint.adapters.mock").warning("held for %s", self._database)
        super().connect()


class _WarnsThenFailsToConnect(_WarnsOnConnect):
    def connect(self) -> None:
        logging.getLogger("dbprint.adapters.mock").warning("held for %s", self._database)

        raise RuntimeError("could not connect to host")


class TestFlushWarningsCalledPerConnection:
    """A warning held while one connection ran prints before the next connection's first line,
    on every exit path: a missing baseline, a failed connect, and a clean comparison.
    """

    def test_missing_baseline_still_flushes_before_the_next_connection(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ "a" has no committed print - the early `continue` never even calls `_run_one`."""

        from dbprint.cli.commands import diff as diff_module

        _seed_project(tmp_path)
        _seed_baseline(tmp_path, "b")
        real = diff_module._baseline_present

        def warn_then_check(conn_config: Any) -> bool:
            logging.getLogger("dbprint.cli").warning("held for %s", conn_config.name)

            return real(conn_config)

        monkeypatch.setattr(diff_module, "_baseline_present", warn_then_check)
        out = _run_live(tmp_path, monkeypatch, _CleanAdapter)

        assert out.index("held for a") < out.index(_B_SUMMARY)
        assert out.count("held for a") == 1

    def test_connection_error_still_flushes_before_the_next_connection(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_project(tmp_path)
        _seed_baseline(tmp_path, "a")
        _seed_baseline(tmp_path, "b")

        out = _run_live(tmp_path, monkeypatch, _WarnsThenFailsToConnect)

        assert out.index("held for db_a") < out.index(_B_SUMMARY)
        assert out.count("held for db_a") == 1

    def test_a_clean_connection_flushes_its_own_warning_before_the_next(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_project(tmp_path)
        _seed_baseline(tmp_path, "a")
        _seed_baseline(tmp_path, "b")

        out = _run_live(tmp_path, monkeypatch, _WarnsOnConnect)

        assert out.index("held for db_a") < out.index(_B_SUMMARY)
        assert out.count("held for db_a") == 1
        assert out.count("held for db_b") == 1
