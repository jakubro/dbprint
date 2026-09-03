"""Source expressions each adapter builds for a narrowed read (SPEC 2.2.8).

A scope carries a predicate or a fraction, never both, so each adapter has exactly two
shapes; where the fraction may bind differs per dialect, hence no shared query builder.
"""

from __future__ import annotations

import pytest

from dbprint.adapters import TableScope
from dbprint.adapters.base import seed_from_fqn
from dbprint.adapters.mysql.stats import _source as mysql_source
from dbprint.adapters.postgres import looks_like as postgres_looks_like
from dbprint.adapters.postgres.stats import _source as postgres_source
from dbprint.adapters.snowflake.identity import Identity
from dbprint.adapters.snowflake.stats import _source as snowflake_source


SUB_DRAW_SEED = 777


def _snowflake(scope: TableScope | None, seed: int | None = None) -> str:
    return snowflake_source(Identity(("db", "sch", "t"), {}), scope, seed)


class TestFullScan:
    @pytest.mark.parametrize("scope", [None, TableScope()], ids=["none", "empty"])
    def test_every_adapter_reads_the_bare_table(self, scope: TableScope | None) -> None:
        assert postgres_source('"public"."t"', scope) == '"public"."t"'
        assert mysql_source("`db`.`t`", scope) == "`db`.`t`"
        assert "SELECT" not in _snowflake(scope)


class TestFilter:
    def test_postgres_wraps_the_predicate(self) -> None:
        out = postgres_source('"public"."t"', TableScope(filter="a > 1"))

        assert out == '(SELECT * FROM "public"."t" WHERE a > 1) AS dbprint_scoped'

    def test_mysql_wraps_the_predicate(self) -> None:
        out = mysql_source("`db`.`t`", TableScope(filter="a > 1"))

        assert out == "(SELECT * FROM `db`.`t` WHERE (a > 1)) AS dbprint_scoped"

    def test_snowflake_wraps_the_predicate(self) -> None:
        assert "WHERE a > 1" in _snowflake(TableScope(filter="a > 1"))


class TestSample:
    def test_postgres_binds_tablesample_to_the_base_table(self) -> None:
        """TABLESAMPLE takes a base table, and a fraction alone has nothing to wrap."""

        out = postgres_source('"public"."t"', TableScope(sample=0.01))

        assert out == '"public"."t" TABLESAMPLE BERNOULLI(1.0)'

    def test_mysql_samples_with_rand_because_it_has_no_tablesample(self) -> None:
        out = mysql_source("`db`.`t`", TableScope(sample=0.25))

        assert out == "(SELECT * FROM `db`.`t` WHERE RAND() < 0.25) AS dbprint_scoped"
        assert "TABLESAMPLE" not in out

    def test_snowflake_binds_sample_to_the_base_table(self) -> None:
        """A seed is unavailable on a view or a subquery, so a wrapper forfeits repeatability."""

        assert _snowflake(TableScope(sample=0.01)) == '"db"."sch"."t" SAMPLE SYSTEM (1.0)'


class TestSeededSample:
    """Every statement in one table's profile reads the same rows, or none do."""

    SEED = 12345

    def test_postgres_takes_the_seed_as_repeatable(self) -> None:
        out = postgres_source('"public"."t"', TableScope(sample=0.01), self.SEED)

        assert out == '"public"."t" TABLESAMPLE BERNOULLI(1.0) REPEATABLE (12345)'

    def test_mysql_seeds_the_rand_draw(self) -> None:
        out = mysql_source("`db`.`t`", TableScope(sample=0.25), self.SEED)

        assert out == "(SELECT * FROM `db`.`t` WHERE RAND(12345) < 0.25) AS dbprint_scoped"

    def test_snowflake_names_a_method_the_seed_reaches(self) -> None:
        """SAMPLE defaults to BERNOULLI, and SEED applies to SYSTEM and BLOCK alone."""

        out = _snowflake(TableScope(sample=0.01), self.SEED)

        assert out == '"db"."sch"."t" SAMPLE SYSTEM (1.0) SEED (12345)'

    def test_a_filtered_scope_carries_no_seed(self) -> None:
        """A predicate is already deterministic; there is no draw to repeat."""

        scope = TableScope(filter="a > 1")

        assert str(self.SEED) not in postgres_source('"public"."t"', scope, self.SEED)
        assert str(self.SEED) not in mysql_source("`db`.`t`", scope, self.SEED)
        assert str(self.SEED) not in _snowflake(scope, self.SEED)

    def test_an_unscoped_read_is_untouched_by_a_seed(self) -> None:
        assert postgres_source('"public"."t"', None, self.SEED) == '"public"."t"'
        assert mysql_source("`db`.`t`", None, self.SEED) == "`db`.`t`"
        assert _snowflake(None, self.SEED) == '"db"."sch"."t"'

    def test_two_tables_draw_independently(self) -> None:
        left = seed_from_fqn("garden.seedbank.accession", 2**31)
        right = seed_from_fqn("garden.seedbank.germination_trial", 2**31)

        assert left != right

    @pytest.mark.parametrize("modulus", [2**31])
    def test_the_seed_lands_inside_the_engines_accepted_range(self, modulus: int) -> None:
        """A value outside it is truncated or rejected, depending on the vendor."""

        seed = seed_from_fqn("garden.seedbank.accession", modulus)

        assert 0 <= seed < modulus


class TestTheTwoNarrowingsAreExclusive:
    """SPEC 2.2.8: a table is narrowed by a predicate or by a fraction, never both."""

    def test_a_scope_carrying_both_is_refused(self) -> None:
        with pytest.raises(ValueError, match="never both"):
            TableScope(sample=0.1, filter="a > 1")

    @pytest.mark.parametrize(
        ("source", "quoted"),
        [(postgres_source, '"public"."t"'), (mysql_source, "`db`.`t`")],
        ids=["postgres", "mysql"],
    )
    def test_a_sampled_source_carries_no_predicate(self, source, quoted: str) -> None:
        assert "WHERE (" not in source(quoted, TableScope(sample=0.1))

    @pytest.mark.parametrize(
        ("source", "quoted"),
        [(postgres_source, '"public"."t"'), (mysql_source, "`db`.`t`")],
        ids=["postgres", "mysql"],
    )
    def test_a_filtered_source_draws_no_fraction(self, source, quoted: str) -> None:
        out = source(quoted, TableScope(filter="a > 1"))

        assert "TABLESAMPLE" not in out
        assert "RAND()" not in out

    def test_the_snowflake_filtered_source_draws_no_fraction(self) -> None:
        assert "SAMPLE" not in _snowflake(TableScope(filter="a > 1"))


class TestLooksLikePathEstimate:
    """The path decision reads the scoped size, not the table's."""

    @staticmethod
    def _scoped_estimate(module: str, scope: TableScope | None) -> float:
        import importlib

        mod = importlib.import_module(f"dbprint.adapters.{module}.looks_like")

        return mod._scoped_estimate(1_000_000, scope)

    @pytest.mark.parametrize("module", ["snowflake", "postgres", "mysql", "duckdb"])
    def test_a_sample_scales_the_estimate(self, module: str) -> None:
        """A fraction is arithmetic, so a sampled read can reach the cheap path."""

        assert self._scoped_estimate(module, TableScope(sample=0.001)) == 1_000.0

    @pytest.mark.parametrize("module", ["snowflake", "postgres", "mysql", "duckdb"])
    def test_a_filter_leaves_the_estimate_alone(self, module: str) -> None:
        """Nothing here estimates selectivity, so a predicate cannot shrink the figure."""

        assert self._scoped_estimate(module, TableScope(filter="a > 1")) == 1_000_000.0

    @pytest.mark.parametrize("module", ["snowflake", "postgres", "mysql", "duckdb"])
    def test_an_unscoped_read_keeps_the_whole_table(self, module: str) -> None:
        assert self._scoped_estimate(module, None) == 1_000_000.0


class TestPostgresSubDraw:
    """TABLESAMPLE takes a base table, so the sampler's draw composes with the scope."""

    def test_an_unscoped_draw_samples_the_base_table(self) -> None:
        source, conjunct = postgres_looks_like._sub_drawn_source(
            '"public"."t"',
            None,
            0.25,
            SUB_DRAW_SEED,
        )

        assert source == '"public"."t" TABLESAMPLE BERNOULLI(25.0) REPEATABLE (777)'
        assert conjunct == ""

    def test_the_sub_draw_shares_the_tables_seed(self) -> None:
        """BERNOULLI decides membership per row from the seed: a narrower rate only drops rows."""

        wide, _ = postgres_looks_like._sub_drawn_source('"public"."t"', None, 0.5, SUB_DRAW_SEED)
        narrow, _ = postgres_looks_like._sub_drawn_source('"public"."t"', None, 0.25, SUB_DRAW_SEED)

        assert f"REPEATABLE ({SUB_DRAW_SEED})" in wide
        assert f"REPEATABLE ({SUB_DRAW_SEED})" in narrow

    def test_a_sampled_scope_folds_into_one_rate(self) -> None:
        """Two fractions over one base table are one fraction, and it is their product."""

        source, conjunct = postgres_looks_like._sub_drawn_source(
            '"public"."t"',
            TableScope(sample=0.5),
            0.25,
            SUB_DRAW_SEED,
        )

        assert source == '"public"."t" TABLESAMPLE BERNOULLI(12.5) REPEATABLE (777)'
        assert conjunct == ""

    def test_a_filtered_scope_draws_with_a_predicate(self) -> None:
        """A predicate has wrapped the table, and no TABLESAMPLE can attach to that."""

        source, conjunct = postgres_looks_like._sub_drawn_source(
            '"public"."t"',
            TableScope(filter="a > 1"),
            0.25,
            SUB_DRAW_SEED,
        )

        assert source == '(SELECT * FROM "public"."t" WHERE a > 1) AS dbprint_scoped'
        assert "TABLESAMPLE" not in source
        assert conjunct == " AND random() < 0.25"
