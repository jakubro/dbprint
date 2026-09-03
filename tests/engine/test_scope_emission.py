"""The `scope` block the engine writes for a narrowed read (SPEC 2.2.8) - the emission half."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from dbprint.adapters import (
    ColumnMeta,
    ColumnStats,
    CommentsMeta,
    MockAdapter,
    MockTable,
    TableScope,
    ValueCount,
)
from dbprint.adapters.base import RowCountMethod, TableCounts
from dbprint.config import ConnectionConfig, RuleConfig, StatisticsConfig, TableSettings
from dbprint.config.project import DiffConfig
from dbprint.conformance.statistics import check
from dbprint.engine import Engine
from dbprint.engine.orchestrator import (
    _EnrichedColumnStats,
    _serialize_statistics,
    _table_scope,
)


def _enriched() -> dict[str, _EnrichedColumnStats]:
    stats = ColumnStats(
        sql_type="integer",
        nullable=False,
        null_count=0,
        null_rate=0.0,
        cardinality=10,
        cardinality_ratio=0.1,
        cardinality_method="exact",
        values=tuple(ValueCount(value=str(i), count=10) for i in range(10)),
        values_coverage=1.0,
        distribution="uniform",
    )

    return {
        "bucket": _EnrichedColumnStats(stats=stats, classification="categorical", inferred=None),
    }


def _emit(
    rows_scanned: int | None,
    scope: TableScope | None,
    row_count: int = 1000,
    method: RowCountMethod = "exact",
) -> dict:
    counts = TableCounts(
        row_count=row_count,
        rows_scanned=row_count if rows_scanned is None else rows_scanned,
        row_count_method=method,
    )

    return yaml.safe_load(
        _serialize_statistics(
            "public.t",
            "table",
            "2026-07-31T13:00:00Z",
            counts,
            _enriched(),
            scope,
        ),
    )


class TestFullScan:
    def test_no_block_and_the_method_stays_exact(self) -> None:
        """Absence is the assertion that nothing was skipped."""

        payload = _emit(1000, None)

        assert "scope" not in payload
        assert payload["row_count_method"] == "exact"

    def test_a_scope_that_narrows_nothing_emits_nothing(self) -> None:
        payload = _emit(1000, TableScope())

        assert "scope" not in payload


class TestNarrowedRead:
    def test_filter_is_recorded_verbatim(self) -> None:
        payload = _emit(100, TableScope(filter="bucket = 0"))

        assert payload["scope"] == {"rows_scanned": 100, "filter": "bucket = 0"}
        assert "sample" not in payload["scope"]

    def test_sample_is_recorded_as_a_fraction(self) -> None:
        payload = _emit(250, TableScope(sample=0.25))

        assert payload["scope"] == {"rows_scanned": 250, "sample": 0.25}

    def test_row_count_method_stops_claiming_exact(self) -> None:
        """A narrowed read that took the catalog estimate says so."""

        payload = _emit(250, TableScope(sample=0.25), method="approximate")

        assert payload["row_count_method"] == "approximate"

    def test_row_count_still_describes_the_table(self) -> None:
        payload = _emit(100, TableScope(filter="bucket = 0"))

        assert payload["row_count"] == 1000
        assert payload["scope"]["rows_scanned"] == 100

    def test_an_empty_match_is_not_an_empty_table(self) -> None:
        payload = _emit(0, TableScope(filter="bucket = 99"))

        assert payload["row_count"] == 1000
        assert payload["scope"]["rows_scanned"] == 0

    @pytest.mark.parametrize(
        "scope",
        [TableScope(sample=0.5), TableScope(filter="bucket = 0")],
        ids=["sample", "filter"],
    )
    def test_the_emitted_block_is_conformant(self, scope: TableScope) -> None:
        payload = _emit(100, scope)
        codes = {i.code for i in check(payload, "statistics.yaml", "public.t")}

        assert not {c for c in codes if c.startswith("stats.scope-")}, codes


class TestPopulationMarker:
    """SPEC 2.2.8: every column of a scoped file echoes `rows_scanned`."""

    def test_every_column_carries_the_marker_when_scoped(self) -> None:
        payload = _emit(250, TableScope(sample=0.25))

        assert payload["columns"]["bucket"]["rows_scanned"] == 250

    def test_no_column_carries_the_marker_when_unscoped(self) -> None:
        payload = _emit(1000, None)

        assert "rows_scanned" not in payload["columns"]["bucket"]

    def test_a_scope_that_narrows_nothing_carries_no_marker_either(self) -> None:
        """The block itself is omitted here (SPEC 2.2.8's own asserts-nothing rule)."""

        payload = _emit(1000, TableScope())

        assert "rows_scanned" not in payload["columns"]["bucket"]

    def test_the_marker_equals_rows_scanned_not_row_count(self) -> None:
        payload = _emit(100, TableScope(filter="bucket = 0"), row_count=1000)

        assert payload["columns"]["bucket"]["rows_scanned"] == 100
        assert payload["row_count"] == 1000

    def test_a_scoped_column_missing_the_marker_is_flagged(self) -> None:
        payload = _emit(100, TableScope(filter="bucket = 0"))
        del payload["columns"]["bucket"]["rows_scanned"]
        codes = {i.code for i in check(payload, "statistics.yaml", "public.t")}

        assert "stats.population-marker-mismatch" in codes

    def test_a_scoped_column_disagreeing_with_scope_is_flagged(self) -> None:
        payload = _emit(100, TableScope(filter="bucket = 0"))
        payload["columns"]["bucket"]["rows_scanned"] = 99
        codes = {i.code for i in check(payload, "statistics.yaml", "public.t")}

        assert "stats.population-marker-mismatch" in codes

    def test_an_unscoped_column_carrying_the_marker_is_flagged(self) -> None:
        payload = _emit(1000, None)
        payload["columns"]["bucket"]["rows_scanned"] = 1000
        codes = {i.code for i in check(payload, "statistics.yaml", "public.t")}

        assert "stats.population-marker-mismatch" in codes


class TestRowCountMethodIsTheAdaptersStatement:
    """SPEC 2.2.1: the method says how the count was obtained, not whether the read narrowed -
    they diverge for a narrowed read that counted exactly.
    """

    def test_a_narrowed_read_that_counted_says_exact(self) -> None:
        payload = _emit(250, TableScope(sample=0.25), method="exact")

        assert payload["row_count_method"] == "exact"

    def test_a_narrowed_read_that_estimated_says_approximate(self) -> None:
        payload = _emit(250, TableScope(sample=0.25), method="approximate")

        assert payload["row_count_method"] == "approximate"

    def test_the_scope_block_survives_an_exact_count(self) -> None:
        """`scope` describes the read, the method describes the count."""

        payload = _emit(100, TableScope(filter="bucket = 0"), method="exact")

        assert payload["scope"] == {"rows_scanned": 100, "filter": "bucket = 0"}


class TestScannedExceedingCountIsBoundedByTheMethod:
    """SPEC 2.2.8 permits `rows_scanned` above `row_count` only for an estimate."""

    def test_an_estimate_that_undershot_is_accepted(self) -> None:
        payload = _emit(500, TableScope(sample=0.5), row_count=400, method="approximate")
        codes = {i.code for i in check(payload, "statistics.yaml", "public.t")}

        assert "stats.scope-rows-scanned-exceeds-row-count" not in codes

    def test_the_same_shape_under_an_exact_count_is_refused(self) -> None:
        """A counted number cannot undershoot, so stamping `exact` narrows the exception."""

        payload = _emit(500, TableScope(sample=0.5), row_count=400, method="exact")
        codes = {i.code for i in check(payload, "statistics.yaml", "public.t")}

        assert "stats.scope-rows-scanned-exceeds-row-count" in codes


class TestTheEngineStampsWhatTheAdapterReported:
    """The same rule through a real `Engine`: the value survives the trip from the adapter."""

    @pytest.mark.parametrize("method", ["exact", "approximate"])
    def test_the_method_reaches_the_artifact(
        self,
        tmp_path: Path,
        method: RowCountMethod,
    ) -> None:
        payload = _generate(tmp_path, rows_scanned=250, method=method)

        assert payload["row_count_method"] == method

    def test_the_scope_block_reports_the_fixtures_scanned_count(self, tmp_path: Path) -> None:
        """A narrowed read that counted exactly still describes the read it made."""

        payload = _generate(tmp_path, rows_scanned=250, method="exact")

        assert payload["row_count"] == 1000
        assert payload["scope"] == {"rows_scanned": 250, "sample": 0.25}

    def test_the_population_marker_reaches_the_column_through_a_real_run(
        self,
        tmp_path: Path,
    ) -> None:
        payload = _generate(tmp_path, rows_scanned=250, method="exact")

        assert payload["columns"]["bucket"]["rows_scanned"] == 250


def _generate(tmp_path: Path, rows_scanned: int, method: RowCountMethod) -> dict[str, Any]:
    conn = ConnectionConfig(
        name="primary",
        adapter="postgres",
        auto=True,
        output=tmp_path,
        include=("*",),
        exclude=(),
        max_age_days=7,
        statistics=StatisticsConfig(),
        diff=DiffConfig(),
        rules=(RuleConfig(sample=0.25),),
    )
    Engine(MockAdapter(_narrowed_fixture(rows_scanned, method)), conn, tmp_path).generate()

    return yaml.safe_load(
        (tmp_path / "primary" / "public" / "t" / "statistics.yaml").read_text(),
    )


def _narrowed_fixture(rows_scanned: int, method: RowCountMethod) -> dict[str, MockTable]:
    """A thousand-row table whose statistics were measured over a quarter of it."""

    return {
        "public.t": MockTable(
            type="table",
            namespace_path=("public", "t"),
            ddl="CREATE TABLE public.t (bucket integer);\n",
            columns=[
                ColumnMeta(
                    name="bucket",
                    sql_type="integer",
                    nullable=False,
                    default=None,
                    ordinal=1,
                ),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "bucket": ColumnStats(
                    sql_type="integer",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=10,
                    cardinality_ratio=0.04,
                    cardinality_method="exact",
                    values=tuple(ValueCount(value=str(i), count=25) for i in range(10)),
                    values_coverage=1.0,
                    distribution="uniform",
                ),
            },
            samples={},
            row_count=1000,
            rows_scanned=rows_scanned,
            row_count_method=method,
        ),
    }


def _conn(*rules: RuleConfig) -> ConnectionConfig:
    return ConnectionConfig(name="w", adapter="postgres", rules=rules)


class TestResolution:
    def test_settings_without_narrowing_mean_no_scope(self) -> None:
        settings = TableSettings(statistics=StatisticsConfig(), max_age_days=7)

        assert _table_scope(settings) is None

    def test_a_matching_rule_produces_a_scope(self) -> None:
        conn = _conn(RuleConfig(include=("public.*",), filter="a > 1"))
        scope = _table_scope(conn.settings_for("public.t"))

        assert scope is not None
        assert scope.filter == "a > 1"

    def test_a_non_matching_rule_leaves_the_table_unscoped(self) -> None:
        conn = _conn(RuleConfig(include=("other.*",), filter="a > 1"))

        assert _table_scope(conn.settings_for("public.t")) is None

    def test_a_rule_matching_everything_samples_every_table(self) -> None:
        conn = _conn(RuleConfig(sample=0.1))
        scope = _table_scope(conn.settings_for("anything.at.all"))

        assert scope is not None
        assert scope.sample == 0.1

    def test_a_later_rule_replaces_an_earlier_predicate(self) -> None:
        conn = _conn(
            RuleConfig(filter="everything"),
            RuleConfig(include=("public.*",), filter="narrow"),
        )
        scope = _table_scope(conn.settings_for("public.t"))

        assert scope is not None
        assert scope.filter == "narrow"
