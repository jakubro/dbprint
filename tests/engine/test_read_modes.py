"""`profile.read` modes: a view read like a table, a table described from the catalog (SPEC 2.2.15)."""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from dbprint.adapters.base import PhysicalLayout, PhysicalLayoutKey, UniqueKeyMeta
from dbprint.adapters.mock import MockAdapter, MockTable
from dbprint.config import ConnectionConfig, ReadConfig, ReadSettings, RuleMatch
from dbprint.config.project import RuleConfig
from dbprint.docs.view import catalog_only_notice
from dbprint.engine import Engine, GenerateRequest
from dbprint.engine.context_assembler import AssemblyOptions, assemble
from dbprint.mcp import ServedConnections
from tests import _mcp_pages
from tests._curator import conn_config, curator_fixture
from tests._engine_run import conformance_errors


VIEW = "public.curator"


def _view_fixture(**changes: Any) -> dict[str, MockTable]:
    fixture = curator_fixture()
    fixture[VIEW] = replace(
        fixture[VIEW],
        type="view",
        physical_layout=PhysicalLayout(
            mechanism="cluster",
            keys=(PhysicalLayoutKey(expression="id", column="id"),),
        ),
        unique_keys=[UniqueKeyMeta(columns=("id",), primary=True)],
        **changes,
    )

    return fixture


class _HookRecorder(MockAdapter):
    def __init__(self, fixture: dict[str, MockTable]) -> None:
        super().__init__(fixture)
        self.hooks: list[tuple[str, str]] = []
        self.statistics: list[str] = []

    def introspect_physical_layout(self, fqn: str) -> Any:
        self.hooks.append(("physical_layout", fqn))

        return super().introspect_physical_layout(fqn)

    def introspect_unique_keys(self, fqn: str) -> Any:
        self.hooks.append(("unique_keys", fqn))

        return super().introspect_unique_keys(fqn)

    def compute_base_statistics(self, fqn: str, *args: Any, **kwargs: Any) -> Any:
        self.statistics.append(fqn)

        return super().compute_base_statistics(fqn, *args, **kwargs)


def _opted_in(
    tmp_path: Path,
    *rules: RuleConfig,
    max_rows: int | None = None,
) -> ConnectionConfig:
    conn = conn_config(tmp_path)
    read = (
        ReadSettings(tables=ReadConfig(mode="sample", max_rows=max_rows))
        if max_rows is not None
        else ReadSettings()
    )

    return replace(conn, rules=rules, profile=replace(conn.profile, read=read))


def _views(mode: str, **params: Any) -> dict[str, Any]:
    return {"read": {"views": {"mode": mode, **params}}}


def _statistics(tmp_path: Path) -> dict[str, Any]:
    return yaml.safe_load((tmp_path / "primary/public/curator/statistics.yaml").read_text())


def _manifest_entry(tmp_path: Path) -> dict[str, Any]:
    return yaml.safe_load((tmp_path / "primary/manifest.yaml").read_text())["tables"][VIEW]


def _errors(tmp_path: Path) -> list[Any]:
    return conformance_errors(tmp_path / "primary")


def test_a_view_is_described_without_a_query_by_default(tmp_path: Path) -> None:
    adapter = _HookRecorder(_view_fixture())
    Engine(adapter, conn_config(tmp_path), tmp_path).generate()

    assert _statistics(tmp_path)["catalog_only"] is True
    assert VIEW not in adapter.statistics


def test_a_view_read_in_full_is_profiled_like_a_table_less_its_layout_and_keys(
    tmp_path: Path,
) -> None:
    adapter = _HookRecorder(_view_fixture())
    conn = _opted_in(
        tmp_path,
        RuleConfig(
            match=RuleMatch(tables=(VIEW,)),
            profile=_views("full"),
        ),
    )
    Engine(adapter, conn, tmp_path).generate()
    statistics = _statistics(tmp_path)

    assert "catalog_only" not in statistics
    assert statistics["row_count"] == 100
    assert statistics["row_count_method"] == "exact"
    assert "physical_layout" not in statistics
    assert VIEW in adapter.statistics
    assert ("physical_layout", VIEW) not in adapter.hooks
    assert _manifest_entry(tmp_path)["row_count"] == 100
    assert "max_rows_scanned" not in _manifest_entry(tmp_path)
    assert _errors(tmp_path) == []


def test_a_later_rule_carves_a_view_back_out(tmp_path: Path) -> None:
    conn = _opted_in(
        tmp_path,
        RuleConfig(profile=_views("full")),
        RuleConfig(match=RuleMatch(tables=(VIEW,)), profile=_views("catalog")),
    )
    Engine(MockAdapter(_view_fixture()), conn, tmp_path).generate()

    assert _statistics(tmp_path)["catalog_only"] is True


def test_a_filter_narrows_a_view(tmp_path: Path) -> None:
    conn = _opted_in(
        tmp_path,
        RuleConfig(
            match=RuleMatch(tables=(VIEW,)),
            profile=_views("filter", filter="id IS NOT NULL"),
        ),
    )
    Engine(MockAdapter(_view_fixture()), conn, tmp_path).generate()

    assert _statistics(tmp_path)["scope"]["filter"] == "id IS NOT NULL"


def test_a_ceiling_never_narrows_a_view(tmp_path: Path) -> None:
    conn = _opted_in(
        tmp_path,
        RuleConfig(match=RuleMatch(tables=(VIEW,)), profile=_views("full")),
        max_rows=10,
    )
    Engine(MockAdapter(_view_fixture()), conn, tmp_path).generate()

    assert "scope" not in _statistics(tmp_path)
    assert "max_rows_scanned" not in _manifest_entry(tmp_path)


def test_a_size_rule_naming_a_read_view_says_it_does_not_apply(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    conn = _opted_in(
        tmp_path,
        RuleConfig(match=RuleMatch(tables=(VIEW,)), profile=_views("full")),
        RuleConfig(match=RuleMatch(min_rows=5), profile={"freshness": {"max_age_days": 3}}),
    )

    with caplog.at_level(logging.WARNING):
        Engine(MockAdapter(_view_fixture()), conn, tmp_path).generate()

    assert [
        r for r in caplog.records if VIEW in r.getMessage() and "do not apply" in r.getMessage()
    ]


def test_switching_a_view_between_modes_rereads_it_each_time(tmp_path: Path) -> None:
    opted = _opted_in(
        tmp_path,
        RuleConfig(
            match=RuleMatch(tables=(VIEW,)),
            profile={"freshness": {"max_age_days": 30}, **_views("full")},
        ),
    )
    plain = _opted_in(
        tmp_path,
        RuleConfig(match=RuleMatch(tables=(VIEW,)), profile={"freshness": {"max_age_days": 30}}),
    )
    reasons = []

    for conn in (plain, opted, opted, plain):
        result = Engine(MockAdapter(_view_fixture()), conn, tmp_path).generate()
        reasons.append(next(t for t in result.tables if t.fqn == VIEW).reason)

    assert reasons == [
        "no committed print",
        "catalog_only changed since it was profiled",
        "younger than max_age_days 30",
        "catalog_only changed since it was profiled",
    ]


def test_a_view_read_in_full_with_no_columns_stays_catalog_only_and_fresh(tmp_path: Path) -> None:
    fixture = _view_fixture(columns=[])
    conn = _opted_in(
        tmp_path,
        RuleConfig(
            match=RuleMatch(tables=(VIEW,)),
            profile={"freshness": {"max_age_days": 30}, **_views("full")},
        ),
    )
    Engine(MockAdapter(fixture), conn, tmp_path).generate()
    result = Engine(MockAdapter(fixture), conn, tmp_path).generate()

    assert _statistics(tmp_path)["catalog_only"] is True
    assert next(t for t in result.tables if t.fqn == VIEW).status == "skipped"


class _FailingRead(MockAdapter):
    def compute_base_statistics(self, fqn: str, *args: Any, **kwargs: Any) -> Any:
        if fqn == VIEW:
            raise RuntimeError("permission denied for view curator")

        return super().compute_base_statistics(fqn, *args, **kwargs)


def test_a_failed_read_fails_the_view_and_keeps_its_committed_file(tmp_path: Path) -> None:
    Engine(MockAdapter(_view_fixture()), conn_config(tmp_path), tmp_path).generate()
    committed = (tmp_path / "primary/public/curator/statistics.yaml").read_text()
    conn = _opted_in(
        tmp_path,
        RuleConfig(
            match=RuleMatch(tables=(VIEW,)),
            profile=_views("full"),
        ),
    )
    result = Engine(_FailingRead(_view_fixture()), conn, tmp_path).generate()

    assert next(t for t in result.tables if t.fqn == VIEW).status == "failed"
    assert (tmp_path / "primary/public/curator/statistics.yaml").read_text() == committed


class _ScopeRecorder(MockAdapter):
    def __init__(self, fixture: dict[str, MockTable]) -> None:
        super().__init__(fixture)
        self.scopes: dict[str, Any] = {}

    def compute_base_statistics(self, fqn: str, *args: Any, **kwargs: Any) -> Any:
        self.scopes[fqn] = args[2] if len(args) > 2 else kwargs.get("scope")

        return super().compute_base_statistics(fqn, *args, **kwargs)


def test_a_narrowed_view_is_counted_exactly_and_a_narrowed_table_is_not_forced(
    tmp_path: Path,
) -> None:
    adapter = _ScopeRecorder(_view_fixture())
    conn = _opted_in(
        tmp_path,
        RuleConfig(
            match=RuleMatch(tables=(VIEW,)),
            profile=_views("filter", filter="id IS NOT NULL"),
        ),
        RuleConfig(
            match=RuleMatch(tables=("public.herbarium",)),
            profile={"read": {"tables": {"mode": "filter", "filter": "id IS NOT NULL"}}},
        ),
    )
    Engine(adapter, conn, tmp_path).generate()

    assert adapter.scopes[VIEW].count_exactly is True
    assert adapter.scopes["public.herbarium"].count_exactly is False


TABLE = "public.herbarium"


def test_a_catalog_only_table_is_described_without_a_query_and_carried_next_run(
    tmp_path: Path,
) -> None:
    adapter = _HookRecorder(curator_fixture())
    conn = _opted_in(
        tmp_path,
        RuleConfig(
            match=RuleMatch(tables=(TABLE,)),
            profile={"freshness": {"max_age_days": 30}, "read": {"tables": {"mode": "catalog"}}},
        ),
    )
    Engine(adapter, conn, tmp_path).generate()
    statistics = yaml.safe_load((tmp_path / "primary/public/herbarium/statistics.yaml").read_text())
    entry = yaml.safe_load((tmp_path / "primary/manifest.yaml").read_text())["tables"][TABLE]
    again = Engine(MockAdapter(curator_fixture()), conn, tmp_path).generate()

    assert statistics["catalog_only"] is True
    assert statistics["type"] == "table"
    assert "row_count" not in statistics
    assert set(statistics["columns"]["id"]) <= {"sql_type", "nullable", "classification"}
    assert TABLE not in adapter.statistics
    assert "row_count" not in entry
    assert "max_rows_scanned" not in entry
    assert next(t for t in again.tables if t.fqn == TABLE).status == "skipped"
    assert _errors(tmp_path) == []


class _EstimateRecorder(MockAdapter):
    def __init__(self, fixture: dict[str, MockTable]) -> None:
        super().__init__(fixture)
        self.estimated: list[str] = []
        self.profiled: list[str] = []

    def estimate_row_count(self, fqn: str) -> Any:
        self.estimated.append(fqn)

        return super().estimate_row_count(fqn)

    def compute_base_statistics(self, fqn: str, *args: Any, **kwargs: Any) -> Any:
        self.profiled.append(fqn)

        return super().compute_base_statistics(fqn, *args, **kwargs)


def test_a_size_rule_describing_large_tables_from_the_catalog_reads_only_the_estimate(
    tmp_path: Path,
) -> None:
    fixture = curator_fixture()
    fixture[TABLE] = replace(fixture[TABLE], row_count_estimate=500)
    adapter = _EstimateRecorder(fixture)
    conn = _opted_in(
        tmp_path,
        RuleConfig(
            match=RuleMatch(min_rows=100),
            profile={"read": {"tables": {"mode": "catalog"}}},
        ),
    )
    Engine(adapter, conn, tmp_path).generate()

    assert TABLE in adapter.estimated
    assert TABLE not in adapter.profiled
    assert "public.curator" in adapter.profiled


def test_a_name_only_catalog_table_fetches_no_estimate(tmp_path: Path) -> None:
    adapter = _EstimateRecorder(curator_fixture())
    conn = _opted_in(
        tmp_path,
        RuleConfig(
            match=RuleMatch(tables=(TABLE,)),
            profile={"read": {"tables": {"mode": "catalog"}}},
        ),
        max_rows=10,
    )
    Engine(adapter, conn, tmp_path).generate()

    assert TABLE not in adapter.estimated
    assert "public.curator" in adapter.estimated


def _catalog_table(tmp_path: Path) -> ConnectionConfig:
    return _opted_in(
        tmp_path,
        RuleConfig(
            match=RuleMatch(tables=(TABLE,)),
            profile={"read": {"tables": {"mode": "catalog"}}},
        ),
    )


def test_every_consumer_renders_a_catalog_only_table_as_not_queried(tmp_path: Path) -> None:
    conn = _catalog_table(tmp_path)
    Engine(MockAdapter(curator_fixture()), conn, tmp_path).generate()
    manifest = yaml.safe_load((tmp_path / "primary/manifest.yaml").read_text())
    statistics = yaml.safe_load((tmp_path / "primary/public/herbarium/statistics.yaml").read_text())
    context = assemble(manifest, tmp_path / "primary", [TABLE], AssemblyOptions()).text
    served = ServedConnections(served={conn.name: conn}, default=conn.name)
    tool = _mcp_pages.joined(
        _mcp_pages.pages(served, "get_table_context", {"table": TABLE, "format": "md"}),
    )

    assert "## Columns (not queried)" in context
    assert "## Columns (not queried)" in tool
    assert catalog_only_notice(statistics) is not None


def test_a_table_switched_from_full_to_catalog_diffs_as_unevaluated(tmp_path: Path) -> None:
    Engine(MockAdapter(curator_fixture()), _opted_in(tmp_path), tmp_path).generate()
    result = Engine(MockAdapter(curator_fixture()), _catalog_table(tmp_path), tmp_path).generate(
        GenerateRequest(force=True),
    )
    diff = yaml.safe_load((tmp_path / "primary/diff.yaml").read_text())

    assert next(t for t in result.tables if t.fqn == TABLE).status == "ok"
    assert diff["summary"]["unevaluated_tables"] == 1
    assert diff["summary"]["unchanged_tables"] == 1
