"""The committed-print model: read once, one disposition per table, one model on every route."""

from __future__ import annotations

import io
import random
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from dbprint import __version__ as DBPRINT_VERSION
from dbprint.adapters import MockAdapter, MockTable
from dbprint.config import TableSettings
from dbprint.config.project import StatisticsConfig
from dbprint.engine import DiffRequest, Engine, GenerateRequest
from dbprint.engine.carried import (
    CarriedTable,
    CommittedPrint,
    CommittedTable,
    RecordedSettings,
    carried_entry,
    freshness,
    plan_carry,
)
from dbprint.engine.diff import DiffSelectors
from dbprint.engine.manifest_builder import profiling_params_dict, statistics_params_dict
from dbprint.engine.result import TableResult, TableStatus
from tests.engine.test_orchestrator import _conn_config, _curator_fixture


CURATOR = "public.curator"
HERBARIUM = "public.herbarium"


class _FailingAdapter(MockAdapter):
    def __init__(self, fixture: dict[str, MockTable], failing: str) -> None:
        super().__init__(fixture)
        self._failing = failing

    def extract_ddl(self, fqn: str) -> str:
        if fqn == self._failing:
            raise RuntimeError("simulated extraction failure")

        return super().extract_ddl(fqn)


def _first_run(tmp_path: Path) -> Path:
    Engine(MockAdapter(_curator_fixture()), _conn_config(tmp_path), tmp_path).generate()

    return tmp_path / "primary"


def _opens(monkeypatch: pytest.MonkeyPatch, root: Path) -> Counter[str]:
    counts: Counter[str] = Counter()
    original = io.open

    def counting(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if not any(flag in mode for flag in "wax+"):
            path = Path(file).resolve() if isinstance(file, (str, Path)) else None

            if path is not None and path.is_relative_to(root.resolve()):
                counts[path.relative_to(root.resolve()).as_posix()] += 1

        return original(file, mode, *args, **kwargs)

    monkeypatch.setattr(io, "open", counting)

    return counts


class TestReadOnce:
    """Every route that leaves a table unread reads each committed artifact at most once."""

    @pytest.mark.parametrize(
        ("route", "adapter", "request_", "conn_changes"),
        [
            ("fresh", None, GenerateRequest(), {}),
            ("failed", CURATOR, GenerateRequest(force=True), {}),
            ("not_attempted", CURATOR, GenerateRequest(force=True, fail_fast=True), {}),
            ("out_of_scope", None, GenerateRequest(force=True, cli_include=(CURATOR,)), {}),
            ("two_sessions", None, GenerateRequest(), {"parallelism": 2}),
        ],
    )
    def test_generate(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        route: str,
        adapter: str | None,
        request_: GenerateRequest,
        conn_changes: dict[str, Any],
    ) -> None:
        root = _first_run(tmp_path)
        engine_adapter = (
            MockAdapter(_curator_fixture())
            if adapter is None
            else _FailingAdapter(_curator_fixture(), adapter)
        )
        conn = replace(_conn_config(tmp_path), **conn_changes)
        counts = _opens(monkeypatch, root)

        Engine(engine_adapter, conn, tmp_path).generate(request_)

        assert counts, route
        assert {path: n for path, n in counts.items() if n > 1} == {}, route

    def test_removed_and_missing_artifact(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = _first_run(tmp_path)
        (root / "public" / "curator" / "statistics.yaml").unlink()
        fixture = {fqn: table for fqn, table in _curator_fixture().items() if fqn != HERBARIUM}
        counts = _opens(monkeypatch, root)

        Engine(MockAdapter(fixture), _conn_config(tmp_path), tmp_path).generate(GenerateRequest())

        assert counts
        assert {path: n for path, n in counts.items() if n > 1} == {}

    def test_diff(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = _first_run(tmp_path)
        counts = _opens(monkeypatch, root)

        Engine(MockAdapter(_curator_fixture()), _conn_config(tmp_path), tmp_path).compute_diff(
            DiffRequest(),
        )

        assert counts
        assert {path: n for path, n in counts.items() if n > 1} == {}


class TestPartition:
    """Every committed table gets exactly one disposition, whatever the listing and results."""

    STATUSES: tuple[TableStatus | None, ...] = ("ok", "skipped", "failed", None)

    @pytest.mark.parametrize("seed", range(20))
    def test_dispositions_partition_the_committed_tables(self, tmp_path: Path, seed: int) -> None:
        rng = random.Random(seed)
        committed = CommittedPrint.load(_first_run(tmp_path))
        extra = {
            f"public.t{i}": CommittedTable(fqn=f"public.t{i}", entry={"path": f"public/t{i}"})
            for i in range(6)
        }
        committed = replace(committed, tables={**committed.tables, **extra})
        universe = [*committed.tables, "public.new_table"]
        listed = [fqn for fqn in universe if rng.random() < 0.6]
        covered = {fqn for fqn in universe if fqn in listed or rng.random() < 0.5}
        results = [
            TableResult(fqn=fqn, status=status, error=None, elapsed_ms=0)
            for fqn in listed
            if (status := rng.choice(self.STATUSES)) is not None
        ]
        scope = _Covers(include=(), exclude=(), covered=frozenset(covered))

        carry = plan_carry(committed, listed=listed, scope=scope).settle(results)

        assert set(carry.dispositions) == set(committed.tables)
        carried = [c.table.fqn for c in carry.carried]
        assert len(carried) == len(set(carried))
        assert set(carried).isdisjoint(carry.removed)
        assert set(carried).isdisjoint(carry.missing_artifact)

    def test_only_a_table_the_scope_covers_and_the_listing_lost_is_removed(
        self,
        tmp_path: Path,
    ) -> None:
        committed = CommittedPrint.load(_first_run(tmp_path))
        extra = {
            fqn: CommittedTable(fqn=fqn, entry={"path": fqn.replace(".", "/")})
            for fqn in ("public.t0", "public.t1")
        }
        committed = replace(committed, tables={**committed.tables, **extra})
        # herbarium and t0 are covered but gone from the listing; t1 is outside the scope.
        scope = _Covers(
            include=(),
            exclude=(),
            covered=frozenset({CURATOR, HERBARIUM, "public.t0"}),
        )

        carry = plan_carry(committed, listed=[CURATOR], scope=scope).settle([])

        assert set(carry.removed) == {HERBARIUM, "public.t0"}

    def test_a_missing_artifact_outranks_every_carry_reason(self, tmp_path: Path) -> None:
        root = _first_run(tmp_path)
        (root / "public" / "herbarium" / "statistics.yaml").unlink()
        committed = CommittedPrint.load(root)
        scope = DiffSelectors(include=("*",), exclude=(), cli_include=(CURATOR,))

        carry = plan_carry(committed, listed=[CURATOR], scope=scope).settle([])

        assert carry.disposition(HERBARIUM) == "missing_artifact"
        assert carry.disposition(CURATOR) == "not_attempted"


class TestSameModelOnEveryRoute:
    """One committed table is the same `CommittedTable` whichever reason carries it."""

    def test_one_table_four_routes(self, tmp_path: Path) -> None:
        root = _first_run(tmp_path)
        reasons = {
            "fresh": ([HERBARIUM], [TableResult(HERBARIUM, "skipped", None, 0)]),
            "failed": ([HERBARIUM], [TableResult(HERBARIUM, "failed", "x", 0)]),
            "not_attempted": ([HERBARIUM], []),
            "out_of_scope": ([CURATOR], []),
        }
        scope = {
            "fresh": DiffSelectors(include=("*",), exclude=()),
            "failed": DiffSelectors(include=("*",), exclude=()),
            "not_attempted": DiffSelectors(include=("*",), exclude=()),
            "out_of_scope": DiffSelectors(include=("*",), exclude=(), cli_include=(CURATOR,)),
        }
        carried: dict[str, CarriedTable] = {}

        for reason, (listed, results) in reasons.items():
            committed = CommittedPrint.load(root)
            carry = plan_carry(committed, listed=listed, scope=scope[reason]).settle(results)
            carried[reason] = next(c for c in carry.carried if c.table.fqn == HERBARIUM)

        assert {c.reason for c in carried.values()} == set(reasons)
        assert len({repr(c.table) for c in carried.values()}) == 1

        entries = {
            reason: carried_entry(c, resolved_max_age_days=3) for reason, c in carried.items()
        }
        assert entries["out_of_scope"].max_age_days == 7
        assert {entries[r].max_age_days for r in ("fresh", "failed", "not_attempted")} == {3}
        assert len({replace(e, max_age_days=None) for e in entries.values()}) == 1


class TestFreshnessVerdict:
    SETTINGS = TableSettings(statistics=StatisticsConfig(), max_age_days=7)
    NOW = "2026-06-10T00:00:00Z"

    @pytest.fixture(autouse=True)
    def _conn(self, tmp_path: Path) -> None:
        self.conn = _conn_config(tmp_path)

    def _table(self, **recorded: Any) -> CommittedTable:
        values: dict[str, Any] = {
            "dbprint_version": DBPRINT_VERSION,
            "profiled_at": "2026-06-09T00:00:00Z",
            "statistics_params": statistics_params_dict(StatisticsConfig()),
            "profiling_params": profiling_params_dict(self.conn),
            **recorded,
        }

        return CommittedTable(fqn=CURATOR, entry={}, recorded=RecordedSettings(**values))

    def test_a_young_print_from_this_release_is_fresh(self) -> None:
        assert freshness(self._table(), self.SETTINGS, generated_at=self.NOW, conn=self.conn).fresh

    @pytest.mark.parametrize(
        ("recorded", "reason"),
        [
            ({"dbprint_version": "0.0.1"}, "written by dbprint 0.0.1"),
            ({"dbprint_version": None}, "written by dbprint None"),
            ({"profiled_at": "yesterday"}, "profiled_at unreadable"),
            ({"profiled_at": "2026-06-03T00:00:00Z"}, "7.0 days old, max_age_days 7"),
        ],
    )
    def test_each_clause_names_itself(self, recorded: dict[str, Any], reason: str) -> None:
        verdict = freshness(
            self._table(**recorded),
            self.SETTINGS,
            generated_at=self.NOW,
            conn=self.conn,
        )

        assert not verdict.fresh
        assert verdict.reason.startswith(reason)

    def test_no_committed_print_is_not_fresh(self) -> None:
        assert not freshness(None, self.SETTINGS, generated_at=self.NOW, conn=self.conn).fresh

    def test_an_offset_less_stamp_reads_as_utc(self) -> None:
        table = self._table(profiled_at="2026-06-09T23:00:00")

        assert freshness(table, self.SETTINGS, generated_at=self.NOW, conn=self.conn).fresh


class TestOffsetLessProfiledAt:
    def test_generate_reads_it_as_utc_and_skips(self, tmp_path: Path) -> None:
        root = _first_run(tmp_path)
        manifest = root / "manifest.yaml"
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))

        for entry in data["tables"].values():
            entry["profiled_at"] = entry["profiled_at"].removesuffix("Z")

        manifest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

        result = Engine(
            MockAdapter(_curator_fixture()),
            _conn_config(tmp_path),
            tmp_path,
        ).generate()

        assert {t.status for t in result.tables} == {"skipped"}


class TestRemovedAgreesWithTheDiff:
    def test_removed_is_what_the_diff_calls_removed(self, tmp_path: Path) -> None:
        """Both readings of "removed" land: the diff names it, and the fresh curator naming it
        is re-read rather than carried with an edge to a table that no longer exists.
        """

        root = _first_run(tmp_path)
        fixture = {fqn: t for fqn, t in _curator_fixture().items() if fqn != HERBARIUM}

        result = Engine(MockAdapter(fixture), _conn_config(tmp_path), tmp_path).generate()

        diff = yaml.safe_load((root / "diff.yaml").read_text())
        removed = {c["table"] for c in diff["changes"] if c["kind"] == "table_removed"}

        assert removed == {HERBARIUM}
        assert {t.fqn: t.status for t in result.tables} == {CURATOR: "ok"}


@dataclass(frozen=True)
class _Covers(DiffSelectors):
    covered: frozenset[str] = frozenset()

    def covers(self, fqn: str) -> bool:
        return fqn in self.covered


class TestTheManifestIsTheExtractedPlusTheCarried:
    def test_a_failed_tables_committed_entry_stays_beside_the_re_extracted_one(
        self,
        tmp_path: Path,
    ) -> None:
        root = _first_run(tmp_path)
        failing = _FailingAdapter(_curator_fixture(), HERBARIUM)

        Engine(failing, _conn_config(tmp_path), tmp_path).generate(GenerateRequest(force=True))

        assert set(yaml.safe_load((root / "manifest.yaml").read_text())["tables"]) == {
            CURATOR,
            HERBARIUM,
        }


class TestTheFailedAndNotAttemptedPaths:
    def test_a_failed_table_keeps_its_committed_entry(self, tmp_path: Path) -> None:
        root = _first_run(tmp_path)
        before = yaml.safe_load((root / "manifest.yaml").read_text())["tables"][HERBARIUM]
        failing = _FailingAdapter(_curator_fixture(), HERBARIUM)

        Engine(failing, _conn_config(tmp_path), tmp_path).generate(GenerateRequest(force=True))

        committed = CommittedPrint.load(root)
        after = yaml.safe_load((root / "manifest.yaml").read_text())["tables"][HERBARIUM]
        carry = plan_carry(
            committed,
            listed=[CURATOR, HERBARIUM],
            scope=DiffSelectors(include=("*",), exclude=()),
        ).settle([TableResult(HERBARIUM, "failed", "x", 0)])
        assert carry.disposition(HERBARIUM) == "failed"
        assert after == before

    def test_a_table_fail_fast_never_reached_leaves_the_print_untouched(
        self,
        tmp_path: Path,
    ) -> None:
        root = _first_run(tmp_path)
        before = (root / "manifest.yaml").read_text()
        failing = _FailingAdapter(_curator_fixture(), CURATOR)

        Engine(failing, _conn_config(tmp_path), tmp_path).generate(
            GenerateRequest(force=True, fail_fast=True),
        )

        carry = plan_carry(
            CommittedPrint.load(root),
            listed=[CURATOR, HERBARIUM],
            scope=DiffSelectors(include=("*",), exclude=()),
        ).settle([TableResult(CURATOR, "failed", "x", 0)])
        assert carry.disposition(HERBARIUM) == "not_attempted"
        assert (root / "manifest.yaml").read_text() == before
