"""`dbprint diff`'s per-connection loop must flush held warnings on every early-exit branch."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Self
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from dbprint.adapters import ColumnMeta, ColumnStats, CommentsMeta, Inferred, MockAdapter, MockTable
from dbprint.cli.main import main
from dbprint.engine import ProgressEvent


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
                    cardinality=3,
                    cardinality_ratio=1.0,
                    cardinality_method="exact",
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
                ),
                "json_text": ColumnStats(
                    sql_type="text",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=3,
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
            samples={"probe_id": [1, 2, 3]},
            row_count=3,
        ),
    }


class _CleanAdapter(MockAdapter):
    REQUIRED_KEYS = ("host", "port", "database", "user", "password")

    def __init__(self, _credentials: dict[str, str]) -> None:
        super().__init__(_fixture())


class _ConnectFailsAdapter(MockAdapter):
    """`connect()` raises for every connection - standing in for EXIT_CONNECTION."""

    REQUIRED_KEYS = ("host", "port", "database", "user", "password")

    def __init__(self, _credentials: dict[str, str]) -> None:
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
                        "inferred": {"candidate_key": True, "looks_like": "numeric_string"},
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
        f"{prefix}_DATABASE": "d",
        f"{prefix}_USER": "u",
        f"{prefix}_PASSWORD": "p",
    }


class _RecordingRenderer:
    """Fake `ProgressRenderer` recording the call sequence `diff_command` makes, no TTY needed."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def on_event(self, event: ProgressEvent) -> None:
        self.calls.append(f"on_event:{event.connection}")

    def connection_summary(self, result: Any) -> None:
        self.calls.append(f"connection_summary:{result.connection_name}")

    def flush_warnings(self) -> None:
        self.calls.append("flush_warnings")

    def finish(self) -> None:
        self.calls.append("finish")

    def log_record(self, text: str) -> None:
        self.calls.append(f"log_record:{text}")


def _seed_project(tmp_path: Path) -> None:
    (tmp_path / ".dbprint.yaml").write_text(PROJECT_TWO_CONNECTIONS_YAML)


def _set_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in {**_credential_env("a"), **_credential_env("b")}.items():
        monkeypatch.setenv(k, v)


def _run_with_recorder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    adapter_class: type,
) -> _RecordingRenderer:
    monkeypatch.chdir(tmp_path)
    _set_credentials(monkeypatch)

    recorder = _RecordingRenderer()
    monkeypatch.setattr(
        "dbprint.cli.commands.diff.build_progress_renderer",
        lambda **kwargs: recorder,
    )

    with patch.dict(
        "dbprint.cli.adapter_registry.ADAPTERS",
        {"postgres": adapter_class},
        clear=True,
    ):
        CliRunner().invoke(main, ["diff", "--no-tui"])

    return recorder


class TestFlushWarningsCalledPerConnection:
    """`flush_warnings()` fires once per connection's iteration, on every exit path: "a" takes
    one of `diff.py`'s three early-exit branches per test, and whatever it held must flush
    before clean "b"'s events begin.
    """

    def test_missing_baseline_still_flushes_before_the_next_connection(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ "a" has no committed print - the early `continue` never even calls `_run_one`."""

        _seed_project(tmp_path)
        _seed_baseline(tmp_path, "b")
        recorder = _run_with_recorder(tmp_path, monkeypatch, _CleanAdapter)

        assert "flush_warnings" in recorder.calls
        first_b_event = next(i for i, c in enumerate(recorder.calls) if c.startswith("on_event:b"))
        first_flush = recorder.calls.index("flush_warnings")

        assert first_flush < first_b_event

    def test_connection_error_still_flushes_before_the_next_connection(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ "a" fails to connect (EXIT_CONNECTION) - the branch that skips `connection_summary`."""

        _seed_project(tmp_path)
        _seed_baseline(tmp_path, "a")
        _seed_baseline(tmp_path, "b")
        recorder = _run_with_recorder(tmp_path, monkeypatch, _ConnectFailsAdapter)

        assert "flush_warnings" in recorder.calls
        # No "b" event to anchor on (both fail); the check is that "a" flushed regardless.
        assert not any(c.startswith("connection_summary") for c in recorder.calls)

    def test_every_connection_gets_exactly_one_flush_on_a_clean_run(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Control: two clean connections still each get their own flush, not a shared one."""

        _seed_project(tmp_path)
        _seed_baseline(tmp_path, "a")
        _seed_baseline(tmp_path, "b")
        recorder = _run_with_recorder(tmp_path, monkeypatch, _CleanAdapter)

        assert recorder.calls.count("flush_warnings") == 2
        assert recorder.calls.count("connection_summary:a") == 1
        assert recorder.calls.count("connection_summary:b") == 1
        # Each connection's own flush follows its own summary, never the other's.
        a_summary = recorder.calls.index("connection_summary:a")
        b_summary = recorder.calls.index("connection_summary:b")
        flush_indices = [i for i, c in enumerate(recorder.calls) if c == "flush_warnings"]

        assert any(a_summary < i < b_summary for i in flush_indices)
