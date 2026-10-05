"""Source expressions each adapter builds for a narrowed read (SPEC 2.2.8).

A scope carries a predicate or a fraction, never both, so each adapter has exactly two
shapes; where the fraction may bind differs per dialect, hence no shared query builder.
"""

from __future__ import annotations

import pytest

from dbprint.adapters import TableScope
from dbprint.adapters.base import seed_from_fqn
from dbprint.adapters.identifiers import Identity
from dbprint.adapters.mysql.stats import _source as mysql_source
from dbprint.adapters.postgres.stats import _source as postgres_source
from dbprint.adapters.snowflake import DIALECT as SNOWFLAKE_DIALECT
from dbprint.adapters.snowflake.stats import _source as snowflake_source
from dbprint.adapters.statements import scoped_estimate


def _snowflake(scope: TableScope | None, seed: int | None = None) -> str:
    return snowflake_source(Identity.of(("db", "sch", "t"), SNOWFLAKE_DIALECT), scope, seed)


class TestFullScan:
    @pytest.mark.parametrize("scope", [None, TableScope()], ids=["none", "empty"])
    def test_every_adapter_reads_the_bare_table(self, scope: TableScope | None) -> None:
        assert postgres_source('"public"."t"', scope) == '"public"."t" src'
        assert mysql_source("`db`.`t`", scope) == "`db`.`t` src"
        assert "SELECT" not in _snowflake(scope)


class TestSeededSample:
    """Every statement in one table's profile reads the same rows, or none do."""

    SEED = 12345

    def test_a_filtered_scope_carries_no_seed(self) -> None:
        """A predicate is already deterministic; there is no draw to repeat."""

        scope = TableScope(filter="a > 1")

        assert str(self.SEED) not in postgres_source('"public"."t"', scope, self.SEED)
        assert str(self.SEED) not in mysql_source("`db`.`t`", scope, self.SEED)
        assert str(self.SEED) not in _snowflake(scope, self.SEED)

    def test_an_unscoped_read_is_untouched_by_a_seed(self) -> None:
        assert postgres_source('"public"."t"', None, self.SEED) == '"public"."t" src'
        assert mysql_source("`db`.`t`", None, self.SEED) == "`db`.`t` src"
        assert _snowflake(None, self.SEED) == '"db"."sch"."t" src'

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

    def test_a_sample_scales_the_estimate(self) -> None:
        """A fraction is arithmetic, so a sampled read can reach the cheap path."""

        assert scoped_estimate(1_000_000, TableScope(sample=0.001)) == 1_000.0

    def test_a_filter_leaves_the_estimate_alone(self) -> None:
        """Nothing here estimates selectivity, so a predicate cannot shrink the figure."""

        assert scoped_estimate(1_000_000, TableScope(filter="a > 1")) == 1_000_000.0

    def test_an_unscoped_read_keeps_the_whole_table(self) -> None:
        assert scoped_estimate(1_000_000, None) == 1_000_000.0

    def test_no_catalog_estimate_routes_to_the_direct_read(self) -> None:
        assert scoped_estimate(None, TableScope(sample=0.5)) < 0
