"""An object whose rows live in another system is described from the catalog unless opted in."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from dbprint.adapters.mock import MockAdapter, MockTable
from dbprint.config import ConnectionConfig
from dbprint.config.project import RuleConfig
from dbprint.engine import Engine, thresholds
from dbprint.engine.context_assembler import AssemblyOptions, assemble
from tests._curator import conn_config, curator_fixture
from tests._engine_run import conformance_errors


REMOTE = "public.curator"
NOT_QUERIED = (
    "External: rows live outside this database - not queried by dbprint; "
    "every query against it reads the other system"
)
READ_THROUGH = (
    "External: rows live outside this database - statistics were read through it; "
    "every query against it reads the other system"
)


def _fixture(*, external: bool = True) -> dict[str, MockTable]:
    fixture = curator_fixture()
    fixture[REMOTE] = replace(fixture[REMOTE], external=external, row_count_estimate=100)

    return fixture


class _Recorder(MockAdapter):
    def __init__(self, fixture: dict[str, MockTable]) -> None:
        super().__init__(fixture)
        self.calls: list[tuple[str, str]] = []

    def estimate_row_count(self, fqn: str) -> Any:
        self.calls.append(("estimate_row_count", fqn))

        return super().estimate_row_count(fqn)

    def compute_base_statistics(self, fqn: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("compute_base_statistics", fqn))

        return super().compute_base_statistics(fqn, *args, **kwargs)


def _conn(tmp_path: Path, *rules: RuleConfig) -> ConnectionConfig:
    return replace(conn_config(tmp_path, max_age_days=30), rules=rules)


def _opted_in(tmp_path: Path) -> ConnectionConfig:
    return _conn(tmp_path, RuleConfig(include=(REMOTE,), read_rows=True))


def _statistics(tmp_path: Path) -> dict[str, Any]:
    return yaml.safe_load((tmp_path / "primary/public/curator/statistics.yaml").read_text())


def _manifest(tmp_path: Path) -> dict[str, Any]:
    return yaml.safe_load((tmp_path / "primary/manifest.yaml").read_text())


def _reason(result: Any) -> str:
    return next(t for t in result.tables if t.fqn == REMOTE).reason


def _context(tmp_path: Path, options: AssemblyOptions) -> str:
    return assemble(_manifest(tmp_path), tmp_path / "primary", [REMOTE], options).text


def test_a_marked_object_no_rule_opts_in_is_described_without_a_query(tmp_path: Path) -> None:
    adapter = _Recorder(_fixture())
    Engine(adapter, _conn(tmp_path), tmp_path).generate()
    statistics = _statistics(tmp_path)
    entry = _manifest(tmp_path)["tables"][REMOTE]

    assert statistics["catalog_only"] is True
    assert statistics["external"] is True
    assert "row_count" not in statistics
    assert "row_count" not in entry
    assert "max_rows_scanned" not in entry
    assert (tmp_path / "primary/public/curator/relationships.yaml").is_file()
    assert [call for call in adapter.calls if call[1] == REMOTE] == []
    assert ("compute_base_statistics", "public.herbarium") in adapter.calls
    assert conformance_errors(tmp_path / "primary") == []


def test_an_opted_in_marked_object_is_profiled_and_keeps_its_mark(tmp_path: Path) -> None:
    Engine(MockAdapter(_fixture()), _opted_in(tmp_path), tmp_path).generate()
    statistics = _statistics(tmp_path)

    assert "catalog_only" not in statistics
    assert statistics["row_count"] == 100
    assert statistics["external"] is True
    assert conformance_errors(tmp_path / "primary") == []


def test_a_size_rule_reads_no_estimate_for_a_marked_object(tmp_path: Path) -> None:
    adapter = _Recorder(_fixture())
    conn = _conn(tmp_path, RuleConfig(min_rows=10, max_age_days=3))
    Engine(adapter, conn, tmp_path).generate()

    assert ("estimate_row_count", REMOTE) not in adapter.calls
    assert ("estimate_row_count", "public.herbarium") in adapter.calls


def test_a_local_table_carries_no_mark(tmp_path: Path) -> None:
    Engine(MockAdapter(_fixture(external=False)), _conn(tmp_path), tmp_path).generate()

    assert "external" not in _statistics(tmp_path)


def test_a_filter_added_to_a_catalog_only_object_leaves_it_fresh(tmp_path: Path) -> None:
    Engine(MockAdapter(_fixture()), _conn(tmp_path), tmp_path).generate()
    filtered = _conn(tmp_path, RuleConfig(include=(REMOTE,), filter="id IS NOT NULL"))
    result = Engine(MockAdapter(_fixture()), filtered, tmp_path).generate()

    assert next(t for t in result.tables if t.fqn == REMOTE).status == "skipped"


def test_opting_in_and_marking_each_reextract_with_their_reason(tmp_path: Path) -> None:
    Engine(MockAdapter(_fixture()), _conn(tmp_path), tmp_path).generate()
    opted = Engine(MockAdapter(_fixture()), _opted_in(tmp_path), tmp_path).generate()
    unmarked = Engine(MockAdapter(_fixture(external=False)), _opted_in(tmp_path), tmp_path)

    assert _reason(opted) == "catalog_only changed since it was profiled"
    assert _reason(unmarked.generate()) == "external changed since it was profiled"


def test_a_local_table_swapped_for_a_remote_one_is_a_shape_change(tmp_path: Path) -> None:
    Engine(MockAdapter(_fixture(external=False)), _conn(tmp_path), tmp_path).generate()
    result = Engine(MockAdapter(_fixture()), _conn(tmp_path), tmp_path).compute_diff()
    swaps = [c for c in result.diff["changes"] if c["kind"] == "external_changed"]

    assert swaps == [{"kind": "external_changed", "table": REMOTE, "before": False, "after": True}]
    assert result.diff["summary"]["tables_modified"] >= 1


def test_a_local_table_staying_local_reports_no_swap(tmp_path: Path) -> None:
    Engine(MockAdapter(_fixture(external=False)), _conn(tmp_path), tmp_path).generate()
    result = Engine(MockAdapter(_fixture(external=False)), _conn(tmp_path), tmp_path).compute_diff()

    assert [c for c in result.diff["changes"] if c["kind"] == "external_changed"] == []


def test_offline_no_size_gate_is_claimed_for_an_entry_nothing_queried(tmp_path: Path) -> None:
    Engine(MockAdapter(_fixture()), _conn(tmp_path), tmp_path).generate()
    manifest = _manifest(tmp_path)

    for entry in manifest["tables"].values():
        entry.pop("max_age_days")

    conn = _conn(tmp_path, RuleConfig(min_rows=10, max_age_days=3))

    assert thresholds.resolve(conn, manifest).size_gated == ("public.herbarium",)


def test_both_context_purposes_say_the_rows_live_elsewhere(tmp_path: Path) -> None:
    Engine(MockAdapter(_fixture()), _conn(tmp_path), tmp_path).generate()

    assert NOT_QUERIED in _context(tmp_path, AssemblyOptions(purpose="query"))
    assert NOT_QUERIED in _context(tmp_path, AssemblyOptions())


def test_an_opted_in_context_says_the_statistics_were_read_through_it(tmp_path: Path) -> None:
    Engine(MockAdapter(_fixture()), _opted_in(tmp_path), tmp_path).generate()

    assert READ_THROUGH in _context(tmp_path, AssemblyOptions())
    assert READ_THROUGH in _context(tmp_path, AssemblyOptions(purpose="query"))


def _snapshot_fixture() -> dict[str, MockTable]:
    fixture = curator_fixture()
    fixture[REMOTE] = replace(fixture[REMOTE], opt_in_only=True, row_count_estimate=100)

    return fixture


def test_an_opt_in_only_object_is_catalog_only_without_the_external_mark(tmp_path: Path) -> None:
    adapter = _Recorder(_snapshot_fixture())
    Engine(adapter, _conn(tmp_path, RuleConfig(min_rows=10, max_age_days=3)), tmp_path).generate()
    statistics = _statistics(tmp_path)

    assert statistics["catalog_only"] is True
    assert "external" not in statistics
    assert [call for call in adapter.calls if call[1] == REMOTE] == []
    assert "External:" not in _context(tmp_path, AssemblyOptions())
    assert conformance_errors(tmp_path / "primary") == []


def test_opting_an_opt_in_only_object_in_profiles_it_and_rereads_it(tmp_path: Path) -> None:
    Engine(MockAdapter(_snapshot_fixture()), _conn(tmp_path), tmp_path).generate()
    result = Engine(MockAdapter(_snapshot_fixture()), _opted_in(tmp_path), tmp_path).generate()
    statistics = _statistics(tmp_path)

    assert _reason(result) == "catalog_only changed since it was profiled"
    assert statistics["row_count"] == 100
    assert "catalog_only" not in statistics
    assert "external" not in statistics


def test_a_marked_object_narrowed_two_ways_fails_alone(tmp_path: Path) -> None:
    conn = _conn(
        tmp_path,
        RuleConfig(include=(REMOTE,), filter="id IS NOT NULL"),
        RuleConfig(include=(REMOTE,), sample=0.5),
    )
    result = Engine(MockAdapter(_fixture()), conn, tmp_path).generate()

    assert {t.fqn: t.status for t in result.tables} == {REMOTE: "failed", "public.herbarium": "ok"}
