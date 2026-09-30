"""A connection's tables profiled on several sessions at once: same artifacts, same session per copy."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from dbprint.adapters import MockAdapter, MockTable, TableScope, trace_context
from dbprint.adapters.base import ColumnMeta, PhaseA, StatisticsConfig, TableCounts
from dbprint.config.project import ConnectionConfig, RuleConfig
from dbprint.engine import Engine, GenerateRequest, GenerateResult, orchestrator
from tests.engine.test_orchestrator import _curator_fixture


PINNED_NOW = "2026-01-01T00:00:00Z"


class _SessionRecordingAdapter(MockAdapter):
    def __init__(self, fixture: dict[str, MockTable]) -> None:
        super().__init__(fixture)
        self.calls: list[tuple[str, str, int]] = []
        self.statement_connections: list[str] = []
        self.opened: list[int] = []
        # Shared by every session `new_session` copies from this one, unlike a plain int.
        self.overlap = {"now": 0, "peak": 0}
        self._lock = threading.Lock()

    def connect(self) -> None:
        super().connect()
        self.opened.append(id(self))

    def extract_ddl(self, fqn: str) -> str:
        with self._lock:
            self.overlap["now"] += 1
            self.overlap["peak"] = max(self.overlap["peak"], self.overlap["now"])

        time.sleep(0.02)

        with self._lock:
            self.overlap["now"] -= 1

        return super().extract_ddl(fqn)

    def materialize_scope(self, fqn: str, scope: TableScope) -> TableScope:
        self.calls.append(("materialize", fqn, id(self)))

        return super().materialize_scope(fqn, scope)

    def compute_base_statistics(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        config: StatisticsConfig,
        scope: TableScope | None = None,
    ) -> tuple[TableCounts, PhaseA]:
        self.calls.append(("base", fqn, id(self)))
        self.statement_connections.append(trace_context.connection.get())

        return super().compute_base_statistics(fqn, columns, config, scope)

    def compute_normalized_cardinality(
        self,
        fqn: str,
        column: str,
        scope: TableScope | None = None,
    ) -> int:
        self.calls.append(("normalized", fqn, id(self)))

        return super().compute_normalized_cardinality(fqn, column, scope)

    def release_scope(self, fqn: str, scope: TableScope) -> None:
        self.calls.append(("release", fqn, id(self)))


class _FailingSessionAdapter(_SessionRecordingAdapter):
    def connect(self) -> None:
        if self.opened:
            raise ConnectionError("too many connections for role")

        super().connect()


class _OneTableFailsAdapter(_SessionRecordingAdapter):
    def extract_ddl(self, fqn: str) -> str:
        if fqn == "public.h1":
            raise RuntimeError("relation vanished")

        return super().extract_ddl(fqn)


def _fixture() -> dict[str, MockTable]:
    base = _curator_fixture()
    herbarium = base["public.herbarium"]
    extra = {
        f"public.h{i}": replace(herbarium, namespace_path=("public", f"h{i}")) for i in range(1, 7)
    }

    return base | extra


def _conn(tmp_path: Path, parallelism: int, *, sample: bool = True) -> ConnectionConfig:
    return ConnectionConfig(
        name="primary",
        adapter="postgres",
        output=tmp_path,
        parallelism=parallelism,
        rules=(RuleConfig(include=("public.h*", "public.curator"), sample=0.5),) if sample else (),
    )


def _run(
    tmp_path: Path,
    parallelism: int,
    monkeypatch: pytest.MonkeyPatch,
    adapter_type: type[_SessionRecordingAdapter] = _SessionRecordingAdapter,
    request: GenerateRequest | None = None,
) -> tuple[_SessionRecordingAdapter, GenerateResult]:
    monkeypatch.setattr(orchestrator, "_utc_iso_now", lambda: PINNED_NOW)
    adapter = adapter_type(_fixture())
    result = Engine(adapter, _conn(tmp_path, parallelism), tmp_path).generate(request)

    return adapter, result


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


class TestAParallelRunWritesWhatASequentialOneDoes:
    def test_every_artifact_is_byte_identical(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sequential, _ = _run(tmp_path / "one", 1, monkeypatch)
        parallel, _ = _run(tmp_path / "four", 4, monkeypatch)
        one, four = _tree(tmp_path / "one"), _tree(tmp_path / "four")

        assert sequential.overlap["peak"] == 1
        assert parallel.overlap["peak"] > 1, (
            "the tables never overlapped; the comparison proves nothing"
        )
        assert len(one) > 8
        assert one == four

    def test_results_follow_listing_order(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, result = _run(tmp_path, 4, monkeypatch)

        assert [t.fqn for t in result.tables] == list(_fixture())


class TestASampledCopyStaysOnItsSession:
    def test_every_read_and_the_release_run_where_the_copy_was_made(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        adapter, _ = _run(tmp_path, 4, monkeypatch)
        made = {fqn: session for kind, fqn, session in adapter.calls if kind == "materialize"}
        kinds = {kind for kind, _, _ in adapter.calls}

        assert {"base", "normalized", "release"} <= kinds
        assert len(set(made.values())) > 1, "every copy landed on one session"

        for kind, fqn, session in adapter.calls:
            if fqn in made:
                assert session == made[fqn], f"{kind} of {fqn} ran on another session"


class TestSessions:
    def test_the_run_holds_as_many_sessions_as_configured(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        adapter, _ = _run(tmp_path, 4, monkeypatch)

        assert len(set(adapter.opened)) == 4

    def test_worker_statements_carry_the_connection_tag(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        adapter, _ = _run(tmp_path, 4, monkeypatch)

        assert adapter.statement_connections
        assert set(adapter.statement_connections) == {"primary"}

    def test_a_session_that_will_not_open_leaves_the_run_on_the_rest(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="dbprint.engine.orchestrator"):
            adapter, result = _run(tmp_path, 4, monkeypatch, _FailingSessionAdapter)

        assert len(adapter.opened) == 1
        assert result.summary.failed == 0
        assert result.summary.ok == len(_fixture())
        assert "ran with 1 of 4 sessions" in caplog.text


class TestFailures:
    def test_one_failed_table_leaves_the_rest_profiled(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, result = _run(tmp_path, 4, monkeypatch, _OneTableFailsAdapter)

        assert [t.fqn for t in result.tables if t.status == "failed"] == ["public.h1"]
        assert result.summary.ok == len(_fixture()) - 1

    def test_fail_fast_starts_nothing_after_the_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, result = _run(
            tmp_path,
            2,
            monkeypatch,
            _OneTableFailsAdapter,
            request=GenerateRequest(fail_fast=True),
        )

        assert "public.h1" in {t.fqn for t in result.tables}
        assert result.not_attempted > 0
        assert len(result.tables) + result.not_attempted == len(_fixture())
