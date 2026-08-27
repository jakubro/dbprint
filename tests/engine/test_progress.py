"""Engine progress-emission tests using MockAdapter.

The `on_progress` contract is additive across every phase: none of it alters GenerateResult.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from dbprint.adapters import (
    BaseStats,
    ColumnMeta,
    ColumnStats,
    CommentsMeta,
    Inferred,
    MockAdapter,
    MockTable,
    StatisticsConfig,
    TableCounts,
    TableScope,
    ValueCount,
)
from dbprint.adapters.base import ColumnProgress
from dbprint.config.project import ConnectionConfig, DiffConfig
from dbprint.engine import DiffRequest, Engine, GenerateRequest, ProgressEvent
from dbprint.spec.sketch import SketchKind


def _conn_config(tmp_path: Path) -> ConnectionConfig:
    return ConnectionConfig(
        name="primary",
        adapter="postgres",
        auto=False,
        output=tmp_path,
        include=("*",),
        exclude=(),
        max_age_days=7,
        statistics=StatisticsConfig(),
        diff=DiffConfig(),
    )


def _table(schema: str, name: str) -> MockTable:
    return MockTable(
        type="table",
        namespace_path=(schema, name),
        ddl=f"CREATE TABLE {schema}.{name} (id uuid PRIMARY KEY, status text);\n",
        columns=[
            ColumnMeta(name="id", sql_type="uuid", nullable=False, default=None, ordinal=1),
            ColumnMeta(name="status", sql_type="text", nullable=True, default=None, ordinal=2),
        ],
        relationships=[],
        indexes=[],
        comments=CommentsMeta(table=None, columns={}),
        stats={
            "id": ColumnStats(
                sql_type="uuid",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=100,
                cardinality_ratio=1.0,
                cardinality_method="exact",
                inferred=Inferred(candidate_key=True),
            ),
            "status": ColumnStats(
                sql_type="text",
                nullable=True,
                null_count=0,
                null_rate=0.0,
                cardinality=3,
                cardinality_ratio=0.03,
                cardinality_method="exact",
                values=(
                    ValueCount(value="a", count=40),
                    ValueCount(value="b", count=35),
                    ValueCount(value="c", count=25),
                ),
                values_coverage=1.0,
                distribution="uniform",
            ),
        },
        samples={"id": [f"00000000-0000-7000-8000-{i:012d}" for i in range(20)], "status": ["a"]},
        row_count=100,
    )


def _two_real_tables() -> dict[str, MockTable]:
    """Two genuinely-named seedbank objects, each with its own real column set."""

    return {
        "seedbank.taxon": MockTable(
            type="table",
            namespace_path=("seedbank", "taxon"),
            ddl=(
                "CREATE TABLE seedbank.taxon (\n"
                "    taxon_id integer NOT NULL,\n"
                "    scientific_name character varying(120) NOT NULL\n"
                ");\n"
            ),
            columns=[
                ColumnMeta(
                    name="taxon_id",
                    sql_type="integer",
                    nullable=False,
                    default=None,
                    ordinal=1,
                ),
                ColumnMeta(
                    name="scientific_name",
                    sql_type="character varying(120)",
                    nullable=False,
                    default=None,
                    ordinal=2,
                ),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "taxon_id": ColumnStats(
                    sql_type="integer",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=300,
                    cardinality_ratio=1.0,
                    cardinality_method="exact",
                    inferred=Inferred(candidate_key=True),
                ),
                "scientific_name": ColumnStats(
                    sql_type="character varying(120)",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=300,
                    cardinality_ratio=1.0,
                    cardinality_method="exact",
                    inferred=Inferred(candidate_key=True),
                ),
            },
            samples={
                "taxon_id": list(range(1, 21)),
                "scientific_name": ["Astro arenaria"],
            },
            row_count=300,
        ),
        "seedbank.vault": MockTable(
            type="table",
            namespace_path=("seedbank", "vault"),
            ddl=(
                "CREATE TABLE seedbank.vault (\n"
                "    vault_id integer NOT NULL,\n"
                "    shelf_code character varying(8) NOT NULL\n"
                ");\n"
            ),
            columns=[
                ColumnMeta(
                    name="vault_id",
                    sql_type="integer",
                    nullable=False,
                    default=None,
                    ordinal=1,
                ),
                ColumnMeta(
                    name="shelf_code",
                    sql_type="character varying(8)",
                    nullable=False,
                    default=None,
                    ordinal=2,
                ),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "vault_id": ColumnStats(
                    sql_type="integer",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=8,
                    cardinality_ratio=0.166667,
                    cardinality_method="exact",
                ),
                "shelf_code": ColumnStats(
                    sql_type="character varying(8)",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=6,
                    cardinality_ratio=0.125,
                    cardinality_method="exact",
                ),
            },
            samples={"vault_id": [1, 2, 3], "shelf_code": ["A", "B", "C"]},
            row_count=48,
        ),
    }


def _engine(tmp_path: Path, fixture: dict[str, MockTable]) -> Engine:
    return Engine(MockAdapter(fixture), _conn_config(tmp_path), tmp_path)


def _collect(tmp_path: Path, fixture: dict[str, MockTable]) -> list[ProgressEvent]:
    events: list[ProgressEvent] = []
    _engine(tmp_path, fixture).generate(GenerateRequest(force=True, on_progress=events.append))

    return events


class TestEventSequence:
    def test_start_precedes_terminal_per_table(self, tmp_path: Path) -> None:
        events = _collect(tmp_path, {"public.t": _table("public", "t")})

        start = next(
            i
            for i, e in enumerate(events)
            if e.fqn == "public.t" and e.phase == "extract" and e.status == "start"
        )
        done = next(i for i, e in enumerate(events) if e.fqn == "public.t" and e.status == "done")

        assert start < done

    def test_finalizing_pair_brackets_the_tail(self, tmp_path: Path) -> None:
        events = _collect(tmp_path, {"public.t": _table("public", "t")})
        finalizing = [e.status for e in events if e.phase == "finalizing"]

        assert finalizing == ["start", "done"]
        assert events[-1].phase == "finalizing"
        assert events[-1].status == "done"

    def test_per_column_statistics_ticks(self, tmp_path: Path) -> None:
        events = _collect(tmp_path, {"public.t": _table("public", "t")})
        columns = [
            (e.column, e.column_index, e.column_total) for e in events if e.phase == "statistics"
        ]

        assert columns == [("id", 1, 2), ("status", 2, 2)]

    def test_index_total_scoped_per_connection(self, tmp_path: Path) -> None:
        fixture = _two_real_tables()
        events = _collect(tmp_path, fixture)
        starts = [
            (e.index, e.total) for e in events if e.phase == "extract" and e.status == "start"
        ]

        assert starts == [(1, 2), (2, 2)]

    def test_prepass_precedes_the_table_loop(self, tmp_path: Path) -> None:
        """connecting -> listing -> inventory: bracketed start/done pairs, all before extract."""

        events = _collect(tmp_path, {"public.t": _table("public", "t")})
        prepass = [e for e in events if e.phase in ("connecting", "listing", "inventory")]

        assert [(e.phase, e.status) for e in prepass] == [
            ("connecting", "start"),
            ("connecting", "done"),
            ("listing", "start"),
            ("listing", "done"),
            ("inventory", "start"),
            ("inventory", "start"),  # the one object's tick
            ("inventory", "done"),
        ]

        table_start = next(
            i
            for i, e in enumerate(events)
            if e.fqn == "public.t" and e.phase == "extract" and e.status == "start"
        )
        last_prepass = max(
            i for i, e in enumerate(events) if e.phase in ("connecting", "listing", "inventory")
        )

        assert last_prepass < table_start

    def test_inventory_ticks_advance_through_the_object_count(self, tmp_path: Path) -> None:
        fixture = _two_real_tables()
        events = _collect(tmp_path, fixture)
        ticks = [(e.index, e.total) for e in events if e.phase == "inventory" and e.index > 0]

        assert ticks == [(1, 2), (2, 2)]

    def test_listing_total_is_unknown_at_start_and_real_at_done(self, tmp_path: Path) -> None:
        fixture = _two_real_tables()
        events = _collect(tmp_path, fixture)
        listing = [(e.status, e.total) for e in events if e.phase == "listing"]

        assert listing == [("start", 0), ("done", 2)]

    def test_infer_relationships_off_emits_no_inventory_phase(self, tmp_path: Path) -> None:
        fixture = {"public.t": _table("public", "t")}
        conn_config = replace(_conn_config(tmp_path), infer_relationships=False)
        events: list[ProgressEvent] = []
        Engine(MockAdapter(fixture), conn_config, tmp_path).generate(
            GenerateRequest(force=True, on_progress=events.append),
        )

        assert not any(e.phase == "inventory" for e in events)
        assert any(e.phase == "connecting" for e in events)
        assert any(e.phase == "listing" for e in events)


class TestTerminalStatuses:
    def test_failed_table_emits_failed_event_and_continues(self, tmp_path: Path) -> None:
        fixture = {"public.good": _table("public", "good"), "public.bad": _table("public", "bad")}
        adapter = _RaisingAdapter(fixture, "public.bad")
        events: list[ProgressEvent] = []
        result = Engine(adapter, _conn_config(tmp_path), tmp_path).generate(
            GenerateRequest(force=True, on_progress=events.append),
        )

        failed = [e for e in events if e.status == "failed"]

        assert [e.fqn for e in failed] == ["public.bad"]
        assert "stats boom" in (failed[0].error or "")
        assert result.summary.ok == 1
        assert result.summary.failed == 1

    def test_all_skipped_still_advances_and_finalizes(self, tmp_path: Path) -> None:
        fixture = {"public.t": _table("public", "t")}
        engine = _engine(tmp_path, fixture)
        engine.generate(GenerateRequest(force=True))

        events: list[ProgressEvent] = []
        result = engine.generate(GenerateRequest(on_progress=events.append))

        terminal = [e.status for e in events if e.fqn == "public.t" and e.status != "start"]

        assert terminal == ["skipped"]
        assert result.summary.skipped == 1
        assert events[-1].phase == "finalizing"


class TestSketchPass:
    """`_write_key_sketches`'s own emission - `_two_real_tables()`'s candidate_key and
    low-cardinality columns are eligible under SPEC 2.2.14's MAY-set without any FK.
    """

    def test_sketch_phase_brackets_the_pass_with_no_fqn(self, tmp_path: Path) -> None:
        events = _collect(tmp_path, _two_real_tables())
        sketch = [e for e in events if e.phase == "sketch"]

        assert sketch, "fixture has eligible columns; expected at least one sketch event"
        assert sketch[0].status == "start"
        assert sketch[0].fqn is None
        assert sketch[-1].status in ("done", "failed")
        assert sketch[-1].fqn is None

    def test_sketch_tables_visit_in_sorted_fqn_order(self, tmp_path: Path) -> None:
        events = _collect(tmp_path, _two_real_tables())
        table_starts = [
            e.fqn
            for e in events
            if e.phase == "sketch" and e.status == "start" and e.fqn is not None
        ]

        assert table_starts
        assert table_starts == sorted(table_starts)

    def test_sketch_column_events_share_their_tables_index_and_total(
        self,
        tmp_path: Path,
    ) -> None:
        """The bar must not stutter within a table - every column event it carries stays
        pinned to that table's own (index, total), never the column's own count.
        """

        events = _collect(tmp_path, _two_real_tables())
        by_fqn: dict[str, set[tuple[int, int]]] = {}

        for e in events:
            if e.phase == "sketch" and e.fqn is not None:
                by_fqn.setdefault(e.fqn, set()).add((e.index, e.total))

        assert by_fqn
        for fqn, pairs in by_fqn.items():
            assert len(pairs) == 1, f"{fqn}: (index, total) moved within one table's events"

    def test_sketch_column_index_increments_from_one_per_table(self, tmp_path: Path) -> None:
        events = _collect(tmp_path, _two_real_tables())
        by_fqn: dict[str, list[int]] = {}

        for e in events:
            if e.phase == "sketch" and e.column is not None and e.fqn is not None:
                assert e.column_index is not None  # always paired with `column` by the emitter
                by_fqn.setdefault(e.fqn, []).append(e.column_index)

        assert by_fqn, "expected at least one column-level sketch event"

        for fqn, indices in by_fqn.items():
            assert indices == list(range(1, len(indices) + 1)), fqn

    def test_sketch_table_terminal_event_carries_elapsed_ms(self, tmp_path: Path) -> None:
        events = _collect(tmp_path, _two_real_tables())
        terminals = [
            e
            for e in events
            if e.phase == "sketch" and e.fqn is not None and e.status in ("done", "failed")
        ]

        assert terminals
        assert all(e.elapsed_ms is not None for e in terminals)

    def test_no_eligible_column_emits_no_sketch_event(self, tmp_path: Path) -> None:
        """A high-cardinality, non-key, non-FK column is not sketch-eligible at all."""

        fixture = {
            "public.wide": MockTable(
                type="table",
                namespace_path=("public", "wide"),
                ddl="CREATE TABLE public.wide (payload text);\n",
                columns=[
                    ColumnMeta(
                        name="payload",
                        sql_type="text",
                        nullable=True,
                        default=None,
                        ordinal=1,
                    ),
                ],
                relationships=[],
                indexes=[],
                comments=CommentsMeta(table=None, columns={}),
                stats={
                    "payload": ColumnStats(
                        sql_type="text",
                        nullable=True,
                        null_count=0,
                        null_rate=0.0,
                        cardinality=2000,
                        # Below 1.0 on purpose - a ratio of 1.0 is itself a candidate-key signal
                        # the engine infers independently, making this column eligible anyway.
                        cardinality_ratio=0.4,
                        cardinality_method="exact",
                    ),
                },
                samples={"payload": ["x"]},
                row_count=5000,
            ),
        }
        events = _collect(tmp_path, fixture)

        assert not any(e.phase == "sketch" for e in events)

    def test_a_failed_sketch_query_fails_only_its_own_table(self, tmp_path: Path) -> None:
        """Run-all-then-report: one column's query failure does not stop the other table."""

        fixture = _two_real_tables()
        adapter = _SketchRaisingAdapter(fixture, "seedbank.taxon", "taxon_id")
        events: list[ProgressEvent] = []
        Engine(adapter, _conn_config(tmp_path), tmp_path).generate(
            GenerateRequest(force=True, on_progress=events.append),
        )

        terminals = {
            e.fqn: e.status
            for e in events
            if e.phase == "sketch" and e.fqn is not None and e.status in ("done", "failed")
        }

        assert terminals.get("seedbank.taxon") == "failed"
        assert terminals.get("seedbank.vault") == "done"


class TestSafetyAndEdges:
    def test_throwing_callback_never_aborts_extraction(self, tmp_path: Path) -> None:
        def boom(_event: ProgressEvent) -> None:
            raise RuntimeError("renderer exploded")

        fixture = {"public.t": _table("public", "t")}
        result = _engine(tmp_path, fixture).generate(GenerateRequest(force=True, on_progress=boom))

        assert result.summary.ok == 1
        assert (tmp_path / "primary" / "manifest.yaml").is_file()

    def test_zero_tables_still_runs_the_prepass_then_finalizes(self, tmp_path: Path) -> None:
        events = _collect(tmp_path, {})

        assert [e.phase for e in events] == [
            "connecting",
            "connecting",
            "listing",
            "listing",
            "inventory",
            "inventory",
            "finalizing",
            "finalizing",
        ]
        assert [e.status for e in events] == ["start", "done"] * 4
        assert all(e.fqn is None for e in events)

    def test_none_callback_matches_collected_result(self, tmp_path: Path) -> None:
        fixture = {"public.t": _table("public", "t")}
        silent = _engine(tmp_path / "a", fixture).generate(GenerateRequest(force=True))
        observed = _engine(tmp_path / "b", fixture).generate(
            GenerateRequest(force=True, on_progress=lambda _e: None),
        )

        assert silent.summary == observed.summary
        assert silent.exit_code == observed.exit_code


class TestComputeDiffProgress:
    def test_diff_emits_per_table_events_and_finalizes(self, tmp_path: Path) -> None:
        fixture = {"public.t": _table("public", "t")}
        engine = _engine(tmp_path, fixture)
        engine.generate(GenerateRequest(force=True))  # seed the baseline manifest

        events: list[ProgressEvent] = []
        engine.compute_diff(DiffRequest(on_progress=events.append))

        assert any(e.phase == "connecting" and e.status == "done" for e in events)
        assert any(
            e.fqn == "public.t" and e.phase == "extract" and e.status == "start" for e in events
        )
        assert any(e.fqn == "public.t" and e.status == "done" for e in events)
        assert events[-1].phase == "finalizing"
        assert events[-1].status == "done"

    def test_none_callback_matches_observed(self, tmp_path: Path) -> None:
        fixture = {"public.t": _table("public", "t")}
        engine = _engine(tmp_path, fixture)
        engine.generate(GenerateRequest(force=True))

        silent = engine.compute_diff(DiffRequest())
        observed = engine.compute_diff(DiffRequest(on_progress=lambda _e: None))

        assert silent.exit_code == observed.exit_code
        assert silent.target_scanned_tables == observed.target_scanned_tables

    def test_diff_never_emits_a_sketch_event(self, tmp_path: Path) -> None:
        """`compute_diff` shares the catalogue pre-pass but never writes artifacts, so it must
        never reach the key-sketch pass - even on a fixture with eligible columns.
        """

        fixture = _two_real_tables()
        engine = _engine(tmp_path, fixture)
        engine.generate(GenerateRequest(force=True))  # seed the baseline manifest

        events: list[ProgressEvent] = []
        engine.compute_diff(DiffRequest(on_progress=events.append))

        assert not any(e.phase == "sketch" for e in events)


class _RaisingAdapter(MockAdapter):
    """MockAdapter that raises in compute_column_statistics (Phase B) for one FQN.

    Phase B runs after sampling and classification, so the fault lands past that work.
    """

    def __init__(self, fixture: dict[str, MockTable], bad_fqn: str) -> None:
        super().__init__(fixture)
        self._bad_fqn = bad_fqn

    def compute_column_statistics(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        config: StatisticsConfig,
        counts: TableCounts,
        base: dict[str, BaseStats],
        fk_source_columns: frozenset[str],
        *,
        suppress_values: frozenset[str] = frozenset(),
        on_column: ColumnProgress | None = None,
        scope: TableScope | None = None,
    ) -> dict[str, ColumnStats]:
        if fqn == self._bad_fqn:
            raise RuntimeError("stats boom")

        return super().compute_column_statistics(
            fqn,
            columns,
            config,
            counts,
            base,
            fk_source_columns,
            suppress_values=suppress_values,
            on_column=on_column,
            scope=scope,
        )


class _SketchRaisingAdapter(MockAdapter):
    """Raises for one table's one column."""

    def __init__(self, fixture: dict[str, MockTable], bad_fqn: str, bad_column: str) -> None:
        super().__init__(fixture)
        self._bad_fqn = bad_fqn
        self._bad_column = bad_column

    def compute_key_sketch(
        self,
        fqn: str,
        column: str,
        sql_type: str,
        kind: SketchKind,
        k: int,
    ) -> tuple[int, ...]:
        if fqn == self._bad_fqn and column == self._bad_column:
            raise RuntimeError("sketch boom")

        return super().compute_key_sketch(fqn, column, sql_type, kind, k)
