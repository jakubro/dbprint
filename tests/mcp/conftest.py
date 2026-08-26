"""Shared fixtures for MCP tests - on-disk print directory, plus a real-stdio-transport fixture.

`StdioServer` drives the real subprocess/stdio wire, scoped to one class in test_server.py
rather than the whole module: a subprocess per call is materially slower than in-process
dispatch.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from dbprint.config import ConnectionConfig
from dbprint.config.project import DiffConfig, StatisticsConfig


@pytest.fixture
def primary_conn(committed_print: Path) -> ConnectionConfig:
    """A ConnectionConfig pointing at a fresh copy of the print the package ships.

    Keeps `committed_print`'s own connection name, `production`, so `conn.output / conn.name`
    lands on exactly the tree it wrote.
    """

    return ConnectionConfig(
        name="production",
        adapter="postgres",
        output=committed_print,
        statistics=StatisticsConfig(),
        diff=DiffConfig(),
    )


@pytest.fixture
def scoped_conn(tmp_path: Path) -> ConnectionConfig:
    """A ConnectionConfig over a hand-built print, the one carrying a `scope` block.

    A partial/sampled read (SPEC 2.2.8) is a state the committed print never reaches, so the
    tests asserting on `scope`/`rows_scanned` stay on this invented shape.
    """

    _seed_print(tmp_path)

    return ConnectionConfig(
        name="primary",
        adapter="postgres",
        output=tmp_path / "prints",
        statistics=StatisticsConfig(),
        diff=DiffConfig(),
    )


@dataclass(frozen=True)
class StdioServer:
    """Where to spawn a real `dbprint serve` subprocess, and which connection to name."""

    project_dir: Path
    conn_name: str
    command: str


@pytest.fixture
def stdio_server(primary_conn: ConnectionConfig, tmp_path: Path) -> StdioServer:
    """A real `dbprint serve` subprocess target over the seeded print `primary_conn` writes.

    `dbprint serve` is read-only, so one on-disk print serves both the in-process and the
    real-transport tests; only the client differs.
    """

    (tmp_path / ".dbprint.yaml").write_text(
        f"connections:\n"
        f"  {primary_conn.name}:\n"
        f"    adapter: {primary_conn.adapter}\n"
        f"    auto: true\n"
        f"    output: prints\n",
    )

    return StdioServer(
        project_dir=tmp_path,
        conn_name=primary_conn.name,
        command=str(Path(sys.executable).parent / "dbprint"),
    )


def _seed_print(tmp_path: Path) -> None:
    """Write a minimal valid print under tmp_path/prints/primary/."""

    prints = tmp_path / "prints" / "primary"
    table_dir = prints / "public" / "curator"
    table_dir.mkdir(parents=True)

    when = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    manifest = {
        "format_version": 1,
        "generated_at": when,
        "connection": "primary",
        "adapter": "postgres",
        "dbprint_version": "0.1.0",
        "tables": {
            "public.curator": {
                "type": "table",
                "path": "public/curator",
                "artifacts": {
                    "ddl": "ddl.sql",
                    "statistics": "statistics.yaml",
                    "relationships": "relationships.yaml",
                },
                "row_count": 5,
                "columns": 2,
                "profiled_at": when,
            },
        },
    }
    statistics: dict[str, Any] = {
        "format_version": 1,
        "table": "public.curator",
        "type": "table",
        "profiled_at": when,
        "row_count": 5,
        "row_count_method": "exact",
        "columns": {
            "id": {
                "sql_type": "uuid",
                "nullable": False,
                "null_count": 0,
                "null_rate": 0.0,
                "cardinality": 5,
                "cardinality_ratio": 1.0,
                "cardinality_method": "exact",
                "classification": "categorical",
                "inferred": {"candidate_key": True, "looks_like": "uuid"},
            },
            "email": {
                "sql_type": "varchar",
                "nullable": False,
                "null_count": 0,
                "null_rate": 0.0,
                "cardinality": 5,
                "cardinality_ratio": 1.0,
                "cardinality_method": "exact",
                "classification": "text",
            },
        },
    }
    relationships = {
        "format_version": 1,
        "table": "public.curator",
        "profiled_at": when,
        "refers_to": [],
        "referenced_by": [],
    }
    diff = {
        "format_version": 1,
        "generated_at": when,
        "connection": "primary",
        "adapter": "postgres",
        "baseline": {"source": "committed_prints", "path": str(prints)},
        "target": {
            "source": "live_database",
            "scanned_at": when,
            "selectors": {"include": ["*"], "exclude": []},
            "tables_scanned": 1,
        },
        "summary": {
            "tables_added": 0,
            "tables_removed": 0,
            "tables_modified": 0,
            "columns_added": 0,
            "columns_removed": 0,
            "columns_type_changed": 0,
            "columns_nullable_changed": 0,
            "columns_default_changed": 0,
            "statistics_drifted": 0,
            "relationships_changed": 0,
            "indexes_changed": 0,
            "comments_changed": 0,
            "unchanged_tables": 1,
            "unevaluated_tables": 0,
        },
        "changes": [],
    }

    (prints / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    (prints / "diff.yaml").write_text(yaml.safe_dump(diff))
    (prints / "reading.md").write_text("# Reading a dbprint print\n\nGenerated by dbprint.\n")
    (table_dir / "ddl.sql").write_text("CREATE TABLE public.curator (id uuid, email varchar);\n")
    (table_dir / "statistics.yaml").write_text(yaml.safe_dump(statistics))
    (table_dir / "relationships.yaml").write_text(yaml.safe_dump(relationships))
