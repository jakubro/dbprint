"""Shared fixtures: on-disk prints covering the v0.2 field surface the docs site renders.

The reference example carries no `scope`, per-column `rows_scanned`,
`values_coverage_method`, or empty `columns` map, so these cover them directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from dbprint.config import ConnectionConfig
from dbprint.config.project import DiffConfig, StatisticsConfig


WHEN = datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _connection(tmp_path: Path, name: str = "primary") -> ConnectionConfig:
    return ConnectionConfig(
        name=name,
        adapter="postgres",
        output=tmp_path / "prints",
        statistics=StatisticsConfig(),
        diff=DiffConfig(),
    )


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


@pytest.fixture
def rich_conn(tmp_path: Path) -> ConnectionConfig:
    """A two-table print exercising grain, null_patterns, physical_layout, dependencies,
    sketch, observed relationships, redaction, and every human annotation kind."""

    root = tmp_path / "prints" / "primary"

    manifest = {
        "format_version": 1,
        "generated_at": WHEN,
        "connection": "primary",
        "adapter": "postgres",
        "dbprint_version": "0.2.0",
        "statistics_params": {
            "enumeration_threshold": 50,
            "top_n_values": 30,
            "top_n_null_patterns": 20,
            "looks_like_sample_size": 1000,
            "percentiles": [1, 25, 50, 75, 99],
        },
        "selectors": {"include": ["seedbank.*"], "exclude": []},
        "redaction_rules_configured": 1,
        "default_collation": "en_US.UTF-8",
        "manifest_annotations": "manifest.annotations.yaml",
        "tables": {
            "seedbank.batch": {
                "type": "table",
                "path": "seedbank/batch",
                "artifacts": {
                    "ddl": "ddl.sql",
                    "statistics": "statistics.yaml",
                    "relationships": "relationships.yaml",
                    "description": "description.md",
                    "statistics_annotations": "statistics.annotations.yaml",
                    "relationships_annotations": "relationships.annotations.yaml",
                },
                "columns": 4,
                "profiled_at": WHEN,
                "row_count": 300,
                "max_age_days": 7,
            },
            "seedbank.cultivar": {
                "type": "table",
                "path": "seedbank/cultivar",
                "artifacts": {
                    "ddl": "ddl.sql",
                    "statistics": "statistics.yaml",
                    "relationships": "relationships.yaml",
                },
                "columns": 2,
                "profiled_at": WHEN,
                "row_count": 40,
            },
        },
    }

    batch_statistics: dict[str, Any] = {
        "format_version": 1,
        "table": "seedbank.batch",
        "type": "table",
        "profiled_at": WHEN,
        "row_count": 300,
        "row_count_method": "exact",
        "null_patterns": {
            "coverage": 1.0,
            "patterns": [
                {"columns": [], "count": 280},
                {"columns": ["notes"], "count": 20},
            ],
        },
        "physical_layout": {
            "mechanism": "cluster",
            "keys": [{"expression": "cultivar_id", "column": "cultivar_id"}],
        },
        "grain": {
            "keys": [{"columns": ["batch_id"], "detection": "declared"}],
        },
        "dependencies": [
            {"determinant": "cultivar_id", "dependent": "cultivar_name", "strength": 1.0},
        ],
        "columns": {
            "batch_id": {
                "sql_type": "bigint",
                "nullable": False,
                "null_count": 0,
                "null_rate": 0.0,
                "cardinality": 300,
                "cardinality_ratio": 1.0,
                "cardinality_method": "exact",
                "classification": "numeric",
                "inferred": {"candidate_key": True},
                "distribution": "long_tail",
                "frequencies": {"top": 1, "bottom": 1, "listed": 30, "total": 30},
                "range": {"min": 1, "max": 300},
                "percentiles": {"p01": 3, "p25": 75, "p50": 150, "p75": 225, "p99": 297},
            },
            "cultivar_id": {
                "sql_type": "integer",
                "nullable": False,
                "null_count": 0,
                "null_rate": 0.0,
                "cardinality": 40,
                "cardinality_ratio": 0.1333,
                "cardinality_method": "exact",
                "classification": "foreign_key_candidate",
                # No `inferred`: an integer column withholds `numeric_string` (SPEC 4.1.5),
                # and nothing else detects here.
                "values": [{"value": 1, "count": 8}, {"value": 2, "count": 7}],
                "values_coverage": 0.05,
                "distribution": "long_tail",
                "physical_layout_key": True,
                "sketch": {"method": "kmv_md5_lo64", "values": "AAAA"},
            },
            "cultivar_name": {
                "sql_type": "character varying(64)",
                "nullable": False,
                "null_count": 0,
                "null_rate": 0.0,
                "cardinality": 40,
                "cardinality_ratio": 0.1333,
                "cardinality_method": "exact",
                "classification": "text",
                "inferred": {"sensitivity": "personal_name"},
                "values": [{"value": "Quercus alba", "count": 8}],
                "values_coverage": 1.0,
                "distribution": "uniform",
            },
            "notes": {
                "sql_type": "text",
                "nullable": True,
                "null_count": 20,
                "null_rate": 0.0667,
                "cardinality": 250,
                "cardinality_ratio": 0.8333,
                "cardinality_method": "exact",
                "classification": "text",
                "inferred": {"looks_like": "prose"},
            },
        },
    }

    batch_relationships = {
        "format_version": 1,
        "table": "seedbank.batch",
        "profiled_at": WHEN,
        "eligible_target": True,
        "refers_to": [
            {
                "column": ["cultivar_id"],
                "target_table": "seedbank.cultivar",
                "target_column": ["cultivar_id"],
                "detection": "declared",
                "on_delete": "RESTRICT",
                "on_update": "NO ACTION",
                "constraint_name": "batch_cultivar_id_fkey",
                "observed": {
                    "fanout_avg": 7.5,
                    "fanout_max": 9,
                    "target_coverage": 1.0,
                    "containment": 1.0,
                    "coherent": True,
                    "scope_compatible": True,
                },
            },
        ],
        "referenced_by": [
            {
                "column": ["batch_id"],
                "referencer_table": "seedbank.sowing_trial",
                "referencer_column": ["batch_id"],
                "detection": "inferred",
            },
        ],
    }

    cultivar_statistics: dict[str, Any] = {
        "format_version": 1,
        "table": "seedbank.cultivar",
        "type": "table",
        "profiled_at": WHEN,
        "row_count": 40,
        "row_count_method": "exact",
        "grain": {"keys": [{"columns": ["cultivar_id"], "detection": "declared"}]},
        "columns": {
            "cultivar_id": {
                "sql_type": "integer",
                "nullable": False,
                "null_count": 0,
                "null_rate": 0.0,
                "cardinality": 40,
                "cardinality_ratio": 1.0,
                "cardinality_method": "exact",
                "classification": "numeric",
                "inferred": {"candidate_key": True},
                "distribution": "long_tail",
                "frequencies": {"top": 1, "bottom": 1, "listed": 30, "total": 30},
                "range": {"min": 1, "max": 40},
                "percentiles": {"p01": 1, "p25": 10, "p50": 20, "p75": 30, "p99": 40},
                "sketch": {"method": "kmv_md5_lo64", "values": "BBBB"},
            },
            "cultivar_name": {
                "sql_type": "character varying(64)",
                "nullable": False,
                "null_count": 0,
                "null_rate": 0.0,
                "cardinality": 40,
                "cardinality_ratio": 1.0,
                "cardinality_method": "exact",
                "classification": "text",
                "values": [{"value": "Quercus alba", "count": 1}],
                "values_coverage": 1.0,
                "distribution": "uniform",
            },
        },
    }

    cultivar_relationships = {
        "format_version": 1,
        "table": "seedbank.cultivar",
        "profiled_at": WHEN,
        "eligible_target": True,
        "refers_to": [],
        "referenced_by": [
            {
                "column": ["cultivar_id"],
                "referencer_table": "seedbank.batch",
                "referencer_column": ["cultivar_id"],
                "detection": "declared",
                "on_delete": "RESTRICT",
                "on_update": "NO ACTION",
            },
        ],
    }

    batch_annotations = {
        "format_version": 1,
        "columns": {
            "cultivar_id": {
                "note": "FK to cultivar.cultivar_id.",
                "claims": {"cardinality_ratio": "> 0.1"},
                "values": [{"value": 1, "note": "the type specimen"}],
            },
            "stale_column_name": {"note": "no longer exists - must be filtered out"},
        },
    }

    batch_rel_annotations = {
        "format_version": 1,
        "refers_to": [
            {
                "column": ["cultivar_id"],
                "target_table": "seedbank.nonexistent",
                "target_column": ["cultivar_id"],
                "verdict": "rejected",
                "note": "name coincidence, no real relationship",
            },
        ],
    }

    manifest_annotations = {
        "format_version": 1,
        "notes": "This print is regenerated on demand, not on a schedule.",
    }

    _write(root / "manifest.yaml", manifest)
    _write(root / "manifest.annotations.yaml", manifest_annotations)
    _write(root / "seedbank" / "batch" / "statistics.yaml", batch_statistics)
    _write(root / "seedbank" / "batch" / "relationships.yaml", batch_relationships)
    _write(
        root / "seedbank" / "batch" / "statistics.annotations.yaml",
        batch_annotations,
    )
    _write(
        root / "seedbank" / "batch" / "relationships.annotations.yaml",
        batch_rel_annotations,
    )
    (root / "seedbank" / "batch" / "ddl.sql").write_text(
        "CREATE TABLE seedbank.batch (batch_id bigint, cultivar_id integer);\n",
    )
    (root / "seedbank" / "batch" / "description.md").write_text(
        "# batch\n\nOne row per seed batch. See `cultivar` for species.\n",
    )
    _write(root / "seedbank" / "cultivar" / "statistics.yaml", cultivar_statistics)
    _write(root / "seedbank" / "cultivar" / "relationships.yaml", cultivar_relationships)
    (root / "seedbank" / "cultivar" / "ddl.sql").write_text(
        "CREATE TABLE seedbank.cultivar (cultivar_id integer, cultivar_name varchar);\n",
    )

    return _connection(tmp_path)


@pytest.fixture
def second_conn(tmp_path: Path) -> ConnectionConfig:
    """A second, independent connection's print - `secondary`, sharing `rich_conn`'s tmp_path.

    Proves a multi-connection render (index page, `docs build`) covers every connection
    passed to `create_app`, not only the first.
    """

    root = tmp_path / "prints" / "secondary"

    manifest = {
        "format_version": 1,
        "generated_at": WHEN,
        "connection": "secondary",
        "adapter": "postgres",
        "dbprint_version": "0.2.0",
        "tables": {
            "public.germination_reading": {
                "type": "table",
                "path": "public/germination_reading",
                "artifacts": {"ddl": "ddl.sql", "statistics": "statistics.yaml"},
                "columns": 1,
                "profiled_at": WHEN,
                "row_count": 10,
            },
        },
    }
    statistics = {
        "format_version": 1,
        "table": "public.germination_reading",
        "type": "table",
        "profiled_at": WHEN,
        "row_count": 10,
        "row_count_method": "exact",
        "grain": {"keys": [{"columns": ["reading_id"], "detection": "declared"}]},
        "columns": {
            "reading_id": {
                "sql_type": "integer",
                "nullable": False,
                "null_count": 0,
                "null_rate": 0.0,
                "cardinality": 10,
                "cardinality_ratio": 1.0,
                "cardinality_method": "exact",
                "classification": "numeric",
                "inferred": {"candidate_key": True},
                "distribution": "long_tail",
                "frequencies": {"top": 1, "bottom": 1, "listed": 10, "total": 10},
                "range": {"min": 1, "max": 10},
                "percentiles": {"p01": 1, "p25": 3, "p50": 5, "p75": 8, "p99": 10},
            },
        },
    }

    _write(root / "manifest.yaml", manifest)
    _write(root / "public" / "germination_reading" / "statistics.yaml", statistics)
    (root / "public" / "germination_reading" / "ddl.sql").write_text(
        "CREATE TABLE public.germination_reading (reading_id integer);\n",
    )

    return _connection(tmp_path, name="secondary")


@pytest.fixture
def scoped_conn(tmp_path: Path) -> ConnectionConfig:
    """One table read under a `scope` block - every ratio is scanned-set-relative."""

    root = tmp_path / "prints" / "primary"

    manifest = {
        "format_version": 1,
        "generated_at": WHEN,
        "connection": "primary",
        "adapter": "postgres",
        "dbprint_version": "0.2.0",
        "tables": {
            "seedbank.curation_event": {
                "type": "table",
                "path": "seedbank/curation_event",
                "artifacts": {"ddl": "ddl.sql", "statistics": "statistics.yaml"},
                "columns": 2,
                "profiled_at": WHEN,
                "row_count": 1_000_000,
            },
        },
    }
    statistics = {
        "format_version": 1,
        "table": "seedbank.curation_event",
        "type": "table",
        "profiled_at": WHEN,
        "row_count": 1_000_000,
        "row_count_method": "approximate",
        "grain": {"keys": []},
        "scope": {"rows_scanned": 10_000, "sample": 0.01},
        "columns": {
            "curation_event_id": {
                "sql_type": "bigint",
                "nullable": False,
                "null_count": 0,
                "null_rate": 0.0,
                "cardinality": 10_000,
                "cardinality_ratio": 1.0,
                "cardinality_method": "exact",
                "classification": "numeric",
                "rows_scanned": 10_000,
                "distribution": "long_tail",
                "frequencies": {"top": 1, "bottom": 1, "listed": 30, "total": 30},
                "range": {"min": 1, "max": 10_000},
                "percentiles": {"p01": 100, "p25": 2500, "p50": 5000, "p75": 7500, "p99": 9900},
            },
            # Exhaustive over the sampled 1% only, never over the whole table.
            "action_type": {
                "sql_type": "character varying(32)",
                "nullable": False,
                "null_count": 0,
                "null_rate": 0.0,
                "cardinality": 3,
                "cardinality_ratio": 0.0003,
                "cardinality_method": "exact",
                "classification": "categorical",
                "rows_scanned": 10_000,
                "physical_name": "actionType",
                "collation": "en_US.UTF-8",
                "values": [
                    {"value": "watered", "count": 6000},
                    {"value": "inspected", "count": 3000},
                    {"value": "treated", "count": 1000},
                ],
                "values_coverage": 1.0,
                "values_coverage_method": "measured",
                "distribution": "imbalanced",
            },
        },
    }

    _write(root / "manifest.yaml", manifest)
    _write(root / "seedbank" / "curation_event" / "statistics.yaml", statistics)
    (root / "seedbank" / "curation_event" / "ddl.sql").write_text(
        "CREATE TABLE seedbank.curation_event (curation_event_id bigint);\n",
    )

    return _connection(tmp_path)


@pytest.fixture
def redacted_conn(tmp_path: Path) -> ConnectionConfig:
    """One table with a `redacted: mask` temporal column - bounds must never be plotted."""

    root = tmp_path / "prints" / "primary"

    manifest = {
        "format_version": 1,
        "generated_at": WHEN,
        "connection": "primary",
        "adapter": "postgres",
        "dbprint_version": "0.2.0",
        "tables": {
            "seedbank.curator_profile": {
                "type": "table",
                "path": "seedbank/curator_profile",
                "artifacts": {"ddl": "ddl.sql", "statistics": "statistics.yaml"},
                "columns": 1,
                "profiled_at": WHEN,
                "row_count": 500,
            },
        },
    }
    statistics = {
        "format_version": 1,
        "table": "seedbank.curator_profile",
        "type": "table",
        "profiled_at": WHEN,
        "row_count": 500,
        "row_count_method": "exact",
        "grain": {"keys": []},
        "columns": {
            "born_on": {
                "sql_type": "date",
                "nullable": False,
                "null_count": 0,
                "null_rate": 0.0,
                "cardinality": 480,
                "cardinality_ratio": 0.96,
                "cardinality_method": "exact",
                "classification": "temporal",
                "redacted": "mask",
                "distribution": "uniform",
                "frequencies": {"top": 2, "bottom": 1, "listed": 30, "total": 60},
                "freshness": {"max_age_days": 90, "classification": "dormant"},
            },
        },
    }

    _write(root / "manifest.yaml", manifest)
    _write(root / "seedbank" / "curator_profile" / "statistics.yaml", statistics)
    (root / "seedbank" / "curator_profile" / "ddl.sql").write_text(
        "CREATE TABLE seedbank.curator_profile (born_on date);\n",
    )

    return _connection(tmp_path)


@pytest.fixture
def empty_columns_conn(tmp_path: Path) -> ConnectionConfig:
    """A scoped read that matched no rows - `columns: {}`, never an empty-table claim."""

    root = tmp_path / "prints" / "primary"

    manifest = {
        "format_version": 1,
        "generated_at": WHEN,
        "connection": "primary",
        "adapter": "postgres",
        "dbprint_version": "0.2.0",
        "tables": {
            "public.narrow": {
                "type": "table",
                "path": "public/narrow",
                "artifacts": {"ddl": "ddl.sql", "statistics": "statistics.yaml"},
                "columns": 0,
                "profiled_at": WHEN,
                "row_count": 500,
            },
        },
    }
    statistics = {
        "format_version": 1,
        "table": "public.narrow",
        "type": "table",
        "profiled_at": WHEN,
        "row_count": 500,
        "row_count_method": "exact",
        "scope": {"rows_scanned": 0, "filter": "rank = 'never-matches'"},
        "grain": {"keys": []},
        "columns": {},
    }

    _write(root / "manifest.yaml", manifest)
    _write(root / "public" / "narrow" / "statistics.yaml", statistics)
    (root / "public" / "narrow" / "ddl.sql").write_text(
        "CREATE TABLE public.narrow (rank text);\n",
    )

    return _connection(tmp_path)


@pytest.fixture
def catalog_only_conn(tmp_path: Path) -> ConnectionConfig:
    """A plain view's catalog-only print (SPEC 2.2.15) - columns, nothing measured."""

    root = tmp_path / "prints" / "primary"

    manifest = {
        "format_version": 1,
        "generated_at": WHEN,
        "connection": "primary",
        "adapter": "postgres",
        "dbprint_version": "0.2.0",
        "tables": {
            "public.active_curators_v": {
                "type": "view",
                "path": "public/active_curators_v",
                "artifacts": {"ddl": "ddl.sql", "statistics": "statistics.yaml"},
                "columns": 2,
                "profiled_at": WHEN,
            },
        },
    }
    statistics = {
        "format_version": 1,
        "table": "public.active_curators_v",
        "type": "view",
        "profiled_at": WHEN,
        "catalog_only": True,
        "grain": {"keys": []},
        "columns": {
            "id": {"sql_type": "uuid", "nullable": False, "classification": "text"},
            "name": {"sql_type": "varchar", "nullable": True, "classification": "categorical"},
        },
    }

    _write(root / "manifest.yaml", manifest)
    _write(root / "public" / "active_curators_v" / "statistics.yaml", statistics)
    (root / "public" / "active_curators_v" / "ddl.sql").write_text(
        "CREATE VIEW public.active_curators_v AS SELECT id, name FROM curator;\n",
    )

    return _connection(tmp_path)


@pytest.fixture
def grain_note_conn(tmp_path: Path) -> ConnectionConfig:
    """A table whose only grain key is human-authored, carrying a note (SPEC 2.7.1)."""

    root = tmp_path / "prints" / "primary"

    manifest = {
        "format_version": 1,
        "generated_at": WHEN,
        "connection": "primary",
        "adapter": "postgres",
        "dbprint_version": "0.2.0",
        "tables": {
            "seedbank.batch": {
                "type": "table",
                "path": "seedbank/batch",
                "artifacts": {
                    "ddl": "ddl.sql",
                    "statistics": "statistics.yaml",
                    "statistics_annotations": "statistics.annotations.yaml",
                },
                "row_count": 40,
                "columns": 1,
                "profiled_at": WHEN,
            },
        },
    }
    statistics = {
        "format_version": 1,
        "table": "seedbank.batch",
        "type": "table",
        "profiled_at": WHEN,
        "row_count": 40,
        "row_count_method": "exact",
        "grain": {"keys": []},
        "columns": {
            "shelf_location": {
                "sql_type": "varchar(8)",
                "nullable": False,
                "null_count": 0,
                "null_rate": 0.0,
                "cardinality": 40,
                "cardinality_ratio": 1.0,
                "cardinality_method": "exact",
                "classification": "text",
                "values": [{"value": f"S{i:03d}", "count": 1} for i in range(40)],
                "values_coverage": 1.0,
                "distribution": "uniform",
            },
        },
    }
    annotations = {
        "format_version": 1,
        "columns": {},
        "grain": {
            "keys": [
                {"columns": ["shelf_location"], "note": "unique in practice, never enforced"},
            ],
        },
    }

    _write(root / "manifest.yaml", manifest)
    _write(root / "seedbank" / "batch" / "statistics.yaml", statistics)
    _write(root / "seedbank" / "batch" / "statistics.annotations.yaml", annotations)
    (root / "seedbank" / "batch" / "ddl.sql").write_text(
        "CREATE TABLE seedbank.batch (shelf_location varchar(8) NOT NULL);\n",
    )

    return _connection(tmp_path)


@pytest.fixture
def companion_conn(tmp_path: Path) -> ConnectionConfig:
    """A table with a genuine multi-column `null_patterns` entry.

    `rich_conn`'s only pattern is single-column, so this covers two columns null on the same
    rows (SPEC 2.2.10); its description also covers linking a plural mention.
    """

    root = tmp_path / "prints" / "primary"

    manifest = {
        "format_version": 1,
        "generated_at": WHEN,
        "connection": "primary",
        "adapter": "postgres",
        "dbprint_version": "0.2.0",
        "tables": {
            "seedbank.botanist": {
                "type": "table",
                "path": "seedbank/botanist",
                "artifacts": {
                    "ddl": "ddl.sql",
                    "statistics": "statistics.yaml",
                    "description": "description.md",
                },
                "columns": 3,
                "profiled_at": WHEN,
                "row_count": 100,
            },
        },
    }
    statistics = {
        "format_version": 1,
        "table": "seedbank.botanist",
        "type": "table",
        "profiled_at": WHEN,
        "row_count": 100,
        "row_count_method": "exact",
        "grain": {"keys": [{"columns": ["botanist_id"], "detection": "declared"}]},
        "null_patterns": {
            "coverage": 1.0,
            "patterns": [
                {"columns": [], "count": 70},
                {"columns": ["email"], "count": 10},
                {"columns": ["phone"], "count": 5},
                {"columns": ["email", "phone"], "count": 15},
            ],
        },
        "columns": {
            "botanist_id": {
                "sql_type": "bigint",
                "nullable": False,
                "null_count": 0,
                "null_rate": 0.0,
                "cardinality": 100,
                "cardinality_ratio": 1.0,
                "cardinality_method": "exact",
                "classification": "numeric",
                "inferred": {"candidate_key": True},
                "distribution": "long_tail",
            },
            "email": {
                "sql_type": "text",
                "nullable": True,
                "null_count": 25,
                "null_rate": 0.25,
                "cardinality": 75,
                "cardinality_ratio": 0.75,
                "cardinality_method": "exact",
                "classification": "text",
                "inferred": {"looks_like": "email"},
            },
            "phone": {
                "sql_type": "text",
                "nullable": True,
                "null_count": 20,
                "null_rate": 0.2,
                "cardinality": 40,
                "cardinality_ratio": 0.4,
                "cardinality_method": "exact",
                "classification": "text",
            },
        },
    }

    _write(root / "manifest.yaml", manifest)
    _write(root / "seedbank" / "botanist" / "statistics.yaml", statistics)
    (root / "seedbank" / "botanist" / "ddl.sql").write_text(
        "CREATE TABLE seedbank.botanist (botanist_id bigint, email text, phone text);\n",
    )
    (root / "seedbank" / "botanist" / "description.md").write_text(
        "# botanist\n\nOne row per botanist, drawn from a wider census of botanists. "
        "Contact fields may be null.\n",
    )

    return _connection(tmp_path)


@pytest.fixture
def degraded_conn(tmp_path: Path) -> ConnectionConfig:
    """A print whose reads failed at both grains (SPEC 2.2.4, 2.2.1).

    Absent `null_patterns` with nulls present claims no column shares its nulls - unless marked.
    """

    root = tmp_path / "prints" / "primary"

    manifest = {
        "format_version": 1,
        "generated_at": WHEN,
        "connection": "primary",
        "adapter": "postgres",
        "dbprint_version": "0.2.0",
        "tables": {
            "seedbank.storage_reading": {
                "type": "table",
                "path": "seedbank/storage_reading",
                "artifacts": {"ddl": "ddl.sql", "statistics": "statistics.yaml"},
                "columns": 3,
                "profiled_at": WHEN,
                "row_count": 300,
            },
        },
    }
    statistics = {
        "format_version": 1,
        "table": "seedbank.storage_reading",
        "type": "table",
        "profiled_at": WHEN,
        "row_count": 300,
        "row_count_method": "exact",
        "grain": {"keys": [{"columns": ["reading_id"], "detection": "declared"}]},
        "unmeasured": ["dependencies", "null_patterns", "physical_layout"],
        "columns": {
            "reading_id": {
                "sql_type": "bigint",
                "nullable": False,
                "null_count": 0,
                "null_rate": 0.0,
                "cardinality": 300,
                "cardinality_ratio": 1.0,
                "cardinality_method": "exact",
                "classification": "numeric",
                "distribution": "long_tail",
                "frequencies": {"top": 1, "bottom": 1, "listed": 30, "total": 30},
                "range": {"min": 1, "max": 300},
                "percentiles": {"p01": 3, "p25": 75, "p50": 150, "p75": 225, "p99": 297},
            },
            # The whole temporal block failed, so every field it would have carried is named.
            "logged_at": {
                "sql_type": "timestamp",
                "nullable": False,
                "null_count": 0,
                "null_rate": 0.0,
                "cardinality": 280,
                "cardinality_ratio": 0.9333,
                "cardinality_method": "exact",
                "classification": "temporal",
                "unmeasured": [
                    "distribution",
                    "freshness",
                    "frequencies",
                    "percentiles",
                    "quantized_count",
                    "range",
                    "values",
                ],
            },
            "note": {
                "sql_type": "text",
                "nullable": True,
                "null_count": 20,
                "null_rate": 0.0667,
                "cardinality": 250,
                "cardinality_ratio": 0.8333,
                "cardinality_method": "exact",
                "classification": "text",
            },
        },
    }

    _write(root / "manifest.yaml", manifest)
    _write(root / "seedbank" / "storage_reading" / "statistics.yaml", statistics)
    (root / "seedbank" / "storage_reading" / "ddl.sql").write_text(
        "CREATE TABLE seedbank.storage_reading (reading_id bigint, logged_at timestamp);\n",
    )

    return _connection(tmp_path)


@pytest.fixture
def declared_missing_conn(tmp_path: Path) -> ConnectionConfig:
    """A table whose manifest declares `statistics` but the file was never written (SPEC 2.5)."""

    root = tmp_path / "prints" / "primary"

    manifest = {
        "format_version": 1,
        "generated_at": WHEN,
        "connection": "primary",
        "adapter": "postgres",
        "dbprint_version": "0.2.0",
        "tables": {
            "public.t": {
                "type": "table",
                "path": "public/t",
                "artifacts": {"ddl": "ddl.sql", "statistics": "statistics.yaml"},
                "columns": 1,
                "profiled_at": WHEN,
                "row_count": 10,
            },
        },
    }

    _write(root / "manifest.yaml", manifest)
    table_dir = root / "public" / "t"
    table_dir.mkdir(parents=True, exist_ok=True)
    (table_dir / "ddl.sql").write_text("CREATE TABLE public.t (id integer);\n")

    return _connection(tmp_path)


@pytest.fixture
def edge_case_conn(tmp_path: Path) -> ConnectionConfig:
    """One table exercising fields the reference example never carries: `unrepresentable`
    bounds and a `candidate_key` whose ratio falls short of 1.0."""

    root = tmp_path / "prints" / "primary"

    manifest = {
        "format_version": 1,
        "generated_at": WHEN,
        "connection": "primary",
        "adapter": "postgres",
        "dbprint_version": "0.2.0",
        "tables": {
            "public.legacy_dates": {
                "type": "table",
                "path": "public/legacy_dates",
                "artifacts": {"ddl": "ddl.sql", "statistics": "statistics.yaml"},
                "columns": 2,
                "profiled_at": WHEN,
                "row_count": 200,
            },
        },
    }
    statistics = {
        "format_version": 1,
        "table": "public.legacy_dates",
        "type": "table",
        "profiled_at": WHEN,
        "row_count": 200,
        "row_count_method": "exact",
        "grain": {"keys": []},
        "columns": {
            "recorded_at": {
                "sql_type": "timestamp_ntz",
                "nullable": False,
                "null_count": 0,
                "null_rate": 0.0,
                "cardinality": 198,
                "cardinality_ratio": 0.99,
                "cardinality_method": "exact",
                "classification": "temporal",
                "distribution": "uniform",
                "frequencies": {"top": 2, "bottom": 1, "listed": 30, "total": 60},
                "range": {"min": "1970-01-01T00:00:00", "max": "52030-01-01T00:00:00"},
                "percentiles": {
                    "p01": "1970-02-01T00:00:00",
                    "p25": "2000-01-01T00:00:00",
                    "p50": "2026-01-01T09:30:00",
                    "p75": "2030-01-01T00:00:00",
                    "p99": "11000-07-05T00:00:00",
                },
                "unrepresentable": ["max", "p99"],
                "freshness": {"max_age_days": 0, "classification": "live"},
            },
            "external_ref": {
                "sql_type": "character varying(20)",
                "nullable": False,
                "null_count": 0,
                "null_rate": 0.0,
                "cardinality": 199,
                "cardinality_ratio": 0.995,
                "cardinality_method": "exact",
                "classification": "text",
                "inferred": {
                    "candidate_key": True,
                    "candidate_key_exception": "measured_duplicates",
                },
                "values": [{"value": "ext-ref-00234", "count": 2}],
                "values_coverage": 0.02,
                "distribution": "long_tail",
            },
        },
    }

    _write(root / "manifest.yaml", manifest)
    _write(root / "public" / "legacy_dates" / "statistics.yaml", statistics)
    (root / "public" / "legacy_dates" / "ddl.sql").write_text(
        "CREATE TABLE public.legacy_dates (recorded_at timestamp, external_ref varchar(20));\n",
    )

    return _connection(tmp_path)
