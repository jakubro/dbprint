"""Column lineage read from a dbt manifest's compiled SQL: loading, mapping and tracing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dbprint.engine.lineage import (
    DbtManifest,
    DbtModel,
    ManifestUnreadable,
    SourceColumn,
    load_manifest,
    trace,
)


COLUMNS = {
    "arboretum.seedbank.collector": ["collector_id", "email", "full_name"],
    "arboretum.seedbank.curator": ["collector_id", "shelf_code"],
    "arboretum.seedbank.curator_profile": ["collector_id", "email", "label", "one", "n"],
}


def _model(
    sql: str,
    relation: str = '"arboretum"."seedbank"."curator_profile"',
    **kw: Any,
) -> DbtModel:
    return DbtModel(
        unique_id=kw.get("unique_id", "model.seedbank.curator_profile"),
        relation_name=relation,
        database=kw.get("database", "arboretum"),
        schema=kw.get("schema", "seedbank"),
        compiled_code=sql,
    )


def _trace(*models: DbtModel, vendor: str = "duckdb", columns: Any = None) -> Any:
    return trace(
        DbtManifest(schema_version=12, adapter_type=vendor, models=models),
        vendor=vendor,
        depth=3,
        columns=columns or COLUMNS,
    )


def _sources(traced: Any, column: str, table: str = "arboretum.seedbank.curator_profile") -> list:
    return list(traced.tables.get(table, {}).get(column, ()))


COLLECTOR_EMAIL = SourceColumn("arboretum.seedbank.collector", "email")


class TestTracing:
    def test_a_plain_copy_is_traced_to_its_source(self) -> None:
        traced = _trace(_model('select email from "arboretum"."seedbank"."collector"'))

        assert _sources(traced, "email") == [COLLECTOR_EMAIL]

    def test_a_cte_pass_through_and_an_inlined_ephemeral_are_traced(self) -> None:
        sql = (
            "with __dbt__cte__base as (select * from arboretum.seedbank.collector), "
            "c as (select email from __dbt__cte__base) select email from c"
        )

        assert _sources(_trace(_model(sql)), "email") == [COLLECTOR_EMAIL]

    def test_a_function_argument_is_a_source(self) -> None:
        sql = "select lower(email) as email from arboretum.seedbank.collector"

        assert _sources(_trace(_model(sql)), "email") == [COLLECTOR_EMAIL]

    def test_select_star_expands_against_the_printed_columns(self) -> None:
        traced = _trace(_model("select * from arboretum.seedbank.collector"))

        assert _sources(traced, "full_name") == []  # not a column of curator_profile
        assert _sources(traced, "email") == [COLLECTOR_EMAIL]

    def test_a_literal_and_count_star_have_no_source(self) -> None:
        sql = "select 1 as one, count(*) over () as n, email from arboretum.seedbank.collector"
        traced = _trace(_model(sql))

        assert _sources(traced, "one") == []
        assert _sources(traced, "n") == []
        assert _sources(traced, "email") == [COLLECTOR_EMAIL]

    def test_an_unqualified_column_over_a_join_resolves_through_the_schema(self) -> None:
        sql = (
            "select shelf_code as label from arboretum.seedbank.collector c "
            "join arboretum.seedbank.curator k on k.collector_id = c.collector_id"
        )

        assert _sources(_trace(_model(sql)), "label") == [
            SourceColumn("arboretum.seedbank.curator", "shelf_code"),
        ]

    def test_a_self_reference_is_dropped(self) -> None:
        sql = "select email from arboretum.seedbank.curator_profile"

        assert _trace(_model(sql)).tables == {}

    def test_a_short_reference_takes_its_qualifier_from_the_model_node(self) -> None:
        sql = "select email from seedbank.collector"

        assert _sources(_trace(_model(sql)), "email") == [COLLECTOR_EMAIL]

    def test_a_qualified_source_outside_the_print_is_kept(self) -> None:
        sql = "select o.email from arboretum.archive.old_collector o"

        assert _sources(_trace(_model(sql)), "email") == [
            SourceColumn("arboretum.archive.old_collector", "email"),
        ]

    def test_a_parse_failure_is_contained_to_its_model(self) -> None:
        broken = _model(
            "selec email frm",
            relation='"arboretum"."seedbank"."curator"',
            unique_id="model.x.bad",
        )
        good = _model("select email from arboretum.seedbank.collector")
        traced = _trace(broken, good)

        assert [uid for uid, _ in traced.unparsed] == ["model.x.bad"]
        assert (traced.models_parsed, traced.models_eligible) == (1, 2)
        assert _sources(traced, "email") == [COLLECTOR_EMAIL]

    def test_sql_nested_past_the_parser_s_recursion_limit_is_contained_to_its_model(self) -> None:
        nested = "coalesce(" * 60 + "email" + ", '')" * 60
        broken = _model(
            f"select {nested} as email from arboretum.seedbank.collector",
            relation='"arboretum"."seedbank"."curator"',
            unique_id="model.x.bad",
        )
        good = _model("select email from arboretum.seedbank.collector")
        traced = _trace(broken, good)

        assert traced.unparsed == (
            ("model.x.bad", "RecursionError: maximum recursion depth exceeded"),
        )
        assert (traced.models_parsed, traced.models_eligible) == (1, 2)
        assert _sources(traced, "email") == [COLLECTOR_EMAIL]

    @pytest.mark.parametrize("relation", ['"unterminated', ""])
    def test_an_unreadable_relation_name_is_contained_to_its_model(self, relation: str) -> None:
        broken = _model("select 1 as x", relation=relation, unique_id="model.x.bad")
        good = _model("select email from arboretum.seedbank.collector")
        traced = _trace(broken, good)

        assert [uid for uid, _ in traced.unparsed] == ["model.x.bad"]
        assert (traced.models_parsed, traced.models_eligible) == (1, 2)
        assert _sources(traced, "email") == [COLLECTOR_EMAIL]

    def test_a_model_not_in_the_print_is_skipped(self) -> None:
        traced = _trace(_model("select 1 as x", relation='"arboretum"."seedbank"."elsewhere"'))

        assert (traced.models_parsed, traced.models_eligible) == (0, 0)


class TestRelationMapping:
    def test_snowflake_unquoted_and_quoted_upper_case_both_fold(self) -> None:
        sql = 'select "EMAIL" from ARBORETUM.SEEDBANK."COLLECTOR"'
        traced = _trace(
            _model(sql, relation="ARBORETUM.SEEDBANK.CURATOR_PROFILE"),
            vendor="snowflake",
        )

        assert _sources(traced, "email") == [COLLECTOR_EMAIL]

    def test_bigquery_drops_the_project(self) -> None:
        columns = {"seedbank.collector": ["email"], "seedbank.curator_profile": ["email"]}
        manifest = DbtManifest(
            schema_version=12,
            adapter_type="bigquery",
            models=(
                _model(
                    "select email from `garden-proj`.`seedbank`.`collector`",
                    relation="`garden-proj`.`seedbank`.`curator_profile`",
                    database="garden-proj",
                ),
            ),
        )
        traced = trace(manifest, vendor="bigquery", depth=2, columns=columns)

        assert traced.tables == {
            "seedbank.curator_profile": {"email": (SourceColumn("seedbank.collector", "email"),)},
        }


def _write_manifest(tmp_path: Path, version: int, nodes: dict[str, Any]) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "metadata": {
                    "dbt_schema_version": f"https://schemas.getdbt.com/dbt/manifest/v{version}.json",
                    "adapter_type": "duckdb",
                },
                "nodes": nodes,
            },
        ),
    )

    return path


def _node(**overrides: Any) -> dict[str, Any]:
    return {
        "unique_id": "model.seedbank.curator_profile",
        "resource_type": "model",
        "language": "sql",
        "database": "arboretum",
        "schema": "seedbank",
        "relation_name": '"arboretum"."seedbank"."curator_profile"',
        "compiled_code": "select email from arboretum.seedbank.collector",
        **overrides,
    }


class TestLoading:
    def test_reads_compiled_models_and_snapshots_only(self, tmp_path: Path) -> None:
        path = _write_manifest(
            tmp_path,
            12,
            {
                "model.a": _node(unique_id="model.a"),
                "snapshot.b": _node(unique_id="snapshot.b", resource_type="snapshot"),
                "model.eph": _node(unique_id="model.eph", relation_name=None),
                "model.py": _node(unique_id="model.py", language="python"),
                "seed.c": _node(unique_id="seed.c", resource_type="seed"),
                "test.d": _node(unique_id="test.d", resource_type="test"),
            },
        )

        assert [m.unique_id for m in load_manifest(path).models] == ["model.a", "snapshot.b"]

    def test_a_manifest_before_v7_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ManifestUnreadable, match=r"manifest v6 predates dbt 1\.3"):
            load_manifest(_write_manifest(tmp_path, 6, {"model.a": _node()}))

    def test_a_newer_manifest_is_read(self, tmp_path: Path) -> None:
        assert (
            load_manifest(_write_manifest(tmp_path, 13, {"model.a": _node()})).schema_version == 13
        )

    def test_an_uncompiled_manifest_is_refused(self, tmp_path: Path) -> None:
        path = _write_manifest(tmp_path, 12, {"model.a": _node(compiled_code=None)})

        with pytest.raises(ManifestUnreadable, match="run `dbt compile` first"):
            load_manifest(path)

    @pytest.mark.parametrize(
        ("body", "message"),
        [(None, "cannot read it"), ("{not json", "not valid JSON"), ("{}", "no metadata")],
    )
    def test_an_unusable_file_is_refused(
        self,
        tmp_path: Path,
        body: str | None,
        message: str,
    ) -> None:
        path = tmp_path / "manifest.json"

        if body is not None:
            path.write_text(body)

        with pytest.raises(ManifestUnreadable, match=message):
            load_manifest(path)

    def test_a_manifest_nested_past_the_json_recursion_limit_is_refused(
        self,
        tmp_path: Path,
    ) -> None:
        path = tmp_path / "manifest.json"
        path.write_text("[" * 100_000 + "]" * 100_000)

        with pytest.raises(ManifestUnreadable, match="nested too deeply to read"):
            load_manifest(path)
