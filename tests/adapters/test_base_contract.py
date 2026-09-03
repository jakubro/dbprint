"""Adapter contract tests - every concrete adapter must pass these.

Parameterised via the `adapter_factory` fixture in conftest.py: the mock adapter is one
parameter, and each DB-backed adapter extends the list.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from dbprint.adapters import (
    Adapter,
    BaseStats,
    BigqueryAdapter,
    ClickhouseAdapter,
    ColumnMeta,
    ColumnStats,
    CommentsMeta,
    DatabricksAdapter,
    ForeignKeyMeta,
    IndexMeta,
    RedshiftAdapter,
    SnowflakeAdapter,
    StatisticsConfig,
    TableCounts,
    TableMeta,
    TableScope,
    ValueCount,
)
from dbprint.adapters.base import row_count_or_none
from dbprint.spec.classification import classify
from dbprint.spec.statistics_matrix import FORBIDDEN_FIELDS, REQUIRED_FIELDS
from dbprint.spec.temporal_age import parse_instant
from tests.adapters.conftest import (
    WIDE_DISTINCT,
    WIDE_FUTURE_MAX,
    WIDE_ROW_COUNT,
    WIDE_TEMPORAL_MAX,
    WIDE_TEMPORAL_SPAN_DAYS,
)


# Method names required on every concrete adapter, written out rather than derived from the
# ABC: `test_the_declared_surface_is_the_abcs` fails when the two drift.
ABSTRACT_METHODS = {
    "connect",
    "close",
    "list_tables",
    "extract_ddl",
    "introspect_columns",
    "default_collation",
    "introspect_relationships",
    "introspect_indexes",
    "introspect_unique_keys",
    "introspect_physical_layout",
    "introspect_view_dependencies",
    "extract_comments",
    "estimate_row_count",
    "compute_base_statistics",
    "compute_column_statistics",
    "compute_null_patterns",
    "probe_grain",
    "probe_timeline",
    "compute_populated_windows",
    "probe_dependencies",
    "sample_values",
    "compute_key_sketch",
    "compute_normalized_cardinality",
    "execute_query",
}


class TestAbstractCoverage:
    def test_the_declared_surface_is_the_abcs(self) -> None:
        assert ABSTRACT_METHODS == set(Adapter.__abstractmethods__)


class TestListTables:
    def test_returns_table_meta_objects(self, adapter_factory: Callable[[], Adapter]) -> None:
        adapter = adapter_factory()
        tables = adapter.list_tables(include=["*"], exclude=[])
        assert tables, "Adapter must surface at least one table for the contract suite."
        assert all(isinstance(t, TableMeta) for t in tables)

    def test_fqn_namespace_path_consistency(self, adapter_factory: Callable[[], Adapter]) -> None:
        adapter = adapter_factory()

        for t in adapter.list_tables(include=["*"], exclude=[]):
            assert isinstance(t.namespace_path, tuple)
            assert all(isinstance(seg, str) for seg in t.namespace_path)
            # Last segment of namespace_path is the object name; should appear in fqn.
            assert t.namespace_path[-1] in t.fqn

    def test_type_is_canonical_literal(self, adapter_factory: Callable[[], Adapter]) -> None:
        adapter = adapter_factory()

        for t in adapter.list_tables(include=["*"], exclude=[]):
            assert t.type in {"table", "view", "matview"}

    def test_include_filter_applied(self, adapter_factory: Callable[[], Adapter]) -> None:
        adapter = adapter_factory()
        all_tables = adapter.list_tables(include=["*"], exclude=[])

        if len(all_tables) < 2:
            pytest.skip("Fixture has fewer than 2 tables; nothing to narrow.")

        target = all_tables[0]
        filtered = adapter.list_tables(include=[target.fqn], exclude=[])
        assert {t.fqn for t in filtered} == {target.fqn}

    def test_exclude_filter_applied(self, adapter_factory: Callable[[], Adapter]) -> None:
        adapter = adapter_factory()
        all_tables = adapter.list_tables(include=["*"], exclude=[])

        if len(all_tables) < 2:
            pytest.skip("Fixture has fewer than 2 tables; nothing to exclude.")

        target = all_tables[0]
        filtered = adapter.list_tables(include=["*"], exclude=[target.fqn])
        assert target.fqn not in {t.fqn for t in filtered}

    def test_empty_include_matches_nothing(self, adapter_factory: Callable[[], Adapter]) -> None:
        adapter = adapter_factory()
        assert adapter.list_tables(include=[], exclude=[]) == []


class TestExtractDdl:
    """Databricks and BigQuery are excluded: both statements are real on the vendor but absent
    from the local substrate (measured), leaving real DDL extraction to the live tier.
    """

    def test_returns_non_empty_string(self, adapter_factory: Callable[[], Adapter]) -> None:
        adapter = adapter_factory()

        if isinstance(adapter, DatabricksAdapter):
            pytest.skip("OSS Delta refuses SHOW CREATE TABLE outright; see class docstring.")

        if isinstance(adapter, BigqueryAdapter):
            pytest.skip("The emulator's COLUMNS carries no ddl field; see class docstring.")

        for t in adapter.list_tables(include=["*"], exclude=[]):
            ddl = adapter.extract_ddl(t.fqn)
            assert isinstance(ddl, str)
            assert ddl.strip(), f"DDL for {t.fqn} must not be empty."

    def test_trailing_newline_per_spec(self, adapter_factory: Callable[[], Adapter]) -> None:
        adapter = adapter_factory()

        if isinstance(adapter, DatabricksAdapter):
            pytest.skip("OSS Delta refuses SHOW CREATE TABLE outright; see class docstring.")

        if isinstance(adapter, BigqueryAdapter):
            pytest.skip("The emulator's COLUMNS carries no ddl field; see class docstring.")

        for t in adapter.list_tables(include=["*"], exclude=[]):
            ddl = adapter.extract_ddl(t.fqn)
            assert ddl.endswith("\n"), f"DDL for {t.fqn} must end with newline (SPEC 2.1.3)."


class TestIntrospect:
    def test_columns_are_column_meta(self, adapter_factory: Callable[[], Adapter]) -> None:
        adapter = adapter_factory()

        for t in _tables_with_columns(adapter):
            cols = adapter.introspect_columns(t.fqn)
            assert all(isinstance(c, ColumnMeta) for c in cols)

    def test_columns_are_ordinally_ordered(self, adapter_factory: Callable[[], Adapter]) -> None:
        adapter = adapter_factory()

        for t in _tables_with_columns(adapter):
            cols = adapter.introspect_columns(t.fqn)
            ordinals = [c.ordinal for c in cols]
            assert ordinals == sorted(ordinals), f"{t.fqn}: columns must be ordinal-ordered."

    def test_relationships_arrays_have_matching_lengths(
        self,
        adapter_factory: Callable[[], Adapter],
    ) -> None:
        adapter = adapter_factory()

        if isinstance(adapter, BigqueryAdapter):
            pytest.skip(
                "TABLE_CONSTRAINTS/KEY_COLUMN_USAGE are absent from the emulator (measured); "
                "left to the environment-gated live tier.",
            )

        for t in adapter.list_tables(include=["*"], exclude=[]):
            for fk in adapter.introspect_relationships(t.fqn):
                assert isinstance(fk, ForeignKeyMeta)
                assert len(fk.column) == len(fk.target_column), (
                    f"{t.fqn}: FK column/target_column lengths must match (SPEC 2.3.4)."
                )
                assert len(fk.column) >= 1

    def test_relationships_actions_are_canonical(
        self,
        adapter_factory: Callable[[], Adapter],
    ) -> None:
        adapter = adapter_factory()
        valid = {"NO ACTION", "CASCADE", "SET NULL", "SET DEFAULT", "RESTRICT"}

        if isinstance(adapter, BigqueryAdapter):
            pytest.skip(
                "TABLE_CONSTRAINTS/KEY_COLUMN_USAGE are absent from the emulator (measured); "
                "left to the environment-gated live tier.",
            )

        for t in adapter.list_tables(include=["*"], exclude=[]):
            for fk in adapter.introspect_relationships(t.fqn):
                assert fk.on_delete in valid
                assert fk.on_update in valid

    def test_indexes_are_index_meta(self, adapter_factory: Callable[[], Adapter]) -> None:
        adapter = adapter_factory()

        for t in adapter.list_tables(include=["*"], exclude=[]):
            for idx in adapter.introspect_indexes(t.fqn):
                assert isinstance(idx, IndexMeta)
                assert idx.columns, f"{t.fqn}: index {idx.name} must reference at least one column."

    def test_comments_shape(self, adapter_factory: Callable[[], Adapter]) -> None:
        adapter = adapter_factory()

        for t in adapter.list_tables(include=["*"], exclude=[]):
            comments = adapter.extract_comments(t.fqn)
            assert isinstance(comments, CommentsMeta)
            assert comments.table is None or isinstance(comments.table, str)
            assert isinstance(comments.columns, dict)


class TestUniqueKeys:
    """Declared PRIMARY KEY / UNIQUE groups, not a measurement - only what every substrate
    declares is asserted, since some lack either the primitive or a catalog to read it back.
    """

    def test_a_declared_primary_key_is_reported(
        self,
        adapter_factory: Callable[[], Adapter],
    ) -> None:
        adapter = adapter_factory()

        if isinstance(adapter, ClickhouseAdapter):
            pytest.skip(
                "ClickHouse's PRIMARY KEY is not a uniqueness constraint; see class docstring.",
            )

        if isinstance(adapter, DatabricksAdapter):
            pytest.skip(
                "Databricks declares no PRIMARY KEY on this substrate; see class docstring.",
            )

        if isinstance(adapter, BigqueryAdapter):
            pytest.skip(
                "TABLE_CONSTRAINTS/KEY_COLUMN_USAGE are absent from the emulator; see class "
                "docstring.",
            )

        for table in _tables_with_columns(adapter):
            groups = [g.columns for g in adapter.introspect_unique_keys(table.fqn)]
            assert ("id",) in groups, (
                f"{table.fqn} declares `id` primary key but it was not reported"
            )

    def test_the_primary_key_is_identified_as_one(
        self,
        adapter_factory: Callable[[], Adapter],
    ) -> None:
        """Not whichever qualifying group sorts first - the one the schema marked primary."""

        adapter = adapter_factory()

        if isinstance(adapter, ClickhouseAdapter):
            pytest.skip(
                "ClickHouse's PRIMARY KEY is not a uniqueness constraint; see class docstring.",
            )

        if isinstance(adapter, DatabricksAdapter):
            pytest.skip(
                "Databricks declares no PRIMARY KEY on this substrate; see class docstring.",
            )

        if isinstance(adapter, BigqueryAdapter):
            pytest.skip(
                "TABLE_CONSTRAINTS/KEY_COLUMN_USAGE are absent from the emulator; see class "
                "docstring.",
            )

        for table in _tables_with_columns(adapter):
            primary = [g.columns for g in adapter.introspect_unique_keys(table.fqn) if g.primary]
            assert primary == [("id",)], f"{table.fqn} reported primary keys {primary}"

    def test_every_reported_column_is_a_real_column(
        self,
        adapter_factory: Callable[[], Adapter],
    ) -> None:
        adapter = adapter_factory()

        if isinstance(adapter, BigqueryAdapter):
            pytest.skip(
                "TABLE_CONSTRAINTS/KEY_COLUMN_USAGE are absent from the emulator; see class "
                "docstring.",
            )

        for table in _tables_with_columns(adapter):
            names = {c.name for c in adapter.introspect_columns(table.fqn)}

            for group in adapter.introspect_unique_keys(table.fqn):
                assert group.columns, f"{table.fqn} reported an empty key group"
                assert set(group.columns) <= names, (
                    f"{table.fqn} reported unknown columns in {group.columns}"
                )

    def test_a_view_declares_no_keys(self, adapter_factory: Callable[[], Adapter]) -> None:
        adapter = adapter_factory()
        views = [t for t in adapter.list_tables(include=["*"], exclude=[]) if t.type == "view"]

        for view in views:
            assert adapter.introspect_unique_keys(view.fqn) == []


class TestProbeGrain:
    """SPEC 2.2.12's measured probe - one batched multi-column DISTINCT count per dialect.

    `curator` rows 11/12 share `(herbarium_id, is_active)` and row 13 differs on both, so the
    pair is genuinely not unique and a probe that always answered yes would fail here.
    """

    def test_a_genuinely_unique_pair_is_reported(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
        empty_stats_config: StatisticsConfig,
    ) -> None:
        _, factory = sql_adapter_factory
        adapter = factory()
        fqn, columns, counts = _curator_probe_context(adapter, empty_stats_config)

        found = adapter.probe_grain(fqn, columns, counts, (("herbarium_id", "email"),))

        assert found == (("herbarium_id", "email"),)

    def test_a_genuine_duplicate_pair_is_not_reported(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
        empty_stats_config: StatisticsConfig,
    ) -> None:
        _, factory = sql_adapter_factory
        adapter = factory()
        fqn, columns, counts = _curator_probe_context(adapter, empty_stats_config)

        found = adapter.probe_grain(fqn, columns, counts, (("herbarium_id", "is_active"),))

        assert found == ()

    def test_every_real_adapter_agrees(
        self,
        all_sql_adapters: dict[str, Adapter],
        empty_stats_config: StatisticsConfig,
    ) -> None:
        candidates = (("herbarium_id", "email"), ("herbarium_id", "is_active"))
        verdicts: dict[str, tuple[tuple[str, str], ...]] = {}

        for vendor, adapter in all_sql_adapters.items():
            fqn, columns, counts = _curator_probe_context(adapter, empty_stats_config)
            verdicts[vendor] = adapter.probe_grain(fqn, columns, counts, candidates)

        assert len(set(verdicts.values())) == 1, verdicts

    def test_no_candidates_returns_empty_without_a_statement(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
        empty_stats_config: StatisticsConfig,
    ) -> None:
        _, factory = sql_adapter_factory
        adapter = factory()
        fqn, columns, counts = _curator_probe_context(adapter, empty_stats_config)

        assert adapter.probe_grain(fqn, columns, counts, ()) == ()


class TestProbeTimeline:
    """SPEC 2.2.16's grouped bucket statement - `observed_at` takes `WIDE_DISTINCT` consecutive
    daily values, so a day-grain probe returns exactly that many buckets summing to the rows.
    """

    def test_day_grain_returns_one_bucket_per_distinct_day(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
        empty_stats_config: StatisticsConfig,
    ) -> None:
        _, factory = sql_adapter_factory
        adapter = factory()
        fqn, columns, counts = _viability_check_probe_context(adapter, empty_stats_config)

        buckets = adapter.probe_timeline(fqn, columns, counts, "observed_at", "day")

        assert len(buckets) == WIDE_DISTINCT
        assert sum(count for _, count in buckets) == WIDE_ROW_COUNT
        starts = [start for start, _ in buckets]
        assert starts == sorted(starts), starts

    def test_every_real_adapter_agrees_on_bucket_count(
        self,
        all_sql_adapters: dict[str, Adapter],
        empty_stats_config: StatisticsConfig,
    ) -> None:
        verdicts: dict[str, int] = {}

        for vendor, adapter in all_sql_adapters.items():
            fqn, columns, counts = _viability_check_probe_context(adapter, empty_stats_config)
            buckets = adapter.probe_timeline(fqn, columns, counts, "observed_at", "day")
            verdicts[vendor] = len(buckets)

        assert len(set(verdicts.values())) == 1, verdicts


class TestPopulatedWindows:
    """SPEC 2.2.4's grouped MIN/MAX statement - `label` is never null in the wide fixture, so
    its window over the `observed_at` anchor degenerates to that anchor's own full range.
    """

    def test_a_never_null_subject_windows_to_the_anchors_own_range(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
        empty_stats_config: StatisticsConfig,
    ) -> None:
        _, factory = sql_adapter_factory
        adapter = factory()
        fqn, columns, counts = _viability_check_probe_context(adapter, empty_stats_config)

        windows = adapter.compute_populated_windows(
            fqn,
            columns,
            counts,
            "observed_at",
            ("label",),
        )

        assert "label" in windows
        _, to_text = windows["label"]
        parsed_to = parse_instant(to_text)
        assert parsed_to is not None, to_text
        assert parsed_to == WIDE_TEMPORAL_MAX["observed_at"].replace(tzinfo=UTC), to_text

    def test_no_subject_columns_returns_empty_without_a_statement(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
        empty_stats_config: StatisticsConfig,
    ) -> None:
        _, factory = sql_adapter_factory
        adapter = factory()
        fqn, columns, counts = _viability_check_probe_context(adapter, empty_stats_config)

        assert adapter.compute_populated_windows(fqn, columns, counts, "observed_at", ()) == {}

    def test_every_real_adapter_agrees(
        self,
        all_sql_adapters: dict[str, Adapter],
        empty_stats_config: StatisticsConfig,
    ) -> None:
        verdicts: dict[str, datetime | None] = {}

        for vendor, adapter in all_sql_adapters.items():
            fqn, columns, counts = _viability_check_probe_context(adapter, empty_stats_config)
            windows = adapter.compute_populated_windows(
                fqn,
                columns,
                counts,
                "observed_at",
                ("label",),
            )
            verdicts[vendor] = parse_instant(windows["label"][1])

        assert len(set(verdicts.values())) == 1, verdicts


class TestProbeDependencies:
    """SPEC 2.2.13's measured probe - one batched multi-column DISTINCT count per dialect.

    Rows 11/12 of `curator` share `herbarium_id` and `is_active`, an exact dependency both ways;
    `viability_check.id` is unique and uncorrelated with `rank`, so an always-1.0 probe fails.
    """

    def test_an_exact_dependency_measures_strength_one(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
        empty_stats_config: StatisticsConfig,
    ) -> None:
        _, factory = sql_adapter_factory
        adapter = factory()
        fqn, columns, counts, base = _curator_dependency_context(adapter, empty_stats_config)

        strengths = adapter.probe_dependencies(
            fqn,
            columns,
            counts,
            base,
            (("herbarium_id", "is_active"),),
        )

        assert strengths == {("herbarium_id", "is_active"): 1.0}

    def test_an_uncorrelated_pair_measures_a_low_strength(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
        empty_stats_config: StatisticsConfig,
    ) -> None:
        _, factory = sql_adapter_factory
        adapter = factory()
        fqn, columns, counts, base = _viability_check_dependency_context(
            adapter,
            empty_stats_config,
        )

        strengths = adapter.probe_dependencies(fqn, columns, counts, base, (("rank", "id"),))

        assert strengths[("rank", "id")] < 0.1

    def test_every_real_adapter_agrees(
        self,
        all_sql_adapters: dict[str, Adapter],
        empty_stats_config: StatisticsConfig,
    ) -> None:
        candidates = (("herbarium_id", "is_active"),)
        verdicts: dict[str, dict[tuple[str, str], float]] = {}

        for vendor, adapter in all_sql_adapters.items():
            fqn, columns, counts, base = _curator_dependency_context(adapter, empty_stats_config)
            verdicts[vendor] = adapter.probe_dependencies(fqn, columns, counts, base, candidates)

        assert len({frozenset(v.items()) for v in verdicts.values()}) == 1, verdicts

    def test_no_candidates_returns_empty_without_a_statement(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
        empty_stats_config: StatisticsConfig,
    ) -> None:
        _, factory = sql_adapter_factory
        adapter = factory()
        fqn, columns, counts, base = _curator_dependency_context(adapter, empty_stats_config)

        assert adapter.probe_dependencies(fqn, columns, counts, base, ()) == {}


class TestBareUniqueIndexAgreement:
    """A bare `CREATE UNIQUE INDEX` backing no named constraint - `herbarium.code` is unique
    through an index alone, so engines with that primitive must agree and report no index.
    """

    def test_every_adapter_declares_it_unique(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
    ) -> None:
        _, factory = sql_adapter_factory
        adapter = factory()

        if isinstance(adapter, SnowflakeAdapter):
            pytest.skip("Snowflake has no unique-index primitive; see class docstring.")

        if isinstance(adapter, ClickhouseAdapter):
            pytest.skip("ClickHouse has no uniqueness primitive; see class docstring.")

        if isinstance(adapter, RedshiftAdapter):
            pytest.skip("Redshift has no CREATE INDEX at all; see class docstring.")

        if isinstance(adapter, DatabricksAdapter):
            pytest.skip("Databricks has no CREATE INDEX at all; see class docstring.")

        if isinstance(adapter, BigqueryAdapter):
            pytest.skip("BigQuery has no CREATE INDEX at all; see class docstring.")

        fqn = _herbarium_fqn(adapter)
        groups = {g.columns for g in adapter.introspect_unique_keys(fqn)}

        assert ("code",) in groups, f"{fqn} did not declare `code` unique"

    def test_every_adapter_excludes_it_from_indexes(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
    ) -> None:
        _, factory = sql_adapter_factory
        adapter = factory()

        if isinstance(adapter, SnowflakeAdapter):
            pytest.skip("Snowflake has no unique-index primitive; see class docstring.")

        fqn = _herbarium_fqn(adapter)
        index_columns = {idx.columns for idx in adapter.introspect_indexes(fqn)}

        assert ("code",) not in index_columns, f"{fqn} double-reported `code` as an index"


class TestEstimateRowCount:
    """The pre-flight read that decides whether a size-conditioned rule governs a table."""

    def test_every_object_answers_a_count_or_admits_it_has_none(
        self,
        adapter_factory: Callable[[], Adapter],
    ) -> None:
        """Views included - a catalog may hold no size for one, and that is an answer."""

        adapter = adapter_factory()

        for table in adapter.list_tables(include=["*"], exclude=[]):
            estimate = adapter.estimate_row_count(table.fqn)

            assert estimate is None or (isinstance(estimate, int) and estimate >= 0), (
                f"{table.fqn} estimated {estimate!r}"
            )


class TestRowCountSentinels:
    """Each catalog reports "no estimate" as a negative; the ABC reports it as None."""

    @pytest.mark.parametrize("sentinel", [-1, -1.0, -42])
    def test_a_negative_reading_is_unavailable(self, sentinel: float) -> None:
        assert row_count_or_none(sentinel) is None

    def test_zero_survives_as_a_known_size(self) -> None:
        """An analyzed empty table has a size, so it fails a bar instead of dodging one."""

        assert row_count_or_none(0) == 0

    def test_a_float_reading_narrows_to_int(self) -> None:
        assert row_count_or_none(250000.0) == 250_000


class TestStatistics:
    def test_returns_stats_keyed_by_column(
        self,
        adapter_factory: Callable[[], Adapter],
        empty_stats_config: StatisticsConfig,
    ) -> None:
        adapter = adapter_factory()

        for t in _tables_with_columns(adapter):
            cols = adapter.introspect_columns(t.fqn)
            _, stats = adapter.compute_statistics(t.fqn, cols, empty_stats_config, frozenset())
            assert isinstance(stats, dict)
            assert all(isinstance(s, ColumnStats) for s in stats.values())

    def test_stats_does_not_include_classification(
        self,
        adapter_factory: Callable[[], Adapter],
        empty_stats_config: StatisticsConfig,
    ) -> None:
        """Engine assigns classification; the adapter MUST NOT stamp it."""

        adapter = adapter_factory()

        for t in _tables_with_columns(adapter):
            cols = adapter.introspect_columns(t.fqn)
            _, stats = adapter.compute_statistics(t.fqn, cols, empty_stats_config, frozenset())

            for s in stats.values():
                assert getattr(s, "classification", None) is None

    def test_no_column_emits_a_field_its_own_resulting_classification_forbids(
        self,
        adapter_factory: Callable[[], Adapter],
        empty_stats_config: StatisticsConfig,
    ) -> None:
        """The general form of the `_drop_forbidden_fields` backstop: an ordinary run must never
        trip it - a field its classification forbids (SPEC 2.2.3) is an adapter bug, not routine.
        """

        adapter = adapter_factory()

        for t in _tables_with_columns(adapter):
            cols = adapter.introspect_columns(t.fqn)

            for name, s in adapter.compute_statistics(t.fqn, cols, empty_stats_config, frozenset())[
                1
            ].items():
                classification = _classification_of(s, empty_stats_config)

                for field in FORBIDDEN_FIELDS[classification]:
                    assert getattr(s, field, None) is None, (
                        f"{t.fqn}.{name}: {classification} forbids {field!r}, but the "
                        f"column carries a value for it"
                    )

    def test_null_count_and_rate_against_seeded_literal(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
        empty_stats_config: StatisticsConfig,
    ) -> None:
        """SPEC 2.2.4: `null_count`/`null_rate` against the seeded literal, not a range bound.

        `curator.seed_count` is one null of three rows, so the non-null denominator differs from
        `rows_scanned`; `curator.withdrawn_at` is null on every row. SQL-backed only.
        """

        _, factory = sql_adapter_factory
        stats = _curator_stats(factory(), empty_stats_config)

        seed_count = stats["seed_count"]
        assert seed_count.null_count == 1
        assert seed_count.null_rate == pytest.approx(1 / 3, abs=1e-6)

        withdrawn = stats["withdrawn_at"]
        assert withdrawn.null_count == 3
        assert withdrawn.null_rate == 1.0
        assert withdrawn.cardinality == 0

    def test_cardinality_ratio_in_unit_range(
        self,
        adapter_factory: Callable[[], Adapter],
        empty_stats_config: StatisticsConfig,
    ) -> None:
        adapter = adapter_factory()

        for t in _tables_with_columns(adapter):
            cols = adapter.introspect_columns(t.fqn)

            for s in adapter.compute_statistics(t.fqn, cols, empty_stats_config, frozenset())[
                1
            ].values():
                if s.cardinality_ratio is not None:
                    assert 0.0 <= s.cardinality_ratio <= 1.0

    def test_internal_and_emitted_cardinality_method_agree(
        self,
        adapter_factory: Callable[[], Adapter],
        empty_stats_config: StatisticsConfig,
    ) -> None:
        """`BaseStats.cardinality_method` decides `candidate_key_exception` (SPEC 4.2); the emitted
        `ColumnStats.cardinality_method` is what conformance recomputes it from, so both must agree.
        """

        adapter = adapter_factory()

        for t in _tables_with_columns(adapter):
            cols = adapter.introspect_columns(t.fqn)
            counts, base = adapter.compute_base_statistics(t.fqn, cols, empty_stats_config)
            stats = adapter.compute_column_statistics(
                t.fqn,
                cols,
                empty_stats_config,
                counts,
                base,
                frozenset(),
            )

            for name, b in base.items():
                emitted = stats[name].cardinality_method

                if emitted is None:
                    continue  # unsupported column - no cardinality on either side

                assert b.cardinality_method == emitted, (t.fqn, name, b.cardinality_method, emitted)

    def test_values_descending_by_count(
        self,
        adapter_factory: Callable[[], Adapter],
        empty_stats_config: StatisticsConfig,
    ) -> None:
        adapter = adapter_factory()

        for t in _tables_with_columns(adapter):
            cols = adapter.introspect_columns(t.fqn)

            for name, s in adapter.compute_statistics(t.fqn, cols, empty_stats_config, frozenset())[
                1
            ].items():
                if s.values is None:
                    classification = _classification_of(s, empty_stats_config)
                    assert "values" not in REQUIRED_FIELDS[classification], (
                        f"{t.fqn}.{name}: {classification} requires `values`, but the "
                        f"column carries none"
                    )

                    continue

                counts = [v.count for v in s.values]
                assert counts == sorted(counts, reverse=True), (
                    "values must be ordered by count DESC (SPEC 2.2.4)."
                )
                assert all(isinstance(v, ValueCount) for v in s.values)

    def test_values_coverage_matches_the_listed_counts(
        self,
        adapter_factory: Callable[[], Adapter],
        empty_stats_config: StatisticsConfig,
    ) -> None:
        """SPEC 2.2.4: coverage is the listed counts over the non-null rows."""

        adapter = adapter_factory()

        for t in _tables_with_columns(adapter):
            cols = adapter.introspect_columns(t.fqn)
            counts, stats = adapter.compute_statistics(
                t.fqn,
                cols,
                empty_stats_config,
                frozenset(),
            )

            for name, s in stats.items():
                if s.values is None:
                    classification = _classification_of(s, empty_stats_config)
                    assert "values" not in REQUIRED_FIELDS[classification], (
                        f"{t.fqn}.{name}: {classification} requires `values`, but the "
                        f"column carries none"
                    )

                    continue

                # `numeric`/`temporal` never carry `values_coverage` (SPEC 2.2.3);
                # `range`/`percentiles` is the proxy, since ColumnStats carries no classification.
                if s.range is not None or s.percentiles is not None:
                    assert s.values_coverage is None, (
                        f"{t.fqn}.{name}: numeric/temporal must not carry values_coverage"
                    )

                    continue

                assert s.values_coverage is not None, (
                    f"{t.fqn}.{name} lists values without coverage"
                )

                non_null = counts.rows_scanned - s.null_count

                if non_null <= 0:
                    # SPEC 2.2.7: no non-null rows still reports coverage 1.0.
                    assert s.values_coverage == 1.0, (
                        f"{t.fqn}.{name}: all-null column reports coverage {s.values_coverage}"
                    )
                    continue

                listed = sum(v.count for v in s.values)
                assert abs(round(listed / non_null, 6) - s.values_coverage) <= 1e-06, (
                    f"{t.fqn}.{name}: coverage {s.values_coverage} disagrees with {listed}/{non_null}"
                )

    def test_a_column_under_the_cap_is_enumerated_in_full(
        self,
        adapter_factory: Callable[[], Adapter],
        empty_stats_config: StatisticsConfig,
    ) -> None:
        """SPEC 2.2.4: cardinality decides completeness, not classification."""

        adapter = adapter_factory()

        for t in _tables_with_columns(adapter):
            cols = adapter.introspect_columns(t.fqn)
            counts, stats = adapter.compute_statistics(
                t.fqn,
                cols,
                empty_stats_config,
                frozenset(),
            )

            for name, s in stats.items():
                if s.values is None:
                    classification = _classification_of(s, empty_stats_config)
                    assert "values" not in REQUIRED_FIELDS[classification], (
                        f"{t.fqn}.{name}: {classification} requires `values`, but the "
                        f"column carries none"
                    )

                    continue

                if s.cardinality is None:
                    continue

                if s.cardinality <= empty_stats_config.top_n_values and counts.rows_scanned:
                    assert len(s.values) == s.cardinality, (
                        f"{t.fqn}.{name}: {s.cardinality} distinct values under the cap "
                        f"but only {len(s.values)} listed"
                    )
                    assert s.values_coverage == 1.0

    def test_values_tie_break_lexicographic(
        self,
        adapter_factory: Callable[[], Adapter],
        empty_stats_config: StatisticsConfig,
    ) -> None:
        """SPEC 2.2.4: ties at the cutoff broken by lexicographic order on value."""

        adapter = adapter_factory()

        for t in _tables_with_columns(adapter):
            cols = adapter.introspect_columns(t.fqn)

            for name, s in adapter.compute_statistics(t.fqn, cols, empty_stats_config, frozenset())[
                1
            ].items():
                if s.values is None:
                    classification = _classification_of(s, empty_stats_config)
                    assert "values" not in REQUIRED_FIELDS[classification], (
                        f"{t.fqn}.{name}: {classification} requires `values`, but the "
                        f"column carries none"
                    )

                    continue

                # Within each run of equal counts, values must ascend lexicographically.
                run_start = 0
                tvs = s.values

                for i in range(1, len(tvs) + 1):
                    if i == len(tvs) or tvs[i].count != tvs[run_start].count:
                        run = tvs[run_start:i]
                        run_values = [str(tv.value) for tv in run]
                        assert run_values == sorted(run_values), (
                            f"{t.fqn}: ties at count={tvs[run_start].count} must "
                            f"break lexicographically by value (SPEC 2.2.4)."
                        )
                        run_start = i


class TestEmptyDrawOverANonEmptyTable:
    """SPEC 2.2.7: a narrowed read drawing nothing publishes no per-column statistics.

    The guard is keyed on the read, not the table - `curator` still has rows.
    """

    def test_a_filter_matching_nothing_returns_no_column_stats(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
        empty_stats_config: StatisticsConfig,
    ) -> None:
        _, factory = sql_adapter_factory
        adapter = factory()
        fqn = next(
            t.fqn for t in adapter.list_tables(include=["*"], exclude=[]) if _is_curator(t.fqn)
        )
        columns = adapter.introspect_columns(fqn)
        scope = TableScope(filter="1 = 0")
        _, stats = adapter.compute_statistics(
            fqn,
            columns,
            empty_stats_config,
            frozenset(),
            scope=scope,
        )

        assert stats == {}

    def test_an_ordinary_scoped_read_still_returns_column_stats(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
        empty_stats_config: StatisticsConfig,
    ) -> None:
        """The control: a `scope` that narrows but still draws rows is unaffected."""

        _, factory = sql_adapter_factory
        adapter = factory()
        fqn = next(
            t.fqn for t in adapter.list_tables(include=["*"], exclude=[]) if _is_curator(t.fqn)
        )
        columns = adapter.introspect_columns(fqn)
        scope = TableScope(filter="1 = 1")
        _, stats = adapter.compute_statistics(
            fqn,
            columns,
            empty_stats_config,
            frozenset(),
            scope=scope,
        )

        assert set(stats) == {c.name for c in columns}


class TestSampling:
    def test_returns_list_bounded_by_n(self, adapter_factory: Callable[[], Adapter]) -> None:
        adapter = adapter_factory()

        for t in adapter.list_tables(include=["*"], exclude=[]):
            for col in adapter.introspect_columns(t.fqn):
                samples = adapter.sample_values(t.fqn, col.name, n=5)
                assert isinstance(samples, list)
                assert len(samples) <= 5


# Helpers


def _tables_with_columns(adapter: Adapter) -> list[TableMeta]:
    """Return only tables/matviews - plain views may have empty column sets per SPEC 1.4."""

    out: list[TableMeta] = []

    for t in adapter.list_tables(include=["*"], exclude=[]):
        if t.type in {"table", "matview"} and adapter.introspect_columns(t.fqn):
            out.append(t)

    return out


def _classification_of(s: ColumnStats, config: StatisticsConfig) -> str:
    """The SPEC 3.2 classification `s`'s own fields resolve to.

    `ColumnStats` carries no classification, so it is recomputed here from the same inputs
    `classify()` takes; `has_declared_fk=False` is safe, the two arms sharing one required set.
    """

    return classify(
        sql_type=s.sql_type,
        cardinality=s.cardinality,
        has_declared_fk=False,
        enumeration_threshold=config.enumeration_threshold,
    )


class TestDayCounts:
    """`span_days` counts whole elapsed days (SPEC 2.2.4), not calendar-date boundaries.

    The two quantities share a unit and disagree in both directions, so the fixture's three
    discriminator columns pin the rule rather than whichever one a dialect finds convenient.
    `range.max` is the raw material `max_age_days` is derived from, engine-side.
    """

    def test_span_days_counts_whole_elapsed_days(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
        empty_stats_config: StatisticsConfig,
    ) -> None:
        _, factory = sql_adapter_factory
        stats = _viability_check_stats(factory(), empty_stats_config)
        actual: dict[str, int | None] = {}

        for name in WIDE_TEMPORAL_SPAN_DAYS:
            rng = stats[name].range
            assert rng is not None, f"{name} produced no range"
            actual[name] = rng.span_days

        assert actual == WIDE_TEMPORAL_SPAN_DAYS

    def test_range_max_matches_the_seeded_maximum(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
        empty_stats_config: StatisticsConfig,
    ) -> None:
        """No adapter derives `max_age_days` - it reports the true maximum and stops."""

        _, factory = sql_adapter_factory
        stats = _viability_check_stats(factory(), empty_stats_config)

        for name, maximum in WIDE_TEMPORAL_MAX.items():
            rng = stats[name].range
            assert rng is not None, f"{name} produced no range"

            parsed = parse_instant(rng.max)
            assert parsed is not None, f"{name}: range.max={rng.max!r} did not parse"
            assert parsed == maximum.replace(tzinfo=UTC), (
                f"{name}: range.max={rng.max!r}, expected {maximum!r}"
            )

    def test_every_temporal_column_reached_the_temporal_branch(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
        empty_stats_config: StatisticsConfig,
    ) -> None:
        """Without this the assertion above could pass on absent statistics."""

        _, factory = sql_adapter_factory
        stats = _viability_check_stats(factory(), empty_stats_config)

        for name in WIDE_TEMPORAL_MAX:
            assert stats[name].range is not None, f"{name} produced no range"


def _viability_check_stats(adapter: Adapter, config: StatisticsConfig) -> dict[str, ColumnStats]:
    """Per-column statistics for the wide fixture table on any substrate."""

    fqn = next(
        t.fqn for t in adapter.list_tables(include=["*"], exclude=[]) if _is_viability_check(t.fqn)
    )
    columns = adapter.introspect_columns(fqn)
    _, stats = adapter.compute_statistics(fqn, columns, config, frozenset())

    return stats


def _is_viability_check(fqn: str) -> bool:
    return fqn.rsplit(".", 1)[-1] == "viability_check"


def _herbarium_fqn(adapter: Adapter) -> str:
    return next(
        t.fqn for t in adapter.list_tables(include=["*"], exclude=[]) if _is_herbarium(t.fqn)
    )


def _is_herbarium(fqn: str) -> bool:
    return fqn.rsplit(".", 1)[-1] == "herbarium"


def _curator_stats(adapter: Adapter, config: StatisticsConfig) -> dict[str, ColumnStats]:
    """Per-column statistics for the null-bearing fixture table on any substrate."""

    fqn = next(t.fqn for t in adapter.list_tables(include=["*"], exclude=[]) if _is_curator(t.fqn))
    columns = adapter.introspect_columns(fqn)
    _, stats = adapter.compute_statistics(fqn, columns, config, frozenset())

    return stats


def _is_curator(fqn: str) -> bool:
    return fqn.rsplit(".", 1)[-1] == "curator"


def _curator_probe_context(
    adapter: Adapter,
    config: StatisticsConfig,
) -> tuple[str, list[ColumnMeta], TableCounts]:
    """The `curator` FQN, its columns, and Phase A counts - what `probe_grain` needs to run."""

    fqn = next(t.fqn for t in adapter.list_tables(include=["*"], exclude=[]) if _is_curator(t.fqn))
    columns = adapter.introspect_columns(fqn)
    counts, _ = adapter.compute_base_statistics(fqn, columns, config)

    return fqn, columns, counts


def _viability_check_probe_context(
    adapter: Adapter,
    config: StatisticsConfig,
) -> tuple[str, list[ColumnMeta], TableCounts]:
    """The wide fixture's FQN, columns, and Phase A counts - what `probe_timeline` needs."""

    fqn = next(
        t.fqn for t in adapter.list_tables(include=["*"], exclude=[]) if _is_viability_check(t.fqn)
    )
    columns = adapter.introspect_columns(fqn)
    counts, _ = adapter.compute_base_statistics(fqn, columns, config)

    return fqn, columns, counts


def _curator_dependency_context(
    adapter: Adapter,
    config: StatisticsConfig,
) -> tuple[str, list[ColumnMeta], TableCounts, dict[str, BaseStats]]:
    """The `curator` FQN, its columns and Phase A output - what `probe_dependencies` needs."""

    fqn = next(t.fqn for t in adapter.list_tables(include=["*"], exclude=[]) if _is_curator(t.fqn))
    columns = adapter.introspect_columns(fqn)
    counts, base = adapter.compute_base_statistics(fqn, columns, config)

    return fqn, columns, counts, base


def _viability_check_dependency_context(
    adapter: Adapter,
    config: StatisticsConfig,
) -> tuple[str, list[ColumnMeta], TableCounts, dict[str, BaseStats]]:
    """The wide `viability_check` FQN, its columns, and Phase A output for `probe_dependencies`."""

    fqn = next(
        t.fqn for t in adapter.list_tables(include=["*"], exclude=[]) if _is_viability_check(t.fqn)
    )
    columns = adapter.introspect_columns(fqn)
    counts, base = adapter.compute_base_statistics(fqn, columns, config)

    return fqn, columns, counts, base


class TestCrossAdapterDayCountAgreement:
    """The same data profiled through any adapter yields the same day counts.

    The parameterised factory cannot express this: each adapter is its own test, so the
    numbers never meet.
    """

    def test_every_adapter_reports_the_same_span_days(
        self,
        all_sql_adapters: dict[str, Adapter],
        empty_stats_config: StatisticsConfig,
    ) -> None:
        spans = {
            vendor: _spans(_viability_check_stats(adapter, empty_stats_config))
            for vendor, adapter in all_sql_adapters.items()
        }

        assert len({tuple(sorted(v.items())) for v in spans.values()}) == 1, spans

    def test_every_adapter_reports_the_same_range_max(
        self,
        all_sql_adapters: dict[str, Adapter],
        empty_stats_config: StatisticsConfig,
    ) -> None:
        """`max_age_days` is derived engine-side from this value - agreement here is enough."""

        maxima = {
            vendor: _range_maxima(_viability_check_stats(adapter, empty_stats_config))
            for vendor, adapter in all_sql_adapters.items()
        }

        assert len({tuple(sorted(v.items())) for v in maxima.values()}) == 1, maxima


def _spans(stats: dict[str, ColumnStats]) -> dict[str, int | None]:
    out: dict[str, int | None] = {}

    for name in WIDE_TEMPORAL_SPAN_DAYS:
        rng = stats[name].range
        assert rng is not None, f"{name} produced no range"
        out[name] = rng.span_days

    return out


def _range_maxima(stats: dict[str, ColumnStats]) -> dict[str, datetime | None]:
    out: dict[str, datetime | None] = {}

    for name in WIDE_TEMPORAL_MAX:
        rng = stats[name].range
        assert rng is not None, f"{name} produced no range"
        out[name] = parse_instant(rng.max)

    return out


class TestFutureDatedColumns:
    """A column whose newest value has not happened yet still reports its true maximum.

    Clamping the derived age to 0 (SPEC 2.2.4) is engine-side, not the adapter's.
    """

    def test_range_max_still_carries_the_true_maximum(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
        empty_stats_config: StatisticsConfig,
    ) -> None:
        """The clamp is lossy, and this is where the lost information stays."""

        _, factory = sql_adapter_factory
        stats = _viability_check_stats(factory(), empty_stats_config)

        for name, maximum in WIDE_FUTURE_MAX.items():
            rng = stats[name].range
            assert rng is not None, f"{name} produced no range"
            assert str(rng.max).startswith(maximum.strftime("%Y-%m-%d")), (
                f"{name}: range.max={rng.max!r} does not carry the seeded maximum"
            )
