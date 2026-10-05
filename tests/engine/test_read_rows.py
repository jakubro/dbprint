"""A `read_rows` rule opts a plain view into being profiled like a table (SPEC 2.2.15)."""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from dbprint.adapters.base import PhysicalLayout, PhysicalLayoutKey, UniqueKeyMeta
from dbprint.adapters.mock import MockAdapter, MockTable
from dbprint.config import ConnectionConfig
from dbprint.config.project import RuleConfig
from dbprint.conformance import validate_print
from dbprint.engine import Engine
from tests.engine.test_orchestrator import _conn_config, _curator_fixture


VIEW = "public.curator"


def _view_fixture(**changes: Any) -> dict[str, MockTable]:
    fixture = _curator_fixture()
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


def _opted_in(tmp_path: Path, *rules: RuleConfig, **changes: Any) -> ConnectionConfig:
    return replace(_conn_config(tmp_path), rules=rules, **changes)


def _statistics(tmp_path: Path) -> dict[str, Any]:
    return yaml.safe_load((tmp_path / "primary/public/curator/statistics.yaml").read_text())


def _manifest_entry(tmp_path: Path) -> dict[str, Any]:
    return yaml.safe_load((tmp_path / "primary/manifest.yaml").read_text())["tables"][VIEW]


def _errors(tmp_path: Path) -> list[Any]:
    return [i for i in validate_print(tmp_path / "primary") if i.severity == "error"]


def test_a_view_no_rule_opts_in_is_described_without_a_query(tmp_path: Path) -> None:
    adapter = _HookRecorder(_view_fixture())
    Engine(adapter, _conn_config(tmp_path), tmp_path).generate()

    assert _statistics(tmp_path)["catalog_only"] is True
    assert VIEW not in adapter.statistics


def test_an_opted_in_view_is_profiled_like_a_table_less_its_layout_and_keys(
    tmp_path: Path,
) -> None:
    adapter = _HookRecorder(_view_fixture())
    conn = _opted_in(tmp_path, RuleConfig(include=(VIEW,), read_rows=True))
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
        RuleConfig(read_rows=True),
        RuleConfig(include=(VIEW,), read_rows=False),
    )
    Engine(MockAdapter(_view_fixture()), conn, tmp_path).generate()

    assert _statistics(tmp_path)["catalog_only"] is True


def test_a_sample_on_an_opted_in_view_fails_that_view_naming_the_rule(tmp_path: Path) -> None:
    conn = _opted_in(tmp_path, RuleConfig(include=(VIEW,), read_rows=True, sample=0.5))
    result = Engine(MockAdapter(_view_fixture()), conn, tmp_path).generate()
    outcome = {t.fqn: t for t in result.tables}

    assert outcome[VIEW].status == "failed"
    assert "rules[0]" in str(outcome[VIEW].error)
    assert "filter" in str(outcome[VIEW].error)
    assert outcome["public.herbarium"].status == "ok"


def test_a_filter_narrows_an_opted_in_view(tmp_path: Path) -> None:
    conn = _opted_in(tmp_path, RuleConfig(include=(VIEW,), read_rows=True, filter="id IS NOT NULL"))
    Engine(MockAdapter(_view_fixture()), conn, tmp_path).generate()

    assert _statistics(tmp_path)["scope"]["filter"] == "id IS NOT NULL"


def test_a_ceiling_never_narrows_a_view_and_says_it_was_read_whole(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    conn = _opted_in(
        tmp_path,
        RuleConfig(include=(VIEW,), read_rows=True),
        max_rows_scanned=10,
    )

    with caplog.at_level(logging.WARNING):
        Engine(MockAdapter(_view_fixture()), conn, tmp_path).generate()

    assert "scope" not in _statistics(tmp_path)
    assert "max_rows_scanned" not in _manifest_entry(tmp_path)
    assert [r for r in caplog.records if VIEW in r.getMessage() and "read whole" in r.getMessage()]


def test_an_opted_in_view_left_catalog_only_draws_no_read_whole_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    conn = _opted_in(
        tmp_path,
        RuleConfig(include=(VIEW,), read_rows=True),
        max_rows_scanned=10,
    )

    with caplog.at_level(logging.WARNING):
        Engine(MockAdapter(_view_fixture(columns=[])), conn, tmp_path).generate()

    assert _statistics(tmp_path)["catalog_only"] is True
    assert not [r for r in caplog.records if "read whole" in r.getMessage()]


def test_turning_the_opt_in_on_and_off_rereads_the_view_each_time(tmp_path: Path) -> None:
    opted = _opted_in(tmp_path, RuleConfig(include=(VIEW,), read_rows=True, max_age_days=30))
    plain = _opted_in(tmp_path, RuleConfig(include=(VIEW,), max_age_days=30))
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


def test_an_opted_in_view_with_no_columns_stays_catalog_only_and_fresh(tmp_path: Path) -> None:
    fixture = _view_fixture(columns=[])
    conn = _opted_in(tmp_path, RuleConfig(include=(VIEW,), read_rows=True, max_age_days=30))
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
    Engine(MockAdapter(_view_fixture()), _conn_config(tmp_path), tmp_path).generate()
    committed = (tmp_path / "primary/public/curator/statistics.yaml").read_text()
    conn = _opted_in(tmp_path, RuleConfig(include=(VIEW,), read_rows=True))
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
        RuleConfig(include=(VIEW,), read_rows=True, filter="id IS NOT NULL"),
        RuleConfig(include=("public.herbarium",), filter="id IS NOT NULL"),
    )
    Engine(adapter, conn, tmp_path).generate()

    assert adapter.scopes[VIEW].count_exactly is True
    assert adapter.scopes["public.herbarium"].count_exactly is False
