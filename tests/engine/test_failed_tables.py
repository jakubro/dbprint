"""A table the run could not profile is named in the manifest's `failed_tables` (SPEC 2.5).

Each failure sink records it, and each next-run disposition keeps or clears the mark.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from dbprint.adapters import MockAdapter, MockTable
from dbprint.config import ConfigError, ConnectionConfig
from dbprint.engine import Engine, GenerateRequest
from tests.engine.test_orchestrator import _conn_config, _curator_fixture


CURATOR = "public.curator"
HERBARIUM = "public.herbarium"


class _Failing(MockAdapter):
    def __init__(self, fixture: dict[str, MockTable], failing: str, sink: str) -> None:
        super().__init__(fixture)
        self._failing = failing
        self._sink = sink

    def estimate_row_count(self, fqn: str) -> int | None:
        if fqn == self._failing and self._sink == "estimate":
            raise RuntimeError("simulated catalog failure")

        return super().estimate_row_count(fqn)

    def extract_ddl(self, fqn: str) -> str:
        if fqn == self._failing and self._sink == "extraction":
            raise RuntimeError("simulated extraction failure")

        return super().extract_ddl(fqn)


def _manifest(tmp_path: Path) -> dict[str, Any]:
    return yaml.safe_load((tmp_path / "primary" / "manifest.yaml").read_text())


def _diff(tmp_path: Path) -> dict[str, Any]:
    return yaml.safe_load((tmp_path / "primary" / "diff.yaml").read_text())


@pytest.mark.parametrize("sink", ["estimate", "config", "extraction"])
def test_every_failure_sink_names_the_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sink: str,
) -> None:
    if sink == "estimate":
        monkeypatch.setattr(ConnectionConfig, "rules_read_row_counts", property(lambda _: True))

    if sink == "config":
        original = ConnectionConfig.settings_for

        def refusing(self: ConnectionConfig, fqn: str, *args: Any, **kwargs: Any) -> Any:
            if fqn == HERBARIUM:
                raise ConfigError("simulated refused rule")

            return original(self, fqn, *args, **kwargs)

        monkeypatch.setattr(ConnectionConfig, "settings_for", refusing)

    adapter = _Failing(_curator_fixture(), HERBARIUM, sink)

    result = Engine(adapter, _conn_config(tmp_path), tmp_path).generate()

    manifest = _manifest(tmp_path)
    diff = _diff(tmp_path)
    assert [t.fqn for t in result.tables if t.status == "failed"] == [HERBARIUM]
    assert manifest["failed_tables"] == [HERBARIUM]
    assert HERBARIUM not in manifest["tables"]
    assert (diff["target"]["tables_scanned"], diff["summary"]["unevaluated_tables"]) == (2, 1)


def test_a_clean_run_writes_no_list(tmp_path: Path) -> None:
    Engine(MockAdapter(_curator_fixture()), _conn_config(tmp_path), tmp_path).generate()

    assert "failed_tables" not in _manifest(tmp_path)


def _carried_failure(tmp_path: Path) -> None:
    Engine(MockAdapter(_curator_fixture()), _conn_config(tmp_path), tmp_path).generate()
    failing = _Failing(_curator_fixture(), HERBARIUM, "extraction")
    Engine(failing, _conn_config(tmp_path), tmp_path).generate(GenerateRequest(force=True))

    assert _manifest(tmp_path)["failed_tables"] == [HERBARIUM]


@pytest.mark.parametrize(
    ("disposition", "adapter", "conn_changes", "request_", "kept"),
    [
        ("failed", "failing", {}, GenerateRequest(), True),
        ("out_of_scope", "failing", {}, GenerateRequest(cli_include=(CURATOR,)), True),
        ("excluded", "clean", {"exclude": (HERBARIUM,)}, GenerateRequest(), False),
        ("re_extracted", "clean", {}, GenerateRequest(), False),
        ("removed", "without", {}, GenerateRequest(), False),
    ],
)
def test_the_next_run_keeps_or_clears_the_mark(
    tmp_path: Path,
    disposition: str,
    adapter: str,
    conn_changes: dict[str, Any],
    request_: GenerateRequest,
    kept: bool,
) -> None:
    _carried_failure(tmp_path)
    fixture = _curator_fixture()
    engine_adapter = {
        "failing": _Failing(fixture, HERBARIUM, "extraction"),
        "clean": MockAdapter(fixture),
        "without": MockAdapter({fqn: t for fqn, t in fixture.items() if fqn != HERBARIUM}),
    }[adapter]

    Engine(engine_adapter, replace(_conn_config(tmp_path), **conn_changes), tmp_path).generate(
        request_,
    )

    assert (_manifest(tmp_path).get("failed_tables") == [HERBARIUM]) is kept, disposition


def test_a_marked_table_is_retried_however_young_its_print(tmp_path: Path) -> None:
    _carried_failure(tmp_path)

    result = Engine(MockAdapter(_curator_fixture()), _conn_config(tmp_path), tmp_path).generate()

    statuses = {t.fqn: t.status for t in result.tables}
    assert (statuses[HERBARIUM], statuses[CURATOR]) == ("ok", "skipped")
