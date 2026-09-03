"""A sampled profile reads one row set, not a fresh one per statement.

Every statistics query selects FROM the same source expression, but an unseeded sampling
construct draws again each time, so one `statistics.yaml` would describe different rows on a
table with no writes. The producer copies the draw once, so the tests split: the copied path,
where coherence is structural, and the seeded fallback where a coherent adapter's per-row seed
does the same job unmaterialized. Both run the whole profile twice.

An adapter whose fallback is NOT coherent (`SAMPLE_FALLBACK_COHERENT = False`) is refused before
it reaches these statements, so these tests skip it; the engine suite covers that refusal.

Neither can show that live Snowflake honours the seed or accepts the copy: duckdb's own seed
guarantee holds only single-threaded and models neither of Snowflake's constraints.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest

from dbprint.adapters import Adapter, ColumnMeta, StatisticsConfig, TableScope
from dbprint.adapters.errors import QueryFailed
from dbprint.cli.adapter_registry import get_adapter_class


SAMPLE = TableScope(sample=0.25)

# A quarter of the wide fixture falls under the default enumeration threshold, which would
# route every column to `categorical` and leave the numeric/temporal branches unreached.
SAMPLED_CONFIG = StatisticsConfig(enumeration_threshold=5)

# The construct each vendor draws its fraction with; carrying it twice draws twice.
DRAW_CLAUSES: dict[str, str] = {
    "postgres": "tablesample",
    "mysql": "rand(",
    "snowflake": "sample system (",
    "duckdb": "tablesample bernoulli(",
    "clickhouse": "sample ",
    "redshift": "random() <",
    "databricks": "tablesample (",
    "bigquery": "tablesample system (",
}

# The vendors whose unmaterialized fallback stays coherent, written out rather than read from the
# adapters: a wrongly-flipped flag would turn the tests below into silent skips, not failures.
COHERENT_FALLBACK_VENDORS = frozenset({"databricks", "duckdb", "postgres"})

# `CREATE TEMPORARY TABLE ... AS SELECT` is a Databricks-only feature OSS Spark rejects
# (measured), so only tests calling `materialize_scope` directly are affected here.
_NO_LOCAL_TEMP_TABLE = {"databricks"}

# The DDL keyword each vendor's copy step opens with - only BigQuery diverges, its copy being
# a genuine non-session table (`adapters.bigquery.stats.materialize`), self-expiring and
# replaceable rather than session-scoped.
_COPY_PREFIX: dict[str, str] = {"bigquery": "create or replace table"}


def _profile(
    adapter: Adapter,
    scope: TableScope | None,
    config: StatisticsConfig | None = None,
) -> tuple:
    """Run one full statistics pass over the wide fixture table."""

    table = next(t for t in adapter.list_tables(include=["*.viability_check"], exclude=[]))
    columns = adapter.introspect_columns(table.fqn)

    return adapter.compute_statistics(
        table.fqn,
        columns,
        config or StatisticsConfig(),
        frozenset(),
        None,
        scope,
    )


def test_which_vendors_claim_a_coherent_fallback_is_pinned() -> None:
    """A flip either way changes what every test below runs at all, so it is asserted directly.

    Requires no connection: this is what makes it the one check a vendor's absence cannot skip.
    """

    claimed = {v for v in DRAW_CLAUSES if get_adapter_class(v).SAMPLE_FALLBACK_COHERENT}

    assert claimed == COHERENT_FALLBACK_VENDORS


class TestRepeatability:
    def test_two_sampled_profiles_of_one_table_agree(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
    ) -> None:
        """The seed derives from the table's own name, which did not move."""

        vendor, factory = sql_adapter_factory

        if not get_adapter_class(vendor).SAMPLE_FALLBACK_COHERENT:
            pytest.skip(f"{vendor} is not SAMPLE_FALLBACK_COHERENT; see module docstring.")

        adapter = factory()

        try:
            first = _profile(adapter, SAMPLE)
            second = _profile(adapter, SAMPLE)
        finally:
            adapter.close()

        counts, _stats = first

        assert counts.rows_scanned, "the sample drew nothing; the comparison would be vacuous"
        assert 0 < counts.rows_scanned < counts.row_count, "the sample read the whole table"
        assert first == second

    def test_an_unsampled_profile_is_still_repeatable(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
    ) -> None:
        """The control: a full scan is repeatable by construction and must stay so."""

        _, factory = sql_adapter_factory
        adapter = factory()

        try:
            assert _profile(adapter, None) == _profile(adapter, None)
        finally:
            adapter.close()


class TestPercentilesComeFromTheRowsBesideThem:
    """A percentile drawn from its own sample can fall outside the reported range."""

    def test_no_percentile_falls_outside_its_range(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
    ) -> None:
        vendor, factory = sql_adapter_factory

        if not get_adapter_class(vendor).SAMPLE_FALLBACK_COHERENT:
            pytest.skip(f"{vendor} is not SAMPLE_FALLBACK_COHERENT; see module docstring.")

        adapter = factory()

        try:
            _, stats = _profile(adapter, SAMPLE, SAMPLED_CONFIG)
        finally:
            adapter.close()

        checked = 0

        for name, col in stats.items():
            if not col.percentiles or col.range is None or col.range.min is None:
                continue

            for key, value in col.percentiles.items():
                assert value is not None, f"{name}.{key} is null - the draw was short of it"
                assert col.range.min <= value <= col.range.max, (
                    f"{name}.{key}={value!r} sits outside "
                    f"{col.range.min!r}..{col.range.max!r}, so they came from different rows"
                )
                checked += 1

        assert checked, "no column carried percentiles; the assertion would be vacuous"


def test_both_statistics_phases_read_the_same_source(
    sql_adapter_factory: tuple[str, Callable[[], Adapter]],
) -> None:
    """The fallback path, where the copy is refused: the two phases build their FROM expression
    independently, so agreement holds only because the seed derives from the table's own name.
    A seed chosen per process or per call would leave the phases reading different rows.
    """

    from tests.adapters.test_dialect_guard import _install_recorder

    vendor, factory = sql_adapter_factory

    if not get_adapter_class(vendor).SAMPLE_FALLBACK_COHERENT:
        pytest.skip(f"{vendor} is not SAMPLE_FALLBACK_COHERENT; see module docstring.")

    adapter = factory()
    recorder = _install_recorder(adapter)

    try:
        table = next(t for t in adapter.list_tables(include=["*.viability_check"], exclude=[]))
        columns = adapter.introspect_columns(table.fqn)
        counts, base = adapter.compute_base_statistics(table.fqn, columns, SAMPLED_CONFIG, SAMPLE)
        phase_a = list(recorder.flattened())
        adapter.compute_column_statistics(
            table.fqn,
            columns,
            SAMPLED_CONFIG,
            counts,
            base,
            frozenset(),
            scope=SAMPLE,
        )
        phase_b = list(recorder.flattened())[len(phase_a) :]
    finally:
        adapter.close()

    drawn_a = _draws(phase_a, vendor)
    drawn_b = _draws(phase_b, vendor)

    assert drawn_a, f"{vendor}: phase A issued no sampled read; the check would be vacuous"
    assert drawn_b, f"{vendor}: phase B issued no sampled read; the check would be vacuous"
    assert len(drawn_a) == 1, f"{vendor}: phase A drew more than one way: {sorted(drawn_a)}"
    assert drawn_a == drawn_b, (
        f"{vendor}: the phases drew different samples.\n"
        f"  phase A: {sorted(drawn_a)}\n  phase B: {sorted(drawn_b)}"
    )


# The draw plus its arguments (rate, and seed where the dialect takes one), not the FROM.
DRAW_EXPRESSIONS: dict[str, str] = {
    "postgres": r"tablesample bernoulli\([^)]*\)(?: repeatable \([^)]*\))?",
    "mysql": r"rand\([^)]*\) < [0-9.]+",
    "snowflake": r"sample system \([^)]*\)(?: seed \([^)]*\))?",
    "duckdb": r"tablesample bernoulli\([^)]*\)(?: repeatable \([^)]*\))?",
    "databricks": r"tablesample \([^)]*\)(?: repeatable \([^)]*\))?",
}


def _draws(statements: list[str], vendor: str) -> set[str]:
    """Every distinct sampling expression the statements carry."""

    pattern = re.compile(DRAW_EXPRESSIONS[vendor])

    return {match for s in statements for match in pattern.findall(s)}


def test_no_statement_draws_its_sample_more_than_once(
    sql_adapter_factory: tuple[str, Callable[[], Adapter]],
) -> None:
    """A statement carrying the draw twice draws twice, whatever the seed says: a percentile
    shape embedding it once per percentile can land a percentile outside its own range, or
    return NULL when an offset exceeds a shorter later draw.
    """

    from tests.adapters.test_dialect_guard import _install_recorder

    vendor, factory = sql_adapter_factory

    if not get_adapter_class(vendor).SAMPLE_FALLBACK_COHERENT:
        pytest.skip(f"{vendor} is not SAMPLE_FALLBACK_COHERENT; see module docstring.")

    adapter = factory()
    recorder = _install_recorder(adapter)

    try:
        _profile(adapter, SAMPLE, SAMPLED_CONFIG)
    finally:
        adapter.close()

    clause = DRAW_CLAUSES[vendor]
    drawn = [s for s in recorder.flattened() if clause in s]
    offenders = [s for s in drawn if s.count(clause) > 1]

    assert drawn, f"{vendor}: no statement carried {clause!r}; the check would be vacuous"
    assert not offenders, (
        f"{vendor}: these statements name the draw more than once, so each reference "
        f"samples independently: {offenders}"
    )


def _copy_the_draw(adapter: Adapter) -> tuple[str, list[ColumnMeta], TableScope]:
    """The wide fixture's name and columns, plus the scope naming its copied draw."""

    table = next(t for t in adapter.list_tables(include=["*.viability_check"], exclude=[]))
    columns = adapter.introspect_columns(table.fqn)

    return table.fqn, columns, adapter.materialize_scope(table.fqn, SAMPLE)


class TestTheCopiedDraw:
    """Copying the drawn rows makes coherence structural: the sampler runs once per table."""

    def test_the_sampler_runs_once_for_a_whole_profile(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
    ) -> None:
        from tests.adapters.test_dialect_guard import _install_recorder

        vendor, factory = sql_adapter_factory

        if vendor in _NO_LOCAL_TEMP_TABLE:
            pytest.skip(f"{vendor} has no local `CREATE TEMPORARY TABLE`; see module docstring.")

        adapter = factory()
        recorder = _install_recorder(adapter)

        try:
            fqn, columns, scope = _copy_the_draw(adapter)

            assert scope.materialized is not None, f"{vendor}: the adapter declined to copy"

            adapter.compute_statistics(fqn, columns, SAMPLED_CONFIG, frozenset(), None, scope)
            statements = list(recorder.flattened())
            adapter.release_scope(fqn, scope)
        finally:
            adapter.close()

        drawn = [s for s in statements if DRAW_CLAUSES[vendor] in s]
        copy_prefix = _COPY_PREFIX.get(vendor, "create temporary table")

        assert len(drawn) == 1, f"{vendor}: the draw was evaluated {len(drawn)} times: {drawn}"
        assert drawn[0].startswith(copy_prefix), (
            f"{vendor}: the one draw is not the copy that makes it repeatable: {drawn[0]}"
        )

    def test_no_column_lists_more_rows_than_it_scanned(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
    ) -> None:
        """The identity a sampled print kept breaking, asserted on the adapter's output."""

        vendor, factory = sql_adapter_factory

        if vendor in _NO_LOCAL_TEMP_TABLE:
            pytest.skip(f"{vendor} has no local `CREATE TEMPORARY TABLE`; see module docstring.")

        adapter = factory()

        try:
            fqn, columns, scope = _copy_the_draw(adapter)
            counts, stats = adapter.compute_statistics(
                fqn,
                columns,
                SAMPLED_CONFIG,
                frozenset(),
                None,
                scope,
            )
            adapter.release_scope(fqn, scope)
        finally:
            adapter.close()

        checked = 0

        for name, col in stats.items():
            if col.values is None:
                continue

            listed = sum(v.count for v in col.values)
            non_null = counts.rows_scanned - col.null_count

            assert listed <= non_null, (
                f"{name}: lists {listed} rows against {non_null} non-null rows scanned, "
                f"so the two came from different draws"
            )

            # numeric/temporal carry `values` but never `values_coverage` (SPEC 2.2.3).
            if col.range is None and col.percentiles is None:
                assert col.values_coverage is not None and col.values_coverage <= 1

            checked += 1

        assert checked, "no column carried a value list; the assertion would be vacuous"

    def test_normalized_cardinality_reads_the_copy_not_a_fresh_draw(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
    ) -> None:
        """SPEC 2.2.4: unlike `sketch`, a scoped column stays eligible for this field - and
        it must read the same copy every other statistic already did, not redraw.
        """

        vendor, factory = sql_adapter_factory

        if vendor in _NO_LOCAL_TEMP_TABLE:
            pytest.skip(f"{vendor} has no local `CREATE TEMPORARY TABLE`; see module docstring.")

        adapter = factory()

        try:
            fqn, _columns, scope = _copy_the_draw(adapter)
            first = adapter.compute_normalized_cardinality(fqn, "label", scope)
            second = adapter.compute_normalized_cardinality(fqn, "label", scope)
            adapter.release_scope(fqn, scope)
        finally:
            adapter.close()

        assert first == second

    def test_releasing_the_copy_drops_it_and_twice_is_harmless(
        self,
        sql_adapter_factory: tuple[str, Callable[[], Adapter]],
    ) -> None:
        """Cleanup runs in a `finally`, so it must survive a second release after a failure."""

        vendor, factory = sql_adapter_factory

        if vendor in _NO_LOCAL_TEMP_TABLE:
            pytest.skip(f"{vendor} has no local `CREATE TEMPORARY TABLE`; see module docstring.")

        adapter = factory()

        try:
            fqn, columns, scope = _copy_the_draw(adapter)
            adapter.release_scope(fqn, scope)
            adapter.release_scope(fqn, scope)

            with pytest.raises(QueryFailed):
                adapter.compute_base_statistics(fqn, columns, SAMPLED_CONFIG, scope)
        finally:
            adapter.close()


def test_clickhouse_refuses_a_table_without_a_sampling_key(clickhouse_native_connection) -> None:
    """`SAMPLE` only works on a MergeTree table that declared `SAMPLE BY` at creation - caught
    from the catalog fact `list_tables` already read, not discovered by a failed `CREATE`.
    """

    from dbprint.adapters import ClickhouseAdapter
    from dbprint.adapters.clickhouse.adapter import SamplingKeyMissing

    cur = clickhouse_native_connection
    adapter = ClickhouseAdapter(
        {"host": "chdb", "database": "seedbank"},
        cursor_factory=lambda _p: cur,
    )
    adapter.connect()

    try:
        table = next(iter(adapter.list_tables(include=["*.unsampled"], exclude=[])))

        with pytest.raises(SamplingKeyMissing, match="no SAMPLE BY key"):
            adapter.materialize_scope(table.fqn, TableScope(sample=0.1))
    finally:
        adapter.close()
