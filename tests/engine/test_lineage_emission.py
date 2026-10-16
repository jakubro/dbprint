"""What `generate` writes from a configured dbt manifest: `derived_from` per column (SPEC 2.2.4)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

import dbprint.engine
from dbprint.adapters import MockAdapter, MockTable
from dbprint.config import ConnectionConfig, ProfileConfig, RelationshipsProfile
from dbprint.conformance import validate_print
from dbprint.engine import EXIT_GENERIC, Engine, GenerateRequest
from tests._engine_run import artifact
from tests._prints import columns, mock_table, unmeasured_stats


def _stats(*names: str) -> dict[str, Any]:
    return {name: unmeasured_stats("text" if name == "email" else "integer") for name in names}


def _fixture(view: bool = False) -> dict[str, MockTable]:
    return {
        "public.collector": mock_table(
            "public.collector",
            columns(("collector_id", "integer"), ("email", "text")),
            _stats("collector_id", "email"),
            row_count=3,
        ),
        "public.curator_profile": mock_table(
            "public.curator_profile",
            columns(("collector_id", "integer"), ("email", "text"), ("one", "integer")),
            _stats("collector_id", "email", "one"),
            row_count=3,
            type="view" if view else "table",
        ),
    }


def _manifest(tmp_path: Path, sql: str) -> Path:
    path = tmp_path / "dbt" / "manifest.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "metadata": {
                    "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
                    "adapter_type": "postgres",
                },
                "nodes": {
                    "model.garden.curator_profile": {
                        "unique_id": "model.garden.curator_profile",
                        "resource_type": "model",
                        "language": "sql",
                        "database": "garden",
                        "schema": "public",
                        "relation_name": '"garden"."public"."curator_profile"',
                        "compiled_code": sql,
                    },
                },
            },
        ),
    )

    return path


SQL = (
    'select c.collector_id, lower(c.email) as email, 1 as one from "garden"."public"."collector" c '
    'join "garden"."archive"."roster" r on r.collector_id = c.collector_id'
)


def _conn(tmp_path: Path, manifest: Path | None) -> ConnectionConfig:
    return ConnectionConfig(
        name="w",
        adapter="postgres",
        output=tmp_path,
        dbt_manifest=manifest,
        profile=ProfileConfig(relationships=RelationshipsProfile(infer=False)),
    )


def _generate(tmp_path: Path, manifest: Path | None, **request: Any) -> Any:
    engine = Engine(MockAdapter(_fixture()), _conn(tmp_path, manifest), tmp_path)

    return engine.generate(GenerateRequest(**request))


def _columns(tmp_path: Path) -> dict[str, Any]:
    return artifact(tmp_path / "w", "public.curator_profile")["columns"]


class TestWriting:
    def test_each_traced_column_records_its_sources_sorted(self, tmp_path: Path) -> None:
        _generate(tmp_path, _manifest(tmp_path, SQL))
        cols = _columns(tmp_path)

        assert cols["email"]["derived_from"] == [{"table": "public.collector", "column": "email"}]
        assert cols["collector_id"]["derived_from"] == [
            {"table": "public.collector", "column": "collector_id"},
        ]
        assert "derived_from" not in cols["one"]
        assert [i.code for i in validate_print(tmp_path / "w") if i.severity == "error"] == []

    def test_a_source_outside_the_print_is_recorded(self, tmp_path: Path) -> None:
        sql = 'select r.collector_id, null as email, 1 as one from "garden"."archive"."roster" r'
        _generate(tmp_path, _manifest(tmp_path, sql))

        assert _columns(tmp_path)["collector_id"]["derived_from"] == [
            {"table": "archive.roster", "column": "collector_id"},
        ]

    def test_a_catalog_only_view_carries_it(self, tmp_path: Path) -> None:
        manifest = _manifest(tmp_path, SQL)
        Engine(MockAdapter(_fixture(view=True)), _conn(tmp_path, manifest), tmp_path).generate()
        statistics = artifact(tmp_path / "w", "public.curator_profile")

        assert statistics["catalog_only"] is True
        assert statistics["columns"]["email"]["derived_from"] == [
            {"table": "public.collector", "column": "email"},
        ]
        assert [i.code for i in validate_print(tmp_path / "w") if i.severity == "error"] == []

    def test_no_manifest_configured_writes_none(self, tmp_path: Path) -> None:
        _generate(tmp_path, None)

        assert all("derived_from" not in col for col in _columns(tmp_path).values())

    def test_a_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        _generate(tmp_path, _manifest(tmp_path, SQL), dry_run=True)

        assert not (tmp_path / "w").exists()


class TestCarriedTables:
    def test_a_fresh_table_is_rewritten_only_when_its_lineage_changes(self, tmp_path: Path) -> None:
        manifest = _manifest(tmp_path, SQL)
        _generate(tmp_path, manifest)
        path = tmp_path / "w" / "public" / "curator_profile" / "statistics.yaml"
        before = path.read_bytes()

        _generate(tmp_path, manifest)
        unchanged = path.read_bytes()
        _manifest(
            tmp_path,
            'select c.collector_id, c.collector_id as email from "garden"."public"."collector" c',
        )
        _generate(tmp_path, manifest)
        after = yaml.safe_load(path.read_text())

        assert unchanged == before
        assert after["columns"]["email"]["derived_from"] == [
            {"table": "public.collector", "column": "collector_id"},
        ]
        assert after["profiled_at"] == yaml.safe_load(before)["profiled_at"]

    def test_an_unconfigured_run_leaves_committed_lineage_alone(self, tmp_path: Path) -> None:
        _generate(tmp_path, _manifest(tmp_path, SQL))
        path = tmp_path / "w" / "public" / "curator_profile" / "statistics.yaml"
        before = path.read_bytes()
        _generate(tmp_path, None)

        assert path.read_bytes() == before


class _CountingAdapter(MockAdapter):
    connects = 0

    def connect(self) -> None:
        type(self).connects += 1
        super().connect()


class TestRefusals:
    def test_a_missing_manifest_fails_the_connection_before_connecting(
        self,
        tmp_path: Path,
    ) -> None:
        engine = Engine(
            _CountingAdapter(_fixture()),
            _conn(tmp_path, tmp_path / "absent.json"),
            tmp_path,
        )
        result = engine.generate()

        assert result.exit_code == EXIT_GENERIC
        assert "absent.json" in (result.error or "")
        assert _CountingAdapter.connects == 0

    def test_a_missing_extra_names_the_install(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setitem(sys.modules, "sqlglot", None)
        monkeypatch.delitem(sys.modules, "dbprint.engine.lineage", raising=False)
        monkeypatch.delattr(dbprint.engine, "lineage", raising=False)
        result = _generate(tmp_path, _manifest(tmp_path, SQL))

        assert result.exit_code == EXIT_GENERIC
        assert "pip install dbprint[dbt]" in (result.error or "")
